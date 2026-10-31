"""写真1枚モードの処理（読み込み→検出→書き出し）。色補正はしない。"""

import time

import numpy as np
from PIL import Image, ImageOps

try:  # iPhone の HEIC を Pillow で開けるようにする
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:  # 無くても JPEG/PNG は処理できる
    pass

from .detect import MIN_CONF, detect
from .mosaic import mosaic_face
from . import nsfw

MAX_LONG = 4000   # これより大きい写真は縮小してから検出する
VIEW_LONG = 2000  # 選択画面に出す画像の長辺


def prepare(src_upload, src_jpg, view_jpg, check=True):
    """アップロード画像を向き補正・RGB化・長辺4000px以下の JPEG にし、検出する。

    撮影メタデータ（位置情報など）は JPEG に書かない。
    check=False は性的な内容の判定を飛ばす（運営が確認して ⭕ を押したもの）。
    戻り値: dict(width, height, faces=[{id,x,y,w,h,conf,low}], warn, sec)
    """
    t0 = time.time()
    with Image.open(src_upload) as im0:
        im = ImageOps.exif_transpose(im0).convert("RGB")
    W, H = im.size
    if max(W, H) > MAX_LONG:
        s = MAX_LONG / float(max(W, H))
        im = im.resize((round(W * s), round(H * s)), Image.LANCZOS)
        W, H = im.size
    if check:  # 性的な内容なら nsfw.Blocked（保存する前に止める）
        nsfw.check_image(np.asarray(im, dtype=np.uint8))
    im.save(src_jpg, "JPEG", quality=95)

    faces, warn = detect(np.asarray(im, dtype=np.uint8))
    out = [{"id": i, "x": f["x"], "y": f["y"], "w": f["w"], "h": f["h"],
            "conf": round(float(f["conf"]), 3), "low": float(f["conf"]) < MIN_CONF}
           for i, f in enumerate(faces, 1)]

    s = min(1.0, VIEW_LONG / float(max(W, H)))
    v = im if s >= 1.0 else im.resize((round(W * s), round(H * s)), Image.LANCZOS)
    v.save(view_jpg, "JPEG", quality=88)
    return {"width": W, "height": H, "faces": out, "warn": warn, "sec": round(time.time() - t0, 2)}


def export(src_jpg, out_jpg, faces, keep_ids, show_low, style):
    """keep_ids 以外の表示中の枠すべてにモザイクをかけて保存する。"""
    t0 = time.time()
    with Image.open(src_jpg) as im:
        arr8 = np.array(im.convert("RGB"), dtype=np.uint8)
    keep, blur = [], []
    for f in faces:
        if f["low"] and not show_low:
            continue
        if f["id"] in keep_ids:
            keep.append(f["id"])
            continue
        mosaic_face(arr8, f, 0, style="blur" if style == "blur" else "pixel")
        blur.append(f["id"])
    Image.fromarray(arr8).save(out_jpg, "JPEG", quality=92)
    return {"keep": keep, "blur": blur, "sec": round(time.time() - t0, 2)}
