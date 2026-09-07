# -*- coding: utf-8 -*-
"""地霊のいまの一言を、ずんだもんの声にしてクラウドへ置く（2026-09-07）。

クラウドには音声合成が無い。espeak-ng も入っていない（実測：
「No such file or directory」が20件）。VOICEVOX は宅内で動いていて、
クラウドから宅内へは入れない。だから宅内の側から取りに行って、
作って、置きに行く。

  python voice/sayd.py            … 動かし続ける（30秒おき）
  python voice/sayd.py --once     … 1回だけ作って終わる

VOICEVOX ENGINE が 127.0.0.1:50021 で動いている必要がある。
ラズパイに engine を置けば、そちらで動かしっぱなしにできる。

台詞の作り方（速さ・頭の沈黙・音量）は make_lines_vv.py と同じにしてある。
場面ごとの作り置き19本と、その場の一言とで、声が変わって聞こえないように。

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


def wanted() -> str:
    """クラウドが「これを声にしてほしい」と言っている一言。無ければ空。"""
    with urllib.request.urlopen(CLOUD + "/spirit/say", timeout=20) as r:
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
    req = urllib.request.Request(u2, method="POST",
                                 data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def to_pcm(wav_bytes: bytes) -> bytes:
    """wav → 生PCM（16kHz・16bit・モノラル）。C3はこの形しか鳴らせない。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
            raise RuntimeError("形が違う: %dch %dbit %dHz"
                               % (w.getnchannels(), w.getsampwidth() * 8, w.getframerate()))
        return w.readframes(w.getnframes())


def put(text: str, pcm: bytes) -> dict:
    """作った声をクラウドへ置く。何を読んだかも添える。"""
    u = CLOUD + "/spirit/say?text=" + urllib.parse.quote(text)
    req = urllib.request.Request(u, data=pcm, method="POST",
                                 headers={"Content-Type": "application/octet-stream",
                                          "X-Upload-Key": KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def once() -> bool:
    """1回ぶん。作ったら True、作るものが無ければ False。"""
    text = wanted()
    if not text:
        return False
    t0 = time.time()
    pcm = to_pcm(synth(text))
    res = put(text, pcm)
    print("%s  %.1f秒で作成  %.1f秒ぶんの音  「%s」"
          % (time.strftime("%H:%M:%S"), time.time() - t0,
             len(pcm) / (RATE * 2.0), text), flush=True)
    return bool(res.get("ok"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1回だけ作って終わる")
    a = ap.parse_args()
    if a.once:
        if not once():
            print("いま声にするものはありません（もう作ってある）")
        return
    print("地霊の声係を始めます ->", CLOUD, flush=True)
    while True:
        try:
            once()
        except Exception as e:
            print("失敗:", e, flush=True)
        time.sleep(GAP)


if __name__ == "__main__":
    main()
