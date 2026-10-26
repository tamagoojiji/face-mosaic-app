# 外部サービスの ID 控え（らくちんモザイク）

作成日: 2026-10-25。秘密値（Channel secret・アクセストークン・Stripe シークレットキー・Webhook 署名シークレット）は書かない。

## LINE（プロバイダー: tamago.app）

| 種別 | 名前 | ID | 状態 |
|---|---|---|---|
| LINEログイン チャネル | らくちんモザイク | Channel ID `2012193762` | 公開済み・アプリタイプ=ウェブアプリ・コールバック `https://mosaic.tamago-ai-world.com/auth/line/callback` |
| Messaging API チャネル | — | 未作成 | LINE Developers から直接作成不可。LINE Official Account Manager で公式アカウント作成→Messaging API 有効化が必要 |

## Stripe（アカウント Tetraris・**テスト環境（サンドボックス）**）

2026-10-26 ③b 段①で `scripts/stripe_setup.py` が作成（価格改定後）。価格ID・ポータル設定IDはアプリの settings テーブルに保存（環境変数には入れない）。

| 商品 | 月額 | prod ID | price ID（lookup_key） | settings キー |
|---|---|---|---|---|
| 画像のみ | 500円 | `prod_VVkJxkxKcnEjGW` | `price_1UUipECK9wiENSxsSPRq1zCd`（fm_image_500） | stripe_price_image |
| 動画A | 980円 | `prod_VVkJGXyUIl9Y6p` | `price_1UUipFCK9wiENSxsTiJpWFE5`（fm_video_a_980） | stripe_price_video_a |
| 動画B | 1,480円 | `prod_VVkJKUYESJCDg2` | `price_1UUipFCK9wiENSxsGXVDtiK2`（fm_video_b_1480） | stripe_price_video_b |
| 高画質オプション | 500円 | `prod_VVkJSsVGnj6Wzl` | `price_1UUipGCK9wiENSxsFG6YPAMD`（fm_hq_500） | stripe_price_hq |

| 種別 | ID | 内容 |
|---|---|---|
| Webhook エンドポイント（名前 face-mosaic） | `we_1UULAoCK9wiENSxsZ1TFOykC` | `https://mosaic.tamago-ai-world.com/stripe/webhook`・イベント6件（checkout.session.completed / customer.subscription.created・updated・deleted / invoice.payment_failed / invoice.paid）。署名シークレットは VPS の設定ファイル STRIPE_WEBHOOK_SECRET |
| カスタマーポータル設定（アプリ専用） | `bpc_1UUipICK9wiENSxs7NHsJyeD` | カード変更ON・解約ON（期末）・プラン切替OFF（プラン変更と高画質はアプリ内 API）・請求履歴ON。settings `stripe_portal_config` |
| カスタマーポータル（既定設定・不使用） | `bpc_1UOY5bCK9wiENSxseyvgtp1V` | 旧。アプリは上の専用設定を使う |

**旧商品（2026-10-25 作成・旧価格 480／980／1,980／2,480）は 2026-10-26 にアーカイブ済み**: `prod_VVLoTIM3d8p0EP` `prod_VVLpJlHhbUxKGw` `prod_VVLp4pIQb9ifqS` `prod_VVLpWvGmYuNyI9`（価格は商品の既定価格のため価格単体では止められず、商品ごと無効化）。

本番モードには何も作っていない。本番移行時は同じ構成を本番側で `scripts/stripe_setup.py` で作り直し（WEBHOOK_ID を本番のものに差し替え）、鍵2つを本番側の設定ファイルに入れる。

## Messaging API チャネル（2026-10-25 本人が LINE Official Account Manager で作成）
- Channel ID: 2012194665
- ベーシックID: @606cuvoe
- Webhook URL 登録済み・「Webhookの利用」はオフ（受け口は未実装）
