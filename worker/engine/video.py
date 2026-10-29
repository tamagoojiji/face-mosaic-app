"""動画モードの処理（1080p 変換→顔の追跡→顔と服の特徴→場面写真→判定→書き出し）。色補正はしない。

~/dev/tools/photo-retouch/dancer_pick.py と video_retouch.py からコピーしたもの（2026-10-24）。
判定ルール（decide・pick_shots・classify_tracks_global・build_frame_boxes）と数値は元と同じ。
変更点:
- キャッシュの置き場所を var/ ではなくジョブのフォルダにした（引数でパスを受け取る）
- 選択画面・確認画面の HTML は作らず、画面の元データ（JSON）と画像だけを作る
- 確認画面の顔の切り抜きは、判定の前に「確認画面に出る可能性のある顔」全部について作っておく
  （確認画面を待たずに開けるようにするため。切り抜くフレームと範囲は build_review と同じ）
- 書き出しは video_retouch.py --no-lut --reuse-tracks --overrides と同じ手順を関数で呼ぶ
- HDR の PQ（sys.exit で止まる）は RuntimeError にする
"""

import argparse
import bisect
import collections
import concurrent.futures
import json
import math
import multiprocessing
import os
import shutil
import statistics
import subprocess
import sys
from fractions import Fraction

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import cv2
import numpy as np
from PIL import Image

from . import hdr_sdr
from .detect import (BODY_ZONE_CONF, MAIN_CONF, MAIN_SUBJECT_FRAC, YUNET_PATH,
                     _overlap, detect_faces_yunet)
from .mosaic import mosaic_face

# 処理の版（並列化版）。jobs.engine_ver に記録し、見込みの統計は同じ版の実測だけ使う
ENGINE_VER = "2026-10-29"

MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "models")
SFACE_PATH = os.path.join(MODELS, "sface.onnx")

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
# Mac はデコードに VideoToolbox を使う。Linux（VPS）には無いので付けない
HWACCEL = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

# ---- video_retouch.py の値
DETECT_LONG_SIDE = 1280
IOU_MATCH = 0.3
EMA_ALPHA = 0.5
HOLD_FRAMES = 15
BITRATE_SCALE = 0.8
BITRATE_CAP = 80_000_000
# video_retouch.py の既定（--faces background --face-keep-ratio 0.5 --face-min-frac 0.03 --detect-every 1）
FACE_KEEP_RATIO = 0.5
FACE_MIN_FRAC = 0.03
EXPORT_DETECT_EVERY = 1

# ---- dancer_pick.py の値
FEAT_W = 0
SAMPLES = 6
BLUR_MIN_CONF = 0.40
PICK_MIN_CONF = 0.5
PICK_MIN_FRAC = 0.025
SHOTS = 12
SHOT_W = 1080
LINK_GAP = 45
LINK_DIST = 2.0
LINK_FACE = 0.30
LINK_CLOTH = 0.55
GLOB_FACE = 0.35
GLOB_CLOTH = 0.60
GLOB_SUM = 1.15
KEEP_MIN_FRAC = 0.042
COSTUME_CLOTH = 0.50
COSTUME_MIN_FRAC = 0.07
COSTUME_MIN_SEEN = 4
REVIEW_MIN_FRAC = 0.0625
REVIEW_THUMB = 200
FEAT_VER = f"w{FEAT_W}-s{SAMPLES}-torso0.6"


# ---------------------------------------------------------------- ffmpeg 周り（video_retouch.py）

def probe(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", path], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe に失敗: {(r.stderr or r.stdout).strip()[-300:]}")
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError(f"映像ストリームがありません: {path}")

    rot = 0
    for sd in v.get("side_data_list") or []:
        if sd.get("rotation") is not None:
            rot = int(round(float(sd["rotation"])))
    rot %= 360

    num, _, den = v.get("r_frame_rate", "30/1").partition("/")
    fps = float(num) / float(den or 1)
    dur = float(v.get("duration") or d["format"]["duration"])
    nb = int(v["nb_frames"]) if str(v.get("nb_frames") or "").isdigit() \
        else int(round(dur * fps))
    br = int(v["bit_rate"]) if str(v.get("bit_rate") or "").isdigit() \
        else int(float(d["format"].get("bit_rate") or 0))

    w, h = int(v["width"]), int(v["height"])
    out_w, out_h = (h, w) if rot in (90, 270) else (w, h)
    return {"width": w, "height": h, "out_width": out_w, "out_height": out_h,
            "rotation": rot, "fps": fps, "fps_str": v.get("r_frame_rate", "30/1"),
            "frames": nb, "duration": dur, "bit_rate": br, "has_b_frames": int(v.get("has_b_frames") or 0),
            "has_audio": any(s.get("codec_type") == "audio" for s in d["streams"])}


def join_vf(*parts):
    return ",".join([p for p in parts if p])


def grab_frame(path, t, vf, width, out_png):
    filt = join_vf(vf, f"scale={width}:-2")
    cmd = [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path,
           "-frames:v", "1", "-vf", filt, "-y", out_png]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_png):
        raise RuntimeError(f"フレーム抽出に失敗 ({t:.2f}s): "
                           f"{(r.stderr or r.stdout).strip()[-300:]}")


def pick_encoder():
    r = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi",
                        "-i", "color=c=black:s=64x64:r=30:d=0.1",
                        "-c:v", "hevc_videotoolbox", "-frames:v", "1", "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return "hevc_videotoolbox"
    return "libx264"


# ---------------------------------------------------------------- 準備（dancer_pick.py）

def prepare(path, work_dir):
    """戻り値 (info, vf, hdr_info)。HDR の LUT は work_dir に作る。"""
    info = probe(path)
    # 回転（縦動画の displaymatrix）は ffmpeg の自動回転に任せる。vf に transpose を足すと、
    # -noautorotate で回した 1080p 版にも displaymatrix が残り（ffmpeg 7.1 で確認）、後の工程で二重に回って縦動画が横になる
    vf = ""
    try:
        hdr_vf, hdr_info = hdr_sdr.plan(path, work_dir, None, None, ffprobe=FFPROBE,
                                        label=os.path.basename(path), quiet=True)
    except SystemExit as e:
        raise RuntimeError(f"HDR(PQ) は未対応: {str(e)[:120]}")
    vf = join_vf(hdr_vf, vf)
    return info, vf, hdr_info


