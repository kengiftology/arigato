# -*- coding: utf-8 -*-
"""見回りの1枚が「使える写真」かを、その場で確かめる（2026-09-10）。

比べない、という選択肢を無くすための係。クラウドは届いた2枚を必ず比べるので、
ぶれた1枚・向きのずれた1枚は、ここで止めて撮り直す。

測るのは3つ。どれもAIは使わず、numpyだけで1秒以内に終わる。
  ぶれ   … 輪郭の鋭さ（ラプラシアンの分散）。首を振っている最中の写真は小さく出る。
  ずれ   … 基準の写真（sink_ref.jpg・定位置で撮った1枚）との位置ずれ（画素）。
           クラウドと同じ位相相関。150px（1280幅）を超えると比較が嘘をつく（9/9実測）。
  静けさ … 1秒あけた2枚の差。動いている最中なら大きく出る。
"""
import io
import os
import numpy as np
from PIL import Image

W, H = 640, 360
REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sink_ref.jpg")
SHIFT_MAX = 150        # 1280幅の画素。クラウドの SHIFT_MAX_PX と同じ
BLUR_MIN = 50.0        # これより小さければぶれている（実測：ぶれた1枚29・鮮明な1枚77〜260）
STILL_MAX = 6.0        # 1秒あけた2枚の差がこれ以下なら止まっている（実測：止まっていて1.3〜3.2）
# 見本と同じ景色か（位置合わせの確かさ）。これ未満なら「違う景色」で使わない（2026-09-22）。
# ずれの数字だけを見ていたので、9/22 は壁を写した1枚を「ずれ小さい＝使える」と通していた。
# クラウドと同じ cv2 の確かさなら 0.08（合っている 0.09〜0.22／違う向き 0.00〜0.06）。
# cv2 が無いときの numpy 版は峰の高さの尺度が違うので 0.035（合っている 0.05〜0.06／違う 0.015〜0.025）。
CONF_MIN_CV2 = 0.08
CONF_MIN_NUMPY = 0.035


def gray(jpg: bytes) -> np.ndarray:
    im = Image.open(io.BytesIO(jpg)).convert("L").resize((W, H), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32) / 255.0


def shift_px(a: np.ndarray, b: np.ndarray) -> tuple:
    """a→b の位置ずれ（1280幅の画素）。cv2.phaseCorrelate と同じ計算。"""
    win = np.outer(np.hanning(H), np.hanning(W)).astype(np.float32)
    fa = np.fft.fft2((a - a.mean()) * win)
    fb = np.fft.fft2((b - b.mean()) * win)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.real(np.fft.ifft2(r))
    dy, dx = np.unravel_index(np.argmax(c), c.shape)
    if dx > W // 2:
        dx -= W
    if dy > H // 2:
        dy -= H
    return int(dx) * 2, int(dy) * 2, float(c.max())


def conf(a: np.ndarray, b: np.ndarray) -> tuple:
    """見本 a と b の確かさ。(値, 目安)。cv2 があればクラウドと同じ計算にそろえる。"""
    try:
        import cv2
        win = cv2.createHanningWindow((W, H), cv2.CV_32F)
        # 写しを渡す：phaseCorrelate は渡した配列を書き換える（窓を掛ける）。そのままだと
        # 呼び出し側の1枚が変わり、次の「動いているか」が同じ写真でも71と出た（9/23 00:42）
        (_, _), r = cv2.phaseCorrelate(a.copy(), b.copy(), win)
        return float(r), CONF_MIN_CV2
    except Exception:
        return shift_px(a, b)[2], CONF_MIN_NUMPY


def blur_score(a: np.ndarray) -> float:
    """輪郭の鋭さ。大きいほど鮮明。"""
    lap = (-4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1]
           + a[1:-1, :-2] + a[1:-1, 2:])
    return float(lap.var() * 1e4)


def still(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean() * 255)


_ref = [None]


def ref() -> np.ndarray | None:
    if _ref[0] is None and os.path.exists(REF):
        with open(REF, "rb") as f:
            _ref[0] = gray(f.read())
    return _ref[0]


def check(jpg: bytes, prev_jpg: bytes | None = None) -> dict:
    """1枚を測って {ok, why, shift, blur, still} を返す。"""
    g = gray(jpg)
    out = {"blur": round(blur_score(g), 2), "shift": None, "still": None, "conf": None,
           "ok": True, "why": ""}
    r = ref()
    if r is not None:
        dx, dy, _ = shift_px(r, g)
        out["shift"] = (dx, dy)
        c, cmin = conf(r, g)
        out["conf"] = round(c, 3)
        if c < cmin:
            out["ok"], out["why"] = False, "違う景色"   # 確かさが低いと、ずれの数字は当てにならない
        elif abs(dx) > SHIFT_MAX or abs(dy) > SHIFT_MAX:
            out["ok"], out["why"] = False, "ずれ"
    if prev_jpg is not None:
        out["still"] = round(still(gray(prev_jpg), g), 2)
        if out["still"] > STILL_MAX and out["ok"]:
            out["ok"], out["why"] = False, "動いている"
    if out["blur"] < BLUR_MIN and out["ok"]:
        out["ok"], out["why"] = False, "ぶれ"
    return out


if __name__ == "__main__":
    import sys
    r = ref()
    for p in sys.argv[1:]:
        with open(p, "rb") as f:
            d = f.read()
        g = gray(d)
        s = shift_px(r, g) if r is not None else None
        print("%-50s blur=%6.2f shift=%s" % (os.path.basename(p), blur_score(g), s), flush=True)


def shift_of(jpg: bytes) -> tuple:
    """この1枚が、基準の写真からどれだけずれているか (dx, dy, 確度)。

    2026-09-12 夜：画角を探し直すのに使う。check() と同じ計算だが、
    合否ではなく数字をそのまま返す。基準が読めなければ (None, None, 0.0)。"""
    try:
        ref = gray(open(REF, "rb").read())
    except Exception:
        return None, None, 0.0
    return shift_px(ref, gray(jpg))
