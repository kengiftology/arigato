# -*- coding: utf-8 -*-
"""
精霊アニメ → 実機用バイナリ書き出し（PC側）
============================================
animate.py のアニメを「セルグリッド＋パレット」の .bin に変換する。
XIAO側は device_player.py がこれをパラパラ漫画として再生する（差分描画）。

形式（1ファイル=1アニメ）:
  [フレーム数:1B] を先頭に、フレームごとに
  [表示ms:2B big] [パレット 6色×RGB565 2B =12B] [セル 32×32=1024B(色番号0..5)]
  色番号: 0=背景 1=体 2=模様 3=アクセント 4=目口 5=ほっぺ

使い方: python export_device.py <人のID>   → spirits_out/dev_<ID>_<anim>.bin
"""
import colorsys
import struct
import sys
from pathlib import Path

import animate
import designer
from designer import _hex, _dim

OUT = Path(__file__).parent.parent / "spirits_out"
GRID = 32
BG = (247, 240, 224)   # あつ森風のあたたかい背景


def vivify(rgb):
    """明るい背景用の色補正: 暗背景向けに選ばれた色を、彩度を上げ中明度に寄せる"""
    h, l, v = colorsys.rgb_to_hls(*[x / 255 for x in rgb])
    v = max(0.34, min(1.0, v * 1.6 + 0.08))   # 彩度に下限＝どの子も血色が出る
    l = min(0.68, max(0.46, l))
    r, g, b = colorsys.hls_to_rgb(h, l, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def rgb565(rgb):
    r, g, b = rgb
    c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return struct.pack(">H", c)


class Capture(dict):
    pass


def _capture_frame(sheet, rows, face_cells=None, cheek_cells=None, extra=None,
                   M=0.1, face_dy=0, scale=7, margin=8):
    """animate.frame() の代わりに、描画せず構成情報だけを記録する"""
    return Capture(sheet=sheet, rows=list(rows), face=face_cells, cheeks=cheek_cells,
                   extra=extra, M=M, dy=face_dy)


def to_bin_frame(fr: Capture, dur_ms: int) -> bytes:
    sheet, rows, dy, M = fr["sheet"], fr["rows"], fr["dy"], fr["M"]
    body = {(c, r) for r, row in enumerate(rows) for c, ch in enumerate(row) if ch in "BP"}  # 顔は体+模様の上（最前面）
    grid = bytearray(GRID * GRID)
    idx = {"B": 1, "P": 2, "A": 3}
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch in idx:
                grid[r * GRID + c] = idx[ch]
    for cells, code in [(fr["cheeks"], 5), (fr["face"], 4)]:
        for c, r in (cells or []):
            rr = r + dy
            if (c, rr) in body:
                grid[rr * GRID + c] = code
    for c, r in (fr["extra"] or []):
        if 0 <= c < GRID and 0 <= r < GRID:
            grid[r * GRID + c] = 3

    h, l, s = colorsys.rgb_to_hls(*[v / 255 for v in _hex(sheet["colors"]["body"])])
    ink = tuple(int(v * 255) for v in colorsys.hls_to_rgb(h, 0.13, min(0.35, s)))
    cheek = tuple(int(v * 255) for v in colorsys.hls_to_rgb((h + 0.11) % 1, 0.62, 0.55))
    pal = (rgb565(BG) + rgb565(vivify(_dim(_hex(sheet["colors"]["body"]), M)))
           + rgb565(vivify(_dim(_hex(sheet["colors"]["pattern"]), M)))
           + rgb565(vivify(_dim(_hex(sheet["colors"]["accent"]), M)))
           + rgb565(ink) + rgb565(cheek))
    return struct.pack(">H", min(65535, dur_ms)) + pal + bytes(grid)


def export(pid: str):
    animate.frame = _capture_frame               # 描画をフックして構成だけ抜く
    sheet = designer.generate(pid)
    OUT.mkdir(exist_ok=True)
    for name, fn in animate.ANIMS.items():
        frames = fn(sheet)
        blob = bytes([len(frames)]) + b"".join(to_bin_frame(f, d) for f, d in frames)
        p = OUT / f"dev_{pid}_{name}.bin"
        p.write_bytes(blob)
        print(f"→ {p.name}  {len(frames)}フレーム {len(blob)}B")


if __name__ == "__main__":
    export(sys.argv[1] if len(sys.argv) > 1 else "kuwahara")
