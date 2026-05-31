# Project State & Handover

**Audience:** a developer (human or AI) picking this project up cold. Read
this before changing anything. Last updated: 2026-05-31, at v0.5.6.

---

## 1. What this project is

**Polyglot TTS** is a self-hosted text-to-speech server. One cloned voice
speaks six languages; it streams audio; it exposes two network protocols
so it works with both Home Assistant and OpenAI-Speech clients (OpenClaw,
scripts, etc.). It ships as a Docker image users `docker pull` and run.

It is a **fork** of [araa47/wyoming_pocket_tts](https://github.com/araa47/wyoming_pocket_tts),
which wraps [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts):

```
Kyutai Pocket TTS   →  araa47/wyoming_pocket_tts  →  Polyglot TTS (this repo)
─ Python TTS engine    ─ + Wyoming protocol           ─ + OpenAI-Speech HTTP endpoint
─ models on HF         ─ + Docker image               ─ + multi-language same-voice
─ "build it yourself"                                  ─ + Lingua auto language ID
                                                       ─ + voice cloning via file-watcher
                                                       ─ + num2words text normalization
                                                       ─ + Mukser-Fix (click-free streaming)
                                                       ─ + multi-arch CPU/CUDA CI
                                                       ─ + non-root security hardening
```

Companion repo: **github.com/Nosdave/hass-polyglot-assist** — a Home
Assistant conversation agent for multi-language Tier-1 intents (the
*input* side of the voice pipeline; this repo is the *output* side).

---

## 2. Architecture

Single Docker image. At startup `polyglot_tts/dispatcher.py` reads env
vars, loads models + voices, builds one shared `PolyglotCore`, and starts
whichever endpoints are enabled.

```
polyglot_tts/
├── __main__.py        # entrypoint → dispatcher.run_sync()
├── dispatcher.py      # env-driven launcher: load models, build core, start endpoints
├── core.py            # PolyglotCore: shared model dict + voice_states + locks
│                      #   - per-voice / per-language state registry
│                      #   - threading.RLock for voice_states (watcher + endpoints)
│                      #   - per-model threading.Lock (pocket-tts is NOT thread-safe)
├── wyoming_server.py  # Wyoming endpoint launcher (TCP 10200)
├── wyoming_handler.py # Wyoming event handler (streaming text-IN, drain-loop,
│                      #   Lingua LID, Mukser-Fix) — carried over, production-proven
├── http_server.py     # FastAPI OpenAI-Speech endpoint (TCP 10201) + voice REST API
├── voice_loader.py    # startup voice loading + ensure_decodable() (ffmpeg transcode)
├── voice_watcher.py   # watchdog Observer on voices-extra/ → auto-embed
├── text_norm.py       # markdown strip + units + num2words (de/en/fr)
└── timing_server.py   # side-channel /timing HTTP endpoint (TCP 10299)
```

**Endpoints** (all bind `POCKET_TTS_HOST`, default 0.0.0.0):

| Port | Protocol | For | Env to disable |
|---|---|---|---|
| 10200 | Wyoming | Home Assistant, Rhasspy | `POCKET_TTS_WYOMING_PORT=` |
| 10201 | OpenAI-Speech HTTP | OpenClaw, scripts, anything | `POCKET_TTS_HTTP_PORT=` |
| 10299 | timing JSON (observability) | dashboards | `POCKET_TTS_TIMING_PORT=` |

**Concurrency model** (there was a dedicated review on this):
- Wyoming + HTTP run on the **same asyncio loop** (`asyncio.gather` in
  `dispatcher._run_endpoints`).
- The file-watcher runs in its **own thread** (watchdog) and spawns more
  threads per embed.
- All three touch `core.voice_states`. Mutations go through
  `core.add_voice` / `core.set_voice_state` / `core.remove_voice` under
  `core._voice_lock` (a `threading.RLock`).
- Blocking inference is offloaded: HTTP via `asyncio.to_thread`, Wyoming
  via its executor. Both acquire `core.get_model_lock(model)` around
  `generate_audio_stream` because pocket-tts mutates per-model state
  (`pad_with_spaces_for_short_inputs`) and is not thread-safe.

---

## 3. Configuration

Full list in `docs/CONFIGURATION.md`. The ones you'll actually touch:

| Var | Default | Notes |
|---|---|---|
| `POCKET_TTS_LANGUAGES` | `english_2026-04,german_24l,french_24l` | comma-sep checkpoints; each adds ~1.3 GB RAM. Singular `POCKET_TTS_LANGUAGE` is a back-compat fallback. |
| `POCKET_TTS_VOICE` | `eve` | default voice |
| `POCKET_TTS_DEVICE` | `auto` | auto/cpu/cuda |
| `POCKET_TTS_VOICES_EXTRA_DIR` | `/app/voices-extra` | watched dir for voice cloning |
| `HF_TOKEN` / `HF_TOKEN_FILE` | unset | **only needed for voice cloning** (gated model) |
| `POCKET_TTS_LAZY_LOAD` | `false` | **declared but NOT implemented** — slated for 0.6 |

Checkpoints: `english_2026-04`, `german_24l`, `french_24l`, `italian_24l`,
`spanish_24l`, `portuguese_24l`.

---

## 4. Image tags and CI

CI: `.github/workflows/build-and-publish.yml`. Each (variant, arch) builds
on its **native** GitHub-hosted runner (no QEMU — `ubuntu-24.04-arm` is GA
for public repos since Aug 2025), pushes by digest, then merge jobs stitch
per-arch digests into multi-arch manifest lists.

| Tag | PyTorch | Arch | Use |
|---|---|---|---|
| `:latest` | cpu | amd64 + arm64 | no GPU |
| `:cuda` | cu128 (CUDA 12.8) | amd64 + arm64 | broad GPU compat (driver ≥ 525, Turing→Blackwell) |
| `:cuda13` | cu130 (CUDA 13) | amd64 + arm64 | native sm_120/sm_121, driver ≥ 580, Turing+ only |

Decision guide: `docs/CUDA.md`. Why two CUDA tags: cu130 drops
Pascal/Volta/Maxwell and needs driver ≥ 580, so it can't be the single
default. `:cuda` stays cu128 for compatibility; `:cuda13` is the opt-in
speed tag (mirrors PyTorch/vLLM/ComfyUI).

CI gotchas already fixed (don't reintroduce):
- The image name **must be lowercased** in every job
  (`IMAGE_NAME=${IMAGE_NAME,,}`) — the repo owner has a capital letter and
  Docker refs must be lowercase.
- PEP 639: an `OSI Approved :: MIT License` classifier alongside
  `license = "MIT"` makes setuptools reject the build. Classifier removed.
- `flavor: suffix=-cuda` + `type=raw,value=cuda` produced `:cuda-cuda`.
  Removed; tags are explicit.

---

## 5. Voice cloning (how it works)

1. User puts an audio file in the mounted `voices-extra/` dir (file stem =
   voice name), or `POST /v1/audio/voices` with a multipart upload.
2. The watcher waits for the file to stabilize, transcodes non-native
   formats to a temp WAV via the bundled ffmpeg (so **iPhone .m4a memos
   work**), encodes embeddings against every loaded language, and
   registers the voice (~3 s on GPU, ~30 s on slow CPU).
3. The voice is then usable by name through every endpoint.

**Gated model:** voice *cloning* uses the gated `kyutai/pocket-tts` HF
model → requires `HF_TOKEN` (free account + accept the gate at
huggingface.co/kyutai/pocket-tts). Without a token the server falls back
to the non-cloning model and preset voices still work.

**Non-root + volumes:** the container runs as UID 10001. Bind-mounted
host dirs for `voices-extra` / the HF cache must be writable by UID 10001
(`chown -R 10001:10001 <dir>`). The Dockerfile pre-creates the in-image
paths owned by `polyglot` so empty *named* volumes inherit ownership.

**Watcher robustness:** a voice is tracked by the exact file path that
produced it (deleting `name.m4a` won't drop a voice made from `name.wav`).
A file that fails to embed is remembered by path+mtime and not retried
until it changes. Files over 100 MB are rejected up front.

---

## 6. Measured performance (real hardware)

Earlier docs claimed 33–38× real-time on Blackwell. That was **never
reproduced** and has been corrected. Direct measurement on an NVIDIA
Blackwell GB10 (both this image and the upstream production image):

- **GPU, cu128 (`:cuda`): ~5× real-time.** sm_121 isn't native in cu128,
  so kernels JIT-fall-back. Still faster than real-time → no streaming lag.
- **CPU on a saturated host: ~0.24×** (not representative of an idle CPU).
- **`:cuda13` (cu130) is expected to beat 5×** via native sm_121 — **not
  yet benchmarked**. That's the headline open task (see below and issue #1).

`docs/PERFORMANCE.md` marks which table rows are measured vs estimated.
Only the GB10 and M4 rows are real; the rest are rough estimates.

A note on shared GPUs: a co-resident process that reserves most of the
GPU memory (e.g. an LLM server with a high memory-utilization setting) can
make a *new* GPU process OOM even for a small model, because GPU memory is
allocated first-come-first-served. On a busy box, give the TTS container
its allocation before the memory hog starts, or run the TTS on CPU.

---

## 7. Open work / backlog

In rough priority:

1. **Benchmark `:cuda13`** (issue #1). The whole point of the cu130 tag:
   confirm native sm_121 beats the cu128 ~5×. If it doesn't help
   meaningfully, reconsider maintaining the tag.
2. **D2H optimization:** in the synth loop, do `clamp + scale + int16`
   **on the GPU** before `.cpu()`. int16 is half the bytes of float32 →
   halves device-to-host transfer. Applies to `http_server.py`
   `_synthesize_pcm` (the `frame.cpu().numpy()` loop) and the Wyoming
   handler. Hot path — keep a rollback ready. ~5 min change.
3. **torch.compile the Mimi decoder** behind `POCKET_TTS_COMPILE_DECODER=true`.
   Expected −10–20% inference. Medium risk, ~1 h. Especially interesting
   on sm_121.
4. **Implement `POCKET_TTS_LAZY_LOAD`** (declared, not built): load a
   missing language model on first request for that language.
5. **Polish:** more languages in `text_norm` (it/es/pt currently fall back
   to the German unit map); error-sidecar UX.

Non-goals: cloud-TTS bridging; a different protocol stack; a GUI (maybe
later, not now).

---

## 8. Making a release

1. Change code, add/adjust tests in `tests/`.
2. `python -m pytest tests/ -q` (needs `pip install pytest watchdog`;
   `voice_loader`/`voice_watcher` logic is tested without models).
3. Syntax-check modules; run `gitleaks detect --no-git`.
4. Add a `CHANGELOG.md` entry under a new version heading.
5. Commit, push `main`, then `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`.
6. CI builds + publishes all tags. When watching a run, verify the actual
   job statuses (`gh run list`), not just a watch command's exit code —
   a green watch can still hide a failed job.

The public line started at **0.5.0** and iterated fast (0.5.1 hardening →
0.5.6 `:cuda13`). Stay below 1.0 until the API and tag set settle.
