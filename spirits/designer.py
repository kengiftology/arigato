# -*- coding: utf-8 -*-
"""
エージェント・デザイナー — 人ごとの精霊をClaudeが創作する
========================================================
役割分担:
  Claude（このファイル）: その人のための精霊を1回だけデザインする
    - 32×32のドット絵（体・模様・かざり）を自由創作
    - 名前・性格・口ぐせも同時に生む（→ 声=spirit.py と共通のキャラシート）
    - 結果は characters/<人のID>.json に保存 = 2回目以降はAPI不要
  パラメトリック（式）: 表情だけを毎回計算で動かす
    - M(散らかり度)・N(放置度) → 目と口。怒り顔は存在しない（設計の憲法）

使い方:
  python designer.py <人のID>            → デザイン生成（キャッシュ済ならスキップ）
  python designer.py <人のID> --force    → 作り直し
  python designer.py <人のID> --render [M N]  → PNG出力
  python designer.py --gallery <ID> <ID> ...  → 並べて出力
"""
import colorsys
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
SHEET_DIR = HERE / "characters"
GRID = 32
MODEL = "claude-opus-5"

M_TH = [0.20, 0.45, 0.70, 0.90]
N_TH = [0.20, 0.40, 0.60, 0.80]


def q(x, th):
    for i, t in enumerate(th):
        if x < t:
            return i
    return len(th)


# ================= 1. エージェントによるデザイン生成 =================

DESIGN_PROMPT = """\
あなたはキャラクターデザイナーです。共有空間（キッチンなど）に宿る小さな精霊を、\
「{person_id}」という人のためだけに1体デザインしてください。\
この精霊はその人にだけ見える、その人だけの子です。他の誰の子とも違う、記憶に残る姿にしてください。

# この子のお題（この人固有の種から決まっています。必ずこのお題から発想すること）
- モチーフ: {motif}
- 気質: {temperament}
- 体型の方向: {shape_hint}
お題をそのまま使うのではなく、あなたの解釈で豊かに膨らませてください。\
ただしモチーフと気質は必ず姿と性格に反映すること。湯気・ゆげの精霊は禁止（既にいます）。

# 世界観
- 場所に宿る精霊。場所が散らかると元気がなくなり、整えてもらうと喜ぶ
- 責めない・怒らない・命令しない、けなげで可愛い存在
- たまごっち的な親しみやすさ。ゆるくて丸みのあるドット絵

# ドット絵の仕様（厳守）
- 32行×32文字のグリッド。使える文字は4種類だけ:
    "." = 背景（何もない）
    "B" = 体
    "P" = 模様（おなか・ぶち・しま・うずまき等、体の内側）
    "A" = アクセント（耳・ツノ・葉っぱ・しっぽ・とさか等、体の外に出る部分）
- 体は下寄せ（だいたい8行目〜28行目）。左右おおむね対称。小さな非対称（アホ毛・葉っぱ等）は魅力になるのでOK
- 輪郭はなめらかに階段状で。1ドットだけ飛び出す孤立点は避ける
- 目と口は描かない（システムが後から動かすため）。顔になる部分は体"B"で塗りつぶしておく
- 顔スペース: eye_yの行とmouth_yの行の周辺は模様を置かず"B"のままにする

# 性格と姿を一致させること
体型・かざり・模様は、性格から発想する（せっかち→とがり気味、のんびり→ずんぐり等）。

# 出力（JSONのみ）
{{
  "name": "ひらがな2〜4文字の名前",
  "species": "何の精霊か一言（例: こけの精霊）",
  "personality": "性格を2〜3文で",
  "speech_style": "口ぐせ・話し方を1〜2文で",
  "likes": "好きなことを1文で",
  "colors": {{"body": "#RRGGBB", "pattern": "#RRGGBB", "accent": "#RRGGBB"}},
  "art": ["32文字の行", ... 32行],
  "face": {{"eye_y": 目の行(13〜17), "eye_gap": 目の間隔(3〜5), "eye_style": 0〜3,
           "eye_size": "big|small", "mouth_y": 口の行(19〜22), "mouth_w": 口の幅(3〜5),
           "smile_bias": -1〜1, "cheeks": true|false}}
}}
eye_style: 0=丸目 1=たれ目(やさしい) 2=りりしい目 3=ジト目
背景色は暗い夜色なので、colorsは暗背景で映える中明度に。
"""


