"""運営向けの見張り: worker 停止・ディスク逼迫（#mosaic-alerts）と毎朝7時の日次サマリ（#mosaic-usage）。"""

import datetime
import logging
import os
import shutil
import statistics
import time

from .. import config, db
from . import billing, discord

log = logging.getLogger("uvicorn.error")
STARTED = time.time()
STATE = {"worker_down": False, "disk_alert_at": 0.0, "past_due_at": 0.0}
DAILY_HOUR = 7


def heartbeat_path():
    return os.path.join(config.DATA_DIR, "worker.heartbeat")


def check_worker(now=None):
    now = now or time.time()
    try:
        age = now - os.path.getmtime(heartbeat_path())
    except OSError:
        age = now - STARTED  # まだ一度も心拍が無い
    down = age > config.WORKER_STALE_SEC
    if down and not STATE["worker_down"]:
        discord.alert(f"worker が止まっています（最後の心拍から {int(age // 60)} 分）。動画の処理が進みません")
    elif not down and STATE["worker_down"]:
        discord.send("alerts", "【復旧】worker が動き出しました")
    STATE["worker_down"] = down


def check_disk(now=None):
    now = now or time.time()
    du = shutil.disk_usage(config.DATA_DIR)
    free = du.free / du.total
    if free < config.DISK_ALERT_FREE_RATIO and now - STATE["disk_alert_at"] >= 3600:
        STATE["disk_alert_at"] = now
        discord.alert(f"ディスクの空きが {free * 100:.0f}% です（{du.free / 1e9:.1f}GB / {du.total / 1e9:.0f}GB）")


def daily_stats(day):
    """その日に作られたジョブの処理数・失敗・解析の待ち時間の中央値（日次サマリと管理画面で共用）。"""
    t0 = time.mktime(day.timetuple())
    t1 = t0 + 86400
    jobs = [j for j in db.jobs_created_between(t0, t1) if j["status"] != "uploading"]
    vids = [j for j in jobs if j["kind"] == "video"]
    imgs = [j for j in jobs if j["kind"] == "image"]
    done = lambda js: sum(1 for j in js if j["t_export_sec"] is not None)
    waits = [j["prep_started_at"] - j["queued_at"] for j in vids if j["queued_at"] and j["prep_started_at"]]
    return {"videos": len(vids), "videos_done": done(vids), "images": len(imgs), "images_done": done(imgs),
            "failed": sum(1 for j in jobs if j["status"] == "failed"),
            "wait_median_sec": statistics.median(waits) if waits else None, "wait_count": len(waits),
            "new_users": db.count_users_created_between(t0, t1)}


def daily_summary_text(day):
    s = daily_stats(day)
    wait = f"{s['wait_median_sec'] / 60:.1f}分（{s['wait_count']}本）" if s["wait_count"] else "—（解析した動画なし）"
    return (f"日次サマリ {day.isoformat()}\n"
            f"処理数: 動画 {s['videos']} 本（完成 {s['videos_done']}）・写真 {s['images']} 枚（完成 {s['images_done']}）\n"
            f"失敗: {s['failed']} 件\n"
            f"解析の待ち時間の中央値: {wait}\n"
            f"新規登録: {s['new_users']} 人")


def daily_summary(now=None, force=False):
    """毎朝7時以降に1回、前日の分を送る（送った日は settings.daily_summary_last に記録）。"""
    now_dt = datetime.datetime.fromtimestamp(now or time.time())
    today = now_dt.date().isoformat()
    if not force and (now_dt.hour < DAILY_HOUR or db.get_setting("daily_summary_last") == today):
        return False
    db.set_setting("daily_summary_last", today)
    discord.usage(daily_summary_text(now_dt.date() - datetime.timedelta(days=1)), wait=True)
    return True


def check_past_due(now=None):
    """決済失敗のまま猶予（7日）を過ぎた購読を、次の更新日で終わらせる（1時間に1回）。"""
    now = now or time.time()
    if now - STATE["past_due_at"] < 3600:
        return 0
    STATE["past_due_at"] = now
    n = 0
    for u in db.past_due_users(now - billing.GRACE_SEC):
        if u["stripe_subscription_id"] and billing.schedule_cancel(u):
            n += 1
    return n
