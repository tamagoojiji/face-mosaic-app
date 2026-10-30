"""運営の専用管理画面（/admin・users.role=admin の人だけ）。DESIGN 3節「運営（専用管理画面）」。

admin 以外には 404（存在を知らせない）。運営の操作は全部 audit_log に残す。
LINE の userId・Stripe の ID は画面に出さない（有無だけ）。
"""

import datetime
import json
import os
import re
import shutil
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import config, db
from . import main as core
from .services import discord, monitor, quota, retention

router = APIRouter(prefix="/admin")
NOT_FOUND = "Not Found"  # 存在しないページと同じ応答
STATUSES = ("active", "stopped")
ROLES = ("user", "tester", "admin")
# ジョブ一覧の絞り込み
JOB_GROUPS = {"queued": ("uploading", "queued", "queued_export"), "running": db.RUNNING,
              "waiting": ("awaiting_pick", "awaiting_review"), "failed": ("failed",), "done": ("done",),
              "deleted": ("deleted",)}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _ToLogin(Exception):
    pass


def require_admin(request: Request, api=False):
    u = core.current_user(request)
    if not u:
        if api:
            raise HTTPException(401, "ログインし直してください")
        raise _ToLogin()
    if u["role"] != "admin":
        raise HTTPException(404, NOT_FOUND)
    return u


def _page(request, name):
    try:
        require_admin(request)
    except _ToLogin:
        return RedirectResponse("/", status_code=302)
    return core.page("admin/" + name)


def _ok(data):
    return JSONResponse(data, headers=core.NO_STORE)


def _rows(sql, args=()):
    with db.connect() as con:
        return [dict(r) for r in con.execute(sql, args)]


def _count(sql, args=()):
    with db.connect() as con:
        return con.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------- 画面

@router.get("")
def dashboard_page(request: Request):
    return _page(request, "dashboard.html")


@router.get("/users")
def users_page(request: Request):
    return _page(request, "users.html")


@router.get("/users/{uid}")
def user_page(request: Request, uid: int):
    return _page(request, "user.html")


@router.get("/jobs")
def jobs_page(request: Request):
    return _page(request, "jobs.html")


@router.get("/reports")
def reports_page(request: Request):
    return _page(request, "reports.html")


@router.get("/settings")
def settings_page(request: Request):
    return _page(request, "settings.html")


# ---------------------------------------------------------------- 共通の形

def _contract(u, now):
    """契約状態の表示: free / お試し残り n 日 / お試し終了 / active / canceling / past_due。"""
    if u.get("role") == "tester":
        if not quota.is_tester(u, now):
            return "テスター終了"
        if u.get("tester_until"):
            d = datetime.datetime.fromtimestamp(u["tester_until"] - 1, quota.JST)
            return f"テスター（{d.month}/{d.day}まで）"
        return "テスター"
    if u.get("sub_status") in ("active", "canceling", "past_due"):
        return u["sub_status"]
    t = u.get("trial_ends_at")
    if (u.get("plan") or "free") == "free" and u.get("line_user_id") and t:
        return f"お試し残り {max(0, -(-int(t - now) // 86400))} 日" if t > now else "お試し終了"
    return "free"


def _tester_until_date(u):
    """tester_until（期限日の翌日 0:00 JST）→ 期限日 "YYYY-MM-DD"（JST）。"""
    t = u.get("tester_until")
    return datetime.datetime.fromtimestamp(t - 1, quota.JST).date().isoformat() if t else None


def _user_row(u, usage, now):
    return {"id": u["id"], "display_name": u["display_name"], "instagram_username": u["instagram_username"],
            "login": "LINE" if u["line_user_id"] else "合言葉" if u["device_id"] else "-",
            "plan": u["plan"], "plan_label": quota.PLANS.get(u["plan"], {}).get("label", u["plan"]),
            "hq": bool(u["hq"]), "launch_price": bool(u["launch_price"]), "role": u["role"], "status": u["status"], "contract": _contract(u, now),
            "tester_until": u.get("tester_until"), "tester_until_date": _tester_until_date(u),
            "usage_video_sec": (usage or {}).get("video_seconds_used") or 0,
            "usage_images": (usage or {}).get("image_count") or 0, "created_at": u["created_at"]}


