# -*- coding: utf-8 -*-
"""Tapoの映像をクラウドの地霊へ橋渡しする（2026-09-01 / 2026-09-02改訂）。

Tapoは家のネットワークの中にいるので、クラウドから直接は取りに行けない。
ラズパイが中に立って、静止画を1枚ずつ取り出して送る。

送り先は今までと同じ /spirit/frame。クラウド側は写真1枚で
「人が居るか・誰か・散らかり具合」をすべて判断する（2026-08-31の統合）ので、
橋渡しは撮って送るだけでよい。

■ 見張りと判断を分ける
  30秒おきに1枚ずつ送っていた頃は、人が入ってきても最悪30秒気づかなかった。
  かといって短くすると、誰も居ない台所を何百回もクラウドに見せることになる。

  そこでラズパイ自身がずっと映像を見張る。ごく小さな白黒（80x45）で流し、
  前のコマとの差を測る。差が出た＝何かが動いた時だけクラウドへ送る。
  足し算と引き算だけなので外部の部品も要らず、CPUもほとんど食わない。

■ 映像は開いたままにする
  写真が要るたびにffmpegを起こしていたが、1枚あたり2.8秒かかっていた。
  中身は接続の手続きで、解像度を落としても縮まらない（主2.87秒／副2.78秒）。
  そこで同じ1本の接続から、見張り用の小さな白黒と、送る用のJPEGを
  同時に出しつづける。写真が要るときはできあがったものを読むだけになる。

■ 見張りは別の流れで回す
  首を振って人を探している間、見張りの読み取りが止まると、映像の通り道が
  詰まってffmpegごと止まる。そうなると肝心の写真も更新されなくなる。
  読み取りだけを別の流れに分け、何があっても映像を流し続ける。

■ 動いている間は大きいほうを送る（2026-09-05）
  保存写真68枚を数え直したところ、顔の幅は1280幅で87〜150px、
  2304幅で100〜222pxだった。新しい人を覚えられる線は120pxなので、
  普段の1枚では3件中2件が届かない。主ストリームを基準コマだけ開いて
  流しつづけ、人が動いている間はそちらを送る。
"""
import http.client
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

try:                                   # 手元では bridge/ の下、ラズパイでは同じ場所
    from bridge import sweep
    from bridge import shot_check      # 見回りの1枚が使えるかを測る（2026-09-10）
except ImportError:
    import sweep
    import shot_check

CAM_URL = os.environ.get("TAPO_URL", "rtsp://thankU:39Kitchen@192.168.0.230:554/stream1")
SERVER = os.environ.get("SPIRIT_SERVER", "https://arigato-3ipecjbnha-an.a.run.app")
KEY = os.environ.get("SPIRIT_KEY", "")   # 鍵はリポジトリに置かない（9/16入れ替え）。ラズパイは ~/spirit_brain/spirit.env
SHOT = "/tmp/tapo.jpg"          # 常に最新の1枚が置かれる（ffmpegが書き替えつづける）

# 見張りと定時報告は副ストリーム（1280x720）から取る。
# 主ストリーム（2304x1296）は全コマを開くとCPUを1コア食い切るが、
# 基準コマだけを開けば負荷はほぼ変わらない（hires_stream を参照）。
# 顔の幅は1280幅で87〜150px、2304幅で100〜222px（9/5の実測）。
WATCH_URL = os.environ.get("TAPO_WATCH_URL", CAM_URL.replace("/stream1", "/stream2"))

WATCH_W, WATCH_H, WATCH_FPS = 80, 45, 2      # 見張り用の小さな白黒
FRAME_BYTES = WATCH_W * WATCH_H
SHOT_FPS = 1                                 # 送る用のJPEGを作り替える速さ
SHOT_MAX_AGE = 6.0                           # これより古い1枚は使わない
HIRES_SHOT = "/tmp/tapo_hi.jpg"              # 大きい1枚（2304x1296）が常に置かれる
HIRES_MAX_AGE = 10.0                         # 見回りなど、急がない場面での上限
# 人が動いている間は、これより古い大きい1枚は使わない。
# 大きい写真は基準コマだけを開いて作るので2〜4秒に1枚しか更新されない。
# 10秒前まで許していた頃は、動きを見つけた瞬間に「人が写る前の景色」を
# 送りかねなかった。通り過ぎる人には致命的なので、古ければ小さいほう（1秒ごとに
# 更新される）の新しい1枚を送る。
HIRES_FRESH = 3.5
HIRES_GAP = 8.0                              # 撮り直しを頼まれたときの最短間隔
HIRES_STALE = 30.0                           # 大きい1枚がこれだけ古ければ、流れが止まっているとみなす
HIRES_REVIVE_GAP = 20.0                      # 起こし直しの最短間隔（起動に数秒かかるので）
_hires = [None, 0.0]                         # いまの大きい流れと、最後に起こし直した時刻
# 写真が古いままこれだけ続いたら、映像ごと繋ぎ直す。
# 見張りの映像だけが流れつづけ、写真の書き出しだけが止まることがある。
# その状態は「映像が切れた」と判定されないので、放っておくと目が閉じたまま
# 動いているように見える（実際に5時間気づけなかった）。
SHOT_STALE_LIMIT = 90.0

GAP_BUSY = 1.5        # 動きがある間、クラウドへ送る最短間隔（9/16に3.0→1.5。実測の間隔は中央3.8秒・平均4.7秒だった）
GAP_ARRIVE = 1.5      # 動き始めの最初のうちは、もっと細かく送る（2026-09-10）
ARRIVE_SEC = 30.0     # その「最初のうち」の長さ。入ってくる人の正面は1回の入室で3〜4コマしか無く、
                      # 3秒に1枚だと半分がクラウドに届かなかった（実測）。登録は25秒に2回要る
