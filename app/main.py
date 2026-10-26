"""顔モザイク Web版 試験版（写真1枚モード・動画モード・合言葉／LINE ログイン・プランと上限）。"""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import statistics
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

import stripe
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from worker.engine import photo
from worker.engine import video as vengine
from worker.run import paths as vpaths

from . import config, db
from .services import billing, discord, monitor, quota, retention

log = logging.getLogger("uvicorn.error")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,24}$")
WORK_LOCK = threading.Lock()  # 検出・書き出しは1件ずつ


def job_dir(job_id):
    return os.path.join(config.DATA_DIR, "jobs", job_id)


PERIODIC = (retention.cleanup_expired, retention.notify_idle, monitor.check_worker, monitor.check_disk,
            monitor.daily_summary, monitor.check_past_due)


async def retention_loop():
    while True:
        for fn in PERIODIC:
            try:
                await asyncio.to_thread(fn)
            except Exception as e:  # 定期処理は止めない
                log.warning("定期処理 %s 失敗: %s", fn.__name__, e)
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if config.AUTH_MODE_REQUESTED == "line" and config.AUTH_MODE != "line":
        log.warning("AUTH_MODE=line ですが LINE_LOGIN_CHANNEL_ID / LINE_LOGIN_CHANNEL_SECRET が未設定のため、合言葉（passcode）で動きます")
    log.info("認証モード: %s%s", config.AUTH_MODE, "（LINE_MOCK）" if config.AUTH_MODE == "line" and config.LINE_MOCK else "")
    if not config.SESSION_SECRET:
        log.warning("SESSION_SECRET が未設定のため、ログインできません")
    elif config.AUTH_MODE == "passcode" and not config.APP_PASSCODE:
        log.warning("APP_PASSCODE が未設定のため、合言葉で入れません")
    task = asyncio.create_task(retention_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(WEB, "static")), name="static")


# ---------------------------------------------------------------- 認証

def _serializer():
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="fm-session")


def _session(request):
    token = request.cookies.get(config.COOKIE_NAME)
    if not token or not config.SESSION_SECRET:
        return None
    try:
        return _serializer().loads(token, max_age=config.SESSION_DAYS * 86400)
    except BadSignature:
        return None


def _device_user(owner):
    """passcode モード: 端末 Cookie ごとに1人（ゲスト・試験中は動画B相当）。"""
    u = db.user_by("device_id", owner)
    if u:
        return u
    try:
        u = db.create_user(device_id=owner, display_name="ゲスト", plan=db.PASSCODE_PLAN)
    except sqlite3.IntegrityError:
        return db.user_by("device_id", owner)
    discord.usage(f"新規登録（合言葉）: {discord.who(u)}")
    return u


def current_user(request: Request):
    if hasattr(request.state, "user"):
        return request.state.user
    u, s = None, _session(request)
    if isinstance(s, dict):
        if config.AUTH_MODE == "line" and s.get("uid"):
            u = db.get_user(s["uid"])
        elif config.AUTH_MODE == "passcode" and "t" in s and getattr(request.state, "owner", None):
            u = _device_user(request.state.owner)
    if u and u["status"] != "active":
        u = None
    request.state.user = u
    return u


def authed(request: Request) -> bool:
    return current_user(request) is not None


def _owner_serializer():
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="fm-owner")


@app.middleware("http")
async def owner_cookie(request: Request, call_next):
    """初回アクセスで端末ごとの owner_id を発行（署名付き Cookie・1年）。"""
    if request.url.path == "/stripe/webhook":  # Stripe からの通知にはセッションも Cookie も要らない
        return await call_next(request)
    owner, fresh = None, False
    if config.SESSION_SECRET:
        token = request.cookies.get(config.OWNER_COOKIE)
        if token:
            try:
                owner = _owner_serializer().loads(token, max_age=config.OWNER_DAYS * 86400)
            except BadSignature:
                owner = None
        if not owner:
            owner, fresh = secrets.token_urlsafe(16), True
    request.state.owner = owner
    res = await call_next(request)
    if fresh:
        res.set_cookie(config.OWNER_COOKIE, _owner_serializer().dumps(owner),
                       max_age=config.OWNER_DAYS * 86400, httponly=True, samesite="lax",
                       secure=request.url.scheme == "https", path="/")
    return res


def require_api(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "ログインし直してください" if config.AUTH_MODE == "line" else "合言葉を入れ直してください")
    return u


def _set_session(request, res, payload):
    res.set_cookie(config.COOKIE_NAME, _serializer().dumps(payload),
                   max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                   secure=request.url.scheme == "https", path="/")


def page(name):
    return FileResponse(os.path.join(WEB, name), headers={"Cache-Control": "no-store"})


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return PlainTextResponse("ok")


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    discord.alert(f"アプリのエラー {request.method} {request.url.path}: {type(exc).__name__}: {str(exc)[:300]}")
    return JSONResponse({"detail": "サーバーでエラーが起きました。時間をおいてもう一度お試しください"}, status_code=500)


@app.api_route("/", methods=["GET", "HEAD"])
def entrance(request: Request):
    if authed(request):
        return RedirectResponse("/home", status_code=302)
    return page("login.html" if config.AUTH_MODE == "line" else "index.html")


@app.post("/login")
async def login(request: Request, passcode: str = Form("")):
    if config.AUTH_MODE != "passcode":
        raise HTTPException(404, "見つかりません")
    ok = bool(config.APP_PASSCODE and config.SESSION_SECRET) and hmac.compare_digest(
        passcode.strip().encode(), config.APP_PASSCODE.encode())
    if not ok:
        await asyncio.sleep(1.0)  # 総当たりを遅くする
        return RedirectResponse("/?e=1", status_code=303)
    res = RedirectResponse("/home", status_code=303)
    _set_session(request, res, {"t": int(time.time())})
    return res


# ---------------------------------------------------------------- LINE ログイン（Web・OpenID Connect）

