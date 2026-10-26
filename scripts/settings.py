"""運営の設定（settings テーブル）を変える CLI。管理画面（③b）ができるまでの代わり。

  python scripts/settings.py list
  python scripts/settings.py get campaign_on
  python scripts/settings.py set campaign_on 1
  python scripts/settings.py set campaign_from 2026-11-01   # 空にするときは ""

VPS では: docker exec face-mosaic-app python scripts/settings.py list
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.services import quota  # noqa: E402

KEYS = {
    "campaign_on": "リリース記念 1=ON / 0=OFF",
    "campaign_from": "リリース記念の開始日 YYYY-MM-DD（空=制限なし）",
    "campaign_to": "リリース記念の終了日 YYYY-MM-DD（その日を含む・空=制限なし）",
    "campaign_images": "リリース記念中の無料プランの画像枚数/月",
    "campaign_videos": "リリース記念中の無料プランの1分動画の本数/月",
    "source_retention_hours": "元動画・元画像・解析データを消すまでの時間（既定は環境変数 SOURCE_RETENTION_HOURS）",
    "output_retention_days": "完成品の保存日数（既定は環境変数 OUTPUT_RETENTION_DAYS）",
    "idle_warn_hours": "選択・確認の放置で再通知するまでの時間",
    "idle_delete_hours": "選択・確認の放置で元動画・解析結果を削除するまでの時間",
    "mosaic_default": "モザイクの標準 block（ブロック粗め）| glass（すりガラス）",
}


def main(argv):
    db.init()
    if len(argv) >= 1 and argv[0] == "list":
        saved = db.all_settings()
        for k, desc in KEYS.items():
            print(f"{k}={quota.setting(k)}{'' if k in saved else '（既定）'}  # {desc}")
        print(f"→ いまリリース記念は {'有効' if quota.campaign_active() else '無効'}")
        return 0
    if len(argv) == 2 and argv[0] == "get":
        print(quota.setting(argv[1]) if argv[1] in KEYS else db.get_setting(argv[1], ""))
        return 0
    if len(argv) == 3 and argv[0] == "set":
        if argv[1] not in KEYS:
            print(f"知らないキーです: {argv[1]}（使えるキー: {', '.join(KEYS)}）", file=sys.stderr)
            return 2
        db.set_setting(argv[1], argv[2].strip())
        print(f"{argv[1]}={argv[2].strip()}")
        print(f"→ いまリリース記念は {'有効' if quota.campaign_active() else '無効'}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
