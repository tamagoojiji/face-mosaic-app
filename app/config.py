"""環境変数の読み込み（値はログに出さない）。"""

import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
APP_PASSCODE = os.environ.get("APP_PASSCODE", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "24"))
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SESSION_DAYS = 30
COOKIE_NAME = "fm_session"
