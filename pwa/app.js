const API = "";

// ── ユーザー管理 ──────────────────────────
function getCurrentUser() {
  const s = localStorage.getItem("arigato_user");
  return s ? JSON.parse(s) : null;
}
function getName() {
  const u = getCurrentUser();
  return u ? `${u.last_name} ${u.first_name}` : "";
}
function saveUser(user) {
  localStorage.setItem("arigato_user", JSON.stringify(user));
}

// 同じ手入れに何度もありがとうを積まないための端末側メモ
function hasThanked(id) {
  try { return JSON.parse(localStorage.getItem("arigato_thanked") || "[]").includes(id); }
  catch (e) { return false; }
}
function markThanked(id) {
  try {
    const a = JSON.parse(localStorage.getItem("arigato_thanked") || "[]");
    if (!a.includes(id)) { a.push(id); localStorage.setItem("arigato_thanked", JSON.stringify(a)); }
  } catch (e) {}
}

// ── 認証フロー ────────────────────────────
let pendingAction = null;
let authType = "thanks";
let selectedUserId = null;

function requireAuth(action, type = "thanks") {
  if (getCurrentUser()) { action(); return; }
  pendingAction = action;
  authType = type;
  showChoice();
}

function showChoice() {
  const title = authType === "post" ? "はじめてのお手伝いですか？" : "はじめてのありがとうですか？";
  document.getElementById("authChoiceTitle").textContent = title;
  document.getElementById("authChoice").classList.remove("hidden");
  document.getElementById("authRegister").classList.add("hidden");
  document.getElementById("authSelect").classList.add("hidden");
}

function showRegister() {
  document.getElementById("authChoice").classList.add("hidden");
  document.getElementById("authRegister").classList.remove("hidden");
}

async function showSelect() {
  document.getElementById("authChoice").classList.add("hidden");
  document.getElementById("authSelect").classList.remove("hidden");
  selectedUserId = null;
  const btn = document.getElementById("confirmBtn");
  btn.disabled = true;
  btn.classList.remove("primary");
  await renderUserGrid();
}

async function renderUserGrid() {
  const grid = document.getElementById("authUserGrid");
  grid.innerHTML = "<div style='color:#bbb;font-size:0.85rem'>読み込み中…</div>";
  try {
    const res = await fetch(`${API}/users`);
    const users = await res.json();
    if (users.length === 0) {
      grid.innerHTML = "<div style='color:#bbb;font-size:0.85rem'>登録済みのユーザーがいません</div>";
      return;
    }
    grid.innerHTML = users.map(u => `
      <div class="auth-user-card" id="ucard-${u.id}" onclick="selectUser('${u.id}')">
        <div class="auth-user-photo">
          ${u.photo_url
            ? `<img src="${u.photo_url}" alt="">`
            : `<div style="width:100%;height:100%;background:#ddd;display:flex;align-items:center;justify-content:center;font-size:1.2rem;font-weight:700;color:#999">${u.last_name[0]}</div>`
          }
        </div>
        <div class="auth-user-name">${u.last_name}<br>${u.first_name}</div>
      </div>
    `).join("");
  } catch(e) {
    grid.innerHTML = "<div style='color:#f66;font-size:0.85rem'>読み込みに失敗しました</div>";
  }
}

function selectUser(id) {
  selectedUserId = id;
  document.querySelectorAll(".auth-user-card").forEach(c => c.classList.remove("selected"));
  document.getElementById("ucard-" + id).classList.add("selected");
  const btn = document.getElementById("confirmBtn");
  btn.disabled = false;
  btn.classList.add("primary");
}

async function confirmSelect() {
  if (!selectedUserId) return;
  const res = await fetch(`${API}/users`);
  const users = await res.json();
  const u = users.find(u => u.id === selectedUserId);
  if (!u) return;
  finishAuth(u);
}

async function completeRegister() {
  const last  = document.getElementById("inputLastName").value.trim();
  const first = document.getElementById("inputFirstName").value.trim();
  if (!last || !first) { alert("姓と名を入力してください"); return; }

  const fileInput = document.getElementById("authSelfieFile");
  if (!fileInput.files[0]) { alert("顔写真を撮ってください"); return; }

  const form = new FormData();
  form.append("last_name", last);
  form.append("first_name", first);
  form.append("photo", fileInput.files[0]);

  try {
    const res = await fetch(`${API}/users/register`, { method: "POST", body: form });
    if (!res.ok) { alert("登録に失敗しました。もう一度お試しください。"); return; }
    const user = await res.json();
    finishAuth(user);
  } catch(e) {
    alert("登録に失敗しました。もう一度お試しください。");
  }
}

function previewAuthSelfie(input) {
  if (!input.files[0]) return;
  const img = document.getElementById("authSelfieImg");
  img.src = URL.createObjectURL(input.files[0]);
  img.style.display = "block";
  document.getElementById("authSelfieIcon").style.display = "none";
}

function finishAuth(user) {
  saveUser(user);
  document.getElementById("authChoice").classList.add("hidden");
  document.getElementById("authRegister").classList.add("hidden");
  document.getElementById("authSelect").classList.add("hidden");
  updateHeaderAvatar(user);
  registerPush();
  if (pendingAction) { const fn = pendingAction; pendingAction = null; fn(); }
}

function updateHeaderAvatar(user) {
  if (!user) return;
  const img = document.getElementById("myAvatarImg");
  if (user.photo_url) {
    img.src = user.photo_url;
    img.style.display = "block";
  }
}

// ── URL からゾーンIDを取得 ─────────────────
function getZoneFromUrl() {
  const m = location.pathname.match(/^\/zone\/(.+)/);
  return m ? m[1] : null;
}

// ── フィード ──────────────────────────────
let userPhotoMap = {}; // person_name → photo_url

