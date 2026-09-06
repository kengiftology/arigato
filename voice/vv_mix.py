# -*- coding: utf-8 -*-
"""選ばれた3人のスタイルを聴き、混ぜて「誰の声でもない」を作る（2026-09-06）。

本人が良いと言ったのは ずんだもん・後鬼・四国めたん。
ただし「そのまま使うのは違う」――有名なキャラの声だと、聞いた人は
そのキャラに帰属させる。場所ではなく。主張#11（感謝の主体を場所へ）が弱まる。

2つ試す:
  スタイル一覧 … なつき度に対応づけられるか見る
  3人を混ぜる  … 同時に鳴らせば、どれか1人には帰属できない
"""
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).parent
OUT = HERE / "work" / "vvmix"
API = "http://127.0.0.1:50021"
LINE = "あのね、わたし、きっちんちゃん。"
WHO = ["ずんだもん", "後鬼", "四国めたん"]


def post(path, data=None, **q):
    url = API + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else b"",
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=60).read()


def synth(sid, text, speed=0.95, pitch=0.0, into=1.0, pre=0.4):
    q = json.loads(post("/audio_query", speaker=sid, text=text))
    q["speedScale"] = speed
    q["pitchScale"] = pitch
    q["intonationScale"] = into
    q["volumeScale"] = 0.85
    q["prePhonemeLength"] = pre
    return post("/synthesis", data=q, speaker=sid)


def main():
    rep = io.open(HERE / "vvmix_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    spk = json.loads(urllib.request.urlopen(API + "/speakers", timeout=30).read())
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    say("使えるスタイル")
    picked = {}
    for s in spk:
        if s["name"] not in WHO:
            continue
        names = [st["name"] for st in s["styles"]]
        say("  %-8s %s" % (s["name"], "・".join(names)))
        picked[s["name"]] = {st["name"]: st["id"] for st in s["styles"]}

    # ① スタイルを聴く（なつき度に対応づけられるか）
    say("")
    say("スタイルごとに喋らせています")
    for who, styles in picked.items():
        for st, sid in list(styles.items())[:6]:
            wav = synth(sid, LINE)
            tag = "style_%s_%s" % (who, st.replace(".", "").replace("/", ""))
            (OUT / (tag + ".wav")).write_bytes(wav)
            say("  %s" % tag)

    # ② 3人を混ぜる。少しずらして、揃いすぎないようにする
    say("")
    say("3人を混ぜています")
    base = [("ずんだもん", "ノーマル"), ("後鬼", "ぬいぐるみver."),
            ("四国めたん", "ノーマル")]
    ys, srs = [], None
    for who, st in base:
        sid = picked[who].get(st) or list(picked[who].values())[0]
        p = OUT / ("_tmp_%s.wav" % who)
        p.write_bytes(synth(sid, LINE, pre=0.4))
        y, sr = sf.read(p)
        srs = sr
        ys.append(y.astype(np.float32))
        p.unlink()
    n = max(len(y) for y in ys)
    rng = np.random.default_rng(5)
    mix = np.zeros(n + int(0.15 * srs), dtype=np.float32)
    for k, y in enumerate(ys):
        off = int(rng.uniform(0.0, 0.06) * srs)
        m = min(len(y), len(mix) - off)
        mix[off:off + m] += y[:m] * (0.75 if k == 0 else 0.6)
    mix = mix / (np.abs(mix).max() + 1e-9) * 0.85
    sf.write(str(OUT / "mix_3人同時.wav"), mix, srs)
    say("  mix_3人同時")

    # ③ 1人だけ、声の高さと抑揚をずらして「そのままではない」形に
    for who, st, pit, into in (("ずんだもん", "ノーマル", -0.06, 1.15),
                               ("四国めたん", "ノーマル", 0.05, 0.85),
                               ("後鬼", "ぬいぐるみver.", -0.04, 1.10)):
        sid = picked[who].get(st) or list(picked[who].values())[0]
        wav = synth(sid, LINE, pitch=pit, into=into)
        (OUT / ("shift_%s.wav" % who)).write_bytes(wav)
        say("  shift_%s（高さ%+.2f・抑揚%.2f）" % (who, pit, into))

    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
