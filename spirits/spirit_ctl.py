# -*- coding: utf-8 -*-
"""統合機の簡易コントローラ（PC→USBシリアル）
使い方:
  python spirit_ctl.py                  → 対話モード
  python spirit_ctl.py scene happy      → 1発コマンド
  python spirit_ctl.py wav <file.wav>   → 16kHz/mono/16bit WAVを実機スピーカーで再生
コマンド: scene notice|happy|sad|sleep|hatch / mur / wav <path> / q
"""
import sys, time, wave
import serial

# 仮名→母音番号（0-4=あいうえお 5=間 6=っ）と子音マスク
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from voice_lab import VOWEL_OF, NOISY
VIDX = {"a": 0, "i": 1, "u": 2, "e": 3, "o": 4}

def kana_to_cmd(text):
    seq, noisy = [], 0
    for ch in text:
        if ch in VOWEL_OF:
            if ch in NOISY:
                noisy |= 1 << len(seq)
            seq.append(VIDX[VOWEL_OF[ch]])
        elif ch in "、。…":
            seq.append(5)
        elif ch in "っッ":
            seq.append(6)
    hexstr = "".join(f"{v:02x}" for v in seq[:30])
    return f"say {hexstr} {noisy:x}"

PORT = "COM18"

def open_dev():
    s = serial.Serial(PORT, 115200, timeout=0.5)
    time.sleep(0.8)
    s.reset_input_buffer()
    return s

def send_wav(s, path):
    w = wave.open(path, "rb")
    assert w.getframerate() == 16000 and w.getnchannels() == 1, "16kHz/monoで"
    data = w.readframes(w.getnframes()); w.close()
    s.write(f"pcm {len(data)}\n".encode())
    t0 = time.time()
    while time.time() - t0 < 20:
        if s.readline().decode(errors="ignore").strip() == "READY":
            break
    for i in range(0, len(data), 2048):
        s.write(data[i:i+2048])
    t0 = time.time()
    while time.time() - t0 < len(data)/32000 + 10:
        if s.readline().decode(errors="ignore").strip() == "DONE":
            print("played"); return
    print("(no DONE)")

def main():
    s = open_dev()
    args = sys.argv[1:]
    if args:
        if args[0] == "wav":
            send_wav(s, args[1])
        else:
            s.write((" ".join(args) + "\n").encode())
            time.sleep(0.3)
            print(s.read(200).decode(errors="ignore").strip())
        s.close(); return
    print("spirit_ctl: say <ひらがな> / voice <高さ> <速さ> / notice|happy|sad|sleep|hatch / mur / wav <path> / q")
    while True:
        try: cmd = input("> ").strip()
        except EOFError: break
        if cmd == "q": break
        if not cmd: continue
        if cmd.startswith("wav "):
            send_wav(s, cmd[4:].strip())
        elif cmd.startswith("say "):
            s.write((kana_to_cmd(cmd[4:].strip()) + "\n").encode())
            time.sleep(0.3)
            print(s.read(200).decode(errors="ignore").strip())
        else:
            s.write((cmd + "\n").encode())
            time.sleep(0.3)
            print(s.read(200).decode(errors="ignore").strip())
    s.close()

if __name__ == "__main__":
    main()
