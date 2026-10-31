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
  tester_until REAL,               -- テスターの期限（期限日の翌日 0:00 JST・NULL=期限なし）
  plan TEXT NOT NULL DEFAULT 'free',          -- free | image | video_a | video_b
  hq INTEGER NOT NULL DEFAULT 0,               -- 高画質オプション（動画A/Bだけ有効）
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
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER,                -- 運営の users.id（Stripe・自動処理は NULL）
  action TEXT NOT NULL,
  target TEXT,
  detail TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  job_id TEXT,                     -- 任意: 対象の動画・写真
  category TEXT NOT NULL,          -- main.REPORT_CATEGORIES の16コード
  body TEXT,                       -- 任意（「その他」のときだけ必須）
  status TEXT NOT NULL DEFAULT 'open',   -- open | closed
  admin_note TEXT,
  created_at REAL,
  updated_at REAL,
  image_count INTEGER NOT NULL DEFAULT 0,  -- 添付のスクリーンショット（DATA_DIR/reports/<id>/<n>.jpg）
  video_name TEXT,                         -- 添付の画面録画（DATA_DIR/reports/<id>/video.<ext>・NULL=なし）
  analysis INTEGER NOT NULL DEFAULT 0      -- 対象の解析データの控え（DATA_DIR/reports/<id>/analysis/・1=あり）
);
CREATE TABLE IF NOT EXISTS line_channels (
  no INTEGER PRIMARY KEY,          -- 公式 LINE の番号（①=1・受付用は 0 推奨）
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'notify',   -- reception（受付用・返信だけ）| notify（通知用）
  basic_id TEXT,                   -- ベーシック ID（友だち追加リンク）
  access_token TEXT,               -- チャネルアクセストークン（画面・ログに出さない）
  channel_secret TEXT,             -- チャネルシークレット（同上）
  capacity INTEGER NOT NULL DEFAULT 5,
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS line_assignments (
  line_user_id TEXT PRIMARY KEY,   -- 受付・ログインで割り当てた番号（登録前の人も定員に数える）
  channel_no INTEGER NOT NULL,
  created_at REAL,
  friend INTEGER NOT NULL DEFAULT 0  -- 登録前にその番号を follow 済み（1）／unfollow（0）
);
CREATE TABLE IF NOT EXISTS billing_events (
  event_id TEXT PRIMARY KEY,       -- Stripe の event.id（Webhook の重複処理よけ）
  type TEXT,
  created_at REAL
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
    "hq_export": "INTEGER",        # 動画: 高画質（元の解像度・fps）で書き出す／書き出した 1、1080p は 0
    "out_width": "INTEGER",        # 動画: 完成品の幅・高さ・fps（ffprobe の実測）
    "out_height": "INTEGER",
    "out_fps": "REAL",
    "engine_ver": "TEXT",          # 動画: 解析した処理の版（worker.engine.video.ENGINE_VER・見込みの統計の切り分け）
    "features_state": "TEXT",      # 動画: 顔と服の特徴（選択画面を出した後に計算）pending | ready | failed
    "nsfw_info": "TEXT",           # アダルトの疑いで止めたときの判定（例: 動画 3/90 コマ max=0.93）
    "nsfw_at": "REAL",             # 止めた時刻（確認待ち nsfw_review の起点）
    "nsfw_msg": "TEXT",            # Discord の通知（チャンネルID/メッセージID）
    "nsfw_ok": "INTEGER",          # 運営が ⭕（アダルトではない）を押した → 判定を飛ばして続ける
}
# 起動時に足す列（users・③b 課金）
ADD_USER_COLUMNS = {
    "trial_ends_at": "REAL",                               # 無料（お試し）の期限＝登録＋7日
    "current_period_end": "REAL",                          # 次の更新日
    "past_due_since": "REAL",                              # 決済失敗の時刻（猶予7日の起点）
    "cancel_at_period_end": "INTEGER NOT NULL DEFAULT 0",
    "stripe_price_plan": "TEXT",                           # いまのプランの価格ID
    "pending_plan": "TEXT",                                # 次の更新日から変わるプラン（下位変更）
    "pending_hq": "INTEGER",                               # 次の更新日から変わる高画質（外す）
    "tester_until": "REAL",                               # テスターの期限（期限日の翌日 0:00 JST・NULL=期限なし）
    "line_friend": "INTEGER",                              # 公式アカウントの友だち（NULL=未確認・0/1）
}
TRIAL_DAYS = 7
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
        ucols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        if "hq" not in ucols:
            con.execute("ALTER TABLE users ADD COLUMN hq INTEGER NOT NULL DEFAULT 0")
        for name, typ in ADD_USER_COLUMNS.items():
            if name not in ucols:
                con.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
        if "launch_price" not in ucols:  # リリース記念価格（定価を入れる前からの有料利用者は記念価格のまま）
            con.execute("ALTER TABLE users ADD COLUMN launch_price INTEGER NOT NULL DEFAULT 0")
            con.execute("UPDATE users SET launch_price=1 WHERE stripe_subscription_id IS NOT NULL")
        if "line_channel_no" not in ucols:  # 公式 LINE の番号（複数運用・2026-10-30）。それまでの利用者は全員 ①
            con.execute("ALTER TABLE users ADD COLUMN line_channel_no INTEGER")
            con.execute("UPDATE users SET line_channel_no=1")
        # LINE の無料利用者にお試し期限（登録＋7日）。合言葉・運営は対象外
        con.execute("UPDATE users SET trial_ends_at=created_at+? WHERE plan='free' AND line_user_id IS NOT NULL "
                    "AND role!='admin' AND trial_ends_at IS NULL AND created_at IS NOT NULL", (TRIAL_DAYS * 86400,))
        # 旧プラン「動画B 高画質」→ 動画B＋高画質オプション
        con.execute("UPDATE users SET plan='video_b', hq=1 WHERE plan='video_b_hq'")
        # reports.body を任意に（初版は NOT NULL で作っていた）→ 作り直す
        if any(r["name"] == "body" and r["notnull"] for r in con.execute("PRAGMA table_info(reports)")):
            con.execute("ALTER TABLE reports RENAME TO reports_old")
            con.executescript(SCHEMA)
            con.execute("INSERT INTO reports (id, user_id, job_id, category, body, status, admin_note, created_at, updated_at) "
                        "SELECT id, user_id, job_id, category, body, status, admin_note, created_at, updated_at FROM reports_old")
            con.execute("DROP TABLE reports_old")
        if "image_count" not in {r["name"] for r in con.execute("PRAGMA table_info(reports)")}:
            con.execute("ALTER TABLE reports ADD COLUMN image_count INTEGER NOT NULL DEFAULT 0")
        if "video_name" not in {r["name"] for r in con.execute("PRAGMA table_info(reports)")}:
            con.execute("ALTER TABLE reports ADD COLUMN video_name TEXT")
        if "analysis" not in {r["name"] for r in con.execute("PRAGMA table_info(reports)")}:
            con.execute("ALTER TABLE reports ADD COLUMN analysis INTEGER NOT NULL DEFAULT 0")
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
    ensure_channel_from_env()


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


def jobs_with_status(status):
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE status=? ORDER BY updated_at", (status,)).fetchall()
    return [dict(r) for r in rows]


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
        # 選択画面を出した後の特徴の計算が途中で止まったもの → 特徴なしで選べるようにする
        con.execute("UPDATE jobs SET features_state='failed' WHERE kind='video' AND status='awaiting_pick' "
                    "AND features_state='pending'")


def done_video_stats(limit=30):
    with connect() as con:
        rows = con.execute(
            "SELECT duration_sec, width, height, fps, engine_ver, mosaic_style, hq_export, t_prepare_sec, t_track_sec, t_feature_sec, t_export_sec FROM jobs "
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
    assert col in ("line_user_id", "device_id", "stripe_subscription_id", "stripe_customer_id")
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


def past_due_users(before):
    """決済失敗のまま猶予（7日）を過ぎた利用者。"""
    with connect() as con:
        rows = con.execute("SELECT * FROM users WHERE status='active' AND sub_status='past_due' "
                           "AND past_due_since<?", (before,)).fetchall()
    return [dict(r) for r in rows]


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


# ---------------------------------------------------------------- 運営・課金の記録

def audit(actor_id, action, target=None, detail=None):
    with connect() as con:
        con.execute("INSERT INTO audit_log (actor_id, action, target, detail, created_at) VALUES (?,?,?,?,?)",
                    (actor_id, action, target, detail, time.time()))


def billing_event_seen(event_id):
    with connect() as con:
        return con.execute("SELECT 1 FROM billing_events WHERE event_id=?", (event_id,)).fetchone() is not None


def add_billing_event(event_id, typ):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO billing_events (event_id, type, created_at) VALUES (?,?,?)",
                    (event_id, typ, time.time()))


# ---------------------------------------------------------------- 通報

def add_report(user_id, category, body, job_id=None):
    now = time.time()
    with connect() as con:
        cur = con.execute("INSERT INTO reports (user_id, job_id, category, body, created_at, updated_at) "
                          "VALUES (?,?,?,?,?,?)", (user_id, job_id, category, body, now, now))
        return cur.lastrowid


def get_report(report_id):
    with connect() as con:
        r = con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    return dict(r) if r else None


def reports_of(user_id):
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM reports WHERE user_id=?", (user_id,))]


def old_report_images(before):
    """画像・動画・解析データの控えの保存期限（作成から90日）を過ぎた通報。"""
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM reports WHERE (image_count>0 OR video_name IS NOT NULL OR analysis=1) AND created_at<?", (before,))]


def update_report(report_id, **cols):
    cols["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in cols)
    with connect() as con:
        con.execute(f"UPDATE reports SET {sets} WHERE id=?", [*cols.values(), report_id])


# ---------------------------------------------------------------- 設定（key/value）

def get_setting(key, default=None):
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with connect() as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))


