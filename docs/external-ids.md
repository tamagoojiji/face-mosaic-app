# 外部サービスの ID 控え（らくらくモザイク）

作成日: 2026-10-25。秘密値（Channel secret・アクセストークン・Stripe シークレットキー・Webhook 署名シークレット）は書かない。

## LINE（プロバイダー: tamago.app）

| 種別 | 名前 | ID | 状態 |
|---|---|---|---|
| LINEログイン チャネル | らくらくモザイク | Channel ID `2012193762` | 公開済み・アプリタイプ=ウェブアプリ・コールバック `https://mosaic.tamago-ai-world.com/auth/line/callback` |
| Messaging API チャネル | — | 未作成 | LINE Developers から直接作成不可。LINE Official Account Manager で公式アカウント作成→Messaging API 有効化が必要 |
| Messaging API チャネル（受付用・no=0） | らくらくモザイク 受付 | Channel ID `2012301058`・ベーシックID `@907hcztm`（友だち追加 https://line.me/R/ti/p/@907hcztm・2026-10-31 作成） | Webhook `https://mosaic.tamago-ai-world.com/line/webhook/0`（2026-10-30 複数運用。返信だけ・通知には使わない） |
| Messaging API チャネル（②・no=2） | らくらくモザイク② | Channel ID `2012301111`・ベーシックID `@597uvhwa`（友だち追加 https://line.me/R/ti/p/@597uvhwa・2026-10-31 作成） | Webhook `https://mosaic.tamago-ai-world.com/line/webhook/2`（2026-10-30 複数運用・定員 5） |
| Messaging API チャネル（③・no=3） | らくらくモザイク③ | Channel ID `2012301176`・ベーシックID `@385ywxzj`（友だち追加 https://line.me/R/ti/p/@385ywxzj・2026-10-31 作成） | Webhook `https://mosaic.tamago-ai-world.com/line/webhook/3`（定員 5・予備＝登録後に「使う」OFF で待機） |

## Stripe（アカウント Tetraris・**テスト環境（サンドボックス）**・2026-10-28 までの控え）

2026-10-26 ③b 段①で `scripts/stripe_setup.py` が作成（価格改定後）。価格ID・ポータル設定IDはアプリの settings テーブルに保存（環境変数には入れない）。

| 商品 | 月額 | prod ID | price ID（lookup_key） | settings キー |
|---|---|---|---|---|
| 画像のみ | 500円 | `prod_VVkJxkxKcnEjGW` | `price_1UUipECK9wiENSxsSPRq1zCd`（fm_image_500） | stripe_price_image |
| 動画A | 980円 | `prod_VVkJGXyUIl9Y6p` | `price_1UUipFCK9wiENSxsTiJpWFE5`（fm_video_a_980） | stripe_price_video_a |
| 動画B | 1,480円 | `prod_VVkJKUYESJCDg2` | `price_1UUipFCK9wiENSxsGXVDtiK2`（fm_video_b_1480） | stripe_price_video_b |
| 高画質オプション | 500円 | `prod_VVkJSsVGnj6Wzl` | `price_1UUipGCK9wiENSxsFG6YPAMD`（fm_hq_500） | stripe_price_hq |
| 画像のみ（定価） | 1,000円 | `prod_VVkJxkxKcnEjGW`（同じ商品） | `price_1UUzo1CK9wiENSxsXe1uzZUq`（fm_image_1000） | stripe_price_image_regular |
| 動画A（定価） | 1,480円 | `prod_VVkJGXyUIl9Y6p`（同じ商品） | `price_1UUzo2CK9wiENSxseq7h50MJ`（fm_video_a_1480） | stripe_price_video_a_regular |
| 動画B（定価） | 1,980円 | `prod_VVkJKUYESJCDg2`（同じ商品） | `price_1UUzo2CK9wiENSxsWFRGzakm`（fm_video_b_1980） | stripe_price_video_b_regular |

上の 500円／980円／1,480円 はリリース記念価格（管理画面の設定 `launch_price_until` までの申し込み・契約を続けている間ずっと）。期限後の申し込みは定価の価格ID（2026-10-27 追加）。高画質オプションは記念・定価とも同じ 500円。

| 種別 | ID | 内容 |
|---|---|---|
| Webhook エンドポイント（face-mosaic・2026-10-26 再発行。旧 `we_1UULAoCK9wiENSxsZ1TFOykC` は署名シークレットの転記ミスで同日削除） | `we_1UUjJ8CK9wiENSxs9O7tLmzI` | `https://mosaic.tamago-ai-world.com/stripe/webhook`・イベント6件（checkout.session.completed / customer.subscription.created・updated・deleted / invoice.payment_failed / invoice.paid）。署名シークレットは VPS の設定ファイル STRIPE_WEBHOOK_SECRET |
| カスタマーポータル設定（アプリ専用） | `bpc_1UUipICK9wiENSxs7NHsJyeD` | カード変更ON・解約ON（期末）・プラン切替OFF（プラン変更と高画質はアプリ内 API）・請求履歴ON。settings `stripe_portal_config` |
| カスタマーポータル（既定設定・不使用） | `bpc_1UOY5bCK9wiENSxseyvgtp1V` | 旧。アプリは上の専用設定を使う |

