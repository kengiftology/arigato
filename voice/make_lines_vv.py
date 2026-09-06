# -*- coding: utf-8 -*-
"""地霊の持ち歌を、VOICEVOX（ずんだもん）で作って、クラウドに置く（2026-09-07）。

「一旦ずんだもんで良いや」。声の探索はいったん止め、まず鳴らす。
台詞・場面の分け方・頭の沈黙・音量は make_lines.py と同じ（憲法どおり）。

  python make_lines_vv.py            … 作るだけ（work/lines_vv/）
  python make_lines_vv.py --upload   … 作って、クラウドへ載せる

クレジット: VOICEVOX:ずんだもん
"""
import argparse
import io
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

from make_lines import LINES

HERE = Path(__file__).parent
OUT = HERE / "work" / "lines_vv"
API = "http://127.0.0.1:50021"
SPEAKER = 3                                          # ずんだもん（ノーマル）
CLOUD = "https://arigato-3ipecjbnha-an.a.run.app/spirit/lines/"
KEY = "06dc964a3cdd2c4f4c5c1d8592dff543"             # 橋渡し機と同じ鍵


def synth(text: str, path: Path):
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": SPEAKER})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        q = json.load(r)
    q["speedScale"] = 0.95                           # 少しゆっくり（第5条 間）
    q["prePhonemeLength"] = 0.4                      # 頭に沈黙
    q["postPhonemeLength"] = 0.2
    u2 = API + "/synthesis?" + urllib.parse.urlencode({"speaker": SPEAKER})
    req = urllib.request.Request(u2, method="POST", data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        path.write_bytes(r.read())


def upload(name: str, pcm: Path) -> str:
    req = urllib.request.Request(CLOUD + name, method="POST", data=pcm.read_bytes(),
                                 headers={"Content-Type": "application/octet-stream",
                                          "X-Upload-Key": KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("url", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true")
    a = ap.parse_args()
    rep = io.open(HERE / "lines_vv_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    n = 0
    for kind, texts in LINES.items():
        for j, text in enumerate(texts):
            tag = "%s_%d" % (kind, j)
            raw = OUT / (tag + "_raw.wav")
            synth(text, raw)
            y, sr = sf.read(str(raw))
            if y.ndim > 1:
                y = y.mean(axis=1)
            y = y / (np.abs(y).max() + 1e-9) * 0.75          # 第7条 音量
            sf.write(str(OUT / (tag + ".wav")), y, sr)
            raw.unlink()
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(OUT / (tag + ".wav")),
                            "-ac", "1", "-ar", "16000", "-f", "s16le",
                            str(OUT / (tag + ".pcm"))], check=True)
            line = "  %-14s %4.1f秒 「%s」" % (tag, len(y) / sr, text)
            if a.upload:
                upload(tag, OUT / (tag + ".pcm"))
                line += "  → 載せた"
            say(line)
            n += 1
    say("")
    say("%d本 %s（VOICEVOX:ずんだもん）" % (n, "作って載せました" if a.upload else "作りました"))
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
