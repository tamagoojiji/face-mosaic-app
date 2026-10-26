"""③b 段①: Stripe Webhook（署名検証・状態遷移・冪等）と利用停止ゲート。ネットワークは使わない。"""

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="fm-test-")
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
for k in ("LINE_MSG_CHANNEL_ACCESS_TOKEN", "DISCORD_WEBHOOK_ALERTS", "DISCORD_WEBHOOK_REPORTS", "DISCORD_WEBHOOK_USAGE",
          "APP_BASE_URL"):
    os.environ.pop(k, None)

import pytest  # noqa: E402
import stripe  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import quota  # noqa: E402

SECRET = "whsec_test"
PRICES = {"image": "price_img", "video_a": "price_va", "video_b": "price_vb", "hq": "price_hq"}


def sign(payload):
    ts = int(time.time())
    v1 = hmac.new(SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


def event(eid, typ, obj):
    return {"id": eid, "object": "event", "type": typ, "data": {"object": obj}}


def post(client, ev, sig=None):
    payload = json.dumps(ev)
    return client.post("/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": sig or sign(payload), "Content-Type": "application/json"})


def sub_obj(items, status="active", cape=False, end=None):
    end = end or time.time() + 30 * 86400
    return {"id": "sub_1", "object": "subscription", "customer": "cus_1", "status": status,
            "cancel_at_period_end": cape,
            "items": {"data": [{"id": f"si_{p}", "price": {"id": p}, "current_period_end": end} for p in items]}}


@pytest.fixture(scope="module")
def client():
    db.init()
    for k, v in PRICES.items():
        db.set_setting(f"stripe_price_{k}", v)
    return TestClient(app)


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda sid, **kw: stripe.StripeObject.construct_from(
                            sub_obj([PRICES["video_b"], PRICES["hq"]]), "k"))
    monkeypatch.setattr(stripe.Subscription, "modify", lambda sid, **kw: log.append(("modify", sid, kw)))
    monkeypatch.setattr(stripe.Subscription, "cancel", lambda sid, **kw: log.append(("cancel", sid, kw)))
    return log


def user():
    return db.user_by("line_user_id", "Utest1")


def test_flow(client, calls, monkeypatch):
    from app.services import notify
    pushed = []
    monkeypatch.setattr(notify, "push_user", lambda kind, user, *a, **kw: pushed.append((kind, kw.get("text"))))
    u = db.create_user(line_user_id="Utest1", display_name="テスト", trial_ends_at=time.time() + 86400)

    # (a) 署名NG → 400
    ev = event("evt_a", "checkout.session.completed", {})
    assert post(client, ev, sig="t=1,v1=deadbeef").status_code == 400

    # (b) checkout.session.completed → video_a・active
    ev = event("evt_b", "checkout.session.completed",
               {"id": "cs_1", "object": "checkout.session", "customer": "cus_1", "subscription": "sub_1",
                "client_reference_id": str(u["id"]), "metadata": {"user_id": str(u["id"]), "plan": "video_a", "hq": "0"}})
    r = post(client, ev)
    assert r.status_code == 200 and r.json() == {"ok": True}
    v = user()
    assert v["plan"] == "video_a" and v["sub_status"] == "active" and v["hq"] == 0
    assert v["stripe_subscription_id"] == "sub_1" and v["stripe_customer_id"] == "cus_1"
    assert v["current_period_end"] and v["stripe_price_plan"] == PRICES["video_a"]
    assert quota.blocked(v) is None
    sub_msgs = [t for k, t in pushed if k == "subscribed"]
    assert len(sub_msgs) == 1 and sub_msgs[0].startswith("お申し込みありがとうございます。動画Aが使えるようになりました。")
    assert "月980円です。" in sub_msgs[0]

    # (f) 同じ event_id の2回目は何もしない
    db.update_user(u["id"], plan="image")
    assert post(client, ev).status_code == 200
    assert user()["plan"] == "image"
    db.update_user(u["id"], plan="video_a")

    # (c) invoice.payment_failed → past_due・past_due_since
    inv = {"id": "in_1", "object": "invoice", "customer": "cus_1",
           "parent": {"subscription_details": {"subscription": "sub_1"}}, "billing_reason": "subscription_cycle"}
    assert post(client, event("evt_c", "invoice.payment_failed", inv)).status_code == 200
    v = user()
    assert v["sub_status"] == "past_due" and v["past_due_since"]
    assert quota.blocked(v) == "past_due"

    # (d) invoice.paid → active・pending（下位変更・高画質外し）を適用
    db.update_user(u["id"], plan="video_b", hq=1, pending_plan="video_a", pending_hq=0)
    assert post(client, event("evt_d", "invoice.paid", inv)).status_code == 200
    v = user()
    assert v["sub_status"] == "active" and v["past_due_since"] is None
    assert v["plan"] == "video_a" and v["hq"] == 0 and v["pending_plan"] is None and v["pending_hq"] is None
    mods = [c for c in calls if c[0] == "modify"]
    assert mods and mods[-1][2]["proration_behavior"] == "none"
    assert {"id": "si_" + PRICES["hq"], "deleted": True} in mods[-1][2]["items"]
    assert {"id": "si_" + PRICES["video_b"], "price": PRICES["video_a"]} in mods[-1][2]["items"]

    # customer.subscription.updated（期末解約）→ canceling
    ev = event("evt_u", "customer.subscription.updated", sub_obj([PRICES["video_a"]], cape=True))
    assert post(client, ev).status_code == 200
    assert user()["sub_status"] == "canceling" and user()["cancel_at_period_end"] == 1

    # (e) customer.subscription.deleted → 自動退会（sub_ended）・Stripe の解約は呼ばない
    ev = event("evt_e", "customer.subscription.deleted", sub_obj([PRICES["video_a"]], status="canceled"))
    assert post(client, ev).status_code == 200
    v = db.get_user(u["id"])
    assert v["status"] == "withdrawn" and v["withdraw_reason"] == "sub_ended"
    assert not [c for c in calls if c[0] == "cancel"]

    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM billing_events").fetchone()[0] == 5  # b c d u e（署名NG の a は記録しない）
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'billing.%'").fetchone()[0] >= 5


