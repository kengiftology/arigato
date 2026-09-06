# -*- coding: utf-8 -*-
"""VOICEVOXの声で地霊を喋らせてみる（2026-09-06）。

ゼロから組んだ声は、ざらつきとこもりが取れなかった。
既にある日本語の声を使う。日本語のアクセントも正しく入っている。

選ぶ観点：
  ・人らしすぎないこと（地霊は人ではない）
  ・有名すぎないこと（キャラアプリに見えてしまう）
  ・小さい生きものに聞こえること
「後鬼」の「ぬいぐるみver.」は、人ではないものの声なので入れてある。
"""
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "work" / "vv"
API = "http://127.0.0.1:50021"
LINE = "あのね、わたし、きっちんちゃん。"

WANT = [
    ("後鬼", "ぬいぐるみver."),
    ("春日部つむぎ", "ノーマル"),
    ("冥鳴ひまり", "ノーマル"),
    ("四国めたん", "ささやき"),
    ("四国めたん", "あまあま"),
    ("もち子さん", "のんびり"),
    ("白上虎太郎", "ふつう"),
    ("ずんだもん", "あまあま"),
]


def post(path, data=None, **q):
    url = API + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else b"",
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=60).read()


def main():
    rep = io.open(HERE / "vv_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    spk = json.loads(urllib.request.urlopen(API + "/speakers", timeout=30).read())
    ids = {}
    for s in spk:
        for st in s["styles"]:
            ids[(s["name"], st["name"])] = st["id"]

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    for name, style in WANT:
        sid = ids.get((name, style))
        if sid is None:
            say("  %s（%s）は見つかりません" % (name, style))
            continue
        q = json.loads(post("/audio_query", speaker=sid, text=LINE))
        q["speedScale"] = 0.95          # 少しゆっくり（憲法第5条の気配）
        q["volumeScale"] = 0.85         # 第7条 音量を上げない
        q["prePhonemeLength"] = 0.4     # 頭に間
        wav = post("/synthesis", data=q, speaker=sid)
        tag = "%s_%s" % (name, style.replace(".", ""))
        (OUT / (tag + ".wav")).write_bytes(wav)
        say("  %s" % tag)

    say("")
    say("できました: %s" % OUT)
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
