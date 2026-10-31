# 秘密情報が漏れたときの再発行手順

値は画面・チャット・Git に出さない。`.env` の差し替えは VPS で `bash scripts/env-set.sh <キー名>`（値は貼り付けても表示されない・差し替える前の `.env` は `.env.bak-日時` に残す・app/worker を作り直す）。

## 共通の流れ
1. 漏れた範囲を決める（`.env` 全体か、1つの値か。サーバー自体に入られたなら全部）
2. 下の表の上から順に再発行する（被害の大きい順）
3. `curl https://mosaic.tamago-ai-world.com/health` → `ok`、LINE ログインと通知が動くか確かめる
4. 古い `.env.bak-*` のうち、漏れた値が入っているものを消す
5. 何がいつ漏れて、何を再発行したかを HANDOFF に1行残す

## 値ごとの手順

| 値 | 漏れると | 再発行 | 影響 |
|---|---|---|---|
| VPS の root の SSH 鍵 | サーバー全体 | Mac で新しい鍵を作る → VPS の `~/.ssh/authorized_keys` を新しい鍵だけにする | なし |
| `SESSION_SECRET` | 全員になりすませる | `bash scripts/env-set.sh SESSION_SECRET --generate` | 全員ログアウト（LINE で入り直せば戻る） |
| `STRIPE_SECRET_KEY` | 返金・顧客情報の操作 | Stripe ダッシュボード → 開発者 → API キー → シークレットキーを「ロール」→ `env-set.sh STRIPE_SECRET_KEY` | なし |
| `STRIPE_WEBHOOK_SECRET` | 偽の支払い完了を送れる | `bash scripts/webhook-reissue.sh`（中の手順どおり） | なし |
| `LINE_LOGIN_CHANNEL_SECRET` | LINE ログインの偽装 | LINE Developers → LINE ログインのチャネル → チャネルシークレットを再発行 → `env-set.sh LINE_LOGIN_CHANNEL_SECRET` | なし |
| 公式 LINE のアクセストークン・シークレット（DB と `LINE_MSG_*`） | 公式 LINE から勝手に送れる | LINE Developers → Messaging API → 再発行 → `docker exec -it face-mosaic-app python scripts/line_channels.py add <番号> …`（入力を求められる・画面に出ない）。①は `env-set.sh LINE_MSG_CHANNEL_ACCESS_TOKEN` / `LINE_MSG_CHANNEL_SECRET` も | なし |
| `DISCORD_WEBHOOK_*` | 偽の運営通知 | Discord のチャンネル設定 → 連携サービス → ウェブフックを削除して作り直す → `env-set.sh DISCORD_WEBHOOK_ALERTS` など | なし |
| バックアップの合言葉（`backup/token`） | GAS のバックアップに書き込める | `bash scripts/backup-to-gas.sh enroll` | なし |
| バックアップの復号鍵（Mac の Keychain） | バックアップの中身（DB）を読める | 新しい age の鍵を作る → `backup/age.pub` を差し替える → GAS 側の古いバックアップを消す | 古いバックアップは古い鍵でしか開けない |
| `APP_PASSCODE` | 合言葉で入れる（AUTH_MODE=passcode のときだけ） | `env-set.sh APP_PASSCODE` | 今は LINE ログインなので影響なし |

## 権限（2026-10-31 に設定済み）
`.env`・`.env.bak-*`・`data/app.db*` は 600、`data/`・`backup/` は 700（root だけ読める）。
