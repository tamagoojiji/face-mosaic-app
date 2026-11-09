"""体（人物）の検出＋追跡と、顔トラックとの紐づけ（prototype/bench_person_track.py から移植・2026-11-09）。

体だけで残す／かけるは決めない。同じ体に属する顔の断片どうしを decide の「つなぐ候補」にするだけ
（体 ID は別人に乗り換えることがあるので、顔か服の似かたで確かめてからつなぐ）。
モデルは YOLOX-tiny の ONNX（Megvii 0.1.1rc0・Apache-2.0・models/yolox-LICENSE.md）を cv2.dnn で動かす。
"""

import collections
import json
import multiprocessing
import os
import subprocess
from fractions import Fraction

import cv2
import numpy as np

from .video import FFMPEG, HWACCEL, MODELS, _env_int, _pool_init, _seg_bounds, join_vf

YOLOX_PATH = os.path.join(MODELS, "yolox_tiny.onnx")
BODY_INPUT = 416         # モデルの入力の辺（tiny は 416）
BODY_LONG_SIDE = 640     # 検出に使うコマの長辺（縮小してから検出する）
BODY_CONF = 0.3          # この信頼度以上の検出を使う
BODY_HIGH_CONF = 0.5     # 1 段目の対応付け・新しいトラックに使う検出の信頼度
BODY_IOU = 0.3           # 追跡の対応付けに要る IoU
BODY_NMS_IOU = 0.45      # 検出の重なりの除去（NMS）の IoU
BODY_HOLD = 30           # 検出が途切れてもトラックを保つコマ数
FACE_TOP_FRAC = 0.4      # 顔の中心が体の枠の上 40% 以内なら、その体に属する
BODY_LINK_MIN = 1        # 顔トラックが体に属すると認める「同じコマで重なった回数」の下限（顔 2 コマ・体 4 コマおきなので 1）
BODY_VER = 1             # bodies.json の形式の版


# ---------------------------------------------------------------- 検出（YOLOX）

class Yolox:
    def __init__(self, model=YOLOX_PATH, size=BODY_INPUT, conf=BODY_CONF):
        self.net = cv2.dnn.readNetFromONNX(model)
        self.size, self.conf = size, conf
        grids, strides = [], []
        for s in (8, 16, 32):
            n = size // s
            yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.stride = np.concatenate(strides).astype(np.float32)

    def __call__(self, img):
        """img: BGR。戻り値 [(x, y, w, h, conf)]（img の座標）。"""
        h, w = img.shape[:2]
        s = self.size
        r = min(s / h, s / w)
        nw, nh = int(round(w * r)), int(round(h * r))
        pad = np.full((s, s, 3), 114, np.uint8)  # letterbox（YOLOX 0.1.1rc0 以降は正規化なし・BGR のまま）
        pad[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        self.net.setInput(pad.transpose(2, 0, 1)[None].astype(np.float32))
        out = self.net.forward()[0]
        score = out[:, 4] * out[:, 5]  # objectness × person（COCO の 0 番）
        m = score >= self.conf
        if not m.any():
            return []
        o, sc = out[m], score[m]
        xy = (o[:, :2] + self.grid[m]) * self.stride[m]
        wh = np.exp(o[:, 2:4]) * self.stride[m]
        boxes = np.concatenate([xy - wh / 2, wh], 1) / r
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), sc.tolist(), self.conf, BODY_NMS_IOU)
        res = []
        for k in np.array(keep).reshape(-1):
            x, y, bw, bh = boxes[k]
            x0, y0 = max(0.0, x), max(0.0, y)
            x1, y1 = min(float(w), x + bw), min(float(h), y + bh)
            if x1 > x0 and y1 > y0:
                res.append((x0, y0, x1 - x0, y1 - y0, float(sc[k])))
        return res


def _read_cmd(path, vf, dw, dh, fps_str, a, b):
    """コマ [a, b) を縮小した bgr24 で読む ffmpeg（b=None は最後まで）。シークは _thumb_range と同じ入力シーク。"""
    cmd = [FFMPEG, "-v", "error", *HWACCEL]
    if a > 0:
        cmd += ["-ss", f"{float(a / Fraction(fps_str)):.9f}"]
    cmd += ["-i", path, "-an", "-sn", "-dn", "-vf", join_vf(vf, f"scale={dw}:{dh}")]
    if b is not None:
        cmd += ["-frames:v", str(b - a)]
    return cmd + ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]


