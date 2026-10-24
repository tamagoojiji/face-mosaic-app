"""顔モザイク Web版 試験版（写真1枚モード・動画モード・合言葉認証）。"""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import statistics
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from worker.engine import photo
from worker.engine import video as vengine
from worker.run import paths as vpaths

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
        db.drop_parts(j["id"])
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
        await asyncio.sleep(600)


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


def _owner_serializer():
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="fm-owner")


@app.middleware("http")
async def owner_cookie(request: Request, call_next):
    """初回アクセスで端末ごとの owner_id を発行（署名付き Cookie・1年）。"""
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
    if not authed(request):
        raise HTTPException(401, "合言葉を入れ直してください")


def page(name):
    return FileResponse(os.path.join(WEB, name), headers={"Cache-Control": "no-store"})


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return PlainTextResponse("ok")


@app.api_route("/", methods=["GET", "HEAD"])
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
    owner = getattr(request.state, "owner", None)
    if not j or (j["owner"] is not None and j["owner"] != owner):  # 他の端末の写真は存在しない扱い（owner 列追加前の旧行は期限まで従来どおり）
        raise HTTPException(404, "見つかりません")
    return j


def _faces(job_id):
    p = os.path.join(job_dir(job_id), "faces.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _public(j, with_faces=False):
    if j["kind"] == "video":
        return _public_video(j, detail=with_faces)
    expired = j["status"] != "deleted" and j["expires_at"] is not None and j["expires_at"] <= time.time()
    d = {"id": j["id"], "status": "deleted" if expired else j["status"], "width": j["width"], "height": j["height"],
         "style": j["mosaic_style"] or "pixel", "keep": json.loads(j["keep"] or "[]"),
         "blur": json.loads(j["blur"] or "[]"), "error": j["error"],
         "expires_at": j["expires_at"], "created_at": j["created_at"], "now": time.time(),
         "has_out": not expired and bool(j["out_path"] and os.path.exists(j["out_path"]))}
    if with_faces:
        d["faces"] = _faces(j["id"])
        d["min_conf"] = photo.MIN_CONF
    return d


NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/jobs")
def list_jobs(request: Request):
    require_api(request)
    return JSONResponse([_public(j) for j in db.recent_jobs(request.state.owner)],
                        headers=NO_STORE)


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    require_api(request)
    return JSONResponse(_public(_load(request, job_id), with_faces=True), headers=NO_STORE)


@app.post("/api/photos")
def upload_photo(request: Request, file: UploadFile = File(...)):
    require_api(request)
    if not request.state.owner:
        raise HTTPException(401, "合言葉を入れ直してください")
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
                  owner=request.state.owner,
                  width=r["width"], height=r["height"], mosaic_style="pixel",
                  expires_at=time.time() + config.RETENTION_HOURS * 3600)
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
    j = _load(request, job_id)
    if not j["out_path"] or not os.path.exists(j["out_path"]) \
            or (j["expires_at"] or 0) <= time.time():
        raise HTTPException(410, "削除済みです")
    name = time.strftime("mosaic_%Y%m%d_%H%M.jpg", time.localtime(j["updated_at"]))
    headers = dict(NO_STORE)
    if dl:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(j["out_path"], media_type="image/jpeg", headers=headers)


# ---------------------------------------------------------------- 動画モード

# 1分あたりの目安秒（引き継ぎ 4節・いまのVPS・1080p高速モード）。実測が貯まったら中央値に置き換わる
FALLBACK_PER_MIN = {"prepare": 70, "track": 144, "feature": 98, "export_pixel": 159, "export_blur": 291}
PREP_STAGES = (("t_prepare_sec", "prepare", "convert"), ("t_track_sec", "track", "track"),
               ("t_feature_sec", "feature", "feature"))
VIDEO_BUSY = ("queued", "preparing", "queued_export", "exporting")
COMPLETING = set()  # complete の二重実行よけ


def _rates():
    rows = db.done_video_stats()

    def med(col, ok=lambda r: True):
        v = [r[col] / r["duration_sec"] * 60 for r in rows if r[col] and ok(r)]
        return statistics.median(v) if v else None
    return {"prepare": med("t_prepare_sec") or FALLBACK_PER_MIN["prepare"],
            "track": med("t_track_sec") or FALLBACK_PER_MIN["track"],
            "feature": med("t_feature_sec") or FALLBACK_PER_MIN["feature"],
            "export_pixel": med("t_export_sec", lambda r: r["mosaic_style"] != "blur") or FALLBACK_PER_MIN["export_pixel"],
            "export_blur": med("t_export_sec", lambda r: r["mosaic_style"] == "blur") or FALLBACK_PER_MIN["export_blur"]}


