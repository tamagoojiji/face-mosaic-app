"""動画のキューを1件ずつ処理する worker（app とは別プロセス・同じ /data を使う）。

queued        → preparing（1080p/30fps 変換・HDR→SDR → 顔の追跡 → 顔と服の特徴 → 場面写真）→ awaiting_pick
queued_export → exporting（書き出し）→ done
失敗は failed＋error（利用者向けの文）。元動画などのファイルは消す。
"""

import json
import logging
import os
import time
import traceback

from app import config, db
from worker.engine import video

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


def fail_prepare(job_id, msg, exc):
    """解析の失敗。続きに使えるものは無いので、元動画と途中のファイルは消す（フォルダと行は期限まで残す）。"""
    log.error("job %s 解析に失敗: %s\n%s", job_id, exc, traceback.format_exc())
    p = paths(job_id)
    for k in ("src", "v1080", "feats", "tracks"):
        _rm(p[k])
    _rm(p["v1080"] + ".part.mp4")
    db.update_job(job_id, status="failed", error=msg, src_path=None, out_path=None,
                  expires_at=time.time() + config.RETENTION_HOURS * 3600)


def fail_export(job_id, exc):
    """書き出しの失敗。フォルダは消さない（1080p 版・追跡結果・切り抜きは選び直しに使う）。

    前の完成動画があれば done に戻して理由を error に残す（前の動画はそのまま保存できる）。
    初回で完成動画が無ければ failed。どちらも期限が来たら retention が消す。
    """
    log.error("job %s 書き出しに失敗: %s\n%s", job_id, exc, traceback.format_exc())
    p = paths(job_id)
    _rm(os.path.join(p["dir"], "out.tmp.mp4"))
    if os.path.exists(p["out"]):
        db.update_job(job_id, status="done", out_path=p["out"], error="処理中にエラーが起きました",
                      expires_at=os.path.getmtime(p["out"]) + config.RETENTION_HOURS * 3600)
    else:
        db.update_job(job_id, status="failed", out_path=None,
                      error="書き出しに失敗しました。お手数ですが、もう一度送ってください",
                      expires_at=time.time() + config.RETENTION_HOURS * 3600)


def do_prepare(j):
    jid, p = j["id"], paths(j["id"])
    db.update_job(jid, status="preparing", error=None, fast_mode=1,
                  t_prepare_sec=None, t_track_sec=None, t_feature_sec=None)
    t0 = time.time()
    info0, vf0, hdr = video.prepare(p["src"], p["dir"])
    db.update_job(jid, fps=info0["fps"], hdr=1 if hdr.get("mode") == "hlg" else 0,
                  width=info0["out_width"], height=info0["out_height"])
    video.to_1080(p["src"], vf0, p["v1080"])
    t1 = time.time()
    db.update_job(jid, t_prepare_sec=round(t1 - t0, 1))
    info, vf, _ = video.prepare(p["v1080"], p["dir"])
    tracks, dw, dh, seen = video.get_tracks(p["v1080"], info, vf, p["tracks"], fast=True)
    t2 = time.time()
    db.update_job(jid, t_track_sec=round(t2 - t1, 1))
    video.compute_features(p["v1080"], info, vf, tracks, dw, p["feats"])
    t3 = time.time()
    db.update_job(jid, t_feature_sec=round(t3 - t2, 1))
    shots = video.pick_shots(tracks, dw, dh, seen)
    items = video.build_shots(p["v1080"], info, vf, shots, dw, p["shots"])
    n_thumbs = video.make_thumbs(p["v1080"], info, vf, tracks, dw, p["thumbs"])
    with open(p["shots_json"], "w", encoding="utf-8") as f:
        json.dump(items, f)
    t4 = time.time()
    db.update_job(jid, status="awaiting_pick", expires_at=time.time() + config.IDLE_DELETE_HOURS * 3600)
    log.info("prepare %s %.0fs  %dx%d %.2ffps hdr=%s  変換 %.1fs 追跡 %.1fs 特徴 %.1fs 場面写真・切り抜き %.1fs  "
             "トラック %d 場面 %d 切り抜き %d", jid, info0["duration"], info0["out_width"], info0["out_height"],
             info0["fps"], hdr.get("mode"), t1 - t0, t2 - t1, t3 - t2, t4 - t3, len(tracks), len(items), n_thumbs)


def do_export(j):
    jid, p = j["id"], paths(j["id"])
    db.update_job(jid, status="exporting", error=None)
    t0 = time.time()
    keep = set(json.loads(j["keep"] or "[]"))
    blur = set(json.loads(j["blur"] or "[]"))
    style = "blur" if j["mosaic_style"] == "blur" else "pixel"
    r = video.export(p["v1080"], p["dir"], p["tracks"], keep, blur, style, p["out"])
    sec = round(time.time() - t0, 1)
    db.update_job(jid, status="done", out_path=p["out"], t_export_sec=sec,
                  expires_at=time.time() + config.RETENTION_HOURS * 3600)
    # 完成後に要らないもの: 元動画・特徴。選び直し（確認画面から書き出し直す）のために 1080p 版・追跡結果・切り抜きは残す
    _rm(p["src"])
    _rm(p["feats"])
    db.update_job(jid, src_path=None)
    log.info("export %s %s %.1fs  keep %d blur %d  コマ %d（モザイクあり %d）%s", jid, style, sec, len(keep),
             len(blur), r["frames_written"], r["frames_blurred"], r["encoder"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    db.reset_running()
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
                msg = "動画を読み込めませんでした。iPhone のカメラで撮った動画を送ってください"
                if "HDR(PQ)" in str(e):
                    msg = "この動画の HDR 形式（HDR10・Dolby Vision の一部）にはまだ対応していません。カメラの設定で HDR ビデオをオフにして撮った動画を送ってください"
                fail_prepare(j["id"], msg, e)
        else:
            try:
                do_export(j)
            except Exception as e:
                fail_export(j["id"], e)


if __name__ == "__main__":
    main()