def _to_1080_cmd(path, vf, out, info):
    # 順番が大事: 先に 30fps へ間引いて 1080p に縮めてから HDR→SDR の LUT を掛ける。
    # LUT（RGB 化→3D LUT→YUV 戻し）を 4K60 のまま掛けると、最後に捨てる 7/8 の画素にも掛かる
    # （VPS 6 実測・4K60 HLG 30秒: LUT→縮小 299秒 / 間引き→縮小→LUT 90秒・PSNR 44dB で見た目は同じ）
    filt = join_vf("fps=30", "scale='if(gt(iw,ih),1920,-2)':'if(gt(iw,ih),-2,1920)'", vf)
    dec = []
    # 50fps 以上で B フレームがあるときは、非参照 B フレームをデコードしない（30fps への間引きで捨てる側のため）
    # （VPS 6 実測・4K60 HLG 1分: 188.6→127.9秒・出力コマ数は同じ）。CONVERT_SKIP_BIDIR=0 で無効
    if os.environ.get("CONVERT_SKIP_BIDIR", "1") != "0" and info["fps"] >= 50 and info.get("has_b_frames"):
        dec = ["-skip_frame", "bidir"]
    return [FFMPEG, "-v", "error", "-y", *HWACCEL, *dec, "-i", path,
            "-vf", filt, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
            "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", "-c:a", "copy", "-metadata:s:v", "rotate=0", out + ".part.mp4"]


def to_1080(path, vf, out):
    """最初に1回だけ 1080p・30fps・SDR へ変換する。以降の工程はすべてこのファイルで行う。

    自動回転で画素を正立させ、長辺を1920に合わせる（縦は1080x1920・横は1920x1080）。
    自動回転を効かせると displaymatrix は出力に引き継がれず、回転情報の無い正立動画になる。"""
    if os.path.exists(out):
        return out
    subprocess.run(_to_1080_cmd(path, vf, out, probe(path)), check=True)
    os.replace(out + ".part.mp4", out)
    return out


# ---------------------------------------------------------------- 顔の追跡（video_retouch.py）

def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _pool_init():
    # 並列のワーカーは1プロセス1スレッド（プロセス数で CPU を使う）
    cv2.setNumThreads(1)


def _track_detect(buf, dw, dh, face_score, upscale):
    """ワーカー側: 1コマの顔検出（検出器は detect.py のキャッシュでプロセスごとに1つ）。"""
    frame = np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 3)
    return detect_faces_yunet(frame, YUNET_PATH, face_score, upscale=upscale)


def track_faces(path, info, vf, args):
    ow, oh = info["out_width"], info["out_height"]
    scale = min(1.0, DETECT_LONG_SIDE / float(max(ow, oh)))
    dw = max(2, int(round(ow * scale / 2)) * 2)
    dh = max(2, int(round(oh * scale / 2)) * 2)

    cmd = [FFMPEG, "-v", "error", *HWACCEL,
           "-i", path, "-an", "-sn", "-dn",
           "-vf", join_vf(vf, f"scale={dw}:{dh}")]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    fsize = dw * dh * 3
    total = info["frames"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=fsize)

    active, tracks = [], []
    next_id = 0
    step = max(1, int(args.detect_every))
    upscale = getattr(args, "detect_upscale", 2)
    mark = 0
    i = 0

    def associate(fi, dets, warn):
        # 対応付け（IoU・EMA・HOLD_FRAMES）はコマの順に親で直列に行う
        nonlocal active, next_id
        if warn and fi == 0:
            print(warn, file=sys.stderr)
        dets.sort(key=lambda f: -float(f.get("conf", 0.0)))
        used = set()
        for d in dets:
            best, best_iou = None, IOU_MATCH
            for t in active:
                if t["id"] in used:
                    continue
                iou, _c = _overlap(d, t["last"])
                if iou > best_iou:
                    best, best_iou = t, iou
            if best is None:
                best = {"id": next_id, "boxes": {}, "confs": [],
                        "last": {k: float(d[k]) for k in "xywh"},
                        "last_frame": fi, "first_frame": fi}
                next_id += 1
                active.append(best)
                tracks.append(best)
            else:
                lb = best["last"]
                best["last"] = {k: EMA_ALPHA * float(d[k]) + (1.0 - EMA_ALPHA) * lb[k]
                                for k in "xywh"}
            used.add(best["id"])
            best["boxes"][fi] = dict(best["last"], conf=float(d.get("conf", 0.0)))
            best["confs"].append(float(d.get("conf", 0.0)))
            best["last_frame"] = fi
        active = [t for t in active if fi - t["last_frame"] <= HOLD_FRAMES]

    n_workers = _env_int("TRACK_WORKERS", 4)
    pool = multiprocessing.get_context("spawn").Pool(n_workers, initializer=_pool_init) \
        if n_workers > 1 else None
    pending = collections.deque()  # (コマ番号, AsyncResult)。未完了は 2N 個まで
    try:
        while True:
            buf = proc.stdout.read(fsize)
            if len(buf) < fsize:
                break
            if i % step == 0:
                if pool is None:
                    frame = np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 3)
                    associate(i, *detect_faces_yunet(frame, YUNET_PATH, args.face_score, upscale=upscale))
                else:
                    while len(pending) >= 2 * n_workers:
                        fi, res = pending.popleft()
                        associate(fi, *res.get())
                    pending.append((i, pool.apply_async(
                        _track_detect, (buf, dw, dh, args.face_score, upscale))))
            i += 1
            if total and i * 10 // max(total, 1) > mark:
                mark = i * 10 // max(total, 1)
                print(f"  追跡 {mark * 10}%  ({i}/{total}f・トラック {len(tracks)})", flush=True)
        while pending:
            fi, res = pending.popleft()
            associate(fi, *res.get())
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()
        try:
            proc.stdout.close()
        except OSError:
            pass
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        rc = proc.wait()
    if rc not in (0, 255) and not tracks:
        raise RuntimeError(f"顔追跡のデコードに失敗 (rc={rc}): {err.strip()[-300:]}")

    for t in tracks:
        hs = [b["h"] for b in t["boxes"].values()]
        t["med_h"] = statistics.median(hs)
        t["med_conf"] = statistics.median(t["confs"])
        t["med_box"] = {k: statistics.median([b[k] for b in t["boxes"].values()])
                        for k in "xywh"}
        t["seen"] = len(t["boxes"])
    return tracks, dw, dh, i


