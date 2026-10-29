"""1080p 化（to_1080）で非参照 B フレームを飛ばす条件（fps 50 以上・B あり・CONVERT_SKIP_BIDIR != 0）。ffmpeg は実行しない。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from worker.engine import video  # noqa: E402


def _cmd(fps, b, monkeypatch, env=None):
    if env is None:
        monkeypatch.delenv("CONVERT_SKIP_BIDIR", raising=False)
    else:
        monkeypatch.setenv("CONVERT_SKIP_BIDIR", env)
    return video._to_1080_cmd("in.mov", "", "out.mp4", {"fps": fps, "has_b_frames": b})


def _skips(cmd):
    if "-skip_frame" not in cmd:
        return False
    k = cmd.index("-skip_frame")
    assert cmd[k + 1] == "bidir" and k < cmd.index("-i")
    return True


def test_fps30_no_skip(monkeypatch):
    assert not _skips(_cmd(30.0, 2, monkeypatch))


def test_fps60_with_b_skips(monkeypatch):
    assert _skips(_cmd(59.94, 2, monkeypatch))


def test_fps60_without_b_no_skip(monkeypatch):
    assert not _skips(_cmd(60.0, 0, monkeypatch))


def test_env_off(monkeypatch):
    assert not _skips(_cmd(60.0, 2, monkeypatch, env="0"))
