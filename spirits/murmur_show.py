# -*- coding: utf-8 -*-
"""
独り言ショー — 顔（XIAO液晶）× 字幕 × あつ森語（AtomS3）を同期させる
=====================================================================
あつ森の「声(聞き取れない) × 字幕(読める)」構造の再現。
  液晶: 精霊が呼吸しつつ、下の帯に独り言が1文字ずつ出る
  音  : 同じ文字列を同じリズムであつ森語で鳴らす（キャラ固有の声）

使い方: python murmur_show.py [人のID] ["独り言テキスト"]
前提: XIAOに lcd_serve.py, device_player.py, dev_<id>_*.bin が入っていること
"""
import sys
import time
import threading
from pathlib import Path

import serial
from PIL import Image, ImageDraw, ImageFont
import numpy as np

from voice_lab import synth_animalese, VOICES
from send_pcm import open_atom, send_pcm

LCD, SPK = "COM18", "COM10"
FONT_CANDIDATES = [r"C:\Windows\Fonts\meiryo.ttc", r"C:\Windows\Fonts\msgothic.ttc",
                   r"C:\Windows\Fonts\YuGothM.ttc"]


def font16():
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, 21)
    return ImageFont.load_default()


def render_text_bits(text, font, width=240, height=24):
    img = Image.new("1", (width, height), 0)
    ImageDraw.Draw(img).text((4, 0), text, font=font, fill=1)
    return np.packbits(np.array(img, dtype=np.uint8), axis=1).tobytes()


class Lcd:
    """XIAO上の lcd_serve.py と生シリアルで会話する"""
    def __init__(self, pid):
        self.s = serial.Serial(LCD, 115200, timeout=0.2)
        time.sleep(0.5)
        # ソフトリセット→ lcd_serve を起動（raw REPLで exec）
        self.s.write(b"\x03\x03")             # 実行中を止める
        time.sleep(0.3)
        self.s.write(b"\x01")                  # raw REPL
        time.sleep(0.3)
        self.s.write(b"exec(open('lcd_serve.py').read())\x04")
        self._wait("LCD READY", 15)
        self.cmd(f"PID {pid}")

    def _wait(self, token, timeout):
        t0 = time.time(); buf = ""
        while time.time() - t0 < timeout:
            buf += self.s.read(256).decode(errors="ignore")
            if token in buf:
                return True
        print("  [lcd] timeout waiting", token, "| got:", buf[-200:])
        return False

    def cmd(self, line, wait=True, timeout=30):
        self.s.write((line + "\n").encode()); self.s.flush()
        if wait:
            return self._wait_line(timeout)

    def _wait_line(self, timeout):
        t0 = time.time(); buf = ""
        while time.time() - t0 < timeout:
            buf += self.s.read(256).decode(errors="ignore")
            for ln in buf.splitlines():
                if ln.startswith(("OK", "DONE", "PONG", "ERR")):
                    if ln.startswith("ERR"): print("  [lcd]", ln)
                    return ln
        return None

    def text(self, bits):
        self.cmd("TEXT " + bits.hex())

    def play(self, anim, loops=1, wait=False):
        self.cmd(f"PLAY {anim} {loops}", wait=wait, timeout=120)

    def close(self):
        self.s.write(b"\x03"); time.sleep(0.2)
        self.s.write(b"\x02")                  # 通常REPLへ
        self.s.close()


def main(pid="kuwahara", text="コンロがつやつやしてるねえ。さっきまでだれかがいたにおいが、まだすこしのこってる。"):
    voice = list(VOICES.values())[0]
    font = font16()
    print("opening speaker...");   spk = open_atom(SPK)
    print("opening lcd...");       lcd = Lcd(pid)
    lcd.cmd("TCLR")
    lcd.play("idle", 1, wait=True)             # まず顔を出す

    parts = [p for p in text.replace("。", "。|").replace("、", "、|").split("|") if p]
    per = (voice["pace_ms"] + voice["gap_ms"]) / 1000.0
    shown = ""
    for part in parts:
        pcm = synth_animalese(part, **voice)
        # 声を送る（送信中に字幕スレッドを走らせる：送信〜再生開始 ≒ 転送時間なので先に文字を出し始める）
        def subtitle():
            nonlocal shown
            time.sleep(len(pcm) / 1024 * 0.012 + 0.3)   # 転送時間ぶん待ってから開始
            for ch in part:
                shown += ch
                line = shown[-10:]
                lcd.text(render_text_bits(line, font))
                time.sleep(per)
        th = threading.Thread(target=subtitle); th.start()
        send_pcm(spk, pcm, label=part)
        th.join()
        time.sleep(0.6)
        if len(shown) >= 10:
            shown = ""
    time.sleep(1.5)
    lcd.cmd("TCLR")
    lcd.play("idle", 2, wait=True)
    lcd.close(); spk.close()
    print("done")


if __name__ == "__main__":
    main(*(sys.argv[1:] or []))