GAP_HEARTBEAT = 300.0 # 何も起きなくても、これだけ経ったら1枚送る（定時報告）
GAP_ERROR = 15.0      # 失敗した時
AWAY_MAX = 60.0       # 人が居ても、定位置から離れたままにするのはこの秒数まで
_away = [0.0]         # 定位置から離れた時刻（戻れば0）
PIR_GAP = 3.0         # C3の人感（クラウド経由）を覗きにいく間隔（2026-09-21）
PIR_HOLD = 20.0       # 人感が「居る」と言った直後、これだけは居るものとして送り続ける
_pir = [0.0, 0.0]     # [最後に「居る」と聞いた時刻, 最後に覗いた時刻]
HINT_GAP = 3.0        # 「探しに行け」の札を覗きにいく間隔
SWEEP_COOLDOWN = 600.0 # 一度探したら、しばらくは探し直さない
                       # 90秒だった頃は1日79回も首を振り、そのたびに景色が
                       # 変わって前後の比較が壊れた
POSE_GAP = 30.0        # カメラの向きを確かめにいく間隔
STILL_HOLD = 20.0     # 最後に動いてからこの秒数は「まだ居る」とみなす
CALIB_FRAMES = 20     # 最初のこの枚数で、その部屋の「静かさ」を測る
SETTLE_FRAMES = 8     # 首を振った直後、揺れが収まるまで捨てるコマ数
SETTLE_AFTER_MOVE = 4.0  # 見回りで振ったあと、映像が入れ替わるのを待つ秒数
CHECK_TRIES = 8          # 使える写真が撮れるまで撮り直す回数（1回10秒ほど）
RECHECK_GAP = 300.0      # 撮れなかったとき、次に見に行くまでの間

# 画角を探し直す（2026-09-12 夜・カメラが落ちた）。
# 同じ向きの数値を送っても、台座が動いていれば別の場所を写す。
# 見本（sink_ref.jpg）と一番よく合う向きを、首を少し振って探し、
# 見つかった差を覚えて以後はそれを足す。
AIM_STEPS = (0.04, 0.02)  # まず粗く、次に細かく
AIM_MAX_TRIES = 16       # 首を振る回数の上限（1回 SETTLE_AFTER_MOVE 秒＋撮影）
AIM_GOOD_PX = 60         # ここまで合えば探すのをやめる
AIM_GAP = 1800.0         # 探し直すのは30分に1回まで（首を振るほど景色が揺れる）
AIM_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aim_fix.json")
_aim_fix = [0.0, 0.0]    # 覚えた補正。看る向きに足す
_aim_last = [0.0]        # 最後に探した時刻


def load_aim_fix() -> None:
    """覚えた補正を読む。無ければ 0。"""
    try:
        with open(AIM_FIX, encoding="utf-8") as f:
            d = json.load(f)
        _aim_fix[0], _aim_fix[1] = float(d.get("dx", 0.0)), float(d.get("dy", 0.0))
        if _aim_fix[0] or _aim_fix[1]:
            print("画角の補正を読んだ: %+.2f / %+.2f" % tuple(_aim_fix), flush=True)
    except Exception:
        pass


def save_aim_fix() -> None:
    try:
        with open(AIM_FIX, "w", encoding="utf-8") as f:
            json.dump({"dx": _aim_fix[0], "dy": _aim_fix[1], "at": time.time()}, f)
    except Exception as e:
        print("画角の補正を書けない:", e, flush=True)


def aimed(x: float, y: float) -> tuple:
    """覚えた補正を足した向き。カメラの動く範囲からは出さない。"""
    return (max(-1.0, min(1.0, x + _aim_fix[0])),
            max(-1.0, min(1.0, y + _aim_fix[1])))


def _aim_score(x: float, y: float) -> tuple:
    """その向きへ首を振って1枚撮り、見本からのずれを測る。(ずれ, dx, dy)。"""
    sweep.look(x, y)
    time.sleep(SETTLE_AFTER_MOVE)
    jpg = grab()
    if jpg is None:
        return 10 ** 6, None, None
    dx, dy, _resp = shot_check.shift_of(jpg)
    if dx is None:
        return 10 ** 6, None, None
    return max(abs(dx), abs(dy)), dx, dy


AIM_AUTO = os.environ.get("AIM_AUTO", "0") == "1"   # 自動の向き合わせ（既定は止める・2026-09-22）


