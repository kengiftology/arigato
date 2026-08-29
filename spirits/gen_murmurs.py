# -*- coding: utf-8 -*-
"""独り言テキスト → 実機用ビットマップ集 murmurs_<pid>.bin
形式: [行数:1B] + 行ごとに [文字数:1B][フレーズ先頭フラグ:1B][720B(24行×30B)]"""
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = ImageFont.truetype(r"C:\Windows\Fonts\meiryo.ttc", 21)

# ほむり（ひだねの精霊・無口な観察者）の独り言。フレーズ＝行のまとまり
PHRASES = {
    "cute_07": [
        ["……ぽ"],
        ["きょうも、", "しずかだね"],
        ["かたづくの、", "みてるよ……ふ"],
        ["ここのすみ、", "あったかい"],
    ],
}

def line_bits(text):
    img = Image.new("1", (240, 24), 0)
    ImageDraw.Draw(img).text((6, 0), text, font=FONT, fill=1)
    return np.packbits(np.array(img, dtype=np.uint8), axis=1).tobytes()  # 24*30=720B

def build(pid):
    blob = b""
    n = 0
    for phrase in PHRASES[pid]:
        for j, line in enumerate(phrase):
            blob += bytes([len(line), 1 if j == 0 else 0]) + line_bits(line)
            n += 1
    out = f"../spirits_out/murmurs_{pid}.bin"
    open(out, "wb").write(bytes([n]) + blob)
    print("→", out, n, "lines")

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "cute_07")
