"""Stripe の商品・価格・Webhook・ポータル設定を作る／そろえる（冪等・何度実行してもよい）。

  python scripts/stripe_setup.py            # 実行（STRIPE_SECRET_KEY が要る）
  python scripts/stripe_setup.py --dry-run  # 作成予定を表示するだけ（鍵なしで可・DB も触らない）

VPS では: docker exec face-mosaic-app python scripts/stripe_setup.py
標準出力は ID と件数だけ（鍵は出さない）。価格ID・ポータル設定IDは settings テーブルに保存する。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stripe  # noqa: E402

from app import config, db  # noqa: E402

# (settings のキー, lookup_key, 商品名, 月額 円)。上4つ=リリース記念価格（高画質は記念も定価も同じ）、下3つ=定価
# 定価は記念価格と同じ商品に足す（商品は1つ・価格が2つ）
PRICES = (("image", "fm_image_500", "画像のみ", 500),
          ("video_a", "fm_video_a_980", "動画A", 980),
          ("video_b", "fm_video_b_1480", "動画B", 1480),
          ("hq", "fm_hq_500", "高画質オプション", 500),
          ("image_regular", "fm_image_1000", "画像のみ", 1000),
          ("video_a_regular", "fm_video_a_1480", "動画A", 1480),
          ("video_b_regular", "fm_video_b_1980", "動画B", 1980))
# 旧商品（docs/external-ids.md・2026-10-26 価格改定でアーカイブ。価格は商品の既定価格なので商品ごと止める）
OLD_PRODUCTS = ("prod_VVLoTIM3d8p0EP", "prod_VVLpJlHhbUxKGw", "prod_VVLp4pIQb9ifqS", "prod_VVLpWvGmYuNyI9")
WEBHOOK_ID = "we_1UUjJ8CK9wiENSxs9O7tLmzI"  # 2026-10-26 再発行（旧 we_1UULAo… は削除）
WEBHOOK_EVENTS = ["checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
                  "customer.subscription.deleted", "invoice.payment_failed", "invoice.paid"]
PORTAL = {"features": {"payment_method_update": {"enabled": True},
                       "subscription_cancel": {"enabled": False},  # 解約はアプリ内「解約する」だけ
                       "subscription_update": {"enabled": False},
                       "invoice_history": {"enabled": True}},
          "business_profile": {"headline": "らくらくモザイク"}}


def dry_run():
    for key, lk, name, yen in PRICES:
        how = "記念価格と同じ商品に追加" if key.endswith("_regular") else "無ければ作成"
        print(f"price {key}: lookup_key={lk} 商品「{name}」 {yen}円/月（{how}）→ settings stripe_price_{key}")
    print(f"旧商品のアーカイブ: {len(OLD_PRODUCTS)} 件 {', '.join(OLD_PRODUCTS)}")
    print(f"webhook {WEBHOOK_ID}: enabled_events {len(WEBHOOK_EVENTS)} 件 {', '.join(WEBHOOK_EVENTS)}")
    print("portal: 設定を1つ作成（カード変更ON・解約OFF・プラン切替OFF・請求履歴ON）→ settings stripe_portal_config"
          "（既にあれば同じ内容に同期）")
    print("dry-run: Stripe と DB には何もしていません")
    return 0


def run():
    if not config.STRIPE_SECRET_KEY:
        print("STRIPE_SECRET_KEY が未設定です（.env に入れてから実行してください）", file=sys.stderr)
        return 2
    stripe.api_key = config.STRIPE_SECRET_KEY
    db.init()
    made = 0
    products = {}  # 商品名 → 商品ID（記念価格の商品に定価を足す）
    for key, lk, name, yen in PRICES:
        found = stripe.Price.list(lookup_keys=[lk], active=True, limit=1).to_dict()["data"]
        if found:
            pid, how = found[0]["id"], "既存"
            prod = found[0]["product"]
            products.setdefault(name, prod if isinstance(prod, str) else prod["id"])
        else:
            kw = {"product": products[name]} if name in products else {"product_data": {"name": name}}
            created = stripe.Price.create(currency="jpy", unit_amount=yen, recurring={"interval": "month"},
                                          lookup_key=lk, **kw)
            pid, how, made = created["id"], "作成", made + 1
            products.setdefault(name, created["product"])
        db.set_setting(f"stripe_price_{key}", pid)
        print(f"price {key}: {pid}（{how}）")
    archived = 0
    for pid in OLD_PRODUCTS:
        try:
            if stripe.Product.retrieve(pid)["active"]:
                stripe.Product.modify(pid, active=False)
                archived += 1
        except stripe.InvalidRequestError as e:
            print(f"旧商品 {pid}: スキップ（{getattr(e, 'code', None) or type(e).__name__}）")
    print(f"旧商品のアーカイブ: {archived} 件（対象 {len(OLD_PRODUCTS)} 件）")
    we = stripe.WebhookEndpoint.modify(WEBHOOK_ID, enabled_events=WEBHOOK_EVENTS)
    print(f"webhook {we['id']}: enabled_events {len(we['enabled_events'])} 件")
    cfg = db.get_setting("stripe_portal_config")
    if cfg:
        stripe.billing_portal.Configuration.modify(cfg, **PORTAL)
        print(f"portal: {cfg}（既存・同期）")
    else:
        cfg = stripe.billing_portal.Configuration.create(**PORTAL)["id"]
        db.set_setting("stripe_portal_config", cfg)
        print(f"portal: {cfg}（作成）")
    print(f"完了: 価格の作成 {made} 件")
    return 0


if __name__ == "__main__":
    sys.exit(dry_run() if "--dry-run" in sys.argv[1:] else run())
