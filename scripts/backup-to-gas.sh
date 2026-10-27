#!/bin/bash
# DB を暗号化して GAS（Google ドライブ）へ送る。VPS のホストの cron から毎晩実行する。
#
#   bash scripts/backup-to-gas.sh           # バックアップして送る
#   bash scripts/backup-to-gas.sh enroll    # 初回だけ: 合言葉を作って GAS に登録する
#   bash scripts/backup-to-gas.sh list      # GAS 側の最新5件を見る
#
# 置き場（リポには入れない・VPS だけ）: /opt/docker/face-mosaic-app/backup/
#   gas_url  … GAS Web アプリの /exec URL
#   age.pub  … 暗号化用の公開鍵（復号用の秘密鍵は運営の Mac の Keychain にだけある）
#   token    … GAS との合言葉（enroll で作る・600）
#   local/   … 暗号化済みの控え（最新7件）
# 合言葉と DB の中身は画面に出さない。失敗したら Discord #mosaic-alerts に知らせる。
set -euo pipefail
APP=/opt/docker/face-mosaic-app
DIR=$APP/backup
URL=$(cat "$DIR/gas_url")
TOKEN="$DIR/token"
log() { echo "$(TZ=Asia/Tokyo date '+%F %T') $*"; }

ALERTED=0
alert() {
  ALERTED=1
  local hook
  hook=$(grep -E '^DISCORD_WEBHOOK_ALERTS=' "$APP/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
  [ -n "$hook" ] && curl -sS -m 15 -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1][:1800], "allowed_mentions": {"parse": []}}))' "【DBバックアップ失敗】$1")" \
    "$hook" >/dev/null || true
}

post() {  # post <action> [追加の curl 引数…]
  local action=$1; shift
  curl -sS -L -m 180 --data-urlencode "action=$action" --data-urlencode "token@$TOKEN" "$@" "$URL"
}

case "${1:-backup}" in
  enroll)
    umask 077
    [ -s "$TOKEN" ] || openssl rand -hex 32 | tr -d '\n' > "$TOKEN"
    curl -sS -L -m 60 --data-urlencode action=enroll --data-urlencode "token@$TOKEN" "$URL"; echo
    exit 0 ;;
  list)
    post list; echo
    exit 0 ;;
  backup) ;;
  *) echo "usage: $0 [backup|enroll|list]" >&2; exit 2 ;;
esac

tmp=$(mktemp -d)
trap 'rc=$?; rm -rf "$tmp"; if [ $rc -ne 0 ] && [ "$ALERTED" = 0 ]; then log "失敗 rc=$rc"; alert "スクリプトが途中で止まりました（rc=$rc）。$DIR/backup.log を見てください"; fi' EXIT
ts=$(TZ=Asia/Tokyo date +%Y%m%d-%H%M%S)
name="app-$ts.db.gz.age"

sqlite3 "$APP/data/app.db" ".backup '$tmp/app.db'"
check=$(sqlite3 "$tmp/app.db" 'PRAGMA integrity_check;')
[ "$check" = ok ] || { alert "コピーした DB の整合性チェックが ok ではありません（$check）"; exit 1; }
users=$(sqlite3 "$tmp/app.db" 'SELECT count(*) FROM users;')
gzip -9 "$tmp/app.db"
age -R "$DIR/age.pub" -o "$tmp/$name" "$tmp/app.db.gz"
sha=$(sha256sum "$tmp/$name" | cut -d' ' -f1)
base64 -w0 "$tmp/$name" > "$tmp/b64"

resp=$(post upload --data-urlencode "name=$name" --data-urlencode "sha256=$sha" --data-urlencode "data@$tmp/b64" || true)
if ! printf '%s' "$resp" | grep -q '"ok":true'; then
  case "$resp" in
    '{'*) why="GAS の応答: $(printf '%s' "$resp" | head -c 200)" ;;
    *'<html'*|*'<!DOCTYPE'*) why="GAS が JSON ではなく HTML を返しました（URL の間違いか、GAS の許可が切れている可能性）" ;;
    '') why="GAS から応答がありません（通信エラーかタイムアウト）" ;;
    *) why="GAS の応答: $(printf '%s' "$resp" | head -c 120)" ;;
  esac
  alert "GAS への送信に失敗しました。$why"
  log "送信失敗 $name resp=$(printf '%s' "$resp" | head -c 200)"
  exit 1
fi

mkdir -p "$DIR/local"
cp "$tmp/$name" "$DIR/local/"
ls -1t "$DIR/local"/app-*.db.gz.age 2>/dev/null | tail -n +8 | xargs -r rm -f
log "ok $name size=$(stat -c %s "$tmp/$name") users=$users sha=${sha:0:12} resp=$(printf '%s' "$resp" | head -c 160)"
