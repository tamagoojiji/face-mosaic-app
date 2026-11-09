"""体（人物）の検出＋追跡の試作ベンチ。本番コード・worker は使わない（単体で動く）。

  python prototype/bench_person_track.py --video v1080.mp4 [--tracks tracks.json] [--out result.json]

- 読み込み: ffmpeg の rawvideo パイプ（worker/engine/video.py の track_faces と同じ）。長辺 --long-side に縮小し、
  --every コマに 1 回だけ検出する
- 検出: cv2.dnn で YOLOX（Apache-2.0）の ONNX。person クラスだけ・信頼度 --conf 以上
- 追跡: ByteTrack 風の簡易版（高信頼→低信頼の 2 段 IoU 対応付け・直前 2 回の差分で位置を線形外挿）
- --tracks（本番の tracks.json）: 顔の枠の中心が体の枠の上 40% に入れば、その体に属するとみなす
使い方・モデルの入手・VPS での測り方は bench_person_track.md。
"""

import argparse
import collections
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
HWACCEL = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []

HIGH_CONF = 0.5   # 1 段目の対応付け・新規トラックに使う検出
IOU_MATCH = 0.3
NMS_IOU = 0.45
FACE_TOP_FRAC = 0.4  # 顔の中心が体の枠の上 40% 以内なら、その体に属する


def probe(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_streams",
                        "-show_format", "-of", "json", path], capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    v = d["streams"][0]
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) % 180 == 90:  # ffmpeg の自動回転後の向き
        w, h = h, w
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den or 1)
    dur = float(v.get("duration") or d["format"].get("duration") or 0)
    return w, h, fps, dur


# ---------------------------------------------------------------- 検出（YOLOX）

class Yolox:
    def __init__(self, model, size, conf):
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
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), sc.tolist(), self.conf, NMS_IOU)
        res = []
        for k in np.array(keep).reshape(-1):
            x, y, bw, bh = boxes[k]
            x0, y0 = max(0.0, x), max(0.0, y)
            x1, y1 = min(float(w), x + bw), min(float(h), y + bh)
            if x1 > x0 and y1 > y0:
                res.append((x0, y0, x1 - x0, y1 - y0, float(sc[k])))
        return res


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
    def __init__(self, hold):
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
                if v >= IOU_MATCH:
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
        high = [d for d in dets if d[4] >= HIGH_CONF]
        low = [d for d in dets if d[4] < HIGH_CONF]
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


# ---------------------------------------------------------------- 顔トラックとの対応付け

def load_face_tracks(p):
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    fw, fh = data["detect_size"]
    return [{"id": t["id"], "boxes": {int(k): v for k, v in t["boxes"].items()}} for t in data["tracks"]], fw, fh


def link_faces(face_tracks, fw, fh, body_tracks, bw, bh, every):
    by_frame = collections.defaultdict(list)  # コマ → [(体トラック id, 正規化した枠)]
    for t in body_tracks:
        for fi, b in t["boxes"].items():
            by_frame[fi].append((t["id"], (b[0] / bw, b[1] / bh, b[2] / bw, b[3] / bh)))
    face_to_body = {}
    for ft in face_tracks:
        votes = collections.Counter()
        for fi, b in ft["boxes"].items():
            cx, cy = (b["x"] + b["w"] / 2) / fw, (b["y"] + b["h"] / 2) / fh
            cands = None
            for off in range(every):  # 体を検出していないコマは近いコマの体を使う
                cands = by_frame.get(fi - off) or by_frame.get(fi + off)
                if cands:
                    break
            hits = [(abs(cx - (x + w / 2)), tid) for tid, (x, y, w, h) in cands or ()
                    if x <= cx <= x + w and y <= cy <= y + FACE_TOP_FRAC * h]
            if hits:  # 複数の体に入るときは横の中心が近い方
                votes[min(hits)[1]] += 1
        if votes:
            face_to_body[ft["id"]] = votes.most_common(1)[0][0]
    return face_to_body


