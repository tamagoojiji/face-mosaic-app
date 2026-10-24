"""顔モザイク Web版 試験版①（写真1枚モード・合言葉認証）。"""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from worker.engine import photo

from . import config, db

log = logging.getLogger("uvicorn.error")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,24}$")
WORK_LOCK = threading.Lock()  # 検出・書き出しは1件ずつ


def job_dir(job_id):
    return os.path.join(config.DATA_DIR, "jobs", job_id)


def cleanup_expired():
    n = 0
    for j in db.expired_jobs(time.time()):
        shutil.rmtree(job_dir(j["id"]), ignore_errors=True)
        db.update_job(j["id"], status="deleted", src_path=None, out_path=None)
        n += 1
    if n:
        log.info("retention: %d 件を削除", n)


async def retention_loop():
    while True:
        try:
            await asyncio.to_thread(cleanup_expired)
        except Exception as e:  # 定期処理は止めない
            log.warning("retention 失敗: %s", e)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if not config.APP_PASSCODE or not config.SESSION_SECRET:
        log.warning("APP_PASSCODE または SESSION_SECRET が未設定のため、合言葉で入れません")
    task = asyncio.create_task(retention_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(WEB, "static")), name="static")


# ---------------------------------------------------------------- 認証

def _serializer():
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="fm-session")


def authed(request: Request) -> bool:
    token = request.cookies.get(config.COOKIE_NAME)
    if not token or not config.SESSION_SECRET:
        return False
    try:
        _serializer().loads(token, max_age=config.SESSION_DAYS * 86400)
        return True
    except BadSignature:
        return False


def require_api(request: Request):
    if not authed(request):
        raise HTTPException(401, "合言葉を入れ直してください")


def page(name):
    return FileResponse(os.path.join(WEB, name), headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return PlainTextResponse("ok")


@app.get("/")
def entrance(request: Request):
    if authed(request):
        return RedirectResponse("/home", status_code=302)
    return page("index.html")


@app.post("/login")
async def login(request: Request, passcode: str = Form("")):
    ok = bool(config.APP_PASSCODE and config.SESSION_SECRET) and hmac.compare_digest(
        passcode.strip().encode(), config.APP_PASSCODE.encode())
    if not ok:
        await asyncio.sleep(1.0)  # 総当たりを遅くする
        return RedirectResponse("/?e=1", status_code=303)
    res = RedirectResponse("/home", status_code=303)
    res.set_cookie(config.COOKIE_NAME, _serializer().dumps({"t": int(time.time())}),
                   max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                   secure=request.url.scheme == "https", path="/")
    return res


def _guarded(request, name):
    if not authed(request):
        return RedirectResponse("/", status_code=302)
    return page(name)


@app.get("/home")
def home(request: Request):
    return _guarded(request, "home.html")


@app.get("/upload")
def upload_page(request: Request):
    return _guarded(request, "upload.html")


@app.get("/pick/{job_id}")
def pick_page(request: Request, job_id: str):
    return _guarded(request, "pick.html")


@app.get("/done/{job_id}")
def done_page(request: Request, job_id: str):
    return _guarded(request, "done.html")


# ---------------------------------------------------------------- API

def _load(job_id):
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(404, "見つかりません")
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "見つかりません")
    return j


def _faces(job_id):
    p = os.path.join(job_dir(job_id), "faces.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _public(j, with_faces=False):
    d = {"id": j["id"], "status": j["status"], "width": j["width"], "height": j["height"],
         "style": j["mosaic_style"] or "pixel", "keep": json.loads(j["keep"] or "[]"),
         "blur": json.loads(j["blur"] or "[]"), "error": j["error"],
         "expires_at": j["expires_at"], "created_at": j["created_at"], "now": time.time(),
         "has_out": bool(j["out_path"] and os.path.exists(j["out_path"]))}
    if with_faces:
        d["faces"] = _faces(j["id"])
        d["min_conf"] = photo.MIN_CONF
    return d


NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/jobs")
def list_jobs(request: Request):
    require_api(request)
    return JSONResponse([_public(j) for j in db.recent_jobs()], headers=NO_STORE)


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    require_api(request)
    return JSONResponse(_public(_load(job_id), with_faces=True), headers=NO_STORE)


@app.post("/api/photos")
def upload_photo(request: Request, file: UploadFile = File(...)):
    require_api(request)
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
    db.insert_job(job_id, kind="image", status="awaiting_pick", src_path=src, bytes=size,
                  width=r["width"], height=r["height"], mosaic_style="pixel",
                  expires_at=time.time() + config.RETENTION_HOURS * 3600)
    log.info("photo %s %dx%d 候補 %d 件 %.2fs", job_id, r["width"], r["height"],
             len(r["faces"]), r["sec"])
    return JSONResponse(_public(db.get_job(job_id), with_faces=True), headers=NO_STORE)


@app.get("/api/jobs/{job_id}/view.jpg")
def view_image(request: Request, job_id: str):
    require_api(request)
    _load(job_id)
    p = os.path.join(job_dir(job_id), "view.jpg")
    if not os.path.exists(p):
        raise HTTPException(410, "削除済みです")
    return FileResponse(p, media_type="image/jpeg", headers=NO_STORE)


@app.post("/api/jobs/{job_id}/export")
async def export_job(request: Request, job_id: str):
    require_api(request)
    j = _load(job_id)
    if j["status"] not in ("awaiting_pick", "done") or not j["src_path"] \
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
    db.update_job(job_id, status="done", out_path=out, mosaic_style=style,
                  keep=json.dumps(r["keep"]), blur=json.dumps(r["blur"]),
                  t_export_sec=r["sec"], error=None,
                  expires_at=time.time() + config.RETENTION_HOURS * 3600)
    log.info("export %s %s モザイク %d・かけない %d %.2fs", job_id, style, len(r["blur"]),
             len(r["keep"]), r["sec"])
    return JSONResponse(_public(db.get_job(job_id)), headers=NO_STORE)


@app.get("/api/jobs/{job_id}/result.jpg")
def result_image(request: Request, job_id: str, dl: int = 0):
    require_api(request)
    j = _load(job_id)
    if not j["out_path"] or not os.path.exists(j["out_path"]):
        raise HTTPException(410, "削除済みです")
    name = time.strftime("mosaic_%Y%m%d_%H%M.jpg", time.localtime(j["updated_at"]))
    headers = dict(NO_STORE)
    if dl:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(j["out_path"], media_type="image/jpeg", headers=headers)
