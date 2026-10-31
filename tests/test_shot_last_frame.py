"""場面写真（build_shots）が指定したコマそのものを切り出すこと。最後のコマでも失敗しないこと。

2026-10-31 本番: 30fps・531コマの動画で最後のコマ（530）を選ぶと -ss 17.667 になり、
最後のコマの時刻 17.6666… を越えて何も書けず「フレーム抽出に失敗」で止まった。
"""

import os
import subprocess
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from worker.engine import video  # noqa: E402

N = 531  # 30fps で 17.7 秒


def _gray_video(path):
    # コマ n の明るさ = n * 7 % 256 にしておき、切り出した画像の明るさでコマ番号を確かめる
    w, h = 64, 64
    proc = subprocess.Popen([video.FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
                             "-s", f"{w}x{h}", "-r", "30", "-i", "-", "-c:v", "libx264", "-qp", "0",
                             "-pix_fmt", "yuv420p", path], stdin=subprocess.PIPE)
    for n in range(N):
        proc.stdin.write(bytes([n * 7 % 256]) * (w * h))
    proc.stdin.close()
    assert proc.wait() == 0


def test_build_shots_exact_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "SHOT_W", 64)
    src = str(tmp_path / "v.mp4")
    _gray_video(src)
    info = video.probe(src)
    assert info["frames"] == N
    frames = [0, 1, 100, 529, 530]
    video.build_shots(src, info, "", [(f, []) for f in frames], 64, str(tmp_path / "shots"))
    for j, f in enumerate(frames):
        with Image.open(tmp_path / "shots" / f"shot{j}.jpg") as im:
            got = float(np.asarray(im.convert("L")).mean())
        assert abs(got - f * 7 % 256) < 3, (f, got)
