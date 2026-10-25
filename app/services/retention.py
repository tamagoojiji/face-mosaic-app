"""保存期間（元データ SOURCE_RETENTION_HOURS／完成品 OUTPUT_RETENTION_DAYS）と放置の再通知。"""

import logging
import os
import shutil
import time

from .. import config, db
from . import notify, quota

log = logging.getLogger("uvicorn.error")


def job_dir(job_id):
    return os.path.join(config.DATA_DIR, "jobs", job_id)


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
    left = (config.IDLE_DELETE_HOURS - config.IDLE_WARN_HOURS) * 3600
    for j in db.idle_jobs(now + left):
        db.update_job(j["id"], idle_notified=1)
        notify.push("idle", j)
