# -*- coding: utf-8 -*-
"""口テスト: 同じ体(No.07)・同じ目(2x2ベタ)で、口の描き方 1〜8 を並べる。
   目と口の間隔は十分に空ける（目の下端から3行空き）。"""
import sys, json, subprocess
from designer import SHEET_DIR, rect, mirror
import export_device, animate
from cute_tour import Lcd, bits_of

def mouths(v, mx, my):
    """mx=中心x(16), my=口の行。すべて左右対称・連続・孤立点なし"""
    if v == "1": return {(mx-1, my), (mx, my)}                                      # ちょん（2点）
    if v == "2": return rect(mx-2, mx+1, my, my)                                    # 短い横線（4）
    if v == "3": return {(mx-2, my-1), (mx-1, my), (mx, my), (mx+1, my-1)}          # 小さなU（4幅）
    if v == "4": return {(mx-3, my-1), (mx-2, my), (mx-1, my), (mx, my), (mx+1, my), (mx+2, my-1)}  # U（6幅・角1段）
    if v == "5": return {(mx-1, my), (mx, my), (mx-1, my+1), (mx, my+1)}            # 2x2 の丸口（o）
    if v == "6": return {(mx-2, my), (mx-1, my+1), (mx, my+1), (mx+1, my)}          # ω っぽい（下に膨らむ）
    if v == "7": return {(mx-2, my-1), (mx-1, my), (mx, my), (mx+1, my-1), (mx-1, my-1), (mx, my-1)}  # 小さな塊笑顔（歯なし）
    if v == "8": return {(mx-1, my), (mx, my), (mx, my-1), (mx-1, my-1)} | {(mx-2, my-1), (mx+1, my-1)}  # 「ω」上向き
    return set()

if __name__ == "__main__":
    base = json.load(open(SHEET_DIR / "cute_07.json", encoding="utf-8"))
    f = base["face"]
    cx = round(15.5 - f["eye_gap"]); cy = f["eye_y"]
    eyes = rect(cx-1, cx, cy-1, cy); eyes |= mirror(eyes)          # 2x2 ベタ目（A）
    my = cy + 4                                                    # 目の下端(cy)から3行空けて口
    animate.frame = export_device._capture_frame
    files = []
    for v in "12345678":
        face = eyes | mouths(v, 16, my)
        fr = export_device._capture_frame(base, base["art"], face_cells=face, cheek_cells=set(), M=0.1)
        blob = bytes([1]) + export_device.to_bin_frame(fr, 4000)
        p = export_device.OUT / f"dev_mouth{v}_idle.bin"; p.write_bytes(blob); files.append(str(p))
    subprocess.run([sys.executable, "-m", "mpremote", "connect", "COM18", "fs", "cp", *files, ":"], stdout=subprocess.DEVNULL)
    lcd = Lcd()
    try:
        while True:
            for v in "12345678":
                lcd.cmd(f"PID mouth{v}"); lcd.cmd("TCLR"); lcd.cmd(f"TEXT {bits_of('M' + v).hex()}")
                lcd.cmd("PLAY idle 1", wait=True)
    finally:
        lcd.close()
