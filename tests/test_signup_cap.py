"""新規登録の残り枠（signup_remaining）と、新規登録した人を n 日テスターにする設定（signup_tester_days）。ネットワークは使わない。"""

import datetime
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

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app import main  # noqa: E402
from app.services import discord, notify, quota  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    monkeypatch.setattr(config, "LINE_MOCK", True)
    monkeypatch.setattr(discord, "send", lambda *a, **k: True)
    monkeypatch.setattr(notify, "push_user", lambda *a, **k: None)
    yield
    db.set_setting("signup_remaining", "")
    db.set_setting("signup_tester_days", "0")


def login(uid, name="テスト"):
    c = TestClient(main.app)
    return c.post("/auth/line/mock", data={"user_id": uid, "name": name}, follow_redirects=False)


def uid(tag):
    return "U" + (tag + "0" * 32)[:32]


def test_signup_cap():
    db.set_setting("signup_remaining", "2")
    a, b, c = uid("a1cap"), uid("b2cap"), uid("c3cap")
    for x in (a, b):
        r = login(x)
        assert r.status_code == 303 and r.headers["location"] == "/home", r.text
    assert db.get_setting("signup_remaining") == "0"
    r = login(c)
    assert r.status_code == 303 and r.headers["location"] == "/?e=closed"
    assert db.user_by("line_user_id", c) is None
    r = login(a)  # 既存の人はログインできる
    assert r.status_code == 303 and r.headers["location"] == "/home"
    assert db.get_setting("signup_remaining") == "0"


def test_signup_unlimited():
    db.set_setting("signup_remaining", "")
    for i in range(3):
        x = uid(f"d{i}free")
        r = login(x)
        assert r.status_code == 303 and r.headers["location"] == "/home"
        assert db.user_by("line_user_id", x) is not None
    assert db.get_setting("signup_remaining") == ""


def test_signup_tester_days():
    db.set_setting("signup_tester_days", "7")
    x = uid("e1test")
    assert login(x).headers["location"] == "/home"
    u = db.user_by("line_user_id", x)
    last = datetime.datetime.now(quota.JST).date() + datetime.timedelta(days=6)
    assert u["role"] == "tester"
    assert u["tester_until"] == quota.tester_until_ts(last.isoformat())
    assert main._me(u)["tester"] is True

    db.set_setting("signup_tester_days", "0")
    y = uid("f1user")
    assert login(y).headers["location"] == "/home"
    v = db.user_by("line_user_id", y)
    assert v["role"] == "user" and v["tester_until"] is None


def test_admin_signup_settings(monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    if not db.user_by("device_id", "dev-admin-signup"):
        db.create_user(device_id="dev-admin-signup", display_name="運営", role="admin")
    a = TestClient(main.app)
    a.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"t": int(time.time())}))
    a.cookies.set(config.OWNER_COOKIE, main._owner_serializer().dumps("dev-admin-signup"))
    r = a.post("/admin/api/settings", json={"signup_remaining": "abc"})
    assert r.status_code == 400, r.text
    r = a.post("/admin/api/settings", json={"signup_remaining": "5"})
    assert r.status_code == 200, r.text
    assert db.get_setting("signup_remaining") == "5"
    assert r.json()["settings"]["signup_remaining"]["value"] == "5"
