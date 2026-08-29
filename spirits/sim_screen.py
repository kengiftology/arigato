# -*- coding: utf-8 -*-
"""実機シミュレータ: デバイスに送った .bin を、device_player と同じ手順でPC上に描画する。
   これで「実機に何が映っているか」を送信前に確認できる。"""
import struct, sys
import numpy as np
from PIL import Image

GRID, CELL, OFF = 32, 7, 8

def load_frames(path):
    frames = []
    with open(path, "rb") as f:
        n = f.read(1)[0]
        for _ in range(n):
            dur = struct.unpack(">H", f.read(2))[0]
            pal = f.read(12)
            grid = f.read(1024)
            frames.append((dur, pal, grid))
    return frames

def pal_rgb(pal, idx):
    v = struct.unpack(">H", pal[idx*2:idx*2+2])[0]
    return ((v >> 11) << 3, ((v >> 5) & 0x3F) << 2, (v & 0x1F) << 3)

def render_frame(pal, grid):
    img = Image.new("RGB", (240, 240), pal_rgb(pal, 0))
    px = img.load()
    for r in range(GRID):
        for c in range(GRID):
            v = grid[r*GRID + c]
            if v:
                col = pal_rgb(pal, v)
                for y in range(OFF + r*CELL, OFF + (r+1)*CELL):
                    for x in range(OFF + c*CELL, OFF + (c+1)*CELL):
                        if 0 <= x < 240 and 0 <= y < 240:
                            px[x, y] = col
    return img

def overlay_window(img, win_path, y0=150):
    with open(win_path, "rb") as f:
        h = f.read(1)[0]; n = f.read(1)[0]
        raw = np.frombuffer(f.read(), dtype=">u2").reshape(h, 240)
    rgb = np.stack([(raw >> 11) << 3, ((raw >> 5) & 0x3F) << 2, (raw & 0x1F) << 3], axis=-1).astype(np.uint8)
    img.paste(Image.fromarray(rgb), (0, y0))
    return img

if __name__ == "__main__":
    frames = load_frames("../spirits_out/dev_cute_07_idle.bin")
    dur, pal, grid = frames[0]
    a = render_frame(pal, grid)
    b = render_frame(pal, grid)
    overlay_window(b, "../spirits_out/win_cute_07_2.bin")
    sheet = Image.new("RGB", (480 + 8, 240), (200, 200, 200))
    sheet.paste(a, (0, 0)); sheet.paste(b, (248, 0))
    sheet = sheet.resize((976, 480), 0)
    sheet.save("../sim_screen.png")
    print("→ ../sim_screen.png  (左: ふだん / 右: 会話窓)")
