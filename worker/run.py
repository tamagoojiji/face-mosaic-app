"""動画のキューを1件ずつ処理する worker（app とは別プロセス・同じ /data を使う）。

queued        → preparing（1080p/30fps 変換・HDR→SDR → 顔の追跡 → 場面写真）→ awaiting_pick → 顔と服の特徴（選択中に計算）
queued_export → exporting（書き出し）→ done
失敗は failed＋error（利用者向けの文）。元動画などのファイルは消す。
LINE 通知（①選択の準備 ②できました ③処理できなかった）と、失敗時の Discord #mosaic-alerts もここから送る。
"""

import json
import logging
import os
import threading
import time
import traceback

from app import config, db
from app.services import discord, monitor, notify, quota, review
from worker.engine import nsfw, video

log = logging.getLogger("worker")


def job_dir(job_id):
    return os.path.join(config.DATA_DIR, "jobs", job_id)


def paths(job_id):
    d = job_dir(job_id)
    return {"dir": d, "src": os.path.join(d, "src.mp4"), "v1080": os.path.join(d, "work_1080.mp4"),
            "tracks": os.path.join(d, "tracks.json"), "feats": os.path.join(d, "features.npz"),
            "shots": os.path.join(d, "shots"), "shots_json": os.path.join(d, "shots.json"),
            "thumbs": os.path.join(d, "thumbs"), "out": os.path.join(d, "out.mp4")}


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def _failed(job_id, stage, msg, exc):
    """失敗の後始末の共通部分: 消費分を戻す → LINE ③ → Discord #mosaic-alerts。"""
    j = db.get_job(job_id)
    if not j:
        return
    j["_refunded"] = quota.refund_video(j)
    notify.push("failed", j, reason=msg)
    discord.alert(f"処理失敗 ジョブ={job_id} 工程={stage} 利用者=#{j.get('user_id')}\n理由: {msg}\n"
                  f"{discord.explain(exc)}")


def prepare_fail_msg(e):
    """解析の失敗で利用者に出す理由。動画そのものが読めないときだけ「撮り直し」を案内し、
    それ以外（アプリの不具合）は同じ動画で送り直してもらう（2026-10-31: 不具合なのに撮り直しを案内していた）"""
    if isinstance(e, nsfw.Blocked):
        return nsfw.BLOCK_MSG
    if "HDR(PQ)" in str(e):
        return "この動画の HDR 形式（HDR10・Dolby Vision の一部）にはまだ対応していません。カメラの設定で HDR ビデオをオフにして撮った動画を送ってください"
    if any(k in str(e) for k in ("ffprobe に失敗", "映像ストリームがありません")):
        return "動画を読み込めませんでした。iPhone のカメラで撮った動画を送ってください"
    return "こちらの不具合で処理できませんでした。運営が調べて直しますので、少し時間をおいて同じ動画をもう一度送ってください"


def fail_prepare(job_id, msg, exc):
    """解析の失敗。続きに使えるものは無いので、元動画と途中のファイルは消す（フォルダと行は期限まで残す）。"""
    log.error("job %s 解析に失敗: %s\n%s", job_id, exc, traceback.format_exc())
    p = paths(job_id)
    for k in ("src", "v1080", "feats", "tracks"):
        _rm(p[k])
    _rm(p["v1080"] + ".part.mp4")
    db.update_job(job_id, status="failed", error=msg, src_path=None, out_path=None,
                  expires_at=time.time() + quota.source_hours() * 3600)
    _failed(job_id, "解析", msg, exc)


def fail_export(job_id, exc):
    """書き出しの失敗。フォルダは消さない（1080p 版・追跡結果・切り抜きは選び直しに使う）。

    前の完成動画があれば done に戻して理由を error に残す（前の動画はそのまま保存できる）。
    初回で完成動画が無ければ failed。どちらも期限が来たら retention が消す。
    """
    log.error("job %s 書き出しに失敗: %s\n%s", job_id, exc, traceback.format_exc())
    p = paths(job_id)
    _rm(os.path.join(p["dir"], "out.tmp.mp4"))
    if os.path.exists(p["out"]):  # 期限（元データ・完成品）は前の完成のまま
        db.update_job(job_id, status="done", out_path=p["out"], error="処理中にエラーが起きました", expires_at=None)
        j = db.get_job(job_id)
        notify.push("failed", j, reason="選び直しの書き出しに失敗しました。前の動画はそのまま保存できます")
        discord.alert(f"処理失敗 ジョブ={job_id} 工程=書き出し（選び直し） 利用者=#{j.get('user_id')}\n"
                      f"{discord.explain(exc)}")
    else:
        msg = "書き出しに失敗しました。お手数ですが、もう一度送ってください"
        db.update_job(job_id, status="failed", out_path=None, error=msg,
                      expires_at=time.time() + quota.source_hours() * 3600)
        _failed(job_id, "書き出し", msg, exc)


