"use strict";

// ── token handling ────────────────────────────────────────────────────────
let TOKEN = localStorage.getItem("polyglot_ui_token") || "";

function authHeaders(extra) {
  const h = extra || {};
  if (TOKEN) h["X-UI-Token"] = TOKEN;
  return h;
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = authHeaders(opts.headers);
  const res = await fetch(path, opts);
  if (res.status === 401) {
    showTokenGate();
    throw new Error("unauthorized");
  }
  return res;
}

function showTokenGate() {
  document.getElementById("token-gate").hidden = false;
}

document.getElementById("token-save").onclick = () => {
  TOKEN = document.getElementById("token-input").value.trim();
  localStorage.setItem("polyglot_ui_token", TOKEN);
  document.getElementById("token-gate").hidden = true;
  init();
};

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
    const langs = s.languages.map((l) => l.bcp47).join(", ") || "—";
    document.getElementById("status-box").innerHTML = `
      <div class="kv"><span>Device</span><span>${s.device}</span></div>
      <div class="kv"><span>Languages</span><span>${langs}</span></div>
      <div class="kv"><span>Voices</span><span>${s.voice_count}</span></div>
      <div class="kv"><span>Default voice</span><span>${s.default_voice}</span></div>
      <div class="kv"><span>Uptime</span><span>${fmtUptime(s.uptime_s)}</span></div>
      <div class="kv"><span>UI auth</span><span>${s.auth_enabled ? "on" : "off"}</span></div>`;
    const t = s.last_synth || {};
    const rtf = t.audio_ms && t.synth_ms ? (t.audio_ms / t.synth_ms).toFixed(2) : "—";
    document.getElementById("timing-box").innerHTML = t.ts
      ? `<div class="kv"><span>Voice</span><span>${t.voice} (${t.language})</span></div>
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
      return `<div class="kv"><span>${e.name}</span><span class="muted">disabled</span></div>`;
    }
    if (isHttp) {
      return `<div class="kv"><span>${e.name}</span><span class="port">${browserHost}</span></div>`;
    }
    // Wyoming / timing: in-container port; host mapping may differ.
    return `<div class="kv"><span>${e.name}</span>
      <span class="port">container :${e.port}</span></div>`;
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
  { key: "",   label: "Fallback (all languages)" },
  { key: "de", label: "Deutsch" },
  { key: "en", label: "English" },
  { key: "fr", label: "Français" },
  { key: "it", label: "Italiano" },
  { key: "es", label: "Español" },
  { key: "pt", label: "Português" },
];
const SLOTS = {};   // lang-key -> {blob, filename}

function langBadges(v) {
  if (!v.languages || !v.languages.length) return "";
  const chips = v.languages.map((l) =>
    `<span class="lng ${l.dedicated ? "ded" : ""}" title="${l.dedicated ? "own reference audio" : "shared fallback audio"}">${l.bcp47}</span>`
  ).join("");
  const star = v.per_language
    ? ` <span class="poly" title="multilingual — a separate reference per language">◆</span>` : "";
  return `<span class="langs">${chips}${star}</span>`;
}

async function refreshVoices() {
  const r = await api("/v1/audio/voices");
  if (!r.ok) return;
  const data = await r.json();
  const voices = data.voices || [];
  VOICE_NAMES = voices.map((v) => v.name);
  if (typeof validateGenerate === "function") validateGenerate();
  document.getElementById("voice-count").textContent = `(${voices.length})`;
  // populate test-voice + voice list
  const sel = document.getElementById("test-voice");
  sel.innerHTML = voices.map((v) => `<option>${v.name}</option>`).join("");
  document.getElementById("voice-list").innerHTML = voices
    .map((v) => `<div class="voice-item">
        <span class="name">${v.name}</span>
        ${langBadges(v)}
        <span class="kind">${v.kind}</span>
        ${v.kind === "custom" ? `<button data-del="${v.name}">delete</button>` : ""}
      </div>`).join("");
  document.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm(`Delete voice "${b.dataset.del}"?`)) return;
      await api("/v1/audio/voices/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
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

function setSlot(key, blob, filename) {
  SLOTS[key] = { blob, filename };
  const st = slotEl(key);
  if (st) { st.textContent = "✓ " + filename; st.classList.add("set"); }
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
  else if (!have.length) { ok = false; msg = "Add at least one audio (a language slot or fallback)."; }
  else { msg = `Ready: ${have.map((k) => k || "fallback").join(", ")}.`; }
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

// build the per-language slot rows
function buildSlots() {
  const host = document.getElementById("lang-slots");
  if (!host) return;
  host.innerHTML = LANG_SLOTS.map((s) => {
    const id = s.key || "fb";
    return `<div class="slot">
      <span class="slot-lang">${s.label}${s.key ? ` <code>${s.key}</code>` : ""}</span>
      <label class="link">file<input type="file" accept="audio/*" hidden data-slot="${s.key}" /></label>
      <button class="rec" data-rec="${s.key}"${micUnavailable ? " disabled" : ""}>● rec</button>
      <span class="slot-status" id="slot-status-${id}">—</span>
      <button class="clr" data-clr="${s.key}" title="clear">✕</button>
    </div>`;
  }).join("");
  host.querySelectorAll('input[type="file"]').forEach((inp) => {
    inp.onchange = () => { const f = inp.files[0]; if (f) setSlot(inp.dataset.slot, f, f.name); inp.value = ""; };
  });
  host.querySelectorAll("[data-clr]").forEach((b) => { b.onclick = () => clearSlot(b.dataset.clr); });
  host.querySelectorAll("[data-rec]").forEach((b) => { b.onclick = () => toggleRecord(b.dataset.rec, b); });
}

document.getElementById("upload-name").addEventListener("input", validateGenerate);

document.getElementById("generate-btn").onclick = async () => {
  const name = document.getElementById("upload-name").value.trim();
  const keys = Object.keys(SLOTS);
  if (!name || !keys.length) return;
  const status = document.getElementById("upload-status");
  document.getElementById("generate-btn").disabled = true;
  status.innerHTML = "⏳ Uploading…";
  for (const key of keys) {
    const s = SLOTS[key];
    let r;
    try { r = await uploadOne(s.blob, s.filename, name, key); }
    catch (e) { status.innerHTML = "❌ Upload failed."; return; }
    if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) {}
      status.innerHTML = "❌ Error: " + (j.detail || r.status); return; }
  }
  LANG_SLOTS.forEach((s) => clearSlot(s.key));
  status.innerHTML = `⏳ Embedding "<b>${name}</b>"… ~30 s per language (longer on CPU).`;
  let waited = 0;
  const poll = setInterval(async () => {
    waited += 3;
    const now = await currentVoiceNames();
    if (now.includes(name)) {
      clearInterval(poll);
      status.innerHTML = `✅ Voice "<b>${name}</b>" is ready.`;
      refreshVoices();
      document.getElementById("upload-name").value = "";
    } else if (waited >= 120) {
      clearInterval(poll);
      status.innerHTML = `⚠️ "<b>${name}</b>" didn't appear after 120 s. Check the server log — ` +
        `the audio may be too short/noisy, or (for cloning) you may need an HF token in Settings.`;
      refreshVoices();
    } else {
      status.innerHTML = `⏳ Embedding "<b>${name}</b>"… (${waited}s)`;
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
  const ph = c.placeholder ? ` placeholder="${c.placeholder}"` : "";
  // Languages: render as checkboxes from the real installed checkpoints,
  // including the lighter (non-24l) variants for weak hardware.
  if (key === "POCKET_TTS_LANGUAGES") {
    const sel = val.split(",").map((x) => x.trim()).filter(Boolean);
    const cps = (CHECKPOINTS && CHECKPOINTS.length)
      ? CHECKPOINTS
      : (c.options || sel).map((o) => ({ checkpoint: o, bcp47: o.slice(0, 2), quality: "" }));
    return `<div class="lang-grid" data-key="${key}">` +
      cps.map((cp) => `<label class="lang-opt" title="${cp.quality}">
        <input type="checkbox" value="${cp.checkpoint}" data-bcp="${cp.bcp47}"
          ${sel.includes(cp.checkpoint) ? "checked" : ""} />
        <span>${cp.checkpoint}</span><small>${cp.bcp47} · ${cp.quality}</small>
      </label>`).join("") + `</div>`;
  }
  if (c.type === "bool") {
    const on = ["1", "true", "yes", ""].includes(val.toLowerCase()) && val !== "0" && val.toLowerCase() !== "false";
    return `<select data-key="${key}">
      <option value="true" ${on ? "selected" : ""}>true</option>
      <option value="false" ${!on ? "selected" : ""}>false</option></select>`;
  }
  if (c.type === "select") {
    return `<select data-key="${key}">` +
      c.options.map((o) => `<option ${o === val ? "selected" : ""}>${o}</option>`).join("") +
      `</select>`;
  }
  if (c.type === "voice-select") {
    const opts = VOICE_NAMES.length ? VOICE_NAMES : [val].filter(Boolean);
    return `<select data-key="${key}">` +
      opts.map((o) => `<option ${o === val ? "selected" : ""}>${o}</option>`).join("") +
      `</select>`;
  }
  if (c.type === "number") {
    return `<input type="number" data-key="${key}" value="${val}"${ph} />`;
  }
  // text — with a datalist of suggestions if options provided
  if (c.options && c.options.length) {
    const listId = "dl-" + key;
    return `<input data-key="${key}" list="${listId}" value="${val}"${ph} />
      <datalist id="${listId}">${c.options.map((o) => `<option value="${o}">`).join("")}</datalist>`;
  }
  return `<input data-key="${key}" value="${val}"${ph} />`;
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
    let help = c.help || "";
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
        <label>${key}${badge}</label>
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
      .map((l) => `<a href="${l.url}" target="_blank" rel="noopener">${l.label} ↗</a>`)
      .join(" · ");
  }

  // populate the normalize-preview language dropdown from loaded languages
  const nl = document.getElementById("norm-lang");
  if (nl) {
    const langs = LOADED_LANGS.length ? LOADED_LANGS : ["de", "en", "fr"];
    nl.innerHTML = langs.map((l) => `<option>${l}</option>`).join("");
  }
}

document.getElementById("settings-save").onclick = () => saveSettings(false);
document.getElementById("restart-btn").onclick = () => saveSettings(true);

async function saveSettings(restart) {
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
  const r = await api("/api/ui/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates }),
  });
  const j = await r.json();
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
  st.textContent = "Saving…";
  const r = await api("/api/ui/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: { HF_TOKEN: tok } }),
  });
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

// ── init ────────────────────────────────────────────────────────────────────
async function init() {
  try {
    await refreshStatus();
    await refreshVoices();
    await refreshSettings();
    setInterval(refreshStatus, 5000);
  } catch (e) { /* token gate already shown */ }
}
init();