def save_tracks(p, tracks, dw, dh, seen_frames, args):
    data = {
        "detect_size": [dw, dh], "seen_frames": seen_frames,
        "face_score": args.face_score, "detect_every": int(args.detect_every),
        "tracks": [{"id": t["id"], "first_frame": t["first_frame"],
                    "last_frame": t["last_frame"], "confs": t["confs"],
                    "boxes": {str(f): b for f, b in t["boxes"].items()}}
                   for t in tracks],
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return p


def load_tracks(p):
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    tracks = []
    for t in data["tracks"]:
        boxes = {int(k): v for k, v in t["boxes"].items()}
        tr = {"id": t["id"], "boxes": boxes, "confs": t["confs"],
              "first_frame": t["first_frame"], "last_frame": t["last_frame"],
              "last": boxes[max(boxes)]}
        hs = [b["h"] for b in boxes.values()]
        tr["med_h"] = statistics.median(hs)
        tr["med_conf"] = statistics.median(t["confs"])
        tr["med_box"] = {k: statistics.median([b[k] for b in boxes.values()])
                         for k in "xywh"}
        tr["seen"] = len(boxes)
        tracks.append(tr)
    dw, dh = data["detect_size"]
    return tracks, dw, dh, data["seen_frames"]


def get_tracks(path, info, vf, tracks_path, fast=False):
    if os.path.exists(tracks_path):
        return load_tracks(tracks_path)
    # fast: 1フレームおき・拡大なしで探す（VPS 実測で 1 フレーム 0.31 秒 → 0.12 秒）
    # DETECT_EVERY（env）で fast の間引きを変えられる（既定 2）
    args = argparse.Namespace(face_score=0.25, detect_every=_env_int("DETECT_EVERY", 2) if fast else 1,
                              preview_only=False,
                              detect_upscale=1 if fast else 2)
    tracks, dw, dh, seen = track_faces(path, info, vf, args)
    save_tracks(tracks_path, tracks, dw, dh, seen, args)
    return tracks, dw, dh, seen


def boxes_of(t):
    return {int(k): v for k, v in t["boxes"].items()}


# ---------------------------------------------------------------- 特徴（dancer_pick.py）

def sample_frames(t, n=SAMPLES):
    bx = boxes_of(t)
    keys = sorted(bx)
    if len(keys) <= n:
        return keys
    seg = len(keys) / float(n)
    out = []
    for i in range(n):
        part = keys[int(i * seg):int((i + 1) * seg)] or keys[-1:]
        out.append(max(part, key=lambda f: bx[f].get("conf", 0.0)))
    return out


def torso_hist(img, x, y, w, h):
    H, W = img.shape[:2]
    x0, x1 = x + w / 2.0 - 0.6 * w, x + w / 2.0 + 0.6 * w
    y0, y1 = y + 1.3 * h, y + 2.8 * h
    cx0, cx1 = int(max(0, x0)), int(min(W, x1))
    cy0, cy1 = int(max(0, y0)), int(min(H, y1))
    if cx1 - cx0 < 4 or cy1 - cy0 < 4:
        return None
    if (cx1 - cx0) * (cy1 - cy0) < 0.5 * (x1 - x0) * (y1 - y0):
        return None
    hsv = cv2.cvtColor(np.ascontiguousarray(img[cy0:cy1, cx0:cx1]), cv2.COLOR_RGB2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [18, 4, 4], [0, 180, 0, 256, 0, 256])
    hist = hist.flatten()
    s = hist.sum()
    return hist / s if s > 0 else None


def face_feature(img, x, y, w, h, det, rec):
    H, W = img.shape[:2]
    m = 0.4
    x0, y0 = int(max(0, x - m * w)), int(max(0, y - m * h))
    x1, y1 = int(min(W, x + (1 + m) * w)), int(min(H, y + (1 + m) * h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = np.ascontiguousarray(img[y0:y1, x0:x1, ::-1])  # BGR
    s = max(1.0, 160.0 / (y1 - y0))
    if s > 1.0:
        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    ch, cw = crop.shape[:2]
    det.setInputSize((cw, ch))
    _, faces = det.detect(crop)
    if faces is not None and len(faces):
        f = max(faces, key=lambda r: r[-1])
        aligned = rec.alignCrop(crop, f)
    else:
        fx0, fy0 = int((x - x0) * s), int((y - y0) * s)
        sq = crop[max(0, fy0):fy0 + int(h * s), max(0, fx0):fx0 + int(w * s)]
        if sq.size == 0:
            return None
        aligned = cv2.resize(sq, (112, 112))
    feat = rec.feature(aligned).flatten().astype(np.float32)
    n = np.linalg.norm(feat)
    return feat / n if n > 0 else None


def _save_feats(path, face_m, cloth_m):
    fi = np.array(sorted(face_m), dtype=np.int64)
    ci = np.array(sorted(cloth_m), dtype=np.int64)
    np.savez(path, ver=np.array(FEAT_VER), face_ids=fi,
             face_vecs=np.stack([face_m[i] for i in fi]) if len(fi) else np.zeros((0, 128)),
             cloth_ids=ci,
             cloth_vecs=np.stack([cloth_m[i] for i in ci]) if len(ci) else np.zeros((0, 288)))


def load_feats(path):
    z = np.load(path)
    face_m = {int(i): v for i, v in zip(z["face_ids"], z["face_vecs"])}
    cloth_m = {int(i): v for i, v in zip(z["cloth_ids"], z["cloth_vecs"])}
    return face_m, cloth_m


_FEAT_MODELS = None  # ワーカー側で遅延生成する (det, rec)


def _feat_frame(buf, fw, fh, k, items):
    """ワーカー側: 1コマ分の [(tid, 顔特徴 or None, 服ヒストグラム or None)]。"""
    global _FEAT_MODELS
    if _FEAT_MODELS is None:
        _FEAT_MODELS = (cv2.FaceDetectorYN.create(YUNET_PATH, "", (160, 160), score_threshold=0.3),
                        cv2.FaceRecognizerSF.create(SFACE_PATH, ""))
    det, rec = _FEAT_MODELS
    img = np.frombuffer(buf, dtype=np.uint8).reshape(fh, fw, 3)
    out = []
    for tid, b in items:
        x, y, w, h = b["x"] * k, b["y"] * k, b["w"] * k, b["h"] * k
        out.append((tid, face_feature(img, x, y, w, h, det, rec), torso_hist(img, x, y, w, h)))
    return out


def compute_features(path, info, vf, tracks, dw, cache):
    """BLUR_MIN_CONF 以上のトラックについて、顔と服の特徴を1パスで計算する。"""
    if os.path.exists(cache) and "ver" in np.load(cache).files \
            and str(np.load(cache)["ver"]) == FEAT_VER:
        return load_feats(cache)
    if not os.path.exists(SFACE_PATH):
        raise RuntimeError(f"顔照合モデルがありません: {SFACE_PATH}")

    ow, oh = info["out_width"], info["out_height"]
    fw = min(FEAT_W, ow) if FEAT_W else ow
    fh = int(round(oh * fw / float(ow) / 2)) * 2
    k = fw / float(dw)

    need = {}
    for t in tracks:
        if t["med_conf"] < BLUR_MIN_CONF:
            continue
        bx = boxes_of(t)
        for f in sample_frames(t):
            need.setdefault(f, []).append((t["id"], bx[f]))

    n_workers = _env_int("FEAT_WORKERS", 4)
    if n_workers > 1:
        pool = multiprocessing.get_context("spawn").Pool(n_workers, initializer=_pool_init)
        det = rec = None  # 親では作らない（各ワーカーで遅延生成）
    else:
        pool = None
        det = cv2.FaceDetectorYN.create(YUNET_PATH, "", (160, 160), score_threshold=0.3)
        rec = cv2.FaceRecognizerSF.create(SFACE_PATH, "")
    faces, cloths = {}, {}

    def collect(rows):
        for tid, fe, th in rows:
            if fe is not None:
                faces.setdefault(tid, []).append(fe)
            if th is not None:
                cloths.setdefault(tid, []).append(th)

    cmd = [FFMPEG, "-v", "error", *HWACCEL,
           "-i", path, "-an", "-sn", "-dn"]
    feat_vf = join_vf(vf, f"scale={fw}:{fh}" if fw != ow else "")
    if feat_vf:
        cmd += ["-vf", feat_vf]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    fsize = fw * fh * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=fsize)
    pending = collections.deque()  # 未完了は 2N 個まで・コマの順に取り出す
    i, last = 0, max(need) if need else -1
    try:
        while i <= last:
            buf = proc.stdout.read(fsize)
            if len(buf) < fsize:
                break
            if i in need:
                if pool is None:
                    img = np.frombuffer(buf, dtype=np.uint8).reshape(fh, fw, 3)
                    for tid, b in need[i]:
                        x, y, w, h = b["x"] * k, b["y"] * k, b["w"] * k, b["h"] * k
                        fe = face_feature(img, x, y, w, h, det, rec)
                        if fe is not None:
                            faces.setdefault(tid, []).append(fe)
                        th = torso_hist(img, x, y, w, h)
                        if th is not None:
                            cloths.setdefault(tid, []).append(th)
                else:
                    while len(pending) >= 2 * n_workers:
                        collect(pending.popleft().get())
                    pending.append(pool.apply_async(_feat_frame, (buf, fw, fh, k, need[i])))
            i += 1
        while pending:
            collect(pending.popleft().get())
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()
        proc.stdout.close()
        proc.kill()
        proc.wait()

    face_m = {}
    for tid, fs in faces.items():
        v = np.mean(fs, axis=0)
        face_m[tid] = v / (np.linalg.norm(v) or 1.0)
    cloth_m = {tid: np.mean(hs, axis=0) for tid, hs in cloths.items()}
    _save_feats(cache, face_m, cloth_m)
    print(f"  特徴 {len(face_m)} トラック（服 {len(cloth_m)}）", flush=True)
    return face_m, cloth_m


def cloth_sim(a, b):
    return float(np.minimum(a, b).sum())


def _sim(a, b, face_m, cloth_m):
    fs = float(np.dot(face_m[a], face_m[b])) if a in face_m and b in face_m else -1.0
    cs = cloth_sim(cloth_m[a], cloth_m[b]) if a in cloth_m and b in cloth_m else 0.0
    return fs, cs


def decide(tracks, picked, face_m, cloth_m, short):
    """picked から似ている断片を広げて keep を決める。戻り値は (keep_ids, blur_ids)。（dancer_pick.py と同じ）"""
    by = {t["id"]: t for t in tracks}
    faces = [t for t in tracks if t["med_conf"] >= BLUR_MIN_CONF]
    min_h = KEEP_MIN_FRAC * short
    ends = {}
    for t in faces:
        bx = boxes_of(t)
        ks = sorted(bx)
        ends[t["id"]] = (ks[0], ks[-1], bx[ks[0]], bx[ks[-1]])

    def ctr(b):
        return b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0

    order = sorted(faces, key=lambda t: ends[t["id"]][0])
    first = [ends[t["id"]][0] for t in order]
    edges = {}
    for t in faces:
        _, l, _, lb = ends[t["id"]]
        lo, hi = bisect.bisect_left(first, l), bisect.bisect_right(first, l + LINK_GAP)
        for u in order[lo:hi]:
            if u is t:
                continue
            fb = ends[u["id"]][2]
            (ax, ay), (bx_, by_) = ctr(lb), ctr(fb)
            if ((ax - bx_) ** 2 + (ay - by_) ** 2) ** 0.5 > LINK_DIST * lb["h"]:
                continue
            if not 0.6 <= fb["h"] / max(lb["h"], 1e-6) <= 1.6:
                continue
            edges.setdefault(t["id"], []).append(u["id"])
            edges.setdefault(u["id"], []).append(t["id"])

    def spread(seed):
        keep, front = set(seed), list(seed)
        while front:
            nxt = []
            for a in front:
                for b in edges.get(a, []):
                    if b in keep or by[b]["med_h"] < min_h:
                        continue
                    fs, cs = _sim(a, b, face_m, cloth_m)
                    if fs >= LINK_FACE and cs >= LINK_CLOTH:
                        keep.add(b)
                        nxt.append(b)
            front = nxt
        return keep

    picked = [p for p in picked if p in by]
    glob = set()
    for t in faces:
        i = t["id"]
        if t["med_h"] < min_h or i not in face_m or i not in cloth_m:
            continue
        for r in picked:
            fs, cs = _sim(i, r, face_m, cloth_m)
            if fs >= GLOB_FACE and cs >= GLOB_CLOTH and fs + cs >= GLOB_SUM:
                glob.add(i)
                break
    costume = set()
    for t in faces:
        i = t["id"]
        if t["med_h"] < COSTUME_MIN_FRAC * short or i not in cloth_m or len(t["boxes"]) < COSTUME_MIN_SEEN:
            continue
        if any(cloth_sim(cloth_m[i], cloth_m[r]) >= COSTUME_CLOTH for r in picked if r in cloth_m):
            costume.add(i)
    keep = spread(spread(picked) | glob) | costume
    blur = {t["id"] for t in faces} - keep
    return keep, blur


# ---------------------------------------------------------------- 選択画面のデータ（dancer_pick.py）

def pick_shots(tracks, dw, dh, n_frames, shots=SHOTS):
    short = min(dw, dh)
    cand = [t for t in tracks if t["med_conf"] >= PICK_MIN_CONF
            and t["med_h"] >= PICK_MIN_FRAC * short]
    per = {}
    for t in cand:
        for f, b in boxes_of(t).items():
            if b.get("conf", 0) >= PICK_MIN_CONF:
                per.setdefault(f, []).append((t["id"], b))
    out = []
    for s in range(shots):
        a, z = int(s * n_frames / shots), int((s + 1) * n_frames / shots)
        best = max(range(a, z), key=lambda f: sum(b["h"] for _, b in per.get(f, [])), default=None)
        if best is not None and per.get(best):
            out.append((best, per[best]))
    return out


def build_shots(path, info, vf, shots, dw, out_dir):
    """build_page と同じ場面写真と枠のデータ。戻り値は items（画面の元データ）。"""
    os.makedirs(out_dir, exist_ok=True)
    k = SHOT_W / float(dw)
    # 切り出しは ffmpeg 待ちなのでスレッドで SHOT_WORKERS 本ずつ同時に行う
    with concurrent.futures.ThreadPoolExecutor(max_workers=_env_int("SHOT_WORKERS", 6)) as ex:
        futs = [ex.submit(grab_frame, path, f / info["fps"], vf, SHOT_W, os.path.join(out_dir, f"shot{j}.jpg"))
                for j, (f, _faces) in enumerate(shots)]
        for fu in futs:
            fu.result()
    items = []
    for j, (f, faces) in enumerate(shots):
        img = f"shot{j}.jpg"
        with Image.open(os.path.join(out_dir, img)) as im:
            sw, sh = im.size
        groups = []
        for tid, b in sorted(faces, key=lambda p: -p[1]["w"] * p[1]["h"]):
            for g in groups:
                iou, inner = _overlap(b, g["box"])
                if iou > 0.4 or inner > 0.5:
                    g["ids"].append(tid)
                    break
            else:
                groups.append({"ids": [tid], "box": b})
        boxes = []
        for g in groups:
            b = g["box"]
            boxes.append({"ids": g["ids"][:1], "l": b["x"] * k / SHOT_W * 100, "t": b["y"] * k / sh * 100,
                          "w": b["w"] * k / SHOT_W * 100, "h": b["h"] * k / sh * 100})
        items.append({"img": j, "sec": round(f / info["fps"], 1), "w": sw, "h": sh, "boxes": boxes})
    return items


def dh_of(info, dw):
    return int(round(info["out_height"] * dw / float(info["out_width"])))


def make_thumbs(path, info, vf, tracks, dw, out_dir):
    """確認画面に出る可能性のある顔（conf 0.40 以上・高さ短辺 4.2% 以上）の切り抜きを作る。

    切り抜くフレーム（conf 最大）と範囲（顔の2倍・200px）は build_review と同じ。
    """
    os.makedirs(out_dir, exist_ok=True)
    short = min(dw, dh_of(info, dw))
    rows = [t for t in tracks if t["med_conf"] >= BLUR_MIN_CONF
            and t["med_h"] >= min(REVIEW_MIN_FRAC, KEEP_MIN_FRAC) * short]
    need = {}
    for t in rows:
        bx = boxes_of(t)
        f = max(bx, key=lambda k: bx[k].get("conf", 0.0))
        need.setdefault(f, []).append((t["id"], bx[f]))
    ow, oh = info["out_width"], info["out_height"]
    fw = min(SHOT_W, ow)
    fh = int(round(oh * fw / float(ow) / 2)) * 2
    k = fw / float(dw)
    cmd = [FFMPEG, "-v", "error", *HWACCEL,
           "-i", path, "-an", "-sn", "-dn", "-vf", join_vf(vf, f"scale={fw}:{fh}"),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    fsize = fw * fh * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=fsize)
    i, last = 0, max(need) if need else -1
    try:
        while i <= last:
            buf = proc.stdout.read(fsize)
            if len(buf) < fsize:
                break
            if i in need:
                im = Image.frombuffer("RGB", (fw, fh), buf, "raw", "RGB", 0, 1)
                for tid, b in need[i]:
                    cx, cy = (b["x"] + b["w"] / 2.0) * k, (b["y"] + b["h"] / 2.0) * k
                    half = max(12.0, max(b["w"], b["h"]) * k)
                    im.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half))) \
                      .resize((REVIEW_THUMB, REVIEW_THUMB), Image.LANCZOS) \
                      .save(os.path.join(out_dir, f"t{tid}.jpg"), quality=85)
            i += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
    return len(rows)


