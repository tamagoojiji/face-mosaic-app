"""アダルト（性的な）内容の判定（Yahoo open_nsfw・BSD-2・OpenCV の dnn で動かす）。

models/nsfw.onnx は open_nsfw の Caffe 版（resnet_50_1by2）を ONNX に変換したもの（OpenCV 5 は Caffe を読めないため。
変換後の出力差は最大 3e-9）。ライセンスは models/nsfw-LICENSE.md。

判定するのは性的な内容だけ。暴力・ゾンビなどの仮装・誹謗中傷は判定しない（運営が通報などで確認して削除する）。
前処理は open_nsfw の classify_nsfw.py と同じ（256x256 に縮小 → 中央 224x224 → BGR・平均値を引く）。
"""

import os

import numpy as np

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "models")
MODEL = os.path.join(MODEL_DIR, "nsfw.onnx")

THRESHOLD = 0.8      # この確率以上のコマを「性的」とみなす（open_nsfw の目安: 0.8 以上はほぼ確実）
VIDEO_HITS = 2       # 動画はこのコマ数以上で止める（1コマだけの誤判定で止めない）
VIDEO_STEP_SEC = 2.0  # 動画は2秒に1コマ調べる
FRAMES_KEEP = 3       # 止めたときに運営の確認用に残すコマ数（確率の高い順）
BLOCK_MSG = "この内容は処理できません（利用規約 第7条）。送った動画・写真は削除しました"

_NET = None


class Blocked(Exception):
    """性的な内容と判定した。frames は確率の高いコマ（BGR・最大 FRAMES_KEEP 枚）で、運営の確認画面に出す。"""

    def __init__(self, msg, frames=()):
        super().__init__(msg)
        self.frames = list(frames)


def _net():
    global _NET
    if _NET is None:
        import cv2
        _NET = cv2.dnn.readNetFromONNX(MODEL)
    return _NET


def score_bgr(bgr):
    """BGR の画像（uint8）1枚の「性的」確率 0〜1。"""
    import cv2
    im = cv2.resize(bgr, (256, 256), interpolation=cv2.INTER_LINEAR)[16:240, 16:240]
    blob = cv2.dnn.blobFromImage(im, 1.0, (224, 224), (104, 117, 123), swapRB=False, crop=False)
    net = _net()
    net.setInput(blob)
    return float(net.forward()[0][1])


def check_image(rgb):
    """写真1枚。性的と判定したら Blocked。戻り値は確率。"""
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    s = score_bgr(bgr)
    if s >= THRESHOLD:
        raise Blocked(f"写真 score={s:.2f}", [bgr])
    return s


def check_video(path):
    """動画を2秒に1コマ調べる。性的なコマが VIDEO_HITS 以上（短くて調べたコマが少なければその数）で Blocked。

    戻り値: dict(frames=調べたコマ数, hits=該当コマ数, max=最大の確率)
    """
    import cv2
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, round(fps * VIDEO_STEP_SEC))
        frames = hits = 0
        top = 0.0
        best = []  # (確率, コマ番号, BGR) の上位 FRAMES_KEEP 件
        for i in range(0, max(n, 1), step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, bgr = cap.read()
            if not ok:
                break
            s = score_bgr(bgr)
            frames += 1
            top = max(top, s)
            if s >= THRESHOLD:
                hits += 1
                best = sorted(best + [(s, i, bgr)], key=lambda t: -t[0])[:FRAMES_KEEP]
    finally:
        cap.release()
    if frames and hits >= min(VIDEO_HITS, frames):
        raise Blocked(f"動画 {hits}/{frames} コマ max={top:.2f}", [b for _, _, b in best])
    return {"frames": frames, "hits": hits, "max": round(top, 3)}
