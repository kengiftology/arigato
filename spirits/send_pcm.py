# -*- coding: utf-8 -*-
"""PCM(16kHz/16bit/mono bytes) を AtomS3 に送って再生する共通関数"""
import time
import serial

def open_atom(port="COM10", wait_boot=True):
    s = serial.Serial(port, 115200, timeout=0.5)
    if wait_boot:                              # 開くと再起動するので起動ログを流し切る
        t0 = time.time(); last = time.time()
        while time.time() - t0 < 40:
            line = s.readline().decode(errors="ignore").strip()
            if line:
                last = time.time()
                if "warming" in line: break
            elif time.time() - last > 4: break
    s.reset_input_buffer()
    return s

def send_pcm(s, data, label="", verbose=False):
    data = data[:110000]
    if label: print(f"▶ {label} ({len(data)//32}ms)")
    s.write(f"pcm {len(data)}\n".encode()); s.flush()
    ready = False; t0 = time.time()
    while time.time() - t0 < 10:
        line = s.readline().decode(errors="ignore").strip()
        if verbose and line: print("<", line)
        if line == "READY": ready = True; break
        if line.startswith("ERR"): print("<", line); return False
    if not ready: print("  no READY"); return False
    for i in range(0, len(data), 1024):
        s.write(data[i:i + 1024]); s.flush(); time.sleep(0.004)
    t0 = time.time()
    while time.time() - t0 < 25:
        line = s.readline().decode(errors="ignore").strip()
        if verbose and line: print("<", line)
        if line == "played": return True
    return False