LINE_AUTH_URL = "https://access.line.me/oauth2/v2.1/authorize"
LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
STATE_COOKIE = "fm_line_state"
LINE_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _state_serializer():
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="fm-line-state")


def _redirect_uri(request):
    return (config.APP_BASE_URL or str(request.base_url).rstrip("/")) + "/auth/line/callback"


def _require_line(mock=None):
    if config.AUTH_MODE != "line" or (mock is not None and config.LINE_MOCK != mock):
        raise HTTPException(404, "見つかりません")


def _form_post(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _line_exchange(code, nonce, redirect_uri):
    """認可コード → ID トークン → 検証。返すのは userId と表示名だけ（アクセストークンは保存しない）。"""
    tok = _form_post(LINE_TOKEN_URL, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                      "client_id": config.LINE_LOGIN_CHANNEL_ID,
                                      "client_secret": config.LINE_LOGIN_CHANNEL_SECRET})
    v = _form_post(LINE_VERIFY_URL, {"id_token": tok["id_token"], "client_id": config.LINE_LOGIN_CHANNEL_ID,
                                     "nonce": nonce})
    return v["sub"], v.get("name") or "LINEユーザー"


def _login_line_user(request, line_user_id, name):
    u = db.user_by("line_user_id", line_user_id)
    if not u:
        try:
            u = db.create_user(line_user_id=line_user_id, display_name=name,
                               trial_ends_at=time.time() + db.TRIAL_DAYS * 86400)
            discord.usage(f"新規登録: {discord.who(u)}")
        except sqlite3.IntegrityError:
            u = db.user_by("line_user_id", line_user_id)
    elif u["status"] == "withdrawn":  # 退会後の再登録: 初回と同じ流れからやり直す
        db.update_user(u["id"], status="active", display_name=name, plan="free", hq=0, notify=1, ig_asked_at=None,
                       agreed_at=None)
        u = db.get_user(u["id"])
        discord.usage(f"新規登録（退会後の再登録）: {discord.who(u)}")
    elif u["status"] != "active":
        return RedirectResponse("/?e=stopped", status_code=303)
    elif name and name != u["display_name"]:
        db.update_user(u["id"], display_name=name)
    res = RedirectResponse("/home", status_code=303)
    _set_session(request, res, {"uid": u["id"]})
    log.info("login line user=%s", u["id"])
    return res


@app.get("/auth/line/start")
def line_start(request: Request):
    _require_line()
    if config.LINE_MOCK:
        return RedirectResponse("/auth/line/mock", status_code=302)
    state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({"response_type": "code", "client_id": config.LINE_LOGIN_CHANNEL_ID,
                                "redirect_uri": _redirect_uri(request), "state": state,
                                "scope": "profile openid", "nonce": nonce})
    res = RedirectResponse(LINE_AUTH_URL + "?" + q, status_code=302)
    res.set_cookie(STATE_COOKIE, _state_serializer().dumps({"s": state, "n": nonce}), max_age=600, httponly=True,
                   samesite="lax", secure=request.url.scheme == "https", path="/auth/line")
    return res


@app.get("/auth/line/callback")
async def line_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    _require_line(mock=False)
    try:
        st = _state_serializer().loads(request.cookies.get(STATE_COOKIE, ""), max_age=600)
    except BadSignature:
        st = None
    if error or not code or not st or not hmac.compare_digest(str(st.get("s", "")), state):
        return RedirectResponse("/?e=line", status_code=303)
    try:
        sub, name = await asyncio.to_thread(_line_exchange, code, st["n"], _redirect_uri(request))
    except Exception as e:
        detail = ""
        if isinstance(e, urllib.error.HTTPError):  # LINE の応答本文（error/error_description のみ・秘密値は含まない）
            try:
                detail = " " + e.read().decode()[:300]
            except Exception:
                pass
        log.warning("LINE ログイン失敗: %s%s", type(e).__name__, detail)
        return RedirectResponse("/?e=line", status_code=303)
    res = _login_line_user(request, sub, name)
    res.delete_cookie(STATE_COOKIE, path="/auth/line")
    return res


@app.get("/auth/line/mock")
def line_mock_page(request: Request):
    _require_line(mock=True)
    return page("mock_login.html")


@app.post("/auth/line/mock")
def line_mock_login(request: Request, user_id: str = Form(""), name: str = Form("")):
    _require_line(mock=True)
    user_id = user_id.strip()
    if not LINE_UID_RE.match(user_id):
        return RedirectResponse("/auth/line/mock?e=1", status_code=303)
    return _login_line_user(request, user_id, name.strip()[:40] or "テスト利用者")


def _guarded(request, name, onboarding=True):
    u = current_user(request)
    if not u:
        return RedirectResponse("/", status_code=302)
    if config.AUTH_MODE == "line" and onboarding:  # 初回: Instagram 名（任意）→ 同意 → ホーム
        if not u["ig_asked_at"]:
            return RedirectResponse("/welcome", status_code=302)
        if not u["agreed_at"]:
            return RedirectResponse("/agree", status_code=302)
    return page(name)


@app.get("/welcome")
def welcome_page(request: Request):
    if config.AUTH_MODE != "line":
        return RedirectResponse("/settings", status_code=302)
    return _guarded(request, "welcome.html", onboarding=False)


@app.get("/agree")
def agree_page(request: Request):
    if config.AUTH_MODE != "line":
        return RedirectResponse("/home", status_code=302)
    return _guarded(request, "agree.html", onboarding=False)


@app.get("/plans")
def plans_page(request: Request):
    return _guarded(request, "plans.html")


@app.get("/settings")
def settings_page(request: Request):
    return _guarded(request, "settings.html")


@app.get("/withdraw")
def withdraw_page(request: Request):
    return _guarded(request, "withdraw.html")


@app.get("/withdrawn")
def withdrawn_page():
    return page("withdrawn.html")


@app.get("/home")
def home(request: Request):
    return _guarded(request, "home.html")


@app.get("/upload")
def upload_page(request: Request):
    return _guarded(request, "upload.html")


