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


def test_flow(client, calls):
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