def realign(x: float, y: float) -> bool:
    """見本と一番よく合う向きを探して、差を覚える。

    ※2026-09-22 から既定で止めている（AIM_AUTO=1 で動く）。13:11 に C3 を移した際に
    カメラの台が回り、見本と今の景色が別物になった。位置合わせはずれの数字だけを見て
    確かさを見ないので、無関係な景色どうしでも小さいずれが出て、補正が
    +0.04→+0.08→+0.12→+0.16 と壁の方へ歩き続け、16:25 から「シンクの向き」が
    壁を写していた。見本に確かさの守りを入れるまで、人が手で向きを決める。

    9/12 の夜にカメラが落ちた。戻したつもりでも同じ画角には戻らず、
    シンクの切り出し（画面の割合で決め打ち）がシンクから外れると、
    「空です」を別の場所について答えてしまう。人が気づくのを待たずに、
    ここで探し直す。

    良くなる方向へ 0.04 刻みで進み、動けなくなったら 0.02 刻みに落とす。
    首を振る回数は16回まで。振るほど景色が揺れるので、30分に1回までにしてある。"""
    if not AIM_AUTO:
        return False
    now = time.time()
    if now - _aim_last[0] < AIM_GAP:
        return False
    _aim_last[0] = now
    base = aimed(x, y)
    best, bdx, bdy = _aim_score(*base)
    bx, by = base
    print(time.strftime("%H:%M:%S"),
          "画角を探し直す。いま ずれ=%s (%s,%s)" % (best, bdx, bdy), flush=True)
    if best <= AIM_GOOD_PX:
        return False                    # ずれていない。触らない
    # 坂を下る。良くなるあいだは同じ刻みで動き続け、動けなくなったら刻みを細かくする。
    # 首を振る回数は AIM_MAX_TRIES で止める（振るほど景色が揺れて比較が壊れる）。
    tries = 1
    for s in AIM_STEPS:
        while tries < AIM_MAX_TRIES and best > AIM_GOOD_PX:
            moved = False
            for ddx, ddy in ((s, 0), (-s, 0), (0, s), (0, -s)):
                if tries >= AIM_MAX_TRIES:
                    break
                cx = max(-1.0, min(1.0, bx + ddx))
                cy = max(-1.0, min(1.0, by + ddy))
                if (cx, cy) == (bx, by):
                    continue                      # 動く範囲の端
                off, dx, dy = _aim_score(cx, cy)
                tries += 1
                print("   %+.2f/%+.2f → ずれ=%s" % (cx, cy, off), flush=True)
                if off < best:
                    best, bx, by, bdx, bdy = off, cx, cy, dx, dy
                    moved = True
                    break                         # 良くなった方向へ、そのまま進む
            if not moved:
                break                             # この刻みでは、もう良くならない
        if best <= AIM_GOOD_PX:
            break
    if best >= 10 ** 6:
        print("画角を探せなかった（映像が無い）", flush=True)
        return False
    if best > shot_check.SHIFT_MAX:
        print("画角が見つからない。台座ごと動いた可能性。ずれ=%s" % best, flush=True)
        return False
    _aim_fix[0] = max(-0.2, min(0.2, _aim_fix[0] + (bx - base[0])))
    _aim_fix[1] = max(-0.2, min(0.2, _aim_fix[1] + (by - base[1])))
    save_aim_fix()
    print(time.strftime("%H:%M:%S"),
          "画角が合った。補正 %+.2f / %+.2f （ずれ=%s）" % (_aim_fix[0], _aim_fix[1], best),
          flush=True)
    return True
MAX_QUIET = 8.0          # 「静かさ」がこれを超えたら測り直す（動いている最中の値）


def watch_stream():
    """1本の接続から2つを同時に出す。

    ひとつは見張り用の小さな白黒（標準出力へ流しっぱなし）。
    もうひとつは送る用のJPEG（同じ名前を上書きしつづける）。
    カメラは梁に逆さに吊ってあるので、JPEGはここで180度回しておく。
    見張り用は回さない（差を測るだけなので向きは関係ない）。"""
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", WATCH_URL,
         "-an", "-vf", "fps=%d,scale=%d:%d,format=gray" % (WATCH_FPS, WATCH_W, WATCH_H),
         "-f", "rawvideo", "pipe:1",
         "-an", "-vf", "fps=%d,hflip,vflip" % SHOT_FPS, "-q:v", "3",
         "-update", "1", "-y", SHOT],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def hires_stream():
    """主ストリーム（2304x1296）から、大きい1枚を作り替えつづける。

    以前は顔が要るたびにffmpegを起こしていたが、接続の手続きだけで2.9秒かかり、
    その間に人が通り過ぎていた。かといって流しっぱなしにするとラズパイの
    CPUを1コア食い切る——と思われていたが、それは全コマを開いていたから。

    `-skip_frame nokey` を付けると、飛び飛びの基準コマ（2〜4秒に1枚）だけを
    開いて、あいだのコマは開かずに捨てる。実測では負荷平均が 0.42 のまま
    変わらなかった（2026-09-05）。これで大きい1枚がいつでも手元にある。"""
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp",
         "-skip_frame", "nokey", "-i", CAM_URL,
         "-an", "-vsync", "0", "-vf", "hflip,vflip", "-q:v", "3",
         "-update", "1", "-y", HIRES_SHOT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def diff(a: bytes, b: bytes) -> float:
    """2コマの違い。0なら全く同じ、大きいほど何かが動いた。"""
    total = 0
    for i in range(0, FRAME_BYTES, 3):        # 3画素に1つ見れば十分（軽くする）
        d = a[i] - b[i]
        total += d if d >= 0 else -d
    return total / (FRAME_BYTES / 3.0)


class Watcher(threading.Thread):
    """映像をひたすら読み、動きがあった時刻だけを外に伝える。

    読み取りを止めないことが何より大事。止めると通り道が詰まり、
    写真を書き出しているffmpegごと巻き添えで止まる。"""

    def __init__(self, proc):
        super().__init__(daemon=True)
        self.proc = proc
        self.last_move = -1e9
        self.ready = False
        self.alive = True
        self.reset = False          # 首を振った直後は基準を取り直す

    def run(self):
        prev = None
        calib, quiet, settle = [], 0.0, SETTLE_FRAMES
        while True:
            buf = self.proc.stdout.read(FRAME_BYTES)
            if not buf or len(buf) < FRAME_BYTES:
                self.alive = False
                return
            if self.reset:                       # 向きが変わった＝別の景色
                self.reset = False
                prev, calib, quiet = None, [], 0.0
                settle = SETTLE_FRAMES
                self.ready = False
            if settle > 0:
                # 止まった直後はまだ首が揺れている。ここを基準に混ぜると
                # 「静かさ」が跳ね上がり（実測0.45→3.98）、本物の人を
                # 見逃すしきい値になってしまう。収まるまで数えない。
                settle -= 1
                prev = buf
                continue
            if prev is not None:
                d = diff(prev, buf)
                if len(calib) < CALIB_FRAMES:    # 最初は黙って基準を測る
                    calib.append(d)
                    if len(calib) == CALIB_FRAMES:
                        # 一番大きい値を基準にしていた頃は、測っている
                        # 10秒のうちに一度でも映像が乱れると、その1回だけで
                        # しきい値が固定された（実測 2.29 → 17.57）。
                        # 上から2番目を使えば、その一発に引きずられない。
                        quiet = sorted(calib)[-2]
                        if quiet > MAX_QUIET:
                            # この部屋の静けさは実測で0.45前後。これを大きく
                            # 超えるのは、測っている間じゅう景色が動いていた
                            # ということ（首振りの途中など）。採用すると
                            # しきい値が跳ね上がり、以後どんな人も通らなくなる
                            # （実測でしきい値167.67になり見張りが死んだ）。
                            # 黙って測り直す。
                            print("揺らぎ %.2f は大きすぎる → 測り直す" % quiet,
                                  flush=True)
                            calib = []
                            settle = SETTLE_FRAMES     # 収まるまでもう一度捨てる
                            prev = buf
                            continue
                        self.ready = True
                        print("静かな時の揺らぎ = %.2f / しきい値 = %.2f"
                              % (quiet, max(quiet * 2.5, 1.5)), flush=True)
                elif d > max(quiet * 2.5, 1.5):
                    now = time.time()
                    if now - self.last_move >= STILL_HOLD:   # 静けさが破られた瞬間
                        print(time.strftime("%H:%M:%S"),
                              "動きあり %.2f" % d, flush=True)
                    self.last_move = now
            prev = buf