def _sub_user(line_id):
    u = db.create_user(line_user_id=line_id, display_name="変更", trial_ends_at=time.time() + 86400)
    db.update_user(u["id"], plan="video_a", hq=0, stripe_customer_id="cus_2", stripe_subscription_id="sub_2",
                   stripe_price_plan=PRICES["video_a"], sub_status="active")
    return db.get_user(u["id"])


@pytest.fixture
def up_stripe(monkeypatch):
    log = []
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda sid, **kw: stripe.StripeObject.construct_from(sub_obj([PRICES["video_a"]]), "k"))
    monkeypatch.setattr(stripe.Subscription, "modify", lambda sid, **kw: log.append(("modify", sid, kw)))
    monkeypatch.setattr(stripe.Invoice, "list", lambda **kw: stripe.StripeObject.construct_from(
        {"object": "list", "data": [{"id": "in_up", "object": "invoice", "billing_reason": "subscription_update",
                                     "amount_paid": 499}]}, "k"))
    return log


def test_change_up_charges_now(client, up_stripe):
    from app.services import billing
    u = _sub_user("Uchg1")
    done = billing.change(u, "video_b", False)
    mod = up_stripe[-1]
    assert mod[0] == "modify" and mod[1] == "sub_2"
    assert mod[2]["proration_behavior"] == "always_invoice"
    assert mod[2]["payment_behavior"] == "error_if_incomplete"
    assert mod[2]["items"] == [{"id": "si_" + PRICES["video_a"], "price": PRICES["video_b"]}]
    assert done["charged"] == 499 and done["invoice_id"] == "in_up" and done["now"] == ["plan"]
    assert done["monthly"] == quota.PLANS["video_b"]["price"]
    assert db.get_user(u["id"])["plan"] == "video_b"


def test_change_card_declined_keeps_db(client, up_stripe, monkeypatch):
    from app.services import billing

    def declined(sid, **kw):
        raise stripe.CardError("Your card was declined.", None, "card_declined")
    monkeypatch.setattr(stripe.Subscription, "modify", declined)
    u = _sub_user("Uchg2")
    before = dict(db.get_user(u["id"]))
    with pytest.raises(billing.PaymentFailed) as e:
        billing.change(u, "video_b", True)
    assert "お支払いができませんでした" in str(e.value)
    assert dict(db.get_user(u["id"])) == before


def test_preview_shape(client, up_stripe, monkeypatch):
    from app.services import billing
    seen = []

    def fake_preview(**kw):
        seen.append(kw)
        return stripe.StripeObject.construct_from({"object": "invoice", "lines": {"object": "list", "data": [
            {"amount": -490, "parent": {"subscription_item_details": {"proration": True}}},
            {"amount": 740, "parent": {"subscription_item_details": {"proration": True}}},
            {"amount": 1480, "parent": {"subscription_item_details": {"proration": False}}},
        ]}}, "k")
    monkeypatch.setattr(stripe.Invoice, "create_preview", fake_preview)
    u = _sub_user("Uchg3")
    pv = billing.preview(u, "video_b", True)
    assert set(pv) == {"now", "monthly", "next_date", "immediate", "deferred"}
    assert pv["now"] == 250 and pv["monthly"] == 1480 + 500
    assert pv["immediate"] == ["plan", "hq"] and pv["deferred"] == [] and pv["next_date"]
    sd = seen[-1]["subscription_details"]
    assert sd["proration_behavior"] == "always_invoice" and seen[-1]["customer"] == "cus_2"
    # 下位へは請求なし（プレビューも呼ばない）
    n = len(seen)
    pv = billing.preview(u, "image", False)
    assert pv["now"] == 0 and pv["deferred"] == ["plan"] and pv["immediate"] == [] and len(seen) == n


