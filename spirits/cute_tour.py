# -*- coding: utf-8 -*-
"""可愛さ候補を液晶で順送り: 番号を字幕帯に出して各キャラ idle を数秒ずつ"""
import sys, time, json
import serial
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import numpy as np

import animate, export_device, designer
from designer import SHEET_DIR

LCD = "COM18"

def bits_of(text):
    img = Image.new("1", (240, 24), 0)
    f = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 22)
    ImageDraw.Draw(img).text((6, 0), text, font=f, fill=1)
    return np.packbits(np.array(img, dtype=np.uint8), axis=1).tobytes()

def export_idle(pid):
    """idle だけ実機バイナリ化"""
    animate.frame = export_device._capture_frame
    sh = json.loads((SHEET_DIR / f"{pid}.json").read_text(encoding="utf-8"))
    frames = animate.anim_idle(sh)
    blob = bytes([len(frames)]) + b"".join(export_device.to_bin_frame(f, d) for f, d in frames)
    p = export_device.OUT / f"dev_{pid}_idle.bin"
    p.write_bytes(blob)
    return p, sh

class Lcd:
    def __init__(self):
        self.s = serial.Serial(LCD, 115200, timeout=0.2); time.sleep(0.5)
        self.s.write(b"\x03\x03"); time.sleep(0.3)
        self.s.write(b"\x01"); time.sleep(0.3)
        self.s.write(b"exec(open('lcd_serve.py').read())\x04")
        self._wait("LCD READY", 15)
    def _wait(self, tok, t):
        t0=time.time(); buf=""
        while time.time()-t0<t:
            buf += self.s.read(256).decode(errors="ignore")
            if tok in buf: return True
        print("timeout", tok, buf[-120:]); return False
    def cmd(self, line, wait=True, t=60):
        self.s.write((line+"\n").encode()); self.s.flush()
        if wait:
            t0=time.time(); buf=""
            while time.time()-t0<t:
                buf += self.s.read(256).decode(errors="ignore")
                for ln in buf.splitlines():
                    if ln.startswith(("OK","DONE","PONG","ERR")):
                        if ln.startswith("ERR"): print(" [lcd]", ln)
                        return ln
    def close(self):
        self.s.write(b"\x03"); time.sleep(0.2); self.s.write(b"\x02"); self.s.close()

def main(ids, secs=5):
    import subprocess
    # 実機へ idle バイナリを転送
    files = []
    for pid in ids:
        p, sh = export_idle(pid)
        files.append(str(p)); print(f"{pid}: {sh['name']}")
    subprocess.run([sys.executable, "-m", "mpremote", "connect", LCD, "fs", "cp", *files, ":"],
                   stdout=subprocess.DEVNULL)
    lcd = Lcd()
    for pid in ids:
        num = pid.split("_")[1]
        lcd.cmd(f"PID {pid}")
        lcd.cmd("TCLR")
        lcd.cmd(f"TEXT {bits_of('No.' + num).hex()}")
        lcd.cmd("PLAY idle 1", wait=True)   # 約4秒
    lcd.cmd("TCLR")
    lcd.close()
    print("tour done")

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main([f"cute_{i:02d}" for i in range(1, n + 1)])
