# -*- coding: utf-8 -*-
"""
精霊アニメーション — Codex Pets流の状態別モーション＋孵化
=========================================================
エージェントが創作した1枚のドット絵（characters/<id>.json）から、
パラメトリック変形だけでアニメGIFを作る。API不要・決定的。

状態（場所の状態と人の気配に対応）:
  hatch  … 卵から孵化（初めての人が来たとき・一度きりの誕生体験）
  idle   … 呼吸＋まばたき（ふだん）
  happy  … 跳ねて喜ぶ（片づけてもらった直後）
  sad    … しおれる（散らかり・放置）
  sleep  … 眠る（長い放置・夜）
  notice … 気づく（人が来た＝PIR反応）

使い方:
  python animate.py <人のID>            → gif一式を spirits_out/ に出力
  python animate.py <人のID> hatch      → 1状態だけ
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from designer import (GRID, generate, _hex, _dim, eye_cells, umouth, rect, disc,
                      mirror, q, M_TH)
import colorsys

HERE = Path(__file__).parent
OUT = HERE.parent / "spirits_out"


# ---------- ドット絵（32行の文字列）の変形 ----------
def blank():
    return "." * GRID


def top_row(rows):
    for i, r in enumerate(rows):
        if r.strip("."):
            return i
    return GRID


def shift_v(rows, k):
    """k>0で下へ、k<0で上へ（はみ出しは捨てる）"""
    if k > 0:
        return [blank()] * k + rows[:-k]
    if k < 0:
        return rows[-k:] + [blank()] * (-k)
    return list(rows)


def squash(rows, n=1):
    """背を n 低くする（底は固定）＝ぷにっと潰れる"""
    t = top_row(rows)
    out = list(rows)
    for _ in range(n):
        del out[t + 2]
        out.insert(0, blank())
    return out


def stretch(rows, n=1):
    """背を n 高くする＝伸びる"""
    t = top_row(rows)
    out = list(rows)
    for _ in range(n):
        out.insert(t + 2, out[t + 2])
        del out[0]
    return out


def lean(rows, k=1):
    """上半身だけ横にずらす＝かしげる"""
    t = top_row(rows)
    h = (27 - t) // 2
    out = []
    for i, r in enumerate(rows):
        if i < t + h:
            out.append(("." * k + r)[:GRID] if k > 0 else (r[-k:] + "." * -k))
        else:
            out.append(r)
    return out


def chibi(rows):
    """半分サイズ（孵化直後のちび姿）。底をそろえて中央寄せ"""
    small = [r[::2] for r in rows[::2]]          # 16×16に間引き
    out = [blank()] * GRID
    for i, r in enumerate(small):
        out[11 + i] = ("." * 8 + r + "." * 8)[:GRID]
    return out


# ---------- 表情（状態を直接指定して作る） ----------
def expr(face, depth, openness, sink=0, cheeks_on=False):
    ey = face["eye_y"] + sink
    ec_l = round(15.5 - face["eye_gap"])
    closed_happy = depth >= 2                    # 口が笑ってる時だけ「＾」閉じ目。悲しみ/眠りは平らな線
    eyes = eye_cells(face["eye_style"], face["eye_size"], ec_l, ey, openness, closed_happy)
    eyes |= mirror(eyes)
    eye_bottom = ey + (1 if face["eye_size"] == "big" else 0)
    mouth_y = max(face["mouth_y"], eye_bottom + 3)          # 目と口は最低2行あける
    mouth = umouth(depth, 16, mouth_y, face["mouth_w"])
    cheeks = set()
    if cheeks_on and face.get("cheeks"):
        ch = {(ec_l - 2, ey + 3), (ec_l - 3, ey + 3)}
        cheeks = ch | mirror(ch)
    return eyes | mouth, cheeks


# ---------- 1フレーム描画 ----------
def frame(sheet, rows, face_cells=None, cheek_cells=None, extra=None,
          M=0.1, face_dy=0, scale=7, margin=8):
    body_col = _dim(_hex(sheet["colors"]["body"]), M)
    pat_col = _dim(_hex(sheet["colors"]["pattern"]), M)
    acc_col = _dim(_hex(sheet["colors"]["accent"]), M)
    h, l, s = colorsys.rgb_to_hls(*[v / 255 for v in _hex(sheet["colors"]["body"])])
    ink = tuple(int(v * 255) for v in colorsys.hls_to_rgb(h, 0.13, min(0.35, s)))
    cheek = tuple(int(v * 255) for v in colorsys.hls_to_rgb((h + 0.11) % 1, 0.62, 0.55))

    body_cells = {(c, r) for r, row in enumerate(rows) for c, ch in enumerate(row) if ch in "BP"}  # 顔は最前面
    size = GRID * scale + margin * 2
    img = Image.new("RGB", (size, size), (13, 15, 20))
    d = ImageDraw.Draw(img)

    def put(c, r, col):
        if 0 <= c < GRID and 0 <= r < GRID:
            d.rectangle([margin + c * scale, margin + r * scale,
                         margin + (c + 1) * scale - 1, margin + (r + 1) * scale - 1], fill=col)

    colmap = {"B": body_col, "P": pat_col, "A": acc_col}
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch in colmap:
                put(c, r, colmap[ch])
    if cheek_cells:
        for c, r in cheek_cells:
            if (c, r + face_dy) in body_cells:
                put(c, r + face_dy, cheek)
    if face_cells:
        for c, r in face_cells:
            if (c, r + face_dy) in body_cells:
                put(c, r + face_dy, ink)
    if extra:                                     # 「!」「Zzz」などの記号
        for c, r in extra:
            put(c, r, acc_col)
    return img


# ---------- 卵 ----------
def egg_rows():
    rows = [list(blank()) for _ in range(GRID)]
    cx, cy, a, b = 15.5, 19.5, 6.8, 8.2
    for r in range(GRID):
        for c in range(GRID):
            if ((c - cx) / a) ** 2 + ((r - cy) / b) ** 2 <= 1:
                rows[r][c] = "B"
    # 模様（点々）
    for c, r in [(13, 17), (18, 15), (15, 21), (19, 20)]:
        rows[r][c] = "P"
    return ["".join(r) for r in rows]


CRACK = [(15, 13), (16, 14), (14, 14), (17, 15), (15, 16), (13, 15)]


# ---------- 状態別アニメ ----------
def anim_idle(sheet, M=0.15):
    a = sheet["art"]
    f = sheet["face"]
    smile = max(-1, min(4, 3 + f["smile_bias"]))
    e_open, ch = expr(f, smile, 2, cheeks_on=True)
    e_half, _ = expr(f, smile, 1)
    e_shut, _ = expr(f, smile, 0)
    return [
        (frame(sheet, a, e_open, ch, M=M), 900),
        (frame(sheet, squash(a), e_open, ch, M=M, face_dy=1), 700),
        (frame(sheet, a, e_open, ch, M=M), 500),
        (frame(sheet, a, e_shut, ch, M=M), 120),
        (frame(sheet, a, e_half, ch, M=M), 80),
        (frame(sheet, a, e_open, ch, M=M), 900),
        (frame(sheet, squash(a), e_open, ch, M=M, face_dy=1), 700),
    ]


def anim_happy(sheet):
    a = sheet["art"]
    f = sheet["face"]
    e_joy, ch = expr(f, 4, 2, cheeks_on=True)
    e_shut, _ = expr(f, 4, 0)
    up2, up4 = shift_v(a, -2), shift_v(a, -4)
    return [
        (frame(sheet, squash(a, 2), e_shut, ch, M=0, face_dy=2), 180),
        (frame(sheet, stretch(shift_v(a, -3), 1), e_joy, ch, M=0, face_dy=-3), 160),
        (frame(sheet, shift_v(a, -5), e_joy, ch, M=0, face_dy=-5), 200),
        (frame(sheet, shift_v(a, -2), e_joy, ch, M=0, face_dy=-2), 140),
        (frame(sheet, squash(a, 1), e_joy, ch, M=0, face_dy=1), 160),
        (frame(sheet, a, e_joy, ch, M=0), 700),
        (frame(sheet, squash(a, 2), e_shut, ch, M=0, face_dy=2), 180),
        (frame(sheet, shift_v(a, -4), e_joy, ch, M=0, face_dy=-4), 220),
        (frame(sheet, a, e_joy, ch, M=0), 900),
    ]


def anim_sad(sheet, M=0.85):
    a = sheet["art"]
    f = sheet["face"]
    e_low, _ = expr(f, 0, 1, sink=1)
    e_shut, _ = expr(f, -1, 1, sink=2)           # 半目まで（完全に閉じると寝顔に見える）
    sq = squash(a, 1)
    return [
        (frame(sheet, a, e_low, M=M), 1200),
        (frame(sheet, sq, e_low, M=M, face_dy=1), 1000),
        (frame(sheet, squash(a, 2), e_shut, M=M, face_dy=2), 1600),
        (frame(sheet, sq, e_low, M=M, face_dy=1), 1000),
    ]


def anim_sleep(sheet):
    a = squash(sheet["art"], 2)
    f = sheet["face"]
    e_shut, _ = expr(f, 1, 0, sink=2)
    t = top_row(a)
    z1 = {(22, t - 1), (23, t - 1), (22, t - 2), (23, t - 2)}
    z2 = z1 | {(25, t - 4), (26, t - 4), (25, t - 3)}
    return [
        (frame(sheet, a, e_shut, M=0.3, face_dy=2, extra=z1), 1100),
        (frame(sheet, squash(a, 1), e_shut, M=0.3, face_dy=3, extra=z2), 1100),
    ]


def anim_notice(sheet):
    a = sheet["art"]
    f = sheet["face"]
    smile = max(-1, min(4, 3 + f["smile_bias"]))
    e_wide, ch = expr(f, 2, 2)
    e_joy, _ = expr(f, smile, 2, cheeks_on=True)
    t = top_row(a)
    mark = {(28, 1), (28, 2), (28, 3), (28, 5)}   # 「!」右上固定（キャラ拡大後も見える）
    return [
        (frame(sheet, a, e_wide, M=0.2), 150),
        (frame(sheet, stretch(a, 1), e_wide, M=0.2, face_dy=-1, extra=mark), 500),
        (frame(sheet, lean(a, 1), e_wide, M=0.2, extra=mark), 450),
        (frame(sheet, lean(a, -1), e_joy, ch, M=0.2), 450),
        (frame(sheet, a, e_joy, ch, M=0.2), 900),
    ]


def anim_hatch(sheet):
    egg = egg_rows()
    crack1 = [r if i not in {13, 14} else
              "".join("." if (c, i) in CRACK[:3] else ch for c, ch in enumerate(r))
              for i, r in enumerate(egg)]
    crack2 = [r if i not in {13, 14, 15, 16} else
              "".join("." if (c, i) in CRACK else ch for c, ch in enumerate(r))
              for i, r in enumerate(egg)]
    f = sheet["face"]
    smile = max(-1, min(4, 3 + f["smile_bias"]))
    e_joy, ch = expr(f, smile, 2, cheeks_on=True)
    e_shut, _ = expr(f, smile, 0)
    ch_rows = chibi(sheet["art"])
    burst = {(9, 12), (22, 12), (7, 18), (24, 18), (11, 8), (20, 8), (15, 6)}
    frames = [
        (frame(sheet, egg, M=0), 1300),
        (frame(sheet, lean(egg, 1), M=0), 220),
        (frame(sheet, lean(egg, -1), M=0), 220),
        (frame(sheet, egg, M=0), 700),
        (frame(sheet, lean(egg, 1), M=0), 160),
        (frame(sheet, lean(egg, -1), M=0), 160),
        (frame(sheet, crack1, M=0), 800),
        (frame(sheet, crack2, M=0), 700),
        (frame(sheet, ["." * GRID] * GRID, extra=burst, M=0), 350),
        (frame(sheet, ch_rows, M=0), 900),        # ちび（顔はまだ・ねぼけ）
        (frame(sheet, sheet["art"], e_shut, M=0, face_dy=0), 600),
        (frame(sheet, sheet["art"], e_joy, ch, M=0), 500),
        (frame(sheet, shift_v(sheet["art"], -3), e_joy, ch, M=0, face_dy=-3), 250),
        (frame(sheet, sheet["art"], e_joy, ch, M=0), 1800),
    ]
    return frames


def anim_egg(sheet):
    """白い卵（まだ誰でもない状態）。初めての顔を見てから人格が生まれるまでの待ち姿。
    ときどき、ふるっと揺れる。"""
    egg = egg_rows()
    return [
        (frame(sheet, egg, M=0), 2600),
        (frame(sheet, lean(egg, 1), M=0), 200),
        (frame(sheet, lean(egg, -1), M=0), 200),
        (frame(sheet, egg, M=0), 3000),
        (frame(sheet, lean(egg, -1), M=0), 180),
        (frame(sheet, lean(egg, 1), M=0), 180),
    ]


ANIMS = {"hatch": anim_hatch, "idle": anim_idle, "happy": anim_happy,
         "sad": anim_sad, "sleep": anim_sleep, "notice": anim_notice,
         "egg": anim_egg}


def save_gif(frames, path):
    imgs = [f for f, _ in frames]
    durs = [d for _, d in frames]
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=durs, loop=0, optimize=False)
    print(f"→ {path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    pid = sys.argv[1]
    sheet = generate(pid)                         # キャッシュ読み込み（無ければ生成）
    OUT.mkdir(exist_ok=True)
    targets = [sys.argv[2]] if len(sys.argv) > 2 else list(ANIMS)
    for name in targets:
        save_gif(ANIMS[name](sheet), OUT / f"{pid}_{name}.gif")
