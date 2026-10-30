"""送る画面の処理時間の目安（/api/config の est）。"""

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

HOST = "testserver.local"
F = main.FALLBACK_PER_MIN


@pytest.fixture
def client(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    monkeypatch.setattr(db, "done_video_stats", lambda limit=30: [])  # 実測なし → fallback
    now = time.time()
    u = db.create_user(line_user_id=f"Uest-{now}", display_name="目安", trial_ends_at=now + 86400,
                       ig_asked_at=now, agreed_at=now)
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": u["id"]}), domain=HOST)
    return c, u


def test_est_without_jobs(client):
    c, _ = client
    r = c.get("/api/config")
    assert r.status_code == 200, r.text
    est = r.json()["est"]
    assert est["prep_per_min"] == F["prepare"] + F["track"]
    assert est["export_per_min"] == F["export_pixel"]
    assert est["queue_count"] >= 0 and est["queue_sec"] >= 0


@pytest.fixture
def queued_job_id():
    jid = f"estq-{time.time()}"
    yield jid
    with db.connect() as con:  # 自分が入れたジョブだけ消す（共有 DB のほかのジョブは残す）
        con.execute("DELETE FROM jobs WHERE id=?", (jid,))


def test_est_counts_queued_job(client, queued_job_id):
    c, u = client
    base = c.get("/api/config").json()["est"]
    now = time.time()
    db.insert_job(queued_job_id, kind="video", status="queued", user_id=u["id"], duration_sec=60,
                  expires_at=now + 3600)
    est = c.get("/api/config").json()["est"]
    assert est["queue_count"] == base["queue_count"] + 1
    assert est["queue_sec"] == pytest.approx(base["queue_sec"] + F["prepare"] + F["track"], abs=1)


def test_est_none_on_error(client, monkeypatch):
    c, _ = client

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(main, "_rates", boom)
    r = c.get("/api/config")
    assert r.status_code == 200
    assert r.json()["est"] is None