MOTIFS = ["こけ", "きのこ", "ほしくず", "しずく", "ひだね（小さな火）", "そよかぜ",
          "つちくれ", "はっぱ", "まるい小石", "ゆきだま", "かみなりのこども", "はなのつぼみ",
          "しおつぶ", "まめ", "くもきれ（小さな雲）", "こなゆき", "みつばち", "かたつむり",
          "どんぐり", "わたげ"]
TEMPERAMENTS = ["せっかちで世話好き", "のんびりマイペース", "さみしがりで甘えん坊",
                "はずかしがりだが好奇心旺盛", "おっとり天然", "元気いっぱいのお調子者",
                "無口な観察者", "心配性でやさしい"]
SHAPE_HINTS = ["ずんぐり低め", "のっぽで細め", "ちびで小さめ", "かくかく角ばり",
               "ぷにぷに横広", "とんがり気味"]


def seeds_of(person_id: str) -> dict:
    """人のID → その人固有のお題（ハッシュで決定的に配る＝多様性の保証）"""
    import hashlib, random
    seed = int.from_bytes(hashlib.sha256(person_id.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    return {"motif": rng.choice(MOTIFS),
            "temperament": rng.choice(TEMPERAMENTS),
            "shape_hint": rng.choice(SHAPE_HINTS)}


def sheet_path(person_id: str) -> Path:
    return SHEET_DIR / f"{person_id}.json"


def generate(person_id: str, force: bool = False) -> dict:
    """Claudeにデザインさせて保存。キャッシュがあればそれを返す（API代は1人1回だけ）"""
    p = sheet_path(person_id)
    if p.exists() and not force:
        return json.loads(p.read_text(encoding="utf-8"))

    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user",
                   "content": DESIGN_PROMPT.format(person_id=person_id, **seeds_of(person_id))}],
        extra_body={"output_config": {"format": {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "species": {"type": "string"},
                "personality": {"type": "string"},
                "speech_style": {"type": "string"},
                "likes": {"type": "string"},
                "colors": {"type": "object", "properties": {
                    "body": {"type": "string"}, "pattern": {"type": "string"},
                    "accent": {"type": "string"}},
                    "required": ["body", "pattern", "accent"],
                    "additionalProperties": False},
                "art": {"type": "array", "items": {"type": "string"}},
                "face": {"type": "object", "properties": {
                    "eye_y": {"type": "integer"}, "eye_gap": {"type": "integer"},
                    "eye_style": {"type": "integer"}, "eye_size": {"type": "string"},
                    "mouth_y": {"type": "integer"}, "mouth_w": {"type": "integer"},
                    "smile_bias": {"type": "integer"}, "cheeks": {"type": "boolean"}},
                    "required": ["eye_y", "eye_gap", "eye_style", "eye_size",
                                 "mouth_y", "mouth_w", "smile_bias", "cheeks"],
                    "additionalProperties": False},
            },
            "required": ["name", "species", "personality", "speech_style", "likes",
                         "colors", "art", "face"],
            "additionalProperties": False,
        }}}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("デザイン生成が拒否されました")
    sheet = json.loads(next(b.text for b in resp.content if b.type == "text"))
    sheet["person_id"] = person_id
    sheet["art"] = _sanitize_art(sheet["art"])
    SHEET_DIR.mkdir(exist_ok=True)
    p.write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8")
    return sheet


def _sanitize_art(rows) -> list:
    """32行×32文字に正規化。未知文字は背景に落とす"""
    ok = set(".BPA")
    out = []
    for r in (rows or [])[:GRID]:
        r = "".join(ch if ch in ok else "." for ch in str(r))[:GRID]
        out.append(r.ljust(GRID, "."))
    while len(out) < GRID:
        out.append("." * GRID)
    return out


# ================= 2. パラメトリックな表情（ランタイム・API不要） =================

