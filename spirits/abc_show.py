# abc_show.py — A/B/C比較画面を8秒ずつループ表示（XIAO/MicroPython）
import time
import device_player as d

d.init_lcd()
while True:
    for lab in ("A", "B", "C"):
        with open("scr_%s.bin" % lab, "rb") as f:
            d._win(0, 0, 239, 239)
            d.dc(1)
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                d.spi.write(chunk)
        time.sleep(8)
