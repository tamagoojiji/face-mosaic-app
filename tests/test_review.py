"""アダルトの疑いの確認（services/review.py）: 止める → ⭕ で続行 / ❌・24時間で削除。Discord は差し替える。"""

import io
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

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import config, db, main  # noqa: E402
from app.services import review  # noqa: E402
from worker import run  # noqa: E402
from worker.engine import nsfw  # noqa: E402

HOST = "testserver.local"


@pytest.fixture
def fake_discord(monkeypatch):
    """Discord を差し替え: reactions[emoji] = 押した人の ID の集合。"""
    st = {"reactions": {review.OK: set(), review.NG: set()}, "puts": [], "edits": [], "pushes": []}
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "bot")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_ALERTS", "https://discord.test/webhook")
    monkeypatch.setattr(review.discord, "post_wait", lambda ch, text: {"id": "m1", "channel_id": "c1"})
    monkeypatch.setattr(review.discord, "edit", lambda ch, mid, text: st["edits"].append(text))
    monkeypatch.setattr(review.discord, "alert", lambda msg: None)
    monkeypatch.setattr(run.discord, "alert", lambda msg: None)
    monkeypatch.setattr(review.time, "sleep", lambda s: None)
    monkeypatch.setattr(review, "_ME", ["bot-id"])

    def bot(method, path):
        if method == "PUT":
            st["puts"].append(path)
            return None
        if "/reactions/" in path:
            for e, users in st["reactions"].items():
                if path.endswith(review.urllib.parse.quote(e)):
                    return [{"id": u} for u in users | {"bot-id"}]
        return {"content": "元の文面"}
    monkeypatch.setattr(review, "_bot", bot)
    for mod in (review.notify, run.notify):
        monkeypatch.setattr(mod, "push", lambda kind, j, **kw: st["pushes"].append(kind))
    return st


def _video_job(jid):
    db.init()
    p = run.paths(jid)
    os.makedirs(p["dir"], exist_ok=True)
    for k in ("src", "v1080"):
        open(p[k], "wb").close()
    db.insert_job(jid, kind="video", status="preparing", duration_sec=10, user_id=None)
    return db.get_job(jid)


def _blocked():
    return nsfw.Blocked("動画 3/5 コマ max=0.93", [np.zeros((100, 80, 3), np.uint8)] * 2)


def test_hold_puts_both_stamps_and_saves_frames(fake_discord):
    j = _video_job("rv1")
    assert review.hold(j, _blocked())
    j = db.get_job("rv1")
    assert j["status"] == "nsfw_review" and j["nsfw_msg"] == "c1/m1" and "3/5" in j["nsfw_info"]
    assert len(fake_discord["puts"]) == 2  # ⭕ と ❌
    assert sorted(os.listdir(review.frames_dir("rv1"))) == ["1.jpg", "2.jpg"]


