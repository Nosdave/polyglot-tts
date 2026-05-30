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
| `POCKET_TTS_LAZY_LOAD` | `false` | (Experimental) Load missing language models on first request. Increases first-call latency by 10–30 s for new languages. |
| `POCKET_TTS_MIN_SYNTH_CHARS` | `30` | First-flush threshold for streaming. Lower = faster first audio at the cost of less natural prosody. |
| `POCKET_TTS_LOG_LEVEL` | `INFO` | Standard Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## Secrets / credentials

| Variable | Default | What it does |
|---|---|---|
| `HF_TOKEN` | unset | HuggingFace token. Only needed if a checkpoint you load is gated. |

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
