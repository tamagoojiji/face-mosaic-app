"""アダルトの疑い（nsfw.Blocked）を運営が確認する。

止めたら status=nsfw_review にして元データを残し、Discord #mosaic-alerts に管理画面のリンクと ⭕❌ のスタンプを置く。
⭕ = アダルトではない → 処理を続ける ／ ❌ = 削除する。HOLD_HOURS 押されなければ削除。
画像は Discord に送らない（管理画面 /admin/review/<job> で見る）。スタンプはボット（DISCORD_BOT_TOKEN）で付けて読む。
ボットが未設定・Discord に送れないときは確認せずに削除する（呼び出し側）。
"""

import json
import logging
import os
import shutil
import time
import urllib.parse
import urllib.request

from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import config, db
from . import discord, notify, quota

log = logging.getLogger("uvicorn.error")
API = "https://discord.com/api/v10"
OK, NG = "⭕", "❌"
HOLD_HOURS = 24
WAIT_MSG = "内容を運営が確認しています。終わったら LINE でお知らせします（24時間以内）"
_ME = []  # ボット自身のユーザーID（スタンプの数から除く）


def _signer():
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="fm-review")


def link_key(job_id):
    """確認ページを運営がログインせずに開くための鍵（Discord のアプリ内ブラウザはログインを引き継がないため）。"""
    return _signer().dumps(job_id)


def key_ok(job_id, key):
    """鍵がこのジョブのもので、期限（HOLD_HOURS+1 時間）内か。"""
    if not key or not config.SESSION_SECRET:
        return False
    try:
        return _signer().loads(key, max_age=(HOLD_HOURS + 1) * 3600) == job_id
    except BadSignature:
        return False


def enabled():
    return bool(config.DISCORD_BOT_TOKEN and config.DISCORD_WEBHOOK_ALERTS)


def job_dir(job_id):
    return os.path.join(config.DATA_DIR, "jobs", job_id)


def frames_dir(job_id):
    return os.path.join(job_dir(job_id), "nsfw")


