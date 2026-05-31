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
  } catch (e) { /* ignore */ }
}

function fmtUptime(s) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

// ── voices ──────────────────────────────────────────────────────────────────
async function refreshVoices() {
  const r = await api("/v1/audio/voices");
  if (!r.ok) return;
  const data = await r.json();
  const voices = data.voices || [];
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

async function uploadBlob(blob, filename, name) {
  const fd = new FormData();
  fd.append("file", blob, filename);
  if (name) fd.append("name", name);
  const status = document.getElementById("upload-status");
  status.textContent = "Uploading…";
  try {
    const r = await api("/v1/audio/voices", { method: "POST", body: fd });
    const j = await r.json();
    if (r.ok) {
      status.textContent = `Queued "${j.name}" — embedding starts shortly. Refreshing…`;
      setTimeout(refreshVoices, 4000);
    } else {
      status.textContent = "Error: " + (j.detail || r.status);
    }
  } catch (e) { status.textContent = "Upload failed."; }
}

// drag & drop + file picker
const dz = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("dragover"); };
dz.ondragleave = () => dz.classList.remove("dragover");
dz.ondrop = (e) => {
  e.preventDefault(); dz.classList.remove("dragover");
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
};
fileInput.onchange = () => { if (fileInput.files[0]) handleFile(fileInput.files[0]); };
function handleFile(f) {
  const name = document.getElementById("upload-name").value.trim();
  uploadBlob(f, f.name, name);
}

// mic recording
let mediaRecorder = null, chunks = [];
const micBtn = document.getElementById("mic-btn");
if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
    !window.isSecureContext) {
  document.getElementById("mic-hint").hidden = false;
}
micBtn.onclick = async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: "audio/webm" });
      const name = document.getElementById("upload-name").value.trim() || "myvoice";
      uploadBlob(blob, name + ".webm", name);
      micBtn.classList.remove("recording");
      micBtn.textContent = "● Record from mic";
      document.getElementById("mic-status").textContent = "Recorded — uploading.";
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
async function refreshSettings() {
  const r = await api("/api/ui/config");
  if (!r.ok) return;
  const cfg = (await r.json()).config;
  const form = document.getElementById("settings-form");
  form.innerHTML = "";
  Object.keys(cfg).forEach((key) => {
    if (key === "HF_TOKEN") return; // handled in its own card
    const c = cfg[key];
    const badge = c.restart_required ? '<span class="badge">restart</span>' : "";
    const row = document.createElement("div");
    row.className = "setting-row";
    row.innerHTML = `<label>${key}${badge}</label>
      <input data-key="${key}" value="${c.value || ""}" />`;
    form.appendChild(row);
  });
}

document.getElementById("settings-save").onclick = () => saveSettings(false);
document.getElementById("restart-btn").onclick = () => saveSettings(true);

async function saveSettings(restart) {
  const updates = {};
  document.querySelectorAll("#settings-form [data-key]").forEach((i) => {
    updates[i.dataset.key] = i.value.trim();
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
    if (!confirm("Restart the container now? It must have a Docker restart policy.")) return;
    st.textContent = "Restarting… the container will exit and come back.";
    await api("/api/ui/restart", { method: "POST" }).catch(() => {});
    setTimeout(() => { st.textContent = "Restart triggered. Reload in ~1–2 min."; }, 1500);
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
