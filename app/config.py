"""環境変数の読み込み（値はログに出さない）。"""

import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
APP_PASSCODE = os.environ.get("APP_PASSCODE", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# 保存期間: 元動画・元画像・解析データ（時間）と完成品（日）。RETENTION_HOURS は旧名（互換で読む）
SOURCE_RETENTION_HOURS = float(os.environ.get("SOURCE_RETENTION_HOURS") or os.environ.get("RETENTION_HOURS") or "24")
RETENTION_HOURS = SOURCE_RETENTION_HOURS
OUTPUT_RETENTION_DAYS = float(os.environ.get("OUTPUT_RETENTION_DAYS") or "30")
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SESSION_DAYS = 30
COOKIE_NAME = "fm_session"
OWNER_COOKIE = "fm_owner"  # 端末ごとの持ち主（合言葉は共有のため）
OWNER_DAYS = 365
# 動画モード
MAX_VIDEO_SEC = float(os.environ.get("MAX_VIDEO_SEC") or "180")  # プランの「1本の長さ」の上限の上限
MAX_VIDEO_BYTES = int(1.5 * 1024 * 1024 * 1024)  # 1.5GB（4K60 の 3 分≒1.2〜1.4GB が通るよう 2026-10-30 に 1GB から変更）
PART_SIZE = 8 * 1024 * 1024
IDLE_WARN_HOURS = 1    # 選択・確認がこの時間放置されたら「あと2時間で削除」を出す
IDLE_DELETE_HOURS = 3  # 選択・確認がこの時間放置されたら元動画・解析結果を削除
UPLOAD_KEEP_HOURS = 24  # 送信途中のまま放置された分割データを消すまで

# 認証: passcode（合言葉＋端末 Cookie）| line（LINE ログイン）
AUTH_MODE_REQUESTED = (os.environ.get("AUTH_MODE") or "passcode").strip().lower()
LINE_MOCK = os.environ.get("LINE_MOCK", "") == "1"  # 開発用: userId を手入力するログイン画面（本番の設定には入れない）
LINE_LOGIN_CHANNEL_ID = os.environ.get("LINE_LOGIN_CHANNEL_ID", "")
LINE_LOGIN_CHANNEL_SECRET = os.environ.get("LINE_LOGIN_CHANNEL_SECRET", "")
LINE_MSG_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_MSG_CHANNEL_ACCESS_TOKEN", "")
LINE_MSG_CHANNEL_SECRET = os.environ.get("LINE_MSG_CHANNEL_SECRET", "")
LINE_OA_BASIC_ID = os.environ.get("LINE_OA_BASIC_ID") or "@606cuvoe"  # 公式アカウントのベーシックID（友だち追加リンク）
LINE_READY = LINE_MOCK or bool(LINE_LOGIN_CHANNEL_ID and LINE_LOGIN_CHANNEL_SECRET)
AUTH_MODE = "line" if AUTH_MODE_REQUESTED == "line" and LINE_READY else "passcode"

# Stripe（価格ID・ポータル設定IDは settings テーブル。秘密値はこの2つだけ）
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_READY = bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET)

# Discord Webhook（運営通知）
DISCORD_WEBHOOK_ALERTS = os.environ.get("DISCORD_WEBHOOK_ALERTS", "")
DISCORD_WEBHOOK_REPORTS = os.environ.get("DISCORD_WEBHOOK_REPORTS", "")
DISCORD_WEBHOOK_USAGE = os.environ.get("DISCORD_WEBHOOK_USAGE", "")
DISK_ALERT_FREE_RATIO = 0.20
WORKER_STALE_SEC = 300  # worker の心拍がこれより古ければ停止とみなす
