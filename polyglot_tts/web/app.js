"use strict";

// Everything the server sends is inserted as text, never as markup: voice
// names, the last-synthesis fields and error details can come from any client
// on the network, and this page holds the management token.
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ── password (login, first-run setup, change) ─────────────────────────────
// Reading and listening work without logging in. Changing anything (voices,
// settings, dictionary, restart) needs the management password: after login
// it is kept in localStorage and sent as "Authorization: Bearer <password>".
// While the server has no password yet it offers "Set password" instead of
// "Login" for a few minutes after its start (see docs/WEB_UI.md).
const TOKEN_KEY = "polyglot_ui_token";   // key kept so existing logins survive
let TOKEN = "";
let LOGGED_IN = false;
let AUTH_MODE = "";        // from /api/ui/status: setup | password | env | memory | closed
let SETUP_LEFT = 0;        // seconds the setup window stays open
let MIN_PW = 10;

function storedToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}
function storeToken(tok) {
  try {
    if (tok) localStorage.setItem(TOKEN_KEY, tok);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) { /* private mode: the login lives for this page only */ }
}
TOKEN = storedToken();

// Old bookmarks may still carry "?token=…". Take it over once and remove it
// from the address bar; this page never builds such URLs itself.
(function adoptUrlToken() {
  let url;
  try { url = new URL(window.location.href); } catch (e) { return; }
  const tok = url.searchParams.get("token");
  if (tok === null) return;
  url.searchParams.delete("token");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
  if (tok && !TOKEN) TOKEN = tok;
})();

function authHeaders(extra) {
  const h = extra || {};
  if (TOKEN) h["Authorization"] = "Bearer " + TOKEN;
  return h;
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = authHeaders(opts.headers);
  const res = await fetch(path, opts);
  if (res.status === 401) {
    const hadToken = !!TOKEN;
    setLoggedOut();
    showAuth(hadToken ? "The stored password is no longer valid." : "");
    throw new Error("unauthorized");
  }
  return res;
}

async function errorDetail(res) {
  try { const j = await res.json(); if (j && typeof j.detail === "string") return j.detail; }
  catch (e) { /* no JSON body */ }
  return "Error " + res.status;
}

// true / false = the server accepted / rejected the password; "limited" =
// too many failed attempts; null = no answer.
async function checkPassword(pw) {
  try {
    const r = await fetch("/api/ui/session", { headers: { "Authorization": "Bearer " + pw } });
    if (r.status === 200) return true;
    if (r.status === 401) return false;
    if (r.status === 429) return "limited";
    return null;
  } catch (e) { return null; }
}

// The password travels in an HTTP header, so it is limited to plain ASCII.
function passwordProblem(pw, repeat) {
  if (pw.length < MIN_PW) return `At least ${MIN_PW} characters.`;
  if (!/^[\x20-\x7E]+$/.test(pw) || pw !== pw.trim()) {
    return "Letters, digits and common symbols only (no umlauts, accents or emoji), " +
      "no space at the start or end.";
  }
  if (pw !== repeat) return "The two passwords differ.";
  return "";
}

function noPasswordYet() { return AUTH_MODE === "setup" || AUTH_MODE === "closed"; }

function renderAuth() {
  const el = (id) => document.getElementById(id);
  el("auth-state").textContent = LOGGED_IN ? "Logged in"
    : (noPasswordYet() ? "No password set" : "Read-only");
  el("login-btn").textContent = noPasswordYet() ? "Set password" : "Login";
  el("login-btn").hidden = LOGGED_IN;
  el("logout-btn").hidden = !LOGGED_IN;
  document.body.classList.toggle("logged-in", LOGGED_IN);
  // settings tab: password card
  el("settings-login-btn").textContent = el("login-btn").textContent;
  el("settings-login-btn").hidden = LOGGED_IN;
  el("settings-logout-btn").hidden = !LOGGED_IN;
  el("change-form").hidden = !(LOGGED_IN && AUTH_MODE === "password");
  let note = "Changing voices or settings needs the management password.";
  if (AUTH_MODE === "env") {
    note = "The password is set on the server through POCKET_TTS_UI_TOKEN; change it there.";
  } else if (AUTH_MODE === "memory") {
    note = "The server cannot store a password (config directory not writable); " +
      "it uses a one-time password from its log.";
  } else if (AUTH_MODE === "password") {
    note = LOGGED_IN ? "Change the management password here."
      : "Log in to change the management password.";
    note += " Forgotten? Delete ui_password.json in the server's config directory " +
      "and restart the server, then set a new one here.";
  } else if (noPasswordYet()) {
    note = "No management password has been set yet.";
  }
  el("password-note").textContent = note;
}

function setLoggedIn(pw) {
  TOKEN = pw;
  LOGGED_IN = true;
  storeToken(pw);
  renderAuth();
}