def review_lists(info, tracks, keep, dw):
    """build_review と同じ「モザイクをかける予定」「かけない予定」の一覧。"""
    short = min(dw, dh_of(info, dw))
    cand = [t for t in tracks if t["id"] not in keep and t["med_conf"] >= BLUR_MIN_CONF
            and t["med_h"] >= REVIEW_MIN_FRAC * short and len(t["boxes"]) >= 2]
    kept = [t for t in tracks if t["id"] in keep and t["med_h"] >= KEEP_MIN_FRAC * short]
    fps = info["fps"]

    def item(t):
        return {"id": t["id"], "sec": round(t["first_frame"] / fps, 1)}
    return {"blur": [item(t) for t in sorted(cand, key=lambda t: t["first_frame"])],
            "keep": [item(t) for t in sorted(kept, key=lambda t: t["first_frame"])]}


# ---------------------------------------------------------------- 書き出し（video_retouch.py）

def classify_tracks_global(tracks, short, mode, keep_ratio, min_frac):
    if mode == "all":
        for t in tracks:
            t["action"] = "blur"
            t["votes"] = [0, 0]
        return False
    if not tracks:
        return False
    big = MAIN_SUBJECT_FRAC * short
    cands = [t for t in tracks if t["med_conf"] >= MAIN_CONF]
    max_h = max((t["med_h"] for t in cands), default=0.0)
    for t in tracks:
        t["votes"] = [0, 0]
    if max_h < big:
        for t in tracks:
            t["action"] = "blur" if (t["med_h"] < big or t["med_conf"] >= MAIN_CONF) else "drop"
        return True
    main = max(cands, key=lambda t: t["med_h"])
    mb = main["med_box"]
    zx0, zx1 = mb["x"] - 0.75 * mb["w"], mb["x"] + 1.75 * mb["w"]
    zy0, zy1 = mb["y"] + 0.5 * mb["h"], mb["y"] + 3.0 * mb["h"]
    for t in tracks:
        if t["med_conf"] < MAIN_CONF:
            t["action"] = "drop"
            continue
        if t is not main and t["med_conf"] < BODY_ZONE_CONF:
            b = t["med_box"]
            cx, cy = b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0
            if zx0 <= cx <= zx1 and zy0 <= cy <= zy1:
                t["action"] = "skip"
                t["reason"] = "main_body_zone"
                continue
        small = t["med_h"] < keep_ratio * max_h or t["med_h"] < min_frac * short
        t["action"] = "blur" if small else "keep"
    main["action"] = main.get("action", "keep")
    return False