def test_cancel_button(client, up_stripe, monkeypatch):
    import app.main as main_mod
    u = _sub_user("Ucan1")
    end = time.time() + 20 * 86400
    db.update_user(u["id"], current_period_end=end)
    monkeypatch.setattr(main_mod, "require_api", lambda request: db.get_user(u["id"]))
    r = client.post("/api/billing/cancel")
    assert r.status_code == 200 and r.json() == {"ok": True, "until": end}
    mod = up_stripe[-1]
    assert mod[0] == "modify" and mod[1] == "sub_2" and mod[2] == {"cancel_at_period_end": True}
    v = db.get_user(u["id"])
    assert v["sub_status"] == "canceling" and v["cancel_at_period_end"] == 1
    # 解約処理中にもう一度 → 400
    r = client.post("/api/billing/cancel")
    assert r.status_code == 400 and r.json()["detail"] == "すでに解約処理中です"
    assert len(up_stripe) == 1


def test_resume_button(client, up_stripe, monkeypatch):
    import app.main as main_mod
    u = _sub_user("Ures1")
    end = time.time() + 20 * 86400
    db.update_user(u["id"], current_period_end=end, sub_status="canceling", cancel_at_period_end=1)
    monkeypatch.setattr(main_mod, "require_api", lambda request: db.get_user(u["id"]))
    r = client.post("/api/billing/resume")
    assert r.status_code == 200 and r.json() == {"ok": True, "next": end}
    assert up_stripe[-1] == ("modify", "sub_2", {"cancel_at_period_end": False})
    v = db.get_user(u["id"])
    assert v["sub_status"] == "active" and v["cancel_at_period_end"] == 0
    # active のとき → 400
    r = client.post("/api/billing/resume")
    assert r.status_code == 400 and r.json()["detail"] == "解約処理中ではありません"
    assert len(up_stripe) == 1


def test_withdraw_notifies_before_status_change(client, monkeypatch):
    import app.main as main_mod
    u = _sub_user("Uwd1")
    db.update_user(u["id"], sub_status="canceling", cancel_at_period_end=1, current_period_end=time.time(),
                   notify=1)
    sent = []
    monkeypatch.setattr(main_mod.notify, "push_user",
                        lambda kind, user, *a, **kw: sent.append((kind, db.get_user(user["id"])["status"], kw.get("text"))))
    main_mod.withdraw_user(db.get_user(u["id"]), "sub_ended", cancel=False)
    assert sent and sent[0][0] == "withdrawn" and sent[0][1] == "active"
    assert sent[0][2].startswith("更新日になったため退会となりました。")
    v = db.get_user(u["id"])
    assert v["status"] == "withdrawn" and v["sub_status"] is None and v["stripe_subscription_id"] is None
    assert v["cancel_at_period_end"] == 0 and v["current_period_end"] is None
    assert v["stripe_customer_id"] == "cus_2"


def test_blocked():
    now = time.time()
    base = {"plan": "free", "line_user_id": "U1", "role": "user", "sub_status": None}
    assert quota.blocked(dict(base, trial_ends_at=now - 1)) == "trial_expired"
    assert quota.blocked(dict(base, trial_ends_at=now + 3600)) is None
    assert quota.blocked(dict(base, trial_ends_at=now - 1, role="admin")) is None
    assert quota.blocked({"plan": "video_b", "line_user_id": None, "device_id": "d", "role": "user",
                          "trial_ends_at": None, "sub_status": None}) is None
    assert quota.blocked(dict(base, plan="free", line_user_id=None, trial_ends_at=now - 1)) is None
    assert quota.check_image(dict(base, id=999, trial_ends_at=now - 1)).startswith("お試し期間が終わりました")


def test_trial_migration():
    with db.connect() as con:
        con.execute("INSERT INTO users (line_user_id, display_name, plan, created_at) VALUES ('Umig', 'x', 'free', ?)",
                    (1000.0,))
        con.execute("INSERT INTO users (line_user_id, display_name, plan, role, created_at) "
                    "VALUES ('Uadm', 'x', 'free', 'admin', ?)", (1000.0,))
    db.init()
    assert db.user_by("line_user_id", "Umig")["trial_ends_at"] == 1000.0 + 7 * 86400
    assert db.user_by("line_user_id", "Uadm")["trial_ends_at"] is None
