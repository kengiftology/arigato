# -*- coding: utf-8 -*-
"""
成長 — 世話の累積回数で、同じ子が育つ（式だけ・API不要）
========================================================
  段階:  たまご(0) → ちび(〜10回) → ふつう(〜30回) → おとな(30回〜)
  連続:  段階の中でも回数に応じて体が少しずつ大きくなる

  ちび   = 基本形を縮小（0.6〜0.85倍）。目は相対的に大きく（幼さ）
  ふつう = 基本形（エージェント創作そのまま）
  おとな = 基本形を拡大（1.0〜1.25倍）＋ かざり(A)を1〜2ドット伸ばす

使い方:  python growth.py <人のID> <世話回数>      → その時点の姿を spirit_grow.png に
         python growth.py <人のID> --demo           → 0,3,8,12,20,30,45,60 回の並び
"""
import json, sys
from pathlib import Path
from designer import SHEET_DIR, GRID, render

STAGES = [(0, "egg"), (1, "chibi"), (10, "normal"), (30, "adult")]


def stage_of(care: int) -> str:
    s = "egg"
    for th, name in STAGES:
        if care >= th:
            s = name
    return s


def scale_of(care: int) -> float:
    """世話回数 → 体の倍率（連続）"""
    if care <= 0:   return 0.0
    if care < 10:   return 0.60 + 0.25 * (care / 10)          # 0.60→0.85
    if care < 30:   return 0.85 + 0.15 * ((care - 10) / 20)   # 0.85→1.00
    return min(1.25, 1.00 + 0.25 * ((care - 30) / 40))        # 1.00→1.25


def scale_art(art, k: float):
    """32×32文字グリッドを底そろえ・中央寄せで k 倍に（最近傍）"""
    src = [list(r) for r in art]
    # 元の有効範囲
    rows = [i for i, r in enumerate(art) if r.strip(".")]
    if not rows: return list(art)
    top, bot = rows[0], rows[-1]
    cols = [c for r in art for c, ch in enumerate(r) if ch != "."]
    left, right = min(cols), max(cols)
    h, w = bot - top + 1, right - left + 1
    nh, nw = max(1, round(h * k)), max(1, round(w * k))
    out = [["."] * GRID for _ in range(GRID)]
    base_bottom = 27                                  # 地面の行
    ox = round(15.5 - nw / 2)
    oy = base_bottom - nh + 1
    for yy in range(nh):
        sy = top + min(h - 1, int(yy / k))
        for xx in range(nw):
            sx = left + min(w - 1, int(xx / k))
            ty, tx = oy + yy, ox + xx
            if 0 <= ty < GRID and 0 <= tx < GRID:
                out[ty][tx] = src[sy][sx]
    return ["".join(r) for r in out]


def grow_accent(art, n=1):
    """おとな: かざり(A)を上に n ドット伸ばす（生え際の上に同じ列を足す）"""
    g = [list(r) for r in art]
    for _ in range(n):
        adds = []
        for r in range(1, GRID):
            for c in range(GRID):
                if g[r][c] == "A" and g[r - 1][c] == ".":
                    adds.append((r - 1, c))
        for r, c in adds:
            g[r][c] = "A"
    return ["".join(r) for r in g]


def scale_face(face, k: float):
    """顔パラメータも体に追従。ちびは目を相対的に大きく（幼い）"""
    f = dict(face)
    top_shift = round((1 - k) * 12)                   # 体が縮むぶん顔は下がる（底そろえ）
    f["eye_y"] = min(24, face["eye_y"] + top_shift)
    f["mouth_y"] = min(26, face["mouth_y"] + top_shift)
    f["eye_gap"] = max(2, round(face["eye_gap"] * k))
    f["mouth_w"] = max(3, round(face["mouth_w"] * k))
    if k < 0.85:
        f["eye_size"] = "big"                         # ちびは目が大きい
    return f


def grown_sheet(sheet: dict, care: int) -> dict:
    """世話回数に応じた姿のキャラシート（art/face を差し替えたコピー）"""
    st = stage_of(care)
    k = scale_of(care)
    out = dict(sheet)
    if st == "egg":
        from animate import egg_rows
        out["art"] = egg_rows()
        out["face"] = dict(sheet["face"], eye_y=40, mouth_y=40)   # 顔なし
        return out
    art = scale_art(sheet["art"], k)
    if st == "adult":
        art = grow_accent(art, 1 if care < 45 else 2)
    out["art"] = art
    out["face"] = scale_face(sheet["face"], k)
    out["stage"] = st
    out["scale"] = k
    return out


if __name__ == "__main__":
    pid = sys.argv[1]
    sheet = json.loads((SHEET_DIR / f"{pid}.json").read_text(encoding="utf-8"))
    if len(sys.argv) > 2 and sys.argv[2] == "--demo":
        from PIL import Image
        cares = [0, 3, 8, 12, 20, 30, 45, 60]
        size = GRID * 7 + 16
        sheet_img = Image.new("RGB", (size * len(cares), size), (13, 15, 20))
        for i, c in enumerate(cares):
            render(grown_sheet(sheet, c), 0.1, 0.1, "_g.png")
            sheet_img.paste(Image.open("_g.png"), (i * size, 0))
        Path("_g.png").unlink()
        sheet_img.save("../growth_demo.png"); print("→ ../growth_demo.png", cares)
    else:
        care = int(sys.argv[2]); g = grown_sheet(sheet, care)
        render(g, 0.1, 0.1, "spirit_grow.png")
        print(f"care={care} stage={g.get('stage')} scale={g.get('scale')}")