async function loadFeed() {
  const zoneId = getZoneFromUrl();
  const [recordsRes, usersRes, thanksRes] = await Promise.all([
    fetch(zoneId ? `${API}/maintenance/zone/${zoneId}` : `${API}/maintenance`),
    fetch(`${API}/users`),
    fetch(`${API}/admin/thanks`)
  ]);
  const records   = await recordsRes.json();
  const users     = await usersRes.json();
  const allThanks = await thanksRes.json();

  // ビフォーだけの記録 = 手伝い待ち（本人の途中経過 or 誰かへの招待状）
  const waiting = records.filter(r => !r.after_photo && r.before_photo && r.status !== "abandoned");
  const done    = records.filter(r => r.after_photo);

  // 名前 → 写真URLのマップを作成
  userPhotoMap = {};
  users.forEach(u => {
    const name = `${u.last_name} ${u.first_name}`;
    if (u.photo_url) userPhotoMap[name] = u.photo_url;
  });

  // 記録ID → ありがとうリストのマップ
  const thanksByRecord = {};
  allThanks.forEach(t => {
    if (!thanksByRecord[t.maintenance_id]) thanksByRecord[t.maintenance_id] = [];
    thanksByRecord[t.maintenance_id].push(t);
  });

  // 自動ありがとう（フィードを見た = その場所を使った）完了済みのみ
  done.slice(0, 5).forEach(r =>
    fetch(`${API}/thanks/${r.id}/auto`, { method: "POST" }).catch(() => {})
  );

  const list = document.getElementById("feedList");
  if (waiting.length === 0 && done.length === 0) {
    list.innerHTML = `<div class="empty">手伝いの記録はまだありません</div>`;
    return;
  }

  // 手伝い待ちをフィードの先頭に表示
  list.innerHTML =
    waiting.map(r => waitingCardHtml(r)).join("") +
    done.map(r => cardHtml(r)).join("");

  // ゾーンページ：貢献者アバターをバナーに表示
  if (zoneId) renderContributors(records);

  // もらったありがとうを表示（フロート + 送り主アバター + 送信済み状態）
  const me = getName();
  records.forEach(r => {
    const tList = thanksByRecord[r.id] || [];
    renderSenders(r.id, tList);
    renderFloats(r.id, tList);
    if (me && tList.some(t => t.sender_name === me)) {
      const btn = document.getElementById(`btn-${r.id}`);
      if (btn) { btn.classList.remove("first"); btn.classList.add("sent"); }
    }
  });
}

// ── ホーム：維持マップ（どこが手入れされ、どこが放置か） ──────────
function loadHome(zones) {
  const list = document.getElementById("feedList");
  const banner = document.getElementById("zoneBanner");
  if (banner) banner.classList.add("hidden");
  const now = Date.now();
  const DAY = 86400 * 1000;

  function state(z) {
    if (!z.last_care_at) return { cls: "untouched", icon: "·", text: "まだ、誰も手をかけていない" };
    const days = Math.floor((now - new Date(z.last_care_at).getTime()) / DAY);
    const when = days <= 0 ? "今日" : days === 1 ? "昨日" : `${days}日前`;
    if (days <= 7)  return { cls: "fresh",   icon: "🌿", text: `${when}、手が入った` };
    if (days <= 21) return { cls: "kept",    icon: "🌱", text: `${when}に手が入った` };
    return                  { cls: "wilting", icon: "🍂", text: `${when}から、誰も手をかけていない` };
  }

  const items = zones.map(z => {
    const s = state(z);
    return `
      <div class="home-item ${s.cls}" onclick="homeTap('${z.id}')">
        <div class="home-icon">${s.icon}</div>
        <div class="home-body">
          <div class="home-name">${z.name}</div>
          <div class="home-state">${s.text}</div>
        </div>
        <div class="home-arrow">›</div>
      </div>`;
  }).join("");

  list.innerHTML = `
    <div class="home-head">
      <div class="home-title">みんなの場所</div>
      <div class="home-sub">手が入っている場所と、待っている場所</div>
    </div>
    ${items}`;
}

function homeTap(zoneId) {
  // デモ：データのあるリビングはチャットへ。他は会話がまだない
  if (zoneId === "b5e6bcea") { window.location.href = "/chat-demo.html"; return; }
  showToast("この場所は、まだ手入れの記録がありません", "");
}

// ── 場所が一人称で話す ───────────────────────
// 開いた人と場所の関係で台詞が変わる。場所＝交換の相手だと体感させる。
function zoneSpeechLines(tl, careEvents) {
  const me = getName();
  const HOUR = 3600 * 1000;
  const now = Date.now();
  const recentCare = careEvents.find(e =>
    e.after_photo && e.created_at && (now - new Date(e.created_at).getTime()) < 24 * HOUR
  );
  const myCare = me && careEvents.some(e => (e.helped_by || e.person_name) === me);

  // すべて「私（場所）」が主語。人は出さない。
  if (myCare) {
    // 手を入れた人へ＝返礼（逆電波）：場所が、自分がどう在れているかを返す
    return ["あなたが手を入れてくれたから、", "私はいい場所でいられています。", "ありがとう。"];
  } else if (recentCare) {
    return ["さっき、誰かが手を入れてくれた。", "私は少しずつ、いい場所になっていきます。"];
  } else if (careEvents.length > 0) {
    // 使う人/まだの人へ＝気づき・招待
    return ["ここは、たくさんの手で整えられてきた場所です。", "よかったら、見ていってください。"];
  } else {
    return ["まだ、誰の手も入っていません。", "はじめの一手を、待っています。"];
  }
}

function zoneSpeech(tl, careEvents) {
  const name = (tl.zone && tl.zone.name) || "この場所";
  const lines = zoneSpeechLines(tl, careEvents);
  return `
    <div class="zone-speech">
      <div class="zone-speech-icon">🌿</div>
      <div class="zone-speech-body">
        <div class="zone-speech-from">${name}</div>
        <div class="zone-speech-bubble">${lines.join("<br>")}</div>
      </div>
    </div>`;
}

// ── チャット版：場所との会話として描く（試作） ──────────────
let chatLatestCareId = null;

