"""性的な内容の判定（worker.engine.nsfw）: 止める条件・動画の失敗扱い・写真の 400。"""

import io
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import config, db, main  # noqa: E402
from worker import run  # noqa: E402
from worker.engine import nsfw  # noqa: E402

HOST = "testserver.local"


def _video(path, n=90, fps=30):
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240))
    for i in range(n):
        w.write(np.full((240, 320, 3), i % 255, np.uint8))
    w.release()


def test_model_scores_plain_image_low():
    assert nsfw.score_bgr(np.full((480, 640, 3), 128, np.uint8)) < nsfw.THRESHOLD


def test_video_needs_two_hits(monkeypatch, tmp_path):
    p = str(tmp_path / "v.mp4")
    _video(p)  # 3秒 → 2秒に1コマで 2コマ調べる
    scores = iter([0.95, 0.1])
    monkeypatch.setattr(nsfw, "score_bgr", lambda bgr: next(scores))
    r = nsfw.check_video(p)  # 1コマだけなら止めない
    assert r["frames"] == 2 and r["hits"] == 1
    monkeypatch.setattr(nsfw, "score_bgr", lambda bgr: 0.9)
    with pytest.raises(nsfw.Blocked):
        nsfw.check_video(p)


def test_prepare_blocked_before_tracking(monkeypatch):
    calls = []
    monkeypatch.setattr(run.video, "prepare", lambda *a, **kw: ({"fps": 30.0, "out_width": 1080, "out_height": 1920,
                                                                 "duration": 10.0}, "", {"mode": None}))
    monkeypatch.setattr(run.video, "to_1080", lambda *a, **kw: None)
    monkeypatch.setattr(run.video, "get_tracks", lambda *a, **kw: calls.append("get_tracks"))

    def blocked(path):
        raise nsfw.Blocked("動画 3/5 コマ")
    monkeypatch.setattr(run.nsfw, "check_video", blocked)
    db.init()
    os.makedirs(run.job_dir("nsfwjob1"), exist_ok=True)
    db.insert_job("nsfwjob1", kind="video", status="queued", duration_sec=10)
    with pytest.raises(nsfw.Blocked):
        run.do_prepare(db.get_job("nsfwjob1"))
    assert calls == []  # 顔の追跡まで進まない


def _login(monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    monkeypatch.setattr(config, "APP_PASSCODE", "pw")
    monkeypatch.setattr(config, "SESSION_SECRET", "s" * 32)
    db.init()
    c = TestClient(main.app, base_url=f"http://{HOST}")
    assert c.post("/login", data={"passcode": "pw"}, follow_redirects=False).status_code == 303
    return c


def test_photo_blocked_returns_400_and_deletes(monkeypatch):
    c = _login(monkeypatch)
    monkeypatch.setattr(nsfw, "score_bgr", lambda bgr: 0.99)
    alerts = []
    monkeypatch.setattr(main.discord, "alert", lambda msg: alerts.append(msg))
    before = set(os.listdir(os.path.join(config.DATA_DIR, "jobs"))) if os.path.isdir(
        os.path.join(config.DATA_DIR, "jobs")) else set()
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 150, 120)).save(buf, "JPEG")
    r = c.post("/api/photos", files={"file": ("a.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 400 and r.json()["detail"] == nsfw.BLOCK_MSG
    after = set(os.listdir(os.path.join(config.DATA_DIR, "jobs")))
    assert after == before  # 写真のフォルダは残さない
    assert alerts and "不適切判定" in alerts[0]
