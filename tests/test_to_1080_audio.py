"""1080p 化の失敗: MP4 に入らない音声でも止まらないことと、失敗したときは ffmpeg のエラー文がアラームに届くこと。"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.services import discord  # noqa: E402
from worker.engine import video  # noqa: E402

pytestmark = pytest.mark.skipif(not os.path.exists(video.FFMPEG), reason="ffmpeg がありません")


@pytest.fixture(autouse=True)
def no_hwaccel(monkeypatch):
    monkeypatch.setattr(video, "HWACCEL", [])


def make_clip(path, *audio):
    """2秒の小さい動画を作る。audio は音声トラックごとのコーデック名。"""
    cmd = [video.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=2"]
    for _ in audio:
        cmd += ["-f", "lavfi", "-i", "sine=d=2"]
    cmd += ["-map", "0:v"]
    for n, codec in enumerate(audio):
        cmd += ["-map", f"{n + 1}:a", f"-c:a:{n}", codec, f"-ar:a:{n}", "8000"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mov", path], check=True)
    return path


def audio_codecs(path):
    r = subprocess.run([video.FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return r.stdout.split()


def test_audio_not_allowed_in_mp4_becomes_aac(tmp_path):
    src = make_clip(str(tmp_path / "src.mp4"), "pcm_mulaw")
    out = video.to_1080(src, "", str(tmp_path / "v1080.mp4"))
    assert audio_codecs(out) == ["aac"]


def test_aac_stays_copied(tmp_path):
    src = make_clip(str(tmp_path / "src.mp4"), "aac")
    assert video.audio_args(video.probe(src)) == ["-c:a", "copy"]
    assert audio_codecs(video.to_1080(src, "", str(tmp_path / "v1080.mp4"))) == ["aac"]


def test_no_audio(tmp_path):
    src = make_clip(str(tmp_path / "src.mp4"))
    assert audio_codecs(video.to_1080(src, "", str(tmp_path / "v1080.mp4"))) == []


def test_only_first_audio_track_is_used(tmp_path):
    # 2本目の音声が MP4 に入らなくても、書き出しと同じ1本目だけを使うので止まらない
    src = make_clip(str(tmp_path / "src.mp4"), "aac", "pcm_mulaw")
    assert audio_codecs(video.to_1080(src, "", str(tmp_path / "v1080.mp4"))) == ["aac"]


def test_hq_join_with_audio_not_allowed_in_mp4(tmp_path):
    # 高画質の書き出しは元動画の音声を付けるので、連結でも同じ扱いにする
    src = make_clip(str(tmp_path / "src.mp4"), "pcm_mulaw")
    info = video.probe(src)
    out = str(tmp_path / "out.mp4")
    video._render_split(False, src, info, "", lambda a, b: {}, info["frames"], 1, out, "libx264", "pixel", 1.0, 1.0, "テスト")
    assert audio_codecs(out) == ["aac"]


def fake_probe(monkeypatch, *audio):
    """audio は音声トラックごとの (codec_name, codec_tag_string)。"""
    streams = [{"index": 0, "codec_type": "video", "width": 64, "height": 64, "r_frame_rate": "30/1", "duration": "1"}]
    streams += [{"index": i + 1, "codec_type": "audio", "codec_name": c, "codec_tag_string": t}
                for i, (c, t) in enumerate(audio)]
    out = json.dumps({"streams": streams, "format": {"duration": "1"}})
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, out, ""))
    return video.probe("x.mov")


@pytest.mark.parametrize("name", ["apple_apac", "unknown", "none"])
def test_spatial_audio_is_skipped_whatever_ffprobe_calls_it(monkeypatch, name):
    # 空間オーディオを知らない版の ffprobe は名前を unknown にする（タグの apac だけが手がかり）
    info = fake_probe(monkeypatch, (name, "apac"), ("aac", "mp4a"))
    assert info["has_audio"] and info["audio_index"] == 2
    assert not fake_probe(monkeypatch, (name, "apac"))["has_audio"]


def test_failure_reaches_alert(tmp_path):
    # 2026-11-04: 原文がコマンドだけで切れ、ffmpeg のエラー文も終了コードもアラームに届かなかった
    src = make_clip(str(tmp_path / "src.mp4"), "aac")
    with pytest.raises(RuntimeError) as e:
        video.to_1080(src, "nosuchfilter", str(tmp_path / "v1080.mp4"))
    cause, raw = discord.explain(e.value).split("\n", 1)
    assert cause == "原因: 動画の変換（ffmpeg）が途中で止まりました"
    assert "rc=" in raw and "nosuchfilter" in raw


def test_long_stderr_keeps_first_and_last(monkeypatch, tmp_path):
    # ffmpeg は本当の理由を先頭に、ディスク不足などは末尾に出す。どちらも原文の300文字に収める
    err = "Could not find tag for codec\n" + "x" * 2000 + "\nNo space left on device\n"
    monkeypatch.setattr(video.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout="", stderr=err))
    monkeypatch.setattr(video, "probe", lambda p: {"has_audio": False})
    with pytest.raises(RuntimeError) as e:
        video.to_1080("src.mp4", "", str(tmp_path / "v1080.mp4"))
    assert "Could not find tag" in discord.explain(e.value)
    assert "原因: サーバーのディスクの空きが足りませんでした" in discord.explain(e.value)
    assert "No space left on device" in str(e.value)[:300]


def test_killed_ffmpeg_shows_signal(monkeypatch, tmp_path):
    # メモリ不足などで強制終了されたときは stderr が空。終了コードだけは残す
    monkeypatch.setattr(video.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, -9, stdout="", stderr=""))
    monkeypatch.setattr(video, "probe", lambda p: {"has_audio": False})
    with pytest.raises(RuntimeError, match="rc=-9"):
        video.to_1080("src.mp4", "", str(tmp_path / "v1080.mp4"))
