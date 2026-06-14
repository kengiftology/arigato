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

  const form = new FormData();
  form.append("last_name", last);
  form.append("first_name", first);
  const fileInput = document.getElementById("authSelfieFile");
  if (fileInput.files[0]) {
    form.append("photo", fileInput.files[0]);
  }

  try {
    const res = await fetch(`${API}/users/register`, { method: "POST", body: form });
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

  // サマリー帯
  const s = tl.summary;
  const summaryHtml = `
    <div class="tl-summary">
      <div class="tl-summary-title">この場所のこれまで</div>
      <div class="tl-summary-nums">手入れ ${s.care_count}回 · 利用 ${s.use_count}回 · ${s.contributor_count}人</div>
      ${s.first_care_at ? `<div class="tl-summary-period">${fmtDate(s.first_care_at)} から ${fmtDate(s.last_care_at)} まで</div>` : ""}
    </div>`;

  // イベント列
  const eventsHtml = tl.events.map(e => {
    if (e.type === "use") {
      const who = e.user_name ? `${e.user_name} が使った` : "誰かが使った";
      const photo = e.user_name ? userPhotoMap[e.user_name] : null;
      const face = photo
        ? `<span class="tl-use-face"><img src="${photo}" alt=""></span>`
        : "";
      return `
        <div class="tl-event tl-use">
          <div class="tl-marker"></div>
          ${face}
          <div class="tl-use-text">${fmtDateTime(e.created_at)}　${who}</div>
        </div>`;
    }
    // care：完了済みは通常カード、ビフォーのみは手伝い待ちカードを流用
    const isWaiting = !e.after_photo && e.before_photo && e.status !== "abandoned";
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

  list.innerHTML = summaryHtml + `<div class="timeline">${eventsHtml}</div>`;

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
function careRecord(e) {
  return {
    id: e.id,
    person_name: e.person_name,
    helped_by: e.helped_by,
    before_photo: e.before_photo,
    after_photo: e.after_photo,
    status: e.status,
    thanks_count: e.thanks_count,
    created_at: e.created_at,
    zone_name: "",
  };
}

// ゾーンを手伝った人たちのアバター（タップで蓄積シート）
function renderContributors(records) {
  const div = document.getElementById("zoneContribs");
  if (!div) return;
  const names = [...new Set(records.map(r => r.helped_by || r.person_name).filter(Boolean))].slice(0, 5);
  div.innerHTML = names.map(n => {
    const p = userPhotoMap[n];
    return `<div class="c-avatar" onclick="openPersonSheet('${encodeURIComponent(n)}')">${
      p ? `<img src="${p}" alt="">` : n[0]
    }</div>`;
  }).join("");
}

// 送り主のアバターを並べる（手動ありがとうのみ）
function renderSenders(id, tList) {
  const div = document.getElementById(`senders-${id}`);
  if (!div) return;
  const names = [...new Set(
    tList.filter(t => t.source === "user" && t.sender_name).map(t => t.sender_name)
  )].slice(0, 6);
  div.innerHTML = names.map(n => {
    const p = userPhotoMap[n];
    return `<div class="sender-avatar">${
      p ? `<img src="${p}" alt="">`
        : `<div style="width:100%;height:100%;background:#ddd;display:flex;align-items:center;justify-content:center;font-size:0.6rem;font-weight:700;color:#999">${n[0]}</div>`
    }</div>`;
  }).join("");
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
    const isUser = t.source === "user" && t.sender_name;
    const photo = isUser ? userPhotoMap[t.sender_name] : null;
    const face = photo
      ? `<img src="${photo}" alt="">`
      : `<div style="width:100%;height:100%;background:#ddd;display:flex;align-items:center;justify-content:center;font-size:0.6rem;font-weight:700;color:#999">${isUser ? t.sender_name[0] : "🌱"}</div>`;
    const text = (t.message || "ありがとう").slice(0, 20);
    pill.innerHTML = `
      <div class="af-face">${face}</div>
      <span class="af-text">${text}</span>
    `;
    overlay.appendChild(pill);
  });
}

// 手伝い待ちカード（ビフォーだけの記録 = 本人の途中経過 or 誰かへの招待状）
function waitingCardHtml(r) {
  const initial  = r.person_name ? r.person_name[0] : "？";
  const photoUrl = userPhotoMap[r.person_name] || null;
  const zoneLabel = r.zone_name && !getZoneFromUrl() ? ` · ${r.zone_name}` : "";
  const mine = getName() === r.person_name;
  return `
    <div class="concern-card">
      <div class="card-header" onclick="openPersonSheet('${encodeURIComponent(r.person_name || "")}')">
        <div class="avatar">
          ${photoUrl
            ? `<img src="${photoUrl}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:50%">`
            : `<span>${initial}</span>`}
        </div>
        <div style="flex:1">
          <div class="card-person">${r.person_name}</div>
          <div class="card-time">${formatTime(r.created_at)}${zoneLabel}</div>
        </div>
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
  const doer     = r.helped_by || r.person_name;   // 実際に手を動かした人
  const initial  = doer ? doer[0] : "？";
  const photoUrl = userPhotoMap[doer] || null;
  const handoff  = r.helped_by ? `${r.person_name}さんの「気になる」に応えました` : "";
  const hasBoth = r.before_photo && r.after_photo;
  const photoSrc = r.after_photo || r.before_photo || "";
  const initLabel = r.after_photo ? "AFTER" : "BEFORE";
  const thanksLabel = "ありがとう";
  const btnClass = r.thanks_count === 0 ? "btn-thanks first" : "btn-thanks";
  const beforeUrl = r.before_photo || "";
  const afterUrl  = r.after_photo  || "";

  return `
    <div class="card" id="card-${r.id}">
      <div class="card-header" onclick="openPersonSheet('${encodeURIComponent(doer || "")}')">
        <div class="avatar">
          ${photoUrl
            ? `<img src="${photoUrl}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:50%">`
            : `<span>${initial}</span>`}
        </div>
        <div>
          <div class="card-person">${doer}</div>
          <div class="card-time">${formatTime(r.created_at)}${handoff ? " · " + handoff : ""}</div>
        </div>
      </div>
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
        <button class="${btnClass}" id="btn-${r.id}" onclick="openThanksModal('${r.id}', '${encodeURIComponent(doer || "")}')">
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

function openThanksModal(id, encodedName) {
  requireAuth(() => _openThanksModal(id, encodedName), "thanks");
}

function _openThanksModal(id, encodedName) {
  pendingThanksId = id;
  const btn = document.getElementById(`btn-${id}`);
  if (btn && btn.classList.contains("sent")) return;

  const name = decodeURIComponent(encodedName || "");
  const initial = name ? name[0] : "？";
  const photo = userPhotoMap[name];

  const photoEl = document.getElementById("modalPersonPhoto");
  photoEl.innerHTML = photo
    ? `<img src="${photo}" alt="" style="width:100%;height:100%;object-fit:cover">`
    : `<span style="font-size:1.4rem;font-weight:700;color:#999">${initial}</span>`;

  document.getElementById("modalHint").textContent = name ? `${name} さんにありがとうを届ける` : "ありがとうを届ける";
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
    showToast("届けました 🙏", "");
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
  const user = getCurrentUser();
  const photoUrl = user ? user.photo_url : null;
  const initial = user ? user.last_name[0] : "？";
  const text = (msg || "ありがとう").slice(0, 20);

  const pill = document.createElement("div");
  pill.className = "af";
  pill.style.left = `${15 + Math.random() * 55}%`;
  pill.style.animationDelay = "0s";
  pill.innerHTML = `
    <div class="af-face">
      ${photoUrl ? `<img src="${photoUrl}" alt="">` : `<div style="width:100%;height:100%;background:#ddd;display:flex;align-items:center;justify-content:center;font-size:0.6rem;font-weight:700;color:#999">${initial}</div>`}
    </div>
    <span class="af-text">${text}</span>
  `;
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
    await loadTimeline(zoneId);
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