def _is_video(request, job_id):
    if not JOB_ID_RE.match(job_id):
        return False
    j = db.get_job(job_id)
    return bool(j and j["kind"] == "video")


@app.get("/pick/{job_id}")
def pick_page(request: Request, job_id: str):
    return _guarded(request, "vpick.html" if _is_video(request, job_id) else "pick.html")


@app.get("/done/{job_id}")
def done_page(request: Request, job_id: str):
    return _guarded(request, "vdone.html" if _is_video(request, job_id) else "done.html")


@app.get("/review/{job_id}")
def review_page(request: Request, job_id: str):
    return _guarded(request, "vreview.html")


@app.get("/status/{job_id}")
def status_page(request: Request, job_id: str):
    return _guarded(request, "vstatus.html")


# ---------------------------------------------------------------- API

def _load(request, job_id):
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(404, "見つかりません")
    j = db.get_job(job_id)
    u = current_user(request)
    if not j or not u:
        raise HTTPException(404, "見つかりません")
    if j["user_id"] is None:  # 持ち主が記録される前の旧行は passcode モードだけ期限まで従来どおり
        if config.AUTH_MODE != "passcode":
            raise HTTPException(404, "見つかりません")
    elif j["user_id"] != u["id"]:  # 他の人のものは存在しない扱い
        raise HTTPException(404, "見つかりません")
    return j


