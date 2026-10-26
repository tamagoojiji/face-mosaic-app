"""Stripe（月額サブスク・お試し後の契約・決済失敗）。DESIGN 2節「契約・解約・決済失敗の条件」。

価格ID・ポータル設定IDは settings（stripe_price_image|video_a|video_b|hq・stripe_portal_config）。
鍵が空なら Stripe を呼ばずに billing(skip) を1行ログに出して None を返す（notify/discord と同じ流儀）。
プラン変更・高画質の ON/OFF・解約はアプリ内で実行（ポータルはカード変更・請求履歴だけ）。
"""

import datetime
import json
import logging
import time
import zoneinfo

import stripe

from .. import config, db
from . import discord, notify, quota

log = logging.getLogger("uvicorn.error")
PRICE_KEYS = ("image", "video_a", "video_b", "hq")
GRACE_SEC = 7 * 86400  # 決済失敗の猶予
JST = zoneinfo.ZoneInfo("Asia/Tokyo")


def _d(obj):
    """StripeObject → dict（SDK v8 以降は dict ではない）。"""
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


def _id(v):
    return v if isinstance(v, str) or v is None else v.get("id")


def _skip(what, user=None):
    log.info("billing(skip) %s user=%s reason=鍵が未設定", what, user["id"] if user else "-")
    return None


def _api():
    if not config.STRIPE_SECRET_KEY:
        return False
    stripe.api_key = config.STRIPE_SECRET_KEY
    return True


def price_ids():
    return {k: db.get_setting(f"stripe_price_{k}") or "" for k in PRICE_KEYS}


def ready():
    """申し込みを受け付けられるか（鍵2つ＋価格ID 4つ）。"""
    return config.STRIPE_READY and all(price_ids().values())


def _plan_of(items):
    """購読のライン → (plan, hq, plan の価格ID)。知らない価格だけなら plan=None。"""
    by_price = {v: k for k, v in price_ids().items() if v}
    plan, hq, plan_price = None, False, None
    for it in items:
        pid = (it.get("price") or {}).get("id")
        k = by_price.get(pid)
        if k == "hq":
            hq = True
        elif k:
            plan, plan_price = k, pid
    return plan, hq, plan_price


def _items(sub):
    return ((sub.get("items") or {}).get("data")) or []


def _period_end(sub):
    """次の更新日（API 2025-03 以降はライン側にある）。"""
    if sub.get("current_period_end"):
        return float(sub["current_period_end"])
    ends = [it["current_period_end"] for it in _items(sub) if it.get("current_period_end")]
    return float(min(ends)) if ends else None


def _invoice_sub(inv):
    if inv.get("subscription"):
        return _id(inv["subscription"])
    det = ((inv.get("parent") or {}).get("subscription_details")) or {}
    return _id(det.get("subscription"))


# ---------------------------------------------------------------- 利用者の操作

def checkout_url(user, plan, hq, base):
    """無料 → 有料の申し込み（Checkout Session・月額）。顧客は既存を再利用、なければ Checkout が作る。"""
    if not _api():
        return _skip("checkout", user)
    ids = price_ids()
    items = [{"price": ids[plan], "quantity": 1}]
    if hq:
        items.append({"price": ids["hq"], "quantity": 1})
    meta = {"user_id": str(user["id"]), "plan": plan, "hq": "1" if hq else "0"}
    params = {"mode": "subscription", "line_items": items, "client_reference_id": str(user["id"]),
              "metadata": meta, "subscription_data": {"metadata": meta},
              "success_url": base + "/billing/return?ok=1", "cancel_url": base + "/plans"}
    if user.get("stripe_customer_id"):
        params["customer"] = user["stripe_customer_id"]
    s = _d(stripe.checkout.Session.create(**params))
    db.audit(user["id"], "billing.checkout_start", f"user:{user['id']}", json.dumps({"plan": plan, "hq": int(hq)}))
    return s["url"]


