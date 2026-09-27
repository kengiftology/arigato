# -*- coding: utf-8 -*-
"""区画をブラウザから足す・直す画面（2026-09-27・本人「誰でも、どこにでも足せるように」）。

それまで区画はコードの中にしかなく、**足すにはコードを直す必要があった。**
ここで直したものは状態に入り、`_zone_all()` が一覧を作るので、
**次の1周から見回りに効く。**

**見本の写真だけは、この画面からは撮れない**（カメラを振る必要がある）。
`scripts/zone_shoot.py` で撮り、**人が目で見て確かめてから**登録する。
この順番は変えない ── **その場所が写っているかを決められるのは人だけ**で、
間違った見本を登録すると、**そこから先ずっと「合っている」と出続ける。**
"""

PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>区画の設定</title><style>
body{font-family:sans-serif;max-width:720px;margin:0 auto;padding:16px;background:#faf6ec;color:#333}
h1{font-size:20px}h2{font-size:17px;margin:0 0 8px}
.card{background:#fff;border-radius:12px;padding:14px;margin:12px 0;box-shadow:0 1px 4px #0002}
label{display:block;font-size:13px;color:#666;margin:8px 0 2px}
input,textarea{font-size:16px;padding:8px;border-radius:8px;border:1px solid #ccc;width:100%;box-sizing:border-box}
textarea{min-height:58px}
button{font-size:16px;padding:10px 14px;border-radius:8px;border:0;background:#4c9be8;color:#fff;margin-top:10px}
.row{display:flex;gap:8px}.row>*{flex:1}
.view{position:relative;overflow:hidden;border-radius:8px;background:#000;margin:8px 0}
.view img{width:100%;display:block}
.view .box{position:absolute;border:2px solid #ffd166;background:rgba(255,209,102,.13)}
.note{font-size:12px;color:#777;line-height:1.6}
.off{opacity:.55}
.msg{font-size:14px;margin-top:8px}
.ok{color:#2b8a3e}.ng{color:#c92a2a}
</style></head><body>
<h1>区画の設定</h1>
<p class=note>ここで直したものは<b>次の1周から見回りに効きます</b>。
写真は「見回りが実際に撮る1枚」（見本）で、黄色い枠がその区画として見ている範囲です。<br>
<b>向きを変えたら、見本を撮り直してください。</b>別の道順で撮った見本は永久に合いません。</p>

<div class=card>
  <label>合言葉（保存するときに要ります）</label>
  <input id=key type=password placeholder="合言葉">
</div>

<div id=zones></div>

<div class=card>
  <h2>区画を足す</h2>
  <label>名前（ほかと重ならない名前）</label><input id=nname placeholder="れいぞうこの上">
  <div class=row>
    <div><label>よこ（-1.00〜1.00）</label><input id=nx value="0.00"></div>
    <div><label>たて（-1.00〜1.00）</label><input id=ny value="0.00"></div>
  </div>
  <label>いつ・どうやって決めたか（必須）</label>
  <textarea id=ndecided placeholder="2026-09-27 本人が写真を見て選んだ。線は当日の実測"></textarea>
  <button onclick="addZone()">足す</button>
  <p class=note>足したあと、<b>見本を撮って、目で見て確かめてから</b>使ってください。
  手順は <code>docs/新しい場所を足す手順_2026-09-27.md</code>。</p>
  <div id=nmsg class=msg></div>
</div>

<script>
var DATA = {};
function esc(s){
  return String(s==null?'':s).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
  });
}
function load(){
  fetch('/spirit/zones/cfg').then(function(r){return r.json();}).then(function(j){
    DATA = j;
    var h = '';
    Object.keys(j.zones).forEach(function(n){
      var z = j.zones[n] || {}, aim = z.aim || {}, crop = z.crop || {},
          ask = z.ask || {}, rule = z.rule || {};
      var box = crop.box || [0, 0, 1, 1], rot = crop.rotate || 0;
      var img = aim.ref ? (j.photo_base + aim.ref) : '';
      h += '<div class="card' + (z.active ? '' : ' off') + '">'
        + '<h2>' + esc(n) + (z.active ? '' : '（止めています）') + '</h2>'
        + (img
           ? '<div class=view><img src="' + esc(img) + '?t=' + Date.now()
             + '" style="transform:rotate(' + rot + 'deg)">'
             + '<div class=box style="left:' + (box[0] * 100) + '%;top:' + (box[1] * 100)
             + '%;width:' + ((box[2] - box[0]) * 100) + '%;height:'
             + ((box[3] - box[1]) * 100) + '%"></div></div>'
           : '<p class=note>見本の写真がまだありません。</p>')
        + '<div class=row><div><label>向き（よこ_たて）</label>'
        + '<input id="p-' + esc(n) + '" value="' + esc(z.pose || '') + '"></div>'
        + '<div><label>見る（1=見る / 0=止める）</label>'
        + '<input id="a-' + esc(n) + '" value="' + (z.active ? 1 : 0) + '"></div></div>'
        + '<div class=row><div><label>線（これ未満は違う景色）</label>'
        + '<input id="c-' + esc(n) + '" value="' + esc(aim.min_conf == null ? '' : aim.min_conf) + '"></div>'
        + '<div><label>ずれの上限（px）</label>'
        + '<input id="s-' + esc(n) + '" value="' + esc(aim.max_shift == null ? '' : aim.max_shift) + '"></div></div>'
        + '<label>切り取り（左, 上, 右, 下 ＝ 0〜1）</label>'
        + '<input id="b-' + esc(n) + '" value="' + box.join(', ') + '">'
        + '<label>何を見るか（AIへの問い）</label>'
        + '<textarea id="q-' + esc(n) + '">' + esc(ask.empty_q || '') + '</textarea>'
        + '<label>数えない物（備え付け）</label>'
        + '<textarea id="f-' + esc(n) + '">' + esc(ask.fixtures || '') + '</textarea>'
        + '<label>決まり（change=変わったときだけ / dwell=続いたら）</label>'
        + '<input id="r-' + esc(n) + '" value="' + esc(rule.kind || 'change') + '">'
        + '<label>いつ・どうやって決めたか（必須・直したら書き足す）</label>'
        + '<textarea id="d-' + esc(n) + '">' + esc(z.decided || '') + '</textarea>'
        + '<button data-zone="' + esc(n) + '" onclick="save(this.dataset.zone)">保存する</button>'
        + '<div class=msg id="m-' + esc(n) + '"></div></div>';
    });
    document.getElementById('zones').innerHTML = h;
  });
}
function val(id){ var e = document.getElementById(id); return e ? e.value.trim() : ''; }
function num(v){ return v === '' ? null : Number(v); }
function post(zone, body, msgId){
  var k = val('key'), m = document.getElementById(msgId);
  if(!k){ m.innerHTML = '<span class=ng>合言葉を入れてください</span>'; return; }
  fetch('/spirit/zones/cfg?zone=' + encodeURIComponent(zone) + '&key=' + encodeURIComponent(k),
        {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
   .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
   .then(function(x){
     if(x.ok){ m.innerHTML = '<span class=ok>保存しました。次の1周から効きます。</span>'; load(); }
     else { m.innerHTML = '<span class=ng>' + esc(x.j.detail || '保存できませんでした') + '</span>'; }
   })
   .catch(function(){ m.innerHTML = '<span class=ng>つながりませんでした</span>'; });
}
function save(n){
  var box = val('b-' + n).split(',').map(function(v){ return Number(v.trim()); });
  post(n, {pose: val('p-' + n), active: val('a-' + n) === '1',
           aim: {min_conf: num(val('c-' + n)), max_shift: num(val('s-' + n))},
           crop: {box: box},
           ask: {empty_q: val('q-' + n), fixtures: val('f-' + n)},
           rule: {kind: val('r-' + n)},
           decided: val('d-' + n)}, 'm-' + n);
}
function addZone(){
  var n = val('nname'), m = document.getElementById('nmsg');
  if(!n){ m.innerHTML = '<span class=ng>名前を入れてください</span>'; return; }
  if(DATA.zones && DATA.zones[n]){ m.innerHTML = '<span class=ng>その名前はもうあります</span>'; return; }
  var q = n + 'に、置かれている物はありますか？ JSONだけで答えてください：'
        + '{"empty": true または false, "items": "あれば短く"}';
  post(n, {id: 'z' + Date.now(), active: true,
           pose: Number(val('nx')).toFixed(2) + '_' + Number(val('ny')).toFixed(2),
           aim: {ref: 'spirit/zoneref/' + n + '.jpg', band: [0.0, 1.0],
                 min_conf: 0.08, max_shift: 60},
           crop: {box: [0.0, 0.0, 1.0, 1.0], rotate: 180},
           ask: {scene: '写真は' + n + 'を天井近くから写したものです。', fixtures: '', empty_q: q},
           rule: {kind: 'change'}, decided: val('ndecided')}, 'nmsg');
}
load();
</script></body></html>"""
