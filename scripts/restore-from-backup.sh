#!/bin/bash
# 暗号化バックアップ（app-YYYYMMDD-HHMMSS.db.gz.age）を運営の Mac で復号して app.db に戻す。
#
#   bash scripts/restore-from-backup.sh <バックアップファイル> [出力先 app.db]
#
# 復号鍵は Mac の Keychain（サービス名 face-mosaic-backup-age）から読み、画面にもディスクにも出さない。
# 戻した app.db は VPS の /opt/docker/face-mosaic-app/data/app.db に置き換える（アプリを止めてから）。
set -euo pipefail
src=${1:?バックアップファイルを指定してください}
out=${2:-./app.db}
[ -e "$out" ] && { echo "$out が既にあります。別の出力先を指定してください" >&2; exit 1; }
security find-generic-password -s face-mosaic-backup-age -w | age -d -i - "$src" | gunzip > "$out"
echo "integrity: $(sqlite3 "$out" 'PRAGMA integrity_check;')  users: $(sqlite3 "$out" 'SELECT count(*) FROM users;')  → $out"
