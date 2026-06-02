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
| `POCKET_TTS_CONFIG_FILE` | `/app/config/settings.json` | Where UI-saved settings are persisted. Mount this path to keep edits across restarts. |

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