def test_hold_disabled_without_bot(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    assert review.hold(_video_job("rv2"), _blocked()) is False
    assert db.get_job("rv2")["status"] == "preparing"


def test_ok_stamp_requeues_video(fake_discord):
    review.hold(_video_job("rv3"), _blocked())
    review.poll()
    assert db.get_job("rv3")["status"] == "nsfw_review"  # 押されるまで待つ
    fake_discord["reactions"][review.OK].add("tamago")
    review.poll()
    j = db.get_job("rv3")
    assert j["status"] == "queued" and j["nsfw_ok"] == 1
    assert not os.path.exists(review.frames_dir("rv3"))
    assert fake_discord["edits"] and "続行" in fake_discord["edits"][0]


def test_ng_stamp_deletes_video_and_wins_over_ok(fake_discord):
    review.hold(_video_job("rv4"), _blocked())
    fake_discord["reactions"][review.OK].add("tamago")
    fake_discord["reactions"][review.NG].add("tamago")
    review.poll()
    j = db.get_job("rv4")
    assert j["status"] == "failed" and j["error"] == nsfw.BLOCK_MSG
    assert not os.path.exists(run.paths("rv4")["src"])
    assert "failed" in fake_discord["pushes"]


def test_timeout_deletes(fake_discord):
    review.hold(_video_job("rv5"), _blocked())
    review.poll(now=time.time() + review.HOLD_HOURS * 3600 + 1)
    assert db.get_job("rv5")["status"] == "failed"


def test_prepare_skips_check_after_ok(monkeypatch):
    calls = []
    monkeypatch.setattr(run.video, "prepare", lambda *a, **kw: ({"fps": 30.0, "out_width": 1080, "out_height": 1920,
                                                                 "duration": 10.0}, "", {"mode": None}))
    monkeypatch.setattr(run.video, "to_1080", lambda *a, **kw: None)
    monkeypatch.setattr(run.nsfw, "check_video", lambda p: calls.append("check"))

    def stop(*a, **kw):
        raise RuntimeError("追跡まで来た")
    monkeypatch.setattr(run.video, "get_tracks", stop)
    j = _video_job("rv6")
    db.update_job("rv6", nsfw_ok=1)
    with pytest.raises(RuntimeError):
        run.do_prepare(db.get_job("rv6"))
    assert calls == []


def _login(monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    monkeypatch.setattr(config, "APP_PASSCODE", "pw")
    monkeypatch.setattr(config, "SESSION_SECRET", "s" * 32)
    db.init()
    c = TestClient(main.app, base_url=f"http://{HOST}")
    assert c.post("/login", data={"passcode": "pw"}, follow_redirects=False).status_code == 303
    return c


def test_photo_hold_then_ok_becomes_pickable(monkeypatch, fake_discord):
    c = _login(monkeypatch)
    scores = iter([0.99])
    real = nsfw.score_bgr
    monkeypatch.setattr(nsfw, "score_bgr", lambda bgr: next(scores, None) or real(bgr))
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 150, 120)).save(buf, "JPEG")
    r = c.post("/api/photos", files={"file": ("a.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 409 and r.json()["detail"] == review.WAIT_MSG
    j = db.jobs_with_status("nsfw_review")[-1]
    assert j["kind"] == "image" and os.path.exists(os.path.join(review.job_dir(j["id"]), "hold.bin"))
    fake_discord["reactions"][review.OK].add("tamago")
    review.poll()
    j = db.get_job(j["id"])
    assert j["status"] == "awaiting_pick" and os.path.exists(j["src_path"])
    assert "ready" in fake_discord["pushes"]


def test_admin_review_page_and_images(monkeypatch, fake_discord):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    review.hold(_video_job("rvAdmin7xxxx"), _blocked())

    def client(device, role):
        db.user_by("device_id", device) or db.create_user(device_id=device, display_name=device, role=role)
        c = TestClient(main.app)
        c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"t": int(time.time())}))
        c.cookies.set(config.OWNER_COOKIE, main._owner_serializer().dumps(device))
        return c
    a, u = client("rv-admin", "admin"), client("rv-user", "user")
    assert a.get("/admin/review/rvAdmin7xxxx").status_code == 200
    d = a.get("/admin/api/review/rvAdmin7xxxx").json()
    assert d["status"] == "nsfw_review" and d["images"] == 2
    r = a.get("/admin/api/review/rvAdmin7xxxx/1.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    for path in ("/admin/api/review/rvAdmin7xxxx", "/admin/api/review/rvAdmin7xxxx/1.jpg"):
        assert u.get(path).status_code == 404  # 利用者には存在しない扱い


def test_review_link_key_opens_without_login(monkeypatch, fake_discord):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    review.hold(_video_job("rvKeyJob0001"), _blocked())
    k = review.link_key("rvKeyJob0001")
    anon = TestClient(main.app)
    assert anon.get(f"/admin/review/rvKeyJob0001?k={k}", follow_redirects=False).status_code == 200
    assert anon.get(f"/admin/api/review/rvKeyJob0001?k={k}").json()["images"] == 2
    assert anon.get(f"/admin/api/review/rvKeyJob0001/1.jpg?k={k}").status_code == 200
    # 鍵なし・他のジョブの鍵・壊れた鍵は開けない
    other = review.link_key("rvOtherJob001")
    for q in ("", f"?k={other}", "?k=broken"):
        assert anon.get(f"/admin/review/rvKeyJob0001{q}", follow_redirects=False).status_code == 302
        assert anon.get(f"/admin/api/review/rvKeyJob0001{q}").status_code == 401
        assert anon.get(f"/admin/api/review/rvKeyJob0001/1.jpg{q}").status_code == 401
    # 期限切れ（max_age が負になるよう確認の時間を縮める）
    monkeypatch.setattr(review, "HOLD_HOURS", -2)
    assert not review.key_ok("rvKeyJob0001", k)