def portal_url(user, base):
    """カード変更・解約（Customer Portal）。"""
    if not _api():
        return _skip("portal", user)
    params = {"customer": user["stripe_customer_id"], "return_url": base + "/settings"}
    cfg = db.get_setting("stripe_portal_config")
    if cfg:
        params["configuration"] = cfg
    return _d(stripe.billing_portal.Session.create(**params))["url"]


class PaymentFailed(Exception):
    """差額の即時請求でカードが通らなかった（購読もDBも変えていない）。"""


def _monthly(plan, hq):
    return quota.PLANS[plan]["price"] + (quota.HQ_OPTION["price"] if hq else 0)


def _plan_text(plan, hq):
    return quota.PLANS[plan]["label"] + ("＋高画質" if hq else "")


def _plan_diff(user, plan, hq):
    """いまの購読と選んだ内容の差。即時に差し替えるライン items（上位へ・高画質追加）を返す。"""
    order = quota.PLAN_ORDER
    cur_plan, cur_hq = user["plan"], bool(user["hq"])
    plan_up = order.index(plan) > order.index(cur_plan)
    plan_down = order.index(plan) < order.index(cur_plan)
    hq_on, hq_off = hq and not cur_hq, cur_hq and not hq
    ids = price_ids()
    sub = _d(stripe.Subscription.retrieve(user["stripe_subscription_id"]))
    plan_item = hq_item = None
    for it in _items(sub):
        if (it.get("price") or {}).get("id") == ids["hq"]:
            hq_item = it
        else:
            plan_item = it
    items = []
    if plan_up and plan_item:
        items.append({"id": plan_item["id"], "price": ids[plan]})
    if hq_on and not hq_item:
        items.append({"price": ids["hq"], "quantity": 1})
    return {"sub": sub, "items": items, "plan_up": plan_up, "plan_down": plan_down, "hq_on": hq_on,
            "hq_off": hq_off, "immediate": (["plan"] if plan_up else []) + (["hq"] if hq_on else []),
            "deferred": (["plan"] if plan_down else []) + (["hq"] if hq_off else [])}


def _is_proration(line):
    """日割り行か（SDK の create_preview 説明: parent.subscription_item_details.proration が真の行）。"""
    parent = line.get("parent") or {}
    det = parent.get("subscription_item_details") or parent.get("invoice_item_details") or {}
    return bool(det.get("proration") or line.get("proration"))


def preview(user, plan, hq):
    """押す前に見せる金額。now=いま請求する差額（円）・monthly=変更後の月額。"""
    if not _api():
        return _skip("preview", user)
    diff = _plan_diff(user, plan, hq)
    now = 0
    if diff["items"]:
        inv = _d(stripe.Invoice.create_preview(
            customer=user["stripe_customer_id"], subscription=user["stripe_subscription_id"],
            subscription_details={"items": diff["items"], "proration_behavior": "always_invoice"}))
        lines = ((inv.get("lines") or {}).get("data")) or []
        now = sum(int(ln.get("amount") or 0) for ln in lines if _is_proration(ln))
    return {"now": now, "monthly": _monthly(plan, hq), "next_date": _period_end(diff["sub"]),
            "immediate": diff["immediate"], "deferred": diff["deferred"]}


def _last_update_invoice(sub_id):
    invs = _d(stripe.Invoice.list(subscription=sub_id, limit=1))
    data = (invs.get("data") if isinstance(invs, dict) else None) or []
    if data and data[0].get("billing_reason") == "subscription_update":
        return data[0]
    return None


