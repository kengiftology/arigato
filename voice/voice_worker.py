# -*- coding: utf-8 -*-
"""地霊の声係（ラズパイ版・2026-09-09）。

クラウドが「この人向けの次の一言」を文で用意する（/spirit/todo）。
この係がそれを VOICEVOX（ずんだもん）で音にして、クラウドへ置く（/spirit/todo/<名前>）。
次にその人が来た瞬間、C3 はその音を鳴らす。1本30秒かかるが、誰も居ない間に作るので困らない。

ついでに、その場の一言（/spirit/say）も従来どおり音にする（sayd.py と同じ）。

  python3 voice_worker.py          … 動かし続ける（30秒おき）
  python3 voice_worker.py --once   … 1回ぶんだけ作って終わる

VOICEVOX ENGINE が 127.0.0.1:50021 で動いている必要がある（~/vv_start.sh）。
クレジット: VOICEVOX:ずんだもん
"""
import argparse
import io
import json
import sys
import time
import urllib.parse
import urllib.request
import wave

API = "http://127.0.0.1:50021"
SPEAKER = 3                     # ずんだもん（ノーマル）
CLOUD = "https://arigato-3ipecjbnha-an.a.run.app"
KEY = "06dc964a3cdd2c4f4c5c1d8592dff543"
GAP = 30.0                      # クラウドを覗きにいく間隔（秒）
RATE = 16000                    # C3のI2Sは 16kHz・16bit・モノラル


def get_json(path: str) -> dict:
    with urllib.request.urlopen(CLOUD + path, timeout=20) as r:
        return json.load(r)


def get_text(path: str) -> str:
    with urllib.request.urlopen(CLOUD + path, timeout=20) as r:
        return r.read().decode("utf-8").strip()


def synth(text: str) -> bytes:
    """VOICEVOXでwavを作る。設定は作り置き19本と揃える。"""
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": SPEAKER})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        q = json.load(r)
    q["speedScale"] = 0.95              # 少しゆっくり（憲法第5条 間）
    q["prePhonemeLength"] = 0.4         # 頭に沈黙。出し抜けに鳴らない
    q["postPhonemeLength"] = 0.2
    q["outputSamplingRate"] = RATE      # C3に合わせて作らせる
    q["outputStereo"] = False
    u2 = API + "/synthesis?" + urllib.parse.urlencode({"speaker": SPEAKER})
    req = urllib.request.Request(u2, method="POST", data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def to_pcm(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
            raise RuntimeError("形が違う: %dch %dbit %dHz"
                               % (w.getnchannels(), w.getsampwidth() * 8, w.getframerate()))
        return w.readframes(w.getnframes())


def put(path: str, text: str, pcm: bytes) -> dict:
    u = CLOUD + path + "?text=" + urllib.parse.quote(text)
    req = urllib.request.Request(u, data=pcm, method="POST",
                                 headers={"Content-Type": "application/octet-stream",
                                          "X-Upload-Key": KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def make(path: str, label: str, text: str) -> bool:
    t0 = time.time()
    pcm = to_pcm(synth(text))
    res = put(path, text, pcm)
    print("%s  %s  %.1f秒で作成  %.1f秒ぶんの音  「%s」"
          % (time.strftime("%H:%M:%S"), label, time.time() - t0,
             len(pcm) / (RATE * 2.0), text), flush=True)
    return bool(res.get("ok"))


def once() -> int:
    """1回ぶん。作った本数を返す。"""
    n = 0
    for item in get_json("/spirit/todo").get("todo", []):
        if make("/spirit/todo/" + item["name"], item["name"], item["text"]):
            n += 1
    text = get_text("/spirit/say")
    if text and make("/spirit/say", "say_0", text):
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if a.once:
        n = once()
        print("作った本数:", n)
        return
    print("地霊の声係（ラズパイ）を始めます ->", CLOUD, flush=True)
    while True:
        try:
            once()
        except Exception as e:
            print("失敗:", e, flush=True)
        time.sleep(GAP)


if __name__ == "__main__":
    main()
