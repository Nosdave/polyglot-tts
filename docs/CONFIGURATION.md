# Configuration

Polyglot TTS is fully configured through environment variables. No YAML,
no config file. Pass variables via `docker run -e …`, `docker-compose`,
your add-on options, or whatever orchestrator you use.

## Endpoints

| Variable | Default | What it does |
|---|---|---|
| `POCKET_TTS_WYOMING_PORT` | `10200` | Wyoming protocol TCP port. Set to empty string to disable. |
| `POCKET_TTS_HTTP_PORT` | `10201` | OpenAI-Speech HTTP TCP port. Set to empty string to disable. |
| `POCKET_TTS_TIMING_PORT` | `10299` | Side-channel timing endpoint (sparkdash-style observability). Set to empty to disable. |
| `POCKET_TTS_HOST` | `0.0.0.0` | Bind address for all endpoints. |

At least one of the Wyoming or HTTP ports must be non-empty.

## Languages

| Variable | Default | What it does |
|---|---|---|
| `POCKET_TTS_LANGUAGES` | `english_2026-04,german_24l,french_24l` | Comma-separated list of Pocket-TTS checkpoint names to load at startup. |

Available checkpoints today:

- `english_2026-04`
- `german_24l`
- `french_24l`
- `italian_24l`
- `spanish_24l`
- `portuguese_24l`

Loading more languages multiplies RAM usage. The Lingua-based LID picks
which checkpoint to use per request.

## Voices

| Variable | Default | What it does |
|---|---|---|
| `POCKET_TTS_VOICE` | `eve` | Default voice when no per-request voice is specified. |
| `POCKET_TTS_VOICES_DIR` | `/app/voices` | Reserved for image-shipped customs (empty today). |
| `POCKET_TTS_VOICES_EXTRA_DIR` | `/app/voices-extra` | User-mounted host volume. File-watcher watches this. |

### Per-language voice references (accent-free multilingual voices)

A cloned voice normally uses **one** reference recording, encoded against every
loaded language model. That single embedding carries the accent of whatever
language the recording was in — so a voice cloned from English audio speaks
German intelligibly but with an English accent.

To remove that, give the **same voice** a separate reference per language using
the filename convention `<voice>.<bcp47>.<ext>` in `voices-extra/`:

```
EL_Jarvis.de.mp3     # German reference  -> encoded only against the German model
EL_Jarvis.en.mp3     # English reference -> encoded only against the English model
EL_Jarvis.fr.mp3     # French reference  -> encoded only against the French model
EL_Jarvis.mp3        # optional fallback -> used for any language without its own file
```

All files sharing the voice name (`EL_Jarvis`) form **one** voice; each language
model speaks it from a native-language reference -> no cross-language accent.

- The tag must be a known language code (`de`, `en`, `fr`, `it`, `es`, `pt`).
  A name like `my_poly_voice.mp3` is unaffected — `poly` is not a language.
- **Fallback / backwards compatible:** a voice with a single untagged file
  behaves exactly as before (one embedding shared across all languages).
- A language with neither its own file nor an untagged fallback simply isn't
  cloned for that voice (it falls back to the default voice at synth time).
- Drop, replace or delete any of a voice's files at runtime — the file-watcher
  rebuilds the whole voice from its current files.

## Device

| Variable | Default | What it does |
|---|---|---|
| `POCKET_TTS_DEVICE` | `auto` | `auto` chooses CUDA if a GPU is visible, otherwise CPU. Force-set to `cpu` or `cuda` to override. |

## Behaviour

