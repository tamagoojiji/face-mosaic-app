#!/bin/bash
# Stripe Webhook エンドポイントの発行し直し（署名シークレットは画面に出さない）。VPS で: bash scripts/webhook-reissue.sh ／ 実行後に scripts/stripe_setup.py の WEBHOOK_ID を新IDへ書き換え、古いエンドポイントは Stripe 側で削除する。
# 1) コンテナ内: 新しい Webhook エンドポイントを作成 → 署名シークレットを /data/.whsec.tmp（600）に書く
# 2) ホスト: python3 が設定ファイルの STRIPE_WEBHOOK_SECRET 行を置換（バックアップ付き）→ tmp を消す
# 3) compose で app/worker を作り直して環境変数を読み直す
set -eu
cd /opt/docker/face-mosaic-app
docker exec -i face-mosaic-app python - <<'PY'
import os, stat, stripe
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
ev = ["checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
      "customer.subscription.deleted", "invoice.payment_failed", "invoice.paid"]
we = stripe.WebhookEndpoint.create(url="https://mosaic.tamago-ai-world.com/stripe/webhook",
                                   enabled_events=ev, description="face-mosaic (" + __import__("time").strftime("%Y-%m-%d") + " 発行)").to_dict()
p = "/data/.whsec.tmp"
with open(p, "w") as f:
    f.write(we["secret"])
os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
print("new endpoint:", we["id"], "events:", len(we["enabled_events"]), "secret len=", len(we["secret"]),
      "prefix ok=", we["secret"].startswith("whsec_"))
PY
python3 - <<'PY'
import re, shutil, time, os
env = "/opt/docker/face-mosaic-app/.env"
tmp = "/opt/docker/face-mosaic-app/data/.whsec.tmp"
sec = open(tmp).read().strip()
bak = env + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
shutil.copy2(env, bak)
s = open(env).read()
new, n = re.subn(r"^STRIPE_WEBHOOK_SECRET=.*$", "STRIPE_WEBHOOK_SECRET=" + sec, s, flags=re.M)
if n == 0:
    new = s.rstrip("\n") + "\nSTRIPE_WEBHOOK_SECRET=" + sec + "\n"
open(env, "w").write(new)
os.remove(tmp)
print("env replaced:", n, "backup:", os.path.basename(bak), "len=", len(sec))
PY
docker compose up -d 2>&1 | tail -2
sleep 5
docker exec -i face-mosaic-app python - <<'PY'
import os, time, hmac, hashlib, json, urllib.request
s = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
print("container secret len=", len(s), "prefix ok=", s.startswith("whsec_"))
payload = json.dumps({"id": "evt_local_diag2", "type": "diag.ping", "data": {"object": {}}}).encode()
ts = str(int(time.time()))
sig = hmac.new(s.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://localhost:8000/stripe/webhook", data=payload, method="POST",
                             headers={"Content-Type": "application/json", "Stripe-Signature": f"t={ts},v1={sig}"})
print("local signed POST ->", urllib.request.urlopen(req).status)
PY
