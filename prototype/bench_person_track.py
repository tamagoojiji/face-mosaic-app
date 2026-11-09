"""体（人物）の検出＋追跡の試作ベンチ。本番コード・worker は使わない（単体で動く）。

  python prototype/bench_person_track.py --video v1080.mp4 [--tracks tracks.json] [--out result.json]

- 読み込み: ffmpeg の rawvideo パイプ（worker/engine/video.py の track_faces と同じ）。長辺 --long-side に縮小し、
  --every コマに 1 回だけ検出する
- 検出: cv2.dnn で YOLOX（Apache-2.0）の ONNX。person クラスだけ・信頼度 --conf 以上
- 追跡: ByteTrack 風の簡易版（高信頼→低信頼の 2 段 IoU 対応付け・直前 2 回の差分で位置を線形外挿）
- --tracks（本番の tracks.json）: 顔の枠の中心が体の枠の上 40% に入れば、その体に属するとみなす
- --workers N: 本番の顔検出と同じく、コマ範囲を N 分割して spawn のプロセスで検出だけ並列に行う（追跡は集めてから 1 プロセス）
- --draw out.mp4: 追跡の後に動画をもう一度読み、体トラックの枠と ID（--tracks があれば顔の枠も）を描いて書き出す
使い方・モデルの入手・VPS での測り方は bench_person_track.md。
"""

import argparse
import collections
import json
import multiprocessing
import os
import shutil
import statistics
import subprocess
import sys
import time
from fractions import Fraction

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
SEG_MIN_FRAMES = 150  # 1 分割あたりの最低コマ数（worker/engine/video.py の _seg_bounds と同じ）


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
    fps_str = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    num, den = fps_str.split("/")
    fps = float(num) / float(den or 1)
    dur = float(v.get("duration") or d["format"].get("duration") or 0)
    nb = int(v.get("nb_frames") or 0) or int(round(dur * fps))
    return w, h, fps, fps_str, dur, nb