def _job_row(j, users=None):
    u = (users or {}).get(j.get("user_id"))
    return {"id": j["id"], "kind": j["kind"], "status": j["status"], "user_id": j.get("user_id"),
            "user_name": (u or {}).get("display_name"), "duration_sec": j.get("duration_sec"),
            "t_prepare_sec": j.get("t_prepare_sec"), "t_track_sec": j.get("t_track_sec"),
            "t_feature_sec": j.get("t_feature_sec"), "t_export_sec": j.get("t_export_sec"),
            "hq_export": bool(j.get("hq_export")), "error": j.get("error"), "created_at": j["created_at"],
            "updated_at": j["updated_at"], "label": core.job_label(j)}


def _users_by_id(ids):
    ids = [i for i in set(ids) if i is not None]
    if not ids:
        return {}
    rows = _rows(f"SELECT id, display_name, instagram_username FROM users WHERE id IN ({','.join('?' * len(ids))})",
                 ids)
    return {r["id"]: r for r in rows}


# ---------------------------------------------------------------- ダッシュボード

@router.get("/api/dashboard")
def api_dashboard(request: Request):
    require_admin(request, api=True)
    now = time.time()
    du = shutil.disk_usage(config.DATA_DIR)
    try:
        hb = now - os.path.getmtime(monitor.heartbeat_path())
    except OSError:
        hb = None
    users = {"active": _count("SELECT COUNT(*) FROM users WHERE status='active'"),
             "paid": _count(f"SELECT COUNT(*) FROM users WHERE status='active' AND plan IN "
                            f"({','.join('?' * len(quota.PAID))})", quota.PAID),
             "canceling": _count("SELECT COUNT(*) FROM users WHERE status='active' AND sub_status='canceling'"),
             "past_due": _count("SELECT COUNT(*) FROM users WHERE status='active' AND sub_status='past_due'")}
    failed = _rows("SELECT * FROM jobs WHERE status='failed' ORDER BY updated_at DESC LIMIT 5")
    fusers = _users_by_id([j["user_id"] for j in failed])
    return _ok({
        "running": _count("SELECT COUNT(*) FROM jobs WHERE kind='video' AND status IN (?,?)", db.RUNNING),
        "queued": _count("SELECT COUNT(*) FROM jobs WHERE kind='video' AND status IN (?,?)", db.QUEUED),
        "today": monitor.daily_stats(datetime.date.today()),
        "disk": {"free_ratio": du.free / du.total, "free_gb": du.free / 1e9, "total_gb": du.total / 1e9,
                 "alert_ratio": config.DISK_ALERT_FREE_RATIO},
        "heartbeat_age": hb, "heartbeat_stale": config.WORKER_STALE_SEC,
        "users": users, "open_reports": _count("SELECT COUNT(*) FROM reports WHERE status='open'"),
        "failed_jobs": [_job_row(j, fusers) for j in failed]})


# ---------------------------------------------------------------- 利用者

@router.get("/api/users")
def api_users(request: Request, q: str = "", status: str = "", plan: str = ""):
    require_admin(request, api=True)
    sql, args = "SELECT * FROM users WHERE 1=1", []
    q = q.strip().lstrip("@")
    if q:
        sql += " AND (display_name LIKE ? OR instagram_username LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if status:
        sql += " AND status=?"
        args.append(status)
    if plan:
        sql += " AND plan=?"
        args.append(plan)
    users = _rows(sql + " ORDER BY id DESC LIMIT 500", args)
    month = quota.month_of()
    usage = {r["user_id"]: r for r in _rows("SELECT * FROM usage WHERE month=?", (month,))}
    now = time.time()
    return _ok({"users": [_user_row(u, usage.get(u["id"]), now) for u in users], "month": month,
                "plans": [{"key": k, "label": quota.PLANS[k]["label"]} for k in quota.PLAN_ORDER]})


