"""確認画面の顔の切り抜き（make_thumbs）をコマ範囲に分けて同時に読んでも、直列と同じファイルができること。

入力は test_track_parallel.py と同じ実写 8 秒（FM_TEST_SRC が無ければ飛ばす）。
"""

import hashlib
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from worker.engine import video  # noqa: E402

SRC = os.environ.get("FM_TEST_SRC", "/private/tmp/claude-501/-Users-yontsuhashikunihiko-dev-tamago-face-mosaic-app/"
                     "ff689d27-65f8-40ad-b14a-286ec47662d5/scratchpad/bench/src300.mov")

pytestmark = pytest.mark.skipif(not os.path.exists(SRC), reason=f"入力動画がありません: {SRC}")


@pytest.fixture(autouse=True)
def no_hwaccel(monkeypatch):
    monkeypatch.setattr(video, "HWACCEL", [])


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    d = os.path.dirname(SRC)
    c8 = os.path.join(d, "test_clip8.mov")
    if not os.path.exists(c8):
        subprocess.run([video.FFMPEG, "-v", "error", "-y", "-ss", "0", "-t", "8", "-i", SRC, "-c", "copy",
                        "-map", "0:v:0", "-map", "0:a:0?", c8], check=True)
    work = str(tmp_path_factory.mktemp("thm"))
    _info0, vf0, _ = video.prepare(c8, work)
    v1080 = os.path.join(d, "test_clip8_1080.mp4")
    video.to_1080(c8, vf0, v1080)
    info, vf, _ = video.prepare(v1080, work)
    mp = pytest.MonkeyPatch()
    mp.setattr(video, "HWACCEL", [])
    tracks, dw, _dh, _seen = video.get_tracks(v1080, info, vf, os.path.join(work, "tracks.json"), fast=True)
    mp.undo()
    return v1080, info, vf, tracks, dw


def _md5s(d):
    return {n: hashlib.md5(open(os.path.join(d, n), "rb").read()).hexdigest() for n in sorted(os.listdir(d))}


def test_thumbs_split_matches_serial(clip, tmp_path, monkeypatch):
    v1080, info, vf, tracks, dw = clip
    # 8 秒（243 コマ）は 1 範囲 150 コマの下限だと分割されないので、下限を下げて 4 分割を実際に通す
    monkeypatch.setattr(video, "SEG_MIN_FRAMES", 10)
    used = []
    monkeypatch.setattr(video.multiprocessing, "get_context",
                        lambda m, _g=video.multiprocessing.get_context: used.append(m) or _g(m))
    out = {}
    for n in (1, 4):
        monkeypatch.setenv("THUMB_WORKERS", str(n))
        d = str(tmp_path / f"thumbs{n}")
        cnt = video.make_thumbs(v1080, info, vf, tracks, dw, d)
        out[n] = (cnt, _md5s(d))
    print(f"\n  切り抜き {out[1][0]} 件・ファイル {len(out[1][1])} 枚（直列）/ {len(out[4][1])} 枚（4分割）")
    assert used == ["spawn"]  # 4 分割のときだけ別プロセスで読んだ
    assert out[1][0] == out[4][0]
    assert set(out[1][1]) == set(out[4][1]) and len(out[1][1]) > 0
    assert out[1][1] == out[4][1]
