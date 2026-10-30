"""role=tester: 枚数・本数の月上限なし・お試し期限なし（1本の長さ・容量はプランのまま）。ネットワークは使わない。"""

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
from app.services import discord, quota  # noqa: E402

HOST = "testserver.local"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")


def expired_user(line_id, role):
    now = time.time()
    u = db.create_user(line_user_id=line_id, display_name="テスター", trial_ends_at=now - 60,
                       ig_asked_at=now, agreed_at=now)
    db.update_user(u["id"], role=role)
    return db.get_user(u["id"])


def test_tester_limits():
    u = expired_user("Utester1", "tester")
    lim = quota.limits(u)
    assert lim["images"] is None and lim["video_count_month"] is None and lim["video_sec_month"] is None
    assert lim["video_len"] == 60 and lim["video_allowed"] is True
    assert lim["tester"] is True and lim["campaign"] is False
    assert quota.blocked(u) is None
    assert quota.check_image(u) is None
    assert quota.check_video(u, 59) is None
    msg = quota.check_video(u, 61)
    assert msg is not None and "1本1分まで" in msg


def test_user_trial_expired_control():
    u = expired_user("Utester2", "user")
    assert quota.blocked(u) == "trial_expired"
    assert quota.limits(u)["tester"] is False


def test_tester_api_me(monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    u = expired_user("Utester3", "tester")
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": u["id"]}), domain=HOST)
    r = c.get("/api/me")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["tester"] is True and m["trial_days_left"] is None and m["remaining"]["images"] is None
    assert m["blocked"] is None and m["trial_expired"] is False


def test_admin_set_role_tester(monkeypatch):
    monkeypatch.setattr(discord, "send", lambda *a, **k: True)
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    admin = db.user_by("device_id", "dev-admin-tester") or db.create_user(device_id="dev-admin-tester",
                                                                           display_name="運営", role="admin")
    a = TestClient(main.app)
    a.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"t": int(time.time())}))
    a.cookies.set(config.OWNER_COOKIE, main._owner_serializer().dumps("dev-admin-tester"))
    target = db.create_user(device_id="dev-tester-target", display_name="対象")
    r = a.post(f"/admin/api/users/{target['id']}", json={"plan": "free", "hq": False, "role": "tester", "status": "active"})
    assert r.status_code == 200, r.text
    assert db.get_user(target["id"])["role"] == "tester"
    assert admin["role"] == "admin"
