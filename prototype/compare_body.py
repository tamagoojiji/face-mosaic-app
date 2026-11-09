"""体の追跡あり／なしで decide の結果を比べる（確認用・本番では使わない）。

  .venv/bin/python prototype/compare_body.py --dir <v1080.mp4 と tracks.json がある場所> [--picks 12,34] [--sheet out.png]

- features.npz が無ければ video.compute_features で、bodies.json が無ければ body.track_bodies で作る（同じ場所に置く）
- --picks 省略時: 体トラック 1 に紐づく顔トラック（特徴のあるもの）のうち、コマ数が最も多い 1 本を「選んだ人」とする
- 体経由で増えた顔トラックの表（体なしの keep＝base との最大の似かた）と、その顔の切り抜き（最大 40 本・各 120px）を
  並べた PNG（--sheet）を出す。
  PNG には実在の顔が写るので、リポジトリの外（scratchpad 等）に書くこと
"""

import argparse
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from worker.engine import body, video  # noqa: E402

TILE = 120
COLS = 8
MAX_TILES = 40


def _stats(xs):
    if not xs:
        return "なし"
    return f"最小 {min(xs):.3f}・中央 {statistics.median(xs):.3f}・最大 {max(xs):.3f}（{len(xs)} 本）"


def make_sheet(v1080, info, tracks, dw, ids, labels, out_png):
    """各トラックの真ん中のコマの顔を切り抜いて格子に並べる。"""
    by = {t["id"]: t for t in tracks}
    want = {}  # コマ → [(並び順, 枠)]
    for n, i in enumerate(ids):
        ks = sorted(by[i]["boxes"])
        f = ks[len(ks) // 2]
        want.setdefault(f, []).append((n, by[i]["boxes"][f]))
    k = info["out_width"] / float(dw)
    tiles = [None] * len(ids)
    cap = cv2.VideoCapture(v1080)
    fi, last = 0, max(want) if want else -1
    while fi <= last and cap.grab():
        if fi in want:
            ok, img = cap.retrieve()
            if ok:
                H, W = img.shape[:2]
                for n, b in want[fi]:
                    x, y, w, h = b["x"] * k, b["y"] * k, b["w"] * k, b["h"] * k
                    m = 0.3 * max(w, h)
                    x0, y0 = int(max(0, x - m)), int(max(0, y - m))
                    x1, y1 = int(min(W, x + w + m)), int(min(H, y + h + m))
                    crop = img[y0:y1, x0:x1]
                    t = cv2.resize(crop, (TILE, TILE)) if crop.size else np.zeros((TILE, TILE, 3), np.uint8)
                    cv2.rectangle(t, (0, TILE - 16), (TILE, TILE), (0, 0, 0), -1)
                    cv2.putText(t, labels[n], (3, TILE - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1,
                                cv2.LINE_AA)
                    tiles[n] = t
        fi += 1
    cap.release()
    tiles = [t if t is not None else np.zeros((TILE, TILE, 3), np.uint8) for t in tiles]
    rows = (len(tiles) + COLS - 1) // COLS
    sheet = np.full((max(1, rows) * TILE, COLS * TILE, 3), 40, np.uint8)
    for n, t in enumerate(tiles):
        r, c = divmod(n, COLS)
        sheet[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE] = t
    cv2.imwrite(out_png, sheet)


def main():
    ap = argparse.ArgumentParser(description="体の追跡あり／なしで decide の結果を比べる")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--picks", help="選んだ人の顔トラック id（カンマ区切り）")
    ap.add_argument("--sheet", help="体経由で増えた顔の切り抜きを並べた PNG（リポジトリの外に）")
    a = ap.parse_args()

    v1080 = os.path.join(a.dir, "v1080.mp4")
    info, vf, _ = video.prepare(v1080, a.dir)
    tracks, dw, dh, _seen = video.load_tracks(os.path.join(a.dir, "tracks.json"))
    feats = os.path.join(a.dir, "features.npz")
    t0 = time.time()
    face_m, cloth_m = video.compute_features(v1080, info, vf, tracks, dw, feats)
    print(f"特徴 {len(face_m)} トラック（服 {len(cloth_m)}）{time.time() - t0:.1f} 秒（既存なら読み込みのみ）")

    bpath = os.path.join(a.dir, "bodies.json")
    if not os.path.exists(bpath):
        t0 = time.time()
        body.track_bodies(v1080, info, vf, bpath)
        print(f"bodies.json を作成 {time.time() - t0:.1f} 秒")
    bts, bw, bh, every = body.load_bodies(bpath)
    body_of = body.link_faces(tracks, dw, dh, bts, bw, bh, every)
    print(f"体トラック {len(bts)} 本（{bw}×{bh}・{every} コマに 1 回）／顔トラック {len(tracks)} 本のうち体に紐づく "
          f"{len(body_of)} 本（{100.0 * len(body_of) / max(1, len(tracks)):.1f}%）")

    by = {t["id"]: t for t in tracks}
    if a.picks:
        picks = [int(x) for x in a.picks.split(",") if x.strip()]
    else:
        cand = [i for i, b in body_of.items() if b == 1 and i in face_m]
        if not cand:
            raise SystemExit("体トラック 1 に紐づく顔トラック（特徴あり）がありません。--picks で指定してください")
        picks = [max(cand, key=lambda i: (len(by[i]["boxes"]), -i))]
        print(f"選んだ人（自動）: 顔トラック {picks[0]}（体 1・{len(by[picks[0]]['boxes'])} コマ）")

    short = min(dw, dh)
    keep_off, _ = video.decide(tracks, picks, face_m, cloth_m, short)
    keep_on, _ = video.decide(tracks, picks, face_m, cloth_m, short, body_of=body_of)
    added = sorted(keep_on - keep_off, key=lambda i: by[i]["first_frame"])
    print(f"keep: 体なし {len(keep_off)} → 体あり {len(keep_on)}（増 {len(added)}・減 {len(keep_off - keep_on)}）")

    fps = video.ANALYSIS_FPS
    rows, fcs, ccs = [], [], []
    for i in added:  # base（体なしの keep）との最大の似かた
        fs = max((float(np.dot(face_m[i], face_m[r])) for r in keep_off if i in face_m and r in face_m),
                 default=None)
        cs = max((video.cloth_sim(cloth_m[i], cloth_m[r]) for r in keep_off if i in cloth_m and r in cloth_m),
                 default=None)
        if fs is not None:
            fcs.append(fs)
        if cs is not None:
            ccs.append(cs)
        t = by[i]
        rows.append((i, body_of.get(i), fs, cs, len(t["boxes"]), t["first_frame"] / fps, t["last_frame"] / fps))
    print("\n| 顔 id | 体 id | 顔 cos | 服 sim | コマ数 | 秒 |")
    print("|---|---|---|---|---|---|")
    for i, b, fs, cs, n, s0, s1 in rows:
        print(f"| {i} | {b} | {'-' if fs is None else f'{fs:.3f}'} | {'-' if cs is None else f'{cs:.3f}'} | {n} | "
              f"{s0:.1f}〜{s1:.1f} |")
    print(f"\nbase との最大の顔 cos: {_stats(fcs)}")
    print(f"base との最大の服 sim: {_stats(ccs)}")

    if a.sheet:
        ids = added[:MAX_TILES]
        labels = [f"{i} f{r[2]:.2f}" if r[2] is not None else f"{i}" for i, r in zip(ids, rows)]
        make_sheet(v1080, info, tracks, dw, ids, labels, a.sheet)
        print(f"切り抜き: {os.path.abspath(a.sheet)}（体経由で増えた顔トラック・出てくる順に最大 {MAX_TILES} 本）")


if __name__ == "__main__":
    main()