async function loadChat(zoneId) {
  const [tlRes, usersRes] = await Promise.all([
    fetch(`${API}/zones/${zoneId}/timeline`),
    fetch(`${API}/users`)
  ]);
  const tl    = await tlRes.json();
  const users = await usersRes.json();
  userPhotoMap = {};
  users.forEach(u => { const n = `${u.last_name} ${u.first_name}`; if (u.photo_url) userPhotoMap[n] = u.photo_url; });

  document.body.classList.add("chat-view");  // 「手伝う」FABを隠す
  const list = document.getElementById("feedList");
  const care = tl.events.filter(e => e.type === "care").slice().reverse(); // 古い順（上が古い）
  const name = (tl.zone && tl.zone.name) || "この場所";
  const me = getName();

  chatLatestCareId = care.length ? care[care.length - 1].id : null;

  // 見た＝使った：完了済みの手入れに自動ありがとうを送る（useを積む／研究データ）
  if (care.length) {
    const viewer = encodeURIComponent(me || "");
    care.filter(e => e.after_photo).slice(-5).forEach(e =>
      fetch(`${API}/thanks/${e.id}/auto?user_name=${viewer}`, { method: "POST" }).catch(() => {})
    );
  }

  // 会話相手＝場所、を最上部に固定（「私、◯◯と話してる」を一目で）
  let html = `
    <div class="chat-header">
      <div class="chat-header-icon">🌿</div>
      <div>
        <div class="chat-header-name">${name}</div>
        <div class="chat-header-sub">いま、あなたと話しています</div>
      </div>
    </div>`;

  html += `<div class="chat">`;

  if (care.length === 0) {
    // 記録ゼロの場所でも、最初の一歩（課題を出す／整えた所を残す）を踏めるように
    html += `
      <div class="msg-place">
        <div class="msg-place-icon">🌿</div>
        <div class="msg-place-bubbles">
          <div class="bubble-place">まだ、わたしの記録はないみたい。<br>気になるところや、整えてくれた所があれば、下の📷で残してね。</div>
        </div>
      </div>`;
  } else {
  // 開始のあいさつ（トークの入り口＝相手＝場所が話しかけてくる）
  html += `
    <div class="msg-place">
      <div class="msg-place-icon">🌿</div>
      <div class="msg-place-bubbles">
        <div class="bubble-place">やあ、来てくれてありがとう。<br>わたしの今日を、ちょっと見ていって。</div>
      </div>
    </div>`;
  // 🍂🌿の意味ガイド（初見の道しるべ）
  html += `<div class="chat-legend"><span>🍂 <b>散らかっていた姿</b></span><span>🌿 <b>整えてもらった姿</b></span></div>`;

  // ③ 電波：自分が整えた手入れに、誰かからありがとうが届いていたら、
  //    場所が「あなた」にお礼を返す（ヒト→環境→ヒトの最後の往復）を会話の先頭に。
  const myThanked = me && care.some(e => (e.helped_by || e.person_name) === me && (e.thanks || []).length > 0);
  if (myThanked) {
    html += `
      <div class="msg-place denpa">
        <div class="msg-place-icon">🌿</div>
        <div class="msg-place-bubbles">
          <div class="bubble-place strong">この前、整えてくれてありがとう。<br>あなたの手、ちゃんと届いてるよ。</div>
        </div>
      </div>`;
  }

  // ③' あなたが出した課題を、別の誰かが解決してくれた → 再訪で必ず気づける（通知に依存しない）
  const myTaskHelped = me && care.some(e => e.person_name === me && e.helped_by && e.helped_by !== me && e.after_photo);
  if (myTaskHelped) {
    html += `
      <div class="msg-place denpa">
        <div class="msg-place-icon">🌿</div>
        <div class="msg-place-bubbles">
          <div class="bubble-place strong">あなたが気にかけたところ、誰かが整えてくれたよ。<br>見ていってね。</div>
        </div>
      </div>`;
  }

  // 写真は1枚ずつ、全部を時系列で。各手入れを2スナップに分割：
  //   ビフォー（散らかってた姿）＝🍂 下がり（変化の「元」。これが無いと何が変わったか分からない）
  //   アフター（整った姿）       ＝🌿 上がり（ありがとうが乗る）
  // 1枚ずつの流れ（最初こう→次こう）にして、元→変化が見えるようにする。
  const snaps = [];
  care.forEach(e => {
    const t = e.created_at || "";
    if (e.before_photo) snaps.push({ key: e.id + "-b", dir: "down", photo: e.before_photo, at: t, order: 0,
                                     suggestion: e.before_suggestion,
                                     recordId: e.id, openTask: e.status === "in_progress" });
    if (e.after_photo)  snaps.push({ key: e.id + "-a", dir: "up", photo: e.after_photo, at: t, order: 1,
                                     line: e.place_line, thanks: (e.thanks || []).length,
                                     beforePhoto: e.before_photo,
                                     beforeKey: e.before_photo ? e.id + "-b" : null,  // 紐づけ用＝引用する元の姿の投稿
                                     thanked: hasThanked(e.id) });
  });
  // 時刻順、同時刻ならビフォー→アフターの順
  snaps.sort((a, b) => (a.at || "").localeCompare(b.at || "") || a.order - b.order);

  snaps.forEach(s => {
    if (s.dir === "down") {
      // 課題の写真 → AIの「こうした方が良い」を一つの吹き出しの文章に（下にぶら下げない）
      const hint = s.suggestion || "散らかってきた。誰か、気づいてくれるかな。";
      // 未解決の課題（招待状）には「これ、やった」＝手伝いを記録するボタン
      const solveBtn = s.openTask
        ? `<button class="chat-thank-btn solve" onclick="solveTask('${s.recordId}')">これ、やった 🌿</button>`
        : "";
      html += `
        <div class="msg-place down" id="care-${s.key}">
          <div class="msg-place-icon">🍂</div>
          <div class="msg-place-bubbles">
            <div class="bubble-place photo down"><img class="card-photo" src="${s.photo}" alt=""></div>
            <div class="bubble-place down">${hint}</div>
            ${solveBtn}
            <div class="chat-time">${fmtDateTime(s.at)}</div>
          </div>
        </div>`;
    } else {
      // アフター（手伝い終えた姿）：最初は言葉なし＝「誰か気づいてくれるかな？」。
      // ありがとうが押されると、場所が具体的にお礼を言う（thankUp）。
      const line = s.line || "ここ、整えてくれた。";
      const quote = s.beforePhoto ? `
        <div class="quote-before"${s.beforeKey ? ` onclick="goToPost('care-${s.beforeKey}')" role="button" tabindex="0"` : ""}>
          <img src="${s.beforePhoto}" alt="">
          <span>さっきの、散らかっていた姿</span>
        </div>` : "";
      // 既にこの端末でありがとう済みなら、お礼の言葉を出しボタンは出さない（二重付与防止）
      const thankBlock = s.thanked
        ? `<div class="bubble-place" id="msg-${s.key}">${line} ありがとう。</div>`
        : `<div class="bubble-place" id="msg-${s.key}">誰か、気づいてくれるかな？</div>
            <button class="chat-thank-btn" id="thx-${s.key}" onclick="thankUp('${s.key}')">ありがとう</button>`;
      html += `
        <div class="msg-place" id="care-${s.key}" data-line="${encodeURIComponent(line)}">
          <div class="msg-place-icon">🌿</div>
          <div class="msg-place-bubbles">
            ${quote}
            <div class="bubble-place photo"><img class="card-photo" src="${s.photo}" alt=""></div>
            ${thankBlock}
            <div class="chat-time">${fmtDateTime(s.at)}</div>
          </div>
        </div>`;
    }
  });

  // 会話の最後（最新＝最初に目に入る所）で、場所があなたに話しかける
  html += `
    <div class="msg-place">
      <div class="msg-place-icon">🌿</div>
      <div class="msg-place-bubbles">
        <div class="bubble-place">来てくれて、ありがとう。<br>今日のわたし、見ていってね。</div>
      </div>
    </div>`;
  }

  html += `</div>`; // .chat

  // 下の入力欄：📷で投稿（課題を出す／整えた所を残す）／文字で話しかける
  html += `
    <div style="text-align:center;font-size:0.72rem;color:#9aa;padding:4px 0 2px">📷 気になる所・整えた所を残す</div>
    <div class="chat-compose">
      <button class="chat-cam" onclick="startPlacePhoto()" title="いまの姿を撮る">📷</button>
      <input class="chat-input" id="chatInput" placeholder="${name}に話しかけてみる…"
        onkeydown="if(event.key==='Enter')sendChatThanks()">
      <button class="chat-send" onclick="sendChatThanks()">送る</button>
    </div>
    <input type="file" id="placePhotoInput" accept="image/*" capture="environment"
      style="display:none" onchange="onPlacePhotoPicked(this)">
    <input type="file" id="solvePhotoInput" accept="image/*" capture="environment"
      style="display:none" onchange="onSolvePhotoPicked(this)">`;

  list.innerHTML = html;

  // 最新（場所の挨拶）が見えるよう、最下部へ
  requestAnimationFrame(() => window.scrollTo(0, document.body.scrollHeight));
}

