"""SQLite（WAL）。jobs テーブルは DESIGN 4節のうち写真・動画モードに要る列だけ。"""

import os
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'image',
  status TEXT NOT NULL,            -- 写真: awaiting_pick | exporting | done | failed | deleted
                                   -- 動画: uploading | queued | preparing | awaiting_pick | awaiting_review |
                                   --       queued_export | exporting | done | failed | deleted
  src_path TEXT,
  out_path TEXT,
  bytes INTEGER,
  width INTEGER,
  height INTEGER,
  mosaic_style TEXT,               -- pixel | blur
  keep TEXT,                       -- json: 残した枠の id
  blur TEXT,                       -- json: モザイクをかけた枠の id
  t_export_sec REAL,
  error TEXT,
  expires_at REAL,
  created_at REAL,
  updated_at REAL,
  owner TEXT                       -- 端末ごとの owner_id（Cookie）。旧行は NULL
);
CREATE TABLE IF NOT EXISTS upload_parts (
  job_id TEXT NOT NULL,
  part_no INTEGER NOT NULL,
  bytes INTEGER,
  received_at REAL,
  PRIMARY KEY (job_id, part_no)
);
"""

# 起動時に足す列（動画モード・DESIGN 4節）
ADD_COLUMNS = {
    "owner": "TEXT",
    "duration_sec": "REAL",
    "fps": "REAL",
    "hdr": "INTEGER",
    "fast_mode": "INTEGER",
    "picks": "TEXT",
    "t_prepare_sec": "REAL",
    "t_track_sec": "REAL",
    "t_feature_sec": "REAL",
}
QUEUED = ("queued", "queued_export")
RUNNING = ("preparing", "exporting")


def connect():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    con = sqlite3.connect(os.path.join(config.DATA_DIR, "app.db"), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init():
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        for name, typ in ADD_COLUMNS.items():  # 旧 DB の移行
            if name not in cols:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {typ}")
        con.execute("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created_at)")


def insert_job(job_id, **cols):
    now = time.time()
    cols.update(id=job_id, created_at=now, updated_at=now)
    keys = ",".join(cols)
    with connect() as con:
        con.execute(f"INSERT INTO jobs ({keys}) VALUES ({','.join('?' * len(cols))})",
                    list(cols.values()))


def update_job(job_id, **cols):
    cols["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in cols)
    with connect() as con:
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*cols.values(), job_id])


def get_job(job_id):
    with connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def recent_jobs(owner, limit=30):
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE owner IS NULL OR owner=? ORDER BY created_at DESC LIMIT ?",
                           (owner, limit)).fetchall()
    return [dict(r) for r in rows]


def expired_jobs(now):
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE status!='deleted' AND expires_at<=?",
                           (now,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 動画: 分割アップロード

def add_part(job_id, part_no, size):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO upload_parts (job_id, part_no, bytes, received_at) VALUES (?,?,?,?)",
                    (job_id, part_no, size, time.time()))


def parts(job_id):
    with connect() as con:
        rows = con.execute("SELECT part_no, bytes FROM upload_parts WHERE job_id=? ORDER BY part_no",
                           (job_id,)).fetchall()
    return {r["part_no"]: r["bytes"] for r in rows}


def drop_parts(job_id):
    with connect() as con:
        con.execute("DELETE FROM upload_parts WHERE job_id=?", (job_id,))


# ---------------------------------------------------------------- 動画: キュー（worker は1件ずつ）

def next_job():
    """待っている中で一番古いもの（queued と queued_export は到着順）。"""
    with connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE kind='video' AND status IN (?,?) ORDER BY updated_at LIMIT 1",
                        QUEUED).fetchone()
    return dict(r) if r else None


def queue_before(job):
    """この job より前にいる（処理中＋先に並んだ）ジョブ。"""
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE kind='video' AND id!=? AND (status IN (?,?) OR (status IN (?,?) AND updated_at<?))",
            (job["id"], *RUNNING, *QUEUED, job["updated_at"])).fetchall()
    return [dict(r) for r in rows]


def reset_running():
    """worker 起動時: 途中で止まったジョブを並び直す。"""
    with connect() as con:
        con.execute("UPDATE jobs SET status='queued' WHERE kind='video' AND status='preparing'")
        con.execute("UPDATE jobs SET status='queued_export' WHERE kind='video' AND status='exporting'")


def done_video_stats(limit=30):
    with connect() as con:
        rows = con.execute(
            "SELECT duration_sec, mosaic_style, t_prepare_sec, t_track_sec, t_feature_sec, t_export_sec FROM jobs "
            "WHERE kind='video' AND t_prepare_sec IS NOT NULL AND duration_sec>0 ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]
