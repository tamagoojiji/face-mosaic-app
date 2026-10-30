"""利用者のプラン・高画質オプション・運営権限を変える CLI。管理画面（③b）ができるまでの代わり。

  python scripts/users.py list
  python scripts/users.py set-plan <id> free|image|video_a|video_b
  python scripts/users.py set-hq <id> 0|1          # 高画質オプション（動画A・B だけ有効）
  python scripts/users.py set-role <id> user|tester|admin # tester=枚数・本数の月上限なし（長さ・容量はプランのまま）

VPS では: docker exec face-mosaic-app python scripts/users.py list
LINE の userId は表示しない（ログイン方法だけ LINE／合言葉 と出す）。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.services import quota  # noqa: E402

ROLES = ("user", "tester", "admin")


def show(u):
    via = "LINE" if u["line_user_id"] else "合言葉" if u["device_id"] else "-"
    made = time.strftime("%Y-%m-%d", time.localtime(u["created_at"])) if u["created_at"] else "-"
    print(f"id={u['id']}  表示名={u['display_name'] or '-'}  plan={u['plan']}  hq={u['hq']}  role={u['role']}  "
          f"status={u['status']}  ログイン={via}  作成日={made}")


def target(uid):
    u = db.get_user(int(uid)) if uid.isdigit() else None
    if not u:
        print(f"利用者が見つかりません: id={uid}", file=sys.stderr)
    return u


def main(argv):
    db.init()
    if argv == ["list"]:
        with db.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM users ORDER BY id")]
        for u in rows:
            show(u)
        print(f"→ {len(rows)} 人")
        return 0
    if len(argv) == 3 and argv[0] in ("set-plan", "set-hq", "set-role"):
        u = target(argv[1])
        if not u:
            return 2
        cmd, val = argv[0], argv[2]
        if cmd == "set-plan" and val not in quota.PLANS:
            print(f"知らないプランです: {val}（使えるプラン: {', '.join(quota.PLAN_ORDER)}）", file=sys.stderr)
            return 2
        if cmd == "set-hq" and val not in ("0", "1"):
            print("set-hq は 0 か 1 です", file=sys.stderr)
            return 2
        if cmd == "set-role" and val not in ROLES:
            print(f"知らない権限です: {val}（使える権限: {', '.join(ROLES)}）", file=sys.stderr)
            return 2
        col = {"set-plan": "plan", "set-hq": "hq", "set-role": "role"}[cmd]
        db.update_user(u["id"], **{col: int(val) if col == "hq" else val})
        u = db.get_user(u["id"])
        show(u)
        if u["hq"] and u["plan"] not in quota.HQ_OPTION["plans"]:
            print("※ 高画質オプションは動画A・動画B のときだけ有効です（いまのプランでは効きません）")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
