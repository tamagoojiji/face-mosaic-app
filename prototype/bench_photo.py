#!/usr/bin/env python3
"""写真1枚モードのVPS計測用ベンチ（無人・全枠にモザイク＝最悪ケース）。

画像1枚につき ①読み込み ②YuNet検出(score 0.25)+merge_faces ③conf>=0.40 の全枠に pixel
④同じく blur ⑤JPEG保存(品質92) の各段の秒数・CPU秒・ピークRSS・入出力バイト数を JSON 1行で出す。
使い方: python3 bench_photo.py <入力画像> <出力フォルダ> [--retouch-dir DIR]
"""
import argparse
import json
import os
import resource
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("out_dir")
ap.add_argument("--retouch-dir", default=os.path.expanduser("~/dev/tools/photo-retouch"))
args = ap.parse_args()

t_start = time.perf_counter()
sys.path.insert(0, args.retouch_dir)
import numpy as np  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402
from retouch import detect_faces_yunet, merge_faces, mosaic_face, YUNET_PATH  # noqa: E402

FACE_SCORE = 0.25
MOSAIC_CONF = 0.40
t_import = time.perf_counter() - t_start

r = {"name": os.path.basename(args.src), "in_bytes": os.path.getsize(args.src),
     "import_s": round(t_import, 3)}

t = time.perf_counter()
im = ImageOps.exif_transpose(Image.open(args.src)).convert("RGB")
arr8 = np.array(im, dtype=np.uint8)
r["load_s"] = round(time.perf_counter() - t, 3)
r["w"], r["h"] = im.size
r["mpix"] = round(im.size[0] * im.size[1] / 1e6, 2)

t = time.perf_counter()
yn, warn = detect_faces_yunet(arr8, YUNET_PATH, FACE_SCORE)
faces = sorted(merge_faces(yn), key=lambda f: -float(f.get("conf", 0)))
r["detect_s"] = round(time.perf_counter() - t, 3)
r["warn"] = warn
targets = [f for f in faces if float(f.get("conf", 0)) >= MOSAIC_CONF]
r["faces_all"], r["faces_mosaic"] = len(faces), len(targets)

t = time.perf_counter()
px = arr8.copy()
for f in targets:
    mosaic_face(px, f, max(4, round(f["w"] / 4)), style="pixel")
r["pixel_s"] = round(time.perf_counter() - t, 3)

t = time.perf_counter()
bl = arr8.copy()
for f in targets:
    mosaic_face(bl, f, 0, style="blur")
r["blur_s"] = round(time.perf_counter() - t, 3)

os.makedirs(args.out_dir, exist_ok=True)
stem = os.path.splitext(r["name"])[0]
t = time.perf_counter()
out_blur = os.path.join(args.out_dir, stem + "_blur.jpg")
Image.fromarray(bl).save(out_blur, quality=92)
r["save_s"] = round(time.perf_counter() - t, 3)
r["out_bytes"] = os.path.getsize(out_blur)

t = time.perf_counter()
out_px = os.path.join(args.out_dir, stem + "_pixel.jpg")
Image.fromarray(px).save(out_px, quality=92)
r["save_pixel_s"] = round(time.perf_counter() - t, 3)
r["out_pixel_bytes"] = os.path.getsize(out_px)

r["total_s"] = round(r["load_s"] + r["detect_s"] + r["pixel_s"] + r["blur_s"] + r["save_s"], 3)
r["wall_in_proc_s"] = round(time.perf_counter() - t_start, 3)
ru = resource.getrusage(resource.RUSAGE_SELF)
r["cpu_s"] = round(ru.ru_utime + ru.ru_stime, 3)
r["peak_rss_mb"] = round(ru.ru_maxrss / 1024.0, 1)  # Linux は KB 単位
print(json.dumps(r, ensure_ascii=False))
