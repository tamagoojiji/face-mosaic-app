"""filled_track_boxes（書き出しの枠）: 隙間の補間・途切れた後の保持（HOLD_TAIL_FRAMES）・1 コマだけのトラックの除外。ffmpeg 不要。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from worker.engine import video  # noqa: E402


def box(x, y=100.0, w=20.0, h=20.0):
    return {"x": float(x), "y": float(y), "w": float(w), "h": float(h), "conf": 0.9}


def track(tid, action, frames, x=100.0):
    boxes = {f: box(x) for f in frames}
    return {"id": tid, "action": action, "boxes": boxes,
            "first_frame": min(frames), "last_frame": max(frames)}


def test_interp_and_tail_hold():
    out = video.filled_track_boxes([track(1, "blur", [0, 2, 4])], n_frames=40, detect_every=1)
    assert len(out) == 1
    frames = set(out[0][1])
    assert {1, 3} <= frames  # 隙間の補間
    assert video.HOLD_TAIL_FRAMES == 5
    assert {5, 6, 7, 8, 9} <= frames  # 途切れた後の保持は 5 コマ
    assert not any(f >= 10 for f in frames)
    assert frames == set(range(10))


def test_single_frame_blur_track_is_dropped():
    out = video.filled_track_boxes([track(1, "blur", [0, 2, 4]), track(2, "blur", [10], x=300.0)],
                                   n_frames=40, detect_every=1)
    assert [t["id"] for t, _ in out] == [1]


def test_single_frame_keep_track_still_removes_interp_on_keep():
    # blur トラックの補間枠（コマ 1）が、1 コマだけの keep トラックの顔の上に来たら除外される（既存挙動）
    blur = track(1, "blur", [0, 2])
    keep = track(2, "keep", [1])
    out = video.filled_track_boxes([blur, keep], n_frames=40, detect_every=1)
    assert [t["id"] for t, _ in out] == [1]
    frames = set(out[0][1])
    assert 1 not in frames
    assert {0, 2} <= frames  # 検出のあるコマは keep の上でも残す