def do_prepare(j):
    jid, p = j["id"], paths(j["id"])
    db.update_job(jid, status="preparing", error=None, fast_mode=1, prep_started_at=time.time(), engine_ver=video.ENGINE_VER,
                  t_prepare_sec=None, t_track_sec=None, t_feature_sec=None, features_state=None)
    t0 = time.time()
    info0, vf0, hdr = video.prepare(p["src"], p["dir"])
    db.update_job(jid, fps=info0["fps"], hdr=1 if hdr.get("mode") == "hlg" else 0,
                  width=info0["out_width"], height=info0["out_height"])
    video.to_1080(p["src"], vf0, p["v1080"])
    # 性的な内容の疑いなら運営の確認待ちにして止める（Discord に置けなければ main で削除）。
    # 運営が ⭕ を押したもの（nsfw_ok）は判定しない。判定の時間は変換の時間に含める
    if not j.get("nsfw_ok"):
        try:
            r = nsfw.check_video(p["v1080"])
        except nsfw.Blocked as e:
            if review.hold(db.get_job(jid), e):
                return
            raise
        log.info("nsfw %s コマ %d 該当 %d 最大 %.3f", jid, r["frames"], r["hits"], r["max"])
    t1 = time.time()
    db.update_job(jid, t_prepare_sec=round(t1 - t0, 1))
    info, vf, _ = video.prepare(p["v1080"], p["dir"])
    tracks, dw, dh, seen = video.get_tracks(p["v1080"], info, vf, p["tracks"], fast=True)
    t2 = time.time()
    db.update_job(jid, t_track_sec=round(t2 - t1, 1))
    shots = video.pick_shots(tracks, dw, dh, seen)
    items = video.build_shots(p["v1080"], info, vf, shots, dw, p["shots"])
    n_thumbs = video.make_thumbs(p["v1080"], info, vf, tracks, dw, p["thumbs"])
    with open(p["shots_json"], "w", encoding="utf-8") as f:
        json.dump(items, f)
    t3 = time.time()
    db.update_job(jid, status="awaiting_pick", expires_at=time.time() + quota.idle_delete_hours() * 3600,
                  idle_notified=0, features_state="pending")
    notify.push("ready", db.get_job(jid))
    log.info("prepare %s %.0fs  %dx%d %.2ffps hdr=%s  変換 %.1fs 追跡 %.1fs 場面写真・切り抜き %.1fs  "
             "トラック %d 場面 %d 切り抜き %d", jid, info0["duration"], info0["out_width"], info0["out_height"],
             info0["fps"], hdr.get("mode"), t1 - t0, t2 - t1, t3 - t2, len(tracks), len(items), n_thumbs)
    # 顔と服の特徴は選択画面を出した後に計算する（選んで「確認へ進む」を押したときに使う）。
    # 失敗しても選択はできる（特徴なしで判定する）ので failed にはしない
    try:
        video.compute_features(p["v1080"], info, vf, tracks, dw, p["feats"])
    except Exception as e:
        log.error("job %s 特徴の計算に失敗: %s\n%s", jid, e, traceback.format_exc())
        db.update_job(jid, features_state="failed")
        discord.alert(f"処理失敗 ジョブ={jid} 工程=特徴（選択は特徴なしで続行）利用者=#{j.get('user_id')}\n"
                      f"{discord.explain(e)}")
        return
    t4 = time.time()
    db.update_job(jid, t_feature_sec=round(t4 - t3, 1), features_state="ready")
    log.info("features %s 特徴 %.1fs", jid, t4 - t3)


def do_export(j):
    jid, p = j["id"], paths(j["id"])
    db.update_job(jid, status="exporting", error=None)
    t0 = time.time()
    keep = set(json.loads(j["keep"] or "[]"))
    blur = set(json.loads(j["blur"] or "[]"))
    style = "blur" if j["mosaic_style"] == "blur" else "pixel"
    # 高画質オプション: 確認画面で決めた hq_export=1 かつ元動画が残っていれば、元の解像度・fps で書き出す
    hq = bool(j.get("hq_export")) and os.path.exists(p["src"])
    if hq:
        r = video.export_hq(p["src"], p["v1080"], p["dir"], p["tracks"], keep, blur, style, p["out"])
    else:
        r = video.export(p["v1080"], p["dir"], p["tracks"], keep, blur, style, p["out"])
    sec = round(time.time() - t0, 1)
    now = time.time()
    o = video.probe(p["out"])
    db.update_job(jid, status="done", out_path=p["out"], t_export_sec=sec, expires_at=None, hq_export=1 if hq else 0,
                  out_width=o["out_width"], out_height=o["out_height"], out_fps=round(o["fps"], 3),
                  src_expires_at=j["src_expires_at"] or now + quota.source_hours() * 3600,
                  out_expires_at=now + quota.output_days() * 86400)
    # 完成後に要らないもの: 特徴。選び直し（確認画面から書き出し直す）のために 1080p 版・追跡結果・切り抜きは残す。
    # 元動画は 1080p で書き出したときは消す。高画質で書き出したときは選び直しを高画質でできるよう期限（24時間）まで残す
    _rm(p["feats"])
    if not hq:
        _rm(p["src"])
        db.update_job(jid, src_path=None)
    log.info("export %s %s %s %.1fs  keep %d blur %d  コマ %d（モザイクあり %d）%s  %dx%d %.2ffps", jid, style,
             "高画質" if hq else "1080p", sec, len(keep), len(blur), r["frames_written"], r["frames_blurred"],
             r["encoder"], o["out_width"], o["out_height"], o["fps"])
    notify.push("done", db.get_job(jid))


def heartbeat():
    """処理中（数分かかる）も止まらないよう別スレッドで心拍ファイルを更新する（app が停止を検知する）。"""
    path = monitor.heartbeat_path()
    while True:
        try:
            with open(path, "a"):
                os.utime(path, None)
        except OSError as e:
            log.warning("heartbeat 失敗: %s", e)
        time.sleep(30)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    db.reset_running()
    threading.Thread(target=heartbeat, daemon=True).start()
    log.info("worker 起動（1件ずつ処理）")
    while True:
        j = db.next_job()
        if not j:
            time.sleep(2)
            continue
        if j["status"] == "queued":
            try:
                do_prepare(j)
            except Exception as e:
                fail_prepare(j["id"], prepare_fail_msg(e), e)
        else:
            try:
                do_export(j)
            except Exception as e:
                fail_export(j["id"], e)


if __name__ == "__main__":
    main()
