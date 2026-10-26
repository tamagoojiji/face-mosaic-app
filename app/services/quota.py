"""プランと月の上限（DESIGN 2節 料金プラン表）。リリース記念は settings の campaign_* で切り替える。"""

import datetime
import time

from .. import config, db

# None = 無制限。video_sec_month=0 かつ video_count_month=0 は「動画なし」
PLANS = {
    "free": {"label": "無料", "price": 0, "video_sec_month": 0, "video_count_month": 0, "video_len": 60,
             "images": 5, "quality": "1080p・30fps"},
    "image": {"label": "画像のみ", "price": 500, "video_sec_month": 0, "video_count_month": 0, "video_len": 0,
              "images": 100, "quality": "画像は長辺4000pxまで"},
    "video_a": {"label": "動画A", "price": 980, "video_sec_month": 600, "video_count_month": None, "video_len": 60,
                "images": 100, "quality": "1080p・30fps"},
    "video_b": {"label": "動画B", "price": 1480, "video_sec_month": 5400, "video_count_month": None, "video_len": 180,
                "images": None, "quality": "1080p・30fps"},
}
PLAN_ORDER = ("free", "image", "video_a", "video_b")
PAID = ("image", "video_a", "video_b")
# 高画質オプション（月＋500円・動画A/Bだけ）。解析は 1080p・30fps のまま、書き出しだけ元の解像度・fps（HDR は SDR に変換）
HQ_OPTION = {"label": "高画質オプション", "price": 500, "plans": ("video_a", "video_b"),
             "quality": "元の解像度と fps で書き出し", "note": "HDR は通常の色（SDR）に変換して書き出し"}
SETTING_DEFAULTS = {"campaign_on": "0", "campaign_from": "", "campaign_to": "",
                    "campaign_images": "10", "campaign_videos": "3"}


def setting(key):
    return db.get_setting(key, SETTING_DEFAULTS.get(key))


def month_of(ts=None):
    return time.strftime("%Y-%m", time.localtime(ts if ts is not None else time.time()))


def campaign_active(today=None):
    if setting("campaign_on") != "1":
        return False
    today = today or datetime.date.today().isoformat()
    f, t = (setting("campaign_from") or "").strip(), (setting("campaign_to") or "").strip()
    return (not f or today >= f) and (not t or today <= t)


def fmt_sec(sec):
    sec = int(round(max(0, sec)))
    if sec < 60:
        return f"{sec}秒"
    return f"{sec // 60}分{sec % 60}秒" if sec % 60 else f"{sec // 60}分"


def limits(user):
    plan = user.get("plan") if user.get("plan") in PLANS else "free"
    p = dict(PLANS[plan], plan=plan, campaign=False, hq=bool(user.get("hq")) and plan in HQ_OPTION["plans"])
    if plan == "free" and campaign_active():
        p.update(campaign=True, images=int(setting("campaign_images") or 10),
                 video_count_month=int(setting("campaign_videos") or 3))
    p["video_allowed"] = bool(p["video_len"]) and (p["video_sec_month"] != 0 or p["video_count_month"] != 0)
    p["video_len"] = min(p["video_len"], config.MAX_VIDEO_SEC) if p["video_allowed"] else 0
    return p


def status(user):
    """プラン・今月の使用量・残り（画面と API 用）。"""
    lim = limits(user)
    u = db.get_usage(user["id"], month_of())
    rem = {"images": None if lim["images"] is None else max(0, lim["images"] - u["image_count"]),
           "video_sec": None, "video_count": None}
    if lim["video_allowed"]:
        if lim["video_sec_month"]:
            rem["video_sec"] = max(0.0, lim["video_sec_month"] - u["video_seconds_used"])
        if lim["video_count_month"]:
            rem["video_count"] = max(0, lim["video_count_month"] - u["video_count"])
    return {"limits": lim, "usage": u, "remaining": rem}


BLOCKED_MSG = {"trial_expired": "お試し期間が終わりました。プランを選ぶと続けて使えます",
               "past_due": "お支払いの確認ができていません。支払い方法を更新してください"}


def blocked(user, now=None):
    """利用停止の理由: "trial_expired"（お試し期限切れ）| "past_due"（決済失敗の猶予中）| None。"""
    if user.get("role") == "admin":
        return None
    if user.get("sub_status") == "past_due":
        return "past_due"
    t = user.get("trial_ends_at")
    if (user.get("plan") or "free") == "free" and user.get("line_user_id") and t is not None \
            and t < (now or time.time()):
        return "trial_expired"
    return None


def check_video(user, duration):
    """送ってよければ None、だめなら理由の文。"""
    b = blocked(user)
    if b:
        return BLOCKED_MSG[b]
    st = status(user)
    lim, rem = st["limits"], st["remaining"]
    if not lim["video_allowed"]:
        if lim["plan"] == "image":
            return "画像のみプランでは動画は送れません"
        return f"無料プランでは動画は送れません（写真は月{lim['images']}枚まで）"
    if duration > lim["video_len"] + 0.5:
        return f"この動画は {fmt_sec(duration)} です。{lim['label']}プランは1本{fmt_sec(lim['video_len'])}までです"
    if rem["video_count"] is not None and rem["video_count"] <= 0:
        return f"今月の動画は {lim['video_count_month']} 本まで送れます。今月の分はもう使いきりました"
    if rem["video_sec"] is not None and duration > rem["video_sec"] + 0.5:
        return f"今月の動画の残りは {fmt_sec(rem['video_sec'])} です（この動画は {fmt_sec(duration)}）"
    return None


def check_image(user):
    b = blocked(user)
    if b:
        return BLOCKED_MSG[b]
    st = status(user)
    if st["remaining"]["images"] is not None and st["remaining"]["images"] <= 0:
        return f"今月の写真は {st['limits']['images']} 枚まで送れます。今月の分はもう使いきりました"
    return None


def count_image(user_id):
    db.add_usage(user_id, month_of(), images=1)


def count_video(job, seconds):
    """受け取り完了時に数える（job.usage_sec に記録して、失敗・放置削除で戻せるようにする）。"""
    if not job.get("user_id"):
        return
    seconds = round(seconds, 2)
    db.add_usage(job["user_id"], month_of(job["created_at"]), seconds=seconds, videos=1)
    db.update_job(job["id"], usage_sec=seconds)


def refund_video(job):
    """処理できなかった・放置で削除した動画の分を戻す（1回だけ）。"""
    sec = job.get("usage_sec") or 0
    if not job.get("user_id") or sec <= 0:
        return False
    db.add_usage(job["user_id"], month_of(job["created_at"]), seconds=-sec, videos=-1)
    db.update_job(job["id"], usage_sec=0)
    return True
