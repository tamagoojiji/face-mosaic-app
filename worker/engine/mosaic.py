"""顔のモザイク（色補正なし）。

~/dev/tools/photo-retouch/retouch.py の mosaic_face と、それが使うぼかし関数をコピーしたもの
（2026-10-24）。変更点は1つだけ: 既定を「ブロック粗め＝顔ごとに block=max(4, round(w/4))」にした
（DESIGN 2節）。
"""

import functools
import math

import cv2
import numpy as np
from PIL import Image


def _box_sizes(sigma, n=3):
    w_ideal = math.sqrt(12.0 * sigma * sigma / n + 1.0)
    wl = int(math.floor(w_ideal))
    if wl % 2 == 0:
        wl -= 1
    wl = max(wl, 1)
    wu = wl + 2
    m_ideal = (12.0 * sigma * sigma - n * wl * wl - 4.0 * n * wl - 3.0 * n) / (-4.0 * wl - 4.0)
    m = int(round(m_ideal))
    return [wl if i < m else wu for i in range(n)]


def _box_blur_axis(a, r, axis):
    if r < 1:
        return a
    n = a.shape[axis]
    r = min(r, max(n - 1, 1))
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    ap = np.pad(a, pad, mode="edge")
    c = np.cumsum(ap, axis=axis, dtype=np.float32)
    zshape = list(c.shape)
    zshape[axis] = 1
    c = np.concatenate([np.zeros(zshape, dtype=np.float32), c], axis=axis)
    sl_hi = [slice(None)] * a.ndim
    sl_lo = [slice(None)] * a.ndim
    sl_hi[axis] = slice(2 * r + 1, 2 * r + 1 + n)
    sl_lo[axis] = slice(0, n)
    return ((c[tuple(sl_hi)] - c[tuple(sl_lo)]) / float(2 * r + 1)).astype(np.float32)


def _box_gauss(a, sigma):
    mean = float(a.mean())
    x = (a - mean).astype(np.float32)
    for w in _box_sizes(sigma):
        r = (w - 1) // 2
        x = _box_blur_axis(x, r, 0)
        x = _box_blur_axis(x, r, 1)
    return (x + mean).astype(np.float32)


def gaussian_blur(a, sigma):
    if sigma < 0.4:
        return a.copy()
    h, w = a.shape
    k = 1
    if sigma > 6.0:
        k = int(max(1, min(8, sigma // 4)))
    if k > 1:
        sw, sh = max(4, w // k), max(4, h // k)
        small = np.asarray(Image.fromarray(a, "F").resize((sw, sh), Image.BILINEAR),
                           dtype=np.float32)
        blurred = _box_gauss(small, sigma / k)
        return np.asarray(Image.fromarray(blurred, "F").resize((w, h), Image.BILINEAR),
                          dtype=np.float32)
    return _box_gauss(a, sigma)


def smoothstep(x, lo, hi):
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def _blur_rgb(region, sigma):
    """gaussian_blur と同じ「縮小→ぼかし→拡大」を cv2 で3色まとめて行う（動画の書き出しで毎コマ呼ばれるため）。"""
    h, w = region.shape[:2]
    k = int(max(1, min(8, sigma // 4))) if sigma > 6.0 else 1
    src = region.astype(np.float32)
    if k > 1:
        small = cv2.resize(src, (max(4, w // k), max(4, h // k)), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (0, 0), sigma / k, borderType=cv2.BORDER_REPLICATE)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.GaussianBlur(src, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE)


@functools.lru_cache(maxsize=256)
def _grain(rh, rw):
    """すりガラスの微粒ノイズ（種が固定なので大きさごとに同じ）。"""
    rng = np.random.default_rng(20260921)
    grain = rng.uniform(-1.0, 1.0, size=(rh, rw)).astype(np.float32)
    grain = gaussian_blur(grain, 0.6)
    peak = float(np.abs(grain).max())
    if peak > 1e-6:
        grain *= 3.0 / peak
    grain.flags.writeable = False
    return grain


@functools.lru_cache(maxsize=256)
def _ellipse_mask(rh, rw):
    """楕円（縁はソフト）の重み。大きさごとに同じ。"""
    yy = (np.arange(rh, dtype=np.float32)[:, None] - (rh - 1) / 2.0) / (rh / 2.0)
    xx = (np.arange(rw, dtype=np.float32)[None, :] - (rw - 1) / 2.0) / (rw / 2.0)
    d = np.sqrt(xx * xx + yy * yy)
    mask = 1.0 - smoothstep(d, 0.8, 1.0)
    mask.flags.writeable = False
    return mask


def mosaic_face(arr8, box, block=0, style="pixel"):
    """顔矩形を 25% 拡大した楕円（縁は矩形幅の10%でソフト）内を隠す。

    style="pixel" … ブロック粗め（既定）。block=0 なら顔ごとに max(4, round(顔幅/4))。
    style="blur"  … すりガラス（顔幅の1/4のガウスぼかし＋微粒ノイズ）。
    戻り値は記録用の dict。領域が小さすぎて何もしなかったときは None。
    """
    H, W = arr8.shape[:2]
    x0 = int(math.floor(box["x"] - 0.25 * box["w"]))
    y0 = int(math.floor(box["y"] - 0.25 * box["h"]))
    x1 = int(math.ceil(box["x"] + 1.25 * box["w"]))
    y1 = int(math.ceil(box["y"] + 1.25 * box["h"]))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    rw, rh = x1 - x0, y1 - y0
    if rw < 4 or rh < 4:
        return None

    region = arr8[y0:y1, x0:x1]
    info = {}

    if style == "pixel":
        b = block if block > 0 else max(4, int(round(box["w"] / 4.0)))
        b = max(2, min(b, rw, rh))
        small = Image.fromarray(region).resize(
            (max(1, rw // b), max(1, rh // b)), Image.BOX)
        fill = np.asarray(small.resize((rw, rh), Image.NEAREST), dtype=np.float32)
        info["block"] = b
    else:
        sigma = max(3.0, 0.25 * box["w"])
        fill = _blur_rgb(region, sigma) + _grain(rh, rw)[:, :, None]
        np.clip(fill, 0.0, 255.0, out=fill)
        info["sigma"] = round(sigma, 2)

    mask = _ellipse_mask(rh, rw)[:, :, None]

    arr8[y0:y1, x0:x1] = np.rint(
        region.astype(np.float32) * (1.0 - mask) + fill * mask).astype(np.uint8)
    return info
