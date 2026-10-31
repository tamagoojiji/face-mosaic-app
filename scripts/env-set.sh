#!/bin/bash
# 設定ファイル（.env）の1行を差し替えて app/worker を作り直す。値は画面に出さない。VPS で:
#   bash scripts/env-set.sh DISCORD_WEBHOOK_ALERTS          # 値を貼り付ける（表示されない）
#   bash scripts/env-set.sh SESSION_SECRET --generate       # ランダムな値を作って入れる
# 差し替える前の .env は .env.bak-日時（600）に残す。手順は docs/key-reissue.md
set -eu
cd /opt/docker/face-mosaic-app
KEY="${1:?キー名を指定してください}"
if [ "${2:-}" = "--generate" ]; then
  VAL=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
else
  read -rsp "$KEY の新しい値（表示されません）: " VAL; echo
fi
[ -n "$VAL" ] || { echo "値が空です"; exit 1; }
KEY="$KEY" VAL="$VAL" python3 - <<'PY'
import os, re, shutil, time
env, key, val = ".env", os.environ["KEY"], os.environ["VAL"]
bak = env + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
shutil.copy2(env, bak)
os.chmod(bak, 0o600)
s = open(env).read()
new, n = re.subn(rf"^{re.escape(key)}=.*$", lambda m: f"{key}={val}", s, flags=re.M)
if n == 0:
    new = s.rstrip("\n") + f"\n{key}={val}\n"
open(env, "w").write(new)
os.chmod(env, 0o600)
print(f"{key}: replaced={n} len={len(val)} backup={bak}")
PY
docker compose up -d 2>&1 | tail -2
