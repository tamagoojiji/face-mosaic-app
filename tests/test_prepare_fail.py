"""解析の失敗: 利用者への理由の出し分けと、使った分（秒・本数）が戻ること（テスターも通常も）。ネットワークは使わない。"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# test_tester.py と同じ環境（config は最初に読んだファイルの値で固まるため、そろえておく）
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fm-test-"))
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
for k in ("LINE_MSG_CHANNEL_ACCESS_TOKEN", "DISCORD_WEBHOOK_ALERTS", "DISCORD_WEBHOOK_REPORTS", "DISCORD_WEBHOOK_USAGE",
          "APP_BASE_URL"):
    os.environ.pop(k, None)

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.services import quota  # noqa: E402
from worker import run  # noqa: E402


@pytest.fixture(autouse=True)
def env():
    db.init()


def test_msg_bug_vs_unreadable():
    bug = run.prepare_fail_msg(RuntimeError("フレーム抽出に失敗 (17.67s): Nothing was written"))
    assert "こちらの不具合" in bug and "iPhone" not in bug
    assert "iPhone のカメラ" in run.prepare_fail_msg(RuntimeError("ffprobe に失敗: Invalid data"))
    assert "HDR" in run.prepare_fail_msg(RuntimeError("HDR(PQ) は未対応: x"))


@pytest.mark.parametrize("role", ["tester", "user"])
def test_usage_refunded_on_fail(role):
    now = time.time()
    u = db.create_user(line_user_id=f"Urefund-{role}", display_name="t", trial_ends_at=now + 86400,
                       ig_asked_at=now, agreed_at=now)
    db.update_user(u["id"], role=role)
    month = quota.month_of()
    for n in range(3):  # 今回と同じく3回続けて失敗
        jid = f"refund{role}{n}"
        db.insert_job(jid, kind="video", status="queued", bytes=1, user_id=u["id"])
        quota.count_video(db.get_job(jid), 17.7)
        assert db.get_usage(u["id"], month)["video_count"] == 1
        run.fail_prepare(jid, "x", RuntimeError("フレーム抽出に失敗"))
        got = db.get_usage(u["id"], month)
        assert got["video_count"] == 0 and got["video_seconds_used"] == 0, got
