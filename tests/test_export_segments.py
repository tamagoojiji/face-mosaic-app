"""書き出しの分割並列（render・render_hq）で、コマの抜け・重複・ずれが無いこと。

各コマに通し番号を焼き込んだ合成動画を作り、出力をデコードして「焼き込み番号 == コマ番号」を全コマで確かめる。
番号は上端の 12 個のブロック（白=1・黒=0 の2進数）で焼く（非可逆の書き出しでも読み取れるように）。
"""

import json
import os
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from worker.engine import video  # noqa: E402

W, H = 320, 240
BITS, BS = 12, 20  # ブロック数・ブロックの一辺
BOX = {"x": 120, "y": 140, "w": 60, "h": 60}  # ③で枠を置く場所（縞模様の上）


def _frame(i):
    a = np.full((H, W, 3), 128, dtype=np.uint8)
    for k in range(BITS):
        a[0:BS, k * BS:(k + 1) * BS] = 255 if (i >> k) & 1 else 0
    xs = np.arange(W)
    a[100:, :] = (128 + 100 * np.sin(2 * np.pi * xs / 24.0)).astype(np.uint8)[None, :, None]
    return a


def _make(path, fps, sec):
    n = fps * sec
    cmd = [video.FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{W}x{H}",
           "-framerate", str(fps), "-i", "-", "-f", "lavfi", "-i", f"sine=frequency=440:duration={sec}",
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(_frame(i).tobytes())
    p.stdin.close()
    assert p.wait() == 0
    return n


def _decode(path):
    r = subprocess.run([video.FFMPEG, "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, H, W, 3)


def _numbers(frames):
    c = BS // 2
    bits = frames[:, c - 4:c + 4, :, 0].astype(np.float32)
    out = np.zeros(len(frames), dtype=np.int64)
    for k in range(BITS):
        v = bits[:, :, k * BS + c - 4:k * BS + c + 4].mean(axis=(1, 2))
        out |= (v > 128).astype(np.int64) << k
    return out


def _has_audio(path):
    r = subprocess.run([video.FFPROBE, "-v", "error", "-show_streams", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    return any(s["codec_type"] == "audio" for s in json.loads(r.stdout)["streams"])


def _check_all(out, n):
    fr = _decode(out)
    assert len(fr) == n
    assert np.array_equal(_numbers(fr), np.arange(n))
    assert _has_audio(out)
    return fr


@pytest.fixture(scope="module")
def src30(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("seg") / "src30.mp4")
    return p, _make(p, 30, 16)  # 480 コマ → 3 分割（1 分割 150 コマ以上）


def test_render_segments_frame_exact(src30, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_SEGMENTS", "3")
    src, n = src30
    info = video.probe(src)
    assert len(video._seg_bounds(info["frames"], 3)) == 3
    out = str(tmp_path / "out.mp4")
    r = video.render(src, info, "", {}, out, "libx264", "pixel")
    assert r["frames_written"] == n and r["frames_blurred"] == 0
    _check_all(out, n)
    assert not [f for f in os.listdir(tmp_path) if ".seg" in f or f.endswith(".list.txt")]


def test_render_hq_segments_frame_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("HQ_SEGMENTS", "2")
    src = str(tmp_path / "src60.mp4")
    n = _make(src, 60, 12)
    info = video.probe(src)
    assert len(video._seg_bounds(int(round(info["duration"] * info["fps"])), 2)) == 2
    out = str(tmp_path / "out_hq.mp4")
    r = video.render_hq(src, info, "", [], 1.0, 1.0, out, "pixel")
    assert r["frames_written"] == n and r["frames_blurred"] == 0
    _check_all(out, n)


def test_render_segments_boxes_use_absolute_frame(src30, tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_SEGMENTS", "3")
    src, n = src30
    info = video.probe(src)
    b1 = video._seg_bounds(info["frames"], 3)[0][1]  # 分割 2 の先頭
    out = str(tmp_path / "out.mp4")
    r = video.render(src, info, "", {b1 - 1: [dict(BOX)], b1: [dict(BOX)]}, out, "libx264", "pixel")
    assert r["frames_blurred"] == 2
    fr = _check_all(out, n)
    ref = _frame(0)[150:190, 130:170].astype(np.float32)  # 縞模様は全コマ同じ
    diff = {f: float(np.abs(fr[f, 150:190, 130:170].astype(np.float32) - ref).mean())
            for f in (b1 - 2, b1 - 1, b1, b1 + 1)}
    assert diff[b1 - 1] > 15 and diff[b1] > 15, diff
    assert diff[b1 - 2] < 5 and diff[b1 + 1] < 5, diff
