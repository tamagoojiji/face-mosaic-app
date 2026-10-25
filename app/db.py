"""SQLite（WAL）。jobs・users・usage・settings（DESIGN 4節のうち試験版③aまでに要る列）。"""

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
  expires_at REAL,                 -- 完成前の期限（送信途中・選択の放置・失敗の後始末）
  created_at REAL,
  updated_at REAL,
  owner TEXT                       -- 旧: 端末ごとの owner_id（user_id へ移行済み・列は残す）
);
CREATE TABLE IF NOT EXISTS upload_parts (
  job_id TEXT NOT NULL,
  part_no INTEGER NOT NULL,
  bytes INTEGER,
  received_at REAL,
  PRIMARY KEY (job_id, part_no)
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_user_id TEXT UNIQUE,        -- line モード
  device_id TEXT UNIQUE,           -- passcode モード（端末 Cookie の owner_id）
  display_name TEXT,
  instagram_username TEXT,         -- 任意・自己申告
  role TEXT NOT NULL DEFAULT 'user',
  plan TEXT NOT NULL DEFAULT 'free',
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  sub_status TEXT,
  notify INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',   -- active | stopped | withdrawn
  ig_asked_at REAL,                -- 初回の Instagram 名の画面を通った（入力 or あとで）
  agreed_at REAL,                  -- 利用規約・プライバシーに同意
  withdraw_reason TEXT,
  withdraw_note TEXT,
  withdrawn_at REAL,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS usage (
  user_id INTEGER NOT NULL,
  month TEXT NOT NULL,             -- YYYY-MM
  video_seconds_used REAL NOT NULL DEFAULT 0,
  video_count INTEGER NOT NULL DEFAULT 0,
  image_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, month)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

# 起動時に足す列（動画モード・DESIGN 4節・③a）
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
    "user_id": "INTEGER",
    "src_expires_at": "REAL",      # 完成後: 元動画・元画像・解析データを消す時刻（消したら NULL）
    "out_expires_at": "REAL",      # 完成後: 完成品を消す時刻
    "usage_sec": "REAL",           # 動画: 今月の上限に数えた秒（戻したら 0）
    "idle_notified": "INTEGER",    # 放置の再通知（④）を送った
    "queued_at": "REAL",           # 動画: 受け取り完了（解析の順番待ちに入った）
    "prep_started_at": "REAL",     # 動画: 解析を始めた
}
QUEUED = ("queued", "queued_export")
PASSCODE_PLAN = "video_b"  # 試験版の合言葉利用者は動画B相当（試験中だけ・DESIGN 7節）
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
        con.execute("CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at)")
        now = time.time()
        # 旧 jobs.owner → users（端末ごとに1人・ゲスト・試験中は動画B相当）→ jobs.user_id
        for r in con.execute("SELECT DISTINCT owner FROM jobs WHERE owner IS NOT NULL AND user_id IS NULL").fetchall():
            con.execute("INSERT OR IGNORE INTO users (device_id, display_name, plan, created_at) VALUES (?, 'ゲスト', ?, ?)",
                        (r["owner"], PASSCODE_PLAN, now))
            uid = con.execute("SELECT id FROM users WHERE device_id=?", (r["owner"],)).fetchone()["id"]
            con.execute("UPDATE jobs SET user_id=? WHERE owner=? AND user_id IS NULL", (uid, r["owner"]))
        # 旧「完成から一律 n 時間」の行 → 元データと完成品の期限を分ける
        con.execute("UPDATE jobs SET src_expires_at=expires_at, out_expires_at=expires_at+?, expires_at=NULL "
                    "WHERE status='done' AND out_expires_at IS NULL AND expires_at IS NOT NULL",
                    (config.OUTPUT_RETENTION_DAYS * 86400 - config.SOURCE_RETENTION_HOURS * 3600,))


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