def apply_overrides(tracks, ov):
    byid = {t["id"]: t for t in tracks}
    forced = {"keep": [], "blur": [], "missing": []}
    for act in ("keep", "blur"):
        for tid in ov.get(act) or []:
            t = byid.get(int(tid))
            if t is None:
                forced["missing"].append(int(tid))
                continue
            t["action"] = act
            t["reason"] = "override"
            forced[act].append(int(tid))
    return forced


def filled_track_boxes(tracks, n_frames, detect_every):
    """blur のトラックごとに、1080p 版（30fps）のフレーム番号 → 枠（解析サイズの座標）を埋めたもの。

    隙間の線形補間・途切れた後の保持（HOLD_FRAMES）・keep の顔の上に来た補間枠の除外は build_frame_boxes と同じ。
    戻り値は [(トラック, {フレーム: 枠})]（トラックの順・フレームの順は build_frame_boxes と同じ）。"""
    keep_at = {}
    for t in tracks:
        if t.get("action") == "keep":
            for f, b in t["boxes"].items():
                keep_at.setdefault(int(f), []).append(b)

    def on_keep(f, b):
        cx, cy = b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0
        return any(k["x"] <= cx <= k["x"] + k["w"] and k["y"] <= cy <= k["y"] + k["h"]
                   for k in keep_at.get(f, ()))

    out = []
    for t in tracks:
        if t.get("action") != "blur":
            continue
        keys = sorted(t["boxes"])
        filled = {}
        for j, f in enumerate(keys):
            filled[f] = t["boxes"][f]
            if j + 1 < len(keys):
                g = keys[j + 1]
                gap = g - f
                if 1 < gap <= HOLD_FRAMES + detect_every:
                    b0, b1 = t["boxes"][f], t["boxes"][g]
                    for k in range(1, gap):
                        w = k / float(gap)
                        filled[f + k] = {c: b0[c] + (b1[c] - b0[c]) * w for c in "xywh"}
        last = keys[-1]
        for k in range(1, HOLD_FRAMES + 1):
            if last + k < n_frames:
                filled[last + k] = t["boxes"][last]
        out.append((t, {f: b for f, b in filled.items() if f in t["boxes"] or not on_keep(f, b)}))
    return out


