# 体（人物）の検出＋追跡ベンチ（bench_person_track.py）

出演者の顔の追跡が途切れる「残し漏れ」を、体の追跡でつなげられるかを本番導入前に測る試作。本番コード・worker は使わない。

## 何を測るか
- 時間: デコード＋縮小／検出／追跡の秒数と、動画 1 分あたりの秒数
- 体トラック数・長さの中央値（コマ）・30 コマ以上続く本数
- `--tracks` あり: 顔トラックのうち体に紐づいた割合、体 1 本あたりの顔トラック数の分布（＝体で何本の顔の断片がまとまるか）

## 使い方
```
source .venv/bin/activate
python prototype/bench_person_track.py --video <v1080.mp4> --tracks <tracks.json> --out result.json
```
主な引数: `--model`（既定 `prototype/models/yolox_tiny.onnx`）`--input-size`（0=名前から: tiny/nano 416・他 640）`--long-side 640` `--every 2` `--conf 0.3` `--hold 30` `--threads 0`（0=OpenCV の既定）

`--tracks` は**同じ動画・同じコマ番号**のもの。本番の prepare と同じく 1080p 化した動画（`v1080.mp4`）とその `tracks.json` を組で渡す。

### v1080.mp4 と tracks.json を手元で作る（本番の do_prepare と同じ手順）
リポジトリのルートで、作業フォルダ `$W`（リポジトリの外）に作る。顔が写った動画・切り出しはリポジトリに置かない。
```
W=/private/tmp/bench; mkdir -p $W
python - ~/Downloads/<動画> $W <<'EOF'
import os, sys; sys.path.insert(0, os.getcwd())
from worker.engine import video
src, d = sys.argv[1], sys.argv[2]
info0, vf0, _ = video.prepare(src, d)
v = video.to_1080(src, vf0, os.path.join(d, "v1080.mp4"))
info, vf, _ = video.prepare(v, d)
t, dw, dh, seen = video.get_tracks(v, info, vf, os.path.join(d, "tracks.json"), fast=True)
print(len(t), dw, dh, seen)
EOF
```

## モデルの入手（YOLOX・Apache-2.0）
Megvii 公式リポジトリ `Megvii-BaseDetection/YOLOX` のリリース `0.1.1rc0` の ONNX。`prototype/models/` は git 管理外。
```
gh api repos/Megvii-BaseDetection/YOLOX/license -q .license.spdx_id   # Apache-2.0
gh release view 0.1.1rc0 -R Megvii-BaseDetection/YOLOX --json assets -q '.assets[].name' | grep onnx
gh release download 0.1.1rc0 -R Megvii-BaseDetection/YOLOX -p yolox_tiny.onnx -p yolox_s.onnx -D prototype/models
shasum -a 256 prototype/models/*.onnx
# 427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7  yolox_tiny.onnx
# c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063  yolox_s.onnx
```
前処理は letterbox（114 で埋める）・BGR のまま・正規化なし（0.1.1rc0 以降の形式。正規化すると検出がほぼ消えるのを確認済み）。出力は生の (N, 85) なので、stride 8/16/32 のグリッドで復元してから person（COCO 0 番）だけ NMS する。

## Mac での参考値（2026-11-09・536×954 59秒のショー動画を 1080p 化・顔トラック 2035 本）
| モデル | 入力 | 検出 1 コマ | 動画 1 分あたり（合計） | 体トラック | 顔→体の紐づき |
|---|---|---|---|---|---|
| yolox_tiny | 416 | 27 ms | 25.6 秒 | 234 本（30 コマ以上 102） | 83.8% |
| yolox_s | 640 | 98 ms | 90.1 秒 | 286 本（30 コマ以上 114） | 85.3% |

## VPS 6 で測る（ユーザーが `!` で 1 ブロックずつ貼る）
本番 worker のイメージを使い捨てのコンテナで動かす。本番 DB（`/data`）・compose・動いている worker には触らない。`nice -n 10` で本番のジョブより下げる。

1. 置き場を作る
```
ssh root@5.104.86.10 "mkdir -p /opt/face-mosaic-bench/models"
```
2. スクリプト・モデル・入力を送る（手元のリポジトリのルートで。`$W` は上で作った作業フォルダ）
```
scp prototype/bench_person_track.py prototype/models/yolox_tiny.onnx prototype/models/yolox_s.onnx root@5.104.86.10:/opt/face-mosaic-bench/
scp $W/v1080.mp4 $W/tracks.json root@5.104.86.10:/opt/face-mosaic-bench/
```
3. イメージと OpenCV の版を確かめる（requirements は版を固定していないので Mac と違うことがある）
```
ssh root@5.104.86.10 "docker run --rm --network none face-mosaic-app-face-mosaic-worker python -c 'import cv2; print(cv2.__version__)'"
```
4. YOLOX-tiny で測る
```
ssh root@5.104.86.10 "docker run --rm --network none -v /opt/face-mosaic-bench:/bench -w /bench face-mosaic-app-face-mosaic-worker nice -n 10 python bench_person_track.py --video v1080.mp4 --tracks tracks.json --model yolox_tiny.onnx --out result_tiny.json"
```
5. （任意）YOLOX-s・スレッドを絞ったとき（本番の顔検出と CPU を分け合う想定）
```
ssh root@5.104.86.10 "docker run --rm --network none -v /opt/face-mosaic-bench:/bench -w /bench face-mosaic-app-face-mosaic-worker nice -n 10 python bench_person_track.py --video v1080.mp4 --tracks tracks.json --model yolox_s.onnx --out result_s.json"
ssh root@5.104.86.10 "docker run --rm --network none -v /opt/face-mosaic-bench:/bench -w /bench face-mosaic-app-face-mosaic-worker nice -n 10 python bench_person_track.py --video v1080.mp4 --tracks tracks.json --model yolox_tiny.onnx --threads 2 --out result_tiny_t2.json"
```
6. 結果を手元へ
```
scp 'root@5.104.86.10:/opt/face-mosaic-bench/result_*.json' $W/
```
7. 入力・出力の動画を消す（顔が写っているため）
```
ssh root@5.104.86.10 "rm -f /opt/face-mosaic-bench/v1080.mp4 /opt/face-mosaic-bench/tracks.json && ls -la /opt/face-mosaic-bench"
```
処理中のジョブがあると時間が伸びる。測る前に `ssh root@5.104.86.10 "docker stats --no-stream face-mosaic-worker"` で worker の CPU が低いことを見ておく。