def _read_fresh(path: str, max_age: float) -> bytes | None:
    """できあがっている1枚を読む。古ければNone。書きかけも捨てる。"""
    try:
        age = time.time() - os.path.getmtime(path)
        if age > max_age:
            return None                       # 映像が止まっている
        with open(path, "rb") as f:
            data = f.read()
        _read_age[0] = age                    # 送る1枚が何秒前のものか（計測用）
        return data if data[-2:] == b"\xff\xd9" else None
    except Exception as e:
        print("grab failed:", path, e, flush=True)
        return None


def grab() -> bytes | None:
    """副ストリーム（1280x720）の最新の1枚。"""
    return _read_fresh(SHOT, SHOT_MAX_AGE)


def grab_big(max_age: float = HIRES_MAX_AGE) -> bytes | None:
    """主ストリーム（2304x1296）の最新の1枚。無ければNone。

    顔の幅は1280幅で87〜150px、2304幅で100〜222px（9/5の実測）。
    新しい人を覚えられる線は120pxなので、人が動いている間は
    こちらを送る。誰も居ない定時報告では副ストリームで足りる。"""
    return _read_fresh(HIRES_SHOT, max_age)


TIMING = os.environ.get("BRIDGE_TIMING", "1") == "1"   # 送信の内訳を1枚ごとに記録に出す（9/15計測）
_read_age = [0.0]      # 最後に読んだ1枚が何秒前のものか
_last_end = [0.0]      # 前の送信が終わった時刻
_conn = [None]         # 使い回すクラウドへの接続

_pose = [""]           # いまカメラが向いている先。写真に添えて送る
_home = ["%.2f_%.2f" % sweep.HOME]      # 待機位置。クラウドから読み直せる
_stay = [False]        # 定位置へ戻すのを止めているか


def refresh_home() -> None:
    """待機位置をクラウドから読む。

    カメラを動かすたびにコードを書き直さずに済むよう、置き場所を外に出した。
    キッチンでスマホから決められる。

    返事は「向き」または「向き paused」。paused の間は定位置へ戻さない。
    設置中に戻されると、向きを決めて押す前にカメラが逃げる。"""
    try:
        with urllib.request.urlopen(SERVER + "/spirit/home", timeout=5) as r:
            v = r.read().decode().strip()
        parts = v.split()
        _stay[0] = "paused" in parts
        v = parts[0] if parts and parts[0] != "paused" else ""
        if v and v.count("_") == 1:
            _home[0] = v
            x, y = (float(a) for a in v.split("_"))
            sweep.HOME = (x, y)
    except Exception:
        pass


def at_home() -> bool:
    """いま定位置を向いているか。

    文字が一致するかで見ていた頃は、カメラが返す値が -0.29 と -0.30 の
    間で僅かに揺れるだけで「ずれている」と判定され、30秒ごとに首を
    動かし直していた。そのたび景色が揺れて、物の前後比較が壊れる。
    近ければ同じ向きとみなす。"""
    try:
        a = [float(v) for v in _pose[0].split("_")]
        b = [float(v) for v in _home[0].split("_")]
    except ValueError:
        return False
    return abs(a[0] - b[0]) < 0.05 and abs(a[1] - b[1]) < 0.05


def refresh_pose() -> None:
    """カメラの向きを確かめて覚える。

    向きが変わった前後を比べると、何も起きていなくても全部変わって見える。
    こちらが首を振った時だけでなく、アプリから動かされることもあるので、
    自分の記憶ではなくカメラ本体に聞く。
    動いている最中の値を掴むと、別の向きの2枚を同じ向きとして比べてしまうので、
    止まるまで待ってから聞く。"""
    w = sweep.settled()
    if w:
        _pose[0] = "%.2f_%.2f" % w