def change(user, plan, hq):
    """有料プラン間の変更。上位へ・高画質追加は即時（差額をその場で登録カードに請求）、
    下位へ・高画質外しは次の更新日（pending・請求なし）。カードが通らなければ PaymentFailed（何も変えない）。

    返り値: {"now": [...], "next": [...], "charged": 円, "invoice_id": str|None, "monthly": 円}
    """
    if not _api():
        return _skip("change", user)
    cur_plan, cur_hq = user["plan"], bool(user["hq"])
    ids = price_ids()
    diff = _plan_diff(user, plan, hq)
    plan_up, plan_down, hq_on, hq_off = diff["plan_up"], diff["plan_down"], diff["hq_on"], diff["hq_off"]
    fields, done = {}, {"now": [], "next": []}
    charged, invoice_id = 0, None
    if diff["items"]:
        try:
            stripe.Subscription.modify(user["stripe_subscription_id"], items=diff["items"],
                                       proration_behavior="always_invoice", payment_behavior="error_if_incomplete")
        except (stripe.CardError, stripe.InvalidRequestError) as e:
            log.warning("billing 差額請求失敗 user=%s: %s %s", user["id"], type(e).__name__, getattr(e, "code", ""))
            raise PaymentFailed("お支払いができませんでした。カードを確認して、もう一度お試しください") from e
        inv = _last_update_invoice(user["stripe_subscription_id"])
        if inv:
            charged, invoice_id = int(inv.get("amount_paid") or 0), inv.get("id")
    if plan_up:
        fields.update(plan=plan, stripe_price_plan=ids[plan])
        done["now"].append("plan")
    if hq_on:
        fields["hq"] = 1
        done["now"].append("hq")
    # 下位・外しは次の更新日（invoice.paid）で適用。いまと同じを選び直したら予約を取り消す
    fields["pending_plan"] = plan if plan_down else None
    fields["pending_hq"] = 0 if hq_off else None
    if plan_down:
        done["next"].append("plan")
    if hq_off:
        done["next"].append("hq")
    db.update_user(user["id"], **fields)
    monthly = _monthly(plan, hq)
    db.audit(user["id"], "billing.change", f"user:{user['id']}",
             json.dumps({"from": [cur_plan, int(cur_hq)], "to": [plan, int(hq)], **done,
                         "charged": charged, "invoice": invoice_id}))
    if done["now"] or done["next"]:
        discord.usage(f"プラン変更: {discord.who(user)} {cur_plan}{'+高画質' if cur_hq else ''} → "
                      f"{plan}{'+高画質' if hq else ''}（即時: {','.join(done['now']) or 'なし'}・"
                      f"次の更新日: {','.join(done['next']) or 'なし'}・請求 {charged}円）")
    if done["now"]:
        notify.push_user("plan_changed", db.get_user(user["id"]),
                         text=f"プランを {_plan_text(plan, hq)} に変えました。今回の請求 {charged:,}円。"
                              f"次回から 月{monthly:,}円です。")
    return dict(done, charged=charged, invoice_id=invoice_id, monthly=monthly)


def cancel_subscription(user):
    """退会時に呼ぶ。購読を即時解約（返金なし）。"""
    sub_id = user.get("stripe_subscription_id")
    if not sub_id:
        return None
    if not _api():
        return _skip("cancel", user)
    try:
        stripe.Subscription.cancel(sub_id, prorate=False)
    except Exception as e:  # 解約できなくても退会は進める（運営が手で止める）
        log.warning("billing 解約失敗 user=%s: %s", user["id"], type(e).__name__)
        discord.alert(f"退会時の購読解約に失敗しました: {discord.who(user)}（Stripe で手動解約が必要）")
        return False
    db.audit(None, "billing.cancel", f"user:{user['id']}", None)
    return True


def _md(ts):
    t = datetime.datetime.fromtimestamp(ts, JST)
    return f"{t.month}月{t.day}日"