def _user_detail(u):
    now = time.time()
    st = quota.status(u)
    lim = st["limits"]
    d = _user_row(u, st["usage"], now)
    d.update(has_line=bool(u["line_user_id"]), has_stripe_customer=bool(u["stripe_customer_id"]),
             has_subscription=bool(u["stripe_subscription_id"]), sub_status=u["sub_status"],
             current_period_end=u["current_period_end"], trial_ends_at=u["trial_ends_at"],
             cancel_at_period_end=bool(u["cancel_at_period_end"]), pending_plan=u["pending_plan"],
             notify=bool(u["notify"]), agreed_at=u["agreed_at"], withdrawn_at=u["withdrawn_at"],
             withdraw_reason=core.WITHDRAW_REASONS.get(u["withdraw_reason"]) if u["withdraw_reason"] else None,
             withdraw_note=u["withdraw_note"], hq_effective=lim["hq"],
             limits={"images": lim["images"], "video_sec_month": lim["video_sec_month"],
                     "video_count_month": lim["video_count_month"]},
             usage_videos=st["usage"]["video_count"])
    jobs = _rows("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (u["id"],))
    reports = _rows("SELECT * FROM reports WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (u["id"],))
    audit = _rows("SELECT a.*, u.display_name AS actor_name FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id "
                  "WHERE a.target=? ORDER BY a.created_at DESC LIMIT 100", (f"user:{u['id']}",))
    return {"user": d, "jobs": [_job_row(j) for j in jobs],
            "reports": [_report_row(r, {}, {j["id"]: j for j in jobs}) for r in reports], "audit": audit,
            "plans": [{"key": k, "label": quota.PLANS[k]["label"]} for k in quota.PLAN_ORDER],
            "hq_plans": list(quota.HQ_OPTION["plans"])}


def _target_user(uid):
    u = db.get_user(uid)
    if not u:
        raise HTTPException(404, "利用者が見つかりません")
    return u


@router.get("/api/users/{uid}")
def api_user(request: Request, uid: int):
    require_admin(request, api=True)
    return _ok(_user_detail(_target_user(uid)))


@router.post("/api/users/{uid}")
async def api_user_update(request: Request, uid: int):
    admin = require_admin(request, api=True)
    u = _target_user(uid)
    b = await core._json_body(request)
    if u["status"] == "withdrawn":
        raise HTTPException(400, "退会済みの利用者は変更できません")
    new = {"plan": b.get("plan", u["plan"]), "hq": 1 if b.get("hq", u["hq"]) else 0,
           "role": b.get("role", u["role"]), "status": b.get("status", u["status"])}
    if new["plan"] not in quota.PLANS:
        raise HTTPException(400, "プランを選び直してください")
    if new["hq"] and new["plan"] not in quota.HQ_OPTION["plans"]:
        raise HTTPException(400, "高画質オプションは動画A・動画B のときだけ付けられます")
    if new["role"] not in ROLES or new["status"] not in STATUSES:
        raise HTTPException(400, "権限・状態を選び直してください")
    if u["id"] == admin["id"] and (new["role"] != "admin" or new["status"] != "active"):
        raise HTTPException(400, "自分の権限と状態は変えられません（ほかの運営に頼んでください）")
    try:
        new["tester_until"] = quota.tester_until_ts(b.get("tester_until_date", _tester_until_date(u)) or "")
    except ValueError:
        raise HTTPException(400, "テスターの期限は YYYY-MM-DD で入れてください")
    if new["role"] != "tester":
        new["tester_until"] = None
    diff = {k: [u[k], v] for k, v in new.items() if u[k] != v}
    if diff:
        db.update_user(u["id"], **{k: v[1] for k, v in diff.items()})
        db.audit(admin["id"], "admin.user_update", f"user:{u['id']}", json.dumps(diff, ensure_ascii=False))
        changes = "・".join(f"{k} {a}→{c}" for k, (a, c) in diff.items())
        discord.usage(f"運営が利用者を変更: {discord.who(db.get_user(u['id']))}（{changes}）")
    return _ok(dict(_user_detail(db.get_user(u["id"])), changed=list(diff)))


# ---------------------------------------------------------------- ジョブ

@router.get("/api/jobs")
def api_jobs(request: Request, status: str = ""):
    require_admin(request, api=True)
    if status and status not in JOB_GROUPS:
        raise HTTPException(400, "絞り込みを選び直してください")
    if status:
        st = JOB_GROUPS[status]
        jobs = _rows(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(st))}) "
                     "ORDER BY created_at DESC LIMIT 300", st)
    else:
        jobs = _rows("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 300")
    users = _users_by_id([j["user_id"] for j in jobs])
    counts = {k: _count(f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(v))})", v)
              for k, v in JOB_GROUPS.items()}
    return _ok({"jobs": [_job_row(j, users) for j in jobs], "counts": counts,
                "total": _count("SELECT COUNT(*) FROM jobs")})


