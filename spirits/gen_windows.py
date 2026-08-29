# -*- coding: utf-8 -*-
"""あつ森風の会話窓（一列・小型・画面幅いっぱい） → win_<pid>_<i>.bin
形式: [h:1B][文字数:1B][RGB565 big-endian 240×h]"""
import sys, json
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BG = (247, 240, 224)
F_TEXT = ImageFont.truetype(r"C:\Windows\Fonts\meiryo.ttc", 23)

PHRASES = {
    "cute_07": ["……ぽ",
                "きょう、しずかだね",
                "かたづき、みてるよ",
                "ここ、あったかいね"],
}

def rgb565(img):
    a = np.asarray(img, dtype=np.uint16)
    c = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
    return c.astype(">u2").tobytes()

def window(text, body_hex):
    """一列だけの小さな吹き出し。幅いっぱい＝角の外側にキャラが来ない"""
    body = tuple(int(body_hex.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    h = 42
    img = Image.new("RGB", (240, h), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 239, h - 1], radius=11, fill=(255, 252, 244), outline=body, width=3)
    d.text((16, h // 2), text, font=F_TEXT, fill=(92, 66, 46), anchor="lm")   # 左揃え
    return img, h

def build(pid):
    sheet = json.load(open(f"characters/{pid}.json", encoding="utf-8"))
    for i, ph in enumerate(PHRASES[pid]):
        img, h = window(ph, sheet["colors"]["body"])
        open(f"../spirits_out/win_{pid}_{i}.bin", "wb").write(
            bytes([h, len(ph)]) + rgb565(img))
    print(f"→ win_{pid}_0..{len(PHRASES[pid])-1}.bin (h=42, 1行)")

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "cute_07")
