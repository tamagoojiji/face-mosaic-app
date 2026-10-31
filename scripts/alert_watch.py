"""運営 Discord の #mosaic-alerts・#mosaic-reports を読む／返信する（Mac の自動調査 alert-autofix.sh から使う）。

  new <ch>                   前回より新しい【要対応】（alerts）／【通報】（reports）を JSON {"last", "items"} で出す
                             （初回は今の最新を既読にして空を出す）
  ack <ch> <id>              <id> までを既読にする
  reply <ch> <id> <file> [画像...]  <id> のメッセージに返信する（本文はファイルから・1900字まで）。返信の id を出す
  approved <ch> <id>         <id> のメッセージに運営（Bot 以外）が ✅ を付けていれば yes、なければ no

Bot トークンは discord-mcp と同じ ~/.config/discord-mcp/ の設定から読む（画面には出さない）。
実行は discord-mcp の venv の python で行う（httpx・python-dotenv）。
"""

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

CHANNELS = {"alerts": ("1563804404828741715", "【要対応】"),   # たまご管理 #mosaic-alerts
            "reports": ("1563804410935640147", "【通報】")}    # たまご管理 #mosaic-reports
API = "https://discord.com/api/v10"
STATE = Path.home() / "Library" / "Application Support" / "mosaic-alert-fix"

load_dotenv(Path.home() / ".config" / "discord-mcp" / ".env")
H = {"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}", "User-Agent": "mosaic-alert-fix"}


def _get(path):
    r = httpx.get(API + path, headers=H, timeout=20)
    r.raise_for_status()
    return r.json()


def _state(ch):
    # alerts は最初の版の置き場所（last_id）をそのまま使う
    return STATE / ("last_id" if ch == "alerts" else f"last_id_{ch}")


def new(ch):
    cid, head = CHANNELS[ch]
    st = _state(ch)
    if not st.exists():
        latest = _get(f"/channels/{cid}/messages?limit=1")
        ack(ch, latest[0]["id"] if latest else "0")
        return {"last": None, "items": []}
    after, out = st.read_text().strip(), []
    while True:  # after 指定は古い順ではないので id で並べ直す
        page = _get(f"/channels/{cid}/messages?after={after}&limit=100")
        if not page:
            break
        page.sort(key=lambda m: int(m["id"]))
        out += page
        after = page[-1]["id"]
        if len(page) < 100:
            break
    # last = 読んだ最後のメッセージ（返信など対象外も含む）。処理を始める前にここまで既読にする
    return {"last": out[-1]["id"] if out else None,
            "items": [{"id": m["id"], "time": m["timestamp"], "text": m["content"]} for m in out
                      if m.get("webhook_id") and m["content"].startswith(head)]}


def ack(ch, mid):
    STATE.mkdir(parents=True, exist_ok=True)
    _state(ch).write_text(mid)


def reply(ch, mid, path, images=()):
    cid = CHANNELS[ch][0]
    payload = {"content": Path(path).read_text(encoding="utf-8")[:1900], "allowed_mentions": {"parse": []},
               "message_reference": {"message_id": mid, "fail_if_not_exists": False}}
    url = f"{API}/channels/{cid}/messages"
    if images:
        files = {f"files[{i}]": (os.path.basename(p), open(p, "rb"), "image/jpeg") for i, p in enumerate(images[:10])}
        r = httpx.post(url, headers=H, timeout=60, data={"payload_json": json.dumps(payload)}, files=files)
    else:
        r = httpx.post(url, headers=H, timeout=20, json=payload)
    r.raise_for_status()
    return r.json()["id"]


def approved(ch, mid):
    cid = CHANNELS[ch][0]
    r = httpx.get(f"{API}/channels/{cid}/messages/{mid}/reactions/%E2%9C%85?limit=100", headers=H, timeout=20)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return any(not u.get("bot") for u in r.json())


if __name__ == "__main__":
    cmd, ch = sys.argv[1], sys.argv[2]
    if cmd == "new":
        print(json.dumps(new(ch), ensure_ascii=False))
    elif cmd == "ack":
        ack(ch, sys.argv[3])
    elif cmd == "reply":
        print(reply(ch, sys.argv[3], sys.argv[4], sys.argv[5:]))
    elif cmd == "approved":
        print("yes" if approved(ch, sys.argv[3]) else "no")