def _target_job(job_id):
    j = db.get_job(job_id) if core.JOB_ID_RE.match(job_id) else None
    if not j:
        raise HTTPException(404, "ジョブが見つかりません")
    return j


@router.post("/api/jobs/{job_id}/retry")
def api_job_retry(request: Request, job_id: str):
    """失敗した動画をもう一度処理する: 書き出しの材料が残っていれば書き出しから、元動画があれば解析から。"""
    admin = require_admin(request, api=True)
    j = _target_job(job_id)
    if j["status"] != "failed":
        raise HTTPException(409, "失敗したジョブだけもう一度処理できます")
    if j["kind"] != "video":
        raise HTTPException(409, "写真は利用者がその場で書き出し直せるため、再処理はありません")
    p = core.vpaths(job_id)
    if j["keep"] is not None and os.path.exists(p["v1080"]) and os.path.exists(p["tracks"]):
        to = "queued_export"
    elif os.path.exists(p["src"]):
        to = "queued"
    else:
        raise HTTPException(409, "元の動画が残っていないため、もう一度処理できません。利用者に送り直してもらってください")
    db.update_job(job_id, status=to, error=None, expires_at=None)
    db.audit(admin["id"], "admin.job_retry", f"job:{job_id}", json.dumps({"to": to}))
    return _ok({"ok": True, "status": to})


@router.post("/api/jobs/{job_id}/delete")
def api_job_delete(request: Request, job_id: str):
    admin = require_admin(request, api=True)
    j = _target_job(job_id)
    if j["status"] in db.RUNNING:
        raise HTTPException(409, "処理中のジョブは削除できません。終わってからもう一度お試しください")
    shutil.rmtree(core.job_dir(job_id), ignore_errors=True)
    with db.connect() as con:
        con.execute("DELETE FROM upload_parts WHERE job_id=?", (job_id,))
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    db.audit(admin["id"], "admin.job_delete", f"job:{job_id}",
             json.dumps({"status": j["status"], "user_id": j["user_id"], "kind": j["kind"]}))
    return _ok({"ok": True})


# ---------------------------------------------------------------- 通報

REPORT_MEDIA_TYPES = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
                      "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}


def _media_type(path):
    return REPORT_MEDIA_TYPES.get(path.rsplit(".", 1)[-1].lower(), "application/octet-stream")


def _report_job_file(j, which):
    """報告の対象ジョブの元ファイル（source）・完成品（output）。無ければ None。"""
    if not j:
        return None
    if which == "source":
        p = os.path.join(core.job_dir(j["id"]), "src.mp4" if j["kind"] == "video" else "src.jpg")
    else:
        p = j.get("out_path")
    return p if p and os.path.exists(p) else None


