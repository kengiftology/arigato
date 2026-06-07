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
  document.getElementById("confirmBtn").disabled = true;
  document.getElementById("confirmBtn").classList.remove("primary");
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
          ${u.photo_url ? `<img src="${u.photo_url}" alt="">` : `<div style="width:100%;height:100%;background:#ddd;display:flex;align-items:center;justify-content:center;font-size:1.2rem">👤</div>`}
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
  document.getElementById("headerName").textContent = getName();
  registerPush();
  if (pendingAction) { const fn = pendingAction; pendingAction = null; fn(); }
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

  // アプリからの自動ありがとう（フィードを見た = 整備された場所を使った）
  records.slice(0, 5).forEach(r =>
    fetch(`${API}/thanks/${r.id}/auto`, { method: "POST" }).catch(() => {})
  );

  const list = document.getElementById("feedList");
  if (records.length === 0) {
    list.innerHTML = `<div class="empty">📷<br>手伝いの記録はまだありません</div>`;
    return;
  }

  list.innerHTML = records.map(r => `
    <div class="card">
      <div class="card-meta">
        <span class="person">🌱 ${r.person_name}</span>
        <span class="zone-tag">${r.zone_name}</span>
      </div>
      <div class="time">${formatTime(r.created_at)}</div>
      ${photoHtml(r)}
      <div class="thanks-area">
        <button class="thanks-btn" onclick="sendThanks('${r.id}', this)">
          ありがとう 🙏
        </button>
        <div class="thanks-count" id="count-${r.id}">
          ${r.thanks_count > 0 ? `${r.thanks_count}件のありがとう` : ""}
        </div>
      </div>
    </div>
  `).join("");
}

function photoHtml(r) {
  if (!r.before_photo && !r.after_photo) return "";
  if (r.before_photo && r.after_photo) {
    return `
      <div class="before-after">
        <div class="photo-wrap">
          <span class="label">Before</span>
          <img src="${r.before_photo}">
        </div>
        <div class="photo-wrap">
          <span class="label">After</span>
          <img src="${r.after_photo}">
        </div>
      </div>`;
  }
  const src = r.after_photo || r.before_photo;
  return `<img class="card-photo" src="${src}">`;
}

// ── ありがとう（手動） ─────────────────────
function sendThanks(id, btn) {
  requireAuth(() => _sendThanks(id, btn), "thanks");
}
async function _sendThanks(id, btn) {
  btn.disabled = true;
  btn.textContent = "ありがとう ✓";
  btn.style.background = "#e0f5ea";
  await fetch(`${API}/thanks/${id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sender_name: getName() || "" })
  });
  const el = document.getElementById(`count-${id}`);
  const n = parseInt(el.textContent) || 0;
  el.textContent = `${n + 1}件のありがとう`;
}

// ── モード選択 ────────────────────────────
let currentZoneId = null;
let currentZoneName = "";
let currentMode = "after"; // "before" | "after"

function openModeSelect() {
  requireAuth(() => _openModeSelect(), "post");
}
function _openModeSelect() {
  const zoneId = getZoneFromUrl();
  const pending = zoneId ? localStorage.getItem(`pending_${zoneId}`) : null;
  if (pending) {
    // 未完了があれば直接AFTERへ
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
  _openModal("before");
}

function startAfter() {
  document.getElementById("modeOverlay").classList.add("hidden");
  currentMode = "after";
  _openModal("after");
}

async function _openModal(mode) {
  const zoneId = getZoneFromUrl();

  if (zoneId) {
    currentZoneId = zoneId;
    document.getElementById("zoneSelectWrap").style.display = "none";
  } else {
    currentZoneId = null;
    document.getElementById("zoneSelectWrap").style.display = "block";
    await populateZoneSelect();
  }

  if (mode === "before") {
    document.getElementById("modalTitle").textContent = "📍 手伝う前の様子";
    document.getElementById("beforeBox").style.display = "flex";
    document.getElementById("afterBox").style.display = "none";
  } else {
    document.getElementById("modalTitle").textContent = "📍 手伝った後の様子";
    document.getElementById("beforeBox").style.display = "none";
    document.getElementById("afterBox").style.display = "flex";
  }

  document.getElementById("modalOverlay").classList.remove("hidden");
}

async function populateZoneSelect() {
  const res = await fetch(`${API}/zones`);
  const zones = await res.json();
  const sel = document.getElementById("modalZoneSelect");
  sel.innerHTML = `<option value="">場所を選んでください</option>` +
    zones.map(z => `<option value="${z.id}">${z.name}</option>`).join("");
}

function closeModal() {
  document.getElementById("modalOverlay").classList.add("hidden");
  // リセット
  ["beforePhoto","afterPhoto"].forEach(id => document.getElementById(id).value = "");
  ["beforeImg","afterImg"].forEach(id => {
    const el = document.getElementById(id);
    el.classList.add("hidden");
    el.src = "";
  });
  document.getElementById("beforeBox").querySelector("span").style.display = "";
  document.getElementById("afterBox").querySelector("span").style.display = "";
}

function previewPhoto(input, boxId, imgId) {
  if (!input.files || !input.files[0]) return;
  const img = document.getElementById(imgId);
  img.src = URL.createObjectURL(input.files[0]);
  img.classList.remove("hidden");
  document.querySelector(`#${boxId} span`).style.display = "none";
  document.querySelector(`#${boxId} .icon`).style.display = "none";
}

// ── 投稿 ──────────────────────────────────
async function submitPost() {
  const name = getName();
  const afterFile  = document.getElementById("afterPhoto").files[0];
  const beforeFile = document.getElementById("beforePhoto").files[0];
  const photoFile  = currentMode === "before" ? beforeFile : afterFile;

  if (!photoFile) {
    alert("写真を撮ってください");
    return;
  }

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

  const pendingKey = `pending_${currentZoneId}`;
  const pendingId  = localStorage.getItem(pendingKey);

  if (currentMode === "after" && pendingId) {
    // 既存のin_progressレコードを完了させる
    const form = new FormData();
    form.append("after_photo", afterFile);
    await fetch(`${API}/maintenance/${pendingId}/complete`, { method: "PATCH", body: form });
    localStorage.removeItem(pendingKey);
  } else {
    // 新規レコード作成
    const form = new FormData();
    form.append("zone_id",     currentZoneId);
    form.append("person_name", name);
    form.append("content",     "");
    if (currentMode === "before") {
      form.append("before_photo", beforeFile);
    } else {
      form.append("after_photo", afterFile);
    }
    const res  = await fetch(`${API}/maintenance`, { method: "POST", body: form });
    const data = await res.json();

    // BEFOREのrecord_idをlocalStorageに保存
    if (currentMode === "before" && data.id) {
      localStorage.setItem(pendingKey, data.id);
    }
  }

  // AFTER完了時だけwelcomeありがとうを送る
  if (currentMode === "after") {
    fetch(`${API}/thanks/welcome`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ person_name: name })
    }).catch(() => {});
  }

  closeModal();
  btn.disabled = false;
  btn.textContent = "記録する";
  loadFeed();
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
    const res = await fetch(`${API}/zones`);
    const zones = await res.json();
    const zone = zones.find(z => z.id === zoneId);
    if (zone) {
      currentZoneName = zone.name;
      const header = document.getElementById("zoneHeader");
      header.classList.remove("hidden");
      header.innerHTML = `📍 <strong>${zone.name}</strong>`;
    }
  }

  await loadFeed();
  await registerPush();
}

// ── 起動 ──────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  const user = getCurrentUser();
  if (user) {
    document.getElementById("headerName").textContent = getName();
  }
  init();
});
