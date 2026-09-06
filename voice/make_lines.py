# -*- coding: utf-8 -*-
"""地霊の持ち歌をまとめて作る（2026-09-06）。

クラウドにGPUは無いので、その場では合成できない。
憲法どおりなら台詞は限られた数で足りる（数も評価も言わないので）。
あらかじめ全部作って置いておく。生き物の持ち歌が限られているのは自然なこと。

守っているもの:
  第4章 宛名 … 「ね」・フィラー・発語片に分解
  第5条 間   … 頭に沈黙
  第7条 音量 … 上げない
  第8条 中身 … 数・評価・助言を言わない
  台帳#12   … 名指ししない
  やらないこと … 毎回同じにしない（場面ごとに複数用意して選ばせる）

声は大人4人の平均。甥っ子さんの声はXTTSが再現できないため、後で差し替える。
"""
import io
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
PEOPLE = HERE / "people"
OUT = HERE / "work" / "lines"
SR = 24000

# 場面ごとの台詞。同じ場面に複数入れて、毎回違うものを選べるようにする
LINES = {
    # 迎える（なつき度べつ）
    "hello_new": [
        "あれ。あのね……えーと、どなたかなあ。",
        "あ、えーと……はじめまして、かなあ。",
    ],
    "hello_known": [
        "あ、きた。",
        "あのね、きたね。",
    ],
    "hello_close": [
        "あ、きてくれたね。うれしいなあ。",
        "あのね、まってたんだよ。",
    ],
    "hello_cold": [
        "……あ。",
        "ふうん。",
    ],
    # 何かが変わった
    "better": [
        "あのね、なんだかね……ひろくなった気がするなあ。",
        "あれ。なんかね、すっきりしてるね。",
    ],
    "worse": [
        "あのね、ちょっとね……そわそわするなあ。",
        "うーん。なんだかね、おちつかないなあ。",
    ],
    # 誰かがやってくれたことを、次の人に伝える（名指ししない）
    "news": [
        "あのね、さっきね……だれかがね、きれいにしてくれたのかなあ。",
        "あのね、なんかね、きれいになっててね……だれだろうね。",
    ],
    # ひとりごと（無人のとき）
    "alone": [
        "……しずかだなあ。",
        "あのね……ひとりだと、ひまだなあ。",
        "ふう。",
    ],
    # ためらい（初対面・久しぶり・変化に気づいたとき）
    "hesitate": [
        "あのね、えーとね……なんだろうね……あ、そうだ。",
        "うーんとね……えーと……なんだったかなあ。",
    ],
}


def main():
    rep = io.open(HERE / "lines_result.txt", "w", encoding="utf-8")

    def say(s=""):
        print(s)
        rep.write(s + chr(10))
        rep.flush()

    import torch
    import soundfile as sf
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts
    from TTS.utils.manage import ModelManager

    path, _, _ = ModelManager().download_model(
        "tts_models/multilingual/multi-dataset/xtts_v2")
    cfg = XttsConfig()
    cfg.load_json(str(Path(path) / "config.json"))
    model = Xtts.init_from_config(cfg)
    model.load_checkpoint(cfg, checkpoint_dir=str(path), eval=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev)

    adults = [d for d in sorted(PEOPLE.glob("p_*")) if d.name != "p_kid"]
    g = torch.tensor(np.mean([np.load(d / "latent.npy") for d in adults], axis=0)).to(dev)
    s = torch.tensor(np.mean([np.load(d / "embedding.npy") for d in adults], axis=0)).to(dev)
    say("声: 大人%d人の平均" % len(adults))

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()

    n = 0
    for kind, texts in LINES.items():
        for j, text in enumerate(texts):
            wav = np.asarray(model.inference(
                text, "ja", g, s, temperature=0.8, speed=1.0)["wav"])
            y = np.concatenate([np.zeros(int(0.4 * SR), dtype=np.float32), wav])
            y = y / (np.abs(y).max() + 1e-9) * 0.75          # 第7条
            tag = "%s_%d" % (kind, j)
            sf.write(str(OUT / (tag + ".wav")), y, SR)
            # C3が鳴らす形（16kHz・16bit・モノラルの生PCM）にもする
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(OUT / (tag + ".wav")),
                 "-ac", "1", "-ar", "16000", "-f", "s16le", str(OUT / (tag + ".pcm"))],
                check=False)
            say("  %-14s 「%s」" % (tag, text))
            n += 1
    say("")
    say("%d本つくりました: %s" % (n, OUT))
    rep.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