def _report_row(r, users, jobs):
    u = users.get(r["user_id"]) or {}
    j = jobs.get(r["job_id"]) if r["job_id"] else None
    return {"id": r["id"], "user_id": r["user_id"], "user_name": u.get("display_name"),
            "instagram_username": u.get("instagram_username"), "job_id": r["job_id"],
            "job_label": core.job_label(j) if r["job_id"] else None, "category": r["category"],
            "stage": core.REPORT_CATEGORIES.get(r["category"], ("（旧分類）", ""))[0],
            "category_label": core.REPORT_CATEGORIES.get(r["category"], ("", r["category"]))[1], "body": r["body"],
            "status": r["status"], "admin_note": r["admin_note"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "image_count": r["image_count"], "video_name": r["video_name"],
            "job_files": {w: bool(_report_job_file(j, w)) for w in ("source", "output")}}


@router.get("/api/reports")
def api_reports(request: Request, status: str = "", stage: str = ""):
    require_admin(request, api=True)
    if status and status not in ("open", "closed") or stage and stage not in core.REPORT_STAGES:
        raise HTTPException(400, "絞り込みを選び直してください")
    sql, args = "SELECT * FROM reports WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    if stage:
        codes = [k for k, (s, _l) in core.REPORT_CATEGORIES.items() if s == stage]
        sql += f" AND category IN ({','.join('?' * len(codes))})"
        args += codes
    rows = _rows(sql + " ORDER BY created_at DESC LIMIT 300", args)
    users = _users_by_id([r["user_id"] for r in rows])
    jids = [r["job_id"] for r in rows if r["job_id"]]
    jobs = {j["id"]: j for j in _rows(f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(jids))})", jids)} \
        if jids else {}
    return _ok({"reports": [_report_row(r, users, jobs) for r in rows],
                "counts": {s: _count("SELECT COUNT(*) FROM reports WHERE status=?", (s,)) for s in ("open", "closed")},
                "stages": list(core.REPORT_STAGES)})


@router.get("/api/reports/{rid}/images/{n}")
def api_report_image(request: Request, rid: int, n: int):
    require_admin(request, api=True)
    r = db.get_report(rid)
    p = os.path.join(retention.report_dir(rid), f"{n}.jpg")
    if not r or not 1 <= n <= r["image_count"] or not os.path.exists(p):
        raise HTTPException(404, "画像が見つかりません（作成から90日たったか、退会で削除済みです）")
    return FileResponse(p, media_type="image/jpeg", headers=core.NO_STORE)


@router.get("/api/reports/{rid}/video")
def api_report_video(request: Request, rid: int):
    require_admin(request, api=True)
    r = db.get_report(rid)
    p = os.path.join(retention.report_dir(rid), r["video_name"]) if r and r["video_name"] else None
    if not p or not os.path.exists(p):
        raise HTTPException(404, "動画が見つかりません（作成から90日たったか、退会で削除済みです）")
    return FileResponse(p, media_type=_media_type(p), headers=core.NO_STORE)


@router.get("/api/reports/{rid}/job/{which}")
def api_report_job_file(request: Request, rid: int, which: str):
    require_admin(request, api=True)
    r = db.get_report(rid)
    j = db.get_job(r["job_id"]) if r and r["job_id"] and which in ("source", "output") else None
    p = _report_job_file(j, which)
    if not p:
        raise HTTPException(404, "元の動画・写真は24時間、完成品は30日で削除されます（削除済み）")
    return FileResponse(p, media_type=_media_type(p), headers=core.NO_STORE)


