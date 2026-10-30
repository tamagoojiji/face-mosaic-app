"""公式 LINE の複数運用（2026-10-30）: 受付用の返信・ログインの振り分け・番号ごとの通知と Webhook・管理 API。ネットワークは使わない。"""

import base64
import hashlib
import hmac
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

import urllib.request  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app import main  # noqa: E402
from app.services import discord, notify  # noqa: E402

SEC = {0: "sec-reception", 1: "sec-one", 2: "sec-two"}
TOK = {0: "tok-reception-0000", 1: "tok-one-1111", 2: "tok-two-2222"}
BASE = {}  # ほかのテストファイルが同じ DB に残した利用者の分（定員は「空き」で決める）


class FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def env(monkeypatch):
    db.init()
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH_MODE", "line")
    monkeypatch.setattr(config, "LINE_MOCK", True)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://example.test")
    monkeypatch.setattr(discord, "send", lambda *a, **k: True)
    monkeypatch.setattr(main, "_notify_registered", lambda u: None)
    # channel 1 は env からの取り込み（DB に行が無いときだけ）
    monkeypatch.setattr(config, "LINE_MSG_CHANNEL_ACCESS_TOKEN", TOK[1])
    monkeypatch.setattr(config, "LINE_MSG_CHANNEL_SECRET", SEC[1])
    db.ensure_channel_from_env()
    BASE.update({n: db.channel_used(n) for n in (1, 2)})
    db.upsert_channel(1, capacity=BASE[1] + 1)
    db.upsert_channel(2, name="らくらくモザイク②", kind="notify", basic_id="@two", capacity=BASE[2] + 2,
                      access_token=TOK[2], channel_secret=SEC[2])
    db.upsert_channel(0, name="らくらくモザイク 受付", kind="reception", basic_id="@recv",
                      access_token=TOK[0], channel_secret=SEC[0])
    sent = []

    def fake(req, timeout=None):
        sent.append({"url": req.full_url, "auth": req.get_header("Authorization"), "body": json.loads(req.data)})
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    yield sent
    with db.connect() as con:
        con.execute("DELETE FROM line_channels")
        con.execute("DELETE FROM line_assignments")
        con.execute("DELETE FROM users WHERE line_user_id LIKE 'Ulc%'")
    db.set_setting("signup_remaining", "")


def sign(body, secret):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def post(c, path, events, secret):
    body = json.dumps({"destination": "U0", "events": events}).encode()
    return c.post(path, content=body, headers={"Content-Type": "application/json", "X-Line-Signature": sign(body, secret)})


def follow(uid, rt="rt"):
    return {"type": "follow", "replyToken": rt, "source": {"type": "user", "userId": uid}}


def login(uid):
    return TestClient(main.app).post("/auth/line/mock", data={"user_id": uid, "name": "テスト"}, follow_redirects=False)


def test_env_import_only_when_missing():
    ch = db.channel(1)
    assert ch["name"] == "らくらくモザイク①" and ch["kind"] == "notify" and ch["access_token"] == TOK[1]
    assert ch["basic_id"] == config.LINE_OA_BASIC_ID
    db.upsert_channel(1, access_token="tok-db-9999")
    db.ensure_channel_from_env()  # 行があれば env は使わない
    assert db.channel(1)["access_token"] == "tok-db-9999"


def test_reception_follow_assigns_and_replies(env):
    c = TestClient(main.app)
    want = [1, 2, 2]
    for i, no in enumerate(want):
        assert post(c, "/line/webhook/0", [follow(f"Ulcrecv{i}", f"rt{i}")], SEC[0]).status_code == 200
        r = env[-1]
        assert r["url"] == notify.REPLY_URL and r["auth"] == "Bearer " + TOK[0] and r["body"]["replyToken"] == f"rt{i}"
        text = r["body"]["messages"][0]["text"]
        ch = db.channel(no)
        assert f"（{no}番）" in text and ch["name"] in text
        assert "https://line.me/R/ti/p/" + ch["basic_id"] in text
        assert "https://example.test/?openExternalBrowser=1" in text
    assert [db.assign(f"Ulcrecv{i}") for i in range(3)] == want
    assert db.channel_used(1) == BASE[1] + 1 and db.channel_used(2) == BASE[2] + 2
    # 満員
    post(c, "/line/webhook/0", [follow("Ulcrecv3", "rt3")], SEC[0])
    assert env[-1]["body"]["messages"][0]["text"] == notify.RECEPTION_FULL
    # 割り当て済みの人のメッセージには同じ番号の案内
    n = len(env)
    post(c, "/line/webhook/0", [{"type": "message", "replyToken": "rtm", "message": {"type": "text", "text": "?"},
                                  "source": {"type": "user", "userId": "Ulcrecv1"}}], SEC[0])
    assert len(env) == n + 1 and "（2番）" in env[-1]["body"]["messages"][0]["text"]
    # unfollow は何もしない
    post(c, "/line/webhook/0", [{"type": "unfollow", "source": {"type": "user", "userId": "Ulcrecv1"}}], SEC[0])
    assert len(env) == n + 1


