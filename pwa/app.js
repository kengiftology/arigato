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
async function loadFeed() {
  const zoneId = getZoneFromUrl();
  const url = zoneId ? `${API}/maintenance/zone/${zoneId}` : `${API}/maintenance`;
  const res = await fetch(url);
  const records = await res.json();

  // 自動ありがとう（フィードを見た = その場所を使った）
  records.slice(0, 5).forEach(r =>
    fetch(`${API}/thanks/${r.id}/auto`, { method: "POST" }).catch(() => {})
  );

  const list = document.getElementById("feedList");
  if (records.length === 0) {
    list.innerHTML = `<div class="empty">手伝いの記録はまだありません</div>`;
    return;
  }

  list.innerHTML = records.map(r => cardHtml(r)).join("");
}

function cardHtml(r) {
  const initial = r.person_name ? r.person_name[0] : "？";
  const photoSrc = r.after_photo || r.before_photo || "";
  const label = r.after_photo ? "AFTER" : "BEFORE";
  const thanksLabel = r.thanks_count > 0 ? `${r.thanks_count}` : "ありがとう";
  const btnClass = r.thanks_count > 0 ? "btn-thanks first" : "btn-thanks";

  return `
    <div class="card" id="card-${r.id}">
      <div class="card-header">
        <div class="avatar">
          <span>${initial}</span>
        </div>
        <div>
          <div class="card-person">${r.person_name}</div>
          <div class="card-time">${formatTime(r.created_at)}</div>
        </div>
      </div>
      ${photoSrc ? `
      <div class="photo-wrap" id="photowrap-${r.id}">
        <img class="card-photo" src="${photoSrc}" alt="">
        <div class="photo-label">${label}</div>
        <div class="arigato-overlay" id="overlay-${r.id}"></div>
      </div>` : ""}
      <div class="thanks-row">
        <div class="thanks-senders" id="senders-${r.id}"></div>
        <button class="${btnClass}" id="btn-${r.id}" onclick="openThanksModal('${r.id}', '${encodeURIComponent(r.person_name || "")}')">
          ${thanksLabel}
        </button>
      </div>
    </div>
  `;
}

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

  const photoEl = document.getElementById("modalPersonPhoto");
  photoEl.innerHTML = `<span style="font-size:1.4rem;font-weight:700;color:#999">${initial}</span>`;

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
  triggerFloat(id);

  await fetch(`${API}/thanks/${id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sender_name: getName() || "", message: msg })
  });
}

function closeModal() {
  document.getElementById("modalOverlay").classList.add("hidden");
  pendingThanksId = null;
}

// ── ありがとうフロートアニメーション ─────────
function triggerFloat(recordId) {
  const overlay = document.getElementById(`overlay-${recordId}`);
  if (!overlay) return;
  const user = getCurrentUser();
  const photoUrl = user ? user.photo_url : null;
  const initial = user ? user.last_name[0] : "？";

  for (let i = 0; i < 3; i++) {
    setTimeout(() => {
      const pill = document.createElement("div");
      pill.className = "af";
      pill.style.left = `${15 + Math.random() * 55}%`;
      pill.style.animationDelay = "0s";
      pill.innerHTML = `
        <div class="af-face">
          ${photoUrl ? `<img src="${photoUrl}" alt="">` : `<div style="width:100%;height:100%;background:#ddd;display:flex;align-items:center;justify-content:center;font-size:0.6rem;font-weight:700">${initial}</div>`}
        </div>
        <span class="af-text">ありがとう</span>
      `;
      overlay.appendChild(pill);
      setTimeout(() => pill.remove(), 5100);
    }, i * 600);
  }
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

async function openPostModal(mode) {
  const zoneId = getZoneFromUrl();

  if (zoneId) {
    currentZoneId = zoneId;
    document.getElementById("zoneSelectWrap").style.display = "none";
  } else {
    currentZoneId = null;
    document.getElementById("zoneSelectWrap").style.display = "block";
    await populateZoneSelect();
  }

  const title = mode === "before" ? "手伝う前の様子を記録する" : "手伝った後の様子を記録する";
  document.getElementById("postModalTitle").textContent = title;

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

// ── ユーティリティ ────────────────────────
function formatTime(iso) {
  const diff = Math.floor((Date.now() - new Date(iso)) / 60000);
  if (diff < 1)    return "たった今";
  if (diff < 60)   return `${diff}分前`;
  if (diff < 1440) return `${Math.floor(diff / 60)}時間前`;
  return `${Math.floor(diff / 1440)}日前`;
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

  await loadFeed();
  await registerPush();
}

// ── 起動 ──────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  const user = getCurrentUser();
  if (user) {
    updateHeaderAvatar(user);
  }
  init();
});
