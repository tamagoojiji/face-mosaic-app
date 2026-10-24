"""SQLite（WAL）。jobs テーブルは DESIGN 4節のうち写真モードに要る列だけ。"""

import os
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'image',
  status TEXT NOT NULL,            -- awaiting_pick | exporting | done | failed | deleted
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
"""


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
        if "owner" not in cols:  # 旧 DB の移行
            con.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
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
        rows = con.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created_at DESC LIMIT ?",
                           (owner, limit)).fetchall()
    return [dict(r) for r in rows]


def expired_jobs(now):
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE status!='deleted' AND expires_at<=?",
                           (now,)).fetchall()
    return [dict(r) for r in rows]
