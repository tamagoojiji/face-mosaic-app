"""公式 LINE（複数運用）の一覧・追加・変更 CLI。管理画面 /admin/line と同じ内容。

  python scripts/line_channels.py list
  python scripts/line_channels.py add <no> <name> reception|notify <basic_id> [capacity]
  python scripts/line_channels.py set <no> capacity|active|name <値>

add のトークン・シークレットは環境変数 LINE_CH_TOKEN・LINE_CH_SECRET から読む（無ければ入力を求める・画面に出さない）。
同じ番号で add すると上書き（トークン・シークレットは入れたときだけ変わる）。

VPS では:
  docker exec -e LINE_CH_TOKEN=… -e LINE_CH_SECRET=… face-mosaic-app python scripts/line_channels.py add 2 "らくらくモザイク②" notify @xxxx
  docker exec face-mosaic-app python scripts/line_channels.py list
トークン・シークレットは末尾 4 文字だけ表示する。
"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402

KINDS = ("reception", "notify")


def tail(v):
    return f"…{v[-4:]}" if v else "未設定"


def show(c):
    used = f"{db.channel_used(c['no'])}/{c['capacity']}" if c["kind"] == "notify" else "—"
    print(f"no={c['no']}  名前={c['name']}  種別={c['kind']}  basic_id={c['basic_id'] or '-'}  人数={used}  "
          f"active={c['active']}  token={tail(c['access_token'])}  secret={tail(c['channel_secret'])}")


def secret(env, label):
    v = os.environ.get(env, "").strip()
    return v or getpass.getpass(f"{label}（空なら変更しない）: ").strip() or None


def main(argv):
    db.init()
    if argv == ["list"]:
        rows = db.channels()
        for c in rows:
            show(c)
        print(f"→ {len(rows)} 件")
        return 0
    if len(argv) in (5, 6) and argv[0] == "add" and argv[1].isdigit() and argv[3] in KINDS:
        cols = {"name": argv[2], "kind": argv[3], "basic_id": argv[4] if argv[4].startswith("@") else "@" + argv[4]}
        if len(argv) == 6:
            if not argv[5].isdigit():
                print("定員は数字で入れてください", file=sys.stderr)
                return 2
            cols["capacity"] = int(argv[5])
        cols["access_token"] = secret("LINE_CH_TOKEN", "チャネルアクセストークン")
        cols["channel_secret"] = secret("LINE_CH_SECRET", "チャネルシークレット")
        db.upsert_channel(int(argv[1]), **cols)
        db.audit(None, "cli.line_channel", f"line:{argv[1]}", "追加・更新（トークン・シークレットは記録しない）")
        show(db.channel(int(argv[1])))
        return 0
    if len(argv) == 4 and argv[0] == "set" and argv[1].isdigit() and argv[2] in ("capacity", "active", "name"):
        no, key, val = int(argv[1]), argv[2], argv[3]
        if not db.channel(no):
            print(f"番号が見つかりません: no={no}", file=sys.stderr)
            return 2
        if key in ("capacity", "active"):
            if not val.isdigit():
                print(f"{key} は数字で入れてください", file=sys.stderr)
                return 2
            val = int(val) if key == "capacity" else (1 if int(val) else 0)
        db.upsert_channel(no, **{key: val})
        db.audit(None, "cli.line_channel", f"line:{no}", f"{key}={val}")
        show(db.channel(no))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
