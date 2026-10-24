"""環境変数の読み込み（値はログに出さない）。"""

import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
APP_PASSCODE = os.environ.get("APP_PASSCODE", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SESSION_DAYS = 30
COOKIE_NAME = "fm_session"
OWNER_COOKIE = "fm_owner"  # 端末ごとの持ち主（合言葉は共有のため）
OWNER_DAYS = 365
# 動画モード
MAX_VIDEO_SEC = float(os.environ.get("MAX_VIDEO_SEC", "180"))
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
PART_SIZE = 8 * 1024 * 1024
IDLE_WARN_HOURS = 1    # 選択・確認がこの時間放置されたら「あと2時間で削除」を出す
IDLE_DELETE_HOURS = 3  # 選択・確認がこの時間放置されたら元動画・解析結果を削除
UPLOAD_KEEP_HOURS = 24  # 送信途中のまま放置された分割データを消すまで