function setLoggedOut() {
  TOKEN = "";
  LOGGED_IN = false;
  storeToken("");
  renderAuth();
}

function loginHint() {
  if (AUTH_MODE === "env") {
    return "The password is the value of POCKET_TTS_UI_TOKEN set for this server.";
  }
  if (AUTH_MODE === "memory") {
    return "The server could not store a password; it wrote a one-time password " +
      "to its log at start.";
  }
  return "Forgotten? Delete ui_password.json in the server's config directory and " +
    "restart the server to set a new one.";
}

// Show the form that fits the server state: set a password, log in, or the
// notice that setup is not possible right now.
function showAuth(message) {
  const el = (id) => document.getElementById(id);
  const which = AUTH_MODE === "setup" ? "setup-form"
    : (AUTH_MODE === "closed" ? "setup-closed" : "login-form");
  ["login-form", "setup-form", "setup-closed"].forEach((id) => { el(id).hidden = id !== which; });
  if (which === "setup-form") {
    const mins = Math.max(1, Math.ceil(SETUP_LEFT / 60));
    el("setup-hint").textContent = `Possible for about ${mins} more minute(s) after ` +
      "the server start. Whoever sets the password first controls the management " +
      "functions — do it now.";
    el("setup-error").textContent = message || "";
  } else if (which === "login-form") {
    el("login-hint").textContent = loginHint();
    el("login-error").textContent = message || "";
  }
  const wasHidden = el("auth-panel").hidden;
  el("auth-panel").hidden = false;
  if (wasHidden) {
    const first = el(which).querySelector('input[type="password"]');
    if (first) first.focus();
  }
}

function hideAuth() {
  document.getElementById("auth-panel").hidden = true;
  ["login-password", "setup-password", "setup-repeat"].forEach((id) => {
    const input = document.getElementById(id);
    input.value = "";
    input.type = "password";
  });
  document.querySelectorAll("#auth-panel [data-toggle]").forEach((b) => {
    b.setAttribute("aria-pressed", "false");
    b.textContent = "Show";
  });
  ["login-error", "setup-error"].forEach((id) => { document.getElementById(id).textContent = ""; });
}

// Gate for every management action: opens login / setup when not logged in.
function requireLogin() {
  if (LOGGED_IN) return true;
  showAuth(AUTH_MODE === "setup" ? "Set a password first."
    : "Log in to change voices or settings.");
  return false;
}

async function doLogin() {
  const pw = document.getElementById("login-password").value.trim();
  const err = document.getElementById("login-error");
  if (!pw) { err.textContent = "Enter the password."; return; }
  err.textContent = "Checking…";
  const ok = await checkPassword(pw);
  if (ok === true) { setLoggedIn(pw); hideAuth(); }
  else if (ok === false) err.textContent = "Wrong password.";
  else if (ok === "limited") err.textContent = "Too many failed attempts — wait a minute.";
  else err.textContent = "Server not reachable — try again.";
}

async function doSetup() {
  const pw = document.getElementById("setup-password").value;
  const err = document.getElementById("setup-error");
  const problem = passwordProblem(pw, document.getElementById("setup-repeat").value);
  if (problem) { err.textContent = problem; return; }
  err.textContent = "Saving…";
  let r;
  try {
    r = await fetch("/api/ui/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
  } catch (e) { err.textContent = "Server not reachable — try again."; return; }
  if (r.ok) {
    AUTH_MODE = "password";
    setLoggedIn(pw);
    hideAuth();
    return;
  }
  err.textContent = await errorDetail(r);
  if (r.status === 403) refreshStatus();   // someone else was first, or the window is over
}

async function doChangePassword() {
  const el = (id) => document.getElementById(id);
  const st = el("change-status");
  const current = el("change-current").value.trim();
  const pw = el("change-new").value;
  if (!requireLogin()) return;
  if (!current) { st.textContent = "Enter the current password."; return; }
  const problem = passwordProblem(pw, el("change-repeat").value);
  if (problem) { st.textContent = problem; return; }
  st.textContent = "Saving…";
  let r;
  try {
    r = await api("/api/ui/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: current, new_password: pw }),
    });
  } catch (e) { st.textContent = "Not changed."; return; }
  if (!r.ok) { st.textContent = await errorDetail(r); return; }
  setLoggedIn(pw);                          // keep this browser logged in
  ["change-current", "change-new", "change-repeat"].forEach((id) => { el(id).value = ""; });
  st.textContent = "Password changed. Other devices have to log in again.";
}

[["login-form", doLogin], ["setup-form", doSetup], ["change-form", doChangePassword]]
  .forEach(([id, handler]) => {
    document.getElementById(id).addEventListener("submit", (e) => { e.preventDefault(); handler(); });
  });
