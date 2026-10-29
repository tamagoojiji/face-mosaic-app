"""確認画面の画質の 2 択（POST review の hq）と、書き出しの見込み（負荷係数・処理の版での切り分け）。"""

import json
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
from worker.engine import video as vengine  # noqa: E402

HOST = "testserver.local"


@pytest.mark.parametrize("ready,body_hq,want", [
    (True, False, 0),   # ① 高画質で書き出せる人が 1080p を選んだ
    (True, True, 1),    # ② 元の画質を選んだ
    (False, True, 0),   # ③ 書き出せない人が hq を送っても 1080p
    (True, None, 1),    # ④ hq なし（古い画面）は従来どおり
])
def test_review_hq_choice(monkeypatch, ready, body_hq, want):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    monkeypatch.setattr(main, "_hq_ready", lambda j: ready)
    now = time.time()
    tag = f"{int(ready)}{body_hq}"
    u = db.create_user(line_user_id="Uq-" + tag, display_name="画質", trial_ends_at=now + 86400,
                       ig_asked_at=now, agreed_at=now)
    vid = "qvideo" + tag
    p = main.vpaths(vid)
    os.makedirs(p["dir"], exist_ok=True)
    with open(p["v1080"], "wb") as f:
        f.write(b"x")
    with open(os.path.join(p["dir"], "review.json"), "w") as f:
        json.dump({"lists": {"blur": [{"id": 1, "sec": 1}], "keep": []}, "auto_keep": [], "auto_blur": [1]}, f)
    db.insert_job(vid, kind="video", status="awaiting_review", user_id=u["id"], duration_sec=30,
                  width=3840, height=2160, fps=60, expires_at=now + 3600)
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": u["id"]}), domain=HOST)
    body = {"keep": [], "blur": [1], "style": "pixel"}
    if body_hq is not None:
        body["hq"] = body_hq
    r = c.post(f"/api/jobs/{vid}/review", json=body)
    assert r.status_code == 200, r.text
    j = db.get_job(vid)
    assert j["status"] == "queued_export" and j["hq_export"] == want


def test_load_factor_bounds():
    assert main.load_factor({"width": 3840, "height": 2160, "fps": 60}) == pytest.approx(1.0)
    assert main.load_factor({"width": 1920, "height": 1080, "fps": 30}) == pytest.approx(0.125)
    assert main.load_factor({}) == 1.0
    assert main.load_factor({"width": 3840, "height": 2160, "fps": None}) == 1.0
    assert main.load_factor({"width": 320, "height": 240, "fps": 15}) == 0.05
    assert main.load_factor({"width": 7680, "height": 4320, "fps": 60}) == 1.5


def _row(ver, sec_per_min, w=3840, h=2160, fps=60, hq=1, style="pixel"):
    return {"duration_sec": 60, "width": w, "height": h, "fps": fps, "engine_ver": ver, "mosaic_style": style,
            "hq_export": hq, "t_prepare_sec": 100, "t_track_sec": 40, "t_feature_sec": 50, "t_export_sec": sec_per_min}


def test_rates_ignore_other_engine_ver(monkeypatch):
    rows = [_row("old", 5000, hq=0), _row("old", 9999), {**_row("old", 1), "t_prepare_sec": 999}]
    monkeypatch.setattr(db, "done_video_stats", lambda limit=30: rows)
    r = main._rates()
    assert r["prepare"] == main.FALLBACK_PER_MIN["prepare"]
    assert r["export_pixel"] == main.FALLBACK_PER_MIN["export_pixel"]
    assert r["export_hq_pixel"] == main.FALLBACK_HQ_PER_MIN_4K60
    rows.append(_row(vengine.ENGINE_VER, 70, hq=0))
    r = main._rates()
    assert r["export_pixel"] == 70 and r["prepare"] == 100


def test_hq_rate_normalized_to_4k60(monkeypatch):
    rows = [_row(vengine.ENGINE_VER, 825), _row(vengine.ENGINE_VER, 103, w=1920, h=1080, fps=30)]
    monkeypatch.setattr(db, "done_video_stats", lambda limit=30: rows)
    r = main._rates()
    assert r["export_hq_pixel"] == pytest.approx(825, rel=0.01)
    # 見積もりはこのジョブの負荷係数を掛ける（1080p30 の 2 分 ≒ 825×2×0.125）
    j = {"mosaic_style": "pixel", "duration_sec": 120, "width": 1920, "height": 1080, "fps": 30}
    assert main._export_est(j, r, "pixel", True) == pytest.approx(825 * 2 * 0.125, rel=0.01)
    assert main._export_est(j, r, "pixel", False) == pytest.approx(main.FALLBACK_PER_MIN["export_pixel"] * 2)
