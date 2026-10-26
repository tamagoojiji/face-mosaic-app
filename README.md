# 顔モザイク Web版（試験版）

パレードやショーの動画・写真を送ると、観客の顔を自動でモザイクにし、ダンサーなど残したいものだけをタップで残して受け取れる Web アプリ。スマホのブラウザだけで使える。

- 設計書: [DESIGN.md](DESIGN.md)（画面・動線・料金プラン・フェーズ）
- 実測: [docs/vps-photo-bench-2026-10-24.md](docs/vps-photo-bench-2026-10-24.md)（写真1枚の処理時間と資源）
- 試験版の公開先: https://mosaic.tamago-ai-world.com （合言葉が必要）

## いまできること（2026-10-24）

| モード | 流れ | 状態 |
|---|---|---|
| 写真1枚 | 送る → 顔を自動検出 → 残す顔をタップ → ブロック粗め／すりガラス → 書き出し → 写真アプリに保存 | 本番で動作 |
| 動画（3分・1GBまで） | 分割送信（途中再開可）→ 1080p 変換・顔の追跡・特徴 → 選択 → 確認（2列入れ替え）→ 書き出し → 保存 | 本番で1分動画を通過 |
| 利用者・プラン（③a・2026-10-25） | 合言葉（端末ごとに利用者を自動作成・試験中は動画B相当）／LINE ログイン（チャネル作成待ち・モックで検証）→ Instagram 名（任意）→ 同意。プラン別の月の上限（リリース記念は `scripts/settings.py` で切替）・プラン比較（購入は準備中）・設定・退会。LINE 通知4種（未設定なら送らない）・Discord 運営通知3チャンネル | 本番は合言葉モード |

- 処理はすべてサーバー内（YuNet／SFace／ffmpeg）。外部の AI サービスは使わない
- 元の動画・写真と解析データは完成から24時間、完成品は30日で自動削除（選び直しは元データがある間だけ）。選択を3時間放置したら削除（自動では書き出さない）
- 自分の作ったものだけ表示（合言葉モードは端末ごと、LINE モードは LINE の利用者ごと）
- 運営の設定: `docker exec face-mosaic-app python scripts/settings.py list`（リリース記念の ON/OFF と期間）
- 利用者の操作: `docker exec face-mosaic-app python scripts/users.py list`（set-plan／set-hq＝高画質オプション／set-role）

## 構成

```
app/            FastAPI（認証・アップロード・ジョブ API・画面配信・定期削除）
worker/run.py   キューを1件ずつ処理する常駐プロセス
worker/engine/  検出・モザイク・写真・動画の処理（photo-retouch から移植。判定ルールは変更なし）
web/            画面（静的 HTML/JS・外部 CDN なし）
models/         yunet.onnx（顔検出）・sface.onnx（顔照合）
nginx/          リバースプロキシ設定
prototype/      Mac 内で流れを確かめた試作とベンチ
```

## 動かす（VPS）

```bash
cp env.example .env   # APP_PASSCODE / SESSION_SECRET を書く（値は VPS 上で直接入力）
docker compose up -d --build
docker logs face-mosaic-app --tail 20
```

設定値の一覧は `env.example`。値そのものはリポに置かない。

## 次の段階

1. Stripe（購入・解約）と管理画面 /admin（③b）
2. 専用 VPS（Contabo Cloud VPS 6）へ移行し、A レコードを付け替え
