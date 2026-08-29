# -*- coding: utf-8 -*-
"""
液晶リモコン — 保存済みキャラ/顔テストを、言うだけで表示（対話式・送信なし）
  入力例:  07        → cute_07 を表示（呼吸ループ）
           A         → 顔テストAを表示
           07 happy  → cute_07 の happy（アニメが入っていれば）
           q         → 終了
液晶側に dev_*.bin が保存済みなら転送ゼロ・即切替。
"""
import sys, threading, time
from cute_tour import Lcd, bits_of

lcd = Lcd()
current = {"pid": None, "anim": "idle"}
stop = threading.Event()

def loop():                       # 選択中のキャラを呼吸ループで表示し続ける
    while not stop.is_set():
        if current["pid"]:
            lcd.cmd(f"PLAY {current['anim']} 1", wait=True, t=60)
        else:
            time.sleep(0.2)

th = threading.Thread(target=loop, daemon=True); th.start()
print("liquid crystal remote ready. (07 / A / 07 happy / q)")
while True:
    try:
        raw = input("> ").strip()
    except EOFError:
        break
    if raw == "q": break
    if not raw: continue
    parts = raw.split()
    key = parts[0]; anim = parts[1] if len(parts) > 1 else "idle"
    pid = f"facetest_{key.upper()}" if len(key) == 1 and key.isalpha() else f"cute_{int(key):02d}" if key.isdigit() else key
    lcd.cmd(f"PID {pid}", wait=True); lcd.cmd("TCLR"); lcd.cmd(f"TEXT {bits_of(key.upper()).hex()}")
    current["pid"], current["anim"] = pid, anim
    print(f"  showing {pid} {anim}")
stop.set(); lcd.cmd("TCLR"); lcd.close()
