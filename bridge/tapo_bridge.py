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
import json
import os
import subprocess
import threading
import time
import urllib.request

try:                                   # 手元では bridge/ の下、ラズパイでは同じ場所
    from bridge import sweep
except ImportError:
    import sweep

CAM_URL = os.environ.get("TAPO_URL", "rtsp://thankU:39Kitchen@192.168.0.230:554/stream1")
SERVER = os.environ.get("SPIRIT_SERVER", "https://arigato-3ipecjbnha-an.a.run.app")
KEY = os.environ.get("SPIRIT_KEY", "06dc964a3cdd2c4f4c5c1d8592dff543")
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
# 写真が古いままこれだけ続いたら、映像ごと繋ぎ直す。
# 見張りの映像だけが流れつづけ、写真の書き出しだけが止まることがある。
# その状態は「映像が切れた」と判定されないので、放っておくと目が閉じたまま
# 動いているように見える（実際に5時間気づけなかった）。
SHOT_STALE_LIMIT = 90.0

GAP_BUSY = 3.0        # 動きがある間、クラウドへ送る最短間隔
GAP_HEARTBEAT = 300.0 # 何も起きなくても、これだけ経ったら1枚送る（定時報告）
GAP_ERROR = 15.0      # 失敗した時
HINT_GAP = 3.0        # 「探しに行け」の札を覗きにいく間隔
SWEEP_COOLDOWN = 600.0 # 一度探したら、しばらくは探し直さない
                       # 90秒だった頃は1日79回も首を振り、そのたびに景色が
                       # 変わって前後の比較が壊れた
POSE_GAP = 30.0        # カメラの向きを確かめにいく間隔
STILL_HOLD = 20.0     # 最後に動いてからこの秒数は「まだ居る」とみなす
CALIB_FRAMES = 20     # 最初のこの枚数で、その部屋の「静かさ」を測る
SETTLE_FRAMES = 8     # 首を振った直後、揺れが収まるまで捨てるコマ数
SETTLE_AFTER_MOVE = 4.0  # 見回りで振ったあと、映像が入れ替わるのを待つ秒数
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
        if time.time() - os.path.getmtime(path) > max_age:
            return None                       # 映像が止まっている
        with open(path, "rb") as f:
            data = f.read()
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


def send(jpg: bytes, big: bool = False) -> dict:
    """クラウドへ送って判断を受け取る。写真には向きを添える。

    big=True は「これはもう大きく撮り直した1枚」の印。これ以上大きくは
    撮れないので、クラウドに同じ頼みを繰り返させない。"""
    url = SERVER + "/spirit/frame?pose=" + _pose[0] + ("&big=1" if big else "")
    req = urllib.request.Request(
        url, data=jpg,
        headers={"Content-Type": "image/jpeg", "X-Upload-Key": KEY})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


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


def go_check(pose: str, w) -> None:
    """キッチンを見に行って、1枚だけ撮って、また戻る（2026-09-06）。

    普段は入り口を向いて待っている。人は数秒で通り過ぎるので、鳴ってから
    振ったのでは顔に間に合わないため。ただし物の増減は入り口からは見えない。

    そこで、誰も居ないと分かってから見に行く。撮るのは1枚だけ。
    前後比較は「誰も居ないキッチンの2枚」どうしになるので成立する。
    往復で10秒ほど。この間に人が来たら入り口の顔は逃すが、人感は鳴るので
    「誰か来ていた」ことは残る。"""
    try:
        x, y = (float(v) for v in pose.split("_"))
    except ValueError:
        print("bad check pose:", pose, flush=True)
        checked()
        return
    print(time.strftime("%H:%M:%S"), "キッチンを見に行く", pose, flush=True)
    if not sweep.look(x, y):
        checked()                      # 振れなかった。次の機会に回す
        return
    time.sleep(SETTLE_AFTER_MOVE)      # 映像が新しい向きに入れ替わるのを待つ
    jpg = grab_big() or grab()
    if jpg is not None:
        _pose[0] = pose                # この1枚に添える向き
        report(jpg, "見回り", big=True)
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


def report(jpg: bytes, why: str, big: bool = False) -> dict:
    """1枚送って、意味のある返事だけ記録する。返事をそのまま返す。"""
    try:
        res = send(jpg, big)
    except Exception as e:
        print("send failed:", e, flush=True)
        return {}
    if res.get("person") or res.get("judged") or res.get("why") == "face_too_small":
        print(time.strftime("%H:%M:%S"), why, res, flush=True)
    return res


def main():
    print("tapo bridge start ->", SERVER, flush=True)
    while True:
        proc = watch_stream()
        big_proc = hires_stream()        # 大きい1枚を作り替えつづける別の流れ
        w = Watcher(proc)
        w.start()
        last_sent = last_hint = last_sweep = last_pose = last_hires = 0.0
        last_fresh = time.time()         # 最後に新しい写真を読めた時刻
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
                busy = (now - w.last_move) < STILL_HOLD

                # 人を探しに行った先に留まったままだと、物の前後比較が成り立たない。
                # 落ち着いたら定位置へ戻す。比べられるのは同じ向きの2枚だけ。
                if not busy and not _stay[0] and not at_home():
                    sweep.go_home()
                    refresh_pose()
                    continue
                gap = GAP_BUSY if busy else GAP_HEARTBEAT
                if now - last_sent >= gap:
                    last_sent = now
                    # 動いている間は大きいほうを送る。顔の幅が1.8倍になり、
                    # 新しい人を覚えられる線（120px）を越えられる。
                    # 誰も居ない定時報告は小さいほうで足りる（費用も軽い）。
                    # 動いている間は、古い大きい1枚より新しい小さい1枚を選ぶ。
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
            for pr in (proc, big_proc):
                try:
                    pr.kill()
                except Exception:
                    pass
        print("watch stream ended", flush=True)
        time.sleep(GAP_ERROR)            # 映像が切れたら少し待って繋ぎ直す


if __name__ == "__main__":
    main()
