"""顔の検出（YuNet のみ）。

~/dev/tools/photo-retouch/retouch.py の detect_faces_yunet / _overlap / merge_faces と
classify_faces の「主役の胴体ゾーン」判定をコピーしたもの（2026-10-24）。
Apple Vision 依存は落としている。判定の数値は元と同じ。
"""

import os

import numpy as np

YUNET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "models", "yunet.onnx")

# 検出のしきい値（試作 detect_only.py の FACE_SCORE と同じ）
FACE_SCORE = 0.25
# 候補として最初から見せる確信度（DESIGN 2節）
MIN_CONF = 0.40
# 「主役の顔」とみなす最小サイズ（短辺比）
MAIN_SUBJECT_FRAC = 0.03
# 主役の基準にしてよい検出の最低 confidence
MAIN_CONF = 0.5
# YuNet が返す顔の最小サイズ（短辺比）
YUNET_MIN_FRAC = 0.01
# 主役の胴体ゾーン内の検出を信じる最低 confidence
BODY_ZONE_CONF = 0.9

# YuNet 検出器のキャッシュ（(w, h, score) ごとに1つ。毎コマのモデル読み直しを避ける）
_YUNET_CACHE = {}


def detect_faces_yunet(rgb_uint8, model_path=YUNET_PATH, score=0.3, upscale=2):
    """OpenCV YuNet で顔矩形を取る。戻り値は (faces, warning)。"""
    try:
        import cv2
    except Exception as e:
        return [], f"cv2 が使えないため YuNet をスキップ: {e}"
    if not os.path.exists(model_path):
        return [], f"YuNet モデルが無いためスキップ: {model_path}"

    h0, w0 = rgb_uint8.shape[:2]
    long_side = max(h0, w0)
    scale = float(upscale) if long_side < 1600 else (0.75 if long_side > 4000 else 1.0)
    bgr = rgb_uint8[:, :, ::-1]
    if abs(scale - 1.0) > 1e-6:
        interp = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=interp)
    bgr = np.ascontiguousarray(bgr)
    h, w = bgr.shape[:2]

    try:
        key = (w, h, float(score))
        det = _YUNET_CACHE.get(key)
        if det is None:
            det = cv2.FaceDetectorYN.create(model_path, "", (w, h), score_threshold=float(score),
                                            nms_threshold=0.3, top_k=5000)
            _YUNET_CACHE[key] = det
        _, raw = det.detect(bgr)
    except Exception as e:
        return [], f"YuNet の検出に失敗: {e}"
    if raw is None:
        return [], None

    min_h = YUNET_MIN_FRAC * min(h0, w0)
    faces = []
    for row in raw:
        x, y, bw, bh = (float(v) / scale for v in row[:4])
        if bw < 2.0 or bh < min_h:
            continue
        x0 = int(max(0, round(x)))
        y0 = int(max(0, round(y)))
        x1 = int(min(w0, round(x + bw)))
        y1 = int(min(h0, round(y + bh)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        faces.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                      "conf": round(float(row[-1]), 3), "src": "yunet"})
    return sorted(faces, key=lambda f: -f["h"]), None


def _overlap(a, b):
    """(IoU, 小さい方に対する重なり率) を返す。"""
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    inter = float(ix * iy)
    if inter <= 0.0:
        return 0.0, 0.0
    aa, ab = float(a["w"] * a["h"]), float(b["w"] * b["h"])
    return inter / (aa + ab - inter), inter / min(aa, ab)


def merge_faces(faces, iou_thr=0.4, contain_thr=0.6):
    """重複した検出を統合する（重複は conf の高い方を残す）。"""
    kept = []
    for f in sorted(faces, key=lambda f: -float(f.get("conf", 0.0))):
        dup = False
        for g in kept:
            iou, contain = _overlap(f, g)
            if iou > iou_thr or contain > contain_thr:
                dup = True
                break
        if not dup:
            kept.append(f)
    return sorted(kept, key=lambda f: -f["h"])


def drop_main_body_zone(faces, img_short):
    """主役の胴体ゾーンに中心が入る、確信度 BODY_ZONE_CONF 未満の枠を外す。

    retouch.py classify_faces の「主役あり」分岐と同じ条件（主役＝confidence MAIN_CONF 以上で
    最も高さのある顔、かつ短辺の MAIN_SUBJECT_FRAC 以上）。
    """
    cands = [f for f in faces if float(f.get("conf", 0.0)) >= MAIN_CONF]
    if not cands:
        return faces
    main = max(cands, key=lambda f: f["h"])
    if main["h"] < MAIN_SUBJECT_FRAC * img_short:
        return faces
    zx0, zx1 = main["x"] - 0.75 * main["w"], main["x"] + 1.75 * main["w"]
    zy0, zy1 = main["y"] + 0.5 * main["h"], main["y"] + 3.0 * main["h"]
    out = []
    for f in faces:
        if f is not main and float(f.get("conf", 0.0)) < BODY_ZONE_CONF:
            cx, cy = f["x"] + f["w"] / 2.0, f["y"] + f["h"] / 2.0
            if zx0 <= cx <= zx1 and zy0 <= cy <= zy1:
                continue
        out.append(f)
    return out


def detect(rgb_uint8):
    """写真1枚の候補枠を確信度の高い順に返す。戻り値は (faces, warning)。"""
    yn, warn = detect_faces_yunet(rgb_uint8, YUNET_PATH, FACE_SCORE)
    faces = merge_faces(yn)
    faces = drop_main_body_zone(faces, min(rgb_uint8.shape[:2]))
    return sorted(faces, key=lambda f: -float(f.get("conf", 0))), warn
