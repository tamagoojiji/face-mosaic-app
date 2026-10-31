"""正解集（運営が ✅ で確かめた通報）で「残す人の判定」（decide）を動画なしで再現して採点する。

  python scripts/score_corpus.py <正解集のフォルダ> [--save out.json] [--compare base.json]

正解集の1件 = <フォルダ>/<通報番号>/ に tracks.json・features.npz・meta.json（picks）・label.json（keep_tracks）。
- 直った: label の keep_tracks が全部「残す」に入った件数
- 増えた残す: --compare の結果より新しく「残す」になった枠のうち、label に無いもの（顔を隠すべき人を残す危険）
基準を変える修正は「直った」が減らず「増えた残す」が 0 のときだけ提案する。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from worker.engine import video  # noqa: E402


def run_case(d):
    with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(d, "label.json"), encoding="utf-8") as f:
        want = {int(x) for x in json.load(f)["keep_tracks"]}
    tracks, dw, dh, _seen = video.load_tracks(os.path.join(d, "tracks.json"))
    feats = os.path.join(d, "features.npz")
    face_m, cloth_m = video.load_feats(feats) if os.path.exists(feats) else ({}, {})
    keep, _blur = video.decide(tracks, meta.get("picks") or [], face_m, cloth_m, min(dw, dh))
    keep = sorted(int(x) for x in keep)
    return {"fixed": want <= set(keep), "want": sorted(want), "keep": keep}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--save")
    ap.add_argument("--compare")
    a = ap.parse_args()
    cases = sorted(n for n in os.listdir(a.corpus) if os.path.isfile(os.path.join(a.corpus, n, "label.json")))
    res = {n: run_case(os.path.join(a.corpus, n)) for n in cases}
    base = json.load(open(a.compare, encoding="utf-8")) if a.compare else {}
    extra = 0
    for n, r in res.items():
        more = sorted(set(r["keep"]) - set(base.get(n, {}).get("keep", r["keep"])) - set(r["want"]))
        extra += len(more)
        print(f"#{n}: {'直った' if r['fixed'] else 'まだ'}  残すべき {r['want']}  判定で残す {r['keep']}"
              + (f"  増えた残す {more}" if more else ""))
    print(f"直った {sum(r['fixed'] for r in res.values())} / {len(res)} 件  増えた残す {extra} 枠")
    if a.save:
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
