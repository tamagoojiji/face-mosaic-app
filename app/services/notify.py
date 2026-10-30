"""LINE 通知4種（Messaging API の push）。DESIGN 3節「LINE通知の一覧」。

送れないとき（LINE_MSG_* 未設定・line_user_id なし・通知 OFF）は送らずに notify(skip) を1行ログに出す。
送れない人にも同じ内容がホームの一覧に出る（選択待ち・放置の警告・失敗の理由・完成）。
"""

import json
import logging
import urllib.request

from .. import config, db
from . import quota

log = logging.getLogger("uvicorn.error")
PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _link(path):
    if not config.APP_BASE_URL:
        return ""
    return f"\n{config.APP_BASE_URL}{path}?openExternalBrowser=1"


def message(kind, job, reason=None, text=None, link=None):
    if kind in ("plan_changed", "cancel_requested", "cancel_resumed", "subscribed"):
        return text + _link("/settings")
    if kind == "registered":  # 再登録でお試しが終わっている人だけ link="/plans"
        return text + _link(link or "/home")
    if kind == "withdrawn":  # 退会後は開ける画面がないのでリンクなし
        return text
    if kind == "payment_failed":
        return "お支払いが確認できません。7日以内にカード情報を更新してください。" + _link("/settings")
    jid = job["id"]
    if kind == "ready":
        return "選択の準備ができました。残したい顔をタップしてください。" + _link(f"/pick/{jid}")
    if kind == "done":
        return f"できました。{quota.output_days():g}日間はいつでも保存できます。" + _link(f"/done/{jid}")
    if kind == "idle":
        page = "pick" if job["status"] == "awaiting_pick" else "review"
        left = quota.idle_delete_hours() - quota.idle_warn_hours()
        return f"まだ選択が残っています。あと{left:g}時間で動画を削除します。" + _link(f"/{page}/{jid}")
    if kind == "failed":
        refunded = "消費した分数は戻しました。" if job.get("_refunded") else ""
        return f"処理できませんでした（{reason or '理由不明'}）。{refunded}" + _link("/home")
    raise ValueError(kind)


def push(kind, job, reason=None):
    """kind: ready（①選択の準備）| done（②できました）| failed（③処理できなかった）| idle（④1時間選択なし）"""
    return push_user(kind, db.get_user(job.get("user_id")), job, reason)


def push_user(kind, user, job=None, reason=None, text=None, link=None):
    """利用者に直接送る（payment_failed・plan_changed・cancel_requested・cancel_resumed・withdrawn・subscribed・registered はジョブなし。payment_failed 以外は本文を text で渡す）。"""
    jid = job["id"] if job else "-"
    uid = user["id"] if user else (job or {}).get("user_id")
    skip = None
    if kind == "done" and quota.setting("notify_done") != "1":
        skip = "完成通知OFF"
    elif not config.LINE_MSG_CHANNEL_ACCESS_TOKEN:
        skip = "LINE_MSG未設定"
    elif not user or not user.get("line_user_id"):
        skip = "line_user_idなし"
    elif not user.get("notify"):
        skip = "通知OFF"
    elif user.get("status") != "active":
        skip = "利用者が無効"
    if skip:
        log.info("notify(skip) kind=%s job=%s user=%s reason=%s", kind, jid, uid, skip)
        return False
    body = json.dumps({"to": user["line_user_id"],
                       "messages": [{"type": "text", "text": message(kind, job, reason, text, link)}]}).encode()
    req = urllib.request.Request(PUSH_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + config.LINE_MSG_CHANNEL_ACCESS_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            log.info("notify(sent) kind=%s job=%s user=%s status=%s", kind, jid, user["id"], r.status)
        return True
    except Exception as e:  # 通知の失敗で処理は止めない（画面にも同じ内容が出る）
        log.warning("notify 失敗 kind=%s job=%s: %s", kind, jid, type(e).__name__)
        return False
