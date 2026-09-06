# -*- coding: utf-8 -*-
"""良いと言われた声が、どう「喋って」いるかを測る（2026-09-06）。

声そのもの（高さ・共鳴）は前に測った。今度は喋り方のほう。

  ・拍ごとの長さ（全部同じか、ばらついているか）
  ・拍ごとの高さ（どこで上がってどこで落ちるか）
  ・無声化（い・う が息だけになる。日本語らしさの大きな部分）

VOICEVOX のエンジンは、合成の前に「どう読むか」の設計図を返す。
音そのものではなく、この設計図の数字だけを見る。
"""
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
API = "http://127.0.0.1:50021"

LINES = [
    "あのね、わたし、きっちんちゃん。",
    "あ、きた。",
    "あのね、さっきね、だれかがね、きれいにしてくれたのかなあ。",
    "なんだかね、そわそわするなあ。",
]
SPEAKERS = {"ずんだもん": 3, "四国めたん": 2, "後鬼": 27}


def query(text, sid):
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": sid})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        return json.load(r)


def main():
    rep = io.open(HERE / "prosody_result.txt", "w", encoding="utf-8")

    def say(s=""):
        rep.write(s + chr(10))

    all_d, all_p = [], []
    for name, sid in SPEAKERS.items():
        say("■ %s" % name)
        for text in LINES:
            q = query(text, sid)
            say("  「%s」" % text)
            say("    %-4s %-8s %-8s %s" % ("拍", "長さ秒", "高さ", "無声"))
            for ph in q["accent_phrases"]:
                for m in ph["moras"]:
                    d = (m.get("consonant_length") or 0.0) + m["vowel_length"]
                    p = m["pitch"]
                    dev = "○" if p == 0.0 else ""
                    say("    %-4s %-8.3f %-8.1f %s"
                        % (m["text"], d, p, dev))
                    all_d.append(d)
                    if p > 0:
                        all_p.append(p)
                if ph.get("pause_mora"):
                    say("    %-4s %-8.3f" % ("、", ph["pause_mora"]["vowel_length"]))
            say("")
        say("")

    import statistics as st
    say("=" * 50)
    say("拍の長さ  平均 %.3f 秒 / 最短 %.3f / 最長 %.3f / ばらつき %.3f"
        % (st.mean(all_d), min(all_d), max(all_d), st.pstdev(all_d)))
    say("高さ      平均 %.2f / 最低 %.2f / 最高 %.2f（対数。差1.0で約2.7倍）"
        % (st.mean(all_p), min(all_p), max(all_p)))
    say("")
    say("いま私が作っている声：拍はすべて 0.150 秒（ばらつき 0.000）")
    say("                      高さは文全体でひとつの山（±10%）")
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
