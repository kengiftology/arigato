# -*- coding: utf-8 -*-
"""統合機ファーム用アセット生成: dev/win の .bin → C ヘッダ (assets.h)
   声用に、各フレーズの母音列（あつ森語の楽譜）も埋め込む"""
import sys
from pathlib import Path

from gen_windows import PHRASES
from voice_lab import VOWEL_OF, NOISY

OUT = Path(__file__).parent.parent / "spirits_out"
VIDX = {"a": 0, "i": 1, "u": 2, "e": 3, "o": 4}

def carr(name, data):
    rows = [", ".join(str(b) for b in data[i:i+20]) for i in range(0, len(data), 20)]
    return f"const uint8_t {name}[{len(data)}] PROGMEM = {{\n  " + ",\n  ".join(rows) + "\n};\n"

def vowel_seq(text):
    """フレーズ → 母音列（0-4=あいうえお 5=間 6=小休止）＋子音ノイズbitmask"""
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
    return seq, noisy

def build(pid):
    out = ["// 自動生成: python gen_assets.py " + pid, "#pragma once", "#include <Arduino.h>", ""]
    anims = ["hatch", "idle", "happy", "sad", "sleep", "notice"]
    for a in anims:
        data = (OUT / f"dev_{pid}_{a}.bin").read_bytes()
        out.append(carr(f"anim_{a}", data))
    wins = sorted(OUT.glob(f"win_{pid}_*.bin"))
    for i, w in enumerate(wins):
        out.append(carr(f"win_{i}", w.read_bytes()))
    # 母音列
    for i, ph in enumerate(PHRASES[pid]):
        seq, noisy = vowel_seq(ph)
        out.append(f"const uint8_t vow_{i}[{len(seq)}] PROGMEM = {{{', '.join(map(str, seq))}}};")
        out.append(f"const uint32_t vow_{i}_noisy = {noisy}u;")
    out.append(f"\n#define N_WINS {len(wins)}")
    out.append("const uint8_t* const WINS[N_WINS] = {" + ", ".join(f"win_{i}" for i in range(len(wins))) + "};")
    out.append("const uint8_t* const VOWS[N_WINS] = {" + ", ".join(f"vow_{i}" for i in range(len(wins))) + "};")
    out.append("const uint32_t VOW_NOISY[N_WINS] = {" + ", ".join(f"vow_{i}_noisy" for i in range(len(wins))) + "};")
    out.append("const uint16_t VOW_LEN[N_WINS] = {" +
               ", ".join(str(len(vowel_seq(ph)[0])) for ph in PHRASES[pid]) + "};")
    p = Path(__file__).parent.parent / "spirit_body" / "assets.h"
    p.parent.mkdir(exist_ok=True)
    p.write_text("\n".join(out), encoding="utf-8")
    print(f"→ {p}  ({sum(len((OUT/f'dev_{pid}_{a}.bin').read_bytes()) for a in anims)//1024}KB anims + {len(wins)} wins)")

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "cute_07")