def seg_bounds(total, n):
    """コマ範囲 [0, total) を n 個の [a, b) に分ける（worker/engine/video.py の _seg_bounds と同じ）。"""
    n = max(1, min(n, total // SEG_MIN_FRAMES))
    return [(total * k // n, total * (k + 1) // n) for k in range(n)]


def read_cmd(video, dw, dh, fps_str, a, b):
    """コマ [a, b) を長辺縮小の bgr24 で読む ffmpeg（b=None は最後まで）。シークは _thumb_range と同じ入力シーク。"""
    cmd = [FFMPEG, "-v", "error", *HWACCEL]
    if a > 0:
        cmd += ["-ss", f"{float(a / Fraction(fps_str)):.9f}"]
    cmd += ["-i", video, "-an", "-sn", "-dn", "-vf", f"scale={dw}:{dh}"]
    if b is not None:
        cmd += ["-frames:v", str(b - a)]
    return cmd + ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]


def detect_range(video, model, size, conf, dw, dh, fps_str, a, b, every, threads):
    """コマ [a, b)（b=None は最後まで）を読み、コマ番号 % every == 0 のコマで体を検出する。

    戻り値 (frames_read, [(コマ番号, dets)], デコード秒, 検出秒)。threads=1 は並列のワーカー（1 プロセス 1 スレッド）。"""
    if threads:
        cv2.setNumThreads(threads)
    det = Yolox(model, size, conf)
    fsize = dw * dh * 3
    proc = subprocess.Popen(read_cmd(video, dw, dh, fps_str, a, b), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=fsize)
    out, t_dec, t_det, i = [], 0.0, 0.0, a
    try:
        while b is None or i < b:
            t0 = time.perf_counter()
            buf = proc.stdout.read(fsize)
            t_dec += time.perf_counter() - t0
            if len(buf) < fsize:
                break
            if i % every == 0:
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 3)
                t0 = time.perf_counter()
                out.append((i, det(frame)))
                t_det += time.perf_counter() - t0
            i += 1
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        rc = proc.wait()
    if rc != 0 and i == a:
        raise RuntimeError(f"デコードに失敗 (rc={rc}, コマ {a}〜): {err.strip()[-300:]}")
    return i - a, out, t_dec, t_det


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


# ---------------------------------------------------------------- 目視用の描画

def _color(tid):
    """体 ID ごとの色（赤は「紐づいていない顔」用に避ける）。"""
    hsv = np.uint8([[[(tid * 37) % 150 + 20, 200, 255]]])
    return tuple(int(c) for c in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0])


def draw_video(out_path, video, dw, dh, fps_str, every, body_tracks, face_tracks, fw, fh, f2b):
    """体トラックの枠と ID、顔の枠（紐づいた体の ID・紐づいていない顔は赤）を縮小コマに描いて mp4 にする。

    体は検出したコマにだけ枠があるので、間のコマは直前の検出コマ（コマ番号 - コマ番号 % every）の枠を出す。"""
    bodies = collections.defaultdict(list)
    for t in body_tracks:
        for fi, b in t["boxes"].items():
            bodies[fi].append((t["id"], b))
    faces = collections.defaultdict(list)
    for ft in face_tracks or ():
        for fi, b in ft["boxes"].items():
            faces[fi].append((f2b.get(ft["id"]), b))
    kx, ky = (dw / float(fw), dh / float(fh)) if face_tracks else (1.0, 1.0)
    fsize = dw * dh * 3
    rd = subprocess.Popen(read_cmd(video, dw, dh, fps_str, 0, None), stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, bufsize=fsize)
    wr = subprocess.Popen([FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                           "-s", f"{dw}x{dh}", "-framerate", fps_str, "-i", "-", "-r", "30",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                           out_path], stdin=subprocess.PIPE)
    font = cv2.FONT_HERSHEY_SIMPLEX
    i = 0
    try:
        while True:
            buf = rd.stdout.read(fsize)
            if len(buf) < fsize:
                break
            img = np.frombuffer(buf, dtype=np.uint8).reshape(dh, dw, 3).copy()
            for tid, (x, y, w, h) in bodies.get(i - i % every, ()):
                c = _color(tid)
                cv2.rectangle(img, (int(x), int(y)), (int(x + w), int(y + h)), c, 2)
                cv2.putText(img, str(tid), (int(x) + 2, int(y) + 16), font, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(img, str(tid), (int(x) + 2, int(y) + 16), font, 0.55, c, 1, cv2.LINE_AA)
            for bid, b in faces.get(i, ()):
                x0, y0 = int(b["x"] * kx), int(b["y"] * ky)
                x1, y1 = int((b["x"] + b["w"]) * kx), int((b["y"] + b["h"]) * ky)
                c = (0, 0, 255) if bid is None else _color(bid)
                cv2.rectangle(img, (x0, y0), (x1, y1), c, 1)
                if bid is not None:
                    cv2.putText(img, str(bid), (x0, max(10, y0 - 3)), font, 0.35, c, 1, cv2.LINE_AA)
            cv2.putText(img, f"{i}", (4, dh - 6), font, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            wr.stdin.write(img.tobytes())
            i += 1
    finally:
        rd.stdout.close()
        rd.kill()
        rd.wait()
        wr.stdin.close()
        rc = wr.wait()
    if rc != 0:
        raise SystemExit(f"描画の書き出しに失敗 (rc={rc})")
    return i


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
    ap.add_argument("--threads", type=int, default=0, help="cv2 のスレッド数（0=OpenCV の既定。--workers 2 以上では各ワーカー 1）")
    ap.add_argument("--workers", type=int, default=1, help="検出を並列にするプロセス数（コマ範囲で分割・1 範囲あたり最低 150 コマ）")
    ap.add_argument("--draw", help="体の ID・顔の紐づきを描いた mp4（時間の計測には含めない）")
    a = ap.parse_args()

    if a.threads:
        cv2.setNumThreads(a.threads)
    name = os.path.basename(a.model)
    size = a.input_size or (416 if ("tiny" in name or "nano" in name) else 640)
    every = max(1, a.every)

    w, h, fps, fps_str, dur, nb = probe(a.video)
    sc = min(1.0, a.long_side / float(max(w, h)))
    dw, dh = max(2, int(round(w * sc / 2)) * 2), max(2, int(round(h * sc / 2)) * 2)

    # 最後の範囲は b=None（nb_frames がずれていても最後まで読む・_render_seg の last と同じ考え方）
    bounds = seg_bounds(nb, max(1, a.workers))
    jobs = [(a.video, a.model, size, a.conf, dw, dh, fps_str, lo, None if k == len(bounds) - 1 else hi, every,
             1 if len(bounds) > 1 else 0) for k, (lo, hi) in enumerate(bounds)]
    t_all = time.perf_counter()
    if len(jobs) > 1:
        pool = multiprocessing.get_context("spawn").Pool(len(jobs))
        try:
            parts = [r.get() for r in [pool.apply_async(detect_range, job) for job in jobs]]
        finally:
            pool.terminate()
            pool.join()
    else:
        try:
            parts = [detect_range(*jobs[0])]
        except RuntimeError as e:
            raise SystemExit(str(e))
    t_det_wall = time.perf_counter() - t_all
    i = sum(p[0] for p in parts)
    t_dec = sum(p[2] for p in parts)  # 並列のときはワーカーの合計（CPU 側の目安）
    t_det = sum(p[3] for p in parts)
    detected = sorted((fi, d) for p in parts for fi, d in p[1])

    tracker = Tracker(a.hold)
    t0 = time.perf_counter()
    for fi, dets in detected:
        tracker.update(fi, dets)
    t_trk = time.perf_counter() - t0
    t_all = time.perf_counter() - t_all
    n_det_frames = len(detected)
    n_dets = sum(len(d) for _fi, d in detected)

    dur = dur or i / fps
    per_min = 60.0 / dur if dur else 0.0
    spans = [t["last_frame"] - t["first_frame"] + 1 for t in tracker.tracks]
    res = {
        "input": {"video": os.path.basename(a.video), "width": w, "height": h, "fps": round(fps, 3),
                  "duration_sec": round(dur, 2), "frames": i},
        "settings": {"model": name, "input_size": size, "long_side": a.long_side, "detect_size": [dw, dh],
                     "every": every, "conf": a.conf, "high_conf": HIGH_CONF, "iou": IOU_MATCH,
                     "hold": a.hold, "threads": 1 if len(jobs) > 1 else cv2.getNumThreads(),
                     "workers": len(jobs), "segments": [[lo, hi] for lo, hi in bounds]},
        # decode_resize・detect は並列のときワーカーの合計。detect_wall は検出（デコード込み）の経過時間
        "time_sec": {"decode_resize": round(t_dec, 2), "detect": round(t_det, 2), "detect_wall": round(t_det_wall, 2),
                     "track": round(t_trk, 3), "total_wall": round(t_all, 2)},
        "time_sec_per_video_min": {"decode_resize": round(t_dec * per_min, 2), "detect": round(t_det * per_min, 2),
                                   "detect_wall": round(t_det_wall * per_min, 2),
                                   "track": round(t_trk * per_min, 3), "total_wall": round(t_all * per_min, 2)},
        "detect": {"frames": n_det_frames, "detections": n_dets,
                   "ms_per_frame": round(1000 * t_det / max(n_det_frames, 1), 1)},
        "body_tracks": {"count": len(spans), "median_span_frames": statistics.median(spans) if spans else 0,
                        "span_ge_30": sum(1 for s in spans if s >= 30)},
    }

    fts, fw, fh, f2b = None, 0, 0, {}
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

    if a.draw:
        t0 = time.perf_counter()
        n = draw_video(a.draw, a.video, dw, dh, fps_str, every, tracker.tracks, fts, fw, fh, f2b)
        res["time_sec"]["draw"] = round(time.perf_counter() - t0, 2)
        res["draw"] = {"path": os.path.basename(a.draw), "frames": n}

    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
