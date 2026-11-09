"""体の追跡で顔の断片をつなぐ（body.link_faces・video.decide の body_of・worker の流れ）。合成データのみ。"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# test_features_later.py と同じ環境
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fm-test-"))
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
for k in ("LINE_MSG_CHANNEL_ACCESS_TOKEN", "DISCORD_WEBHOOK_ALERTS", "DISCORD_WEBHOOK_REPORTS", "DISCORD_WEBHOOK_USAGE",
          "APP_BASE_URL"):
    os.environ.pop(k, None)

import numpy as np  # noqa: E402

from app import db  # noqa: E402
from worker import run  # noqa: E402
from worker.engine import body, video  # noqa: E402

from test_features_later import _fake_prepare, _job  # noqa: E402


def _box(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def _body(bid, frames, box):
    return {"id": bid, "boxes": {f: _box(*box) for f in frames}}


def _face(fid, frames, box):
    return {"id": fid, "boxes": {f: _box(*box) for f in frames}}


# ---------------------------------------------------------------- link_faces

def test_rule_a_drops_faces_under_half_of_tallest():
    # 体 (100,100,200,600) の上 40% に顔が 3 つ（出演者 h=60・半分以上の h=40・後ろの観客 h=20）
    bodies = [_body(7, [0, 4, 8], (100, 100, 200, 600))]
    faces = [_face(1, [0, 4, 8], (170, 150, 60, 60)), _face(2, [0, 4, 8], (180, 160, 40, 40)),
             _face(3, [0, 4, 8], (220, 200, 20, 20))]
    assert body.link_faces(faces, 1000, 1000, bodies, 1000, 1000, every=4) == {1: 7, 2: 7}


def test_rule_b_min_overlaps_and_takes_most(monkeypatch):
    bodies = [_body(1, [0, 4, 8, 12], (0, 0, 100, 500)), _body(2, [0, 4, 8, 12], (500, 0, 100, 500))]
    one = _face(10, [12], (30, 30, 40, 40))                         # 重なり 1 回
    two = _face(11, [0, 4], (30, 30, 40, 40))                       # 2 回
    multi = {"id": 12, "boxes": {0: _box(530, 30, 40, 40), 4: _box(530, 30, 40, 40),
                                 8: _box(30, 30, 40, 40), 12: _box(530, 30, 40, 40)}}  # 体 2 に 3 回・体 1 に 1 回
    args = ([one, two, multi], 1000, 1000, bodies, 1000, 1000)
    assert body.BODY_LINK_MIN == 1
    assert body.link_faces(*args, every=4) == {10: 1, 11: 1, 12: 2}
    monkeypatch.setattr(body, "BODY_LINK_MIN", 2)  # 定数を上げれば 1 回だけの重なりは捨てる
    assert body.link_faces(*args, every=4) == {11: 1, 12: 2}


def test_coordinate_scaling_and_nearby_frame():
    # 顔は 1280×720・体は 640×360（半分）。体は 4 コマおき、顔は 2 コマおき（2・6 は近いコマの体を使う）
    bodies = [_body(3, [0, 4, 8], (100, 50, 100, 300))]               # 顔の座標では (200,100,200,600)
    inside = _face(1, [2, 6], (270, 120, 60, 60))                    # 中心 (300,150)＝上 40% に入る
    below = _face(2, [2, 6], (270, 500, 60, 60))                     # 中心 y=530 は上 40%（〜340）の外
    assert body.link_faces([inside, below], 1280, 720, bodies, 640, 360, every=4) == {1: 3}


# ---------------------------------------------------------------- decide の body_of

def _vec(cos_with_e0, k):
    """選んだ人（e0）との cos が cos_with_e0 で、残りは k 番目の軸（断片どうしはほぼ似ない）。"""
    v = np.zeros(128)
    v[0], v[k] = cos_with_e0, (1 - cos_with_e0 ** 2) ** 0.5
    return v


def _cloth(sim, k):
    """[1,0,…] との min の和が sim になるヒストグラム（残りは k 番目のビン）。"""
    v = np.zeros(8)
    v[0], v[k] = sim, 1.0 - sim
    return v


def _t(i, n=2):
    # 時間が離れた断片（既存の edges は無い・LINK_GAP より遠い）
    f0 = 300 * i
    return {"id": i, "med_conf": 0.9, "med_h": 200.0,
            "boxes": {f0 + 2 * k: _box(100 + 300 * i, 100, 200, 200) for k in range(n)}}


def _tracks():
    # 0: 選んだ人。1: 顔 0.3。2: 顔 0.1・服 0.2。3: 特徴なし
    return [_t(0), _t(1), _t(2), _t(3)]


def _feats():
    face_m = {0: _vec(1.0, 1), 1: _vec(0.3, 2), 2: _vec(0.1, 3)}
    cloth_m = {0: _cloth(1.0, 1), 1: _cloth(0.3, 2), 2: _cloth(0.2, 3)}
    return face_m, cloth_m


def test_decide_body_links_weak_but_not_dissimilar():
    tr = _tracks()
    face_m, cloth_m = _feats()
    keep, blur = video.decide(tr, [0], face_m, cloth_m, 1080, body_of={0: 5, 1: 5, 2: 5, 3: 5})
    assert keep == {0, 1}  # 1: 顔 0.3 ≥ 0.25 で入る。2: 顔 0.1・服 0.2 は入らない。3: 特徴なしは入らない
    assert blur == {2, 3}


def test_decide_without_body_is_unchanged():
    tr = _tracks()
    face_m, cloth_m = _feats()
    base = video.decide(tr, [0], face_m, cloth_m, 1080)
    assert video.decide(tr, [0], face_m, cloth_m, 1080, body_of=None) == base
    assert video.decide(tr, [0], face_m, cloth_m, 1080, body_of={}) == base
    assert base[0] == {0}


def test_decide_cloth_alone_links_via_body():
    tr = _tracks()
    face_m, cloth_m = _feats()
    cloth_m[2] = _cloth(0.5, 3)  # 顔 0.1 でも服 0.5 ≥ 0.45
    keep, _ = video.decide(tr, [0], face_m, cloth_m, 1080, body_of={0: 5, 2: 5})
    assert keep == {0, 2}


def test_decide_compares_with_whole_base_not_only_picked():
    # 4: 衣装（costume）で keep に入る断片（服 0.6・顔は似ない・4 コマ以上）。5: 4 と同じ体で、4 の顔にだけ似ている
    tr = [_t(0), _t(4, n=4), _t(5)]
    face_m = {0: _vec(1.0, 1), 4: _vec(0.0, 4), 5: _vec(0.1, 4)}
    cloth_m = {0: _cloth(1.0, 1), 4: _cloth(0.6, 4), 5: _cloth(0.2, 5)}
    assert video.decide(tr, [0], face_m, cloth_m, 1080)[0] == {0, 4}
    keep, _ = video.decide(tr, [0], face_m, cloth_m, 1080, body_of={0: 1, 4: 9, 5: 9})
    assert keep == {0, 4, 5}


def test_decide_body_does_not_chain():
    # 1 は体経由で入る（顔 0.3）。6 は 1 にだけ似ている（cos 0.95）が、体経由で入った 1 とは比べない
    tr = [_t(0), _t(1), _t(6)]
    face_m = {0: _vec(1.0, 1), 1: _vec(0.3, 2), 6: _vec(0.0, 2)}
    cloth_m = {0: _cloth(1.0, 1), 1: _cloth(0.3, 2), 6: _cloth(0.1, 6)}
    keep, blur = video.decide(tr, [0], face_m, cloth_m, 1080, body_of={0: 5, 1: 5, 6: 5})
    assert keep == {0, 1} and blur == {6}


# ---------------------------------------------------------------- worker の流れ

def test_body_failure_still_ready(monkeypatch):
    calls = []
    _fake_prepare(monkeypatch, calls)

    def boom(*a, **kw):
        calls.append("track_bodies")
        raise RuntimeError("体 boom")
    monkeypatch.setattr(run.body, "track_bodies", boom)
    monkeypatch.setenv("BODY_TRACK", "1")
    j = _job("bodyfail01")
    run.do_prepare(j)
    j = db.get_job("bodyfail01")
    assert j["status"] == "awaiting_pick" and j["features_state"] == "ready" and j["error"] is None
    assert calls.index("compute_features") < calls.index("track_bodies")
    assert sum(1 for c in calls if c.startswith("alert:")) == 1
    assert any(c.startswith("alert:工程=体の追跡") for c in calls)


def test_body_track_off(monkeypatch):
    calls = []
    _fake_prepare(monkeypatch, calls)
    monkeypatch.setattr(run.body, "track_bodies", lambda *a, **kw: calls.append("track_bodies") or [])
    monkeypatch.setenv("BODY_TRACK", "0")
    j = _job("bodyoff001")
    run.do_prepare(j)
    assert "track_bodies" not in calls
    assert db.get_job("bodyoff001")["features_state"] == "ready"


def test_body_ok_records_time(monkeypatch):
    calls = []
    _fake_prepare(monkeypatch, calls)
    monkeypatch.setattr(run.body, "track_bodies", lambda *a, **kw: calls.append("track_bodies") or [{"id": 0}])
    monkeypatch.setenv("BODY_TRACK", "1")
    j = _job("bodyok0001")
    run.do_prepare(j)
    j = db.get_job("bodyok0001")
    assert "track_bodies" in calls and j["t_body_sec"] is not None and j["features_state"] == "ready"
