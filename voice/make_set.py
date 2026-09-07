# -*- coding: utf-8 -*-
"""表の行ごとに、音を1本ずつ付ける（2026-09-07）。

本人がまとめた表（順 1・2・3・5・6・7・8）に対応する音を、行ごとに1本にする。
行1〜3は、その時点のコードを git から取り出して同じ台詞で作り直す。
直す前と後が、同じ台詞で並ぶ。
"""
import io
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

HERE = Path(__file__).parent
W = HERE / "work"
OUT = W / "set"
STEPS = W / "steps"
API = "http://127.0.0.1:50021"
SR = 24000
GAP = 0.6

# ゼロから組んだ声の、各時点のコード（git のコミット）
VERSIONS = [
    ("67d13b5", "プチプチ（直す前）"),
    ("4816358", "プチプチを直した"),
    ("d5982d5", "読み方を合わせた"),
    ("5bb91ee", "口を止めないようにした"),
    ("fd741f7", "部品まで直した（最後）"),
]


def rebuild(sha: str) -> Path:
    """その時点の kana_voice.py を取り出して走らせ、出力フォルダを返す。"""
    dst = STEPS / sha
    if dst.exists() and any(dst.glob("*.wav")):
        return dst
    dst.mkdir(parents=True, exist_ok=True)
    src = subprocess.run(["git", "show", "%s:voice/kana_voice.py" % sha],
                         capture_output=True, check=True).stdout.decode("utf-8")
    src = src.replace('OUT = HERE / "work" / "kana"', 'OUT = HERE / "steps" / "%s"' % sha)
    py = W / ("kv_%s.py" % sha)
    py.write_text(src, encoding="utf-8")
    subprocess.run([str(HERE / ".venv" / "Scripts" / "python.exe"), str(py)],
                   env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
                   capture_output=True, check=True)
    py.unlink()
    return dst


def load(p: Path) -> np.ndarray:
    x, sr = sf.read(str(p))
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        from math import gcd
        g = gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g)
    x = np.asarray(x, dtype=np.float32)
    return x / (np.abs(x).max() + 1e-9) * 0.7


def narrate(text: str) -> np.ndarray:
    u = API + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": 3})
    with urllib.request.urlopen(urllib.request.Request(u, method="POST"), timeout=30) as r:
        q = json.load(r)
    q["speedScale"] = 1.05
    u2 = API + "/synthesis?" + urllib.parse.urlencode({"speaker": 3})
    req = urllib.request.Request(u2, method="POST", data=json.dumps(q).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    p = OUT / "_narr.wav"
    with urllib.request.urlopen(req, timeout=60) as r:
        p.write_bytes(r.read())
    return load(p) * 0.8


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    idx = io.open(OUT / "set_index.txt", "w", encoding="utf-8")

    v = {sha: rebuild(sha) for sha, _ in VERSIONS}
    L = "1_なのり.wav"

    # 行番号, 見出し（読み上げ）, [(説明, ファイル)…]
    ROWS = [
        ("1", "ゼロから声を組む。直す前と、直したあと", [
            ("プチプチ、直す前", v["67d13b5"] / L),
            ("直したあと", v["4816358"] / L),
            ("直す前、別の台詞", v["67d13b5"] / "6_そわそわ.wav"),
            ("直したあと、別の台詞", v["4816358"] / "6_そわそわ.wav")]),
        ("2", "読み方を測って合わせた。前と後", [
            ("合わせる前", v["4816358"] / L),
            ("合わせたあと", v["d5982d5"] / L),
            ("合わせる前、別の台詞", v["4816358"] / "5_しらせ.wav"),
            ("合わせたあと、別の台詞", v["d5982d5"] / "5_しらせ.wav")]),
        ("3", "肉声と比べた。口が止まっていた。前と後", [
            ("直す前", v["d5982d5"] / L),
            ("口を止めないようにした", v["5bb91ee"] / L),
            ("部品まで直した、最後のもの", v["fd741f7"] / L)]),
        ("5", "同じ文を、既存の合成音声と並べた", [
            ("ゼロから組んだ声、最後のもの", v["fd741f7"] / L),
            ("既存の合成音声、ずんだもん", W / "parts" / "vv.wav")]),
        ("6", "ゼミ録音を人ごとに分けて混ぜた", [
            ("ひとり目", W / "each" / "単独_p_a.wav"),
            ("ふたり目", W / "each" / "単独_p_b.wav"),
            ("さんにん目、C", W / "each" / "単独_p_c.wav"),
            ("よにん目", W / "each" / "単独_p_d.wav"),
            ("均一に混ぜた", W / "blend_people" / "1_なのり.wav"),
            ("Cを半分にした", W / "each" / "重み_C半分.wav")]),
        ("7", "かわいい声を材料として混ぜた", [
            ("キャラ100", W / "chars_people" / "1_なのり_キャラ100.wav"),
            ("キャラ70、人30", W / "chars_people" / "1_なのり_キャラ70_人30.wav"),
            ("キャラ50、人50", W / "chars_people" / "1_なのり_キャラ50_人50.wav"),
            ("キャラ40、人60", W / "chars_people" / "1_なのり_キャラ40_人60.wav"),
            ("キャラ30、人70", W / "chars_people" / "1_なのり_キャラ30_人70.wav")]),
        ("8", "一旦ずんだもんで。クラウドに載せた持ち歌", [
            ("迎える", W / "lines_vv" / "hello_known_0.wav"),
            ("なついている人を迎える", W / "lines_vv" / "hello_close_0.wav"),
            ("知らせ", W / "lines_vv" / "news_0.wav"),
            ("そわそわ", W / "lines_vv" / "worse_0.wav"),
            ("ひとりごと", W / "lines_vv" / "alone_1.wav"),
            ("ためらい", W / "lines_vv" / "hesitate_0.wav")]),
    ]

    gap = np.zeros(int(GAP * SR), np.float32)
    for num, title, items in ROWS:
        pieces, t = [], 0.0
        idx.write("\n順%s  %s\n" % (num, title))
        head = narrate("順、%s。%s" % (num, title))
        pieces += [head, gap]
        t += (len(head) + len(gap)) / SR
        for label, p in items:
            if not p.exists():
                idx.write("  （見つからない）%s\n" % p.name)
                continue
            lab = narrate(label)
            x = load(p)
            idx.write("  %d:%02d  %s  ← %s\n" % (int(t) // 60, int(t) % 60, label,
                                                  p.relative_to(W)))
            pieces += [lab, gap, x, gap]
            t += (len(lab) + len(x) + 2 * len(gap)) / SR
        y = np.concatenate(pieces)
        stem = "順%s_%s" % (num, title.split("。")[0].replace(" ", ""))
        sf.write(str(OUT / (stem + ".wav")), y, SR)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(OUT / (stem + ".wav")),
                        "-b:a", "96k", str(OUT / (stem + ".mp3"))], check=True)
        (OUT / (stem + ".wav")).unlink()
        print("%s  %d:%02d" % (stem, int(t) // 60, int(t) % 60))
    (OUT / "_narr.wav").unlink(missing_ok=True)
    idx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
