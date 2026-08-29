# -*- coding: utf-8 -*-
"""
人ごとの固有キャラ生成 — まるい精霊（地霊）v2
============================================
「同じ場所でも、見る人ごとに違う精霊が宿る」（風景＝主観）を顔で実現する。

v2: 色だけでなくシルエットで個性を出す。
  遺伝子 = 体型(6種) × かざり(7種) × 模様(4種) × 目の形(4種) × 目の大きさ
           × 口ぐせ × ほっぺ × 体色  → 数千通りの組み合わせ

仕組み:
  人のID（名前・端末ID・将来は顔ベクトル） → ハッシュ → 種（シード）
  → すべての遺伝子が計算で決まる。同じ人にはいつも同じ子。API不要・オフライン。

気分は従来どおり M(散らかり度)・N(放置度)。怒り顔は存在しない（設計の憲法）。
32×32マス・1マス7px → 240×240 の液晶(ST7789)にそのまま載る。

使い方:
  python face_gen.py <人のID> [M] [N]     → spirit_<ID>.png
  python face_gen.py --gallery            → 見本ギャラリー
"""
import colorsys
import hashlib
import math
import random
import sys

GRID = 32
M_TH = [0.20, 0.45, 0.70, 0.90]
N_TH = [0.20, 0.40, 0.60, 0.80]


def q(x, th):
    for i, t in enumerate(th):
        if x < t:
            return i
    return len(th)


def rect(c0, c1, r0, r1):
    return {(c, r) for c in range(c0, c1 + 1) for r in range(r0, r1 + 1)}


def disc(cx, cy, rad):
    return {(c, r) for c in range(GRID) for r in range(GRID)
            if (c - cx) ** 2 + (r - cy) ** 2 <= rad * rad}


def mirror(cells):
    return {(31 - c, r) for c, r in cells}


