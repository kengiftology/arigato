# -*- coding: utf-8 -*-
"""これまで作った声を、種類ごとに並べて1本にする（2026-09-07）。

聴き比べ用。種類の頭でずんだもんが名前を読み上げる。
生の録音（人の声そのもの）は入れない。作った声だけ。
何分何秒に何があるかは reel_index.txt に書く。
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
OUT = W / "reel"
API = "http://127.0.0.1:50021"
SR = 24000
GAP, GAP_SEC = 0.6, 1.4                         # 曲間 / 種類の前

# （種類の名前, [ファイル…]）。時系列
SECTIONS = [
    ("1. ゼミ4人の平均。声の学習で作った最初の声", [
        "voice/blend.wav", "voice/pushed.wav", "voice/device.wav"]),
    ("2. ひとりずつと、かわいらしさの試み", [
        "cute/solo_p_a.wav", "cute/solo_p_b.wav", "cute/solo_p_d.wav", "cute/solo_p_e.wav",
        "cute/cute1_高さ+4.wav", "cute/cute3_両方.wav"]),
    ("3. 誰の声でもない声の試み", [
        "nobody/0_平均ひとつ.wav", "nobody/1_4人が同時に.wav", "nobody/2_平均を薄く重ねる.wav",
        "nobody/3_平均＋部屋.wav", "nobody/4_4人同時＋部屋.wav", "nobody/5_薄い重ね＋部屋.wav"]),
    ("4. 言葉にならない鳴き声。あつ森方式", [
        "babble/きづいた.wav", "babble/うれしい.wav", "babble/きになる.wav", "babble/しょんぼり.wav",
        "babble3/half_きづいた.wav", "babble3/half_うれしい.wav", "babble3/half_こまった.wav"]),
    ("5. 宛名。ねを足す、発語片、ためらい", [
        "atena/1_流暢.wav", "atena/2_ねを足す.wav", "atena/3_発語片.wav", "atena/4_ためらい.wav",
        "atena/5_知らせ_流暢.wav", "atena/6_知らせ_宛名.wav"]),
    ("6. 甥っ子の声から", [
        "kidsolo/1_流暢.wav", "kidsolo/2_発語片.wav", "kidsolo/3_ためらい.wav",
        "mix/kid30.wav", "mix/kid50.wav", "mix/kid70.wav"]),
    ("7. ゼロから組んだ声。最初のもの", [
        "scratch/1_小さい生きもの.wav", "scratch/2_もっと小さい.wav", "scratch/3_おおきめ.wav",
        "scratch/4_息おおめ.wav"]),
    ("8. ゼロから組んだ声。最後のもの", [
        "kana/1_なのり.wav", "kana/4_あいさつ.wav", "kana/5_しらせ.wav", "kana/6_そわそわ.wav"]),
    ("9. ボイスボックスをそのまま", [
        "vv/ずんだもん_あまあま.wav", "vv/四国めたん_あまあま.wav", "vv/四国めたん_ささやき.wav",
        "vv/後鬼_ぬいぐるみver.wav", "vv/もち子さん_のんびり.wav", "vv/冥鳴ひまり_ノーマル.wav",
        "vv/春日部つむぎ_ノーマル.wav", "vv/白上虎太郎_ふつう.wav"]),
    ("10. ボイスボックスを重ねる、ずらす", [
        "vvmix/mix_3人同時.wav", "vvmix/shift_ずんだもん.wav", "vvmix/shift_四国めたん.wav",
        "vvmix/shift_後鬼.wav", "vvmix/style_ずんだもん_あまあま.wav", "vvmix/style_ずんだもん_ささやき.wav"]),
    ("11. ボイスボックス3人を、部品ごとに溶かす", [
        "blend/1_なのり_そのまま.wav", "blend/1_なのり_小さめ.wav", "blend/1_なのり_もっと小さめ.wav",
        "blend/1_なのり_大きめ.wav", "blend/3_しらせ_小さめ.wav"]),
    ("12. 書評ゼミの4人。ひとりずつと、混ぜたもの", [
        "each/単独_p_a.wav", "each/単独_p_b.wav", "each/単独_p_c.wav", "each/単独_p_d.wav",
        "blend_people/1_なのり.wav", "each/重み_C半分.wav", "each/抜き_p_cなし.wav"]),
    ("13. 4人の声に、ずんだもんの読み方を写す", [
        "manner/1_なのり_0_もと.wav", "manner/1_なのり_0b_分解して戻すだけ.wav",
        "manner/1_なのり_1_読み方だけ.wav", "manner/1_なのり_3_読み方＋小さめ＋高め.wav",
        "manner/診断_A_もとの線を1.3倍.wav", "manner/診断_B_写した線をなめらかに.wav",
        "manner/診断_C_写した線を半分だけ.wav"]),
    ("14. キャラ3人と、4人を混ぜる", [
        "chars_people/1_なのり_キャラ100.wav", "chars_people/1_なのり_キャラ70_人30.wav",
        "chars_people/1_なのり_キャラ50_人50.wav", "chars_people/1_なのり_キャラ40_人60.wav",
        "chars_people/1_なのり_キャラ30_人70.wav"]),
    ("15. いま載せているもの。ずんだもんの持ち歌", [
        "lines_vv/hello_known_0.wav", "lines_vv/hello_close_0.wav", "lines_vv/news_0.wav",
        "lines_vv/worse_0.wav", "lines_vv/alone_1.wav", "lines_vv/hesitate_0.wav"]),
]


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
    idx = io.open(OUT / "reel_index.txt", "w", encoding="utf-8")
    pieces, t = [], 0.0
    missing = []

    def push(x):
        nonlocal t
        pieces.append(x)
        t += len(x) / SR

    def mmss(s):
        return "%d:%02d" % (int(s) // 60, int(s) % 60)

    for title, files in SECTIONS:
        push(np.zeros(int(GAP_SEC * SR), np.float32))
        idx.write("\n[%s] %s\n" % (mmss(t), title))
        push(narrate(title))
        push(np.zeros(int(GAP * SR), np.float32))
        for f in files:
            p = W / f
            if not p.exists():
                missing.append(f)
                continue
            idx.write("  %s  %s\n" % (mmss(t), f))
            push(load(p))
            push(np.zeros(int(GAP * SR), np.float32))
    y = np.concatenate(pieces)
    wav = OUT / "声の記録_2026-09-07.wav"
    sf.write(str(wav), y, SR)
    mp3 = OUT / "声の記録_2026-09-07.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-b:a", "96k", str(mp3)], check=True)
    idx.write("\n合計 %s\n" % mmss(t))
    if missing:
        idx.write("見つからなかったもの: %s\n" % ", ".join(missing))
    idx.close()
    (OUT / "_narr.wav").unlink(missing_ok=True)
    print("合計 %s / 見つからなかった %d" % (mmss(t), len(missing)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