def send(jpg: bytes, big: bool = False, check: bool = False) -> dict:
    """クラウドへ送って判断を受け取る。写真には向きを添える。

    big=True は「これはもう大きく撮り直した1枚」の印。これ以上大きくは
    撮れないので、クラウドに同じ頼みを繰り返させない。"""
    url = (SERVER + "/spirit/frame?pose=" + _pose[0]
           + ("&big=1" if big else "") + ("&check=1" if check else ""))
    if not TIMING:
        req = urllib.request.Request(
            url, data=jpg,
            headers={"Content-Type": "image/jpeg", "X-Upload-Key": KEY})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode())

    # 1枚にかかる時間の内訳（2026-09-15）。設定3秒に対し実測6.45秒（9/15の465回）。
    # クラウドの ms は写真を受け取り終わってから数えるので、送る時間が入っていない。
    # 繋ぐ／送り終える／返事の頭が来る／読み終える、に分けてラズパイ側で測る。
    #
    # 繋ぐのに毎回 0.19秒（中央）〜0.75秒（9割）かかっていた（9/15の2716回）。
    # 同じ接続を使い回し、切れていたら1回だけ繋ぎ直す（2026-09-16）。
    # 送るのはすべて main の流れからなので、接続は1本で足りる。
    u = urllib.parse.urlsplit(url)
    t0 = time.time()
    for attempt in (1, 2):
        conn = _conn[0]
        try:
            if conn is None:
                conn = _conn[0] = http.client.HTTPSConnection(u.hostname, timeout=40)
                conn.connect()
            t1 = time.time()
            conn.request("POST", u.path + "?" + u.query, body=jpg,
                         headers={"Content-Type": "image/jpeg", "X-Upload-Key": KEY})
            t2 = time.time()
            r = conn.getresponse()
            t3 = time.time()
            body = r.read()
            t4 = time.time()
            break
        except (http.client.HTTPException, OSError):
            try:
                conn.close()
            except Exception:
                pass
            _conn[0] = None
            if attempt == 2:
                raise
    res = json.loads(body.decode())
    srv = sum((res.get("ms") or {}).values())
    idle = t0 - _last_end[0] if _last_end[0] else -1.0
    _last_end[0] = t4
    print(time.strftime("%H:%M:%S"),
          "計測 前の送信から=%.2f 写真の古さ=%.2f %dKB 繋ぐ=%.2f 送る=%.2f 返事待ち=%.2f"
          " 読む=%.2f 計=%.2f クラウド内=%.2f 向き=%s %s"
          % (idle, _read_age[0], len(jpg) // 1024, t1 - t0, t2 - t1, t3 - t2,
             t4 - t3, t4 - t0, srv / 1000, _pose[0] or "?", res.get("why")), flush=True)
    return res


def pir_here() -> bool:
    """C3の人感が「人が居る」と言っているか（クラウド経由・2026-09-21）。

    橋渡しは 80x45 の粗い白黒で動きを見ているが、人はフレームのごく一部しか
    占めないので、実測では人の動きが 1.5〜2.9（基準1.50）しか出ない。
    9/21 の来訪（12:47〜12:55）は一度も検知できず、写真が0枚だった。
    C3 の人感は人を捉えているので、そちらが「居る」と言う間は動きに関係なく送る。"""
    now = time.time()
    if now - _pir[1] >= PIR_GAP:
        _pir[1] = now
        try:
            with urllib.request.urlopen(SERVER + "/spirit/presence", timeout=5) as r:
                if r.read().decode().strip() == "occupied":
                    _pir[0] = now
        except Exception as e:
            print("人感の確認に失敗:", e, flush=True)
    return time.time() - _pir[0] < PIR_HOLD


