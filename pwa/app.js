const API = "";

// ── 名前管理 ──────────────────────────────
function getName() { return localStorage.getItem("arigato_name"); }

function saveName() {
  const name = document.getElementById("nameInput").value.trim();
  if (!name) return;
  localStorage.setItem("arigato_name", name);
  document.getElementById("nameScreen").style.display = "none";
  document.getElementById("headerName").textContent = name;
  init();
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
    list.innerHTML = `<div class="empty">📷<br>まだ記録がありません<br>QRを読んで整備を記録しよう</div>`;
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
          <img src="${API}/maintenance/photo/${r.before_photo}">
        </div>
        <div class="photo-wrap">
          <span class="label">After</span>
          <img src="${API}/maintenance/photo/${r.after_photo}">
        </div>
      </div>`;
  }
  const src = r.after_photo || r.before_photo;
  return `<img class="card-photo" src="${API}/maintenance/photo/${src}">`;
}

// ── ありがとう（手動） ─────────────────────
async function sendThanks(id, btn) {
  btn.disabled = true;
  btn.textContent = "ありがとう ✓";
  btn.style.background = "#e0f5ea";
  await fetch(`${API}/thanks/${id}`, { method: "POST" });
  const el = document.getElementById(`count-${id}`);
  const n = parseInt(el.textContent) || 0;
  el.textContent = `${n + 1}件のありがとう`;
}

// ── モーダル ──────────────────────────────
let currentZoneId = null;
let currentZoneName = "";

async function openModal() {
  const zoneId = getZoneFromUrl();

  if (zoneId) {
    // QRからのアクセス → ゾーン固定
    currentZoneId = zoneId;
    document.getElementById("modalTitle").textContent = `📍 ${currentZoneName} を良くしました`;
    document.getElementById("zoneSelectWrap").style.display = "none";
  } else {
    // 直接アクセス → ゾーン選択を表示
    currentZoneId = null;
    document.getElementById("modalTitle").textContent = "📍 どこを良くしましたか？";
    document.getElementById("zoneSelectWrap").style.display = "block";
    await populateZoneSelect();
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
  const afterFile = document.getElementById("afterPhoto").files[0];
  const beforeFile = document.getElementById("beforePhoto").files[0];

  if (!afterFile && !beforeFile) {
    alert("写真を1枚以上撮ってください");
    return;
  }

  const btn = document.getElementById("postBtn");
  btn.disabled = true;
  btn.textContent = "送信中…";

  // ゾーン選択（QRなしの場合）
  if (!currentZoneId) {
    currentZoneId = document.getElementById("modalZoneSelect").value;
  }
  if (!currentZoneId) {
    alert("場所を選んでください");
    btn.disabled = false;
    btn.textContent = "記録する";
    return;
  }

  const form = new FormData();
  form.append("zone_id", currentZoneId);
  form.append("person_name", name);
  form.append("content", "");  // テキストなし
  if (beforeFile) form.append("before_photo", beforeFile);
  if (afterFile)  form.append("after_photo",  afterFile);

  await fetch(`${API}/maintenance`, { method: "POST", body: form });

  // 投稿直後にアプリからありがとうを送る
  fetch(`${API}/thanks/welcome`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_name: name })
  }).catch(() => {});

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
  const name = getName();
  if (!name) {
    document.getElementById("nameScreen").style.display = "flex";
  } else {
    document.getElementById("nameScreen").style.display = "none";
    document.getElementById("headerName").textContent = name;
    init();
  }
});
