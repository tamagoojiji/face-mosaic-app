"""#mosaic-alerts の新しいアラームを読む／返信する（Mac の自動修正 alert-autofix.sh から使う）。

  new             前回より新しい【要対応】を JSON {"last", "alerts"} で出す（初回は今の最新を既読にして空を出す）
  ack <id>        <id> までを既読にする
  reply <id> <file>  <id> のアラームに返信する（本文はファイルから・1900字まで）

Bot トークンは discord-mcp と同じ ~/.config/discord-mcp/ の設定から読む（画面には出さない）。
実行は discord-mcp の venv の python で行う（httpx・python-dotenv）。
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

CHANNEL = "1563804404828741715"  # たまご管理 #mosaic-alerts
API = "https://discord.com/api/v10"
STATE = Path.home() / "Library" / "Application Support" / "mosaic-alert-fix" / "last_id"

load_dotenv(Path.home() / ".config" / "discord-mcp" / ".env")
H = {"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}", "User-Agent": "mosaic-alert-fix"}


def _get(path):
    r = httpx.get(API + path, headers=H, timeout=20)
    r.raise_for_status()
    return r.json()


def new():
    if not STATE.exists():
        latest = _get(f"/channels/{CHANNEL}/messages?limit=1")
        ack(latest[0]["id"] if latest else "0")
        return {"last": None, "alerts": []}
    after, out = STATE.read_text().strip(), []
    while True:  # after 指定は古い順ではないので id で並べ直す
        page = _get(f"/channels/{CHANNEL}/messages?after={after}&limit=100")
        if not page:
            break
        page.sort(key=lambda m: int(m["id"]))
        out += page
        after = page[-1]["id"]
        if len(page) < 100:
            break
    # last = 読んだ最後のメッセージ（返信など【要対応】以外も含む）。処理を始める前にここまで既読にする
    return {"last": out[-1]["id"] if out else None,
            "alerts": [{"id": m["id"], "time": m["timestamp"], "text": m["content"]} for m in out
                       if m.get("webhook_id") and m["content"].startswith("【要対応】")]}


def ack(mid):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(mid)


def reply(mid, path):
    text = Path(path).read_text(encoding="utf-8")[:1900]
    r = httpx.post(f"{API}/channels/{CHANNEL}/messages", headers=H, timeout=20, json={
        "content": text, "allowed_mentions": {"parse": []},
        "message_reference": {"message_id": mid, "fail_if_not_exists": False}})
    r.raise_for_status()


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "new":
        print(json.dumps(new(), ensure_ascii=False))
    elif cmd == "ack":
        ack(sys.argv[2])
    elif cmd == "reply":
        reply(sys.argv[2], sys.argv[3])