def take_signup_slot():
    """新規登録の枠を1つ使う。signup_remaining が空・非数字なら制限なしで True、0 なら False（受付停止）。"""
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key='signup_remaining'").fetchone()
        v = (r["value"] or "").strip() if r else ""
        if not v.isdigit():
            return True
        cur = con.execute("UPDATE settings SET value=CAST(CAST(value AS INTEGER)-1 AS TEXT) "
                          "WHERE key='signup_remaining' AND CAST(value AS INTEGER)>0")
        return cur.rowcount == 1


def all_settings():
    with connect() as con:
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings ORDER BY key")}


# ---------------------------------------------------------------- 公式 LINE（複数運用・2026-10-30）

CHANNEL_COLS = ("name", "kind", "basic_id", "access_token", "channel_secret", "capacity", "active")


def ensure_channel_from_env():
    """① が DB に無く env にトークンがあれば、env から no=1 として取り込む（DB に行があれば env は使わない）。"""
    if not config.LINE_MSG_CHANNEL_ACCESS_TOKEN:
        return
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO line_channels (no, name, kind, basic_id, access_token, channel_secret, created_at) "
                    "VALUES (1, 'らくらくモザイク①', 'notify', ?, ?, ?, ?)",
                    (config.LINE_OA_BASIC_ID, config.LINE_MSG_CHANNEL_ACCESS_TOKEN, config.LINE_MSG_CHANNEL_SECRET or None,
                     time.time()))


