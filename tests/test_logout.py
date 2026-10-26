"""ログアウト: セッション Cookie を消す（合言葉モードの端末 Cookie は残す）。ネットワークは使わない。"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# test_billing.py と同じ環境（config は最初に読んだファイルの値で固まるため、そろえておく）
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fm-test-"))
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
for k in ("LINE_MSG_CHANNEL_ACCESS_TOKEN", "DISCORD_WEBHOOK_ALERTS", "DISCORD_WEBHOOK_REPORTS", "DISCORD_WEBHOOK_USAGE",
          "APP_BASE_URL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app import main  # noqa: E402

HOST = "testserver.local"  # TestClient の Cookie の domain（ログアウトの Set-Cookie と同じ所に置く）


def test_logout(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    # LINE モード: ログアウトでセッションが消え、直後の /api/me は 401・画面はログインへ
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    u = db.create_user(line_user_id="Ulogout1", display_name="ログアウト", ig_asked_at=time.time(), agreed_at=time.time())
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": u["id"]}), domain=HOST)
    assert c.get("/api/me").status_code == 200
    r = c.post("/api/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert f"{config.COOKIE_NAME}=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert config.COOKIE_NAME not in c.cookies
    assert c.get("/api/me").status_code == 401
    assert c.get("/home", follow_redirects=False).status_code == 302
    # 合言葉モード: 端末の持ち主 Cookie は残す
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    p = TestClient(main.app)
    p.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"t": int(time.time())}), domain=HOST)
    p.cookies.set(config.OWNER_COOKIE, main._owner_serializer().dumps("dev-logout"), domain=HOST)
    assert p.get("/api/me").status_code == 200
    r = p.post("/api/logout")
    assert config.OWNER_COOKIE not in r.headers["set-cookie"]
    assert config.OWNER_COOKIE in p.cookies and config.COOKIE_NAME not in p.cookies
    assert p.get("/api/me").status_code == 401
