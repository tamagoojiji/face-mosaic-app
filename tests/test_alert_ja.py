"""#mosaic-alerts の例外は日本語の原因を出し、英語の原文は伏せ字（||…||）に入れること。"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.services import discord  # noqa: E402


def test_known_cause():
    e = RuntimeError("フレーム抽出に失敗 (17.67s): [out#0/image2] Nothing was written into output file")
    cause, raw = discord.explain(e).split("\n")
    assert cause == "原因: 場面写真の1コマを動画から切り出せませんでした（ffmpeg）"
    assert raw.startswith("原文（タップで表示）: ||RuntimeError: フレーム抽出に失敗") and raw.endswith("||")


def test_by_type_and_unknown():
    assert "動画の変換（ffmpeg）が途中で止まりました" in discord.explain(subprocess.CalledProcessError(1, ["ffmpeg"]))
    assert discord.explain(ValueError("bad")).startswith("原因: 想定していないエラーです\n")


def test_spoiler_not_broken():
    # 原文に || や ` があっても伏せ字が途中で切れない
    raw = discord.explain(RuntimeError("a || b `c`")).split("\n")[1]
    assert raw.count("||") == 2 and "`" not in raw