**旧商品（2026-10-25 作成・旧価格 480／980／1,980／2,480）は 2026-10-26 にアーカイブ済み**: `prod_VVLoTIM3d8p0EP` `prod_VVLpJlHhbUxKGw` `prod_VVLp4pIQb9ifqS` `prod_VVLpWvGmYuNyI9`（価格は商品の既定価格のため価格単体では止められず、商品ごと無効化）。

## Stripe（アカウント Tetraris・**本番モード**・2026-10-28 本番化）

鍵はアプリ専用の標準キー `face-mosaic`（2026-10-28 作成・ダッシュボード 開発者 → APIキー）。VPS の設定ファイル `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` に入れてある。`scripts/stripe_setup.py` が作成（価格7本 livemode=true を API で読み返し確認）。

| 商品 | 月額 | prod ID | price ID（lookup_key） | settings キー |
|---|---|---|---|---|
| 画像のみ | 500円 | `prod_VWWFhwd7hXRl6g` | `price_1UVTCrCK9wiENSxsEOydgt7W`（fm_image_500） | stripe_price_image |
| 動画A | 980円 | `prod_VWWFkReU8z40oF` | `price_1UVTCrCK9wiENSxscrd1uLxP`（fm_video_a_980） | stripe_price_video_a |
| 動画B | 1,480円 | `prod_VWWFzW4diMPHr1` | `price_1UVTCrCK9wiENSxsdAx6Wvte`（fm_video_b_1480） | stripe_price_video_b |
| 高画質オプション | 500円 | `prod_VWWFEzHsG2RIrW` | `price_1UVTCsCK9wiENSxs6vJiyvYh`（fm_hq_500） | stripe_price_hq |
| 画像のみ（定価） | 1,000円 | `prod_VWWFhwd7hXRl6g`（同じ商品） | `price_1UVTCsCK9wiENSxsLXwZrQU3`（fm_image_1000） | stripe_price_image_regular |
| 動画A（定価） | 1,480円 | `prod_VWWFkReU8z40oF`（同じ商品） | `price_1UVTCtCK9wiENSxsEmlsa010`（fm_video_a_1480） | stripe_price_video_a_regular |
| 動画B（定価） | 1,980円 | `prod_VWWFzW4diMPHr1`（同じ商品） | `price_1UVTCtCK9wiENSxs8ScUaNUm`（fm_video_b_1980） | stripe_price_video_b_regular |

| 種別 | ID | 内容 |
|---|---|---|
| Webhook エンドポイント（本番・2026-10-28） | `we_1UVTEpCK9wiENSxsch2dg6I7` | `https://mosaic.tamago-ai-world.com/stripe/webhook`・イベント6件。署名シークレットは VPS の設定ファイル STRIPE_WEBHOOK_SECRET（`webhook-reissue.sh` が転記・自己署名 POST 200 確認） |
| カスタマーポータル設定（アプリ専用・本番） | `bpc_1UVTKeCK9wiENSxs2Ft5M1PW` | カード変更ON・解約OFF・プラン切替OFF・請求履歴ON・見出し「らくらくモザイク」。settings `stripe_portal_config` |

**本番化で誤ってテスト環境に作った Webhook** `we_1UVT7WCK9wiENSxsVpWzea5k`（1回目の `webhook-reissue.sh` がテスト鍵のコンテナで作った。同日スクリプトを修正）→ テスト環境のダッシュボードで削除してよい（未削除）。

上の「テスト環境」の節は、テスト環境で動かすときの控えとして残す（本番の settings テーブルには本番の ID が入っている）。

## Messaging API チャネル（2026-10-25 本人が LINE Official Account Manager で作成）
- Channel ID: 2012194665
- ベーシックID: @606cuvoe
- 友だち追加リンク: https://line.me/R/ti/p/@606cuvoe
- Webhook URL 登録済み・「Webhookの利用」はオフ（受け口は未実装）

## DB バックアップ（GAS・2026-10-27）
- GAS プロジェクト: face-mosaic-backup・scriptId `18T0vKKRifiaI-hkBoLAGBL5IA02H89Kfu4hmdIrztUx1p8Z7AYYDOWfG`（正本 `~/dev/gas-tools/face-mosaic-backup`）
- Web アプリのデプロイ ID（@1・更新は `clasp redeploy` でこの ID のまま）: `AKfycbxOvLYaS8OFgejmR3t3rg0jTrTEuYzJoIEzMCoO1SMgvfcwp-6U5O9uMTk1nrtV5We0nQ`
- 保存先: Google ドライブ（tamagoojiji@gmail.com）のフォルダ `face-mosaic-backup`（ID `1EpaZFNgPFNs_VypwN_YPx6_XaTQZzDty`）
- 合言葉: GAS の Script Properties `TOKEN` と VPS `backup/token`（値はどこにも書かない）
- 復号鍵: 運営の Mac の Keychain（サービス名 `face-mosaic-backup-age`）
