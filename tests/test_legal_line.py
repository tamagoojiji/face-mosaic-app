"""③b 段③: 法務ページ（ログイン不要）と LINE Webhook（署名検証・follow の reply・unfollow で通知 OFF）。ネットワークは使わない。"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# test_billing.py と同じ環境（config は最初に読んだファイルの値で固まるため、そろえておく）
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fm-test-"))
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
for k in ("LINE_MSG_CHANNEL_ACCESS_TOKEN", "DISCORD_WEBHOOK_ALERTS", "DISCORD_WEBHOOK_REPORTS", "DISCORD_WEBHOOK_USAGE",
          "APP_BASE_URL"):
    os.environ.pop(k, None)

import urllib.request  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app import main  # noqa: E402

LINE_SECRET = "line-secret-test"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "LINE_MSG_CHANNEL_SECRET", LINE_SECRET)
    monkeypatch.setattr(config, "LINE_MSG_CHANNEL_ACCESS_TOKEN", "line-token-test")
    monkeypatch.setattr(config, "APP_BASE_URL", "https://example.test")


def sign(body):
    return base64.b64encode(hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()).decode()


def post(c, events, signature=None):
    body = json.dumps({"destination": "U0", "events": events}).encode()
    return c.post("/line/webhook", content=body,
                  headers={"Content-Type": "application/json", "X-Line-Signature": signature or sign(body)})


class FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_legal_pages():
    c = TestClient(main.app)
    for name, title in (("terms", "利用規約"), ("privacy", "プライバシーポリシー"), ("tokushoho", "特定商取引法に基づく表記")):
        r = c.get(f"/legal/{name}")
        assert r.status_code == 200, name
        assert f"<h1>{title}</h1>" in r.text
        assert "noindex" not in r.text
    assert c.get("/legal/foo").status_code == 404
    assert c.get("/legal/..%2Fagree").status_code == 404


def test_webhook_bad_signature():
    c = TestClient(main.app)
    r = post(c, [{"type": "follow", "replyToken": "rt"}], signature="bad")
    assert r.status_code == 400


def test_webhook_secret_unset(monkeypatch):
    monkeypatch.setattr(config, "LINE_MSG_CHANNEL_SECRET", "")
    c = TestClient(main.app)
    assert post(c, [{"type": "follow", "replyToken": "rt"}], signature="bad").status_code == 200


def test_webhook_follow_no_reply(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("follow では返信しない"))
    c = TestClient(main.app)
    r = post(c, [{"type": "follow", "replyToken": "rt-1", "source": {"type": "user", "userId": "Ufollow"}}])
    assert r.status_code == 200


def test_webhook_follow_friend_on(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
    u = db.create_user(line_user_id="Ufollow2", display_name="追加", notify=0, line_friend=0)
    c = TestClient(main.app)
    assert post(c, [{"type": "follow", "replyToken": "rt-2", "source": {"type": "user", "userId": "Ufollow2"}}]).status_code == 200
    got = db.get_user(u["id"])
    assert got["line_friend"] == 1 and got["notify"] == 1


def test_webhook_unfollow_notify_off(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("unfollow では送らない"))
    u = db.create_user(line_user_id="Uunfollow1", display_name="ブロック", notify=1)
    c = TestClient(main.app)
    r = post(c, [{"type": "unfollow", "source": {"type": "user", "userId": "Uunfollow1"}}])
    assert r.status_code == 200
    got = db.get_user(u["id"])
    assert got["notify"] == 0 and got["line_friend"] == 0


def test_webhook_message_no_reply(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("message には返信しない"))
    c = TestClient(main.app)
    r = post(c, [{"type": "message", "replyToken": "rt-m", "message": {"type": "text", "text": "こんにちは"},
                  "source": {"type": "user", "userId": "Umsg"}}])
    assert r.status_code == 200


class JsonResp(FakeResp):
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self, *a):
        return self.data


def test_fetch_friend_flag(monkeypatch):
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        return JsonResp({"friendFlag": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert main.fetch_friend_flag("at-1") is False
    assert seen[0].full_url == "https://api.line.me/friendship/v1/status"
    assert seen[0].get_header("Authorization") == "Bearer at-1"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: JsonResp({"friendFlag": True}))
    assert main.fetch_friend_flag("at-1") is True

    def boom(*a, **k):
        raise OSError("network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert main.fetch_friend_flag("at-1") is None


def test_login_saves_friend(monkeypatch):
    """ログインで友だち状態を保存（False → 0、取得失敗 None → NULL のまま）。"""
    from starlette.requests import Request as SReq
    req = SReq({"type": "http", "method": "GET", "path": "/", "headers": [], "scheme": "http", "server": ("t", 80),
                "query_string": b""})
    monkeypatch.setattr(main, "_notify_registered", lambda u: None)
    main._login_line_user(req, "Ufriend0", "未追加", False)
    assert db.user_by("line_user_id", "Ufriend0")["line_friend"] == 0
    main._login_line_user(req, "Ufriend9", "不明", None)
    assert db.user_by("line_user_id", "Ufriend9")["line_friend"] is None
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": db.user_by("line_user_id", "Ufriend0")["id"]}))
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    me = c.get("/api/me").json()
    assert me["line_friend"] == 0 and me["line_add_url"] == "https://line.me/R/ti/p/@606cuvoe"