document.querySelectorAll("[data-auth-cancel]").forEach((b) => { b.onclick = hideAuth; });
document.getElementById("login-btn").onclick = () => showAuth("");
document.getElementById("settings-login-btn").onclick = () => showAuth("");
document.getElementById("logout-btn").onclick = () => { setLoggedOut(); hideAuth(); };
document.getElementById("settings-logout-btn").onclick = () => { setLoggedOut(); hideAuth(); };

// show / hide what was typed (one button can switch several fields)
document.querySelectorAll("[data-toggle]").forEach((btn) => {
  btn.onclick = () => {
    const show = btn.getAttribute("aria-pressed") !== "true";
    btn.dataset.toggle.split(",").forEach((id) => {
      document.getElementById(id).type = show ? "text" : "password";
    });
    btn.setAttribute("aria-pressed", show ? "true" : "false");
    btn.textContent = show ? "Hide" : "Show";
  };
});

// On load: a stored password is only kept if the server still accepts it.
async function restoreLogin() {
  if (!TOKEN) { renderAuth(); return; }
  const ok = await checkPassword(TOKEN);
  if (ok === true) setLoggedIn(TOKEN);
  else if (ok === false) setLoggedOut();
  else renderAuth();          // no clear answer: keep it, stay read-only for now
}

// ── tabs ──────────────────────────────────────────────────────────────────
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tab").forEach((t) => (t.hidden = true));
    document.getElementById(btn.dataset.tab).hidden = false;
  };
});

// ── dashboard ───────────────────────────────────────────────────────────────
async function refreshStatus() {
  try {
    const r = await api("/api/ui/status");
    if (!r.ok) return;
    const s = await r.json();
    document.getElementById("version").textContent = "v" + s.version;
    const modeBefore = AUTH_MODE;
    AUTH_MODE = s.auth_mode || "";
    SETUP_LEFT = s.setup_seconds_left || 0;
    MIN_PW = s.min_password_length || MIN_PW;
    if (AUTH_MODE !== modeBefore) {
      renderAuth();
      // keep an open panel in line with the server (e.g. setup window over)
      if (!LOGGED_IN && !document.getElementById("auth-panel").hidden) showAuth("");
    }
    const langs = s.languages.map((l) => l.bcp47).join(", ") || "—";
    document.getElementById("status-box").innerHTML = `
      <div class="kv"><span>Device</span><span>${esc(s.device)}</span></div>
      <div class="kv"><span>Languages</span><span>${esc(langs)}</span></div>
      <div class="kv"><span>Voices</span><span>${esc(s.voice_count)}</span></div>
      <div class="kv"><span>Default voice</span><span>${esc(s.default_voice)}</span></div>
      <div class="kv"><span>Uptime</span><span>${fmtUptime(s.uptime_s)}</span></div>
      <div class="kv"><span>Management</span><span>${noPasswordYet() ? "no password set yet" : "password required"}</span></div>`;
    const t = s.last_synth || {};
    const rtf = t.audio_ms && t.synth_ms ? (t.audio_ms / t.synth_ms).toFixed(2) : "—";
    document.getElementById("timing-box").innerHTML = t.ts
      ? `<div class="kv"><span>Voice</span><span>${esc(t.voice)} (${esc(t.language)})</span></div>
         <div class="kv"><span>Audio</span><span>${(t.audio_ms/1000).toFixed(1)} s</span></div>
         <div class="kv"><span>Synth</span><span>${(t.synth_ms/1000).toFixed(2)} s</span></div>
         <div class="kv"><span>RTF</span><span>${rtf}×</span></div>`
      : '<span class="muted">No synthesis yet.</span>';
    renderEndpoints(s);
    LOADED_LANGS = (s.languages || []).map((l) => l.bcp47);
  } catch (e) { /* ignore */ }
}

