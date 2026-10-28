# らくらくモザイク（face-mosaic-app）

ルートの `~/CLAUDE.md` と AGENTS.md のルールに従う。このファイルはプロジェクト固有の入口だけ。

## 最初に読む
1. `~/Tech-Notes/face-mosaic-app/HANDOFF-2026-10-28.md`（引き継ぎの正本: 完了・次の1手・決定事項・コマンド）
2. `DESIGN.md`（設計書。6節末尾に Stripe 本番移行チェックリスト）
3. 宣伝コンテンツは `~/Tech-Notes/face-mosaic-app/marketing/`（`BRIEF.md` が事実の正本・`schedule.md` が日程）。このリポには置かない

## URL台帳
- 本番: https://mosaic.tamago-ai-world.com（管理画面 `/admin`・説明書 `/manual/quick` `/manual/guide`・規約 `/legal/{terms,privacy,tokushoho}`）
- GitHub: https://github.com/tamagoojiji/face-mosaic-app（ブランチ master）
- VPS 6（東京）: `root@5.104.86.10`・`/opt/docker/face-mosaic-app/`
- 外部ID（LINE・Stripe・GAS）は `docs/external-ids.md`

## 本番反映
`scp <file> root@5.104.86.10:/opt/docker/face-mosaic-app/<file>` → `ssh root@5.104.86.10 "cd /opt/docker/face-mosaic-app && docker compose up -d --build"` → `curl https://mosaic.tamago-ai-world.com/health` と該当ページの表示確認。コンテナは COPY なので web/docs だけの変更でも build が要る。送る前に本番側のファイルが手元の変更前と同じか `cmp` で比べる。

## 守ること
- 名前は「らくらくモザイク」（アプリ内の表示は「顔モザイク」）。問い合わせ先は contact@tetraris.com
- `.env`・鍵・トークンの中身を端末に出さない。Stripe/LINE の値はサーバー内の python で読み書きし、出力は名前・長さ・件数だけ
- 本番の Stripe 書き換え・設定変更（`scripts/settings.py set`）は auto モードの分類器に止められる → コマンドを書いてユーザーの `!` 実行に委ねる
- 価格・お試し・契約条件は HANDOFF の「決定事項」から変えない。記念価格を書くときは期限（12/1（火））を必ず併記
- 説明書の画像（`docs/manual/img/`）は git 管理外。撮り直しは本番から Playwright で（実在の顔は出さない）
- Tech-Notes 側での `git add -A` は禁止（`git add <ファイル名>`）
