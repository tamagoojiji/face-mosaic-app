# -*- coding: utf-8 -*-
# 出典: 朝倉駿「リール1本制作キット v4.1」engine/hdr_sdr.py（2026-10-14 取り込み・自分の動画制作用途のみ）
# 変更: 無し（video_retouch.py が plan() と vf_for() を使う）
"""HDR（HLG）で撮った素材を、通常の色（SDR・Rec.709）へ直す。iPhone の「HDRビデオ」で撮った素材が対象。
build.py・face_track.py・telop_shadow_color.py がこのファイルを読み込み、素材の画素を読むすべての経路で同じ変換を掛ける
（書き出し・試写・顔位置の実測・影色の自動計測が食い違わないようにするため）。

しくみ: 素材の色の種類（color_transfer）を ffprobe で調べ、HLG（arib-std-b67）なら、変換用の3D LUT（.cube）を
  作業フォルダへその場で作り、ffmpeg の lut3d で掛ける。zscale / libplacebo は使わない（Homebrew の ffmpeg に入っていないことが多い）。
  変換後は「bt709 タグの付いた普通の10bit素材」として後段へ渡すので、以降の処理（色補正・クロップ・ズーム・テロップ）は通常の素材と同じ。
LUT の中身: HLG の逆OETF → OOTF（γ1.2・1000nit基準）→ BT.2020→BT.709 の行列 → 基準白203nit を1.0に × 露出係数
  → 明るい側をなだらかに圧縮（白飛びさせない）→ 1/2.4乗。
config:
  "hdr": 省略 = 自動（HLG のときだけ変換）/ false = 変換しない / true = タグが無くても HLG として変換する
  "hdr_exposure": 明るさの係数（既定 DEFAULT_EXPOSURE）。試写で明るすぎれば下げ、暗すぎれば上げる（0.05〜0.1刻み）
PQ（smpte2084・HDR10 / Dolby Vision の一部）は未対応。変換せずに案内を出して止める（黙って白飛びさせない）。
単体で確かめる: python3 hdr_sdr.py 動画   → 色の種類と、掛かるフィルタ列を表示"""
import json, os, shutil, subprocess, sys

HLG = "arib-std-b67"
PQ = "smpte2084"
# 既定の露出係数。iPhone の HLG 素材（人物・室内・白壁）7本×3コマを測り、同じ人を SDR で撮った素材と macOS の
# SDR 表示（クイックルック）に、肌・白壁の明るさが近く、白壁が白飛びしない値として決めた（0.25〜0.32 が妥当な範囲で、その中ほど）。
# 0.45 を超えると白壁が白に張り付き始める。素材や好みで合わないときは config "hdr_exposure" で変える。
DEFAULT_EXPOSURE = 0.30
LUT_SIZE = 33
KNEE = 0.75          # これより明るい側をなだらかに圧縮する（リニア値）

PQ_HINT = ("この素材は HDR（PQ / HDR10・Dolby Vision 系）で撮られていますが、このキットが自動で直せるのは HLG 方式だけです。\n"
           "このまま書き出すと白飛び・色ずれが出るため、止めています。次のどちらかで進めてください:\n"
           "  1. カメラの HDR をオフにして撮り直す（いちばん確実）\n"
           "  2. 撮影したアプリ・編集アプリで SDR（Rec.709）へ書き出したものを渡す\n"
           "  見え方を承知のうえでそのまま進めるときは、config に \"hdr\": false を入れてください")


def _find_bin(name):
    p = shutil.which(name)
    if p: return p
    for c in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if os.path.exists(c): return c
    raise FileNotFoundError(f"{name} が見つかりません。「brew install ffmpeg」を実行してください")


