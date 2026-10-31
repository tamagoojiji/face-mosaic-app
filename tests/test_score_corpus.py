"""正解集の採点（scripts/score_corpus.py）: 「直った」と「増えた残す」（label に無いのに新しく残す枠）を数えること。"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import score_corpus  # noqa: E402


def _case(root, rid, picks, want):
    d = root / rid
    d.mkdir()
    (d / "tracks.json").write_text("{}")
    (d / "meta.json").write_text(json.dumps({"picks": picks}))
    (d / "label.json").write_text(json.dumps({"keep_tracks": want}))


def test_score(tmp_path, monkeypatch, capsys):
    keep_now = {"1": [1, 2], "2": [5]}
    monkeypatch.setattr(score_corpus.video, "load_tracks", lambda p: ([], 100, 100, 0))
    monkeypatch.setattr(score_corpus.video, "decide",
                        lambda tracks, picks, f, c, short: (set(keep_now[str(picks[0])]), set()))
    _case(tmp_path, "1", [1], [2])   # 直っている
    _case(tmp_path, "2", [2], [6])   # まだ
    monkeypatch.setattr(sys, "argv", ["score", str(tmp_path), "--save", str(tmp_path / "base.json")])
    score_corpus.main()
    assert "直った 1 / 2 件  増えた残す 0 枠" in capsys.readouterr().out
    # 基準を変えたら #2 は直ったが、正解に無い 7 も残すようになった → 増えた残す 1
    keep_now["2"] = [5, 6, 7]
    monkeypatch.setattr(sys, "argv", ["score", str(tmp_path), "--compare", str(tmp_path / "base.json")])
    score_corpus.main()
    out = capsys.readouterr().out
    assert "直った 2 / 2 件  増えた残す 1 枠" in out and "増えた残す [7]" in out