// ── 投稿フロー：📷で撮る →「気になる(課題)/整えた」を選ぶ → 保存 ──────
function startPlacePhoto() {
  const inp = document.getElementById("placePhotoInput");
  if (inp) inp.click();
}

function onPlacePhotoPicked(input) {
  if (!input.files || !input.files[0]) return;
  const file = input.files[0];
  const url = URL.createObjectURL(file);
  input.value = "";
  openIntentPicker(url, file);
}

// 撮った写真が「気になる(課題=before)」か「整えた(after)」かを選ばせる
function openIntentPicker(newPhotoUrl, newFile) {
  const ov = document.createElement("div");
  ov.className = "cont-overlay";
  ov.id = "contOverlay";
  ov.innerHTML = `
    <div class="cont-sheet">
      <div class="cont-newwrap">
        <div class="cont-label">撮った、いまの姿</div>
        <img class="cont-new" src="${newPhotoUrl}">
      </div>
      <div class="cont-label">これは、どっち？</div>
      <div class="cont-thumbs">
        <div class="cont-thumb cont-new-place" onclick="postSnap('before')">🍂 ここ、気になる<br>（誰かに気づいてほしい）</div>
        <div class="cont-thumb cont-new-place" onclick="postSnap('after')">🌿 きれいにした<br>（整えた所を残す）</div>
      </div>
      <button class="cont-cancel" onclick="closeContinuePicker()">やめる</button>
    </div>`;
  ov._newFile = newFile;
  document.body.appendChild(ov);
}

function closeContinuePicker() {
  const ov = document.getElementById("contOverlay");
  if (ov) ov.remove();
}

// 送信中オーバーレイ：AI生成＋アップロードの数秒、画面を覆って二重投稿を防ぐ
function showBusy(msg) {
  let ov = document.getElementById("busyOverlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "busyOverlay";
    ov.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.35);" +
      "display:flex;align-items:center;justify-content:center";
    ov.innerHTML = '<div style="background:#fff;padding:18px 26px;border-radius:14px;' +
      'font-size:0.95rem;color:#333;box-shadow:0 6px 24px rgba(0,0,0,.2)">' +
      '<span id="busyMsg"></span></div>';
    document.body.appendChild(ov);
  }
  ov.querySelector("#busyMsg").textContent = msg || "送信中…";
  ov.style.display = "flex";
}
function hideBusy() {
  const ov = document.getElementById("busyOverlay");
  if (ov) ov.style.display = "none";
}

// 選んだ意図で /maintenance に保存（before=課題/招待状、after=整えた記録）
function postSnap(kind) {
  const ov = document.getElementById("contOverlay");
  if (!ov) return;
  const newFile = ov._newFile;
  closeContinuePicker();
  if (!newFile) return;

  // 未登録ならまず登録（顔写真）→ そのあと保存。保存後はサーバーの実データで再描画。
  requireAuth(async () => {
    const zoneId = getZoneFromUrl();
    if (!zoneId) return;
    showBusy(kind === "before" ? "気になる所を記録中…" : "整えた所を記録中…");
    try {
      const form = new FormData();
      form.append("zone_id",     zoneId);
      form.append("person_name", getName() || "");
      form.append("content",     "");
      form.append(kind === "before" ? "before_photo" : "after_photo", newFile);
      const res = await fetch(`${API}/maintenance`, { method: "POST", body: form });
      if (!res.ok) throw new Error("post failed");
      showToast(
        kind === "before" ? "気になる所を、この場所に残しました 🍂" : "整えた所を、この場所に記録しました 🌿",
        "この記録は、他の人にも見えます"
      );
      await loadChat(zoneId);
    } catch(e) {
      alert("記録に失敗しました。もう一度お試しください。");
    } finally {
      hideBusy();
    }
  }, "post");
}

// ── 手伝いの受け渡し：🍂課題を「これ、やった」で完了させる ──────
let pendingSolveId = null;
function solveTask(recordId) {
  pendingSolveId = recordId;
  const inp = document.getElementById("solvePhotoInput");
  if (inp) inp.click();
}

function onSolvePhotoPicked(input) {
  if (!input.files || !input.files[0]) return;
  const file = input.files[0];
  input.value = "";
  const recordId = pendingSolveId;
  if (!recordId) return;

  // 未登録ならまず登録 → 既存の課題(in_progress)に after を付けて完了。
  // helper_name が投稿者と違えば helped_by 記録＋投稿者へ通知（受け渡し）。
  requireAuth(async () => {
    const zoneId = getZoneFromUrl();
    showBusy("手伝いを記録中…");
    try {
      const form = new FormData();
      form.append("after_photo", file);
      form.append("helper_name", getName() || "");
      const res = await fetch(`${API}/maintenance/${recordId}/complete`, { method: "PATCH", body: form });
      if (!res.ok) throw new Error("complete failed");
      showToast("手伝いを記録しました 🌿", "気にかけた人に、場所から届きます");
      if (zoneId) await loadChat(zoneId);
    } catch(e) {
      alert("記録に失敗しました。もう一度お試しください。");
    } finally {
      hideBusy();
    }
  }, "post");
}

// 場所が「手が入った」ことを伝える一言（匿名・場所が主語）
function chatCareLine(e) {
  if (!e.after_photo && e.before_photo) return "ここ、ちょっと気になっているんだ。";
  return "誰かが、私に手を入れてくれた。";
}