_DET = None  # プロセスごとの検出器（ワーカー側で遅延生成）


def _detect_range(path, vf, dw, dh, fps_str, a, b, every):
    """コマ [a, b)（b=None は最後まで）を読み、コマ番号 % every == 0 のコマで体を検出する。戻り値 [(コマ番号, dets)]。"""
    global _DET
    if _DET is None:
        _DET = Yolox()
    fsize = dw * dh * 3
    proc = subprocess.Popen(_read_cmd(path, vf, dw, dh, fps_str, a, b), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=fsize)
    out, i = [], a
    try:
        while b is None or i < b:
            buf = proc.stdout.read(fsize)
            if len(buf) < fsize:
                break
            if i % every == 0:
                out.append((i, _DET(np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 3))))
            i += 1
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0 and i == a:
        raise RuntimeError(f"体の追跡のデコードに失敗 (rc={rc}, コマ {a}〜): {err.strip()[-300:]}")
    return out


# ---------------------------------------------------------------- 追跡（ByteTrack 風の簡易版）

def iou(a, b):
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = min(ax1, bx1) - max(a[0], b[0])
    ih = min(ay1, by1) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


class Tracker:
    """高信頼→低信頼の 2 段 IoU 対応付け・直前 2 回の差分で位置を線形外挿する。"""

    def __init__(self, hold=BODY_HOLD):
        self.hold = hold
        self.active, self.tracks = [], []

    @staticmethod
    def predict(t, fi):
        obs = t["obs"]
        (f2, b2) = obs[-1]
        if len(obs) < 2:
            return b2
        (f1, b1) = obs[-2]
        k = (fi - f2) / float(f2 - f1)
        return tuple(b2[j] + (b2[j] - b1[j]) * k for j in range(4))

    @staticmethod
    def _greedy(tracks, preds, dets):
        pairs = []
        for ti, p in enumerate(preds):
            for di, d in enumerate(dets):
                v = iou(p, d)
                if v >= BODY_IOU:
                    pairs.append((v, ti, di))
        pairs.sort(reverse=True)
        ut, ud, m = set(), set(), []
        for _v, ti, di in pairs:
            if ti in ut or di in ud:
                continue
            ut.add(ti)
            ud.add(di)
            m.append((tracks[ti], dets[di]))
        return m, [t for i, t in enumerate(tracks) if i not in ut], [d for i, d in enumerate(dets) if i not in ud]

    def update(self, fi, dets):
        high = [d for d in dets if d[4] >= BODY_HIGH_CONF]
        low = [d for d in dets if d[4] < BODY_HIGH_CONF]
        preds = [self.predict(t, fi) for t in self.active]
        m1, rest_t, rest_high = self._greedy(self.active, preds, high)
        m2, _lost, _ = self._greedy(rest_t, [self.predict(t, fi) for t in rest_t], low)
        for t, d in m1 + m2:
            t["obs"].append((fi, d[:4]))
            if len(t["obs"]) > 2:
                t["obs"].pop(0)
            t["boxes"][fi] = d[:4]
            t["last_frame"] = fi
        for d in rest_high:
            t = {"id": len(self.tracks), "obs": [(fi, d[:4])], "boxes": {fi: d[:4]},
                 "first_frame": fi, "last_frame": fi}
            self.tracks.append(t)
            self.active.append(t)
        self.active = [t for t in self.active if fi - t["last_frame"] <= self.hold]


# ---------------------------------------------------------------- 本体

