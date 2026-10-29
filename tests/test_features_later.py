"""顔と服の特徴は選択画面を出した後に計算する（worker.do_prepare の順番・picks の待ち方）。"""

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
from worker import run  # noqa: E402

HOST = "testserver.local"
INFO = {"fps": 30.0, "out_width": 1080, "out_height": 1920, "duration": 10.0}


def _fake_prepare(monkeypatch, calls, feat_exc=None):
    def rec(name, ret=None):
        def f(*a, **kw):
            calls.append(name)
            return ret
        return f

    def feats(*a, **kw):
        calls.append("compute_features")
        if feat_exc:
            raise feat_exc
        return {}, {}

    v = run.video
    monkeypatch.setattr(v, "prepare", rec("prepare", (INFO, "", {"mode": None})))
    monkeypatch.setattr(v, "to_1080", rec("to_1080"))
    monkeypatch.setattr(v, "get_tracks", rec("get_tracks", ([], 640, 1138, 300)))
    monkeypatch.setattr(v, "compute_features", feats)
    monkeypatch.setattr(v, "pick_shots", rec("pick_shots", []))
    monkeypatch.setattr(v, "build_shots", rec("build_shots", []))
    monkeypatch.setattr(v, "make_thumbs", rec("make_thumbs", 0))
    monkeypatch.setattr(run.notify, "push", lambda kind, j, **kw: calls.append(f"push:{kind}"))
    monkeypatch.setattr(run.discord, "alert", lambda msg: calls.append("alert:" + msg.split()[2]))
    orig = run.db.update_job

    def upd(jid, **cols):
        if cols.get("status") == "awaiting_pick":
            calls.append("update:awaiting_pick")
        orig(jid, **cols)
    monkeypatch.setattr(run.db, "update_job", upd)


def _job(jid):
    db.init()
    os.makedirs(run.job_dir(jid), exist_ok=True)
    db.insert_job(jid, kind="video", status="queued", duration_sec=10)
    return db.get_job(jid)


def test_ready_before_features(monkeypatch):
    calls = []
    _fake_prepare(monkeypatch, calls)
    j = _job("featorder1")
    run.do_prepare(j)
    assert calls.index("update:awaiting_pick") < calls.index("compute_features")
    assert calls.index("push:ready") < calls.index("compute_features")
    j = db.get_job("featorder1")
    assert j["status"] == "awaiting_pick" and j["features_state"] == "ready" and j["t_feature_sec"] is not None


def test_feature_failure_keeps_awaiting_pick(monkeypatch):
    calls = []
    _fake_prepare(monkeypatch, calls, feat_exc=RuntimeError("boom"))
    j = _job("featfail01")
    run.do_prepare(j)  # 例外を外に出さない（failed にしない）
    j = db.get_job("featfail01")
    assert j["status"] == "awaiting_pick" and j["features_state"] == "failed" and j["error"] is None
    assert any(c.startswith("alert:工程=特徴") for c in calls)
    assert "push:failed" not in calls


def test_reset_running_marks_pending_failed():
    db.init()
    db.insert_job("featreset1", kind="video", status="awaiting_pick", features_state="pending")
    db.reset_running()
    assert db.get_job("featreset1")["features_state"] == "failed"


@pytest.fixture
def client(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    now = time.time()
    u = db.create_user(line_user_id="Ufeat-" + str(now), display_name="特徴", trial_ends_at=now + 86400,
                       ig_asked_at=now, agreed_at=now)
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": u["id"]}), domain=HOST)
    seen = {}
    ve = main.vengine
    monkeypatch.setattr(ve, "load_tracks", lambda p: ([], 640, 1138, 300))

    def load_feats(p):
        seen["loaded"] = True
        return {1: "f"}, {1: "c"}
    monkeypatch.setattr(ve, "load_feats", load_feats)

    def decide(tracks, picks, face_m, cloth_m, short):
        seen["face_m"], seen["cloth_m"] = face_m, cloth_m
        return set(), set()
    monkeypatch.setattr(ve, "decide", decide)
    monkeypatch.setattr(ve, "review_lists", lambda *a: {"blur": [], "keep": []})
    monkeypatch.setattr(ve, "probe", lambda p: INFO)
    monkeypatch.setattr(ve, "compute_features", lambda *a, **kw: pytest.fail("app 側で特徴を計算してはいけない"))
    monkeypatch.setattr(main, "FEATURES_WAIT_SEC", 2)
    return c, u, seen


def _vjob(u, jid, state):
    os.makedirs(run.job_dir(jid), exist_ok=True)
    db.insert_job(jid, kind="video", status="awaiting_pick", user_id=u["id"], duration_sec=10,
                  expires_at=time.time() + 3600, features_state=state)


def test_picks_pending_returns_202(client):
    c, u, seen = client
    _vjob(u, "picks202aa", "pending")
    r = c.post("/api/jobs/picks202aa/picks", json={"picks": []})
    assert r.status_code == 202 and r.json() == {"status": "features_pending", "retry_after": 3}
    assert db.get_job("picks202aa")["status"] == "awaiting_pick" and "loaded" not in seen


def test_picks_ready_returns_200(client):
    c, u, seen = client
    _vjob(u, "picks200rd", "ready")
    r = c.post("/api/jobs/picks200rd/picks", json={"picks": []})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_review"
    assert seen["loaded"] and seen["face_m"] == {1: "f"}


def test_picks_failed_returns_200_without_features(client):
    c, u, seen = client
    _vjob(u, "picks200fl", "failed")
    r = c.post("/api/jobs/picks200fl/picks", json={"picks": []})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_review"
    assert "loaded" not in seen and seen["face_m"] == {} and seen["cloth_m"] == {}