def rect(c0, c1, r0, r1):
    return {(c, r) for c in range(c0, c1 + 1) for r in range(r0, r1 + 1)}


def disc(cx, cy, rad):
    return {(c, r) for c in range(GRID) for r in range(GRID)
            if (c - cx) ** 2 + (r - cy) ** 2 <= rad * rad}


def mirror(cells):
    return {(31 - c, r) for c, r in cells}


def umouth(depth, cx, cy, wh, thick=1):
    """口 v2（2026-08-18 口テストより）
    法則: 横線から1ドットだけ上に飛び出す端は「目」に誤読される。
    → 口は「飛び出しゼロの1行」か「塊」だけで描く。放物線は使わない。
    depth: 4=大喜び 3=笑顔 2=にっこり 1=ほぼ平 0=平 -1=控えめな悲しみ
    wh は幅の目安（2〜3）。cx=16 を中心に左右対称。"""
    w = max(2, min(3, wh - 1))                     # 半幅（左右 w ずつ）
    L = list(range(cx - w, cx + w))                # 例 w=2 → 14,15,16,17
    if depth >= 4:                                 # 大喜び: 横線 + 下に1段の塊（開いた口）
        return {(c, cy) for c in L} | {(c, cy + 1) for c in L[1:-1]}
    if depth == 3:                                 # 笑顔: 横線（やや広い）
        return {(c, cy) for c in range(cx - w - 1, cx + w + 1)}
    if depth == 2:                                 # にっこり: 横線
        return {(c, cy) for c in L}
    if depth == 1:                                 # ほぼ平: 短め
        return {(c, cy) for c in L[1:-1]}
    if depth == 0:                                 # 平: ちょん（2点）
        return {(cx - 1, cy), (cx, cy)}
    # 悲しみ: 横線を1段下げて短く（端は上げない＝目に見せない）
    return {(cx - 1, cy + 1), (cx, cy + 1)}


def eye_cells(style, size, cx, cy, openness, closed_happy=True):
    """可愛さの型 v1（2026-08-18 選好データより）:
    - 当たりは全員「丸目 or ジト目」。たれ目/つり目は口と干渉して顔が読めなくなるので廃止
    - 目は必ず 2x2 以上の塊（点1つだと模様と区別がつかない）
    - 閉じ目は2種類（2026-08-26）: 嬉しい時＝＾アーチ／悲しい・眠い時＝平らな線
      （しょんぼり中に＾が出ると「ちょこちょこニコッ」に見えるバグの対策）"""
    big = (size == "big")
    if openness <= 0:
        if closed_happy:                         # 嬉しい閉じ目（＾）
            return {(cx - 1, cy), (cx, cy - 1), (cx + 1, cy)}
        return rect(cx - 1, cx + 1, cy, cy)      # 悲しい/眠い閉じ目（−）
    if style == 3:                               # ジト目（横長・眠そう）
        e = rect(cx - 1, cx + 1, cy, cy + (1 if big else 0))
        return e
    # style 0/1/2 はすべて丸目に統一（大きさだけ違う）
    if openness == 2:
        return rect(cx - 1, cx, cy - 1, cy) if not big else disc(cx, cy, 1.6)   # 抜き無しのベタ（Dは廃止）
    return rect(cx - 1, cx, cy, cy)              # 半目
    e = rect(cx - 1, cx + 1, cy, cy)             # ジト目
    if big:
        e |= rect(cx - 2, cx + 2, cy, cy)
    return e


def expression(face: dict, M: float, N: float):
    mi, ni = q(M, M_TH), q(N, N_TH)
    depth = max(-1, min(4, [4, 3, 2, 0, -1][mi] + face["smile_bias"]))
    openness = [2, 2, 1, 1, 0][ni]
    sink = [0, 0, 1, 1, 2][ni]
    ey = face["eye_y"] + sink
    ec_l = round(15.5 - face["eye_gap"])
    eyes = eye_cells(face["eye_style"], face["eye_size"], ec_l, ey, openness)
    eyes |= mirror(eyes)
    eye_bottom = ey + (1 if face["eye_size"] == "big" else 0)
    mouth_y = max(face["mouth_y"], eye_bottom + 3)          # 目と口は最低2行あける（接触＝きもい）
    mouth = umouth(depth, 16, mouth_y, face["mouth_w"])
    cheeks = set()
    if face.get("cheeks") and mi <= 2:
        ch = {(ec_l - 2, ey + 3), (ec_l - 3, ey + 3)}
        cheeks = ch | mirror(ch)
    return eyes | mouth, cheeks


