# -*- coding: utf-8 -*-
"""
顔と声の同期デモ（PC側オーケストレーター）
==========================================
XIAO(COM18・液晶) で精霊アニメを再生しつつ、その状態マーカー('ANIM xxx')を読み、
AtomS3(COM10・スピーカー) へメロディコマンドを送る。

構成:  XIAO ──USB──> このスクリプト ──USB──> AtomS3
        (顔)          橋渡し                  (声)

使い方: python demo_sync.py [人のID] [サイクル数]   （Ctrl+Cで終了）
"""
import subprocess
import sys

import serial

LCD_PORT = "COM18"
SPK_PORT = "COM10"


def main(pid="kuwahara", cycles="2"):
    spk = serial.Serial(SPK_PORT, 115200, timeout=1)
    proc = subprocess.Popen(
        [sys.executable, "-m", "mpremote", "connect", LCD_PORT, "exec",
         f"import device_player; device_player.demo('{pid}', cycles={cycles})"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("ANIM "):
                state = line.split()[1]
                print(f"♪ {state}")
                spk.write((state + "\n").encode())
            elif line:
                print(f"  [lcd] {line}")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        spk.close()


if __name__ == "__main__":
    main(*(sys.argv[1:] or []))
