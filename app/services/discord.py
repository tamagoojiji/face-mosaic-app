"""Discord Webhook（運営通知 3チャンネル）。未設定なら送らずに discord(skip) を1行ログに出す。

#mosaic-alerts  処理失敗・アプリの例外・worker 停止・ディスク逼迫
#mosaic-reports 通報（③a では送信関数だけ）
#mosaic-usage   新規登録・Instagram 名の登録・退会（理由つき）・日次サマリ
"""

import json
import logging
import threading
import urllib.request

from .. import config

log = logging.getLogger("uvicorn.error")
CHANNELS = {"alerts": "DISCORD_WEBHOOK_ALERTS", "reports": "DISCORD_WEBHOOK_REPORTS", "usage": "DISCORD_WEBHOOK_USAGE"}


def _post(url, text):
    body = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "face-mosaic-app"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def send(channel, text, wait=False):
    url = getattr(config, CHANNELS[channel])
    if not url:
        log.info("discord(skip) channel=%s reason=未設定 text=%s", channel, text.splitlines()[0][:80])
        return False

    def run():
        try:
            _post(url, text)
        except Exception as e:  # 通知の失敗で本処理は止めない（URL はログに出さない）
            log.warning("discord 送信失敗 channel=%s: %s", channel, type(e).__name__)

    if wait:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()
    return True


def alert(text, wait=False):
    return send("alerts", "【要対応】" + text, wait)


def report(text, wait=False):
    return send("reports", "【通報】" + text, wait)


def usage(text, wait=False):
    return send("usage", text, wait)


def who(user):
    if not user:
        return "（不明）"
    ig = f" @{user['instagram_username']}" if user.get("instagram_username") else ""
    return f"#{user['id']} {user.get('display_name') or 'ゲスト'}{ig}"