def _faces(job_id):
    p = os.path.join(job_dir(job_id), "faces.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _eff_status(j, now):
    """期限を過ぎたものは、削除処理（10分おき）を待たずに削除済みとして扱う。"""
    st = j["status"]
    if st == "done":
        return "deleted" if j["out_expires_at"] is not None and j["out_expires_at"] <= now else st
    if st not in ("deleted", "queued_export", "exporting") and j["expires_at"] is not None and j["expires_at"] <= now:
        return "deleted"
    return st


def _keep_fields(j, st, now, source_exists):
    """完成後の2段の期限: 選び直し（元データがある間）と保存（完成品）。"""
    can = st == "done" and j["src_expires_at"] is not None and j["src_expires_at"] > now and source_exists
    return {"can_repick": can, "src_expires_at": j["src_expires_at"] if can else None,
            "out_expires_at": j["out_expires_at"] if st == "done" else None}


def _public(j, with_faces=False):
    if j["kind"] == "video":
        return _public_video(j, detail=with_faces)
    now = time.time()
    st = _eff_status(j, now)
    d = {"id": j["id"], "status": st, "width": j["width"], "height": j["height"],
         "style": j["mosaic_style"] or "pixel", "keep": json.loads(j["keep"] or "[]"),
         "blur": json.loads(j["blur"] or "[]"), "error": j["error"],
         "expires_at": j["expires_at"], "created_at": j["created_at"], "now": now,
         "has_out": st == "done" and bool(j["out_path"] and os.path.exists(j["out_path"]))}
    d.update(_keep_fields(j, st, now, bool(j["src_path"] and os.path.exists(j["src_path"]))))
    if with_faces:
        d["faces"] = _faces(j["id"])
        d["min_conf"] = photo.MIN_CONF
    return d


NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/jobs")
def list_jobs(request: Request):
    u = require_api(request)
    return JSONResponse([_public(j) for j in db.recent_jobs(u["id"], include_legacy=config.AUTH_MODE == "passcode")],
                        headers=NO_STORE)


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    require_api(request)
    return JSONResponse(_public(_load(request, job_id), with_faces=True), headers=NO_STORE)


def _check_blocked(u):
    """お試し期限切れ・お支払い未確認の間は送れない。"""
    b = quota.blocked(u)
    if b:
        raise HTTPException(403, quota.BLOCKED_MSG[b])


@app.post("/api/photos")
def upload_photo(request: Request, file: UploadFile = File(...)):
    u = require_api(request)
    _check_blocked(u)
    msg = quota.check_image(u)
    if msg:
        raise HTTPException(402, msg)
    job_id = secrets.token_urlsafe(9)
    d = job_dir(job_id)
    os.makedirs(d, exist_ok=True)
    raw = os.path.join(d, "upload.bin")
    size = 0
    with open(raw, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                out.close()
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, "写真は30MBまでです")
            out.write(chunk)
    src = os.path.join(d, "src.jpg")
    try:
        with WORK_LOCK:
            r = photo.prepare(raw, src, os.path.join(d, "view.jpg"))
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        log.warning("prepare 失敗: %s", e)
        raise HTTPException(400, "この写真は読み込めませんでした。JPEG・HEIC・PNG の写真を選んでください")
    finally:
        if os.path.exists(raw):
            os.remove(raw)
    with open(os.path.join(d, "faces.json"), "w", encoding="utf-8") as f:
        json.dump(r["faces"], f)
    if r["warn"]:
        log.warning("detect: %s", r["warn"])
    db.insert_job(job_id, kind="image", status="awaiting_pick", src_path=src, bytes=size, user_id=u["id"],
                  width=r["width"], height=r["height"], mosaic_style="pixel",
                  expires_at=time.time() + config.SOURCE_RETENTION_HOURS * 3600)
    quota.count_image(u["id"])
    log.info("photo %s %dx%d 候補 %d 件 %.2fs", job_id, r["width"], r["height"],
             len(r["faces"]), r["sec"])
    return JSONResponse(_public(db.get_job(job_id), with_faces=True), headers=NO_STORE)


@app.get("/api/jobs/{job_id}/view.jpg")
def view_image(request: Request, job_id: str):
    require_api(request)
    _load(request, job_id)
    p = os.path.join(job_dir(job_id), "view.jpg")
    if not os.path.exists(p):
        raise HTTPException(410, "削除済みです")
    return FileResponse(p, media_type="image/jpeg", headers=NO_STORE)


@app.post("/api/jobs/{job_id}/export")
async def export_job(request: Request, job_id: str):
    require_api(request)
    j = _load(request, job_id)
    if j["kind"] != "image":
        raise HTTPException(404, "見つかりません")
    if _eff_status(j, time.time()) not in ("awaiting_pick", "done") or not j["src_path"] \
            or not os.path.exists(j["src_path"]):
        raise HTTPException(410, "元の写真が削除されています。もう一度送ってください")
    try:
        body = await request.json()
        keep_ids = {int(x) for x in body.get("keep", [])}
        show_low = bool(body.get("show_low", False))
        style = "blur" if body.get("style") == "blur" else "pixel"
    except Exception:
        raise HTTPException(400, "送信内容が正しくありません")
    out = os.path.join(job_dir(job_id), "out.jpg")

    def run():
        with WORK_LOCK:
            return photo.export(j["src_path"], out, _faces(job_id), keep_ids, show_low, style)

    db.update_job(job_id, status="exporting")
    try:
        r = await asyncio.to_thread(run)
    except Exception as e:
        log.warning("export 失敗 %s: %s", job_id, e)
        db.update_job(job_id, status="awaiting_pick", error="書き出しに失敗しました")
        raise HTTPException(500, "書き出しに失敗しました。もう一度お試しください")
    now = time.time()
    db.update_job(job_id, status="done", out_path=out, mosaic_style=style,
                  keep=json.dumps(r["keep"]), blur=json.dumps(r["blur"]),
                  t_export_sec=r["sec"], error=None, expires_at=None,
                  src_expires_at=j["src_expires_at"] or now + config.SOURCE_RETENTION_HOURS * 3600,
                  out_expires_at=now + config.OUTPUT_RETENTION_DAYS * 86400)
    log.info("export %s %s モザイク %d・かけない %d %.2fs", job_id, style, len(r["blur"]),
             len(r["keep"]), r["sec"])
    return JSONResponse(_public(db.get_job(job_id)), headers=NO_STORE)


@app.get("/api/jobs/{job_id}/result.jpg")
def result_image(request: Request, job_id: str, dl: int = 0):
    require_api(request)
    j = _load(request, job_id)
    if not j["out_path"] or not os.path.exists(j["out_path"]) or _eff_status(j, time.time()) == "deleted":
        raise HTTPException(410, "削除済みです")
    name = time.strftime("mosaic_%Y%m%d_%H%M.jpg", time.localtime(j["updated_at"]))
    headers = dict(NO_STORE)
    if dl:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(j["out_path"], media_type="image/jpeg", headers=headers)


# ---------------------------------------------------------------- 動画モード

# 1分あたりの目安秒（引き継ぎ 4節・いまのVPS・1080p高速モード）。実測が貯まったら中央値に置き換わる
FALLBACK_PER_MIN = {"prepare": 70, "track": 144, "feature": 98, "export_pixel": 159, "export_blur": 291}
# 高画質（元の解像度・fps）の書き出しは、実測が無い間は 1080p の何倍で見積もるか。
# VPS 実測（4K60 HLG 1分・ブロック粗め）: 高画質 1191.7秒 ÷ 1080p 75.3秒 ≒ 15.8
HQ_FALLBACK_FACTOR = 16
PREP_STAGES = (("t_prepare_sec", "prepare", "convert"), ("t_track_sec", "track", "track"),
               ("t_feature_sec", "feature", "feature"))
VIDEO_BUSY = ("queued", "preparing", "queued_export", "exporting")
COMPLETING = set()  # complete の二重実行よけ


def _rates():
    rows = db.done_video_stats()

    def med(col, ok=lambda r: True):
        v = [r[col] / r["duration_sec"] * 60 for r in rows if r[col] and ok(r)]
        return statistics.median(v) if v else None
    def exp(style, hq):
        return med("t_export_sec", lambda r: bool(r["hq_export"]) == hq and (r["mosaic_style"] == "blur") == (style == "blur"))
    r = {"prepare": med("t_prepare_sec") or FALLBACK_PER_MIN["prepare"],
         "track": med("t_track_sec") or FALLBACK_PER_MIN["track"],
         "feature": med("t_feature_sec") or FALLBACK_PER_MIN["feature"],
         "export_pixel": exp("pixel", False) or FALLBACK_PER_MIN["export_pixel"],
         "export_blur": exp("blur", False) or FALLBACK_PER_MIN["export_blur"]}
    r["export_hq_pixel"] = exp("pixel", True) or r["export_pixel"] * HQ_FALLBACK_FACTOR
    r["export_hq_blur"] = exp("blur", True) or r["export_blur"] * HQ_FALLBACK_FACTOR
    return r


def _export_est(j, rates, style=None, hq=None):
    """書き出しの目安秒（1分あたりの実測中央値 × 長さ）。"""
    style = style or ("blur" if j["mosaic_style"] == "blur" else "pixel")
    hq = bool(j.get("hq_export")) if hq is None else hq
    return rates[("export_hq_" if hq else "export_") + style] * (j["duration_sec"] or 60) / 60.0


def _hq_ready(j):
    """高画質で書き出せるか: 持ち主が高画質オプション（動画A/B）で、元動画が残っている。"""
    u = db.get_user(j["user_id"]) if j.get("user_id") else None
    return bool(u and quota.limits(u)["hq"] and os.path.exists(vpaths(j["id"])["src"]))


def _remaining(j, rates, now):
    """このジョブの残り秒の目安と、いまの工程。"""
    m = (j["duration_sec"] or 60) / 60.0
    exp = _export_est(j, rates)
    st = j["status"]
    if st == "queued":
        return (rates["prepare"] + rates["track"] + rates["feature"]) * m, "waiting"
    if st == "preparing":
        rem, stage = 0.0, None
        for col, key, name in PREP_STAGES:
            if j[col] is not None:
                continue
            est = rates[key] * m
            if stage is None:
                est, stage = max(5.0, est - (now - j["updated_at"])), name
            rem += est
        return (rem, stage) if stage else (10.0, "shots")
    if st == "queued_export":
        return exp, "waiting_export"
    if st == "exporting":
        return max(5.0, exp - (now - j["updated_at"])), "export"
    return 0.0, st


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _public_video(j, detail=False):
    now = time.time()
    st = _eff_status(j, now)
    p = vpaths(j["id"])
    d = {"id": j["id"], "kind": "video", "status": st, "duration_sec": j["duration_sec"],
         "width": j["width"], "height": j["height"], "style": j["mosaic_style"] or "pixel",
         "keep": json.loads(j["keep"] or "[]"), "blur": json.loads(j["blur"] or "[]"), "error": j["error"],
         "expires_at": j["expires_at"], "created_at": j["created_at"], "now": now,
         "has_out": st != "deleted" and bool(j["out_path"] and os.path.exists(j["out_path"])),
         "has_thumb": st != "deleted" and os.path.exists(os.path.join(p["shots"], "shot0.jpg")),
         "idle_warn": st in ("awaiting_pick", "awaiting_review") and j["expires_at"] is not None
         and j["expires_at"] - now <= (config.IDLE_DELETE_HOURS - config.IDLE_WARN_HOURS) * 3600,
         "hq_export": bool(j.get("hq_export")), "out_width": j.get("out_width"), "out_height": j.get("out_height"),
         "out_fps": j.get("out_fps")}
    d.update(_keep_fields(j, st, now, os.path.exists(p["v1080"])))
    if st in VIDEO_BUSY:
        rates = _rates()
        ahead = db.queue_before(j) if st in db.QUEUED else []
        eta, stage = _remaining(j, rates, now)
        eta += sum(_remaining(a, rates, now)[0] for a in ahead)
        d.update(ahead=len(ahead), eta_sec=round(eta), stage=stage if not ahead else
                 ("waiting" if st == "queued" else "waiting_export"))
    if detail and st != "deleted":
        if st in ("awaiting_pick", "awaiting_review"):
            d["shots"] = _read_json(p["shots_json"], [])
            d["picks"] = json.loads(j["picks"] or "[]")
        if st in ("awaiting_review", "queued_export", "exporting", "done"):
            rv = _read_json(os.path.join(p["dir"], "review.json"))
            if rv:
                d["review"] = rv["lists"]
        if st in ("awaiting_review", "done") and _hq_ready(j):  # 確認画面の「高画質で書き出す（約 n 分）」
            rates = _rates()
            d["hq_est_sec"] = {s: round(_export_est(j, rates, s, True)) for s in ("pixel", "blur")}
    return d


def _video(request, job_id, statuses=None):
    j = _load(request, job_id)
    if j["kind"] != "video":
        raise HTTPException(404, "見つかりません")
    if j["status"] != "deleted" and _eff_status(j, time.time()) == "deleted":
        raise HTTPException(410, "時間がたったので動画を削除しました。もう一度送ってください")
    if statuses and j["status"] not in statuses:
        raise HTTPException(409, "この動画はいまこの操作ができません。画面を読み込み直してください")
    return j


async def _json_body(request):
    try:
        b = await request.json()
        if not isinstance(b, dict):
            raise ValueError
        return b
    except Exception:
        raise HTTPException(400, "送信内容が正しくありません")


@app.get("/api/config")
def api_config(request: Request):
    u = require_api(request)
    lim = quota.limits(u)
    return JSONResponse({"max_video_sec": lim["video_len"], "max_video_bytes": config.MAX_VIDEO_BYTES,
                         "part_size": config.PART_SIZE, "plan_label": lim["label"],
                         "video_block": quota.check_video(u, 0), "image_block": quota.check_image(u)},
                        headers=NO_STORE)


def _upload_info(j):
    size = j["bytes"] or 0
    n = max(1, -(-size // config.PART_SIZE))
    return {"id": j["id"], "status": j["status"], "bytes": size, "part_size": config.PART_SIZE, "parts": n,
            "received": sorted(db.parts(j["id"])) if j["status"] == "uploading" else []}


@app.post("/api/uploads")
async def create_upload(request: Request):
    u = require_api(request)
    _check_blocked(u)
    b = await _json_body(request)
    try:
        size, dur = int(b.get("bytes") or 0), float(b.get("duration") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "送信内容が正しくありません")
    if size <= 0:
        raise HTTPException(400, "動画を選び直してください")
    if size > config.MAX_VIDEO_BYTES:
        raise HTTPException(413, f"この動画は {size / 1048576:.0f}MB です。試験版は1GBまでです")
    msg = quota.check_video(u, dur)
    if msg:
        raise HTTPException(402, msg)
    job_id = secrets.token_urlsafe(9)
    os.makedirs(os.path.join(job_dir(job_id), "parts"), exist_ok=True)
    db.insert_job(job_id, kind="video", status="uploading", bytes=size, duration_sec=dur or None,
                  user_id=u["id"], mosaic_style="pixel",
                  expires_at=time.time() + config.UPLOAD_KEEP_HOURS * 3600)
    log.info("upload 開始 %s %.1fMB %.1fs", job_id, size / 1048576, dur)
    return JSONResponse(_upload_info(db.get_job(job_id)), headers=NO_STORE)


@app.get("/api/uploads/{job_id}")
def get_upload(request: Request, job_id: str):
    require_api(request)
    return JSONResponse(_upload_info(_video(request, job_id)), headers=NO_STORE)


@app.put("/api/uploads/{job_id}/parts/{n}")
async def put_part(request: Request, job_id: str, n: int):
    require_api(request)
    j = _video(request, job_id, ("uploading",))
    info = _upload_info(j)
    if not 0 <= n < info["parts"]:
        raise HTTPException(400, "送信内容が正しくありません")
    expect = config.PART_SIZE if n < info["parts"] - 1 else info["bytes"] - config.PART_SIZE * (info["parts"] - 1)
    d = os.path.join(job_dir(job_id), "parts")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f"{n:05d}.tmp{secrets.token_hex(4)}")
    got = 0
    try:
        with open(tmp, "wb") as out:
            async for chunk in request.stream():
                got += len(chunk)
                if got > expect:
                    break
                out.write(chunk)
        if got != expect:
            raise HTTPException(400, "途中で切れました。続きから送り直します")
        os.replace(tmp, os.path.join(d, f"{n:05d}.part"))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    db.add_part(job_id, n, got)
    return JSONResponse({"ok": True, "n": n}, headers=NO_STORE)


@app.post("/api/uploads/{job_id}/complete")
async def complete_upload(request: Request, job_id: str):
    u = require_api(request)
    j = _video(request, job_id, ("uploading",))
    info = _upload_info(j)
    missing = [n for n in range(info["parts"]) if n not in set(info["received"])]
    if missing:
        raise HTTPException(409, f"まだ届いていない部分があります（{len(missing)} か所）。続きから送ります")
    if job_id in COMPLETING:
        raise HTTPException(409, "受け取り中です。少しお待ちください")
    COMPLETING.add(job_id)
    d = job_dir(job_id)
    src = os.path.join(d, "src.mp4")

    def join():
        with open(src + ".part", "wb") as out:
            for n in range(info["parts"]):
                with open(os.path.join(d, "parts", f"{n:05d}.part"), "rb") as f:
                    shutil.copyfileobj(f, out, 4 * 1024 * 1024)
        os.replace(src + ".part", src)
        shutil.rmtree(os.path.join(d, "parts"), ignore_errors=True)
        return vengine.probe(src)

    def reject(msg, e=None, code=400):
        log.warning("upload 不可 %s: %s %s", job_id, msg, e or "")
        shutil.rmtree(d, ignore_errors=True)
        db.drop_parts(job_id)
        db.update_job(job_id, status="failed", error=msg, src_path=None, expires_at=None)
        raise HTTPException(code, msg)

    try:
        try:
            v = await asyncio.to_thread(join)
        except Exception as e:
            reject("動画として読み込めませんでした。iPhone のカメラで撮った動画を選んでください", e)
        if os.path.getsize(src) != info["bytes"]:
            reject("受け取った大きさが合いませんでした。もう一度送ってください")
        msg = quota.check_video(u, v["duration"])
        if msg:
            reject(msg, code=402)
        db.drop_parts(job_id)
        db.update_job(job_id, status="queued", src_path=src, duration_sec=round(v["duration"], 2),
                      width=v["out_width"], height=v["out_height"], fps=v["fps"], expires_at=None,
                      queued_at=time.time())
        quota.count_video(db.get_job(job_id), v["duration"])
    finally:
        COMPLETING.discard(job_id)
    log.info("upload 完了 %s %dx%d %.1fs %.2ffps", job_id, v["out_width"], v["out_height"], v["duration"], v["fps"])
    return JSONResponse(_public(db.get_job(job_id)), headers=NO_STORE)


@app.post("/api/jobs/{job_id}/picks")
async def video_picks(request: Request, job_id: str):
    require_api(request)
    j = _video(request, job_id, ("awaiting_pick", "awaiting_review"))
    b = await _json_body(request)
    try:
        picks = sorted({int(x) for x in b.get("picks", [])})
    except (TypeError, ValueError):
        raise HTTPException(400, "送信内容が正しくありません")
    p = vpaths(job_id)

    def run():
        tracks, dw, dh, _seen = vengine.load_tracks(p["tracks"])
        face_m, cloth_m = vengine.load_feats(p["feats"])
        keep, blur = vengine.decide(tracks, picks, face_m, cloth_m, min(dw, dh))
        lists = vengine.review_lists(vengine.probe(p["v1080"]), tracks, keep, dw)
        return keep, blur, lists

    try:
        keep, blur, lists = await asyncio.to_thread(run)
    except Exception as e:
        log.warning("picks 失敗 %s: %s", job_id, e)
        raise HTTPException(500, "判定に失敗しました。もう一度お試しください")
    rv = {"picked": picks, "auto_keep": sorted(keep), "auto_blur": sorted(blur), "lists": lists}
    with open(os.path.join(p["dir"], "review.json"), "w", encoding="utf-8") as f:
        json.dump(rv, f)
    db.update_job(job_id, status="awaiting_review", picks=json.dumps(picks), keep=json.dumps(sorted(keep)),
                  blur=json.dumps(sorted(blur)), expires_at=time.time() + config.IDLE_DELETE_HOURS * 3600,
                  idle_notified=0)
    log.info("picks %s 選択 %d → keep %d blur %d（確認に出す モザイク %d・かけない %d）", job_id, len(picks),
             len(keep), len(blur), len(lists["blur"]), len(lists["keep"]))
    return JSONResponse(_public(db.get_job(job_id), with_faces=True), headers=NO_STORE)


@app.post("/api/jobs/{job_id}/review")
async def video_review(request: Request, job_id: str):
    require_api(request)
    j = _video(request, job_id, ("awaiting_review", "done"))
    b = await _json_body(request)
    try:
        keep_ids = {int(x) for x in b.get("keep", [])}
        blur_ids = {int(x) for x in b.get("blur", [])}
        style = "blur" if b.get("style") == "blur" else "pixel"
    except (TypeError, ValueError):
        raise HTTPException(400, "送信内容が正しくありません")
    p = vpaths(job_id)
    rv = _read_json(os.path.join(p["dir"], "review.json"))
    if not rv or not os.path.exists(p["v1080"]):
        raise HTTPException(410, "解析結果が削除されています。もう一度送ってください")
    shown = {it["id"] for it in rv["lists"]["blur"] + rv["lists"]["keep"]}
    keep_ids &= shown
    blur_ids = (blur_ids & shown) - keep_ids
    keep = (set(rv["auto_keep"]) - blur_ids) | keep_ids
    blur = (set(rv["auto_blur"]) - keep_ids) | blur_ids
    hq = _hq_ready(j)
    db.update_job(job_id, status="queued_export", keep=json.dumps(sorted(keep)), blur=json.dumps(sorted(blur)),
                  mosaic_style=style, error=None, expires_at=None, hq_export=1 if hq else 0)
    log.info("review %s %s keep %d blur %d（%s）%s", job_id, style, len(keep), len(blur),
             "選び直し" if j["status"] == "done" else "初回", "高画質" if hq else "1080p")
    return JSONResponse(_public(db.get_job(job_id)), headers=NO_STORE)


def _video_file(request, job_id, path, media):
    _video(request, job_id)
    if not os.path.exists(path):
        raise HTTPException(410, "削除済みです")
    return FileResponse(path, media_type=media, headers=NO_STORE)


@app.get("/api/jobs/{job_id}/shots/{n}.jpg")
def video_shot(request: Request, job_id: str, n: int):
    require_api(request)
    return _video_file(request, job_id, os.path.join(vpaths(job_id)["shots"], f"shot{n}.jpg"), "image/jpeg")


@app.get("/api/jobs/{job_id}/thumbs/{tid}.jpg")
def video_thumb(request: Request, job_id: str, tid: int):
    require_api(request)
    return _video_file(request, job_id, os.path.join(vpaths(job_id)["thumbs"], f"t{tid}.jpg"), "image/jpeg")


@app.get("/api/jobs/{job_id}/result.mp4")
def video_result(request: Request, job_id: str, dl: int = 0):
    require_api(request)
    j = _video(request, job_id)
    if not j["out_path"] or not os.path.exists(j["out_path"]):
        raise HTTPException(410, "削除済みです")
    headers = dict(NO_STORE)
    if dl:
        name = time.strftime("mosaic_%Y%m%d_%H%M.mp4", time.localtime(j["updated_at"]))
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(j["out_path"], media_type="video/mp4", headers=headers)


# ---------------------------------------------------------------- 利用者（設定・プラン・退会）

IG_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
WITHDRAW_REASONS = {"unused": "使う場面がなくなりました", "price": "料金が使い方に合いませんでした",
                    "quality": "仕上がりの画質に満足できませんでした", "slow": "処理に時間がかかりすぎました",
                    "other_service": "ほかのサービスを使うことにしました", "other": "その他",
                    "sub_ended": "更新日に自動退会", "past_due_expired": "お支払い未確認のまま猶予終了"}
USER_WITHDRAW_REASONS = ("unused", "price", "quality", "slow", "other_service", "other")  # 退会アンケートの選択肢


def _me(u):
    st = quota.status(u)
    lim = st["limits"]
    now = time.time()
    trial = u.get("trial_ends_at") if lim["plan"] == "free" and u.get("line_user_id") else None
    blocked = quota.blocked(u, now)
    return {"id": u["id"], "display_name": u["display_name"], "instagram_username": u["instagram_username"],
            "notify": bool(u["notify"]), "auth_mode": config.AUTH_MODE, "plan": lim["plan"], "plan_label": lim["label"],
            "hq": lim["hq"], "hq_available": lim["plan"] in quota.HQ_OPTION["plans"],
            "campaign": lim["campaign"], "video_allowed": lim["video_allowed"], "video_len": lim["video_len"],
            "limits": {"images": lim["images"], "video_sec_month": lim["video_sec_month"],
                       "video_count_month": lim["video_count_month"]},
            "usage": {"images": st["usage"]["image_count"], "videos": st["usage"]["video_count"],
                      "video_sec": st["usage"]["video_seconds_used"]},
            "remaining": st["remaining"], "source_hours": config.SOURCE_RETENTION_HOURS,
            "output_days": config.OUTPUT_RETENTION_DAYS,
            "sub_status": u.get("sub_status"), "trial_ends_at": trial,
            "trial_days_left": max(0, -(-int(trial - now) // 86400)) if trial else None,
            "trial_expired": blocked == "trial_expired", "current_period_end": u.get("current_period_end"),
            "cancel_at_period_end": bool(u.get("cancel_at_period_end")), "pending_plan": u.get("pending_plan"),
            "pending_hq": u.get("pending_hq"), "stripe_ready": billing.ready(), "blocked": blocked}


@app.get("/api/me")
def api_me(request: Request):
    return JSONResponse(_me(require_api(request)), headers=NO_STORE)


@app.get("/api/plans")
def api_plans(request: Request):
    u = require_api(request)
    plans = [dict(quota.PLANS[k], key=k) for k in quota.PLAN_ORDER]
    hq = dict(quota.HQ_OPTION, plans=[{"key": k, "label": quota.PLANS[k]["label"],
                                       "price": quota.PLANS[k]["price"] + quota.HQ_OPTION["price"]}
                                      for k in quota.HQ_OPTION["plans"]])
    return JSONResponse({"me": _me(u), "plans": plans, "hq_option": hq}, headers=NO_STORE)


@app.post("/api/me/instagram")
async def api_instagram(request: Request):
    """Instagram のユーザー名（任意）。skip=true は初回の「あとで」。空文字は登録を消す。"""
    u = require_api(request)
    b = await _json_body(request)
    fields = {"ig_asked_at": u["ig_asked_at"] or time.time()}
    if not b.get("skip"):
        name = str(b.get("username") or "").strip().lstrip("@")
        if name and not IG_RE.match(name):
            raise HTTPException(400, "ユーザー名は半角の英数字・ピリオド・アンダーバー（30文字まで）で入れてください")
        fields["instagram_username"] = name or None
    db.update_user(u["id"], **fields)
    new = fields.get("instagram_username")
    if new and new != u["instagram_username"]:
        discord.usage(f"Instagram名の登録: {discord.who(db.get_user(u['id']))}")
    return JSONResponse(_me(db.get_user(u["id"])), headers=NO_STORE)


@app.post("/api/me/agree")
def api_agree(request: Request):
    u = require_api(request)
    if not u["agreed_at"]:
        db.update_user(u["id"], agreed_at=time.time())
    return JSONResponse({"ok": True}, headers=NO_STORE)


@app.post("/api/me/notify")
async def api_notify(request: Request):
    u = require_api(request)
    b = await _json_body(request)
    db.update_user(u["id"], notify=1 if b.get("on") else 0)
    return JSONResponse(_me(db.get_user(u["id"])), headers=NO_STORE)


def withdraw_user(u, reason, note=None, cancel=True):
    """退会処理（本人の退会・購読終了の自動退会で共用）。cancel=False は Stripe 側で終わっているとき。"""
    if cancel:
        billing.cancel_subscription(u)
    for j in db.user_jobs(u["id"]):
        shutil.rmtree(job_dir(j["id"]), ignore_errors=True)
    n = db.delete_jobs_of(u["id"])
    db.update_user(u["id"], status="withdrawn", instagram_username=None, notify=0, withdraw_reason=reason,
                   withdraw_note=note or None, withdrawn_at=time.time())
    discord.usage(f"退会: #{u['id']} {u['display_name'] or 'ゲスト'}（{config.AUTH_MODE}）\n理由: {WITHDRAW_REASONS[reason]}"
                  + (f"\nひとこと: {note}" if note else ""))
    log.info("withdraw user=%s reason=%s 削除 %d 件", u["id"], reason, n)


@app.post("/api/me/withdraw")
async def api_withdraw(request: Request):
    """退会: 購読の解約 → 完成品・履歴・Instagram 名を即削除 → users は withdrawn（理由だけ残す）。"""
    u = require_api(request)
    b = await _json_body(request)
    reason = b.get("reason")
    if reason not in USER_WITHDRAW_REASONS:
        raise HTTPException(400, "退会の理由を1つ選んでください")
    withdraw_user(u, reason, str(b.get("note") or "").strip()[:500])
    res = JSONResponse({"ok": True}, headers=NO_STORE)
    res.delete_cookie(config.COOKIE_NAME, path="/")
    res.delete_cookie(config.OWNER_COOKIE, path="/")
    return res


# ---------------------------------------------------------------- お支払い（Stripe）

def _base_url(request):
    return config.APP_BASE_URL or str(request.base_url).rstrip("/")


def _require_billing():
    if not billing.ready():
        raise HTTPException(503, "お支払いの受付は準備中です")


def _plan_body(b):
    plan, hq = b.get("plan"), bool(b.get("hq"))
    if plan not in quota.PAID:
        raise HTTPException(400, "有料プランを選んでください")
    if hq and plan not in quota.HQ_OPTION["plans"]:
        raise HTTPException(400, "高画質オプションは動画A・動画B に付けられます")
    return plan, hq


@app.post("/api/billing/checkout")
async def api_checkout(request: Request):
    u = require_api(request)
    plan, hq = _plan_body(await _json_body(request))
    _require_billing()
    if u.get("sub_status") in ("active", "canceling", "past_due"):
        raise HTTPException(400, "すでに契約中です。プランの変更はこの画面の各プランから選んでください")
    url = await asyncio.to_thread(billing.checkout_url, u, plan, hq, _base_url(request))
    return JSONResponse({"url": url}, headers=NO_STORE)


def _require_sub(u, msgs=None):
    if u.get("sub_status") != "active" or not u.get("stripe_subscription_id"):
        msg = (msgs or {"canceling": "解約処理中はプランを変えられません",
                        "past_due": "お支払いの確認ができていません。支払い方法を更新してください"}).get(u.get("sub_status"))
        raise HTTPException(400, msg or "契約がありません。プランを選んで申し込んでください")


@app.post("/api/billing/cancel")
async def api_cancel(request: Request):
    u = require_api(request)
    _require_billing()
    _require_sub(u, {"canceling": "すでに解約処理中です", "past_due": "お支払いの確認ができていません"})
    r = await asyncio.to_thread(billing.cancel_at_period_end, u)
    return JSONResponse({"ok": True, "until": (r or {}).get("until")}, headers=NO_STORE)


@app.post("/api/billing/preview")
async def api_preview(request: Request):
    u = require_api(request)
    plan, hq = _plan_body(await _json_body(request))
    _require_billing()
    _require_sub(u)
    pv = await asyncio.to_thread(billing.preview, u, plan, hq)
    return JSONResponse(pv, headers=NO_STORE)


@app.post("/api/billing/change")
async def api_change(request: Request):
    u = require_api(request)
    plan, hq = _plan_body(await _json_body(request))
    _require_billing()
    _require_sub(u)
    try:
        done = await asyncio.to_thread(billing.change, u, plan, hq)
    except billing.PaymentFailed as e:
        raise HTTPException(402, str(e))
    return JSONResponse({"me": _me(db.get_user(u["id"])), "done": done,
                         "charged": (done or {}).get("charged", 0), "monthly": (done or {}).get("monthly")},
                        headers=NO_STORE)


@app.post("/api/billing/portal")
async def api_portal(request: Request):
    u = require_api(request)
    _require_billing()
    if not u.get("stripe_customer_id"):
        raise HTTPException(400, "お支払いの記録がありません")
    url = await asyncio.to_thread(billing.portal_url, u, _base_url(request))
    return JSONResponse({"url": url}, headers=NO_STORE)


@app.get("/billing/return")
def billing_return():
    return RedirectResponse("/home?paid=1", status_code=302)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe の通知（署名検証 → users に反映）。セッション不要。"""
    if not config.STRIPE_WEBHOOK_SECRET:
        log.info("billing(skip) webhook reason=鍵が未設定")
        return JSONResponse({"detail": "not configured"}, status_code=503)
    payload = await request.body()
    try:
        stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature"), config.STRIPE_WEBHOOK_SECRET)
        event = json.loads(payload)
    except (ValueError, stripe.SignatureVerificationError):
        log.warning("stripe webhook 署名NG")
        return JSONResponse({"detail": "invalid signature"}, status_code=400)
    typ = await asyncio.to_thread(billing.apply_event, event, withdraw_user)
    log.info("stripe webhook %s %s%s", event.get("id"), event.get("type"), "" if typ else "（重複・処理済み）")
    return JSONResponse({"ok": True})