// ── 場所＝ひとつの生きてる存在（AIなし・出し分け） ──────────────
// 単位は「いまの姿（1枚）」。ビフォー/アフターのペアは無い。
// ありがとうの有無で、場所の「変化の受け取り方」が変わる。
function loadPlace(zoneId) {
  return fetch(`${API}/zones/${zoneId}/timeline`).then(r => r.json()).then(tl => {
    const list = document.getElementById("feedList");
    const name = (tl.zone && tl.zone.name) || "この場所";
    const me = getName();
    // 各手入れの写真を「その時の姿」として、新しい順に（ビフォー単体も含む）。
    const snaps = tl.events.filter(e => e.type === "care" && (e.after_photo || e.before_photo))
      .map(e => ({
        id: e.id,
        photo: e.after_photo || e.before_photo,
        at: e.created_at,
        thanks: (e.thanks || []).length,
        doer: e.helped_by || e.person_name,   // この姿にした人
      }))
      .sort((a, b) => (b.at || "").localeCompare(a.at || ""));

    if (snaps.length === 0) { list.innerHTML = `<div class="empty">まだ、この場所の姿はありません</div>`; return; }

    const now = Date.now(), DAY = 86400000;
    const latest = snaps[0];
    const days = Math.floor((now - new Date(latest.at).getTime()) / DAY);

    let nowLine;
    if (days <= 7)       nowLine = "いま、よく整ってる。気持ちいい。";
    else if (days <= 21) nowLine = "まだ、この前の整いが残ってる。";
    else                 nowLine = "最近、また少し、雑然としてきたかも。";

    // ③ 環境からのありがとう：自分が整えた姿に、誰かからありがとうが届いていたら、
    //    場所が「あなた」に向けてお礼を返す（ヒト→環境→ヒトの電波の最後の往復）
    const myThanked = me && snaps.some(s => s.doer === me && s.thanks > 0);
    const fromPlace = myThanked ? `
      <div class="place-from">
        <div class="place-from-icon">🌿</div>
        <div class="place-from-body">
          <div class="place-from-name">${name}より</div>
          <div class="place-from-line">この前、整えてくれてありがとう。<br>おかげで、まだ気持ちよくいられてる。</div>
        </div>
      </div>` : "";

    let html = `
      <div class="place">
        ${fromPlace}
        <div class="place-now">
          <div class="place-now-label">${name}のいま</div>
          <img class="place-now-photo" src="${latest.photo}" alt="">
          <div class="place-now-line" id="nowLine">${nowLine}</div>
        </div>
        <div class="place-hist-head">これまでの姿</div>`;

    snaps.forEach(s => {
      html += `
        <div class="place-snap" id="snap-${s.id}" data-thanks="${s.thanks}" data-doer="${encodeURIComponent(s.doer || "")}">
          <img class="place-snap-photo" src="${s.photo}" alt="">
          <div class="place-snap-body">
            <div class="place-snap-when">${fmtDate(s.at)}</div>
            <div class="place-snap-line" id="snapline-${s.id}">${receptionLine(s.thanks)}</div>
            <button class="place-thank-btn" onclick="placeThank('${s.id}')">ありがとうを送る</button>
          </div>
        </div>`;
    });

    html += `</div>`;
    list.innerHTML = html;

    const banner = document.getElementById("zoneBanner");
    if (banner) banner.classList.add("hidden");
  });
}

// ありがとうの数で、場所の「変化の受け取り方」が変わる（核）
function receptionLine(n) {
  if (n === 0)  return "変わった。でも、気づいてもらえたかは、まだ分からない。";
  if (n < 3)    return "整えてもらった、って伝わってきた。";
  return "あのときのこと、みんなが気にかけてくれた。";
}

// その姿に「ありがとう」を送る → 場所の受け取り方が変わり、整えた人へ環境から返る
function placeThank(snapId) {
  const snap = document.getElementById("snap-" + snapId);
  if (!snap) return;
  const n = (parseInt(snap.dataset.thanks, 10) || 0) + 1;
  snap.dataset.thanks = n;
  const line = document.getElementById("snapline-" + snapId);
  if (line) line.textContent = receptionLine(n);
  const nowLine = document.getElementById("nowLine");
  if (nowLine) nowLine.textContent = "誰かが気づいてくれた。ここは、保たれていく。";
  // ヒト→環境→ヒト：あなたのありがとうを、場所が整えた人へ届ける
  showToast(`あなたのありがとうを、${"この場所"}が受け取りました 🌿`, "整えた人へ、場所から届きます");
}

// 引用画像から、元の投稿（散らかっていた姿）へ飛ぶ
function goToPost(id) {
  const el = document.getElementById(id);
  if (!el) return;
  requestAnimationFrame(() => {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  el.classList.add("post-highlight");
  setTimeout(() => el.classList.remove("post-highlight"), 1600);
}

// アフター写真への「ありがとう」：押されると、場所が具体的にお礼を言う
function thankUp(key) {
  const post = document.getElementById("care-" + key);
  if (!post) return;
  const line = post.dataset.line ? decodeURIComponent(post.dataset.line) : "";
  const msg = document.getElementById("msg-" + key);
  if (msg) msg.textContent = line ? `${line} ありがとう。` : "整えてくれて、ありがとう。";
  const btn = document.getElementById("thx-" + key);
  if (btn) btn.remove();
  // サーバーへ：この手入れ(careId)にありがとうを保存し、整えた人へ届ける
  const careId = key.replace(/-[ab]$/, "");
  markThanked(careId);  // 再読込しても二重に積まないよう端末に記録
  fetch(`${API}/thanks/${careId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sender_name: getName() || "", message: "" })
  }).catch(() => {});
}

// チャットの入力から、場所にありがとうを送る
async function sendChatThanks() {
  const input = document.getElementById("chatInput");
  if (!input) return;
  const msg = input.value.trim();
  input.value = "";

  const chat = document.querySelector(".chat");
  if (!chat) return;

  // あなたの一言（右）
  const you = document.createElement("div");
  you.className = "msg-you";
  you.innerHTML = `<div class="bubble-you">${msg || "ありがとう"}</div>`;
  chat.appendChild(you);

  // 場所が受け取って返す（整えた人へ届ける＝電波）
  const reply = document.createElement("div");
  reply.className = "msg-place";
  reply.innerHTML = `
    <div class="msg-place-icon">🌿</div>
    <div class="msg-place-bubbles">
      <div class="bubble-place">受け取ったよ。整えてくれた人に、ちゃんと届けるね。</div>
    </div>`;
  chat.appendChild(reply);
  reply.scrollIntoView({ behavior: "smooth", block: "end" });

  if (chatLatestCareId) {
    fetch(`${API}/thanks/${chatLatestCareId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender_name: getName() || "", message: msg })
    }).catch(() => {});
  }
}