def channels(active_only=False):
    with connect() as con:
        rows = con.execute("SELECT * FROM line_channels" + (" WHERE active=1" if active_only else "") + " ORDER BY no")
        return [dict(r) for r in rows]


def channel(no):
    if no is None:
        return None
    with connect() as con:
        r = con.execute("SELECT * FROM line_channels WHERE no=?", (no,)).fetchone()
    return dict(r) if r else None


def upsert_channel(no, **cols):
    """追加・更新。access_token・channel_secret は渡されたとき（None 以外）だけ更新する。"""
    bad = set(cols) - set(CHANNEL_COLS)
    assert not bad, bad
    cols = {k: v for k, v in cols.items() if v is not None or k not in ("access_token", "channel_secret")}
    with connect() as con:
        if con.execute("SELECT 1 FROM line_channels WHERE no=?", (no,)).fetchone():
            if cols:
                con.execute(f"UPDATE line_channels SET {','.join(f'{k}=?' for k in cols)} WHERE no=?",
                            [*cols.values(), no])
        else:
            cols.update(no=no, created_at=time.time())
            con.execute(f"INSERT INTO line_channels ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        list(cols.values()))


def _used(con, no):
    return con.execute(
        "SELECT (SELECT COUNT(*) FROM users WHERE status!='withdrawn' AND line_channel_no=?) + "
        "(SELECT COUNT(*) FROM line_assignments a WHERE a.channel_no=? AND NOT EXISTS "
        "(SELECT 1 FROM users u WHERE u.line_user_id=a.line_user_id))", (no, no)).fetchone()[0]


def channel_used(no):
    """定員に数える人数: その番号の利用者（退会除く）＋未登録の割り当て。"""
    with connect() as con:
        return _used(con, no)


def _pick(con):
    for r in con.execute("SELECT no, capacity FROM line_channels WHERE active=1 AND kind='notify' ORDER BY no").fetchall():
        if _used(con, r["no"]) < r["capacity"]:
            return r["no"]
    return None


def pick_channel():
    """空きのある最小番号（active・通知用）。全部満員なら None。"""
    with connect() as con:
        return _pick(con)


def assign(line_user_id):
    """その人の番号。利用者の番号か既存の割り当てがあればそれ、無ければ空きの最小番号を割り当てて記録（満員なら None）。"""
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT line_channel_no FROM users WHERE line_user_id=?", (line_user_id,)).fetchone()
        if r and r["line_channel_no"] is not None:
            return r["line_channel_no"]
        r = con.execute("SELECT channel_no FROM line_assignments WHERE line_user_id=?", (line_user_id,)).fetchone()
        if r:
            return r["channel_no"]
        no = _pick(con)
        if no is not None:
            con.execute("INSERT INTO line_assignments (line_user_id, channel_no, created_at) VALUES (?,?,?)",
                        (line_user_id, no, time.time()))
        return no


def assignment(line_user_id):
    """割り当て行（channel_no・friend）。無ければ None（記録はしない）。"""
    with connect() as con:
        r = con.execute("SELECT * FROM line_assignments WHERE line_user_id=?", (line_user_id,)).fetchone()
    return dict(r) if r else None


def set_assignment_friend(line_user_id, no, on):
    """登録前の人の follow/unfollow を記録（行が無ければ no で作る・あれば channel_no はそのまま）。"""
    with connect() as con:
        con.execute("INSERT INTO line_assignments (line_user_id, channel_no, created_at, friend) VALUES (?,?,?,?) "
                    "ON CONFLICT(line_user_id) DO UPDATE SET friend=excluded.friend",
                    (line_user_id, no, time.time(), 1 if on else 0))


def reception_channel():
    """受付用（active な reception の最小番号）。"""
    with connect() as con:
        r = con.execute("SELECT * FROM line_channels WHERE active=1 AND kind='reception' ORDER BY no LIMIT 1").fetchone()
    return dict(r) if r else None