def test_login_uses_assignment():
    c = TestClient(main.app)
    post(c, "/line/webhook/0", [follow("Ulclogin1")], SEC[0])
    post(c, "/line/webhook/0", [follow("Ulclogin2")], SEC[0])
    assert db.assign("Ulclogin2") == 2
    assert login("Ulclogin2").headers["location"] == "/home"
    u = db.user_by("line_user_id", "Ulclogin2")
    assert u["line_channel_no"] == 2 and u["line_friend"] == 0
    assert db.channel_used(2) == BASE[2] + 1  # 登録後は users 側だけで数える


def test_login_without_reception_redirects():
    """受付 LINE を通っていない新規ログインは登録せず、受付へ案内する（枠も番号も使わない）。"""
    db.set_setting("signup_remaining", "3")
    r = login("Ulcnorecv")
    assert r.status_code == 303 and r.headers["location"] == "/?e=reception&r=%40recv"
    assert db.user_by("line_user_id", "Ulcnorecv") is None and db.assignment("Ulcnorecv") is None
    assert db.get_setting("signup_remaining") == "3"
    db.set_setting("signup_remaining", "")


def test_login_direct_and_full():
    db.upsert_channel(0, active=0)  # 受付用が無い運用では、直接ログインで空きの番号に入る
    for uid, no in (("Ulcdirect1", 1), ("Ulcdirect2", 2), ("Ulcdirect3", 2)):
        assert login(uid).headers["location"] == "/home"
        assert db.user_by("line_user_id", uid)["line_channel_no"] == no
    r = login("Ulcdirect4")
    assert r.status_code == 303 and r.headers["location"] == "/?e=closed"
    assert db.user_by("line_user_id", "Ulcdirect4") is None
    # 定員を増やせば入れる
    db.upsert_channel(2, capacity=BASE[2] + 3)
    assert login("Ulcdirect4").headers["location"] == "/home"
    me_c = TestClient(main.app)
    me_c.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"uid": db.user_by("line_user_id", "Ulcdirect4")["id"]}))
    me = me_c.get("/api/me").json()
    assert me["line_channel_no"] == 2 and me["line_channel_name"] == "らくらくモザイク②"
    assert me["line_add_url"] == "https://line.me/R/ti/p/@two"


def test_push_uses_channel_token(env):
    db.upsert_channel(1, capacity=BASE[1])  # ①は満員 → ②
    c = TestClient(main.app)
    post(c, "/line/webhook/0", [follow("Ulcpush2")], SEC[0])
    post(c, "/line/webhook/2", [follow("Ulcpush2")], SEC[2])
    login("Ulcpush2")
    u = db.user_by("line_user_id", "Ulcpush2")
    assert u["line_channel_no"] == 2
    assert notify.push_user("payment_failed", u) is True
    assert env[-1]["url"] == notify.PUSH_URL and env[-1]["auth"] == "Bearer " + TOK[2]
    n = len(env)
    db.update_user(u["id"], line_channel_no=9)  # 行が無い番号は送らない
    assert notify.push_user("payment_failed", db.get_user(u["id"])) is False
    assert len(env) == n


def test_webhook_per_channel_secret():
    u = db.create_user(line_user_id="Ulcwh2", display_name="②の人", notify=0, line_friend=0, line_channel_no=2)
    c = TestClient(main.app)
    assert post(c, "/line/webhook/2", [follow("Ulcwh2")], SEC[1]).status_code == 400
    assert db.get_user(u["id"])["line_friend"] == 0
    assert post(c, "/line/webhook/2", [follow("Ulcwh2")], SEC[2]).status_code == 200
    got = db.get_user(u["id"])
    assert got["line_friend"] == 1 and got["notify"] == 1
    assert post(c, "/line/webhook/7", [follow("Ulcwh2")], SEC[2]).status_code == 404
    db.upsert_channel(2, active=0)
    assert post(c, "/line/webhook/2", [follow("Ulcwh2")], SEC[2]).status_code == 404
    # 番号の無い人が通知用を follow → その番号に
    v = db.create_user(line_user_id="Ulcwh9", display_name="番号なし", line_channel_no=None)
    assert post(c, "/line/webhook/1", [follow("Ulcwh9")], SEC[1]).status_code == 200
    assert db.get_user(v["id"])["line_channel_no"] == 1