function fmtUptime(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// Endpoints: the HTTP row reflects the REAL address you're on (the browser
// knows it). Wyoming/timing only know their in-container bind port — Docker
// may publish them on a different host port, so we label them honestly.
function renderEndpoints(s) {
  const eps = s.endpoints || [];
  const browserHost = window.location.host;       // e.g. spark:11201 (real)
  const rows = eps.map((e) => {
    const off = e.port === "off";
    const isHttp = e.name.indexOf("HTTP") !== -1;
    if (off) {
      return `<div class="kv"><span>${esc(e.name)}</span><span class="muted">disabled</span></div>`;
    }
    if (isHttp) {
      return `<div class="kv"><span>${esc(e.name)}</span><span class="port">${esc(browserHost)}</span></div>`;
    }
    // Wyoming / timing: in-container port; host mapping may differ.
    return `<div class="kv"><span>${esc(e.name)}</span>
      <span class="port">container :${esc(e.port)}</span></div>`;
  });
  rows.push('<p class="field-help">HTTP shows the address you\'re connected on. ' +
    'Wyoming/timing show the in-container port — if you remapped ports in Docker ' +
    '(e.g. <code>11200:10200</code>), use your host port, not the container port.</p>');
  document.getElementById("endpoints-box").innerHTML = rows.join("");
}

let LOADED_LANGS = [];
let VOICE_NAMES = [];
let CHECKPOINTS = [];

// ── voices ──────────────────────────────────────────────────────────────────
// One upload slot per language the engine can speak, plus an untagged fallback.
const LANG_SLOTS = [
  { key: "de", label: "Deutsch" },
  { key: "en", label: "English" },
  { key: "fr", label: "Français" },
  { key: "it", label: "Italiano" },
  { key: "es", label: "Español" },
  { key: "pt", label: "Português" },
  { key: "nl", label: "Nederlands" },
];
const SLOTS = {};        // lang-key -> {blob, filename}
let FALLBACK_KEY = "";   // which slot's audio also serves the untagged fallback
let AVAILABLE_LANGS = [];

// A chip for EVERY available language: solid = own audio, faint = shared
// fallback, struck = not covered by this voice.
function langBadges(v) {
  const have = {};
  (v.languages || []).forEach((l) => { have[l.bcp47] = l.dedicated; });
  const set = AVAILABLE_LANGS.length ? AVAILABLE_LANGS : Object.keys(have);
  const chips = set.map((b) => {
    const has = b in have;
    const cls = !has ? "miss" : (have[b] ? "ded" : "");
    const t = !has ? "not available for this voice"
      : (have[b] ? "own reference audio" : "shared fallback audio");
    return `<span class="lng ${cls}" title="${t}">${esc(b)}</span>`;
  }).join("");
  const star = v.per_language
    ? ` <span class="poly" title="multilingual — a separate reference per language">◆</span>` : "";
  return `<span class="langs">${chips}${star}</span>`;
}

async function refreshVoices() {
  const r = await api("/v1/audio/voices");
  if (!r.ok) return;
  const data = await r.json();
  const voices = data.voices || [];
  if (data.languages && data.languages.length) AVAILABLE_LANGS = data.languages;
  VOICE_NAMES = voices.map((v) => v.name);
  if (typeof validateGenerate === "function") validateGenerate();
  document.getElementById("voice-count").textContent = `(${voices.length})`;
  // populate test-voice + voice list
  const sel = document.getElementById("test-voice");
  sel.innerHTML = voices.map((v) => `<option>${esc(v.name)}</option>`).join("");
  document.getElementById("voice-list").innerHTML = voices
    .map((v) => `<div class="voice-item">
        <span class="name">${esc(v.name)}</span>
        ${langBadges(v)}
        <span class="kind">${esc(v.kind)}</span>
        ${v.kind === "custom" ? `<button data-del="${esc(v.name)}">delete</button>` : ""}
      </div>`).join("");
  document.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = async () => {
      if (!requireLogin()) return;
      if (!confirm(`Delete voice "${b.dataset.del}"?`)) return;
      let r;
      try {
        r = await api("/v1/audio/voices/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
      } catch (e) { return; }
      if (!r.ok) alert("Not deleted: " + (await errorDetail(r)));
      setTimeout(refreshVoices, 500);
    };
  });
}

async function currentVoiceNames() {
  try {
    const r = await api("/v1/audio/voices");
    if (!r.ok) return [];
    return ((await r.json()).voices || []).map((v) => v.name);
  } catch (e) { return []; }
}

async function uploadOne(blob, filename, name, language) {
  const fd = new FormData();
  fd.append("file", blob, filename);
  fd.append("name", name);
  if (language) fd.append("language", language);
  return api("/v1/audio/voices", { method: "POST", body: fd });
}

function nameOk(s) {
  return s && !s.startsWith(".") && s.indexOf("..") === -1 &&
    /^[A-Za-z0-9_.-]+$/.test(s);
}

function slotEl(key) { return document.getElementById("slot-status-" + (key || "fb")); }

function fbRadio(key) { return document.querySelector(`input.fb[value="${key}"]`); }

function setSlot(key, blob, filename) {
  SLOTS[key] = { blob, filename };
  const st = slotEl(key);
  if (st) { st.textContent = "✓ " + filename; st.classList.add("set"); }
  const rb = fbRadio(key);
  if (rb) { rb.disabled = false; if (!FALLBACK_KEY) { rb.checked = true; FALLBACK_KEY = key; } }
  // pre-fill the voice name from the first file's stem (minus any lang tag)
  const nm = document.getElementById("upload-name");
  if (!nm.value.trim()) {
    nm.value = filename.replace(/\.[^.]+$/, "").replace(/\.(de|en|fr|it|es|pt)$/i, "");
  }
  validateGenerate();
}