def probe(path, ffprobe=None):
    """映像の color_transfer / color_primaries / color_space を返す（取れない項目は ""）。動画でない・読めないときは {}。"""
    try:
        r = subprocess.run([ffprobe or _find_bin("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=color_transfer,color_primaries,color_space", "-of", "json", path],
                           capture_output=True, text=True)
        st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        return {"trc": st.get("color_transfer") or "", "primaries": st.get("color_primaries") or "",
                "space": st.get("color_space") or ""}
    except Exception:
        return {}


def make_lut(path, exposure, n=LUT_SIZE):
    """HLG(BT.2020) → SDR(Rec.709) の3D LUT（.cube）を書き出す。numpy が必要。"""
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("HDR（HLG）素材を通常の色へ直すには numpy が必要です。次を実行してから、もう一度お試しください:\n"
                           "  pip3 install numpy\n"
                           "  （「externally-managed-environment」エラーが出る場合は pip3 install --break-system-packages numpy）")
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    g = np.linspace(0, 1, n)
    B, G, R = np.meshgrid(g, g, g, indexing="ij")                 # .cube は R が最速で変化
    rgb = np.stack([R, G, B], -1)
    lin = np.where(rgb <= 0.5, rgb ** 2 / 3.0, (np.exp((rgb - c) / a) + b) / 12.0)   # HLG 逆OETF（シーンリニア 0..1）
    Y = (lin * np.array([0.2627, 0.6780, 0.0593])).sum(-1, keepdims=True)
    disp = lin * np.power(np.maximum(Y, 1e-6), 0.2)                # OOTF（γ1.2・1000nit基準）
    M = np.array([[1.6605, -0.5876, -0.0728], [-0.1246, 1.1329, -0.0083], [-0.0182, -0.1006, 1.1187]])   # BT.2020 → BT.709
    sdr = np.maximum((disp @ M.T) * (1000.0 / 203.0) * float(exposure), 0)             # 基準白203nitを1.0へ × 露出
    k = KNEE
    sdr = np.where(sdr > k, k + (1 - k) * np.tanh((sdr - k) / (1 - k)), sdr)           # 明るい側をなだらかに圧縮
    v = np.clip(sdr, 0, 1) ** (1 / 2.4)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"LUT_3D_SIZE {n}\n")
        f.write("\n".join("%.6f %.6f %.6f" % tuple(p) for p in v.reshape(-1, 3)) + "\n")
    os.replace(tmp, path)
    return path


def _esc(path):
    """ffmpeg のフィルタ文字列に書けるよう、パスの特殊文字をエスケープする（オプション値の段 → フィルタグラフの段）。"""
    s = path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    out = ""
    for ch in s:
        out += ("\\" + ch) if ch in "\\',;[]" else ch
    return out


def lut_file(work, exposure):
    return os.path.join(work, f"hdr_hlg_to_sdr_e{float(exposure):.3f}.cube")


def vf_for(lut):
    """HLG素材の先頭に置くフィルタ列（末尾にカンマは付けない）。出力は bt709 タグ付きの yuv420p10le。"""
    return ("scale=in_color_matrix=bt2020,format=gbrp10le,"
            f"lut3d={_esc(lut)},"
            "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p10le,"
            "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709")


def plan(path, work, setting=None, exposure=None, ffprobe=None, label="素材", quiet=False):
    """素材1本の扱いを決める。戻り値 (フィルタ列, 情報dict)。変換しないときのフィルタ列は ""。
    setting: None=自動 / False=変換しない / True=HLGとして強制変換。PQ は自動のとき SystemExit で止める。"""
    info = probe(path, ffprobe)
    trc = info.get("trc", "")
    info["mode"] = "sdr"
    say = (lambda *a: None) if quiet else print
    if setting is False:
        if trc in (HLG, PQ):
            say(f"⚠ {label}は HDR（{trc}）ですが、\"hdr\": false の指定どおり変換せずに使います（白飛び・色ずれが出ることがあります）")
            info["mode"] = "hdr_off"
        return "", info
    if trc == PQ and setting is not True:
        sys.exit(f"{label}: {path}\n" + PQ_HINT)
    if trc != HLG and setting is not True:
        return "", info
    exp = DEFAULT_EXPOSURE if exposure is None else float(exposure)
    if not (0.05 <= exp <= 4.0):
        sys.exit(f"hdr_exposure は 0.05〜4.0 の数値で指定してください（指定値: {exposure!r}。既定は {DEFAULT_EXPOSURE}）")
    os.makedirs(work, exist_ok=True)
    lut = lut_file(work, exp)
    if not (os.path.exists(lut) and os.path.getsize(lut) > 1000):
        try:
            make_lut(lut, exp)
        except RuntimeError as e:
            sys.exit(str(e))
    if trc == HLG:
        say(f"HDR(HLG) を検出→通常の色へ変換（露出 {exp:.2f}）: {label}")
    else:
        say(f"\"hdr\": true の指定により HLG として通常の色へ変換（露出 {exp:.2f}）: {label}"
            f"（素材のタグは {trc or '不明'}。HLG で撮っていない素材に掛けると暗く・くすんで見えます）")
    info.update(mode="hlg", exposure=exp, lut=lut)
    return vf_for(lut), info


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("使い方: python3 hdr_sdr.py 動画 [露出係数]")
    import tempfile
    _v = os.path.abspath(os.path.expanduser(sys.argv[1]))
    print("色の種類:", probe(_v))
    _vf, _info = plan(_v, tempfile.mkdtemp(prefix="hdr_sdr_"), exposure=float(sys.argv[2]) if len(sys.argv) > 2 else None)
    print("フィルタ列:", _vf or "（変換なし）")