def cancel_at_period_end(user):
    """設定の「解約する」: 次の更新日で終わらせる（それまでは通常どおり・更新日に deleted で自動退会・返金なし）。"""
    if not _api():
        return _skip("cancel_at_period_end", user)
    stripe.Subscription.modify(user["stripe_subscription_id"], cancel_at_period_end=True)
    db.update_user(user["id"], sub_status="canceling", cancel_at_period_end=1)
    until = user.get("current_period_end")
    db.audit(user["id"], "billing.cancel_requested", f"user:{user['id']}", json.dumps({"until": until}))
    discord.usage(f"解約処理中: {discord.who(user)}")
    day = _md(until) if until else "次の更新日"
    notify.push_user("cancel_requested", db.get_user(user["id"]),
                     text=f"解約を受け付けました。{day}までは今のまま使えます。更新日に自動で退会となり、返金はありません。")
    return {"until": until}


def schedule_cancel(user):
    """決済失敗の猶予（7日）切れ: 次の更新日で終わらせる（deleted で自動退会）。"""
    if not _api():
        return _skip("schedule_cancel", user)
    stripe.Subscription.modify(user["stripe_subscription_id"], cancel_at_period_end=True)
    db.update_user(user["id"], sub_status="canceling", cancel_at_period_end=1)
    db.audit(None, "billing.past_due_expired", f"user:{user['id']}", None)
    discord.usage(f"お支払い未確認のまま猶予終了→解約処理中: {discord.who(user)}")
    return True


# ---------------------------------------------------------------- Webhook

def _find_user(sub_id=None, customer_id=None):
    u = db.user_by("stripe_subscription_id", sub_id) if sub_id else None
    if not u and customer_id:
        u = db.user_by("stripe_customer_id", customer_id)
    return u


def apply_event(event, withdraw):
    """Webhook の中身を users に反映。event は署名検証済みの dict。withdraw(u, reason, cancel=False) は退会処理。

    返り値: 処理した type。同じ event_id の2回目は None（何もしない）。
    """
    eid, typ = event["id"], event["type"]
    if db.billing_event_seen(eid):
        log.info("billing 重複イベント %s %s", eid, typ)
        return None
    handler = HANDLERS.get(typ)
    if handler:
        handler(event["data"]["object"], withdraw)
    db.add_billing_event(eid, typ)
    return typ


def _on_checkout(s, _withdraw):
    meta = s.get("metadata") or {}
    uid = str(meta.get("user_id") or s.get("client_reference_id") or "")
    u = db.get_user(int(uid)) if uid.isdigit() else None
    if not u:
        log.warning("billing checkout: 利用者が見つかりません")
        return
    plan = meta.get("plan") if meta.get("plan") in quota.PAID else u["plan"]
    hq = 1 if meta.get("hq") == "1" and plan in quota.HQ_OPTION["plans"] else 0
    sub_id = _id(s.get("subscription"))
    period_end = None
    if sub_id and _api():
        period_end = _period_end(_d(stripe.Subscription.retrieve(sub_id)))
    db.update_user(u["id"], stripe_customer_id=_id(s.get("customer")), stripe_subscription_id=sub_id, plan=plan, hq=hq,
                   stripe_price_plan=price_ids().get(plan) or None, sub_status="active", cancel_at_period_end=0,
                   past_due_since=None, pending_plan=None, pending_hq=None, current_period_end=period_end)
    db.audit(None, "billing.checkout_completed", f"user:{u['id']}", json.dumps({"plan": plan, "hq": hq}))
    discord.usage(f"有料プラン契約: {discord.who(u)} {plan}{'+高画質' if hq else ''}")


def _on_sub_updated(sub, _withdraw):
    u = _find_user(sub.get("id"), _id(sub.get("customer")))
    if not u:
        return
    cape = bool(sub.get("cancel_at_period_end"))
    fields = {"current_period_end": _period_end(sub), "cancel_at_period_end": 1 if cape else 0}
    st = sub.get("status")
    if cape:
        fields["sub_status"] = "canceling"
    elif st in ("past_due", "unpaid"):
        fields.update(sub_status="past_due", past_due_since=u["past_due_since"] or time.time())
    elif st in ("active", "trialing"):
        fields["sub_status"] = "active"
    plan, hq, plan_price = _plan_of(_items(sub))
    if plan:
        fields.update(plan=plan, hq=1 if hq and plan in quota.HQ_OPTION["plans"] else 0, stripe_price_plan=plan_price)
    db.update_user(u["id"], **fields)
    db.audit(None, "billing.subscription_updated", f"user:{u['id']}",
             json.dumps({"status": fields.get("sub_status"), "plan": fields.get("plan"), "hq": fields.get("hq")}))
    if fields.get("sub_status") == "canceling" and u["sub_status"] != "canceling":
        discord.usage(f"解約処理中: {discord.who(u)}")