function clearSlot(key) {
  delete SLOTS[key];
  const st = slotEl(key);
  if (st) { st.textContent = "—"; st.classList.remove("set"); }
  const rb = fbRadio(key);
  if (rb) { rb.disabled = true; rb.checked = false; }
  if (FALLBACK_KEY === key) {            // re-point the fallback to another filled slot
    FALLBACK_KEY = Object.keys(SLOTS)[0] || "";
    const nrb = FALLBACK_KEY && fbRadio(FALLBACK_KEY);
    if (nrb) nrb.checked = true;
  }
  validateGenerate();
}

function validateGenerate() {
  const name = document.getElementById("upload-name").value.trim();
  const btn = document.getElementById("generate-btn");
  const hint = document.getElementById("name-hint");
  const have = Object.keys(SLOTS);
  let ok = true, msg = "";
  if (!name) { ok = false; msg = "Enter a voice name."; }
  else if (!nameOk(name)) { ok = false; msg = "Allowed: letters, digits, _ - . (no leading dot)."; }
  else if (VOICE_NAMES.includes(name)) { ok = false; msg = `"${name}" already exists — pick another name.`; }
  else if (!have.length) { ok = false; msg = "Add audio for at least one language."; }
  else {
    const fb = FALLBACK_KEY ? ` · fallback: ${FALLBACK_KEY}` : "";
    msg = `Ready: ${have.join(", ")}${fb}.`;
  }
  if (btn) btn.disabled = !ok;
  if (hint) { hint.textContent = msg; hint.style.color = ok ? "var(--ok)" : "var(--muted)"; }
}

// recording into a specific slot (one recorder, retargeted per slot button)
let mediaRecorder = null, chunks = [];
const micUnavailable = !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
    !window.isSecureContext;
if (micUnavailable) document.getElementById("mic-hint").hidden = false;

async function toggleRecord(key, btn) {
  if (mediaRecorder && mediaRecorder.state === "recording") { mediaRecorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: "audio/webm" });
      setSlot(key, blob, "recording" + (key ? "." + key : "") + ".webm");
      btn.classList.remove("recording"); btn.textContent = "● rec";
    };
    mediaRecorder.start();
    btn.classList.add("recording"); btn.textContent = "■ stop";
  } catch (e) {
    document.getElementById("mic-hint").hidden = false;
  }
}

// build the per-language slot rows; one can be marked as the fallback
function buildSlots() {
  const host = document.getElementById("lang-slots");
  if (!host) return;
  host.innerHTML =
    `<div class="slot slot-head">
       <span class="fb-col" title="this recording is reused for the languages you don't fill">fallback</span>
       <span class="slot-lang">language</span></div>` +
    LANG_SLOTS.map((s) => `<div class="slot">
      <input type="radio" name="fb-radio" class="fb" value="${s.key}" disabled
             title="use this recording as the fallback for the other languages" />
      <span class="slot-lang">${s.label} <code>${s.key}</code></span>
      <label class="link">file<input type="file" accept="audio/*" hidden data-slot="${s.key}" /></label>
      <button class="rec" data-rec="${s.key}"${micUnavailable ? " disabled" : ""}>● rec</button>
      <span class="slot-status" id="slot-status-${s.key}">—</span>
      <button class="clr" data-clr="${s.key}" title="clear">✕</button>
    </div>`).join("");
  host.querySelectorAll('input[type="file"]').forEach((inp) => {
    inp.onchange = () => { const f = inp.files[0]; if (f) setSlot(inp.dataset.slot, f, f.name); inp.value = ""; };
  });
  host.querySelectorAll("[data-clr]").forEach((b) => { b.onclick = () => clearSlot(b.dataset.clr); });
  host.querySelectorAll("[data-rec]").forEach((b) => { b.onclick = () => toggleRecord(b.dataset.rec, b); });
  host.querySelectorAll("input.fb").forEach((rb) => {
    rb.onchange = () => { FALLBACK_KEY = rb.value; validateGenerate(); };
  });
}

document.getElementById("upload-name").addEventListener("input", validateGenerate);

