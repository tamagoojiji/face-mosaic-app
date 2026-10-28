"""顔の追跡・特徴の並列化（TRACK_WORKERS / FEAT_WORKERS）で結果が直列と同じこと。

実写の入力が要るので、FM_TEST_SRC（既定はスクラッチの src300.mov）が無ければ飛ばす。
切り出した 8 秒は入力の隣に置く（実在の顔が写っているため git には入れない）。
"""

import os
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from worker.engine import video  # noqa: E402

SRC = os.environ.get("FM_TEST_SRC", "/private/tmp/claude-501/-Users-yontsuhashikunihiko-dev-tamago-face-mosaic-app/"
                     "ff689d27-65f8-40ad-b14a-286ec47662d5/scratchpad/bench/src300.mov")

pytestmark = pytest.mark.skipif(not os.path.exists(SRC), reason=f"入力動画がありません: {SRC}")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    d = os.path.dirname(SRC)
    c8 = os.path.join(d, "test_clip8.mov")
    if not os.path.exists(c8):
        subprocess.run([video.FFMPEG, "-v", "error", "-y", "-ss", "0", "-t", "8", "-i", SRC, "-c", "copy",
                        "-map", "0:v:0", "-map", "0:a:0?", c8], check=True)
    work = str(tmp_path_factory.mktemp("trk"))
    _info0, vf0, _ = video.prepare(c8, work)
    v1080 = os.path.join(d, "test_clip8_1080.mp4")
    video.to_1080(c8, vf0, v1080)
    info, vf, _ = video.prepare(v1080, work)
    return v1080, info, vf


@pytest.fixture(autouse=True)
def no_hwaccel(monkeypatch):
    # VPS と同じくソフトウェアデコード（Mac の videotoolbox は実行ごとに画素がわずかに揺れる）
    monkeypatch.setattr(video, "HWACCEL", [])


def _run(clip, tmp_path, monkeypatch, n):
    v1080, info, vf = clip
    monkeypatch.setenv("TRACK_WORKERS", str(n))
    monkeypatch.setenv("FEAT_WORKERS", str(n))
    tp = str(tmp_path / f"tracks{n}.json")
    tracks, dw, _dh, _seen = video.get_tracks(v1080, info, vf, tp, fast=True)
    face_m, cloth_m = video.compute_features(v1080, info, vf, tracks, dw, str(tmp_path / f"feats{n}.npz"))
    with open(tp, encoding="utf-8") as f:
        return f.read(), face_m, cloth_m


def test_parallel_matches_serial(clip, tmp_path, monkeypatch):
    j1, f1, c1 = _run(clip, tmp_path, monkeypatch, 1)
    j4, f4, c4 = _run(clip, tmp_path, monkeypatch, 4)
    assert j1 == j4
    assert f1 and sorted(f1) == sorted(f4) and sorted(c1) == sorted(c4)
    assert all(np.allclose(f1[t], f4[t], atol=1e-5) for t in f1)
    assert all(np.allclose(c1[t], c4[t], atol=1e-5) for t in c1)