def build_frame_boxes(tracks, n_frames, inv_scale, detect_every):
    per_frame = {}
    for _t, filled in filled_track_boxes(tracks, n_frames, detect_every):
        for f, b in filled.items():
            box = {"x": int(round(b["x"] * inv_scale)), "y": int(round(b["y"] * inv_scale)),
                   "w": max(2, int(round(b["w"] * inv_scale))),
                   "h": max(2, int(round(b["h"] * inv_scale)))}
            per_frame.setdefault(f, []).append(box)
    return per_frame


# 書き出しの分割並列: 1 分割あたりの最低コマ数（短い動画は分割数を減らす）
SEG_MIN_FRAMES = 150


def _seg_bounds(total, n):
    """コマ範囲 [0, total) を n 個の [a, b) に分ける。"""
    n = max(1, min(n, total // SEG_MIN_FRAMES))
    return [(total * k // n, total * (k + 1) // n) for k in range(n)]


def _render_seg(hq, path, info, vf, boxes, a, b, last, seg_path, encoder, style, sx, sy, label):
    """コマ [a, b) を音声なしで seg_path に書く（last は最後の分割＝ b で止めずに最後まで読む）。

    hq=False: 1080p 版。boxes は {絶対コマ番号: [枠]}。
    hq=True: 元動画（cfr）。boxes は filled_tracks、sx・sy は元の解像度への倍率。"""
    w, h = info["out_width"], info["out_height"]
    fps = info["fps"]
    fsize = w * h * 3

    dec_cmd = [FFMPEG, "-v", "error", *HWACCEL]
    if hq and HQ_DEC_THREADS:
        dec_cmd += ["-threads", str(HQ_DEC_THREADS)]
    if a > 0:
        # 入力シーク（accurate_seek 既定）。コマ a の時刻 = a / fps（分数で計算）
        dec_cmd += ["-ss", f"{float(a / Fraction(info['fps_str'])):.9f}"]
    dec_cmd += ["-i", path, "-an", "-sn", "-dn"]
    if vf:
        dec_cmd += ["-vf", vf]
    if hq:
        # 1コマ＝1/元fps 秒に揃える（可変フレームレートでもフレーム番号と時刻を対応させるため）
        dec_cmd += ["-fps_mode", "cfr", "-r", info["fps_str"]]
    if not last:
        dec_cmd += ["-frames:v", str(b - a)]
    dec_cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    enc_cmd = [FFMPEG, "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{w}x{h}",
               "-framerate", info["fps_str"], "-i", "-", "-map", "0:v:0"]
    if hq:
        enc_cmd += ["-c:v", "libx264", "-crf", HQ_CRF, "-preset", HQ_PRESET]
        if HQ_ENC_THREADS:
            enc_cmd += ["-threads", str(HQ_ENC_THREADS)]
        if HQ_LOOKAHEAD is not None:
            enc_cmd += ["-rc-lookahead", str(HQ_LOOKAHEAD)]
        enc_cmd += ["-tag:v", "avc1"]
    else:
        bitrate = min(int(info["bit_rate"] * BITRATE_SCALE) or BITRATE_CAP, BITRATE_CAP)
        enc_cmd += ["-c:v", encoder]
        if encoder == "libx264":
            enc_cmd += ["-crf", "20", "-preset", "veryfast"]
        else:
            enc_cmd += ["-b:v", str(bitrate)]
        enc_cmd += ["-threads", "2", "-tag:v", "avc1" if encoder == "libx264" else "hvc1"]
    enc_cmd += ["-pix_fmt", "yuv420p",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                "-f", "mp4", seg_path]

    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=fsize)
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=fsize)

    total = b - a
    mark, j, blurred = 0, 0, 0
    try:
        while True:
            buf = dec.stdout.read(fsize)
            if len(buf) < fsize:
                break
            # 枠は絶対コマ番号 a+j で引く
            fb = hq_boxes_at(boxes, a + j, fps, sx, sy) if hq else boxes.get(a + j)
            if fb:
                arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3).copy()
                for box in fb:
                    mosaic_face(arr, box, 0, style)
                enc.stdin.write(arr.tobytes())
                blurred += 1
            else:
                enc.stdin.write(buf)
            j += 1
            if total and j * 10 // max(total, 1) > mark:
                mark = min(10, j * 10 // max(total, 1))
                print(f"  {label} {mark * 10}%  ({j}/{total}f)", flush=True)
    except BrokenPipeError:
        pass
    finally:
        for p in (dec.stdout, enc.stdin):
            try:
                p.close()
            except OSError:
                pass
        dec_err = dec.stderr.read().decode("utf-8", "replace")
        enc_err = enc.stderr.read().decode("utf-8", "replace")
        dec.stderr.close()
        enc.stderr.close()
        drc = dec.wait()
        rc = enc.wait()
    if hq:
        if rc != 0 or drc not in (0, 255) or j == 0 or not os.path.exists(seg_path):
            raise RuntimeError(f"高画質の書き出しに失敗 (dec rc={drc} enc rc={rc}): "
                               f"{(enc_err or dec_err).strip()[-400:]}")
    elif rc != 0 or not os.path.exists(seg_path):
        raise RuntimeError(f"書き出しに失敗 (enc rc={rc}): "
                           f"{(enc_err or dec_err).strip()[-400:]}")
    return j, blurred


def _render_split(hq, path, info, vf, boxes_of_seg, total, n_seg, out_path, encoder, style, sx, sy, label):
    """[0, total) を分割して別プロセスで同時に描画し、音声を付けて連結する。戻り値は (書いたコマ数, モザイクのコマ数)。"""
    bounds = _seg_bounds(total, n_seg)
    n = len(bounds)
    segs = [f"{out_path}.seg{k}.mp4" for k in range(n)]
    lst = f"{out_path}.list.txt"
    jobs = [(hq, path, info, vf, boxes_of_seg(a, b), a, b, k == n - 1, segs[k], encoder, style, sx, sy,
             f"{label} {k + 1}/{n}" if n > 1 else label)
            for k, (a, b) in enumerate(bounds)]
    pool = None
    try:
        if n == 1:
            res = [_render_seg(*jobs[0])]
        else:
            pool = multiprocessing.get_context("spawn").Pool(n)
            rs = [pool.apply_async(_render_seg, job) for job in jobs]
            res = [r.get() for r in rs]
        with open(lst, "w", encoding="utf-8") as f:
            for sp in segs:
                f.write("file '" + os.path.abspath(sp).replace("'", "'\\''") + "'\n")
        cmd = [FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst]
        if info["has_audio"]:
            cmd += ["-i", path, "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
        else:
            cmd += ["-map", "0:v:0"]
        cmd += ["-c:v", "copy", "-movflags", "+faststart", "-f", "mp4", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(f"{label}の連結に失敗 (rc={r.returncode}): "
                               f"{(r.stderr or r.stdout).strip()[-400:]}")
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()
        for sp in segs + [lst]:
            if os.path.exists(sp):
                os.remove(sp)
    return sum(x[0] for x in res), sum(x[1] for x in res)


def render(path, info, vf, per_frame, out_path, encoder, style):
    """video_retouch.render の LUT なし・プレビューなし版。EXPORT_SEGMENTS 本に分けて同時に描画する。"""
    def boxes_of_seg(a, b):
        return {f: v for f, v in per_frame.items() if a <= f < b}
    written, blurred = _render_split(False, path, info, vf, boxes_of_seg, info["frames"],
                                     _env_int("EXPORT_SEGMENTS", 3), out_path, encoder, style, 1.0, 1.0,
                                     "書き出し")
    return {"frames_written": written, "frames_blurred": blurred, "encoder": encoder}


def export(path, work_dir, tracks_path, keep, blur, style, out_path):
    """video_retouch.py <1080版> --no-lut --reuse-tracks --overrides と同じ書き出し。"""
    info, vf, _ = prepare(path, work_dir)
    tracks, dw, dh, seen = load_tracks(tracks_path)
    classify_tracks_global(tracks, min(dw, dh), "background", FACE_KEEP_RATIO, FACE_MIN_FRAC)
    apply_overrides(tracks, {"keep": sorted(keep), "blur": sorted(blur)})
    inv = info["out_width"] / float(dw)
    per_frame = build_frame_boxes(tracks, seen, inv, EXPORT_DETECT_EVERY)
    # 一時ファイルへ書き、成功したときだけ置き換える（選び直しの失敗で前の完成動画を壊さない）
    tmp = os.path.join(os.path.dirname(out_path), "out.tmp.mp4")
    try:
        r = render(path, info, vf, per_frame, tmp, pick_encoder(), "blur" if style == "blur" else "pixel")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return r


# ---------------------------------------------------------------- 高画質オプション（元の解像度・元の fps で書き出し）

ANALYSIS_FPS = 30.0  # to_1080 の fps=30。tracks.json のフレーム番号はこの fps 基準
# 高画質のエンコード・デコード設定（VPS 4コア/8GB の実測で決めた値。DESIGN 2節「画質について」）
HQ_CRF = "18"
# 4K60 HLG 1分: medium・既定スレッド 1856秒/ピーク4.05GB → veryfast・下の値 1192秒/ピーク2.99GB（見た目の差なし）
HQ_PRESET = "veryfast"
HQ_ENC_THREADS = 3
HQ_LOOKAHEAD = 10
HQ_DEC_THREADS = 2


def hq_boxes_at(filled_tracks, j, src_fps, sx, sy):
    """元動画のフレーム j の枠（元の解像度の座標）。

    時刻 j/元fps を 1080p 版のフレーム位置 f=時刻×30 に直し、前後の解析フレーム f0・f0+1 の枠を時刻で線形補間する。
    片側しか無いとき（トラックの端）はある方の枠をそのまま使う（途切れる端は最後の枠を保持）。"""
    f = j * ANALYSIS_FPS / src_fps
    f0 = int(math.floor(f + 1e-6))
    a = f - f0
    boxes = []
    for _t, filled in filled_tracks:
        b0 = filled.get(f0)
        b1 = filled.get(f0 + 1) if a > 1e-6 else None
        if b0 is not None and b1 is not None:
            b = {c: b0[c] + (b1[c] - b0[c]) * a for c in "xywh"}
        elif b0 is not None:
            b = b0
        elif b1 is not None:
            b = b1
        else:
            continue
        boxes.append({"x": int(round(b["x"] * sx)), "y": int(round(b["y"] * sy)),
                      "w": max(2, int(round(b["w"] * sx))), "h": max(2, int(round(b["h"] * sy)))})
    return boxes


def render_hq(path, info, vf, filled_tracks, sx, sy, out_path, style):
    """元動画をデコード（自動回転・HDR は hdr_sdr と同じ LUT で SDR 化・元の fps のまま）→ 1コマずつモザイク → libx264。

    HQ_SEGMENTS 本に分けて同時に描画する（1 本 2.7GB 使うため既定 2）。"""
    total = int(round(info["duration"] * info["fps"]))  # CFR で出るコマ数（可変fpsの nb_frames より多い）
    # hq_boxes_at はトラック本体を使わないので、子プロセスへは枠だけ渡す
    slim = [(None, filled) for _t, filled in filled_tracks]
    written, blurred = _render_split(True, path, info, vf, lambda a, b: slim, total,
                                     _env_int("HQ_SEGMENTS", 2), out_path, None, style, sx, sy,
                                     "高画質の書き出し")
    return {"frames_written": written, "frames_blurred": blurred, "encoder": f"libx264-crf{HQ_CRF}-{HQ_PRESET}"}


def export_hq(src, v1080, work_dir, tracks_path, keep, blur, style, out_path):
    """高画質オプションの書き出し。解析（tracks.json・keep/blur）は 1080p 版のまま、元動画のフレームに当てる。"""
    info_src, vf_src, _ = prepare(src, work_dir)
    info1080 = probe(v1080)
    tracks, dw, dh, seen = load_tracks(tracks_path)
    classify_tracks_global(tracks, min(dw, dh), "background", FACE_KEEP_RATIO, FACE_MIN_FRAC)
    apply_overrides(tracks, {"keep": sorted(keep), "blur": sorted(blur)})
    filled = filled_track_boxes(tracks, seen, EXPORT_DETECT_EVERY)
    # 解析サイズ → 1080p 版（export と同じ倍率）→ 元の解像度（縦横それぞれ・自動回転後の寸法）
    sx = info1080["out_width"] / float(dw) * (info_src["out_width"] / float(info1080["out_width"]))
    sy = info1080["out_height"] / float(dh) * (info_src["out_height"] / float(info1080["out_height"]))
    tmp = os.path.join(os.path.dirname(out_path), "out.tmp.mp4")
    try:
        r = render_hq(src, info_src, vf_src, filled, sx, sy, tmp, "blur" if style == "blur" else "pixel")
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return r