// ── 場所の変遷タイムライン ───────────────────
async function loadTimeline(zoneId) {
  const [tlRes, usersRes] = await Promise.all([
    fetch(`${API}/zones/${zoneId}/timeline`),
    fetch(`${API}/users`)
  ]);
  const tl    = await tlRes.json();
  const users = await usersRes.json();

  userPhotoMap = {};
  users.forEach(u => {
    const name = `${u.last_name} ${u.first_name}`;
    if (u.photo_url) userPhotoMap[name] = u.photo_url;
  });

  // 貢献者アバターをバナーに
  const careEvents = tl.events.filter(e => e.type === "care");
  renderContributors(careEvents.map(e => ({ person_name: e.person_name, helped_by: e.helped_by })));

  // この場所を見た = 使った：完了済みの手入れに自動ありがとうを送る（useを積む）
  const viewer = encodeURIComponent(getName() || "");
  careEvents.filter(e => e.after_photo).slice(0, 5).forEach(e =>
    fetch(`${API}/thanks/${e.id}/auto?user_name=${viewer}`, { method: "POST" }).catch(() => {})
  );

  const list = document.getElementById("feedList");
  if (tl.events.length === 0) {
    list.innerHTML = `<div class="empty">まだこの場所の記録はありません</div>`;
    return;
  }

  // use イベントを maintenance_id ごとに集計（フロート用）
  const useByRecord = {};
  tl.events.filter(e => e.type === "use").forEach(e => {
    useByRecord[e.maintenance_id] = (useByRecord[e.maintenance_id] || 0) + 1;
  });

  // 場所が一人称で、いま開いた人に話しかける（環境との交換を体感させる）
  const speechHtml = zoneSpeech(tl, careEvents);

  // イベント列
  const eventsHtml = tl.events.map(e => {
    if (e.type === "use") {
      // 匿名：誰が使ったかは出さない
      return `
        <div class="tl-event tl-use">
          <div class="tl-marker"></div>
          <div class="tl-use-text">${fmtDateTime(e.created_at)}　誰かが使った</div>
        </div>`;
    }
    // care：完了済みは通常カード、ビフォーのみは手伝い待ちカードを流用
    const isWaiting = !e.after_photo && e.before_photo && e.status !== "abandoned";
    // 場所名はバナーに出るので、カード側は中立ラベル（"手入れ"）にする
    const inner = isWaiting ? waitingCardHtml(careRecord(e)) : cardHtml(careRecord(e));
    return `
      <div class="tl-event tl-care">
        <div class="tl-marker"></div>
        <div class="tl-care-head">
          <div class="tl-care-when">${fmtDateTime(e.created_at)}</div>
        </div>
        ${inner}
      </div>`;
  }).join("");

  list.innerHTML = speechHtml + `<div class="timeline">${eventsHtml}</div>`;

  // フロート・送り主・送信済み状態（完了カードのみ）
  const me = getName();
  careEvents.forEach(e => {
    const userT = (e.thanks || []).map(t => ({ source: "user", sender_name: t.sender_name, message: t.message }));
    const autoCount = useByRecord[e.id] || 0;
    const autoT = Array.from({ length: Math.min(autoCount, 4) }, () => ({ source: "auto" }));
    renderSenders(e.id, userT);
    renderFloats(e.id, [...userT, ...autoT]);
    if (me && userT.some(t => t.sender_name === me)) {
      const btn = document.getElementById(`btn-${e.id}`);
      if (btn) { btn.classList.remove("first"); btn.classList.add("sent"); }
    }
  });
}

// timeline の care イベントを cardHtml/waitingCardHtml が読める形に
function careRecord(e, zoneName = "") {
  return {
    id: e.id,
    person_name: e.person_name,
    helped_by: e.helped_by,
    before_photo: e.before_photo,
    after_photo: e.after_photo,
    status: e.status,
    thanks_count: e.thanks_count,
    created_at: e.created_at,
    zone_name: zoneName,
  };
}

// 匿名：人数は出さない（人のデータは見せない）
function renderContributors(records) {
  const div = document.getElementById("zoneContribs");
  if (div) div.innerHTML = "";
}

// その手入れに灯ったありがとうの数（匿名：送り主は出さない）
function renderSenders(id, tList) {
  const div = document.getElementById(`senders-${id}`);
  if (!div) return;
  const count = tList.filter(t => t.source === "user").length;
  div.innerHTML = count ? `<div class="thanks-count-label">${count} のありがとう</div>` : "";
}

// もらったありがとうを写真上に常時浮かせる
function renderFloats(id, tList) {
  const overlay = document.getElementById(`overlay-${id}`);
  if (!overlay || tList.length === 0) return;
  const shown = tList.slice(0, 8); // 多すぎると邪魔なので最大8件
  const interval = 5 / shown.length;
  shown.forEach((t, i) => {
    const pill = document.createElement("div");
    pill.className = "af";
    pill.style.left = `${10 + Math.random() * 58}%`;
    pill.style.animationDelay = `${-(interval * i).toFixed(2)}s`;
    // 匿名：送り主の顔は出さない。無地のありがとうピル
    const text = (t.message || "ありがとう").slice(0, 20);
    pill.innerHTML = `<span class="af-text">🌱 ${text}</span>`;
    overlay.appendChild(pill);
  });
}

// 手伝い待ちカード（ビフォーだけの記録 = 本人の途中経過 or 誰かへの招待状）
function waitingCardHtml(r) {
  // 匿名：誰が置いたか・タイトルは出さない。「気になっています」タグだけ
  const mine = getName() === r.person_name;
  return `
    <div class="concern-card">
      <div class="card-header" style="justify-content:flex-end">
        <div class="concern-tag">気になっています</div>
      </div>
      ${r.before_photo ? `<img class="concern-photo" src="${r.before_photo}" alt="">` : ""}
      <div class="concern-row">
        <div class="concern-hint">ありがとうが待っています</div>
        <button class="btn-help" onclick="helpRecord('${r.id}')">${mine ? "できた" : "手伝う"}</button>
      </div>
    </div>
  `;
}

function cardHtml(r) {
  // 匿名：人もタイトルも受け渡し文も出さない。写真とありがとうだけ
  const hasBoth = r.before_photo && r.after_photo;
  const photoSrc = r.after_photo || r.before_photo || "";
  const initLabel = r.after_photo ? "AFTER" : "BEFORE";
  const thanksLabel = "ありがとう";
  const btnClass = r.thanks_count === 0 ? "btn-thanks first" : "btn-thanks";
  const beforeUrl = r.before_photo || "";
  const afterUrl  = r.after_photo  || "";

  return `
    <div class="card" id="card-${r.id}">
      ${photoSrc ? `
      <div class="photo-wrap" id="photowrap-${r.id}"
        data-before="${beforeUrl}" data-after="${afterUrl}"
        data-showing="${r.after_photo ? 'after' : 'before'}"
        data-hasboth="${hasBoth ? '1' : '0'}">
        <img class="card-photo" id="photo-${r.id}" src="${photoSrc}" alt="">
        <div class="photo-label" id="label-${r.id}">${initLabel}</div>
        ${hasBoth ? '<div class="photo-tap-hint">タップで切り替え</div>' : ""}
        <div class="arigato-overlay" id="overlay-${r.id}"></div>
      </div>` : ""}
      <div class="thanks-row">
        <div class="thanks-senders" id="senders-${r.id}"></div>
        <button class="${btnClass}" id="btn-${r.id}" onclick="openThanksModal('${r.id}')">
          ${thanksLabel}
        </button>
      </div>
    </div>
  `;
}

