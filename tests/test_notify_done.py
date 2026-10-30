"""完成（できました）の LINE 通知を settings notify_done で止められる。ready は止めない。ネットワークは使わない。"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# test_billing.py と同じ環境（config は最初に読んだファイルの値で固まるため、そろえておく）
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fm-test-"))
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")

import urllib.request  # noqa: E402

import pytest  # noqa: E402

from app import config, db  # noqa: E402
from app.services import notify  # noqa: E402

USER = {"id": 1, "line_user_id": "U1", "notify": 1, "status": "active", "line_channel_no": 1}
JOB = {"id": "j1", "user_id": 1, "status": "done"}


class FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "LINE_MSG_CHANNEL_ACCESS_TOKEN", "line-token-test")
    db.upsert_channel(1, name="らくらくモザイク①", kind="notify", access_token="line-token-test")  # 送信は DB の ① のトークン
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(1) or FakeResp())
    yield calls
    db.set_setting("notify_done", "1")
    with db.connect() as con:
        con.execute("DELETE FROM line_channels")


def test_done_off_not_sent(env):
    db.set_setting("notify_done", "0")
    assert notify.push_user("done", USER, JOB) is False
    assert env == []


def test_done_on_sent(env):
    db.set_setting("notify_done", "1")
    assert notify.push_user("done", USER, JOB) is True
    assert env == [1]


def test_ready_sent_even_if_done_off(env):
    db.set_setting("notify_done", "0")
    assert notify.push_user("ready", USER, JOB) is True
    assert env == [1]