def hist(values, bins=((1, 1), (2, 2), (3, 5), (6, 10), (11, 30), (31, 10 ** 9))):
    out = {}
    for lo, hi in bins:
        key = str(lo) if lo == hi else (f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+")
        out[key] = sum(1 for v in values if lo <= v <= hi)
    return out


# ---------------------------------------------------------------- 本体

def main():
    ap = argparse.ArgumentParser(description="体の検出＋追跡の時間と顔トラックとの紐づきを測る")
    ap.add_argument("--video", required=True)
    ap.add_argument("--tracks", help="本番と同じ形式の tracks.json（同じ動画・同じコマ番号のもの）")
    ap.add_argument("--out", help="結果の JSON")
    ap.add_argument("--model", default=os.path.join(HERE, "models", "yolox_tiny.onnx"))
    ap.add_argument("--input-size", type=int, default=0, help="モデルの入力（0=名前から: tiny/nano は 416・他は 640）")
    ap.add_argument("--long-side", type=int, default=640)
    ap.add_argument("--every", type=int, default=2)
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--hold", type=int, default=30)
    ap.add_argument("--threads", type=int, default=0, help="cv2 のスレッド数（0=OpenCV の既定）")
    a = ap.parse_args()

    if a.threads:
        cv2.setNumThreads(a.threads)
    name = os.path.basename(a.model)
    size = a.input_size or (416 if ("tiny" in name or "nano" in name) else 640)
    det = Yolox(a.model, size, a.conf)
    every = max(1, a.every)

    w, h, fps, dur = probe(a.video)
    sc = min(1.0, a.long_side / float(max(w, h)))
    dw, dh = max(2, int(round(w * sc / 2)) * 2), max(2, int(round(h * sc / 2)) * 2)
    cmd = [FFMPEG, "-v", "error", *HWACCEL, "-i", a.video, "-an", "-sn", "-dn",
           "-vf", f"scale={dw}:{dh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    fsize = dw * dh * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=fsize)

    tracker = Tracker(a.hold)
    t_dec = t_det = t_trk = 0.0
    n_det_frames = n_dets = 0
    i = 0
    t_all = time.perf_counter()
    try:
        while True:
            t0 = time.perf_counter()
            buf = proc.stdout.read(fsize)
            t_dec += time.perf_counter() - t0
            if len(buf) < fsize:
                break
            if i % every == 0:
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 3)
                t0 = time.perf_counter()
                dets = det(frame)
                t1 = time.perf_counter()
                tracker.update(i, dets)
                t_trk += time.perf_counter() - t1
                t_det += t1 - t0
                n_det_frames += 1
                n_dets += len(dets)
            i += 1
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        rc = proc.wait()
    t_all = time.perf_counter() - t_all
    if rc != 0 and i == 0:
        raise SystemExit(f"デコードに失敗 (rc={rc}): {err.strip()[-300:]}")

    dur = dur or i / fps
    per_min = 60.0 / dur if dur else 0.0
    spans = [t["last_frame"] - t["first_frame"] + 1 for t in tracker.tracks]
    res = {
        "input": {"video": os.path.basename(a.video), "width": w, "height": h, "fps": round(fps, 3),
                  "duration_sec": round(dur, 2), "frames": i},
        "settings": {"model": name, "input_size": size, "long_side": a.long_side, "detect_size": [dw, dh],
                     "every": every, "conf": a.conf, "high_conf": HIGH_CONF, "iou": IOU_MATCH,
                     "hold": a.hold, "threads": cv2.getNumThreads()},
        "time_sec": {"decode_resize": round(t_dec, 2), "detect": round(t_det, 2), "track": round(t_trk, 3),
                     "total_wall": round(t_all, 2)},
        "time_sec_per_video_min": {"decode_resize": round(t_dec * per_min, 2), "detect": round(t_det * per_min, 2),
                                   "track": round(t_trk * per_min, 3), "total_wall": round(t_all * per_min, 2)},
        "detect": {"frames": n_det_frames, "detections": n_dets,
                   "ms_per_frame": round(1000 * t_det / max(n_det_frames, 1), 1)},
        "body_tracks": {"count": len(spans), "median_span_frames": statistics.median(spans) if spans else 0,
                        "span_ge_30": sum(1 for s in spans if s >= 30)},
    }

    if a.tracks:
        t0 = time.perf_counter()
        fts, fw, fh = load_face_tracks(a.tracks)
        f2b = link_faces(fts, fw, fh, tracker.tracks, dw, dh, every)
        per_body = collections.Counter(f2b.values())
        n = len(fts)
        long_ids = {ft["id"] for ft in fts if len(ft["boxes"]) >= 5}
        res["faces"] = {
            "face_tracks": n,
            "linked": len(f2b),
            "linked_ratio": round(len(f2b) / n, 3) if n else 0.0,
            "unlinked": n - len(f2b),
            # 検出 5 回以上の顔トラックだけ（1〜2 コマの誤検出・横顔の断片を除いた参考値）
            "seen_ge_5": {"face_tracks": len(long_ids), "linked": sum(1 for k in f2b if k in long_ids)},
            "bodies_with_faces": len(per_body),
            "faces_per_body_hist": hist(list(per_body.values())),
            "faces_per_body_median": statistics.median(per_body.values()) if per_body else 0,
            "faces_per_body_max": max(per_body.values()) if per_body else 0,
            "link_sec": round(time.perf_counter() - t0, 2),
        }

    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