@router.post("/api/reports/{rid}")
async def api_report_update(request: Request, rid: int):
    admin = require_admin(request, api=True)
    r = db.get_report(rid)
    if not r:
        raise HTTPException(404, "通報が見つかりません")
    b = await core._json_body(request)
    status = b.get("status", r["status"])
    if status not in ("open", "closed"):
        raise HTTPException(400, "状態を選び直してください")
    note = str(b.get("admin_note") if b.get("admin_note") is not None else (r["admin_note"] or "")).strip()[:2000]
    db.update_report(rid, status=status, admin_note=note or None)
    db.audit(admin["id"], "admin.report_update", f"report:{rid}",
             json.dumps({"status": [r["status"], status], "note_changed": (r["admin_note"] or "") != note},
                        ensure_ascii=False))
    return _ok({"ok": True})


# ---------------------------------------------------------------- 設定

SETTING_LABELS = {
    "launch_price_until": "リリース記念価格の申し込み期限",
    "campaign_on": "リリース記念", "campaign_from": "リリース記念の開始日", "campaign_to": "リリース記念の終了日",
    "campaign_images": "記念中の写真（枚/月）", "campaign_videos": "記念中の1分動画（本/月）",
    "source_retention_hours": "元データを消すまで（時間）", "output_retention_days": "完成品の保存（日）",
    "idle_warn_hours": "放置の再通知まで（時間）", "idle_delete_hours": "放置で削除するまで（時間）",
    "mosaic_default": "モザイクの標準",
    "signup_remaining": "新規登録の残り枠（空=制限なし・登録のたびに1減る・0で受付停止）",
    "signup_tester_days": "新規登録した人をテスターにする日数（0=通常の登録）",
}


def _settings_view():
    saved = db.all_settings()
    return {"settings": {k: {"value": quota.setting(k), "default": quota.SETTING_DEFAULTS[k],
                             "overridden": k in saved, "label": SETTING_LABELS[k]} for k in SETTING_LABELS},
            "campaign_active": quota.campaign_active(), "launch_active": quota.launch_active()}


def _check_setting(k, v):
    if k == "campaign_on":
        return v in ("0", "1")
    if k in ("campaign_from", "campaign_to", "launch_price_until"):
        return v == "" or bool(DATE_RE.match(v))
    if k == "mosaic_default":
        return v in ("block", "glass")
    if k == "signup_remaining":
        return v == "" or v.isdigit()
    if k == "signup_tester_days":
        return v.isdigit()
    try:
        n = float(v)
    except ValueError:
        return False
    if k in ("campaign_images", "campaign_videos"):
        return n >= 0 and n == int(n)
    return n > 0


@router.get("/api/settings")
def api_settings(request: Request):
    require_admin(request, api=True)
    return _ok(_settings_view())


@router.post("/api/settings")
async def api_settings_update(request: Request):
    admin = require_admin(request, api=True)
    b = await core._json_body(request)
    new = {k: str(b[k]).strip() for k in SETTING_LABELS if k in b and b[k] is not None}
    bad = [SETTING_LABELS[k] for k, v in new.items() if not _check_setting(k, v)]
    if bad:
        raise HTTPException(400, "入れ直してください: " + "・".join(bad))
    merged = {k: new.get(k, quota.setting(k)) for k in SETTING_LABELS}
    if merged["campaign_from"] and merged["campaign_to"] and merged["campaign_from"] > merged["campaign_to"]:
        raise HTTPException(400, "リリース記念の終了日は開始日より後にしてください")
    if float(merged["idle_warn_hours"]) >= float(merged["idle_delete_hours"]):
        raise HTTPException(400, "放置の再通知は、削除するまでの時間より短くしてください")
    diff = {k: [quota.setting(k), v] for k, v in new.items() if quota.setting(k) != v}
    for k, (_old, v) in diff.items():
        db.set_setting(k, v)
    if diff:
        db.audit(admin["id"], "admin.settings", "settings", json.dumps(diff, ensure_ascii=False))
    return _ok(dict(_settings_view(), changed=list(diff)))