// ── 人の蓄積シート ─────────────────────────
async function openPersonSheet(encodedName) {
  const name = decodeURIComponent(encodedName || "");
  if (!name) return;

  const avatarEl = document.getElementById("personAvatar");
  const photo = userPhotoMap[name];
  avatarEl.innerHTML = photo
    ? `<img src="${photo}" alt="">`
    : `<span>${name[0]}</span>`;
  document.getElementById("personName").textContent = name;
  document.getElementById("personSub").textContent = "読み込み中…";
  document.getElementById("personGrid").innerHTML = "";
  document.getElementById("personOverlay").classList.add("open");

  try {
    const res = await fetch(`${API}/maintenance`);
    const records = (await res.json())
      .filter(r => (r.helped_by || r.person_name) === name)
      .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || "")); // 古い順 = 積み重ね

    document.getElementById("personSub").textContent = `${records.length}回のお手伝い`;

    if (records.length === 0) {
      document.getElementById("personGrid").innerHTML = "";
      return;
    }

    document.getElementById("personGrid").innerHTML = records.map(r => {
      const src = r.after_photo || r.before_photo;
      const d = new Date(r.created_at);
      const label = `${d.getMonth() + 1}月${d.getDate()}日 · ${r.zone_name || ""}`;
      return `
        <div class="person-photo-item">
          ${src ? `<img src="${src}" alt="">` : ""}
          <div class="person-photo-date">${label}</div>
        </div>`;
    }).join("");
  } catch(e) {
    document.getElementById("personSub").textContent = "読み込みに失敗しました";
  }
}

function closePersonSheet(e) {
  if (e.target === document.getElementById("personOverlay")) {
    document.getElementById("personOverlay").classList.remove("open");
  }
}

// ── 写真トグル（イベント委譲） ────────────────
document.addEventListener("click", e => {
  const wrap = e.target.closest(".photo-wrap");
  if (!wrap || wrap.dataset.hasboth !== "1") return;
  const id = wrap.id.replace("photowrap-", "");
  const img   = document.getElementById(`photo-${id}`);
  const label = document.getElementById(`label-${id}`);
  if (!img || !label) return;
  if (wrap.dataset.showing === "after") {
    img.src = wrap.dataset.before;
    label.textContent = "BEFORE";
    wrap.dataset.showing = "before";
  } else {
    img.src = wrap.dataset.after;
    label.textContent = "AFTER";
    wrap.dataset.showing = "after";
  }
});

// ── ありがとうモーダル ─────────────────────
let pendingThanksId = null;

function openThanksModal(id) {
  requireAuth(() => _openThanksModal(id), "thanks");
}

function _openThanksModal(id) {
  pendingThanksId = id;
  const btn = document.getElementById(`btn-${id}`);
  if (btn && btn.classList.contains("sent")) return;

  // 匿名：宛先は人ではなく「その場所」。案A＝環境に向けてありがとうを送る
  const place = currentZoneName || "この場所";
  const photoEl = document.getElementById("modalPersonPhoto");
  photoEl.innerHTML = `<span style="font-size:1.6rem">🌱</span>`;

  document.getElementById("modalHint").textContent = `${place}にありがとうを伝える`;
  document.getElementById("thanksText").value = "";
  document.getElementById("modalOverlay").classList.remove("hidden");
}