# ---------- 人のID → 遺伝子 ----------
def traits_of(person_id: str) -> dict:
    seed = int.from_bytes(hashlib.sha256(person_id.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    return {
        "hue": rng.uniform(0, 360),
        "sat": rng.uniform(0.38, 0.62),
        "shape": rng.choice(["round", "tall", "wide", "square", "onigiri", "drop"]),
        "deco": rng.choice(["cat", "rabbit", "horns", "antenna", "leaf", "ahoge", "none"]),
        "pattern": rng.choice(["belly", "spots", "stripe", "none"]),
        "eye_style": rng.randrange(4),           # 0丸 1たれ目 2つり目 3ジト目
        "eye_size": rng.choice(["big", "small"]),
        "eye_gap": rng.choice([3, 4, 5]),
        "mouth_w": rng.choice([3, 4, 5]),
        "smile_bias": rng.choice([-1, 0, 0, 1]),
        "cheeks": rng.random() < 0.5,
        "spot_seed": rng.randrange(10 ** 6),
    }


# ---------- 体型（行ごとの半幅で形を作る） ----------
def body_cells(tr):
    cx = 15.5
    shape = tr["shape"]
    a, b = {                                     # 横半径, 縦半径
        "round":   (10.5, 10.5),
        "tall":    (8.5, 12.0),
        "wide":    (12.0, 8.5),
        "square":  (10.0, 10.0),
        "onigiri": (11.0, 10.5),
        "drop":    (9.5, 11.5),
    }[shape]
    cy = 27 - b                                  # 下端をそろえる（地に足がつく）
    cells = set()
    r0, r1 = math.ceil(cy - b), math.floor(cy + b)
    for r in range(r0, r1 + 1):
        t = (r - cy) / b                         # -1(頭)〜+1(底)
        t = max(-1.0, min(1.0, t))
        if shape == "square":
            w = a * (1 - abs(t) ** 4) ** 0.25    # 角ばる
        elif shape == "onigiri":
            w = a * math.sqrt(max(0, 1 - t * t)) * (0.55 + 0.45 * (t + 1) / 2)  # 上すぼみ
        elif shape == "drop":
            w = a * math.sqrt(max(0, 1 - t * t)) * (0.62 + 0.38 * (t + 1) / 2)  # しずく
        else:
            w = a * math.sqrt(max(0, 1 - t * t))
        for c in range(GRID):
            if abs(c - cx) <= w:
                cells.add((c, r))
    return cells, cx, cy, a, b


# ---------- かざり（大きく・シルエットが変わるレベルで） ----------
def deco_cells(tr, cx, cy, a, b):
    top = round(cy - b)
    ex = round(cx - a * 0.55)                    # 耳の付け根x（左）
    d = tr["deco"]
    s = set()
    if d == "cat":                               # 三角耳（大）
        for k in range(4):
            s |= rect(ex - (3 - k) // 2, ex + (3 - k) // 2, top - 3 + k, top - 3 + k)
        s = {(c, r) for c, r in s} | rect(ex - 1, ex + 1, top - 1, top)
    elif d == "rabbit":                          # 長い耳
        s = rect(ex - 1, ex, top - 6, top) | {(ex - 1, top - 7), (ex, top - 7)}
    elif d == "horns":                           # ツノ（外向き）
        s = {(ex, top - 1), (ex - 1, top - 2), (ex - 1, top - 3), (ex - 2, top - 3)}
    elif d == "antenna":                         # アンテナ＋玉（玉を大きく＝十字架に見せない）
        s = rect(16, 16, top - 3, top - 1) | disc(16, top - 5, 1.9)
        return s                                 # 中央のみ・ミラーしない
    elif d == "leaf":                            # 葉っぱ
        s = {(16, top - 1), (16, top - 2), (17, top - 2), (18, top - 3), (17, top - 3)}
        return s
    elif d == "ahoge":                           # アホ毛
        s = {(16, top - 1), (17, top - 2), (18, top - 2)}
        return s
    else:
        return set()
    return s | mirror(s)


# ---------- 模様 ----------
def pattern_cells(tr, body, cx, cy, a, b):
    p = tr["pattern"]
    if p == "belly":                             # おなか（下半分の明るい楕円）
        return {(c, r) for c, r in body
                if ((c - cx) / (a * 0.52)) ** 2 + ((r - (cy + b * 0.38)) / (b * 0.48)) ** 2 <= 1}
    if p == "spots":                             # ぶち（3つ）
        rng = random.Random(tr["spot_seed"])
        s = set()
        for _ in range(3):
            sc = cx + rng.uniform(-a * 0.6, a * 0.6)
            sr = cy + rng.uniform(-b * 0.5, b * 0.6)
            s |= disc(sc, sr, rng.choice([1.4, 1.8]))
        return s & body
    if p == "stripe":                            # 底のしま2本
        rows = {round(cy + b * 0.55), round(cy + b * 0.80)}
        return {(c, r) for c, r in body if r in rows}
    return set()


# ---------- 表情（M→口 / N→目。喜び>嘆き・怒りなし） ----------
def umouth(depth, cx, cy, wh, thick=2):
    cells = set()
    for c in range(cx - wh, cx + wh):
        t = (c - cx + 0.5) / wh
        y = cy + depth * (0.5 - t * t)
        base = round(y)
        for k in range(thick):
            cells.add((c, base + k))
    return cells


def eye_cells(style, size, cx, cy, openness):
    if openness <= 0:                            # とじ気味の線
        return rect(cx - 1, cx + 1, cy, cy)
    big = (size == "big")
    if style == 0:                               # 丸
        return disc(cx, cy, (1.9 if big else 1.2) if openness == 2 else 1.0)
    if style == 1:                               # たれ目（外側が下がる＝ハの字でやさしい）
        e = {(cx - 1, cy + 1), (cx, cy), (cx + 1, cy - 1)}
        if big:
            e |= {(cx - 1, cy + 2), (cx, cy + 1), (cx + 1, cy)}
        return e
    if style == 2:                               # りりしい目（外側がわずかに上・怒りには見せない）
        e = {(cx - 1, cy - 1), (cx, cy), (cx + 1, cy)}
        if big:
            e |= {(cx, cy + 1), (cx + 1, cy + 1)}
        return e
    e = rect(cx - 1, cx + 1, cy, cy)             # ジト目
    if big:
        e |= rect(cx - 2, cx + 2, cy, cy)
    return e


def face_cells(tr, cx, cy, b, M, N):
    mi, ni = q(M, M_TH), q(N, N_TH)
    depth = max(-1, min(4, [4, 3, 2, 0, -1][mi] + tr["smile_bias"]))
    openness = [2, 2, 1, 1, 0][ni]
    sink = [0, 0, 1, 1, 2][ni]
    ey = round(cy - b * 0.28) + sink
    ec_l = round(cx - tr["eye_gap"])
    eyes = eye_cells(tr["eye_style"], tr["eye_size"], ec_l, ey, openness)
    eyes |= mirror(eyes)
    mouth = umouth(depth, 16, round(cy + b * 0.22), tr["mouth_w"])
    cheeks = set()
    if tr["cheeks"] and mi <= 2:
        ch = {(ec_l - 2, ey + 3), (ec_l - 3, ey + 3)}
        cheeks = ch | mirror(ch)
    return eyes | mouth, cheeks


# ---------- 色 ----------
def colors_of(tr, M):
    sat = max(0.15, tr["sat"] - 0.25 * M)        # 散らかると色がくすむ
    lig = 0.55 - 0.10 * M
    def hls(h, l, s):
        r, g, b = colorsys.hls_to_rgb(h % 360 / 360, l, s)
        return (int(r * 255), int(g * 255), int(b * 255))
    return {
        "body": hls(tr["hue"], lig, sat),
        "ink": hls(tr["hue"], 0.14, 0.25),
        "pattern": hls(tr["hue"], lig + 0.16, sat * 0.9) if tr["pattern"] == "belly"
                   else hls(tr["hue"], lig - 0.16, sat),
        "cheek": hls(tr["hue"] + 40, 0.62, 0.55),
    }


# ---------- 合成・描画 ----------
def build(person_id, M, N):
    tr = traits_of(person_id)
    body, cx, cy, a, b = body_cells(tr)
    deco = deco_cells(tr, cx, cy, a, b)
    body_all = body | deco
    pattern = pattern_cells(tr, body, cx, cy, a, b)
    face, cheeks = face_cells(tr, cx, cy, b, M, N)
    face &= body
    cheeks = (cheeks & body) - face
    pattern -= face | cheeks
    return tr, body_all, pattern, cheeks, face


def to_png(person_id, M, N, path, scale=7, margin=8):
    from PIL import Image, ImageDraw
    tr, body, pattern, cheeks, face = build(person_id, M, N)
    col = colors_of(tr, M)
    size = GRID * scale + margin * 2
    img = Image.new("RGB", (size, size), (13, 15, 20))
    d = ImageDraw.Draw(img)
    for cells, c in [(body, col["body"]), (pattern, col["pattern"]),
                     (cheeks, col["cheek"]), (face, col["ink"])]:
        for cc, rr in cells:
            if 0 <= cc < GRID and 0 <= rr < GRID:
                d.rectangle([margin + cc * scale, margin + rr * scale,
                             margin + (cc + 1) * scale - 1, margin + (rr + 1) * scale - 1], fill=c)
    img.save(path)
    return tr


def gallery(people, states, path="spirits_gallery.png", scale=7, margin=8):
    from PIL import Image
    import os
    size = GRID * scale + margin * 2
    sheet = Image.new("RGB", (size * len(people), size * len(states)), (13, 15, 20))
    for x, pid in enumerate(people):
        for y, (m, n) in enumerate(states):
            to_png(pid, m, n, "_tmp_cell.png")
            sheet.paste(Image.open("_tmp_cell.png"), (x * size, y * size))
    sheet.save(path)
    os.remove("_tmp_cell.png")
    print(f"→ {path}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--gallery":
        people = ["kuwahara", "sugiyama", "kaneko", "kazu", "matsukawa", "guest01"]
        states = [(0.1, 0.1), (0.6, 0.4), (0.9, 0.9)]
        gallery(people, states)
    elif len(sys.argv) >= 2:
        pid = sys.argv[1]
        M = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
        N = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
        tr = to_png(pid, M, N, f"spirit_{pid}.png")
        print(f"→ spirit_{pid}.png")
        print("  " + " ".join(f"{k}={v}" for k, v in tr.items() if k not in ("spot_seed",)))
    else:
        print("使い方: python face_gen.py <人のID> [M] [N] / python face_gen.py --gallery")
