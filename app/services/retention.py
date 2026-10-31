"""保存期間（元データ SOURCE_RETENTION_HOURS／完成品 OUTPUT_RETENTION_DAYS）と放置の再通知。"""

import json
import logging
import os
import shutil
import time

from .. import config, db
from . import notify, quota

log = logging.getLogger("uvicorn.error")


REPORT_IMAGE_DAYS = 90  # 通報の画像・動画・解析データの控えは作成から90日で消す（文字は残す）


def job_dir(job_id):
    return os.path.join(config.DATA_DIR, "jobs", job_id)


def report_dir(report_id):
    return os.path.join(config.DATA_DIR, "reports", str(report_id))


def delete_report_images(r):
    shutil.rmtree(report_dir(r["id"]), ignore_errors=True)
    db.update_report(r["id"], image_count=0, video_name=None, analysis=0)


# 通報の調査用に控える解析データ（元動画・完成品は控えない）。判定（decide）を動画なしで再現できる組
ANALYSIS_FILES = ("tracks.json", "features.npz", "review.json", "shots.json", "shots", "thumbs")


def save_report_analysis(rid, j):
    """通報の対象ジョブの解析データを reports/<id>/analysis/ に控える（完成後24時間で元のほうは消えるため）。
    控えたものが1つでもあれば True。消えていたものは meta.json の missing に残す。"""
    src, dst = job_dir(j["id"]), os.path.join(report_dir(rid), "analysis")
    os.makedirs(dst, exist_ok=True)
    missing = []
    for name in ANALYSIS_FILES:
        p = os.path.join(src, name)
        if os.path.isdir(p):
            shutil.copytree(p, os.path.join(dst, name), dirs_exist_ok=True)
        elif os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, name))
        else:
            missing.append(name)
    meta = {k: j.get(k) for k in ("id", "kind", "status", "duration_sec", "fps", "width", "height", "hdr",
                                  "mosaic_style", "engine_ver", "hq_export", "created_at")}
    for k in ("picks", "keep", "blur"):
        meta[k] = json.loads(j.get(k) or "[]")
    meta["missing"] = missing
    if len(missing) == len(ANALYSIS_FILES):  # 何も残っていない（完成から24時間を過ぎた等）
        shutil.rmtree(dst, ignore_errors=True)
        return False
    with open(os.path.join(dst, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return True


def cleanup_report_images(now=None):
    """作成から90日を過ぎた通報の画像・動画・解析データの控えを消す。"""
    now = now or time.time()
    rs = db.old_report_images(now - REPORT_IMAGE_DAYS * 86400)
    for r in rs:
        delete_report_images(r)
    if rs:
        log.info("retention: 通報の画像・動画 %d 件を削除（作成から%d日）: %s", len(rs), REPORT_IMAGE_DAYS,
                 ",".join(f"#{r['id']}" for r in rs))
    return len(rs)


def purge_source(j):
    """完成品だけ残して、元動画・元画像・解析データ（場面写真・切り抜き・追跡結果）を消す。"""
    d = job_dir(j["id"])
    keep = os.path.basename(j["out_path"]) if j.get("out_path") else None
    if os.path.isdir(d):
        for name in os.listdir(d):
            if name == keep:
                continue
            p = os.path.join(d, name)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
    db.drop_parts(j["id"])
    db.update_job(j["id"], src_path=None, src_expires_at=None)


def delete_all(j):
    shutil.rmtree(job_dir(j["id"]), ignore_errors=True)
    db.drop_parts(j["id"])
    db.update_job(j["id"], status="deleted", src_path=None, out_path=None, src_expires_at=None)


def cleanup_expired(now=None):
    now = now or time.time()
    n_all = n_src = n_out = 0
    for j in db.expired_jobs(now):  # 完成前の期限（送信途中・選択の放置・失敗）
        if j["kind"] == "video" and j["status"] in ("awaiting_pick", "awaiting_review"):
            quota.refund_video(j)  # 放置で削除した分は戻す
        delete_all(j)
        n_all += 1
    for j in db.src_expired_jobs(now):  # 完成後: 元データだけ先に消す（選び直しはここまで）
        purge_source(j)
        n_src += 1
    for j in db.out_expired_jobs(now):  # 完成品の保存期限
        delete_all(j)
        n_out += 1
    if n_all or n_src or n_out:
        log.info("retention: 期限切れ %d 件・元データ %d 件・完成品 %d 件を削除", n_all, n_src, n_out)
    return n_all, n_src, n_out


def notify_idle(now=None):
    """選択・確認が IDLE_WARN_HOURS 放置されたら ④ を1回だけ送る。"""
    now = now or time.time()
    left = (quota.idle_delete_hours() - quota.idle_warn_hours()) * 3600
    for j in db.idle_jobs(now + left):
        db.update_job(j["id"], idle_notified=1)
        notify.push("idle", j)