document.getElementById("generate-btn").onclick = async () => {
  const name = document.getElementById("upload-name").value.trim();
  const keys = Object.keys(SLOTS);
  if (!name || !keys.length) return;
  if (!requireLogin()) return;
  const status = document.getElementById("upload-status");
  // Check the password before sending audio: a rejected upload would
  // otherwise be cut off mid-transfer instead of showing the login.
  const valid = await checkPassword(TOKEN);
  if (valid === false) {
    setLoggedOut();
    showAuth("The stored password is no longer valid.");
    return;
  }
  if (valid === "limited") {
    status.textContent = "Too many failed attempts — wait a minute and try again.";
    return;
  }
  document.getElementById("generate-btn").disabled = true;
  status.innerHTML = "⏳ Uploading…";
  for (const key of keys) {
    const s = SLOTS[key];
    let r;
    try { r = await uploadOne(s.blob, s.filename, name, key); }
    catch (e) { status.innerHTML = "❌ Upload failed."; validateGenerate(); return; }
    if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) {}
      status.innerHTML = "❌ Error: " + esc(j.detail || r.status); validateGenerate(); return; }
  }
  // the chosen recording also serves as the untagged fallback for any language
  // the user didn't fill (saved as "<name>.<ext>").
  if (FALLBACK_KEY && SLOTS[FALLBACK_KEY]) {
    const s = SLOTS[FALLBACK_KEY];
    try { await uploadOne(s.blob, s.filename, name, ""); } catch (e) {}
  }
  LANG_SLOTS.forEach((s) => clearSlot(s.key));
  FALLBACK_KEY = "";
  status.innerHTML = `⏳ Embedding "<b>${esc(name)}</b>"… ~30 s per language (longer on CPU).`;
  let waited = 0;
  const poll = setInterval(async () => {
    waited += 3;
    const now = await currentVoiceNames();
    if (now.includes(name)) {
      clearInterval(poll);
      status.innerHTML = `✅ Voice "<b>${esc(name)}</b>" is ready.`;
      refreshVoices();
      document.getElementById("upload-name").value = "";
    } else if (waited >= 120) {
      clearInterval(poll);
      status.innerHTML = `⚠️ "<b>${esc(name)}</b>" didn't appear after 120 s. Check the server log — ` +
        `the audio may be too short/noisy, or (for cloning) you may need an HF token in Settings.`;
      refreshVoices();
    } else {
      status.innerHTML = `⏳ Embedding "<b>${esc(name)}</b>"… (${waited}s)`;
    }
  }, 3000);
};

buildSlots();

// quick test synth
document.getElementById("test-btn").onclick = async () => {
  const input = document.getElementById("test-text").value;
  const voice = document.getElementById("test-voice").value;
  const info = document.getElementById("test-info");
  info.textContent = "Synthesizing…";
  const r = await api("/v1/audio/speech", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input, voice, response_format: "mp3" }),
  });
  if (!r.ok) { info.textContent = "Error " + r.status; return; }
  const blob = await r.blob();
  const audio = document.getElementById("test-audio");
  audio.src = URL.createObjectURL(blob);
  audio.hidden = false;
  audio.play();
  info.textContent = "";
};

// ── settings ────────────────────────────────────────────────────────────────
function settingField(key, c) {
  const val = c.value == null ? "" : String(c.value);
  const ph = c.placeholder ? ` placeholder="${esc(c.placeholder)}"` : "";
  // Languages: render as checkboxes from the real installed checkpoints,
  // including the lighter (non-24l) variants for weak hardware.
  if (key === "POCKET_TTS_LANGUAGES") {
    const sel = val.split(",").map((x) => x.trim()).filter(Boolean);
    const cps = (CHECKPOINTS && CHECKPOINTS.length)
      ? CHECKPOINTS
      : (c.options || sel).map((o) => ({ checkpoint: o, bcp47: o.slice(0, 2), quality: "" }));
    return `<div class="lang-grid" data-key="${esc(key)}">` +
      cps.map((cp) => `<label class="lang-opt" title="${esc(cp.quality)}">
        <input type="checkbox" value="${esc(cp.checkpoint)}" data-bcp="${esc(cp.bcp47)}"
          ${sel.includes(cp.checkpoint) ? "checked" : ""} />
        <span>${esc(cp.checkpoint)}</span><small>${esc(cp.bcp47)} · ${esc(cp.quality)}</small>
      </label>`).join("") + `</div>`;
  }
  if (c.type === "bool") {
    const on = ["1", "true", "yes", ""].includes(val.toLowerCase()) && val !== "0" && val.toLowerCase() !== "false";
    return `<select data-key="${esc(key)}">
      <option value="true" ${on ? "selected" : ""}>true</option>
      <option value="false" ${!on ? "selected" : ""}>false</option></select>`;
  }
  if (c.type === "select") {
    // Keep an unlisted current value selectable (e.g. env-set "cuda:0").
    // Without this the select silently falls back to the first option and
    // the next save would overwrite the real value with it.
    const opts = c.options.includes(val) ? c.options : [...c.options, val];
    return `<select data-key="${esc(key)}">` +
      opts.map((o) => `<option ${o === val ? "selected" : ""}>${esc(o)}</option>`).join("") +
      `</select>`;
  }
  if (c.type === "voice-select") {
    const opts = VOICE_NAMES.length ? VOICE_NAMES : [val].filter(Boolean);
    return `<select data-key="${esc(key)}">` +
      opts.map((o) => `<option ${o === val ? "selected" : ""}>${esc(o)}</option>`).join("") +
      `</select>`;
  }
  if (c.type === "number") {
    return `<input type="number" data-key="${esc(key)}" value="${esc(val)}"${ph} />`;
  }
  // text — with a datalist of suggestions if options provided
  if (c.options && c.options.length) {
    const listId = "dl-" + key;
    return `<input data-key="${esc(key)}" list="${esc(listId)}" value="${esc(val)}"${ph} />
      <datalist id="${esc(listId)}">${c.options.map((o) => `<option value="${esc(o)}">`).join("")}</datalist>`;
  }
  return `<input data-key="${esc(key)}" value="${esc(val)}"${ph} />`;
}

