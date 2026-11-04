"""確認画面の一覧（review_lists）: 書き出しでモザイクがかかる顔を、小さくてもすべて選択肢に出す。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from worker.engine import video as v  # noqa: E402

INFO = {"out_width": 720, "out_height": 1280, "fps": 30.0}


def track(tid, h, conf=0.8, frames=3):
    box = {"x": 100.0, "y": 100.0, "w": h, "h": h}
    return {"id": tid, "first_frame": tid, "boxes": {tid + i: dict(box) for i in range(frames)},
            "med_h": h, "med_conf": conf, "med_box": box}


def final_blur(tracks, keep):
    """書き出しと同じ手順で、実際にモザイクがかかる顔を求める。"""
    ts = [dict(t) for t in tracks]
    v.classify_tracks_global(ts, 720, "background", v.FACE_KEEP_RATIO, v.FACE_MIN_FRAC)
    blur = [t["id"] for t in ts if t["med_conf"] >= v.BLUR_MIN_CONF and t["id"] not in keep]
    v.apply_overrides(ts, {"keep": sorted(keep), "blur": blur})
    return {t["id"] for t in ts if t.get("action") == "blur" and len(t["boxes"]) >= v.BLUR_MIN_SEEN}


def test_small_face_is_listed():
    tracks = [track(0, 200), track(1, 15), track(2, 30), track(3, 60)]  # 15px は短辺の約 2%
    lists = v.review_lists(INFO, tracks, {0}, 720)
    assert [i["id"] for i in lists["blur"]] == [1, 2, 3]


def test_list_matches_export_when_all_faces_are_small():
    # どの顔も小さいと、conf 0.40 未満でも書き出しではモザイクがかかる
    tracks = [track(0, 15), track(1, 15, conf=0.3), track(2, 15, frames=1)]
    lists = v.review_lists(INFO, tracks, set(), 720)
    assert {i["id"] for i in lists["blur"]} == final_blur(tracks, set()) == {0, 1}


def test_kept_face_is_not_in_blur_list():
    lists = v.review_lists(INFO, [track(0, 200), track(1, 15)], {1}, 720)
    assert 1 not in {i["id"] for i in lists["blur"]}


SAME = {i: np.array([1.0, 0.0]) for i in range(5)}  # 全員同じ顔の特徴


def frag(tid, first, n, x=100.0, h=60.0):
    box = {"x": x, "y": 100.0, "w": h, "h": h}
    return {"id": tid, "first_frame": first, "boxes": {first + i: dict(box) for i in range(n)},
            "med_h": h, "med_conf": 0.8, "med_box": box}


def test_fragments_of_one_person_become_one_item():
    lists = v.review_lists(INFO, [frag(0, 0, 5), frag(1, 10, 5), frag(2, 200, 5)], set(), 720, SAME)
    assert [i["ids"] for i in lists["blur"]] == [[0, 1], [2]]


def test_far_or_overlapping_or_other_size_are_not_joined():
    ts = [frag(0, 0, 5), frag(1, 8, 5, x=400.0), frag(2, 2, 5, x=104.0), frag(3, 8, 5, x=100.0, h=120.0)]
    assert sorted(len(i["ids"]) for i in v.review_lists(INFO, ts, set(), 720, SAME)["blur"]) == [1, 1, 1, 1]


def test_different_face_or_no_feature_is_not_joined():
    ts = [frag(0, 0, 5), frag(1, 10, 5)]
    other = {0: np.array([1.0, 0.0]), 1: np.array([0.0, 1.0])}
    assert len(v.review_lists(INFO, ts, set(), 720, other)["blur"]) == 2
    assert len(v.review_lists(INFO, ts, set(), 720)["blur"]) == 2


def test_one_fragment_joins_only_the_nearest_next():
    ts = [frag(0, 0, 5), frag(1, 8, 5, x=110.0), frag(2, 8, 5, x=130.0)]
    assert [i["ids"] for i in v.review_lists(INFO, ts, set(), 720, SAME)["blur"]] == [[0, 1], [2]]


def test_small_faces_are_marked_and_every_id_stays():
    ts = [frag(0, 0, 5), frag(1, 10, 3, x=300.0, h=15.0), frag(2, 100, 3, x=500.0, h=15.0)]
    blur = v.review_lists(INFO, ts, set(), 720)["blur"]
    assert {i for it in blur for i in it["ids"]} == {0, 1, 2}
    assert [it["small"] for it in blur] == [False, True, True]
