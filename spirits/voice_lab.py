# -*- coding: utf-8 -*-
"""
声の実験室 — あつ森方式（Animalese）ほかを AtomS3 で鳴らす
==========================================================
方式:
  animalese  … 実テキストの仮名を1文字ずつ短い音素にして高速再生（ぎり聞き取れない）
               母音(あいうえお)で音程・響きが変わるので「喋ってる感」が出る
  chipmunk   … TTS(WAV)を早回し＋ピッチ上げ（ぎり聞き取れる寄り）

声の遺伝子（キャラ固有の声）:
  base  … 声の高さ(Hz)  pace … 1文字の長さ(ms)  wobble … 揺れ  tail … 語尾上げ癖

使い方:
  python voice_lab.py            → 実験メドレーをAtomS3で再生
  python voice_lab.py pc         → PCで確認用WAVを書き出すだけ（スピーカー不要）
"""
import sys
import time
import wave
from pathlib import Path

import numpy as np

RATE = 16000
PORT = "COM10"

# ---- 仮名 → (子音クラス, 母音) ----
VOWEL_OF = {}
_ROWS = [
    ("あかさたなはまやらわがざだばぱ", "a"), ("いきしちにひみりぎじぢびぴ", "i"),
    ("うくすつぬふむゆるぐずづぶぷ", "u"), ("えけせてねへめれげぜでべぺ", "e"),
    ("おこそとのほもよろをごぞどぼぽ", "o"),
]
for chars, v in _ROWS:
    for ch in chars:
        VOWEL_OF[ch] = v
        VOWEL_OF[chr(ord(ch) + 96)] = v          # カタカナも
NOISY = set("かきくけこさしすせそたちつてとはひふへほぱぴぷぺぽカキクケコサシスセソタチツテトハヒフヘホパピプペポ")

# 母音ごとの音程倍率と倍音バランス（これが「喋ってる感」の正体）
VOWEL_PITCH = {"a": 1.00, "i": 1.35, "u": 1.15, "e": 1.22, "o": 0.92}
VOWEL_TIMBRE = {"a": 0.50, "i": 0.15, "u": 0.30, "e": 0.25, "o": 0.60}  # 2倍音の量


def synth_animalese(text, base=340.0, pace_ms=70, wobble=0.10, tail=0.5, seed=1, gap_ms=12):
    """仮名テキスト → あつ森風の声（int16 PCM 16kHz）"""
    rng = np.random.default_rng(seed)
    out = []
    moras = [ch for ch in text if ch in VOWEL_OF or ch in "、。…ー っッ"]
    for i, ch in enumerate(moras):
        if ch in "、。… ":
            out.append(np.zeros(int(RATE * 0.10)))
            continue
        if ch in "っッ":
            out.append(np.zeros(int(RATE * 0.05)))
            continue
        if ch == "ー":
            v, dur = None, pace_ms * 1.6          # 直前の音を伸ばす代わりに休符
            out.append(np.zeros(int(RATE * dur / 1000)))
            continue
        v = VOWEL_OF[ch]
        f0 = base * VOWEL_PITCH[v] * (1 + rng.uniform(-wobble, wobble))
        if i >= len(moras) - 2 and rng.random() < tail:
            f0 *= 1.15                            # 語尾上げの癖
        n = int(RATE * pace_ms / 1000)
        t = np.arange(n) / RATE
        h2 = VOWEL_TIMBRE[v]
        sig = (1 - h2) * np.sin(2 * np.pi * f0 * t) + h2 * np.sin(4 * np.pi * f0 * t)
        env = np.minimum(1, np.minimum(t / 0.008, (n / RATE - t) / 0.02) / 1)
        sig *= np.clip(env, 0, 1)
        if ch in NOISY:                           # 子音のシュッ（ノイズ8ms）
            m = int(RATE * 0.008)
            noise = rng.uniform(-1, 1, m) * np.linspace(1, 0, m) * 0.5
            sig[:m] = sig[:m] * 0.3 + noise
        out.append(sig * 0.55)
        out.append(np.zeros(int(RATE * gap_ms / 1000)))   # 文字間のすき間
    pcm = np.concatenate(out) if out else np.zeros(RATE // 2)
    return (np.clip(pcm, -1, 1) * 32767 * 0.8).astype("<i2").tobytes()


def chipmunk(wav_path, factor=1.4):
    """WAVを factor 倍速＋ピッチ上げ（あつ森の実際の作り方に近い）"""
    w = wave.open(str(wav_path), "rb")
    data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    w.close()
    idx = np.arange(0, len(data) - 1, factor)
    fast = np.interp(idx, np.arange(len(data)), data)
    return fast.astype("<i2").tobytes()


def save_wav(pcm_bytes, path):
    w = wave.open(str(path), "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    w.writeframes(pcm_bytes); w.close()
    print(f"→ {path}")


# ---- AtomS3 へ送って再生 ----
def send_pcm(ser, data, label=""):
    data = data[:110000]                          # RAM上限
    print(f"▶ {label} ({len(data)//32}ms)")
    ser.reset_input_buffer()
    ser.write(f"pcm {len(data)}\n".encode())
    t0 = time.time()
    while time.time() - t0 < 30:
        line = ser.readline().decode(errors="ignore").strip()
        if line == "READY":
            break
        if line.startswith("ERR"):
            print("<", line); return
    for i in range(0, len(data), 4096):
        ser.write(data[i:i + 4096])
    time.sleep(len(data) / 32000 + 0.8)           # 再生完了待ち


def wait_boot(ser, quiet_s=3, max_s=30):
    """開くと再起動するので、起動ログが静かになるまで待つ"""
    t0 = time.time(); last = time.time()
    while time.time() - t0 < max_s:
        line = ser.readline().decode(errors="ignore").strip()
        if line:
            print("  [boot]", line); last = time.time()
            if "接続失敗" in line or "接続成功" in line or "warming" in line:
                break
        elif time.time() - last > quiet_s:
            break


MURMUR = "コンロがつやつやしてるねえ。さっきまでだれかがいたにおいが、まだすこしのこってる。"

VOICES = {                                        # 声の遺伝子（キャラ固有の声のデモ）
    "ぽかん（のんびり・低め）":   dict(base=300, pace_ms=150, wobble=0.08, tail=0.6, seed=7, gap_ms=40),
    "こくり（おっとり・中）":     dict(base=390, pace_ms=125, wobble=0.06, tail=0.3, seed=3, gap_ms=35),
    "そよぎ（心配性・高く細かく）": dict(base=540, pace_ms=90, wobble=0.14, tail=0.7, seed=11, gap_ms=25),
}


def main_device():
    import serial, os
    ser = serial.Serial(PORT, 115200, timeout=1)
    wait_boot(ser)
    # (c) あつ森方式 ×3キャラ（同じ文を違う声で）
    for name, g in VOICES.items():
        send_pcm(ser, synth_animalese("コンロがつやつやしてるねえ。", **g), f"animalese: {name}")
        time.sleep(0.6)
    # (d) 早回しTTS（ぎり聞き取れる寄り）
    p = Path(os.path.expandvars(r"%TEMP%\murmur.wav"))
    if p.exists():
        send_pcm(ser, chipmunk(p, 1.4), "chipmunk: Haruka×1.4")
    ser.close()
    print("done")


def main_pc():
    out = Path(__file__).parent.parent / "spirits_out"
    out.mkdir(exist_ok=True)
    for name, g in VOICES.items():
        key = name.split("（")[0]
        save_wav(synth_animalese(MURMUR, **g), out / f"voice_{key}.wav")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pc":
        main_pc()
    else:
        main_device()