async function sendThanks() {
  const id = pendingThanksId;
  if (!id) return;
  const msg = document.getElementById("thanksText").value.trim();
  const btn = document.getElementById(`btn-${id}`);

  closeModal();

  if (btn) {
    btn.classList.remove("first");
    btn.classList.add("sent");
    btn.textContent = "ありがとう";
  }

  // フロートアニメーション
  triggerFloat(id, msg);

  try {
    await fetch(`${API}/thanks/${id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender_name: getName() || "", message: msg })
    });
    showToast(`${currentZoneName || "この場所"}にありがとうを伝えました 🌱`, "");
  } catch(e) {
    console.error("sendThanks error:", e);
  }
}

function closeModal() {
  document.getElementById("modalOverlay").classList.add("hidden");
  pendingThanksId = null;
}

// ── ありがとうフロートアニメーション ─────────
// 自分が送った瞬間に1つ追加（そのまま常時ループに加わる）
function triggerFloat(recordId, msg = "") {
  const overlay = document.getElementById(`overlay-${recordId}`);
  if (!overlay) return;
  // 匿名：送り主の顔は出さない
  const text = (msg || "ありがとう").slice(0, 20);

  const pill = document.createElement("div");
  pill.className = "af";
  pill.style.left = `${15 + Math.random() * 55}%`;
  pill.style.animationDelay = "0s";
  pill.innerHTML = `<span class="af-text">🌱 ${text}</span>`;
  overlay.appendChild(pill);
}

// ── モード選択 ────────────────────────────
let currentZoneId = null;
let currentZoneName = "";
let currentMode = "after";

function openModeSelect() {
  requireAuth(() => _openModeSelect(), "post");
}
function _openModeSelect() {
  const zoneId = getZoneFromUrl();
  const pending = zoneId ? localStorage.getItem(`pending_${zoneId}`) : null;
  if (pending) {
    startAfter();
    return;
  }
  document.getElementById("modeOverlay").classList.remove("hidden");
}

function closeModeSelect(e) {
  if (e.target === document.getElementById("modeOverlay")) {
    document.getElementById("modeOverlay").classList.add("hidden");
  }
}

function startBefore() {
  document.getElementById("modeOverlay").classList.add("hidden");
  currentMode = "before";
  openPostModal("before");
}

function startAfter() {
  document.getElementById("modeOverlay").classList.add("hidden");
  currentMode = "after";
  openPostModal("after");
}

// 手伝い待ちカードから：その記録にアフターを付けて完了させる
let helpingRecordId = null;
function helpRecord(id) {
  requireAuth(() => {
    helpingRecordId = id;
    currentMode = "help";
    openPostModal("help");
  }, "post");
}

async function openPostModal(mode) {
  const zoneId = getZoneFromUrl();

  if (mode === "help") {
    // 既存の記録に付けるのでゾーン選択は不要
    document.getElementById("zoneSelectWrap").style.display = "none";
  } else if (zoneId) {
    currentZoneId = zoneId;
    document.getElementById("zoneSelectWrap").style.display = "none";
  } else {
    currentZoneId = null;
    document.getElementById("zoneSelectWrap").style.display = "block";
    await populateZoneSelect();
  }

  const titles = {
    before: "気になる場所を記録する",
    after:  "手伝った後の様子を記録する",
    help:   "手伝った後の様子を記録する",
  };
  document.getElementById("postModalTitle").textContent = titles[mode] || titles.after;
  document.getElementById("postBtn").textContent = "記録する";

  // 写真プレビューリセット
  document.getElementById("photoPreview").style.display = "none";
  document.getElementById("photoIcon").style.display = "block";
  document.getElementById("photoInput").value = "";

  document.getElementById("postModalOverlay").classList.remove("hidden");
}

async function populateZoneSelect() {
  const res = await fetch(`${API}/zones`);
  const zones = await res.json();
  const sel = document.getElementById("modalZoneSelect");
  sel.innerHTML = `<option value="">場所を選んでください</option>` +
    zones.map(z => `<option value="${z.id}">${z.name}</option>`).join("");
}

function closePostModal() {
  document.getElementById("postModalOverlay").classList.add("hidden");
  document.getElementById("photoPreview").style.display = "none";
  document.getElementById("photoIcon").style.display = "block";
  document.getElementById("photoInput").value = "";
}

function previewPostPhoto(input) {
  if (!input.files[0]) return;
  const img = document.getElementById("photoPreview");
  img.src = URL.createObjectURL(input.files[0]);
  img.style.display = "block";
  document.getElementById("photoIcon").style.display = "none";
}

// ── 投稿 ──────────────────────────────────
async function submitPost() {
  const photoFile = document.getElementById("photoInput").files[0];
  if (!photoFile) { alert("写真を撮ってください"); return; }

  const btn = document.getElementById("postBtn");
  btn.disabled = true;
  btn.textContent = "送信中…";

  // 手伝い待ちカード経由：既存の記録にアフターを付けて完了
  if (currentMode === "help" && helpingRecordId) {
    const helperName = getName();
    try {
      const form = new FormData();
      form.append("after_photo", photoFile);
      form.append("helper_name", helperName);
      await fetch(`${API}/maintenance/${helpingRecordId}/complete`, { method: "PATCH", body: form });
      // 自分のビフォーを自分で閉じた場合はpendingを掃除
      Object.keys(localStorage)
        .filter(k => k.startsWith("pending_") && localStorage.getItem(k) === helpingRecordId)
        .forEach(k => localStorage.removeItem(k));
      helpingRecordId = null;
      fetch(`${API}/thanks/welcome`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person_name: helperName })
      }).catch(() => {});
      showToast("この場所を手伝ってくれてありがとう 🌱", "あなたのお手伝いが記録されました");
      closePostModal();
      await loadFeed();
    } catch(e) {
      alert("投稿に失敗しました。もう一度お試しください。");
    }
    btn.disabled = false;
    btn.textContent = "記録する";
    return;
  }

  if (!currentZoneId) {
    currentZoneId = document.getElementById("modalZoneSelect").value;
  }
  if (!currentZoneId) {
    alert("場所を選んでください");
    btn.disabled = false;
    btn.textContent = "記録する";
    return;
  }

  const name = getName();
  const pendingKey = `pending_${currentZoneId}`;
  const pendingId  = localStorage.getItem(pendingKey);

  try {
    if (currentMode === "after" && pendingId) {
      const form = new FormData();
      form.append("after_photo", photoFile);
      await fetch(`${API}/maintenance/${pendingId}/complete`, { method: "PATCH", body: form });
      localStorage.removeItem(pendingKey);
    } else {
      const form = new FormData();
      form.append("zone_id",     currentZoneId);
      form.append("person_name", name);
      form.append("content",     "");
      if (currentMode === "before") {
        form.append("before_photo", photoFile);
      } else {
        form.append("after_photo", photoFile);
      }
      const res  = await fetch(`${API}/maintenance`, { method: "POST", body: form });
      const data = await res.json();
      if (currentMode === "before" && data.id) {
        localStorage.setItem(pendingKey, data.id);
      }
    }

    if (currentMode === "after") {
      fetch(`${API}/thanks/welcome`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person_name: name })
      }).catch(() => {});
      showToast("この場所を手伝ってくれてありがとう 🌱", "あなたのお手伝いが記録されました");
    } else {
      showToast("気づいてくれてありがとう 🌱", "きれいになったら、まっさきにお知らせします");
    }

    closePostModal();
    await loadFeed();
  } catch(e) {
    alert("投稿に失敗しました。もう一度お試しください。");
  }

  btn.disabled = false;
  btn.textContent = "記録する";
}

// ── プッシュ通知 ──────────────────────────
async function registerPush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  try {
    const reg = await navigator.serviceWorker.register("/sw.js");
    const keyRes = await fetch(`${API}/push/vapid-public-key`);
    const { key } = await keyRes.json();
    if (!key) return;

    const perm = await Notification.requestPermission();
    if (perm !== "granted") return;

    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key)
    });
    await fetch(`${API}/push/subscribe?person_name=${encodeURIComponent(getName())}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub)
    });
  } catch (e) {}
}

// ── アプリ内トースト通知 ───────────────────
let toastTimer = null;
function showToast(msg, sub = "") {
  const toast = document.getElementById("toast");
  if (!toast) return;
  document.getElementById("toastMsg").textContent = msg;
  document.getElementById("toastSub").textContent = sub;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}

// Service Workerからのプッシュをフォアグラウンドでも表示
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", e => {
    if (e.data && e.data.type === "push") {
      showToast(e.data.title || "ありがとう", e.data.body || "");
    }
  });
}

// ── ユーティリティ ────────────────────────
function formatTime(iso) {
  const diff = Math.floor((Date.now() - new Date(iso)) / 60000);
  if (diff < 1)    return "たった今";
  if (diff < 60)   return `${diff}分前`;
  if (diff < 1440) return `${Math.floor(diff / 60)}時間前`;
  return `${Math.floor(diff / 1440)}日前`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function urlBase64ToUint8Array(base64) {
  const pad = "=".repeat((4 - base64.length % 4) % 4);
  const b64 = (base64 + pad).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(b64), c => c.charCodeAt(0));
}

// ── 初期化 ────────────────────────────────
async function init() {
  const zoneId = getZoneFromUrl();

  if (zoneId) {
    try {
      const res = await fetch(`${API}/zones`);
      const zones = await res.json();
      const zone = zones.find(z => z.id === zoneId);
      if (zone) {
        currentZoneName = zone.name;
        const banner = document.getElementById("zoneBanner");
        banner.classList.remove("hidden");
        document.getElementById("zoneNameText").textContent = zone.name;
      }
    } catch(e) {}
  }

  if (zoneId) {
    await loadChat(zoneId);
  } else {
    await loadFeed();
  }
  // ログイン済みの場合のみプッシュ通知を購読
  if (getCurrentUser()) {
    await registerPush();
  }
}

// ── 起動 ──────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  const user = getCurrentUser();
  if (user) {
    updateHeaderAvatar(user);
  }
  init();
});