def _bot(method, path):
    req = urllib.request.Request(API + path, method=method, data=b"" if method == "PUT" else None, headers={
        "Authorization": "Bot " + config.DISCORD_BOT_TOKEN,
        "User-Agent": "DiscordBot (https://mosaic.tamago-ai-world.com, 1.0)"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _reaction_path(msg, emoji):
    cid, mid = msg.split("/")
    return f"/channels/{cid}/messages/{mid}/reactions/{urllib.parse.quote(emoji)}"


def _users(msg, emoji):
    """そのスタンプを押した人（ボット自身を除く）。"""
    if not _ME:
        _ME.append(_bot("GET", "/users/@me")["id"])
    return {u["id"] for u in _bot("GET", _reaction_path(msg, emoji)) or []} - set(_ME)


def _save_frames(job_id, frames):
    import cv2
    d = frames_dir(job_id)
    os.makedirs(d, exist_ok=True)
    for n, bgr in enumerate(frames, 1):
        h, w = bgr.shape[:2]
        s = min(1.0, 720 / float(max(h, w)))
        im = bgr if s >= 1.0 else cv2.resize(bgr, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(d, f"{n}.jpg"), im, [cv2.IMWRITE_JPEG_QUALITY, 85])


def hold(j, exc):
    """処理を止めて確認待ちにする。Discord に置けたら True（False なら呼び出し側が削除する）。"""
    if not enabled():
        return False
    _save_frames(j["id"], exc.frames)
    kind = "動画" if j["kind"] == "video" else "写真"
    text = (f"🔞 アダルトの疑いで止めました（{kind}） job={j['id']} 利用者=#{j.get('user_id')}\n判定: {exc}\n"
            f"画像: {config.APP_BASE_URL}/admin/review/{j['id']}?k={link_key(j['id'])}\n"
            f"{OK} アダルトではない（処理を続ける）／{NG} 削除する。{HOLD_HOURS}時間押されなければ削除します")
    try:
        m = discord.post_wait("alerts", text)
        msg = f"{m['channel_id']}/{m['id']}"
        for e in (OK, NG):
            _bot("PUT", _reaction_path(msg, e) + "/@me")
            time.sleep(0.5)  # スタンプの連続付与は制限がかかりやすい
    except Exception as e:
        log.warning("review: Discord に置けませんでした job=%s: %s", j["id"], type(e).__name__)
        return False
    # 確認待ちにするのは Discord に置けてから（poll が通知の無い行を拾わないように）
    now = time.time()
    db.update_job(j["id"], status="nsfw_review", nsfw_info=str(exc), nsfw_at=now, nsfw_msg=msg, error=None,
                  expires_at=now + (HOLD_HOURS + 1) * 3600)  # 期限の削除は poll が先にする（retention は保険）
    log.info("review: 確認待ち job=%s %s", j["id"], exc)
    return True


def _close(j, result):
    """Discord の通知に結果を書き足す（失敗しても決定は変えない）。"""
    shutil.rmtree(frames_dir(j["id"]), ignore_errors=True)
    if not j.get("nsfw_msg"):
        return
    cid, mid = j["nsfw_msg"].split("/")
    try:
        old = _bot("GET", f"/channels/{cid}/messages/{mid}")["content"]
        discord.edit("alerts", mid, old + "\n→ " + result)
    except Exception as e:
        log.warning("review: 結果を書き足せませんでした job=%s: %s", j["id"], type(e).__name__)


def approve(j):
    """⭕: 判定を飛ばして続ける。動画は解析の列に戻す。写真はここで読み込んで選択待ちにする。"""
    if j["kind"] == "video":
        db.update_job(j["id"], status="queued", nsfw_ok=1, expires_at=None)
    else:
        from worker.engine import photo
        d = job_dir(j["id"])
        raw, src = os.path.join(d, "hold.bin"), os.path.join(d, "src.jpg")
        r = photo.prepare(raw, src, os.path.join(d, "view.jpg"), check=False)
        os.remove(raw)
        with open(os.path.join(d, "faces.json"), "w", encoding="utf-8") as f:
            json.dump(r["faces"], f)
        db.update_job(j["id"], status="awaiting_pick", nsfw_ok=1, src_path=src, width=r["width"], height=r["height"],
                      expires_at=time.time() + quota.source_hours() * 3600)
        quota.count_image(j["user_id"])
        notify.push("ready", db.get_job(j["id"]))
    log.info("review: ⭕ 続行 job=%s", j["id"])
    _close(j, f"{OK} 続行しました")


def reject(j, why):
    """❌ または期限切れ: 削除して利用者に知らせる。"""
    from worker.engine import nsfw
    if j["kind"] == "video":
        from worker import run
        run.fail_prepare(j["id"], nsfw.BLOCK_MSG, nsfw.Blocked(why))
    else:
        shutil.rmtree(job_dir(j["id"]), ignore_errors=True)
        db.update_job(j["id"], status="failed", error=nsfw.BLOCK_MSG, src_path=None)
        notify.push("failed", db.get_job(j["id"]), reason=nsfw.BLOCK_MSG)
    log.info("review: 削除 job=%s %s", j["id"], why)
    _close(j, f"{NG} 削除しました（{why}）")


def poll(now=None):
    """確認待ちのスタンプを読んで決める（app の定期処理から呼ぶ）。❌ が押されていれば ⭕ より優先。"""
    if not enabled():
        return
    now = now or time.time()
    for j in db.jobs_with_status("nsfw_review"):
        try:
            if _users(j["nsfw_msg"], NG):
                reject(j, f"{NG} が押されました")
            elif _users(j["nsfw_msg"], OK):
                approve(j)
            elif now - (j["nsfw_at"] or now) >= HOLD_HOURS * 3600:
                reject(j, f"{HOLD_HOURS}時間たちました")
        except Exception as e:  # 1件の失敗で他を止めない（次の回にもう一度）
            log.warning("review: 確認の処理に失敗 job=%s: %s", j["id"], type(e).__name__)
