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


def test_webhook_follow_reply(monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    c = TestClient(main.app)
    r = post(c, [{"type": "follow", "replyToken": "rt-1", "source": {"type": "user", "userId": "Ufollow"}}])
    assert r.status_code == 200
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://api.line.me/v2/bot/message/reply"
    sent = json.loads(req.data)
    assert sent["replyToken"] == "rt-1"
    text = sent["messages"][0]["text"]
    assert text.startswith("友だち追加ありがとうございます。")
    assert text.endswith("https://example.test/?openExternalBrowser=1")


def test_webhook_unfollow_notify_off(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("unfollow では送らない"))
    u = db.create_user(line_user_id="Uunfollow1", display_name="ブロック", notify=1)
    c = TestClient(main.app)
    r = post(c, [{"type": "unfollow", "source": {"type": "user", "userId": "Uunfollow1"}}])
    assert r.status_code == 200
    assert db.get_user(u["id"])["notify"] == 0
