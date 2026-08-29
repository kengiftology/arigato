# -*- coding: utf-8 -*-
"""
可愛さ最適化ラボ — 番号付きギャラリーで「当たり」を選んでもらう
================================================================
エージェントに、可愛さの方向をわざと散らした16体を作らせ、番号付きで並べる。
選ばれた番号の共通点（頭身・目・輪郭・大きさ…）を後で抽出して「可愛さの型」にする。

使い方:
  python cute_lab.py gen      → 16体生成（characters/cute_XX.json）
  python cute_lab.py sheet    → 番号付きギャラリー cute_sheet.png
"""
import json
import sys
from pathlib import Path

import designer
from designer import generate, render, GRID, SHEET_DIR

# 可愛さの方向をわざと散らす（各軸から1つずつ組む）
STYLES = [
    ("まんまる・目は大きく低め・口ちいさく",             "ちいかわ系: 究極に単純。輪郭は真円に近く、目は点でなく大きめの丸を顔の下寄りに"),
    ("ずんぐり2頭身・目は小さな点・ほっぺ大きく",       "たまごっち系: 記号的。目は2×2の点、ほっぺの丸を強調"),
    ("おにぎり型・目は縦長の楕円・小さな口",             "すみっコ系: しずく～おにぎりの輪郭、目は縦長で少し離す"),
    ("横に広いもち型・目は線（笑ってる）・口なしでも可", "もちもち系: 横長でぺたんこ、目はにこ目（アーチ）"),
    ("小さい体に大きな頭・大きな耳・丸い目",             "動物系: 耳が体の1/3くらい大きい。ピカチュウ的比率"),
    ("しずく型・小さな目を離して・体は無地",             "ミニマル系: 模様なし、目は小さめで間隔広め（幼さ）"),
    ("四角っぽい・目は大きく・角は丸く",                 "ブロック系: 角丸の正方形、大きな丸目"),
    ("卵型・つぶらな瞳（ハイライト付き）・小さなくちばし","ひよこ系: 目は2ドットの黒に1ドットの白ハイライト"),
    ("ふわふわ輪郭（雲）・目は点・口は小さなw",           "雲/わたげ系: 輪郭にこぶを付けてふわふわに"),
    ("低い台形・目は横長・のんびり",                     "なめこ/きのこ系: 帽子付き、目は横に細長い"),
    ("真ん丸ボール・目は大きな丸で近め・体色は淡く",     "ボール系: 目を中央寄せにして幼くする"),
    ("縦長・小さな手が生えてる・目は点",                 "はにわ系: 小さな突起の手を体の横に"),
    ("小さくてちび・画面の中央にちょこん・目大きい",     "ちび系: 体は他より小さめ（半径7〜8）にして余白を活かす"),
    ("楕円で丸い・目は少し垂れ・ほっぺ",                 "たれ目系: 目の外側を1ドット下げる"),
    ("おなかに模様（大きな明るい楕円）・丸目",           "おなか系: おなかの模様を体の半分近く大きく"),
    ("頭に小さな葉っぱ・まんまる・目は点",               "植物系: 頭のてっぺんに小さな葉、体は真円"),
]

CUTE_PROMPT_EXTRA = """
# 今回のスタイル指示（最優先）
方向: {style}
ヒント: {hint}
可愛さの基本原則（守ること）:
- 目は顔の中心より下寄りに置く（幼く見える）
- 輪郭は滑らかな階段状にし、1ドットの飛び出しを作らない
- 体の左右対称を基本にする
- 色は淡くやわらかいトーン（彩度は低め・明るめ）
- モチーフは何でもよいが、姿は「小さくてほっとけない生き物」に
"""


def gen():
    base_prompt = designer.DESIGN_PROMPT
    for i, (style, hint) in enumerate(STYLES, 1):
        pid = f"cute_{i:02d}"
        if (SHEET_DIR / f"{pid}.json").exists():
            print(f"{pid}: cached"); continue
        # プロンプトにスタイル指示を差し込む（お題のモチーフはハッシュで配られる）
        designer.DESIGN_PROMPT = base_prompt + CUTE_PROMPT_EXTRA.format(style=style, hint=hint)
        for attempt in range(3):
            try:
                sh = generate(pid, force=True)
                print(f"{pid}: {sh['name']}（{sh['species']}）")
                break
            except Exception as e:
                print(f"{pid}: retry {attempt+1} ({type(e).__name__})")
        else:
            print(f"{pid}: FAILED")
    designer.DESIGN_PROMPT = base_prompt


def sheet(cols=4, M=0.1, N=0.1, path="../cute_sheet.png"):
    from PIL import Image, ImageDraw, ImageFont
    scale, margin = 7, 8
    size = GRID * scale + margin * 2
    ids = [f"cute_{i:02d}" for i in range(1, len(STYLES) + 1)]
    rows = (len(ids) + cols - 1) // cols
    out = Image.new("RGB", (size * cols, size * rows), (13, 15, 20))
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 30)
    except Exception:
        font = ImageFont.load_default()
    for k, pid in enumerate(ids):
        p = SHEET_DIR / f"{pid}.json"
        if not p.exists():
            continue
        sh = json.loads(p.read_text(encoding="utf-8"))
        render(sh, M, N, "_c.png")
        cell = Image.open("_c.png")
        d = ImageDraw.Draw(cell)
        d.rectangle([4, 4, 60, 40], fill=(240, 236, 220))
        d.text((10, 6), f"{k+1:02d}", font=font, fill=(30, 30, 30))
        out.paste(cell, ((k % cols) * size, (k // cols) * size))
    Path("_c.png").unlink(missing_ok=True)
    out.save(path)
    print("→", path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sheet":
        sheet()
    else:
        gen()
        sheet()