def recent_jobs(user_id, limit=30, include_legacy=False):
    """include_legacy: 持ち主が記録される前の旧行（user_id NULL）も出す（passcode モードのみ）。"""
    q = "SELECT * FROM jobs WHERE user_id=?" + (" OR user_id IS NULL" if include_legacy else "")
    with connect() as con:
        rows = con.execute(q + " ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def user_jobs(user_id):
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE user_id=?", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def delete_jobs_of(user_id):
    with connect() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM jobs WHERE user_id=?", (user_id,))]
        con.executemany("DELETE FROM upload_parts WHERE job_id=?", [(i,) for i in ids])
        con.execute("DELETE FROM jobs WHERE user_id=?", (user_id,))
    return len(ids)


def expired_jobs(now):
    """完成前の期限切れ（送信途中・選択の放置・失敗の後始末）。完成済みと書き出し中は対象外。"""
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE status NOT IN ('deleted','done','queued_export','exporting') "
                           "AND expires_at<=?", (now,)).fetchall()
    return [dict(r) for r in rows]


def src_expired_jobs(now):
    """完成済みで、元動画・元画像・解析データの期限が来たもの。"""
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE status='done' AND src_expires_at<=?", (now,)).fetchall()
    return [dict(r) for r in rows]


def out_expired_jobs(now):
    """完成品の保存期限が来たもの（書き出し直しの途中は終わってから）。"""
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE status NOT IN ('deleted','queued_export','exporting') "
                           "AND out_expires_at<=?", (now,)).fetchall()
    return [dict(r) for r in rows]


def idle_jobs(warn_before):
    """選択・確認の放置で、まだ再通知（④）を送っていないもの。"""
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE kind='video' AND status IN ('awaiting_pick','awaiting_review') "
                           "AND expires_at<=? AND COALESCE(idle_notified,0)=0", (warn_before,)).fetchall()
    return [dict(r) for r in rows]


def jobs_created_between(t0, t1):
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE created_at>=? AND created_at<?", (t0, t1)).fetchall()
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


# ---------------------------------------------------------------- 利用者

def get_user(user_id):
    if user_id is None:
        return None
    with connect() as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def user_by(col, value):
    assert col in ("line_user_id", "device_id")
    with connect() as con:
        r = con.execute(f"SELECT * FROM users WHERE {col}=?", (value,)).fetchone()
    return dict(r) if r else None


def create_user(**cols):
    cols.setdefault("created_at", time.time())
    keys = ",".join(cols)
    with connect() as con:
        cur = con.execute(f"INSERT INTO users ({keys}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
        uid = cur.lastrowid
    return get_user(uid)


def update_user(user_id, **cols):
    sets = ",".join(f"{k}=?" for k in cols)
    with connect() as con:
        con.execute(f"UPDATE users SET {sets} WHERE id=?", [*cols.values(), user_id])


def count_users_created_between(t0, t1):
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM users WHERE created_at>=? AND created_at<?", (t0, t1)).fetchone()[0]


# ---------------------------------------------------------------- 月次カウンタ

def get_usage(user_id, month):
    with connect() as con:
        r = con.execute("SELECT * FROM usage WHERE user_id=? AND month=?", (user_id, month)).fetchone()
    return dict(r) if r else {"user_id": user_id, "month": month, "video_seconds_used": 0, "video_count": 0,
                              "image_count": 0}


def add_usage(user_id, month, seconds=0.0, videos=0, images=0):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO usage (user_id, month) VALUES (?,?)", (user_id, month))
        con.execute("UPDATE usage SET video_seconds_used=MAX(0, video_seconds_used+?), video_count=MAX(0, video_count+?), "
                    "image_count=MAX(0, image_count+?) WHERE user_id=? AND month=?",
                    (seconds, videos, images, user_id, month))


# ---------------------------------------------------------------- 設定（key/value）

def get_setting(key, default=None):
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with connect() as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))


def all_settings():
    with connect() as con:
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings ORDER BY key")}
