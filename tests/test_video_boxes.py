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


def det(x, y, h=40.0):
    return {"x": float(x), "y": float(y), "w": h * 0.8, "h": float(h), "conf": 0.9}


def act(tid, x, y, h=40.0):
    return {"id": tid, "last": {"x": float(x), "y": float(y), "w": h * 0.8, "h": float(h)}}


def test_center_match_links_when_iou_fails():
    # 30px 横に動いた（IoU は 0.3 未満）が中心距離は 1.0×h 以内 → 2段目でつながる
    d = det(130, 100)
    t = act(1, 100, 100)
    assert video._match_detection(d, [t], set()) is None
    assert video._match_by_center(d, [t], set()) is t
    assert video._match_by_center(d, [t], {1}) is None  # 使用済みは候補にしない


def test_center_match_refuses_when_two_candidates_are_close():
    d = det(130, 100)
    a, b = act(1, 100, 100), act(2, 160, 100)  # どちらも 30px（2番目が最近傍の 1.5 倍以内）
    assert video._match_by_center(d, [a, b], set()) is None
    d2 = det(110, 100)
    near, other = act(4, 100, 100), act(5, 140, 100)  # 10px と 30px（どちらも半径 40px 内・1.5 倍を超える）→ 近い方につなぐ
    assert video._match_by_center(d2, [near, other], set()) is near


def test_center_match_refuses_size_mismatch_and_far():
    d = det(130, 100, h=40.0)
    assert video._match_by_center(d, [act(1, 100, 100, h=80.0)], set()) is None  # 高さの比 0.5
    assert video._match_by_center(d, [act(2, 200, 100)], set()) is None  # 中心距離 70px > 1.0×40


def test_decide_glob_requires_sum_1_30():
    # 時間も場所も離れた（spread でつながらない）トラック 2・3 が、選んだトラック 1 との顔＋服の類似の和だけで keep になるか
    import math
    import numpy as np

    def tr(tid, f0, x):
        boxes = {f: {"x": float(x), "y": 100.0, "w": 40.0, "h": 50.0, "conf": 0.9} for f in (f0, f0 + 1, f0 + 2)}
        return {"id": tid, "boxes": boxes, "confs": [0.9] * 3, "med_conf": 0.9, "med_h": 50.0,
                "first_frame": f0, "last_frame": f0 + 2}

    tracks = [tr(1, 0, 100), tr(2, 500, 600), tr(3, 900, 800)]
    face_m = {1: np.array([1.0, 0.0]), 2: np.array([0.5, math.sqrt(0.75)]), 3: np.array([0.6, 0.8])}
    cloth_m = {1: np.array([1.0, 0.0]), 2: np.array([0.7, 0.3]), 3: np.array([0.75, 0.25])}
    # 2: 顔 0.5＋服 0.7＝1.2（旧 1.15 なら keep・新 1.30 未満）/ 3: 顔 0.6＋服 0.75＝1.35
    # 高さ 50 は短辺 1000 の 7%（COSTUME_MIN_FRAC）未満なので服だけの costume には入らない
    keep, blur = video.decide(tracks, [1], face_m, cloth_m, 1000)
    assert video.GLOB_SUM == 1.30
    assert keep == {1, 3}
    assert blur == {2}
