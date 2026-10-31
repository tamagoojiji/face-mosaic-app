"""問題の報告: 対象を選んだときの確認の同意・画面録画の添付・管理画面から添付と対象ジョブを開く。"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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
from app.services import discord, retention  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    sent = []
    monkeypatch.setattr(discord, "send", lambda ch, text, wait=False, files=None: sent.append((ch, text, files)))
    return sent


def login(device, role="user"):
    u = db.user_by("device_id", device) or db.create_user(device_id=device, display_name=device, role=role)
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"t": int(time.time())}))
    c.cookies.set(config.OWNER_COOKIE, main._owner_serializer().dumps(device))
    return c, u


NOFILE = [("images", ("", b"", "application/octet-stream"))]


def _tmp_files():
    d = os.path.join(config.DATA_DIR, "reports")
    return [n for n in os.listdir(d) if n.startswith("tmp-")] if os.path.isdir(d) else []


def test_consent_required_with_job(env):
    c, u = login("dev-rv-consent")
    db.insert_job("jobRVC000001", kind="video", status="done", user_id=u["id"], duration_sec=10.0)
    r = c.post("/api/reports", json={"category": "out_uncovered", "job_id": "jobRVC000001"})
    assert r.status_code == 400 and "同意" in r.json()["detail"]
    r = c.post("/api/reports", json={"category": "out_uncovered", "job_id": "jobRVC000001", "consent": "1"})
    assert r.status_code == 201, r.text
    assert "対象の確認: 同意あり" in env[-1][1]
    # multipart でも同じ（ブラウザの FormData。空のファイル欄で multipart にする）（チェックボックスの "on" も同意）
    r = c.post("/api/reports", data={"category": "out_uncovered", "job_id": "jobRVC000001"}, files=NOFILE)
    assert r.status_code == 400
    r = c.post("/api/reports", data={"category": "out_uncovered", "job_id": "jobRVC000001", "consent": "on"}, files=NOFILE)
    assert r.status_code == 201
    # 対象なしなら同意は要らない
    assert c.post("/api/reports", json={"category": "login"}).status_code == 201


def test_report_video(env, monkeypatch):
    c, u = login("dev-rv-video")
    r = c.post("/api/reports", data={"category": "out_uncovered", "body": "録画つき"},
               files=[("video", ("rec.mp4", b"fake-mp4-bytes", "video/mp4"))])
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert db.get_report(rid)["video_name"] == "video.mp4"
    p = os.path.join(retention.report_dir(rid), "video.mp4")
    assert os.path.exists(p) and open(p, "rb").read() == b"fake-mp4-bytes"
    assert "動画 1本" in env[-1][1] and not env[-1][2]  # Discord には添付しない
    assert not _tmp_files()
    # 拡張子が違う → 400（記録しない・一時ファイルも残らない）
    with db.connect() as con:
        before = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    r = c.post("/api/reports", data={"category": "login"}, files=[("video", ("x.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 400 and "mp4" in r.json()["detail"]
    # 100MB 超 → 400（一時ファイルを消す）
    monkeypatch.setattr(main, "REPORT_VIDEO_BYTES", 10)
    r = c.post("/api/reports", data={"category": "login"}, files=[("video", ("big.mov", b"x" * 11, "video/quicktime"))])
    assert r.status_code == 400 and r.json()["detail"] == "動画は1本100MBまでです"
    assert not _tmp_files()
    # 受け取ったあとのバリデーションで落ちても一時ファイルは残らない
    r = c.post("/api/reports", data={"category": "nope"}, files=[("video", ("s.webm", b"x" * 5, "video/webm"))])
    assert r.status_code == 400 and not _tmp_files()
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == before


def test_admin_report_files(env):
    c, u = login("dev-rv-admin")
    jid = "jobRVA000001"
    db.insert_job(jid, kind="video", status="done", user_id=u["id"], duration_sec=10.0)
    r = c.post("/api/reports", data={"category": "out_uncovered", "job_id": jid, "consent": "1"},
               files=[("video", ("rec.mov", b"0123456789" * 10, "video/quicktime"))])
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    a, _ = login("dev-rv-admin-a", role="admin")
    g = a.get(f"/admin/api/reports/{rid}/video")
    assert g.status_code == 200 and g.headers["content-type"] == "video/quicktime"
    g = a.get(f"/admin/api/reports/{rid}/video", headers={"Range": "bytes=0-9"})
    assert g.status_code == 206 and g.content == b"0123456789"
    assert c.get(f"/admin/api/reports/{rid}/video").status_code == 404  # 管理者だけ
    # 対象ジョブのファイルはまだ無い → 404・一覧では False
    assert a.get(f"/admin/api/reports/{rid}/job/source").status_code == 404
    assert a.get(f"/admin/api/reports/{rid}/job/output").status_code == 404
    row = next(x for x in a.get("/admin/api/reports").json()["reports"] if x["id"] == rid)
    assert row["video_name"] == "video.mov" and row["job_files"] == {"source": False, "output": False}
    # 置くと開ける
    d = main.job_dir(jid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "src.mp4"), "wb").write(b"src")
    out = os.path.join(d, "out.mp4")
    open(out, "wb").write(b"out")
    db.update_job(jid, out_path=out)
    s = a.get(f"/admin/api/reports/{rid}/job/source")
    assert s.status_code == 200 and s.content == b"src" and s.headers["content-type"] == "video/mp4"
    assert a.get(f"/admin/api/reports/{rid}/job/output").content == b"out"
    assert a.get(f"/admin/api/reports/{rid}/job/other").status_code == 404
    row = next(x for x in a.get("/admin/api/reports").json()["reports"] if x["id"] == rid)
    assert row["job_files"] == {"source": True, "output": True}


def test_report_video_expire():
    u = db.create_user(device_id="dev-rv-old", display_name="古い")
    rid = db.add_report(u["id"], "login", None)
    d = retention.report_dir(rid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "video.mp4"), "wb").write(b"x")
    db.update_report(rid, video_name="video.mp4", created_at=time.time() - 91 * 86400)
    assert retention.cleanup_report_images() >= 1
    assert not os.path.exists(d) and db.get_report(rid)["video_name"] is None


def test_privacy_mentions_review_limit():
    r = TestClient(main.app).get("/legal/privacy")
    assert r.status_code == 200 and "確認は、報告があったものと、自動の確認で性的な内容の疑いがあったもの（その場面の画像だけ）に限ります" in r.text


def test_report_saves_analysis(env):
    """対象の動画の解析データ（判定を動画なしで再現できる組）を通報に控え、90日で消す。元動画・完成品は控えない。"""
    c, u = login("dev-rv-analysis")
    jid = "jobRVS000001"
    db.insert_job(jid, kind="video", status="done", user_id=u["id"], duration_sec=10.0)
    db.update_job(jid, picks="[3, 5]", keep="[3, 5, 7]", blur="[1]")
    d = retention.job_dir(jid)
    os.makedirs(os.path.join(d, "thumbs"), exist_ok=True)
    for name, data in (("tracks.json", b"{}"), ("features.npz", b"npz"), ("src.mp4", b"v"), ("out.mp4", b"o"),
                       ("thumbs/t3.jpg", b"j")):
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
    r = c.post("/api/reports", json={"category": "out_wrong", "job_id": jid, "consent": "1"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    a = os.path.join(retention.report_dir(rid), "analysis")
    assert db.get_report(rid)["analysis"] == 1
    assert sorted(os.listdir(a)) == ["features.npz", "meta.json", "thumbs", "tracks.json"]
    meta = main.json.load(open(os.path.join(a, "meta.json"), encoding="utf-8"))
    assert meta["picks"] == [3, 5] and meta["keep"] == [3, 5, 7] and meta["missing"] == ["review.json", "shots.json", "shots"]
    assert retention.cleanup_report_images(time.time() + 91 * 86400) >= 1
    assert not os.path.exists(a) and db.get_report(rid)["analysis"] == 0


def test_report_analysis_gone(env):
    """解析データが消えた後の通報は控えなし（受け付けはする）。"""
    c, u = login("dev-rv-gone")
    db.insert_job("jobRVS000002", kind="video", status="done", user_id=u["id"], duration_sec=10.0)
    r = c.post("/api/reports", json={"category": "out_wrong", "job_id": "jobRVS000002", "consent": "1"})
    assert r.status_code == 201
    assert db.get_report(r.json()["id"])["analysis"] == 0
    assert not os.path.exists(os.path.join(retention.report_dir(r.json()["id"]), "analysis"))