def admin_client():
    if not db.user_by("device_id", "dev-admin-line"):
        db.create_user(device_id="dev-admin-line", display_name="運営", role="admin")
    a = TestClient(main.app)
    a.cookies.set(config.COOKIE_NAME, main._serializer().dumps({"t": int(time.time())}))
    a.cookies.set(config.OWNER_COOKIE, main._owner_serializer().dumps("dev-admin-line"))
    return a


def test_admin_api(monkeypatch):
    monkeypatch.setattr(config, "AUTH_MODE", "passcode")
    a = admin_client()
    r = a.get("/admin/api/line")
    assert r.status_code == 200
    for t in list(TOK.values()) + list(SEC.values()):
        assert t not in r.text
    rows = {c["no"]: c for c in r.json()["channels"]}
    assert rows[2]["token_tail"] == "2222" and rows[2]["secret_set"] is True
    assert rows[2]["webhook_url"] == "https://example.test/line/webhook/2"
    assert rows[0]["used"] is None and rows[2]["used"] == BASE[2]
    r = a.post("/admin/api/line/2", json={"capacity": 7, "access_token": "", "channel_secret": ""})
    assert r.status_code == 200, r.text
    assert db.channel(2)["capacity"] == 7 and db.channel(2)["access_token"] == TOK[2]
    r = a.post("/admin/api/line/3", json={"name": "らくらくモザイク③", "kind": "notify", "basic_id": "three",
                                          "capacity": 5, "active": 1, "access_token": "tok-three-3333",
                                          "channel_secret": "sec-three"})
    assert r.status_code == 200, r.text
    assert "tok-three-3333" not in r.text and "sec-three" not in r.text
    assert db.channel(3)["basic_id"] == "@three" and db.channel(3)["access_token"] == "tok-three-3333"
    with db.connect() as con:
        logs = " ".join(row[0] or "" for row in con.execute("SELECT detail FROM audit_log WHERE target LIKE 'line:%'"))
    assert "tok-three-3333" not in logs and "sec-three" not in logs
    assert a.post("/admin/api/line/4", json={"kind": "notify"}).status_code == 400  # 名前なし
    # 利用者の番号の変更
    u = db.create_user(line_user_id="Ulcadm1", display_name="変更", line_channel_no=1)
    r = a.post(f"/admin/api/users/{u['id']}", json={"line_channel_no": 3})
    assert r.status_code == 200, r.text
    assert db.get_user(u["id"])["line_channel_no"] == 3
    assert a.post(f"/admin/api/users/{u['id']}", json={"line_channel_no": 0}).status_code == 400  # 受付用は不可


def test_follow_before_login_sets_friend():
    """受付 → ②を follow（まだ未登録）→ ログイン で line_friend=1・notify=1。"""
    db.upsert_channel(1, capacity=BASE[1])  # ①は満員 → ②
    c = TestClient(main.app)
    post(c, "/line/webhook/0", [follow("Ulcfb1")], SEC[0])
    assert db.assignment("Ulcfb1")["channel_no"] == 2
    assert post(c, "/line/webhook/2", [follow("Ulcfb1")], SEC[2]).status_code == 200
    assert db.assignment("Ulcfb1")["friend"] == 1
    assert login("Ulcfb1").headers["location"] == "/home"
    u = db.user_by("line_user_id", "Ulcfb1")
    assert u["line_channel_no"] == 2 and u["line_friend"] == 1 and u["notify"] == 1
    # 登録前に unfollow した人は 0
    post(c, "/line/webhook/0", [follow("Ulcfb2")], SEC[0])
    post(c, "/line/webhook/2", [follow("Ulcfb2")], SEC[2])
    post(c, "/line/webhook/2", [{"type": "unfollow", "source": {"type": "user", "userId": "Ulcfb2"}}], SEC[2])
    login("Ulcfb2")
    v = db.user_by("line_user_id", "Ulcfb2")
    assert v["line_friend"] == 0 and v["notify"] == 0


def test_other_channel_unfollow_ignored():
    u = db.create_user(line_user_id="Ulcother", display_name="②の人", notify=1, line_friend=1, line_channel_no=2)
    c = TestClient(main.app)
    assert post(c, "/line/webhook/1", [{"type": "unfollow", "source": {"type": "user", "userId": "Ulcother"}}],
                SEC[1]).status_code == 200
    got = db.get_user(u["id"])
    assert got["notify"] == 1 and got["line_friend"] == 1 and got["line_channel_no"] == 2


def test_signup_closed_leaves_no_assignment():
    db.upsert_channel(0, active=0)
    db.set_setting("signup_remaining", "0")
    r = login("Ulcclosed")
    assert r.headers["location"] == "/?e=closed"
    assert db.user_by("line_user_id", "Ulcclosed") is None and db.assignment("Ulcclosed") is None