# ================= 3. 描画 =================

def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _dim(rgb, M):
    """散らかると色がくすむ（彩度・明度を落とす）"""
    h, l, s = colorsys.rgb_to_hls(*[v / 255 for v in rgb])
    l = max(0.10, l - 0.10 * M)
    s = max(0.10, s - 0.30 * M)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(r * 255), int(g * 255), int(b * 255))


def render(sheet: dict, M: float, N: float, path: str, scale=7, margin=8):
    from PIL import Image, ImageDraw
    body_col = _dim(_hex(sheet["colors"]["body"]), M)
    pat_col = _dim(_hex(sheet["colors"]["pattern"]), M)
    acc_col = _dim(_hex(sheet["colors"]["accent"]), M)
    h, l, s = colorsys.rgb_to_hls(*[v / 255 for v in _hex(sheet["colors"]["body"])])
    ink = tuple(int(v * 255) for v in colorsys.hls_to_rgb(h, 0.13, min(0.35, s)))
    cheek = tuple(int(v * 255) for v in colorsys.hls_to_rgb((h + 0.11) % 1, 0.62, 0.55))

    body_cells = {(c, r) for r, row in enumerate(sheet["art"])
                  for c, ch in enumerate(row) if ch in "BP"}   # 顔は体+模様の上（最前面）
    face, cheeks = expression(sheet["face"], M, N)
    face &= body_cells                            # 体の上にだけ顔が乗る
    cheeks = (cheeks & body_cells) - face

    size = GRID * scale + margin * 2
    img = Image.new("RGB", (size, size), (13, 15, 20))
    d = ImageDraw.Draw(img)
    colmap = {"B": body_col, "P": pat_col, "A": acc_col}
    for r, row in enumerate(sheet["art"]):
        for c, ch in enumerate(row):
            if ch in colmap:
                d.rectangle([margin + c * scale, margin + r * scale,
                             margin + (c + 1) * scale - 1, margin + (r + 1) * scale - 1],
                            fill=colmap[ch])
    for cells, col in [(cheeks, cheek), (face, ink)]:
        for c, r in cells:
            d.rectangle([margin + c * scale, margin + r * scale,
                         margin + (c + 1) * scale - 1, margin + (r + 1) * scale - 1], fill=col)
    img.save(path)


def gallery(ids, states, path="spirits_gallery.png", scale=7, margin=8):
    from PIL import Image
    import os
    size = GRID * scale + margin * 2
    sheet_img = Image.new("RGB", (size * len(ids), size * len(states)), (13, 15, 20))
    for x, pid in enumerate(ids):
        sh = generate(pid)
        for y, (m, n) in enumerate(states):
            render(sh, m, n, "_tmp_cell.png")
            sheet_img.paste(Image.open("_tmp_cell.png"), (x * size, y * size))
    sheet_img.save(path)
    os.remove("_tmp_cell.png")
    print(f"→ {path}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        sys.exit(0)
    if args[0] == "--gallery":
        ids = args[1:] or ["kuwahara"]
        gallery(ids, [(0.1, 0.1), (0.6, 0.4), (0.9, 0.9)])
        sys.exit(0)
    pid = args[0]
    sh = generate(pid, force="--force" in args)
    print(f"{sh['name']}（{sh['species']}）: {sh['personality']}")
    if "--render" in args:
        i = args.index("--render")
        M = float(args[i + 1]) if len(args) > i + 1 else 0.1
        N = float(args[i + 2]) if len(args) > i + 2 else 0.1
        render(sh, M, N, f"spirit_{pid}.png")
        print(f"→ spirit_{pid}.png")
