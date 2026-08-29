# -*- coding: utf-8 -*-
"""顔判読テスト: 同じ体(No.07)に、目・口の描き方の候補を A〜H で並べる。
   問い: どれが一番「顔だ」と読めるか。可愛さは問わない。"""
import json, sys, time
from designer import SHEET_DIR, GRID, rect, disc, mirror, umouth
import export_device, animate

def eyes_variant(v, cx, cy):
    """左目セル（右目はmirror）"""
    if v == "A": return rect(cx-1, cx, cy-1, cy)                     # 2x2 ベタ（小）
    if v == "B": return rect(cx-1, cx+1, cy-1, cy+1)                 # 3x3 ベタ（大）
    if v == "C": return disc(cx, cy, 1.6)                            # 丸（大）ハイライト無し
    if v == "D": e = disc(cx, cy, 1.6); e.discard((cx-1, cy-1)); return e   # 丸+左上抜き（今のやつ）
    if v == "E": return rect(cx-1, cx+1, cy, cy)                     # 横線 1x3（ジト目）
    if v == "F": return rect(cx, cx, cy-1, cy+1)                     # 縦線 1x3
    if v == "G": return {(cx-1, cy), (cx, cy-1), (cx+1, cy)}         # にこ目（^）
    if v == "H": return rect(cx-1, cx, cy-1, cy)                     # 2x2（Aと同じ、口違いで比較用）
    return set()

def mouth_variant(v, mx, my, w):
    if v in "ABCDEFG": return umouth(2, mx, my, w, thick=1)         # 細い笑み
    if v == "H":       return {(mx-1, my), (mx, my)}                 # ちょん（点2つ）
    return set()

def build(sheet, v):
    f = sheet["face"]; cx = round(15.5 - f["eye_gap"]); cy = f["eye_y"]
    eyes = eyes_variant(v, cx, cy); eyes |= mirror(eyes)
    mouth = mouth_variant(v, 16, f["mouth_y"], f["mouth_w"])
    return eyes | mouth

if __name__ == "__main__":
    base = json.load(open(SHEET_DIR / "cute_07.json", encoding="utf-8"))
    animate.frame = export_device._capture_frame
    out = export_device.OUT
    files = []
    for v in "ABCDEFGH":
        face = build(base, v)
        fr = export_device._capture_frame(base, base["art"], face_cells=face, cheek_cells=set(), M=0.1)
        blob = bytes([1]) + export_device.to_bin_frame(fr, 4000)
        p = out / f"dev_facetest_{v}_idle.bin"; p.write_bytes(blob); files.append(str(p))
    import subprocess
    subprocess.run([sys.executable, "-m", "mpremote", "connect", "COM18", "fs", "cp", *files, ":"], stdout=subprocess.DEVNULL)
    from cute_tour import Lcd, bits_of
    lcd = Lcd()
    for v in "ABCDEFGH":
        lcd.cmd(f"PID facetest_{v}"); lcd.cmd("TCLR")
        lcd.cmd(f"TEXT {bits_of(v).hex()}")
        lcd.cmd("PLAY idle 1", wait=True)
    lcd.cmd("TCLR"); lcd.close(); print("face test done")
