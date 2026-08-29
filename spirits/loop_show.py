# -*- coding: utf-8 -*-
"""指定リストを記号付きでループ表示: python loop_show.py A B C D E F G H"""
import sys, time
from cute_tour import Lcd, bits_of
keys = sys.argv[1:] or list("ABCDEFGH")
def pid_of(k): return f"facetest_{k.upper()}" if len(k)==1 and k.isalpha() else f"cute_{int(k):02d}" if k.isdigit() else k
lcd = Lcd()
try:
    while True:
        for k in keys:
            lcd.cmd(f"PID {pid_of(k)}"); lcd.cmd("TCLR"); lcd.cmd(f"TEXT {bits_of(k.upper()).hex()}")
            lcd.cmd("PLAY idle 1", wait=True, t=60)
finally:
    lcd.close()