| Variable | Default | What it does |
|---|---|---|
| `POCKET_TTS_WARMUP` | `true` | Run a short synthesis per loaded language at startup to warm up JIT-compiled CUDA kernels and worker threads. |
| `POCKET_TTS_TEXT_NORM` | `true` | Apply Markdown-strip, unit-expansion, number-to-words before synthesis. |
| `POCKET_TTS_AUTO_LID` | `true` | Enable Lingua-based per-sentence language detection. |
| `POCKET_TTS_LAZY_LOAD` | `false` | **Declared but not yet implemented** — slated for 0.6.0. Today: all languages listed in `POCKET_TTS_LANGUAGES` are loaded eagerly at startup. |
| `POCKET_TTS_MIN_SYNTH_CHARS` | `30` | First-flush threshold for streaming. Lower = faster first audio at the cost of less natural prosody. |
| `POCKET_TTS_TEMP` | `0.7` | Sampling temperature (`0.1`–`1.5`). Higher = more expressive/varied but less stable; lower = flatter/more consistent. Sets the **global** value (all voices and languages). Applies **live** — `model.temp` is read at each decode step, so saving it via the UI/config takes effect on the next synthesis with no restart. Override per call with the `temperature` field on `POST /v1/audio/speech`. |
| `POCKET_TTS_OUTPUT_GAIN` | `1.0` | Output volume multiplier (`0.0`–`4.0`). `1.0` = unchanged, `<1` quieter, `>1` louder; the signal is clipped to full scale after scaling (very high values distort). Applies **live** to both endpoints. Override per call with the `gain` field on `POST /v1/audio/speech`. |
| `POCKET_TTS_VOICE_NORMALIZE` | `true` | Loudness-normalize a voice sample (EBU R128 `loudnorm`) when cloning, so a quiet recording still makes a strong voice prompt. Read at clone time; needs ffmpeg (bundled in the image). |
| `POCKET_TTS_LOG_LEVEL` | `INFO` | Standard Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## Secrets / credentials

| Variable | Default | What it does |
|---|---|---|
| `HF_TOKEN` | unset | HuggingFace token. **Only needed for voice cloning** — see below. Basic TTS with the 26 built-in voices needs no token. |
| `HF_TOKEN_FILE` | unset | Path to a file containing the token (for Docker secrets). Read at startup if `HF_TOKEN` is not already set. |

### When you need an HF token

The preset voices and all language models load **without any token**. The
*voice-cloning* model (`kyutai/pocket-tts`) is gated on HuggingFace, so to
clone your own voices you need a (free) token:

1. Create a free account at <https://huggingface.co>.
2. Visit <https://huggingface.co/kyutai/pocket-tts> and click
   **"Agree and access repository"** (one-time gate acceptance).
3. Create a read token at <https://huggingface.co/settings/tokens>.
4. Provide it to the container by **one** of these (never paste it into a
   compose file you might commit):

   **`.env` file (simplest):**
   ```
   # .env  (chmod 600; .gitignore already excludes it)
   HF_TOKEN=hf_xxxxxxxxxxxxx
   ```
   ```yaml
   services:
     polyglot-tts:
       env_file: .env
   ```

   **Docker secret:**
   ```yaml
   secrets:
     hf_token:
       file: ./hf_token.txt
   services:
     polyglot-tts:
       secrets: [hf_token]
       environment:
         HF_TOKEN_FILE: /run/secrets/hf_token
   ```

   **Shell environment:**
   ```bash
   export HF_TOKEN=hf_xxxxxxxxxxxxx
   docker compose up -d
   ```

The token value is never logged.

## Web UI

| Variable | Default | What it does |
|---|---|---|
| `POCKET_TTS_UI_TOKEN` | unset | If set, the web UI (`/ui`) and its `/api/ui/*` endpoints require this token. Unset = open (LAN-only). |
| `POCKET_TTS_CONFIG_FILE` | `/app/config/settings.json` | Where UI-saved settings are persisted. Mount `/app/config` to keep edits across restarts. **The mount must be writable by the container user (UID 10001)** — prefer a named volume (inherits the right ownership); a host bind dir must be `chown`ed to `10001` or the UI's "Save" fails. |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` | Native PyTorch CUDA-allocator knob, shipped on by default. Grows VRAM segments on demand instead of pre-reserving large blocks → smaller footprint and fewer fragmentation OOMs, which matters on shared/unified memory (DGX Spark). Override the whole value to tune further (e.g. `expandable_segments:True,max_split_size_mb:128`). No effect on the CPU image. |

See [docs/WEB_UI.md](WEB_UI.md) for the full UI guide (mic-recording HTTPS
requirement, reverse-proxy setup, settings-restart behaviour).

## Disabling endpoints

To run Wyoming-only (HA-only deployment, smaller attack surface):

```bash
POCKET_TTS_HTTP_PORT=
```

To run HTTP-only (no HA, just OpenClaw / scripts):

```bash
POCKET_TTS_WYOMING_PORT=
```

## Disabling features

| Goal | Setting |
|---|---|
| Skip warmup (faster start, slower first call) | `POCKET_TTS_WARMUP=false` |
| Force a specific language regardless of LID | `POCKET_TTS_AUTO_LID=false` + set `POCKET_TTS_LANGUAGES` to that one language |
| Receive raw input without text normalization | `POCKET_TTS_TEXT_NORM=false` |
| Disable side-channel timing endpoint | `POCKET_TTS_TIMING_PORT=` |
