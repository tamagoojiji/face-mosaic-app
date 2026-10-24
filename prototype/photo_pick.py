#!/usr/bin/env python3
"""顔モザイク 写真1枚モード（試作）。

使い方:
  python3 photo_pick.py 画像 [--out DIR] [--port N] [--min-conf 0.40] [--no-open]

流れ:
  1. 顔を検出する（detect_only.py と同じ: Vision＋YuNet score 0.25 → merge_faces）
  2. 127.0.0.1 の一時サーバーで選択画面を開く。「モザイクをかけない」枠をタップ
  3. 「この内容で書き出す」で、選ばなかった表示中の枠すべてに mosaic_face を当てる
     （色補正なし・元画像の画素に直接）→ <元名>_mosaic.jpg / .json を保存して open
  4. 書き出しが終わったら 90 秒後にサーバーを閉じる（タイムアウトの自動書き出しはしない）
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RETOUCH_DIR = os.path.expanduser("~/dev/tools/photo-retouch")
DETECT_DIR = os.path.expanduser("~/Downloads/顔検出テスト_2026-10-24")
sys.path.insert(0, RETOUCH_DIR)
sys.path.insert(0, DETECT_DIR)

import numpy as np
from PIL import Image, ImageOps

from retouch import detect_faces, detect_faces_yunet, merge_faces, mosaic_face, YUNET_PATH
from detect_only import FACE_SCORE

DEFAULT_OUT = os.path.expanduser("~/Downloads/モザイク後")
VIEW_LONG = 2000      # 画面表示用の画像の長辺
LINGER_SEC = 90       # 書き出し後にサーバーを閉じるまでの秒数


def detect(im):
    """detect_only.process と同じ呼び方で検出し、確信度の高い順に返す。"""
    arr8 = np.asarray(im, dtype=np.uint8)
    found, notes = [], []
    try:
        found += detect_faces(im)
    except (RuntimeError, OSError) as e:
        notes.append(f"Vision失敗: {e}")
    yn, warn = detect_faces_yunet(arr8, YUNET_PATH, FACE_SCORE)
    if warn:
        notes.append(warn)
    found += yn
    faces = sorted(merge_faces(found), key=lambda f: -float(f.get("conf", 0)))
    return faces, notes


def build_html(stem, W, H, faces, min_conf):
    data = [{"id": i, "l": f["x"] / W * 100, "t": f["y"] / H * 100,
             "w": f["w"] / W * 100, "h": f["h"] / H * 100,
             "conf": round(float(f["conf"]), 3), "low": float(f["conf"]) < min_conf}
            for i, f in enumerate(faces, 1)]
    return (PAGE.replace("__TITLE__", stem).replace("__MINCONF__", f"{min_conf:.2f}")
            .replace("__RATIO__", f"{W / H:.6f}").replace("__DATA__", json.dumps(data)))


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>モザイクをかけない顔の選択</title>
<style>
:root{--bg:#f6f5f2;--fg:#1d1d1f;--sub:#62626a;--line:#d9d7d2;--card:#fff;--pri:#1f5fbf;--mz:#f08a00;--keep:#16a34a;--low:#e02424}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececee;--sub:#a0a0a8;--line:#34343a;--card:#1d1d21;--pri:#3b82f6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,"Hiragino Sans",sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center}
h1{font-size:16px;margin:0;flex:1 1 100%;line-height:1.4}
.opt{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--sub);min-height:44px}
.opt input{width:20px;height:20px}
.zoom{margin-left:auto;display:flex;gap:6px}
.zoom button{min-width:44px;min-height:44px;font:inherit;font-size:18px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
#sc{height:60vh;overflow:auto;touch-action:pan-x pan-y;-webkit-overflow-scrolling:touch;background:#000}
#in{position:relative;margin:0 auto}
#in img{display:block;width:100%;user-select:none;-webkit-user-drag:none}
.b{position:absolute;padding:0;margin:0;background:transparent;border:2px solid var(--mz);border-radius:4px;cursor:pointer;box-shadow:0 0 0 1px rgba(0,0,0,.55)}
.b::before{content:"";position:absolute;left:50%;top:50%;width:max(100%,44px);height:max(100%,44px);transform:translate(-50%,-50%)}
.b .tag{position:absolute;left:-2px;bottom:100%;margin-bottom:2px;background:var(--mz);color:#111;font:700 12px/1 -apple-system,sans-serif;padding:3px 5px;border-radius:4px;white-space:nowrap;pointer-events:none}
.b.keep{border-color:var(--keep);border-width:3px;background:rgba(22,163,74,.2)}
.b.keep .tag{background:var(--keep);color:#fff}
.b.low{border-style:dashed;border-color:var(--low)}
.b.low .tag{background:var(--low);color:#fff}
.b.low.keep{border-style:dashed;border-color:var(--keep)}
.b.low.keep .tag{background:var(--keep)}
.b.hide{display:none}
footer{position:fixed;left:0;right:0;bottom:0;z-index:10;border-top:1px solid var(--line);background:var(--card);padding:8px 16px calc(8px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:6px}
#cnt{font-size:14px;font-weight:600;line-height:20px}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.seg{display:flex;gap:6px}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg label{display:flex;align-items:center;gap:2px;padding:0 12px;min-height:44px;font-size:13px;border:1px solid var(--line);border-radius:999px;cursor:pointer;white-space:nowrap}
.seg label::before{content:"";display:inline-block;width:1em}
.seg label:has(input:checked){background:var(--fg);color:var(--bg);border-color:var(--fg);font-weight:600}
.seg label:has(input:checked)::before{content:"✓"}
.seg label:has(input:focus-visible){outline:2px solid var(--pri);outline-offset:2px}
#go{flex:1 1 150px;height:56px;border:0;border-radius:10px;background:var(--pri);color:#fff;font:700 17px/1 -apple-system,"Hiragino Sans",sans-serif;cursor:pointer}
#go:disabled{opacity:.6}
#done{display:none;flex:1;overflow:auto;padding:32px 16px;text-align:center}
#done p{margin:10px 0;word-break:break-all}
#done a{display:inline-block;margin-top:16px;min-height:44px;line-height:44px;color:var(--pri);font-weight:600}
</style></head><body>
<header id="hd"><h1>モザイクをかけない顔やものをタップしてください</h1>
<label class="opt"><input type="checkbox" id="low">確信度__MINCONF__未満も表示</label>
<div class="zoom"><button id="zo" aria-label="縮小">－</button><button id="zi" aria-label="拡大">＋</button></div></header>
<div id="sc"><div id="in"><img id="ph" src="view.jpg" alt="__TITLE__"></div></div>
<footer id="ft"><span id="cnt"></span><div class="row">
<div class="seg" role="radiogroup" aria-label="モザイクの種類"><label><input type="radio" name="st" value="pixel" checked>ブロック粗め</label><label><input type="radio" name="st" value="blur">すりガラス</label></div>
<button id="go">この内容で書き出す</button></div></footer>
<div id="done"></div>
<script>
const D=__DATA__,R=__RATIO__,keep=new Set(),inn=document.getElementById('in'),sc=document.getElementById('sc'),low=document.getElementById('low');
D.slice().sort((a,b)=>b.w*b.h-a.w*a.h).forEach(d=>{const e=document.createElement('button');e.className='b'+(d.low?' low hide':'');e.dataset.id=d.id;
 Object.assign(e.style,{left:d.l+'%',top:d.t+'%',width:d.w+'%',height:d.h+'%'});
 const t=document.createElement('span');t.className='tag';e.appendChild(t);d.el=e;paint(d);
 e.onclick=()=>{keep.has(d.id)?keep.delete(d.id):keep.add(d.id);paint(d);count()};inn.appendChild(e)});
function paint(d){const k=keep.has(d.id);d.el.classList.toggle('keep',k);d.el.firstChild.textContent=k?'✓ '+d.id:String(d.id);
 d.el.setAttribute('aria-label','枠'+d.id+'（確信度'+d.conf+'）'+(k?'モザイクをかけない':'モザイクをかける'));d.el.setAttribute('aria-pressed',k)}
function shown(){return D.filter(d=>!d.low||low.checked)}
function count(){const s=shown(),m=s.filter(d=>!keep.has(d.id)).length;document.getElementById('cnt').textContent='モザイク '+m+' 件・かけない '+(s.length-m)+' 件'}
low.onchange=()=>{D.forEach(d=>{if(d.low)d.el.classList.toggle('hide',!low.checked)});count()};
let zoom=1,base=0;
const hd=document.getElementById('hd'),ft=document.getElementById('ft');
function fit(){const fh=ft.offsetHeight;document.body.style.paddingBottom=fh+'px';
 sc.style.height=Math.max(200,innerHeight-hd.offsetHeight-fh)+'px';
 const r=sc.getBoundingClientRect();base=Math.min(r.width,r.height*R);inn.style.width=(base*zoom)+'px'}
new ResizeObserver(fit).observe(ft);
function setZoom(z,cx,cy){const old=zoom;zoom=Math.min(6,Math.max(1,z));const r=sc.getBoundingClientRect();cx=cx??r.width/2;cy=cy??r.height/2;
 const px=(sc.scrollLeft+cx)/old,py=(sc.scrollTop+cy)/old;inn.style.width=(base*zoom)+'px';sc.scrollLeft=px*zoom-cx;sc.scrollTop=py*zoom-cy}
document.getElementById('zi').onclick=()=>setZoom(zoom*1.5);
document.getElementById('zo').onclick=()=>setZoom(zoom/1.5);
sc.addEventListener('wheel',e=>{if(!e.ctrlKey)return;e.preventDefault();const r=sc.getBoundingClientRect();setZoom(zoom*Math.exp(-e.deltaY/200),e.clientX-r.left,e.clientY-r.top)},{passive:false});
const pts=new Map();let pd=0,pz=1;
sc.addEventListener('pointerdown',e=>{pts.set(e.pointerId,e);if(pts.size===2){const[a,b]=[...pts.values()];pd=Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);pz=zoom}});
sc.addEventListener('pointermove',e=>{if(!pts.has(e.pointerId))return;pts.set(e.pointerId,e);if(pts.size===2&&pd){const[a,b]=[...pts.values()];
 const r=sc.getBoundingClientRect();setZoom(pz*Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY)/pd,(a.clientX+b.clientX)/2-r.left,(a.clientY+b.clientY)/2-r.top)}});
['pointerup','pointercancel'].forEach(t=>sc.addEventListener(t,e=>{pts.delete(e.pointerId);if(pts.size<2)pd=0}));
document.addEventListener('gesturestart',e=>e.preventDefault());
window.addEventListener('resize',fit);document.getElementById('ph').addEventListener('load',fit);fit();count();
document.getElementById('go').onclick=async()=>{const go=document.getElementById('go');go.disabled=true;go.textContent='書き出しています…';
 const body={keep:[...keep],show_low:low.checked,style:document.querySelector('input[name=st]:checked').value};
 let j=null;try{const r=await fetch('/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(r.ok)j=await r.json()}catch(e){}
 ['hd','sc','ft'].forEach(id=>document.getElementById(id).style.display='none');document.body.style.paddingBottom='0';const d=document.getElementById('done');d.style.display='block';
 if(!j){d.innerHTML='<p>書き出しに失敗しました</p><a href="/">もう一度選び直す</a>';return}
 d.innerHTML='';const p=(t)=>{const e=document.createElement('p');e.textContent=t;d.appendChild(e)};
 p('書き出しました');p('保存先: '+j.path);p('モザイク '+j.blur+' 件・かけない '+j.keep+' 件（'+j.sec+' 秒）');
 const a=document.createElement('a');a.href='/';a.textContent='もう一度選び直す';d.appendChild(a)};
</script></body></html>"""