async function refreshSettings() {
  VOICE_NAMES = await currentVoiceNames();
  const r = await api("/api/ui/config");
  if (!r.ok) return;
  const body = await r.json();
  const cfg = body.config;
  CHECKPOINTS = body.checkpoints || [];
  const form = document.getElementById("settings-form");
  form.innerHTML = "";
  const oneLang = LOADED_LANGS.length === 1;
  Object.keys(cfg).forEach((key) => {
    if (key === "HF_TOKEN") return; // handled in its own card
    const c = cfg[key];
    const badge = c.restart_required ? '<span class="badge">restart</span>' : "";
    let help = esc(c.help || "");
    if (key === "POCKET_TTS_LANGUAGES") {
      help += ' Pick one checkpoint per language. <code>_24l</code> = ' +
        'higher quality (24-layer, slower); the plain name (e.g. ' +
        '<code>german</code>) is the lighter, faster model — better for weak ' +
        'CPUs like a Raspberry Pi. More languages = more RAM.';
    }
    // Contextual hint: auto language-ID does nothing with a single language.
    if (key === "POCKET_TTS_AUTO_LID" && oneLang) {
      help += ' <b>Only one language is loaded</b>, so language detection has ' +
        'no effect — every request uses that language. Load more languages above ' +
        'to make auto-detection meaningful.';
    }
    const row = document.createElement("div");
    row.className = "setting-block";
    row.innerHTML = `<div class="setting-row">
        <label>${esc(key)}${badge}</label>
        ${settingField(key, c)}
      </div>
      ${help ? `<p class="field-help">${help}</p>` : ""}`;
    form.appendChild(row);
  });
  // One checkpoint per language: checking a variant unchecks the others of
  // the same language (loading two for one language wastes RAM and only one
  // is ever used).
  form.querySelectorAll('.lang-grid input[type=checkbox]').forEach((cb) => {
    cb.addEventListener("change", () => {
      if (!cb.checked) return;
      const bcp = cb.dataset.bcp;
      cb.closest(".lang-grid")
        .querySelectorAll(`input[data-bcp="${bcp}"]`)
        .forEach((other) => { if (other !== cb) other.checked = false; });
    });
  });

  // HF token helper links (request model access + create a token), from the
  // backend field metadata so the UI just mirrors what the config layer ships.
  const hfLinks = document.getElementById("hf-links");
  if (hfLinks) {
    const links = (cfg.HF_TOKEN && cfg.HF_TOKEN.links) || [];
    hfLinks.innerHTML = links
      .map((l) => `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label)} ↗</a>`)
      .join(" · ");
  }

  // populate the normalize-preview language dropdown from loaded languages
  const nl = document.getElementById("norm-lang");
  if (nl) {
    const langs = LOADED_LANGS.length ? LOADED_LANGS : ["de", "en", "fr"];
    nl.innerHTML = langs.map((l) => `<option>${esc(l)}</option>`).join("");
  }

  refreshReplacements();
}

document.getElementById("settings-save").onclick = () => saveSettings(false);
document.getElementById("restart-btn").onclick = () => saveSettings(true);

