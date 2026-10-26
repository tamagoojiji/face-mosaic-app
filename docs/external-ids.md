# 外部サービスの ID 控え（らくちんモザイク）

作成日: 2026-10-25。秘密値（Channel secret・アクセストークン・Stripe シークレットキー・Webhook 署名シークレット）は書かない。

## LINE（プロバイダー: tamago.app）

| 種別 | 名前 | ID | 状態 |
|---|---|---|---|
| LINEログイン チャネル | らくちんモザイク | Channel ID `2012193762` | 公開済み・アプリタイプ=ウェブアプリ・コールバック `https://mosaic.tamago-ai-world.com/auth/line/callback` |
| Messaging API チャネル | — | 未作成 | LINE Developers から直接作成不可。LINE Official Account Manager で公式アカウント作成→Messaging API 有効化が必要 |

## Stripe（アカウント Tetraris・**テスト環境（サンドボックス）**）

| 商品 | 月額 | prod ID | price ID | .env キー |
|---|---|---|---|---|
| 画像のみ | 480円 | `prod_VVLoTIM3d8p0EP` | `price_1UUL7FCK9wiENSxsJfbCiNX3` | STRIPE_PRICE_IMAGE |
| 動画A | 980円 | `prod_VVLpJlHhbUxKGw` | `price_1UUL7fCK9wiENSxsa3liLGkz` | STRIPE_PRICE_VIDEO_A |
| 動画B | 1,980円 | `prod_VVLp4pIQb9ifqS` | `price_1UUL7uCK9wiENSxsLwo8FZhg` | STRIPE_PRICE_VIDEO_B |
| 動画B 高画質 | 2,480円 | `prod_VVLpWvGmYuNyI9` | `price_1UUL89CK9wiENSxsWTk06r8x` | （不使用） |

**動画B 高画質 2,480円（prod_VVLpWvGmYuNyI9）は不使用**（2026-10-26 に高画質をプランからオプションへ変更）。③b で「高画質オプション 500円」の商品を作って差し替える（キーは STRIPE_PRICE_HQ_OPTION）。

**Stripe テスト環境の商品は旧価格（480／980／1,980／2,480）で作成済み。③bで 画像のみ 500・動画B 1,480 の新価格と 高画質オプション 500 を作り直し、旧価格はアーカイブする**（2026-10-26 価格改定）。

| 種別 | ID | 内容 |
|---|---|---|
| Webhook エンドポイント（名前 face-mosaic） | `we_1UULAoCK9wiENSxsZ1TFOykC` | `https://mosaic.tamago-ai-world.com/stripe/webhook`・イベント5件（checkout.session.completed / customer.subscription.created・updated・deleted / invoice.payment_failed）・API 2026-05-27.dahlia |
| カスタマーポータル（既定設定） | `bpc_1UOY5bCK9wiENSxseyvgtp1V` | キャンセル許可=ON。プラン切替=OFF（未設定・要対応） |

本番モードには何も作っていない。本番移行時は同じ構成を本番側で作り直し、ID を差し替える。

## Messaging API チャネル（2026-10-25 本人が LINE Official Account Manager で作成）
- Channel ID: 2012194665
- ベーシックID: @606cuvoe
- Webhook URL 登録済み・「Webhookの利用」はオフ（受け口は未実装）