class App:
    def __init__(self, src, out_dir, min_conf):
        self.src, self.out_dir, self.min_conf = src, out_dir, min_conf
        self.stem = os.path.splitext(os.path.basename(src))[0]
        self.im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
        self.W, self.H = self.im.size
        t0 = time.time()
        self.faces, notes = detect(self.im)
        nlow = sum(1 for f in self.faces if float(f["conf"]) < min_conf)
        print(f"{os.path.basename(src)} {self.W}x{self.H}: 検出 {len(self.faces)} 件"
              f"（{min_conf:.2f}以上 {len(self.faces) - nlow}・未満 {nlow}）{time.time() - t0:.1f}s"
              + (f" [{'; '.join(notes)}]" if notes else ""), flush=True)
        s = min(1.0, VIEW_LONG / float(max(self.W, self.H)))
        v = self.im if s >= 1.0 else self.im.resize((round(self.W * s), round(self.H * s)), Image.LANCZOS)
        from io import BytesIO
        buf = BytesIO()
        v.save(buf, "JPEG", quality=88)
        self.view = buf.getvalue()
        self.html = build_html(self.stem, self.W, self.H, self.faces, min_conf).encode()
        self.last_export = None
        self.lock = threading.Lock()

    def export(self, keep_ids, show_low, style):
        t0 = time.time()
        arr8 = np.array(self.im, dtype=np.uint8)
        keep, blur = [], []
        for i, f in enumerate(self.faces, 1):
            conf = float(f["conf"])
            if conf < self.min_conf and not show_low:
                continue
            rec = {"id": i, "x": f["x"], "y": f["y"], "w": f["w"], "h": f["h"], "conf": round(conf, 3)}
            if i in keep_ids:
                keep.append(rec)
                continue
            if style == "blur":
                info = mosaic_face(arr8, f, 0, style="blur")
            else:
                info = mosaic_face(arr8, f, max(4, round(f["w"] / 4)), style="pixel")
            rec["applied"] = info
            blur.append(rec)
        os.makedirs(self.out_dir, exist_ok=True)
        jpg = os.path.join(self.out_dir, self.stem + "_mosaic.jpg")
        Image.fromarray(arr8).save(jpg, quality=92)
        sec = round(time.time() - t0, 2)
        meta = {"source": self.src, "output": jpg, "size": [self.W, self.H],
                "style": "blur" if style == "blur" else "pixel",
                "style_label": "すりガラス" if style == "blur" else "ブロック粗め",
                "min_conf": self.min_conf, "show_low": bool(show_low),
                "keep": keep, "blur": blur, "seconds": sec}
        with open(os.path.join(self.out_dir, self.stem + "_mosaic.json"), "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False, indent=1)
        print(f"  書き出し {jpg}  モザイク {len(blur)}・かけない {len(keep)}・{meta['style_label']}・{sec}s",
              flush=True)
        return {"path": jpg, "blur": len(blur), "keep": len(keep), "sec": sec}


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, app.html, "text/html; charset=utf-8")
            elif path == "/view.jpg":
                self._send(200, app.view, "image/jpeg")
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/export":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length", "0"))
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                keep_ids = {int(x) for x in data.get("keep", [])}
                show_low = bool(data.get("show_low", False))
                style = "blur" if data.get("style") == "blur" else "pixel"
            except Exception:
                self.send_error(400)
                return
            try:
                with app.lock:
                    res = app.export(keep_ids, show_low, style)
            except Exception as e:
                print(f"  書き出しに失敗: {e}", flush=True)
                self.send_error(500)
                return
            self._send(200, json.dumps(res, ensure_ascii=False).encode(), "application/json")
            app.last_export = time.time()
            if app.open_result:
                subprocess.run(["open", res["path"]])
    return H


def main():
    p = argparse.ArgumentParser(description="写真1枚の顔モザイク（選んだもの以外をモザイク）")
    p.add_argument("input")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--min-conf", type=float, default=0.40)
    p.add_argument("--no-open", dest="no_open", action="store_true",
                   help="ブラウザと完成画像を開かない（検証用）")
    a = p.parse_args()

    app = App(os.path.abspath(os.path.expanduser(a.input)), os.path.expanduser(a.out), a.min_conf)
    app.open_result = not a.no_open
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"  選択画面: {url}", flush=True)
    if not a.no_open:
        subprocess.run(["open", url])
    # 書き出しが終わるまで待つ。最後の書き出しから 90 秒で閉じる（選び直しで延長）
    while app.last_export is None or time.time() - app.last_export < LINGER_SEC:
        time.sleep(0.5)
    srv.shutdown()
    print("  終了", flush=True)


if __name__ == "__main__":
    main()
