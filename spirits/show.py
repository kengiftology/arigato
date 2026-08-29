# -*- coding: utf-8 -*-
"""1発表示: python show.py <A|07|07 happy> [秒]  → 指定を出して（ループで）指定秒保つ"""
import sys, time
from cute_tour import Lcd, bits_of
key = sys.argv[1]; anim = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].isdigit() else "idle"
secs = int(sys.argv[-1]) if sys.argv[-1].isdigit() and len(sys.argv) > 2 else 600
pid = f"facetest_{key.upper()}" if len(key) == 1 and key.isalpha() else f"cute_{int(key):02d}" if key.isdigit() else key
lcd = Lcd()
lcd.cmd(f"PID {pid}"); lcd.cmd("TCLR"); lcd.cmd(f"TEXT {bits_of(key.upper()).hex()}")
t0 = time.time()
while time.time() - t0 < secs:
    lcd.cmd(f"PLAY {anim} 1", wait=True, t=60)
lcd.close()
