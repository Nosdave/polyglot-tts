# Changelog

All notable changes will be documented here. Semantic versioning.

## [Unreleased]

## [0.7.1] – 2026-06-22

### Added

- **Text normalization: emoji stripping + currency symbols.** Emoji and
  pictographs are removed before synthesis (the model otherwise mumbles over
  them) — scoped to the emoji blocks so the spoken symbols (`→`, `<`, `>`, `=`,
  `~`) are untouched. Currency symbols next to an amount are spoken as a word
  after it, per language: `$12` → „zwölf Dollar", `50 €` → „fünfzig Euro", `£20`
  → „zwanzig Pfund" (en/fr/it/es/pt analogous, plural unless the amount is 1).
  German „1 €" is spoken „ein Euro" (not „eins"), since the currency nouns are
  masculine/neuter.

### Changed

- **Lower model-load + warmup VRAM peak.** `gc.collect()` + `torch.cuda.empty_cache()`
  after each model is moved to the GPU and once after warmup, returning the
  CPU→GPU load transients and the warmup high-water reservation to the OS
  (PyTorch's caching allocator holds them otherwise). Trims the startup peak and
  steady-state footprint by ~1–2 GB on multi-language GPU setups. Frees only
  unused cached blocks — model weights, compiled kernels and output are
  unchanged.

## [0.7.0] – 2026-06-16

### Added

- **Web UI: per-language voice management.** The voice list shows each voice's
  coverage across **every available language** as bcp47 badges — solid = its own
  reference audio (accent-free), faint = shares the fallback, struck-through = not
  covered; a ◆ marks a true multilingual voice. The "Add a voice" card has one
  upload-or-record slot per supported language (de/en/fr/it/es/pt); fill one, two,
  or all, then mark one slot as the **fallback** (a radio) whose recording is reused
  for the languages left empty. `GET /v1/audio/voices` gained
  `languages: [{bcp47, dedicated}]` + `per_language` per voice and a top-level
  `languages` (loaded codes); `POST /v1/audio/voices` accepts a `language` form
  field (saved as `<voice>.<bcp47>.<ext>`); `DELETE` removes every per-language
  file of a voice.

- **Text normalization for dates, clock times, numbered lists and `=`.** Before
  the generic number step, `text_norm` now expands: numeric dates `DD.MM[.]`
  → spoken month form (de `14.06` → „vierzehnter Juni", with dative after
  am/vom/zum; fr `14 juin`); clock times `HH:MM` → „zweiundzwanzig **Uhr**
  siebenundfünfzig" (de) / „… **heures** …" (fr) / „<h> <m>" (others), swallowing
  a redundant trailing „Uhr"; numbered-list markers `1.`/`2.` at line start →
  ordinal adverbs („Erstens, "/„Zweitens, "); and `=` → „gleich"/„equals"/„égale".
  Previously `14.06` was read as the decimal „vierzehn Komma null sechs", `22:57`
  kept its colon, list markers became „eins. zwei.", and `=` was dropped.
  Dates also handle a 4-digit year (`14.06.2026` → „… zweitausendsechsundzwanzig").
  False-positive guards: a `DD.MM` is treated as a date only when the day is > 12
  or a 4-digit year is present, so a version like `Mistral 3.5.` stays
  „drei Komma fünf" (not „dritter Mai"); a non-time `digit:digit` colon is a
  score/ratio (`2:1` → „zwei zu eins", `16:9` → „sechzehn zu neun"); and a
  hyphen/en-/em-dash between two integers as a range (`10-20` → „zehn bis zwanzig"
  / „ten to twenty" / „dix à vingt"), while a spaced dash between words stays a
  comma pause. More spoken symbols: `<` → „kleiner als" / „less than", `>` →
  „größer als" / „greater than", an arrow (`→`, `->`, `=>`) → a directional
  connector „zu" / „to" / „vers" (universal across path/mapping/sequence/
  implication senses), and a tilde before a number
  (`~10` → „circa 10" / „about 10" / „environ 10"); a lone `~` or `~~strike~~`
  is left alone. `•`/`‣`/`◦` bullets are now stripped like `-`/`*`/`+`.
- **Locale-aware number parsing.** de/fr/es/it/pt read `.` as the thousands
  separator and `,` as the decimal point (`1.000` → „eintausend", `1.250.000` →
  „eine Million …", `14,06` → „vierzehn Komma null sechs"); English keeps the
  inverse. Previously `1.000` was misread as the decimal `1.0` („eins").

- **Per-language voice references — accent-free multilingual voices.** A voice
  can now have one reference recording *per language*, coupled to the matching
  model, via the filename convention `<voice>.<bcp47>.<ext>` in `voices-extra/`
  (e.g. `Jarvis.de.mp3`, `Jarvis.en.mp3`, `Jarvis.fr.mp3`). All files sharing a
  voice name form **one** voice; each language model is cloned from a
  native-language reference, so the voice no longer carries a cross-language
  accent (a voice cloned only from English audio used to speak German with an
  English accent). A single untagged `<voice>.<ext>` keeps the previous
  behaviour (one embedding shared across all languages) and is also used as the
  fallback for any language without its own file. The file-watcher rebuilds the
  whole voice from its current files on any add/replace/delete. See
  `docs/CONFIGURATION.md`.

### Fixed

- **Silent-collapse on ultra-short inputs (auto eos_threshold retry).** A 1–2
  syllable phrase (e.g. French „C'est fait.") could hit pocket-tts' end-of-speech
  threshold instantly and render near-silence — the synth came out empty. The
  synthesiser now detects a near-silent result (mean RMS below
  `POCKET_TTS_SILENCE_RMS`, default 0.005 — measured collapse ≈0.0005 vs real
  speech ≥0.04, a ~50× margin) and retries **once** with a raised
  `eos_threshold` (`POCKET_TTS_EOS_RETRY`, default 0.0; higher = the model keeps
  generating instead of stopping early). **Latency-neutral**: the RMS is
  accumulated from audio already generated/streamed, and the retry only runs when
  the first attempt was silent anyway — the normal and long-sentence paths are
  untouched (no buffering, full streaming preserved). Also adds a per-request
  `eos_threshold` field on `/v1/audio/speech` (clamped [-12, 12]); setting it
  disables the auto-retry for that call. Note: this fixes *empty* output, not the
  separate cold-start onset garble on the undistilled 24-layer DE/FR checkpoints
  (no distilled DE/FR model exists upstream — `english_2026-04` is distilled and
  immune).

- **Clipped final word of a sentence (tail truncation).** FlowLM fires its
  end-of-speech token slightly early, so the last acoustic-delay frames were
  never rendered — e.g. „Der Rolladen … wurde geschlossen." came out as
  „… gesch". A `frames_after_eos` tail budget already existed but was applied
  **only to texts under 5 words** on the Wyoming path and **not at all** on the
  HTTP path, so medium sentences slipped through. It is now applied universally
  on both paths, default **8** (empirically: 5 still clipped ~40 % of medium
  sentences, 8 ≈ none; values that are too high make the vocoder render a loud
  click past EOS, so it is bounded to [3, 32]). Tunable via
  `POCKET_TTS_FRAMES_AFTER_EOS`, and per request on `/v1/audio/speech` via a new
  `frames_after_eos` field. The onset `pad_with_spaces_for_short_inputs` stays
  short-text-only (a separate concern).

### Changed

- **Use jemalloc to reclaim CPU RAM after model load.** The 4-stage weight load
  + warmup allocate gigabytes of transient CPU buffers; glibc malloc frees them
  but doesn't return them to the OS (fragmentation — `malloc_trim`,
  `MALLOC_ARENA_MAX`, and disabling THP were all measured to not reclaim it),
  leaving ~2–3 GB of resident anonymous RAM per 3-language load (it is **not** a
  duplicate of the GPU weights — those move to the GPU cleanly). The image now
  ships `libjemalloc2` with `LD_PRELOAD=libjemalloc.so.2`, which returns the
  freed memory. Override with `-e LD_PRELOAD=` to disable.
- **Ship `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` by default.** The
  CUDA allocator now grows segments on demand instead of pre-reserving large
  blocks, trimming the per-process VRAM footprint (~1 GB on a 3-language load)
  and avoiding fragmentation OOMs — most valuable on shared/unified memory like
  the DGX Spark. Overridable via `-e`; no effect on the CPU image.

### Fixed

- **UI "Save" silently failed when `/app/config` wasn't writable by the
  container user.** A host-bind config dir owned by the host user (not UID
  10001) made every `POST /api/ui/config` raise a `PermissionError` → opaque
  500, so saved settings never persisted (looked like the UI ignored them).
  The endpoint now returns a clear message naming the writability requirement,
  and the docker-compose example uses a **named volume** for `/app/config`
  (inherits the image's UID-10001 ownership) instead of a host bind. Docs note
  the chown requirement for host binds.

### Added

- **Voice-sample loudness normalization on clone.** A new voice is loudness-
  normalized (EBU R128 `loudnorm`) before encoding, so a quiet recording still
  makes a strong, consistent voice prompt. On by default; toggle with
  `POCKET_TTS_VOICE_NORMALIZE`. Best-effort — skipped cleanly if ffmpeg is
  unavailable or the filter fails.
- **Output volume control.** `POCKET_TTS_OUTPUT_GAIN` (0.0–4.0, default 1.0)
  scales the generated audio, live, on both the Wyoming and HTTP paths (clipped
  to full scale after scaling). `POST /v1/audio/speech` also accepts a per-call
  `gain` field. Streaming-safe (applied per chunk).

### Build

- **Image layers split so updates pull only a few MB, not gigabytes.** The app
  code used to be installed into the same multi-GB `site-packages` layer as
  torch/CUDA, so any code change re-pulled the whole thing. The app now lives
  in its own thin layer (`/opt/polyglot-pkg`, on `PYTHONPATH`); the heavy deps
  layer stays cached across releases. Also fixed the healthcheck's Wyoming
  describe grep to match the renamed `polyglot-tts` program.

### Changed

- **Sampling temperature now applies live and per request.** `model.temp` is
  read at each decode step, so it never needed a restart. Saving
  `POCKET_TTS_TEMP` in the UI/config now takes effect on the next synthesis
  (no longer restart-required), and `POST /v1/audio/speech` accepts a
  `temperature` field to override the global value for a single call (clamped
  to `0.1`–`1.5`, restored afterwards under the per-model lock). Corrects the
  0.6.3 claim that temperature was baked in at load.

## [0.6.3] — 2026-06-02

### Added

- **`POST /v1/text/normalize`** — preview the text-normalization pipeline
  (numbers, units, dates, ordinals, Markdown strip) for a given text +
  language. Surfaced in the web UI as a live, editable preview panel.
- **Sampling temperature (`POCKET_TTS_TEMP`)** exposed via env, the
  `/api/ui/config` REST surface, and the Settings UI. The one built-in
  prosody lever pocket-tts offers: `0.1`–`1.5` (default `0.7`); higher =
  more expressive/varied but less stable. It is **global** (every voice and
  language) and **baked into the model at load**, so it is not a per-request
  parameter and a change needs a restart.
- **Selectable lighter language variants.** The Settings language list now
  offers the non-`_24l` checkpoints (e.g. `german` vs `german_24l`),
  enumerated from the installed pocket-tts, so weak hardware (Raspberry Pi)
  can trade quality for speed/RAM.
- **HuggingFace helper links** next to the token field (request gated model
  access → create a read token), sourced from backend field metadata.

### Fixed

- **Version display was stuck at 0.5.0.** `__version__` is read from the
  installed package metadata (single source of truth = pyproject), so the UI
  and `/health` report the real version.
- **Numbers in it/es/pt were read in German.** Added Italian/Spanish/
  Portuguese unit maps and removed the silent German fallback.
- **Lighter language variants didn't work.** `bcp47 → checkpoint` resolution
  was hardcoded to the `_24l` names, so loading the plain `german` left `de`
  resolving to an unloaded `german_24l` and falling back to the default
  language. Resolution is now built from the **actually-loaded** models, in
  both the HTTP and Wyoming paths.
- **Cloned voices didn't appear in Home Assistant without a restart.** The
  Wyoming `Describe` response replayed the voice list built once at startup.
  It now rebuilds from the **live** registry (lock-guarded), so a voice
  cloned at runtime shows up on the next HA integration refresh.

### Changed

- ⚠️ **Wyoming program renamed `pocket-tts` → `polyglot-tts`.** This changes
  the advertised program name, so Home Assistant creates a new TTS entity
  and orphans the old `tts.pocket_tts_*` — repoint any automations that
  reference it after upgrading.
- **No German number fallback for unlisted languages.** Numbers always use
  the requested language via num2words (left as digits if unsupported);
  units expand only where a localized map exists. An unlisted language is
  never silently read in German. Works for all 6 Kyutai languages and any
  future ones.
- **One checkpoint per language** is enforced in the config layer
  (`POCKET_TTS_LANGUAGES` deduped by language on save), not just in the UI —
  loading two variants of one language wasted RAM. The UI checkbox
  exclusivity now mirrors backend behaviour.

### Improved (text normalization)

- **Paragraphs and list items are terminated** with a period so they no
  longer run together ("Milch Brot Eier" → "Milch. Brot. Eier."). A cleanup
  pass tidies the punctuation artifacts the symbol maps leave behind.
- **Quotation marks are dropped** (delimiters, not spoken); in-word
  apostrophes are kept.
- **Dashes become comma pauses** (en/em-dash, and a free-standing spaced
  hyphen); in-word hyphens (`E-Auto`) and signed numbers (`-5`) are spared.
- **Dotted abbreviations are expanded** per language ("z. B." → "zum
  Beispiel", "e.g." → "for example", …), tolerant of spacing and case.

### Improved (web UI)

- Endpoints/ports on the dashboard reflect reality (HTTP shows the address
  you connected on; Wyoming/timing show the in-container bind port with a
  note about remapped Docker host ports).
- **Gated voice creation** — an explicit 3-step flow (pick/record a sample →
  enter a unique name, validated live → **Generate voice**). Mic recordings
  set the sample instead of uploading immediately.
- Languages are checkboxes; auto language-ID shows a hint when only one
  language is loaded; static assets are cache-busted so UI updates reach the
  browser without a manual hard-refresh.

## [0.6.2] — 2026-06-01

### Fixed

- **Web UI "Save & Restart" no longer kills the container permanently.**
  It previously called `os._exit(0)` and relied on a Docker restart policy
  to bring the container back — so on a container with `restart: "no"` (or
  none) the server just stopped and never returned. It now **re-execs the
  process in place** (`os.execv`): the container's PID 1 is replaced with a
  fresh `python -m polyglot_tts`, settings.json is re-read, and the UI
  polls + reconnects automatically (~30–90 s). Works regardless of the
  Docker restart policy.

## [0.6.1] — 2026-06-01

### Added

- **German ordinals in text normalization.** "Der 20. Juni" now reads as
  "der zwanzigste Juni" instead of "zwanzig" + a sentence break. Uses
  high-precision signals (an article/preposition before, or a month /
  "Jahrhundert" after) and applies dative declension ("am ersten Mai",
  "im zwanzigsten Jahrhundert"). Bare sentence-ending numbers stay
  cardinals ("Es waren 20." → "zwanzig"). German-only; other languages
  unaffected. Tests in `tests/test_text_norm.py`.

### Improved (web UI)

- **Voice upload now shows progress.** After upload the UI polls until the
  voice actually appears (embedding takes ~30 s, longer on CPU) and shows
  "Embedding… (Ns)" → "✅ ready", or a clear timeout hint pointing at the
  log / HF-token. Previously a single early refresh meant the new voice
  often wasn't visible yet, so it looked like nothing happened.
- **Settings fields now have help text and proper inputs.** Each setting
  shows a one-line description; `POCKET_TTS_DEVICE` is a dropdown
  (auto/cpu/cuda), booleans are true/false selects, the default voice is a
  dropdown of loaded voices, and `POCKET_TTS_LANGUAGES` offers a
  suggestion list of available checkpoints.

## [0.6.0] — 2026-06-01

### Added — built-in web UI

A small dependency-free web interface served by the HTTP endpoint at
`/ui` (e.g. `http://<host>:10201/ui`). Three tabs:

- **Dashboard** — device, loaded languages, voice count, uptime, last
  synthesis timing (with RTF), and a quick text-to-speech test player.
- **Voices** — list; add by drag-and-drop, file picker, or microphone
  recording; delete custom voices.
- **Settings** — view/edit runtime settings, enter a HuggingFace token
  (for cloning), and restart the container to apply restart-only settings.

Supporting pieces:

- `config_store.py` — UI-saved settings are persisted to a JSON file
  (`POCKET_TTS_CONFIG_FILE`, default `/app/config/settings.json`) and
  overlaid onto the environment at startup. Precedence: UI > compose env >
  default. Only an allow-list of keys is writable; the HF token is stored
  but never displayed or logged.
- Optional auth: `POCKET_TTS_UI_TOKEN` gates the UI page and `/api/ui/*`
  (settings / token / restart / status). The `/v1/audio/*` integration API
  stays open like before.
- HF token entered in the UI applies live — the next voice you add uses it,
  no restart needed.
- Mic recording uses `MediaRecorder` → ffmpeg transcode. Browsers require a
  secure context (`https://` or `localhost`); the UI detects a plain-HTTP
  LAN context and shows a hint. `docs/WEB_UI.md` covers a reverse-proxy
  (Tailscale serve / Caddy) for HTTPS.

### Fixed

- docker-compose.example.yaml: the HF-cache volume was mounted at
  `/root/.cache/huggingface`, but the container runs as UID 10001 with
  `HF_HOME=/app/.cache/huggingface`. Corrected to `/app/.cache/huggingface`
  so the cache actually persists.

### Docs

- `docs/WEB_UI.md` (new), `docs/CONFIGURATION.md` (UI vars), README bullet.
- compose example mounts `./config` and documents the restart-policy
  requirement for the UI's Save & Restart button.

## [0.5.7] — 2026-05-31

### Docs

- **`:cuda13` benchmark results, honestly.** Measured cu128 vs cu130 on a
  DGX Spark GB10: native sm_121 (cu130) does **not** improve steady-state
  RTF (~5.4× vs ~5.6×) — only the cold-start warmup is faster (~520 ms vs
  ~1490 ms). The ~5× ceiling is bound by the autoregressive decode +
  device-to-host transfer + framework overhead, not kernel architecture.
  CUDA.md / PERFORMANCE.md / PROJECT_STATE.md updated to say so plainly.
- `:cuda13` is kept (faster boot, native kernels, future-proof for the
  decode-path optimizations) but is **not** advertised as faster per
  request.
- The real performance levers are now the headline backlog items: GPU-side
  int16 conversion to halve D2H transfer, and `torch.compile` on the Mimi
  decoder. See [issue #1](https://github.com/Nosdave/polyglot-tts/issues/1).

### Verified live (DGX Spark, v0.5.6 image)

- ffmpeg auto-transcode: an iPhone-style `.m4a` dropped directly into
  `voices-extra/` clones without manual conversion. (The v0.5.4 fix,
  confirmed on real hardware.)

## [0.5.6] — 2026-05-31

### Added

- **New `:cuda13` image variant** built with PyTorch cu130 (CUDA 13).
  Native sm_120/sm_121 kernels for RTX 50xx and NVIDIA DGX Spark (GB10)
  — no JIT fallback. Requires NVIDIA driver ≥ 580 and a Turing-or-newer
  GPU (Pascal/Volta/Maxwell were dropped in CUDA 13).
- `docs/CUDA.md` — guide for choosing between `:latest`, `:cuda`, and
  `:cuda13`.

### Notes

- `:cuda` stays on cu128 (CUDA 12.8) as the broad-compatibility default —
  driver ≥ 525, Turing→Blackwell, doesn't break older GPUs/drivers.
  Moving the single tag to cu130 would silently break Pascal/Volta users
  and pinned-driver (Proxmox/ESXi) hosts. This mirrors what PyTorch,
  vLLM, and ComfyUI do.
- Addresses [issue #1](https://github.com/Nosdave/polyglot-tts/issues/1).

## [0.5.5] — 2026-05-31

Hardens the voice file-watcher against bad input.

### Fixed

- **No more retry-storm on a broken / non-voice file.** A file that
  fails to embed (corrupt audio, a non-audio file renamed to `.wav`,
  etc.) is now remembered by path+mtime and skipped on subsequent
  watcher events, instead of being re-attempted on every filesystem
  event. Replacing the file (new mtime) clears the skip and retries.
- **Oversized files are rejected up front.** Files over 100 MB in
  `voices-extra/` are rejected with a clear `.error` sidecar before any
  CPU/GPU work — guards against accidentally dropping a movie or disk
  image into the voice folder.

### Changed

- Embedding failures now log at WARNING (not full stack traces) and
  write a clearer sidecar message.

## [0.5.4] — 2026-05-31

More fixes from the live Spark voice-cloning test — both directly affect
the "drop a file, get a voice" UX.

### Fixed

- **m4a / aac / opus voice samples now work.** libsndfile can't decode
  m4a (AAC), so iPhone voice memos — the most common casual recording —
  failed with "Format not recognised". The image now bundles `ffmpeg`,
  and non-native formats are transcoded to a temp 24 kHz mono WAV before
  encoding. `.wav/.flac/.ogg/.mp3` still load natively (no transcode).
- **Deleting a same-stem file no longer drops an unrelated voice.** The
  watcher tracked voices by file *stem*, so deleting `myvoice.m4a`
  removed the voice that had been registered from `myvoice.wav`. The
  watcher now records which exact source file produced each voice and
  only removes the voice when *that* file is deleted.

### Added

- Unit tests for the watcher's path→voice tracking and the audio-format
  passthrough helper (`tests/test_voice_watcher.py`).
- `.aac` and `.opus` added to the accepted upload extensions.

## [0.5.3] — 2026-05-31

First real-hardware deployment (NVIDIA DGX Spark, GB10) surfaced two bugs
and corrected two inaccurate claims.

### Fixed

- **Named-volume permission bug.** With the v0.5.1 non-root hardening
  (UID 10001), mounting a fresh named volume at the HuggingFace cache
  path failed with `PermissionError: /app/.cache/huggingface`. The
  Dockerfile now pre-creates the full `/app/.cache/huggingface` path
  owned by `polyglot`, so Docker initializes the empty volume with the
  correct ownership. Affected anyone using the example compose with
  named volumes.

### Added

- `HF_TOKEN_FILE` support: the dispatcher reads the token from a file
  (Docker-secret friendly) when `HF_TOKEN` is not set directly.

### Docs

- **Corrected RTF claims.** README/PERFORMANCE.md previously claimed
  33–38× real-time on Blackwell. Direct measurement on a GB10 (both the
  public image and the pre-fork production image) shows **~5×** — there
  is no regression between them; the 33–38× figure was never reproduced.
  Tables now mark which rows are measured vs. estimated.
- Documented the **HF token flow for voice cloning** — the
  `kyutai/pocket-tts` model is gated; preset voices need no token.
  `.env` / Docker-secret / shell-env options in CONFIGURATION.md and
  VOICE_CLONING.md.
- Hardware table de-marketed: only DGX Spark and M4 rows are measured;
  the rest are flagged as estimates pending real benchmarks.

### Known issues

- GPU RTF on Blackwell GB10 is capped at ~5× by the cu128 build's lack
  of native sm_121 kernels (JIT fallback). Native sm_121 via a cu130
  build is being explored — see
  [issue #1](https://github.com/Nosdave/polyglot-tts/issues/1). 5× is
  still faster than real-time; streaming voice has no lag.

## [0.5.2] — 2026-05-31

### Changed

- CI now builds `linux/arm64` variants of both `:latest` (CPU) and
  `:cuda` natively on GitHub-hosted `ubuntu-24.04-arm` runners. The
  `:cuda` tag is now a multi-arch manifest covering `linux/amd64` +
  `linux/arm64` — NVIDIA DGX Spark (Grace + GB10), Jetson Orin/AGX,
  and amd64 NVIDIA hosts all `docker pull ghcr.io/nosdave/polyglot-tts:cuda`
  to get the right image automatically.
- Workflow uses the digest-based multi-platform pattern (per-arch
  build → merge by manifest list) so each arch builds on its native
  runner — no QEMU, no self-hosted Spark dependency, no manual push.

### Docs

- `docs/SPARK_BUILD.md` recast as "local-build for special cases"
  rather than "Spark users must do this". Default path is the
  multi-arch `:cuda` image.
- README's GPU bullet and use-cases table updated to reflect Spark /
  Jetson as first-class targets covered by the published image.
- `docker-compose.example.yaml` adds an explicit comment confirming
  the `:cuda` profile works on Spark/Jetson.

### Why the cleanup matters

Through v0.5.1, ARM64+CUDA users were directed to a local-build flow.
GitHub-hosted ARM64 runners went GA for public repos in August 2025,
making that constraint unnecessary. Multi-arch `:cuda` distribution is
now standard for the public image lineup.

## [0.5.1] — 2026-05-31

Post-launch hardening pass driven by an independent multi-agent code +
security + concurrency + docs review.

### Fixed (security)

- `POST /v1/audio/voices` now streams uploads to disk with a 50 MB
  hard cap. Previously the entire payload was buffered in RAM, which
  could OOM-kill a 4 GB host (HA Green, Pi 5) with a single curl.
- `DELETE /v1/audio/voices/{name}` and `POST /v1/audio/voices` now
  reject names containing path separators, `..`, or leading dots.
  Previously path-traversal was possible against the voices-extra/
  directory.
- `POST /v1/audio/speech` now caps the `input` field at 4000 characters
  to prevent GPU/CPU saturation via a single oversized request.
- Synthesis error responses no longer echo internal exception detail
  to the caller; tracebacks remain in server logs only.
- Filesystem path resolution now verifies that resolved paths stay
  under `voices-extra/` before unlink (defence-in-depth against
  symlink-escape).

### Fixed (concurrency)

- Added a shared per-model `threading.Lock` on `PolyglotCore` so the
  HTTP synth path no longer races with the Wyoming path's mutation of
  pocket-tts's non-thread-safe per-model state.
- Voice-state mutations from the HTTP on-demand-encode path now go
  through `core.set_voice_state()` under the existing voice lock,
  replacing the unguarded `voice_states[…][…] = …` write.
- On-demand preset encoding wrapped in try/except — a pocket-tts
  exception on an unknown preset now returns HTTP 404 instead of 500.

### Fixed (build)

- `pyproject.toml`: dropped the redundant `License :: OSI Approved ::
  MIT License` classifier. Newer setuptools (PEP 639) rejects this
  when `license = "MIT"` is also set; was the root cause of the
  first GHA build failing.
- GitHub Actions workflow: dropped `flavor: suffix=-cuda` which
  combined with `type=raw,value=cuda` produced `:cuda-cuda` tags
  instead of the intended `:cuda`.

### Fixed (correctness)

- `dispatcher.py` now passes the configured `POCKET_TTS_HOST` to
  `start_timing_server`. Previously the timing endpoint hardcoded
  `0.0.0.0` regardless of the env-declared bind address.
- `POCKET_TTS_LANGUAGE` (singular) is now read as a back-compat
  fallback for users migrating from araa47's fork, as advertised in
  the migration guide.

### Hardened

- Container now runs as a non-root system user (UID 10001).
  `HF_HOME` and `HOME` redirected to `/app`. Compose-mounted volumes
  must be writable by UID 10001.
- `python-multipart` floor bumped to `>=0.0.18` (CVE GHSA-59g5-xgcq-4qw3).
- `uvicorn[standard]` floor bumped to `>=0.30.0` (clears websockets CVE
  in the `[standard]` extra).
- `HF_TOKEN` presence is no longer logged on startup (silent is safer
  for log-aggregator alerting).

### Docs

- NOTICE corrected from "28" to "26" pre-made voices.
- README's CUDA-13 wording clarified — the published `:cuda` image is
  CUDA 12.8 only; CUDA 13 / ARM64-GPU is build-from-source.
- CONFIGURATION.md: `POCKET_TTS_LAZY_LOAD` now explicitly flagged as
  declared-but-not-yet-implemented.
- HOME_ASSISTANT.md: removed references to the unimplemented
  `POCKET_TTS_MIN_LID_CHARS` env var, replaced with the correct
  20-char threshold and explicit-hint workaround.

## [0.5.0] — 2026-05-30

Initial public release. Fork of araa47/wyoming_pocket_tts with
substantial additions.

### Added

- OpenAI-Speech-compatible HTTP endpoint on TCP `:10201`
  (`POST /v1/audio/speech`, `GET /v1/audio/voices`,
  `GET /v1/audio/languages`, `GET /health`).
- Voice-management REST API (POST/DELETE on `/v1/audio/voices`).
- File-watcher on `voices-extra/` for drop-and-clone workflow.
  Auto-embeds new WAVs within 1–2 s, no restart needed.
- Multi-language same-voice loading: one cloned voice speaks every
  loaded language.
- Lingua-based per-sentence language detection (replaces py3langid).
- Mukser-Fix: 120-sample fade-in + tail padding to suppress
  ConvTranspose click on streaming chunk boundaries.
- num2words-driven text normalization (de / en / fr).
- HA-aware streaming text-IN handler with sentence-buffering and
  drain-loop architecture.
- Side-channel timing HTTP endpoint on `:10299` for observability.
- Multi-arch Docker image: `:latest` (linux/amd64 + linux/arm64 CPU)
  and `:cuda` (linux/amd64 CUDA 12.8).
- Environment-driven configuration — no YAML.
- Three example compose profiles: cpu, cuda, minimal.

### Attribution

Built upon:
- [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts) — MIT (code), CC-BY 4.0 (models).
- [araa47/wyoming_pocket_tts](https://github.com/araa47/wyoming_pocket_tts) — MIT.

See [NOTICE](NOTICE) for the full attribution block.

### Known limitations

- Lazy-loading of additional languages (`POCKET_TTS_LAZY_LOAD=true`)
  is declared in the env-schema but not yet implemented. Will land in
  0.6.0.
- ARM64+CUDA13 builds for DGX Spark / Grace+Blackwell are
  production-verified locally but not yet in CI. Spark users build
  with `docker build --build-arg PYTORCH_INDEX=<spark-index>` for now.
