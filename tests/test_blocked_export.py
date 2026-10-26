"""利用停止中（お試し期限切れ・お支払い未確認）は書き出し（写真 export・動画 review）が 403。完成品の取得はできる。"""

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
from app.services import quota  # noqa: E402

HOST = "testserver.local"


@pytest.mark.parametrize("state", ["trial_expired", "past_due"])
def test_blocked_user_cannot_export_but_can_fetch_result(monkeypatch, state):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    now = time.time()
    if state == "trial_expired":
        u = db.create_user(line_user_id="Ublk-" + state, display_name="停止", trial_ends_at=now - 60,
                           ig_asked_at=now, agreed_at=now)
    else:
        u = db.create_user(line_user_id="Ublk-" + state, display_name="停止", trial_ends_at=now + 86400,
                           ig_asked_at=now, agreed_at=now)
        db.update_user(u["id"], plan="video_a", sub_status="past_due", past_due_since=now)
    assert quota.blocked(db.get_user(u["id"])) == state

    # 完成済みの写真（選び直しもできる状態）
    pid = "blkphoto" + state[:4]
    d = main.job_dir(pid)
    os.makedirs(d, exist_ok=True)
    src, out = os.path.join(d, "src.jpg"), os.path.join(d, "out.jpg")
    for p in (src, out):
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")
    db.insert_job(pid, kind="image", status="done", src_path=src, out_path=out, user_id=u["id"], width=10, height=10,
                  src_expires_at=now + 3600, out_expires_at=now + 86400)
    # 確認待ちの動画
    vid = "blkvideo" + state[:4]
    db.insert_job(vid, kind="video", status="awaiting_review", user_id=u["id"], duration_sec=30,
                  expires_at=now + 3600)

    c = TestClient(main.app)
    c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": u["id"]}), domain=HOST)
    r = c.post(f"/api/jobs/{pid}/export", json={"keep": [], "style": "pixel"})
    assert r.status_code == 403 and r.json()["detail"] == quota.BLOCKED_MSG[state]
    r = c.post(f"/api/jobs/{vid}/review", json={"keep": [], "blur": [], "style": "pixel"})
    assert r.status_code == 403 and r.json()["detail"] == quota.BLOCKED_MSG[state]
    assert db.get_job(vid)["status"] == "awaiting_review"
    # 完成品は保存期限まで取得できる
    r = c.get(f"/api/jobs/{pid}/result.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8\xff\xd9"