async function saveSettings(restart) {
  if (!requireLogin()) return;
  const updates = {};
  document.querySelectorAll("#settings-form [data-key]").forEach((el) => {
    const key = el.dataset.key;
    if (el.classList.contains("lang-grid")) {
      // collect checked language checkboxes into a comma list
      const sel = Array.from(el.querySelectorAll("input[type=checkbox]:checked"))
        .map((cb) => cb.value);
      updates[key] = sel.join(",");
    } else {
      updates[key] = el.value.trim();
    }
  });
  const st = document.getElementById("settings-status");
  st.textContent = "Saving…";
  let r;
  try {
    r = await api("/api/ui/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates }),
    });
  } catch (e) { st.textContent = "Not saved."; return; }
  let j = {};
  try { j = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok) { st.textContent = "Not saved: " + (j.detail || "error " + r.status); return; }
  const need = j.restart_required_for || [];
  st.textContent = need.length
    ? `Saved. Restart required for: ${need.join(", ")}.`
    : "Saved (took effect live).";
  if (restart) {
    if (!confirm("Restart the server now to apply restart-only settings?\n" +
                 "It comes back automatically in ~30–90 s.")) return;
    st.textContent = "Restarting… reloading models + warmup (~30–90 s). This page will reconnect.";
    await api("/api/ui/restart", { method: "POST" }).catch(() => {});
    // Poll the status endpoint until it answers again.
    setTimeout(function poll() {
      api("/api/ui/status").then((r) => {
        if (r.ok) { st.textContent = "✅ Back up."; refreshStatus(); refreshVoices(); }
        else setTimeout(poll, 4000);
      }).catch(() => setTimeout(poll, 4000));
    }, 8000);
  }
}

document.getElementById("hf-save").onclick = async () => {
  const tok = document.getElementById("hf-token").value.trim();
  const st = document.getElementById("hf-status");
  if (!tok) { st.textContent = "Enter a token first."; return; }
  if (!requireLogin()) return;
  st.textContent = "Saving…";
  let r;
  try {
    r = await api("/api/ui/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates: { HF_TOKEN: tok } }),
    });
  } catch (e) { st.textContent = "Not saved."; return; }
  st.textContent = r.ok ? "Token saved. Next voice you add will use it." : "Error.";
  document.getElementById("hf-token").value = "";
};

// ── text normalization preview ───────────────────────────────────────────────
document.getElementById("norm-btn").onclick = async () => {
  const input = document.getElementById("norm-input").value;
  const language = document.getElementById("norm-lang").value;
  const out = document.getElementById("norm-output");
  out.textContent = "…";
  try {
    const r = await api("/v1/text/normalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input, language }),
    });
    const j = await r.json();
    out.textContent = r.ok ? j.normalized : ("Error " + r.status);
  } catch (e) { out.textContent = "Request failed."; }
};

// ── pronunciation dictionary ─────────────────────────────────────────────────
function addReplRow(token, spoken) {
  const wrap = document.getElementById("repl-rows");
  if (!wrap) return;
  const row = document.createElement("div");
  row.className = "repl-row";
  const k = document.createElement("input");
  k.className = "repl-k"; k.placeholder = "token (e.g. HA)"; k.value = token || "";
  k.autocomplete = "off"; k.spellcheck = false;
  const arrow = document.createElement("span");
  arrow.className = "repl-arrow"; arrow.textContent = "→";
  const v = document.createElement("input");
  v.className = "repl-v"; v.placeholder = "spoken as…"; v.value = spoken || "";
  v.autocomplete = "off";
  const del = document.createElement("button");
  del.className = "repl-del"; del.type = "button";
  del.textContent = "✕"; del.title = "Remove";
  del.onclick = () => row.remove();
  row.append(k, arrow, v, del);
  wrap.appendChild(row);
}

async function refreshReplacements() {
  const wrap = document.getElementById("repl-rows");
  if (!wrap) return;
  const r = await api("/api/ui/replacements");
  if (!r.ok) return;
  const map = (await r.json()).replacements || {};
  wrap.innerHTML = "";
  const keys = Object.keys(map);
  if (!keys.length) addReplRow("", "");
  else keys.forEach((key) => addReplRow(key, map[key]));
}

document.getElementById("repl-add").onclick = () => addReplRow("", "");
document.getElementById("repl-save").onclick = async () => {
  const st = document.getElementById("repl-status");
  if (!requireLogin()) return;
  const map = {};
  document.querySelectorAll("#repl-rows .repl-row").forEach((row) => {
    const key = row.querySelector(".repl-k").value.trim();
    const val = row.querySelector(".repl-v").value;
    if (key) map[key] = val;
  });
  st.textContent = "Saving…";
  let r;
  try {
    r = await api("/api/ui/replacements", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ replacements: map }),
    });
  } catch (e) { st.textContent = "Not saved."; return; }
  if (!r.ok) { st.textContent = "Error " + r.status; return; }
  const j = await r.json();
  st.textContent = `Saved ${j.count} entr${j.count === 1 ? "y" : "ies"} — live, no restart.`;
  refreshReplacements();
};

// ── init ────────────────────────────────────────────────────────────────────
async function init() {
  renderAuth();
  try {
    await refreshStatus();
    await restoreLogin();
    // first run: offer "Set password" right away
    if (!LOGGED_IN && AUTH_MODE === "setup") showAuth("");
    await refreshVoices();
    await refreshSettings();
    setInterval(refreshStatus, 5000);
  } catch (e) { /* a read failed; the next status poll retries */ }
}
init();
