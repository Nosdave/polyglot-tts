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

// ── voices ──────────────────────────────────────────────────────────────────
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

async function uploadBlob(blob, filename, name) {
  const status = document.getElementById("upload-status");
  const before = await currentVoiceNames();
  const fd = new FormData();
  fd.append("file", blob, filename);
  if (name) fd.append("name", name);
  status.innerHTML = '⏳ Uploading…';
  let j;
  try {
    const r = await api("/v1/audio/voices", { method: "POST", body: fd });
    j = await r.json();
    if (!r.ok) { status.innerHTML = '❌ Error: ' + (j.detail || r.status); return; }
  } catch (e) { status.innerHTML = '❌ Upload failed.'; return; }

  const want = j.name;
  status.innerHTML = `⏳ Embedding "<b>${want}</b>"… this can take ~30 s (longer on CPU).`;
  // Poll until the voice appears (or a timeout).
  let waited = 0;
  const poll = setInterval(async () => {
    waited += 3;
    const now = await currentVoiceNames();
    if (now.includes(want)) {
      clearInterval(poll);
      status.innerHTML = `✅ Voice "<b>${want}</b>" is ready.`;
      refreshVoices();
      document.getElementById("upload-name").value = "";
    } else if (waited >= 90) {
      clearInterval(poll);
      status.innerHTML = `⚠️ "<b>${want}</b>" didn't appear after 90 s. ` +
        `Check the server log — the file may be too short, noisy, or (for cloning) ` +
        `you may need an HF token in Settings.`;
      refreshVoices();
    } else {
      status.innerHTML = `⏳ Embedding "<b>${want}</b>"… (${waited}s)`;
    }
  }, 3000);
}

// ── gated voice creation: sample + unique name + Generate ──────────────────
let SELECTED_BLOB = null;       // File or Blob
let SELECTED_FILENAME = "";     // filename to send

function nameOk(s) {
  return s && !s.startsWith(".") && s.indexOf("..") === -1 &&
    /^[A-Za-z0-9_.-]+$/.test(s);
}

function setSample(blob, filename, label) {
  SELECTED_BLOB = blob;
  SELECTED_FILENAME = filename;
  document.getElementById("selected-file").textContent = label;
  document.getElementById("selected-file").classList.remove("muted");
  validateGenerate();
}

function validateGenerate() {
  const name = document.getElementById("upload-name").value.trim();
  const btn = document.getElementById("generate-btn");
  const hint = document.getElementById("name-hint");
  let ok = true, msg = "";
  if (!SELECTED_BLOB) { ok = false; msg = "Pick or record an audio sample first."; }
  else if (!name) { ok = false; msg = "Enter a voice name."; }
  else if (!nameOk(name)) { ok = false; msg = "Allowed: letters, digits, _ - . (no leading dot)."; }
  else if (VOICE_NAMES.includes(name)) { ok = false; msg = `"${name}" already exists — pick another name.`; }
  else { msg = `Ready to generate "${name}".`; }
  btn.disabled = !ok;
  hint.textContent = msg;
  hint.style.color = ok ? "var(--ok)" : "var(--muted)";
}

document.getElementById("upload-name").addEventListener("input", validateGenerate);

const dz = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("dragover"); };
dz.ondragleave = () => dz.classList.remove("dragover");
dz.ondrop = (e) => {
  e.preventDefault(); dz.classList.remove("dragover");
  const f = e.dataTransfer.files[0];
  if (f) {
    setSample(f, f.name, f.name);
    // pre-fill the name from the filename stem if the field is empty
    const nm = document.getElementById("upload-name");
    if (!nm.value.trim()) { nm.value = f.name.replace(/\.[^.]+$/, ""); validateGenerate(); }
  }
};
fileInput.onchange = () => {
  const f = fileInput.files[0];
  if (f) {
    setSample(f, f.name, f.name);
    const nm = document.getElementById("upload-name");
    if (!nm.value.trim()) { nm.value = f.name.replace(/\.[^.]+$/, ""); validateGenerate(); }
  }
};

document.getElementById("generate-btn").onclick = () => {
  if (!SELECTED_BLOB) return;
  const name = document.getElementById("upload-name").value.trim();
  uploadBlob(SELECTED_BLOB, SELECTED_FILENAME, name);
  // reset selection so it can't be double-submitted
  SELECTED_BLOB = null; SELECTED_FILENAME = "";
  document.getElementById("selected-file").textContent = "— none yet —";
  document.getElementById("selected-file").classList.add("muted");
  document.getElementById("generate-btn").disabled = true;
};

// mic recording → sets the sample (does NOT auto-upload; user clicks Generate)
let mediaRecorder = null, chunks = [];
const micBtn = document.getElementById("mic-btn");
if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
    !window.isSecureContext) {
  document.getElementById("mic-hint").hidden = false;
}
micBtn.onclick = async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") { mediaRecorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: "audio/webm" });
      setSample(blob, "recording.webm", "microphone recording");
      micBtn.classList.remove("recording");
      micBtn.textContent = "● Record from mic";
      document.getElementById("mic-status").textContent = "Recorded — now name it and click Generate.";
    };
    mediaRecorder.start();
    micBtn.classList.add("recording");
    micBtn.textContent = "■ Stop recording";
    document.getElementById("mic-status").textContent = "Recording… speak 10–30 s.";
  } catch (e) {
    document.getElementById("mic-hint").hidden = false;
    document.getElementById("mic-status").textContent = "Mic blocked (needs https/localhost).";
  }
};

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
  // Languages: render as checkboxes for the available checkpoints so it's
  // clear how to enable multiple languages.
  if (key === "POCKET_TTS_LANGUAGES") {
    const sel = val.split(",").map((x) => x.trim()).filter(Boolean);
    const opts = (c.options && c.options.length) ? c.options : sel;
    return `<div class="lang-grid" data-key="${key}">` +
      opts.map((o) => `<label class="lang-opt">
        <input type="checkbox" value="${o}" ${sel.includes(o) ? "checked" : ""} /> ${o}
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
  const cfg = (await r.json()).config;
  const form = document.getElementById("settings-form");
  form.innerHTML = "";
  const oneLang = LOADED_LANGS.length === 1;
  Object.keys(cfg).forEach((key) => {
    if (key === "HF_TOKEN") return; // handled in its own card
    const c = cfg[key];
    const badge = c.restart_required ? '<span class="badge">restart</span>' : "";
    let help = c.help || "";
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
