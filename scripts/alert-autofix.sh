#!/bin/zsh
# #mosaic-alerts の自動調査・修正（launchd: com.tamago.mosaic-alert-fix / 15分ごと・Mac 起動中だけ）
# 新しい【要対応】があるときだけ claude -p を起動する。修正は別の作業フォルダ（git worktree）で
# autofix/<日時> ブランチに commit → GitHub へ push → アラームに返信。本番への反映はしない（承認後に手で）。
#   --force-json <file> : Discord を読まずに、このファイルの JSON をアラームとして扱う（手動テスト用）
set -u
export PATH="/Users/yontsuhashikunihiko/.nodebrew/current/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO="/Users/yontsuhashikunihiko/dev/tamago/face-mosaic-app"
WT="/Users/yontsuhashikunihiko/dev/tamago/face-mosaic-autofix"
PY="/Users/yontsuhashikunihiko/dev/infra/discord-mcp/.venv/bin/python3"
WATCH="$REPO/scripts/alert_watch.py"
CLAUDE_BIN="/Users/yontsuhashikunihiko/.nodebrew/current/bin/claude"
LOG_DIR="$HOME/Library/Logs/mosaic-alert-fix"
LOCK="$LOG_DIR/lock"
STAMP="$(date +'%Y%m%d-%H%M')"
LOG_FILE="$LOG_DIR/$STAMP.log"
BRANCH="autofix/$STAMP"

mkdir -p "$LOG_DIR"
log() { print -r -- "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"; }

# 前の回の claude がまだ動いていれば何もしない（2時間を超えた lock は残骸とみなす）
if ! mkdir "$LOCK" 2>/dev/null; then
  [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]] || exit 0
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
trap 'rm -rf "$LOCK"' EXIT

if [[ "${1:-}" == "--force-json" ]]; then
  NEW="$(cat "$2")"
else
  NEW="$("$PY" "$WATCH" new 2>>"$LOG_FILE")" || { log "Discord の読み取りに失敗"; exit 1; }
fi
COUNT="$(print -r -- "$NEW" | jq '.alerts | length')"
LAST="$(print -r -- "$NEW" | jq -r '.last // empty')"
if [[ "$COUNT" == "0" ]]; then
  [[ -n "$LAST" ]] && "$PY" "$WATCH" ack "$LAST"
  rm -f "$LOG_FILE"  # 何もなかった回のログは残さない
  exit 0
fi
# 先に既読にする（claude が失敗しても同じアラームで15分ごとに起動し続けない）
[[ -n "$LAST" ]] && "$PY" "$WATCH" ack "$LAST"
REPLY_TO="$(print -r -- "$NEW" | jq -r '.alerts[-1].id')"
log "新しいアラーム $COUNT 件 → $BRANCH"

# 作業フォルダ: 最新の master から autofix ブランチを切る（手元の作業中のファイルには触れない）
git -C "$REPO" fetch -q origin master >> "$LOG_FILE" 2>&1
git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1
git -C "$REPO" worktree prune
git -C "$REPO" worktree add -q -B "$BRANCH" "$WT" origin/master >> "$LOG_FILE" 2>&1 \
  || { log "worktree の作成に失敗"; exit 1; }

ALERTS="$(print -r -- "$NEW" | jq -r '.alerts[] | "---- \(.time)\n\(.text)"')"
PROMPT="らくらくモザイク（このフォルダ）の運営 Discord #mosaic-alerts に、次のアラームが届きました。無人で調査・修正してください。人への質問はできません。

$ALERTS

やること:
1. CLAUDE.md・DESIGN.md と該当コードを読み、アラームごとに原因を特定する。伏せ字 ||…|| の中は英語の原文。同じ原因のアラームはまとめてよい
2. 分類する: (A) アプリのバグ → 手元で再現するテストを先に書き、最小限の修正をしてテストを通す。(B) 利用者側の原因・一時的な外部要因・運用（worker停止・ディスク・Stripe 等）→ コードは変えず、運営がやることを書く
3. (A) のとき: 全テスト $REPO/.venv/bin/python -m pytest -q を通してから、このブランチに git commit する（メッセージは日本語・既存の書き方に合わせる・末尾に Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>）

禁止: ssh・scp など本番サーバーへの接続、git push、このフォルダの外のファイルの変更、価格・契約条件の変更、テストを消す・弱めること。確信が持てない修正は commit せず、原因の見立てと修正案だけを報告する。

最後に ===SUMMARY=== の行を出し、その後に日本語で12行以内: 原因（何が起きたか・なぜ）／分類 A or B／やったこと（修正内容・追加したテスト・全テストの結果の件数）／運営がやること。"

log "claude -p 開始"
cd "$WT"
perl -e 'alarm shift; exec @ARGV' 3600 "$CLAUDE_BIN" -p "$PROMPT" --model opus --permission-mode bypassPermissions \
  --disallowedTools "Bash(ssh:*)" "Bash(scp:*)" "Bash(rsync:*)" "Bash(git push:*)" \
  > "$LOG_DIR/$STAMP.claude.out" 2>&1
CLAUDE_EXIT=$?
log "claude -p 終了 exit=$CLAUDE_EXIT"

AHEAD="$(git -C "$WT" rev-list --count origin/master..HEAD 2>/dev/null || echo 0)"
if grep -q '===SUMMARY===' "$LOG_DIR/$STAMP.claude.out"; then
  SUMMARY="$(sed -n '/===SUMMARY===/,$p' "$LOG_DIR/$STAMP.claude.out" | tail -n +2)"
else
  SUMMARY="自動調査が最後まで終わりませんでした（exit=$CLAUDE_EXIT）。ログ: $LOG_DIR/$STAMP.claude.out"
fi

if [[ "$AHEAD" -gt 0 ]]; then
  if git -C "$WT" push -q origin "$BRANCH" >> "$LOG_FILE" 2>&1; then
    TAIL="修正をブランチ $BRANCH に push しました（commit ${AHEAD}件・本番は未反映）。
反映するときは Claude に「$BRANCH を本番に反映して」と伝えてください。"
  else
    TAIL="修正は commit しましたが push に失敗しました。Mac のブランチ $BRANCH（$WT）に残っています。"
  fi
else
  TAIL="コードの修正はありません。"
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1
  git -C "$REPO" branch -q -D "$BRANCH" >/dev/null 2>&1
fi

BODY="$LOG_DIR/$STAMP.reply.txt"
print -r -- "🤖 自動調査（アラーム ${COUNT}件）
$SUMMARY
$TAIL" > "$BODY"
if [[ "${1:-}" == "--force-json" ]]; then
  log "テスト実行のため返信は送らない: $BODY"
else
  "$PY" "$WATCH" reply "$REPLY_TO" "$BODY" >> "$LOG_FILE" 2>&1 && log "返信しました" || log "返信に失敗"
fi
exit 0