def _on_sub_deleted(sub, withdraw):
    u = _find_user(sub.get("id"), _id(sub.get("customer")))
    if not u or u["status"] != "active":  # 本人の退会で解約したときは済んでいる
        return
    reason = "past_due_expired" if u["past_due_since"] else "sub_ended"
    db.audit(None, "billing.subscription_deleted", f"user:{u['id']}", json.dumps({"reason": reason}))
    withdraw(u, reason, cancel=False)


def _on_payment_failed(inv, _withdraw):
    u = _find_user(_invoice_sub(inv), _id(inv.get("customer")))
    if not u:
        return
    db.update_user(u["id"], sub_status="past_due", past_due_since=u["past_due_since"] or time.time())
    db.audit(None, "billing.payment_failed", f"user:{u['id']}", None)
    notify.push_user("payment_failed", db.get_user(u["id"]))
    discord.usage(f"お支払い失敗（猶予7日・利用停止）: {discord.who(u)}")


def _on_paid(inv, _withdraw):
    u = _find_user(_invoice_sub(inv), _id(inv.get("customer")))
    if not u:
        return
    fields = {"past_due_since": None}
    if u["sub_status"] == "past_due":
        fields["sub_status"] = "active"
    # 下位変更・高画質外しの予約は更新時（subscription_cycle）に適用
    if inv.get("billing_reason") == "subscription_cycle" and (u["pending_plan"] or u["pending_hq"] is not None):
        fields.update(_apply_pending(u))
    db.update_user(u["id"], **fields)
    db.audit(None, "billing.invoice_paid", f"user:{u['id']}",
             json.dumps({"reason": inv.get("billing_reason"), "plan": fields.get("plan"), "hq": fields.get("hq")}))
    if u["sub_status"] == "past_due":
        discord.usage(f"お支払い確認（利用再開）: {discord.who(u)}")


def _apply_pending(u):
    plan = u["pending_plan"] or u["plan"]
    hq = (bool(u["hq"]) if u["pending_hq"] is None else bool(u["pending_hq"])) and plan in quota.HQ_OPTION["plans"]
    ids = price_ids()
    if _api() and u["stripe_subscription_id"]:
        sub = _d(stripe.Subscription.retrieve(u["stripe_subscription_id"]))
        items = []
        for it in _items(sub):
            pid = (it.get("price") or {}).get("id")
            if pid == ids["hq"]:
                if not hq:
                    items.append({"id": it["id"], "deleted": True})
            elif pid != ids[plan]:
                items.append({"id": it["id"], "price": ids[plan]})
        if items:
            stripe.Subscription.modify(u["stripe_subscription_id"], items=items, proration_behavior="none")
    else:
        _skip("apply_pending", u)
    discord.usage(f"プラン変更（更新日に適用）: {discord.who(u)} → {plan}{'+高画質' if hq else ''}")
    return {"plan": plan, "hq": 1 if hq else 0, "stripe_price_plan": ids.get(plan) or None,
            "pending_plan": None, "pending_hq": None}


HANDLERS = {
    "checkout.session.completed": _on_checkout,
    "customer.subscription.updated": _on_sub_updated,
    "customer.subscription.deleted": _on_sub_deleted,
    "invoice.payment_failed": _on_payment_failed,
    "invoice.paid": _on_paid,
}
