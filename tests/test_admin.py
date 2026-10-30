"""③b 段②: 管理画面の認可・通報・利用者の変更の記録・設定の上書き。ネットワークは使わない。"""

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

import io  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app import main  # noqa: E402
from app.services import discord, notify, quota, retention  # noqa: E402


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")


def login(device, role="user"):
    """passcode モードの端末利用者としてログインした TestClient。"""
    u = db.user_by("device_id", device) or db.create_user(device_id=device, display_name=device, role=role)
    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"t": int(time.time())}))
    c.cookies.set(config.OWNER_COOKIE, main._owner_serializer().dumps(device))
    return c, u


def test_require_admin_404():
    c, _ = login("dev-user1")
    for path in ("/admin", "/admin/users", "/admin/api/dashboard", "/admin/api/users", "/admin/api/settings"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 404, path
        assert r.json() == {"detail": "Not Found"}
    # 未ログイン: 画面はログインへ、API は 401
    anon = TestClient(main.app)
    assert anon.get("/admin", follow_redirects=False).status_code == 302
    assert anon.get("/admin/api/dashboard").status_code == 401
    a, _ = login("dev-admin1", role="admin")
    assert a.get("/admin").status_code == 200
    assert a.get("/admin/api/dashboard").status_code == 200


def test_post_report(monkeypatch):
    sent = []
    monkeypatch.setattr(discord, "send", lambda ch, text, wait=False, files=None: sent.append((ch, text)))
    c, u = login("dev-rep1")
    db.insert_job("jobREP000001", kind="video", status="done", user_id=u["id"], duration_sec=83.0)
    r = c.post("/api/reports", json={"category": "out_uncovered", "body": "  0:42 で顔が見えます  ", "job_id": "jobREP000001",
                                          "consent": "1"})
    assert r.status_code == 201
    rep = db.get_report(r.json()["id"])
    assert rep["user_id"] == u["id"] and rep["category"] == "out_uncovered" and rep["body"] == "0:42 で顔が見えます"
    assert rep["job_id"] == "jobREP000001" and rep["status"] == "open"
    assert sent and sent[-1][0] == "reports" and "動画 1分23秒" in sent[-1][1]
    assert "仕上がり › 顔が隠れていない場面がある" in sent[-1][1]
    # 内容は任意（空は None）。「その他」だけ必須
    r = c.post("/api/reports", json={"category": "out_uncovered", "body": "   "})
    assert r.status_code == 201 and db.get_report(r.json()["id"])["body"] is None
    r = c.post("/api/reports", json={"category": "other", "body": ""})
    assert r.status_code == 400 and "その他" in r.json()["detail"]
    assert c.post("/api/reports", json={"category": "other", "body": "そのほか"}).status_code == 201
    assert c.post("/api/reports", json={"category": "bug", "body": "x"}).status_code == 400  # 旧コード・未知のコード
    assert c.post("/api/reports", json={"body": "x"}).status_code == 400
    assert c.post("/api/reports", json={"category": "login", "body": "x" * 501}).status_code == 400
    # 他の人のジョブは選べない
    other = db.create_user(device_id="dev-rep2", display_name="他人")
    db.insert_job("jobREP000002", kind="image", status="done", user_id=other["id"])
    assert c.post("/api/reports", json={"category": "login", "body": "x", "job_id": "jobREP000002"}).status_code == 400
    # 管理画面: 段階と種類は日本語、段階で絞り込める
    a, _ = login("dev-admin-rep", role="admin")
    d = a.get("/admin/api/reports?stage=仕上がり").json()
    assert d["reports"] and all(x["stage"] == "仕上がり" for x in d["reports"])
    assert d["reports"][-1]["category_label"] == "顔が隠れていない場面がある"
    assert d["stages"][0] == "送る" and len(d["stages"]) == 6
    assert a.get("/admin/api/reports?stage=nope").status_code == 400


def _audit_count():
    with db.connect() as con:
        return con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def test_admin_user_update_audits(monkeypatch):
    monkeypatch.setattr(discord, "send", lambda *a, **k: True)
    a, admin = login("dev-admin2", role="admin")
    target = db.create_user(device_id="dev-target", display_name="対象")
    n = _audit_count()
    r = a.post(f"/admin/api/users/{target['id']}", json={"plan": "video_a", "hq": True, "role": "user", "status": "active"})
    assert r.status_code == 200, r.text
    assert _audit_count() == n + 1
    v = db.get_user(target["id"])
    assert v["plan"] == "video_a" and v["hq"] == 1
    with db.connect() as con:
        row = con.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["actor_id"] == admin["id"] and row["action"] == "admin.user_update" and row["target"] == f"user:{target['id']}"
    # 変更なしは記録しない・高画質は動画A/Bだけ
    assert a.post(f"/admin/api/users/{target['id']}", json={"plan": "video_a", "hq": True}).status_code == 200
    assert _audit_count() == n + 1
    assert a.post(f"/admin/api/users/{target['id']}", json={"plan": "image", "hq": True}).status_code == 400
    # 詳細に LINE の userId・Stripe の ID は出さない
    db.update_user(target["id"], line_user_id="Usecret_line", stripe_customer_id="cus_secret")
    body = a.get(f"/admin/api/users/{target['id']}").text
    assert "Usecret_line" not in body and "cus_secret" not in body


def test_settings_override_retention(monkeypatch):
    pushed = []
    monkeypatch.setattr(notify, "push", lambda kind, j, reason=None: pushed.append((kind, j["id"])))
    now = time.time()
    db.insert_job("jobIDLE00001", kind="video", status="awaiting_pick", expires_at=now + 5 * 3600)
    # 既定（再通知1時間・削除3時間 → 残り2時間で通知）では対象外
    retention.notify_idle(now)
    assert ("idle", "jobIDLE00001") not in pushed
    # 上書き（削除8時間 → 残り7時間で通知）で対象になる
    a, _ = login("dev-admin3", role="admin")
    r = a.post("/admin/api/settings", json={"idle_warn_hours": "1", "idle_delete_hours": "8", "source_retention_hours": "48"})
    assert r.status_code == 200, r.text
    assert quota.idle_delete_hours() == 8 and quota.source_hours() == 48
    retention.notify_idle(now)
    assert ("idle", "jobIDLE00001") in pushed
    assert notify.message("idle", {"id": "x", "status": "awaiting_pick"}).startswith("まだ選択が残っています。あと7時間")
    # 不正値は 400（再通知 ≥ 削除）
    assert a.post("/admin/api/settings", json={"idle_warn_hours": "9"}).status_code == 400
    for k in ("idle_warn_hours", "idle_delete_hours", "source_retention_hours"):
        with db.connect() as con:
            con.execute("DELETE FROM settings WHERE key=?", (k,))


def _png(w=40, h=30):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(b, "PNG")
    return b.getvalue()


def test_report_images(monkeypatch):
    sent = []
    monkeypatch.setattr(discord, "send", lambda ch, text, wait=False, files=None: sent.append((ch, text, files)))
    c, u = login("dev-img1")
    r = c.post("/api/reports", data={"category": "out_uncovered", "body": "画像つき"},
               files=[("images", ("a.png", _png(), "image/png"))])
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert db.get_report(rid)["image_count"] == 1
    p = os.path.join(retention.report_dir(rid), "1.jpg")
    assert os.path.exists(p) and Image.open(p).format == "JPEG"
    assert sent[-1][2] and len(sent[-1][2]) == 1 and "画像 1 枚" in sent[-1][1]
    # 管理画面だけが見られる
    a, _ = login("dev-admin-img", role="admin")
    g = a.get(f"/admin/api/reports/{rid}/images/1")
    assert g.status_code == 200 and g.headers["content-type"] == "image/jpeg"
    assert a.get(f"/admin/api/reports/{rid}/images/2").status_code == 404
    assert c.get(f"/admin/api/reports/{rid}/images/1").status_code == 404
    # 4枚・画像でないファイルは 400（記録もしない）
    with db.connect() as con:
        before = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    r = c.post("/api/reports", data={"category": "login"}, files=[("images", (f"{i}.png", _png(), "image/png")) for i in range(4)])
    assert r.status_code == 400 and "3枚" in r.json()["detail"]
    r = c.post("/api/reports", data={"category": "login"}, files=[("images", ("x.png", b"not an image", "image/png"))])
    assert r.status_code == 400 and r.json()["detail"] == "画像ファイルを選んでください"
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == before
    # 退会で画像は消える（通報の文字は残る）
    monkeypatch.setattr(notify, "push_user", lambda *a, **k: False)
    assert c.post("/api/me/withdraw", json={"reason": "other"}).status_code == 200
    assert not os.path.exists(retention.report_dir(rid))
    rep = db.get_report(rid)
    assert rep["image_count"] == 0 and rep["body"] == "画像つき"


def test_report_images_expire():
    u = db.create_user(device_id="dev-img-old", display_name="古い")
    rid = db.add_report(u["id"], "login", None)
    d = retention.report_dir(rid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "1.jpg"), "wb").write(b"x")
    db.update_report(rid, image_count=1, created_at=time.time() - 91 * 86400)
    assert retention.cleanup_report_images() >= 1
    assert not os.path.exists(d) and db.get_report(rid)["image_count"] == 0