def _remaining(j, rates, now):
    """このジョブの残り秒の目安と、いまの工程。"""
    m = (j["duration_sec"] or 60) / 60.0
    exp = rates["export_blur" if j["mosaic_style"] == "blur" else "export_pixel"] * m
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
    st = j["status"]
    if st != "deleted" and j["expires_at"] is not None and j["expires_at"] <= now:
        st = "deleted"
    p = vpaths(j["id"])
    d = {"id": j["id"], "kind": "video", "status": st, "duration_sec": j["duration_sec"],
         "width": j["width"], "height": j["height"], "style": j["mosaic_style"] or "pixel",
         "keep": json.loads(j["keep"] or "[]"), "blur": json.loads(j["blur"] or "[]"), "error": j["error"],
         "expires_at": j["expires_at"], "created_at": j["created_at"], "now": now,
         "has_out": st != "deleted" and bool(j["out_path"] and os.path.exists(j["out_path"])),
         "has_thumb": st != "deleted" and os.path.exists(os.path.join(p["shots"], "shot0.jpg")),
         "idle_warn": st in ("awaiting_pick", "awaiting_review") and j["expires_at"] is not None
         and j["expires_at"] - now <= (config.IDLE_DELETE_HOURS - config.IDLE_WARN_HOURS) * 3600}
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
    return d


def _video(request, job_id, statuses=None):
    j = _load(request, job_id)
    if j["kind"] != "video":
        raise HTTPException(404, "見つかりません")
    if j["status"] != "deleted" and j["expires_at"] is not None and j["expires_at"] <= time.time():
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


def _mmss(sec):
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}秒"
    return f"{sec // 60}分{sec % 60}秒" if sec % 60 else f"{sec // 60}分"


@app.get("/api/config")
def api_config(request: Request):
    require_api(request)
    return JSONResponse({"max_video_sec": config.MAX_VIDEO_SEC, "max_video_bytes": config.MAX_VIDEO_BYTES,
                         "part_size": config.PART_SIZE}, headers=NO_STORE)


def _upload_info(j):
    size = j["bytes"] or 0
    n = max(1, -(-size // config.PART_SIZE))
    return {"id": j["id"], "status": j["status"], "bytes": size, "part_size": config.PART_SIZE, "parts": n,
            "received": sorted(db.parts(j["id"])) if j["status"] == "uploading" else []}


@app.post("/api/uploads")
async def create_upload(request: Request):
    require_api(request)
    if not request.state.owner:
        raise HTTPException(401, "合言葉を入れ直してください")
    b = await _json_body(request)
    try:
        size, dur = int(b.get("bytes") or 0), float(b.get("duration") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "送信内容が正しくありません")
    if size <= 0:
        raise HTTPException(400, "動画を選び直してください")
    if size > config.MAX_VIDEO_BYTES:
        raise HTTPException(413, f"この動画は {size / 1048576:.0f}MB です。試験版は1GBまでです")
    if dur > config.MAX_VIDEO_SEC + 0.5:
        raise HTTPException(400, f"この動画は {_mmss(dur)} です。試験版は{_mmss(config.MAX_VIDEO_SEC)}までです")
    job_id = secrets.token_urlsafe(9)
    os.makedirs(os.path.join(job_dir(job_id), "parts"), exist_ok=True)
    db.insert_job(job_id, kind="video", status="uploading", bytes=size, duration_sec=dur or None,
                  owner=request.state.owner, mosaic_style="pixel",
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
    require_api(request)
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

    def reject(msg, e=None):
        log.warning("upload 不可 %s: %s %s", job_id, msg, e or "")
        shutil.rmtree(d, ignore_errors=True)
        db.drop_parts(job_id)
        db.update_job(job_id, status="failed", error=msg, src_path=None, expires_at=None)
        raise HTTPException(400, msg)

    try:
        try:
            v = await asyncio.to_thread(join)
        except Exception as e:
            reject("動画として読み込めませんでした。iPhone のカメラで撮った動画を選んでください", e)
        if os.path.getsize(src) != info["bytes"]:
            reject("受け取った大きさが合いませんでした。もう一度送ってください")
        if v["duration"] > config.MAX_VIDEO_SEC + 0.5:
            reject(f"この動画は {_mmss(v['duration'])} です。試験版は{_mmss(config.MAX_VIDEO_SEC)}までです")
        db.drop_parts(job_id)
        db.update_job(job_id, status="queued", src_path=src, duration_sec=round(v["duration"], 2),
                      width=v["out_width"], height=v["out_height"], fps=v["fps"], expires_at=None)
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
                  blur=json.dumps(sorted(blur)), expires_at=time.time() + config.IDLE_DELETE_HOURS * 3600)
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
    db.update_job(job_id, status="queued_export", keep=json.dumps(sorted(keep)), blur=json.dumps(sorted(blur)),
                  mosaic_style=style, error=None, expires_at=None)
    log.info("review %s %s keep %d blur %d（%s）", job_id, style, len(keep), len(blur),
             "選び直し" if j["status"] == "done" else "初回")
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