def hint() -> str:
    """クラウドに立った札を覗く。数バイトしか返らない。"""
    try:
        with urllib.request.urlopen(SERVER + "/spirit/hint", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


def checked() -> None:
    """見回りが済んだと伝える。"""
    try:
        req = urllib.request.Request(SERVER + "/spirit/checked",
                                     data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("checked failed:", e, flush=True)


_recheck = [0.0, ""]   # 使える写真が撮れなかったとき、いつ・どこを見直すか


def _next_frame(prev: bytes | None, after: float = 0.0) -> bytes | None:
    """いまの1枚より新しい1枚を待って読む（最大6秒）。

    大きい流れは2〜4秒に1枚しか更新されない。続けて2回読むと同じ1枚が
    返り、「止まっている」と誤って判定するので、更新を待つ。

    after を渡すと、その時刻より後に書かれた1枚だけを使う（2026-09-22）。
    9/22 18:13、大きい流れが止まっていたため、首を振る前の部屋側の1枚
    （しかも小さい方）が「シンクの写真」として送られ、見本との比較が
    336px ずれて止まった。首を振ったあとの1枚が無ければ、何も返さない。"""
    t0 = time.time()
    while time.time() - t0 < 6.0:
        jpg = grab_big(HIRES_MAX_AGE)
        fresh = True
        if after:
            try:
                fresh = os.path.getmtime(HIRES_SHOT) > after
            except OSError:
                fresh = False
        if jpg is not None and jpg != prev and fresh:
            return jpg
        time.sleep(0.3)
    if after:
        return None                     # 首を振ったあとの大きい1枚が無い＝撮り直す
    return grab_big(HIRES_MAX_AGE) or grab()


def take_good_shot(x: float, y: float) -> bytes | None:
    """使える1枚が撮れるまで撮り直す（2026-09-10・本人の方針「比べないことはしない」）。

    「使える」＝ぶれていない・定位置の基準写真と位置が合っている・1枚前と同じ景色。
    今朝09:18、首を振っている最中のぶれた1枚が「前」として保存され、その後の
    見回りが全部「ずれている」で比べられなかった。原因はこの関数の前身が、
    4秒待っただけで最大10秒古い1枚をそのまま送っていたこと。

    ずれていれば向け直し、ぶれ・動きなら待って撮り直す。CHECK_TRIES 回で
    だめなら None（送らない。RECHECK_GAP 後にもう一度来る）。"""
    prev = None
    off = 0                            # ずれで撮り直した回数
    moved_at = time.time()             # 首を振り終えた時刻。これより前の1枚は使わない
    for i in range(1, CHECK_TRIES + 1):
        time.sleep(SETTLE_AFTER_MOVE if i == 1 else 1.0)
        jpg = _next_frame(prev, moved_at + SETTLE_AFTER_MOVE)
        if jpg is None:
            print(time.strftime("%H:%M:%S"), "見回りの写真 %d回目: 映像が無い" % i, flush=True)
            continue
        r = shot_check.check(jpg, prev)
        print(time.strftime("%H:%M:%S"),
              "見回りの写真 %d回目: ぶれ=%.1f ずれ=%s 動き=%s → %s"
              % (i, r["blur"], r["shift"], r["still"], "使える" if r["ok"] else r["why"]),
              flush=True)
        if r["ok"] and prev is not None:
            return jpg                 # 1枚前と同じ景色で、ぶれもずれもない
        prev = jpg
        if not r["ok"] and r["why"] == "ずれ":
            off += 1
            sweep.look(*aimed(x, y))   # 向け直してから撮り直す
            moved_at = time.time()
            prev = None
    # 何度振り直してもずれたまま＝カメラが動いている。画角を探し直す（30分に1回）
    if off >= 2 and realign(x, y):
        moved_at = time.time()
        for i in range(1, 3):
            time.sleep(SETTLE_AFTER_MOVE if i == 1 else 1.0)
            jpg = _next_frame(prev, moved_at + SETTLE_AFTER_MOVE)
            if jpg is None:
                continue
            r = shot_check.check(jpg, prev)
            if r["ok"] and prev is not None:
                print(time.strftime("%H:%M:%S"), "探し直したあとで撮れた", flush=True)
                return jpg
            prev = jpg
    return None


def go_check(pose: str, w) -> None:
    """キッチンを見に行って、使える1枚を撮って、また戻る（2026-09-06／撮り直し 2026-09-10）。

    普段は入り口を向いて待っている。人は数秒で通り過ぎるので、鳴ってから
    振ったのでは顔に間に合わないため。ただし物の増減は入り口からは見えない。

    そこで、誰も居ないと分かってから見に行く。撮るのは使える1枚だけ。
    前後比較は「誰も居ないキッチンの2枚」どうしになるので成立する。
    往復で20秒ほど。この間に人が来たら入り口の顔は逃すが、人感は鳴るので
    「誰か来ていた」ことは残る。"""
    try:
        x, y = (float(v) for v in pose.split("_"))
    except ValueError:
        print("bad check pose:", pose, flush=True)
        checked()
        return
    print(time.strftime("%H:%M:%S"), "キッチンを見に行く", pose,
          ("補正 %+.2f/%+.2f" % tuple(_aim_fix)) if (_aim_fix[0] or _aim_fix[1]) else "",
          flush=True)
    _recheck[0] = 0.0
    if not sweep.look(*aimed(x, y)):      # 覚えた補正を足した向きへ
        checked()                      # 振れなかった。次の機会に回す
        return
    jpg = take_good_shot(x, y)
    if jpg is not None:
        _pose[0] = pose                # この1枚に添える向き
        report(jpg, "見回り", big=True, check=True)
    else:
        print(time.strftime("%H:%M:%S"),
              "使える写真が撮れなかった → %d秒後にもう一度" % RECHECK_GAP, flush=True)
        _recheck[0], _recheck[1] = time.time() + RECHECK_GAP, pose
    checked()
    sweep.go_home()                    # 入り口へ戻る（止まるまで待つ）
    refresh_pose()
    time.sleep(SETTLE_AFTER_MOVE)      # 映像が入れ替わるのを待ってから
    w.reset = True                     # 静かさを測り直す（ここで初めて）
    print(time.strftime("%H:%M:%S"), "入り口へ戻った", _pose[0], flush=True)


def hint_clear() -> None:
    """探し終わったら札を下ろす。"""
    try:
        req = urllib.request.Request(SERVER + "/spirit/hint/clear",
                                     data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def someone(res: dict) -> bool:
    """この返事は「人が居た」と言っているか。

    顔が小さすぎて誰か分からなかった時も、人が居ることは確かなので
    含める。ここを person だけで見ていた頃は、顔が小さいというだけで
    首振りが人の前を素通りして探し続けていた。"""
    return bool(res.get("person")) or res.get("why") == "face_too_small"


def has_person(jpg: bytes) -> bool:
    """この1枚に人が写っているかをクラウドに聞く。"""
    try:
        return someone(send(jpg))
    except Exception:
        return False


def report(jpg: bytes, why: str, big: bool = False, check: bool = False) -> dict:
    """1枚送って、意味のある返事だけ記録する。返事をそのまま返す。"""
    try:
        res = send(jpg, big, check)
    except Exception as e:
        print("send failed:", e, flush=True)
        return {}
    if res.get("person") or res.get("judged") or res.get("why") == "face_too_small":
        print(time.strftime("%H:%M:%S"), why, res, flush=True)
    return res


# ---- 呼び名を聞く（2026-09-17）----
# クラウドが写真の返事に ask_name=<ID> を付けてきたら、問いかけの声を C3 に鳴らさせ、
# 答えの数秒ぶんだけ Tapo のマイクの音を取ってクラウドへ送る。音はここにも残さない。
# C3 はふだん1分おきにしか声を取りに来ないので、無線の命令口（UDP 5006）に `mur`
# （いま1回だけ取りに来る）を送って、すぐ鳴らさせる。
C3_ADDR = (os.environ.get("C3_IP", "192.168.0.233"), 5006)
ASK_SPEAK_WAIT = 3.3   # クラウドは問いかけを3秒ためてから渡す（spirit_name の SPEAK_SLOW）
LISTEN_TOTAL = 16.0    # 音を取る長さ。映像の繋ぎに約3秒かかり、問いかけ（3秒）の後ろ半分も入る。
                       # 9/19 の実測：繋いでから音が出るまでに数秒かかることがある
                       # 9/21 21:23 の実機：mur を送ってから C3 が声を取りに来るまで約4.5秒かかり、
                       # 問いかけが鳴り終わってから録音の終わりまで数秒しか残らなかった（聞こえた字数0）。
                       # 12→16秒にして、答える時間を8秒ほど取る
ASK_REPEAT_GAP = 60.0  # 同じ人の問いかけを、続けて扱わない
ASK_ROUNDS = 5         # 聞き返しを含めて、聞いて送るのは最大この回数（打ち切りはクラウドが決める。これは保険）
ASK_SPEAK_SEC = 3.1    # 作り置きの問いかけ（ask_name_0/1）の長さ。3.0秒と3.08秒
_asked = {"pid": "", "at": 0.0}


def c3(cmd: str) -> None:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(cmd.encode(), C3_ADDR)
    finally:
        s.close()


# 声が途切れたら録音を終える（2026-09-22・本人「名前を聞いたあと、変な間がある」）。
# 16秒固定で録っていた頃は、答え終わってからも録音の終わりまで8〜13秒待っていた。
# キャラ自身の問いかけもマイクに入るので、「問いかけが鳴り終わるころ」までは数えない。
CHUNK_SEC = 0.1          # 大きさを見る単位
SPEECH_LEVEL = 60        # これより大きければ声（9/19：静か7〜10・1mの声200〜300）
SILENCE_LEVEL = 40       # これより小さければ静か
END_SILENCE = 1.2        # 答えのあと、これだけ静かなら締める
NO_ANSWER_SEC = 7.0      # 問いかけが鳴り終わってから、これだけ声が無ければ締める（答えなし）
C3_FETCH_SEC = 2.0       # mur を送ってから C3 が鳴らし始めるまでの見込み（9/21〜22 の実測 1〜3秒）


def _wav(pcm: bytes) -> bytes:
    import io
    import wave
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(pcm)
    w.close()
    return b.getvalue()


def _rms(chunk: bytes) -> int:
    import array
    import math
    d = array.array("h")
    d.frombytes(chunk[:len(chunk) // 2 * 2])
    return int(math.sqrt(sum(x * x for x in d) / len(d))) if d else 0


def listen(sec: float, speak_end: float = 0.0) -> bytes | None:
    """Tapo のマイクから、答えが終わるまで（最長 sec 秒）の WAV を取る。メモリの中だけ。

    speak_end＝キャラの声が鳴り終わる見込みの時刻。それまでの音は数えない（自分の声で締めないため）。
    そのあと声が始まり、END_SILENCE 秒静かになったら締める。声が NO_ANSWER_SEC 来なければ締める。"""
    step = int(16000 * 2 * CHUNK_SEC)
    try:
        p = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", WATCH_URL,
             "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "-t", str(sec),
             "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("listen failed:", e, flush=True)
        return None
    buf, spoke, quiet, why = bytearray(), False, 0.0, "上限"
    t_end = time.time() + sec + 15
    try:
        while time.time() < t_end:
            chunk = p.stdout.read(step)
            if not chunk:
                break
            buf += chunk
            now = time.time()
            if now < speak_end:
                continue                          # まだキャラが喋っている
            lv = _rms(chunk)
            if lv >= SPEECH_LEVEL:
                spoke, quiet = True, 0.0
            elif lv < SILENCE_LEVEL:
                quiet += CHUNK_SEC
            if spoke and quiet >= END_SILENCE:
                why = "答え終わり"
                break
            if not spoke and now - speak_end >= NO_ANSWER_SEC:
                why = "答えなし"
                break
    finally:
        try:
            p.kill()
        except Exception:
            pass
    print(time.strftime("%H:%M:%S"), "録音を締めた：%s（%.1f秒ぶん）" % (why, len(buf) / 32000), flush=True)
    return _wav(bytes(buf)) if len(buf) > 16000 else None


def _post(path: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(SERVER + path, data=data if data is not None else b"",
                                 headers={"Content-Type": "audio/wav", "X-Upload-Key": KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def ask_name(pid: str) -> None:
    now = time.time()
    if _asked["pid"] == pid and now - _asked["at"] < ASK_REPEAT_GAP:
        return
    _asked["pid"], _asked["at"] = pid, now
    print(time.strftime("%H:%M:%S"), "呼び名を聞く:", pid, flush=True)
    time.sleep(ASK_SPEAK_WAIT)          # 早く取りに行くと「まだ」で無音が返る
    c3("mur")
    speak_end = time.time() + C3_FETCH_SEC + ASK_SPEAK_SEC
    q = "?person=" + urllib.parse.quote(pid)
    # クラウドが聞き返す（「◯◯……で、あってる？」「もういっかい、いって？」）あいだは、
    # 鳴らして → 聞いて → 送る、をくり返す（2026-09-21）。回数はクラウドが打ち切る。
    for _ in range(ASK_ROUNDS):
        wav = listen(LISTEN_TOTAL, speak_end)
        if wav is None:
            print("呼び名：音が取れなかった", flush=True)
            return
        # 答えを受けたらすぐ「ん……」を鳴らす（9/22）。文字にして判断して声を作る約5秒を埋める
        try:
            if _post("/spirit/name/hmm" + q).get("say"):
                c3("mur")
        except Exception as e:
            print("呼び名：つなぎを鳴らせなかった", e, flush=True)
        try:
            res = _post("/spirit/name" + q, wav)
        except Exception as e:
            print("呼び名：送れなかった", e, flush=True)
            return
        print(time.strftime("%H:%M:%S"), "呼び名の返事:",
              {k: res.get(k) for k in ("ok", "why", "say", "listen")},
              "覚えた" if res.get("name") else "", flush=True)
        if not res.get("say"):
            return
        time.sleep(1.0)                  # クラウドは0.8秒ためてから渡す
        c3("mur")
        speak_end = time.time() + C3_FETCH_SEC + float(res.get("speak_sec") or 3.0)
        if not res.get("listen"):
            return


def _revive_hires() -> None:
    """大きい1枚の流れが死んでいたら、起こし直す（2026-09-19）。

    死んだか（poll）と、写真が更新されているか（mtime）の両方を見る。
    ffmpeg は生きたまま止まることがあるので、生死だけでは足りない。"""
    p = _hires[0]
    if p is None:
        return
    now = time.time()
    if now - _hires[1] < HIRES_REVIVE_GAP:
        return
    dead = p.poll() is not None
    try:
        stale = now - os.path.getmtime(HIRES_SHOT) > HIRES_STALE
    except OSError:
        stale = True
    if not (dead or stale):
        return
    _hires[1] = now
    print(time.strftime("%H:%M:%S"),
          "大きい映像が%s → 起こし直す" % ("死んでいた" if dead else "止まっていた"),
          flush=True)
    try:
        p.kill()
    except Exception:
        pass
    _hires[0] = hires_stream()


def main():
    print("tapo bridge start ->", SERVER, flush=True)
    while True:
        proc = watch_stream()
        big_proc = hires_stream()        # 大きい1枚を作り替えつづける別の流れ
        _hires[0], _hires[1] = big_proc, time.time()
        w = Watcher(proc)
        w.start()
        last_sent = last_hint = last_sweep = last_pose = last_hires = 0.0
        last_fresh = time.time()         # 最後に新しい写真を読めた時刻
        busy_since, was_busy = 0.0, False
        load_aim_fix()
        refresh_pose()
        refresh_home()
        try:
            while w.alive:
                time.sleep(0.5)
                now = time.time()

                # 人感が鳴っていたら、カメラの向きの外に人が居るということ。
                # 首を振って探しに行く。見張りは別の流れなので止まらない。
                if now - last_hint >= HINT_GAP:
                    last_hint = now
                    tag = hint()
                    if tag.startswith("check "):
                        go_check(tag.split(None, 1)[1].strip(), w)
                        continue
                    if _recheck[0] and now >= _recheck[0] and now - w.last_move > STILL_HOLD:
                        go_check(_recheck[1], w)   # さっき撮れなかった分をやり直す
                        continue
                    if now - last_sweep >= SWEEP_COOLDOWN and tag == "sweep":
                        last_sweep = now
                        hint_clear()
                        if sweep.search(grab, has_person):
                            w.last_move = now         # 見つけた＝人が居る
                        refresh_pose()                # 向きが変わった
                        w.reset = True                # 景色が変わったので測り直す
                        continue

                if now - last_pose >= POSE_GAP:      # アプリから動かされた分も拾う
                    last_pose = now
                    refresh_pose()
                    refresh_home()                   # 待機位置が変わっていたら拾う

                # 写真が古いままなら、映像を繋ぎ直す。
                # ここを見ていないと、目が閉じたまま何時間でも走りつづける。
                if grab() is not None:
                    last_fresh = now
                elif now - last_fresh > SHOT_STALE_LIMIT:
                    print(time.strftime("%H:%M:%S"),
                          "写真が %d 秒更新されていない → 映像を繋ぎ直す"
                          % (now - last_fresh), flush=True)
                    break

                if not w.ready:
                    continue
                busy = (now - w.last_move) < STILL_HOLD or pir_here()
                if busy and not was_busy:
                    busy_since = now             # 動き始めの時刻（入室の最初のうちを測る）
                was_busy = busy

                # 人を探しに行った先に留まったままだと、物の前後比較が成り立たない。
                # 落ち着いたら定位置へ戻す。比べられるのは同じ向きの2枚だけ。
                # 人が居る間も、定位置から長く離れたままにはしない（2026-09-22）。
                # 「在室の間は送り続ける」を入れてから、人が居るかぎり busy が続くようになり、
                # 起動した時の向きや人を探しに行った先から戻れなくなった。9/22 21:43〜21:53 は
                # 試し撮りの最後の向き（IH と床）のまま348枚を送っていた。
                if not at_home() and not _stay[0]:
                    if _away[0] == 0.0:
                        _away[0] = now
                    if not busy or now - _away[0] > AWAY_MAX:
                        sweep.go_home()
                        refresh_pose()
                        _away[0] = 0.0
                        continue
                else:
                    _away[0] = 0.0
                if busy and now - busy_since < ARRIVE_SEC:
                    gap = GAP_ARRIVE             # 入ってきた直後は細かく
                else:
                    gap = GAP_BUSY if busy else GAP_HEARTBEAT
                if now - last_sent >= gap:
                    last_sent = now
                    # 動いている間は大きいほうを送る。顔の幅が1.8倍になり、
                    # 新しい人を覚えられる線（120px）を越えられる。
                    # 誰も居ない定時報告は小さいほうで足りる（費用も軽い）。
                    # 動いている間は、古い大きい1枚より新しい小さい1枚を選ぶ。
                    # 大きいほうの流れが死んでいたら起こし直す（2026-09-19）。
                    # これまで big_proc を起こすのは外側の周回だけで、副（小さい）流れが
                    # 生きているかぎり誰も面倒を見なかった。9/17夜〜9/18は実際にこれで
                    # 1280x720 の小さい1枚だけが送られ続け、顔が小さすぎて誰も見分けられなかった。
                    _revive_hires()
                    jpg, is_big = (grab_big(HIRES_FRESH), True) if busy else (None, False)
                    if jpg is None:
                        jpg, is_big = grab(), False
                    if jpg is None:
                        continue                      # 次の周で撮り直せばよい
                    res = report(jpg, ("動き・大" if is_big else "動き") if busy
                                 else "定時", big=is_big)
                    if someone(res):
                        # 座って動かない人を見失わないため、クラウドが人を
                        # 見たと言う間は「まだ居る」として見張りを続ける。
                        # 動きだけを頼りにすると、じっとしている人が消える。
                        w.last_move = now
                    if res.get("ask_name"):
                        ask_name(res["ask_name"])
                        w.last_move = time.time()
                    if res.get("hires") and now - last_hires >= HIRES_GAP:
                        # 人は写っているのに顔が取れなかった、と返ってきた。
                        # 大きく撮り直せば取れるかもしれないので、もう一度送る。
                        last_hires = now
                        shot = grab_big()
                        if shot is not None:
                            print(time.strftime("%H:%M:%S"),
                                  "顔を探すため大きく撮り直す", flush=True)
                            if someone(report(shot, "拡大", big=True)):
                                w.last_move = now
        except Exception as e:
            print("watch failed:", e, flush=True)
        finally:
            big_proc = _hires[0] or big_proc      # 途中で起こし直した分も止める
            _hires[0] = None
            for pr in (proc, big_proc):
                try:
                    pr.kill()
                except Exception:
                    pass
        print("watch stream ended", flush=True)
        time.sleep(GAP_ERROR)            # 映像が切れたら少し待って繋ぎ直す


if __name__ == "__main__":
    main()