def track_bodies(path, info, vf, out_path):
    """動画の体を検出（BODY_EVERY コマに 1 回・BODY_WORKERS プロセスでコマ範囲を分けて並列）して追跡し、out_path に保存する。

    戻り値は体トラックの一覧 [{"id", "boxes": {コマ: {"x","y","w","h"}}}]（座標は detect_size）。"""
    ow, oh = info["out_width"], info["out_height"]
    total = info["frames"]
    if not os.path.exists(YOLOX_PATH):
        raise RuntimeError(f"体の検出モデルがありません: {YOLOX_PATH}")
    sc = min(1.0, BODY_LONG_SIDE / float(max(ow, oh)))
    dw, dh = max(2, int(round(ow * sc / 2)) * 2), max(2, int(round(oh * sc / 2)) * 2)
    every = _env_int("BODY_EVERY", 4)
    fps_str = info.get("fps_str") or "30/1"

    # 最後の範囲は b=None（nb_frames がずれていても最後まで読む・_render_seg の last と同じ考え方）
    bounds = _seg_bounds(total, _env_int("BODY_WORKERS", 4))
    jobs = [(path, vf, dw, dh, fps_str, lo, None if k == len(bounds) - 1 else hi, every)
            for k, (lo, hi) in enumerate(bounds)]
    if len(jobs) > 1:
        pool = multiprocessing.get_context("spawn").Pool(len(jobs), initializer=_pool_init)
        try:
            parts = [r.get() for r in [pool.apply_async(_detect_range, job) for job in jobs]]
        finally:
            pool.terminate()
            pool.join()
    else:
        parts = [_detect_range(*jobs[0])]

    tracker = Tracker()
    for fi, dets in sorted((fi, d) for p in parts for fi, d in p):
        tracker.update(fi, dets)
    tracks = [{"id": t["id"], "boxes": {fi: dict(zip("xywh", (round(float(v), 1) for v in b)))
                                         for fi, b in sorted(t["boxes"].items())}}
              for t in tracker.tracks]
    data = {"ver": BODY_VER, "detect_size": [dw, dh], "every": every,
            "tracks": [{"id": t["id"], "boxes": {str(fi): b for fi, b in t["boxes"].items()}} for t in tracks]}
    tmp = out_path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, out_path)
    return tracks


def load_bodies(path):
    """bodies.json → (体トラック, 幅, 高さ, every)。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    tracks = [{"id": t["id"], "boxes": {int(k): v for k, v in t["boxes"].items()}} for t in data["tracks"]]
    bw, bh = data["detect_size"]
    return tracks, bw, bh, int(data.get("every") or 1)


def link_faces(face_tracks, fw, fh, body_tracks, bw, bh, every=4):
    """顔トラック → 属する体トラックの dict（face_id → body_id）。

    顔の中心が体の枠の上 FACE_TOP_FRAC 以内なら、そのコマで重なったとみなす（体を検出していないコマは
    every 以内の近いコマの体を使う・複数の体に入るときは横の中心が近い方）。
    規則 A: 同じコマで 1 つの体に顔が 2 つ以上入ったら、最大の顔の高さの半分未満の顔は数えない（後ろの観客の誤紐づけ対策）。
    規則 B: 重なった回数が BODY_LINK_MIN 以上の体だけ採る。複数の体に重なったら回数が最大の体 1 つ。"""
    by_frame = collections.defaultdict(list)  # コマ → [(体トラック id, 正規化した枠)]
    for t in body_tracks:
        for fi, b in t["boxes"].items():
            by_frame[int(fi)].append((t["id"], (b["x"] / bw, b["y"] / bh, b["w"] / bw, b["h"] / bh)))
    per = collections.defaultdict(list)  # (コマ, 体 id) → [(顔の高さ, 顔トラック id)]
    for ft in face_tracks:
        for fi, b in ft["boxes"].items():
            fi = int(fi)
            cx, cy = (b["x"] + b["w"] / 2) / fw, (b["y"] + b["h"] / 2) / fh
            cands = None
            for off in range(max(1, every)):
                cands = by_frame.get(fi - off) or by_frame.get(fi + off)
                if cands:
                    break
            hits = [(abs(cx - (x + w / 2)), tid) for tid, (x, y, w, h) in cands or ()
                    if x <= cx <= x + w and y <= cy <= y + FACE_TOP_FRAC * h]
            if hits:
                per[(fi, min(hits)[1])].append((b["h"] / fh, ft["id"]))
    votes = collections.defaultdict(collections.Counter)  # 顔トラック id → {体 id: 重なった回数}
    for (_fi, bid), faces in per.items():
        top = max(h for h, _fid in faces)
        for h, fid in faces:
            if h >= top / 2:  # 規則 A
                votes[fid][bid] += 1
    out = {}
    for fid, c in votes.items():
        bid, n = max(c.items(), key=lambda kv: (kv[1], -kv[0]))  # 規則 B（同数なら id の小さい体）
        if n >= BODY_LINK_MIN:
            out[fid] = bid
    return out
