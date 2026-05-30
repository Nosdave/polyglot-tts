# Polyglot TTS

**Multi-language streaming TTS server with voice cloning.**
**Wyoming + OpenAI-Speech endpoints. Powered by [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts).**

---

## What it does

- 🌍 **True polyglot** — one voice speaks six languages (en / fr / de / it / es / pt). Automatic per-sentence language detection via [Lingua](https://github.com/pemistahl/lingua-py).
- ⚡ **Streaming-capable** — audio starts playing while text is still arriving. ~200 ms first chunk on CPU, under 100 ms on GPU.
- 🏠 **Wyoming endpoint** — plug-and-play with [Home Assistant](https://www.home-assistant.io/voice_control/) Voice-Pipeline and Voice-PE.
- 🤖 **OpenAI-Speech-compatible HTTP** — works out of the box with [OpenClaw](https://openclaw.ai/), LangChain, custom scripts, and anything else that speaks the OpenAI Speech API.
- 🎙️ **Voice cloning via drop-file** — copy a 10–30 s WAV into `voices-extra/` and the voice is available in *every* loaded language within ~30 s. No restart, no config edit.
- 🚀 **GPU-accelerated** — production-tested on NVIDIA Blackwell (DGX Spark, 33–38× real-time). CUDA 12 + 13 supported. Auto-detect at startup.
- 🥧 **Runs small too** — Pi 5 reaches real-time for a single language. HA Green (ARM64) and ordinary x86 boxes work.
- 🗣️ **26 built-in voices** plus unlimited user clones (Kyutai's voice library).
- 🔢 **Text normalization built in** — numbers, dates, currencies, units, and abbreviations spoken naturally in the target language.
- 🎚️ **Mukser-Fix** — fade-in + tail-padding eliminate click artifacts between streaming chunks.
- 🧱 **Sentence-buffering** — synthesizer gets complete sentences instead of token fragments, producing natural prosody.
- 🔒 **100 % self-hosted** — no cloud calls, no API keys required (except optional HuggingFace token for first model download).
- 🐳 **Single Docker image** — `linux/amd64` + `linux/arm64`, CPU + CUDA variants on `ghcr.io`.
- 🎛️ **Fully env-driven** — every behaviour is a single environment variable; no YAML editing required.

---

## Quick start

### With GPU (NVIDIA)

```bash
docker run -d --gpus all \
  -p 10200:10200 -p 10201:10201 \
  -v $(pwd)/voices-extra:/app/voices-extra \
  ghcr.io/nosdave/polyglot-tts:cuda
```

### CPU only (HA Green, Pi 5, x86 servers)

```bash
docker run -d \
  -p 10200:10200 -p 10201:10201 \
  -v $(pwd)/voices-extra:/app/voices-extra \
  -e POCKET_TTS_LANGUAGES=english_2026-04 \
  ghcr.io/nosdave/polyglot-tts:latest
```

### Test it

```bash
curl -X POST http://localhost:10201/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"polyglot-1","input":"Bonjour le monde","voice":"eve","response_format":"mp3"}' \
  --output hello.mp3
```

---

## Use cases

| You want to … | Use this endpoint | See |
|---|---|---|
| Talk through your Home Assistant Voice-PE | Wyoming `:10200` | [docs/INTEGRATIONS/HOME_ASSISTANT.md](docs/INTEGRATIONS/HOME_ASSISTANT.md) |
| Wire it up to OpenClaw | OpenAI-Speech `:10201` | [docs/INTEGRATIONS/OPENCLAW.md](docs/INTEGRATIONS/OPENCLAW.md) |
| Drop in to LangChain / custom scripts | OpenAI-Speech `:10201` | OpenAI Python SDK with `base_url=` override |
| Clone your own voice | drop a WAV in `voices-extra/` | [docs/VOICE_CLONING.md](docs/VOICE_CLONING.md) |
| Tune performance / device | environment variables | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Migrate from araa47's fork | rename a few env vars | [docs/MIGRATION_FROM_ARAA47.md](docs/MIGRATION_FROM_ARAA47.md) |

---

## Hardware

| Box | Mode | RTF (Real-Time Factor) | First-chunk latency |
|---|---|---|---|
| NVIDIA DGX Spark (ARM64 + Blackwell, CUDA 13) | Multi-lang DE/EN/FR | 33–38× | ~80 ms |
| RTX 3060+ (amd64, CUDA 12) | Single-lang | 20–40× | ~100 ms |
| MacBook Air M4 (CPU only) | Single-lang | ~6× | ~200 ms |
| Mini-PC N100 / similar (CPU) | Single-lang | ~3–4× | ~400 ms |
| Raspberry Pi 5 (CPU) | Single-lang | ~2–3× | ~500 ms |
| Raspberry Pi 4 (CPU) | Single-lang | ~1–2× | ~800 ms |
| HA Green (ARM64 CPU) | Single-lang | ~1–2× | ~800 ms |

RTF > 1× means real-time-or-better. Multi-language mode roughly multiplies RAM usage by `N` (one model per language); inference per request is unaffected.

See [docs/PERFORMANCE.md](docs/PERFORMANCE.md) for benchmark methodology.

---

## Voice cloning in one paragraph

Record (or pull from any source) a clean 10–30 second WAV. Copy it to `voices-extra/aria.wav`. Watch the log: within a few seconds you'll see `Voice 'aria' ready in N language(s)`. Now `voice: "aria"` works in every endpoint — Wyoming, HTTP, anywhere. The voice is computed once and works across *all* loaded languages without per-language re-recording. To remove the voice, delete the file. To replace it, overwrite it.

See [docs/VOICE_CLONING.md](docs/VOICE_CLONING.md) for tips on recording quality, error handling, and the REST API for programmatic voice management.

---

## Companion project

For multi-language Tier-1 voice intents in Home Assistant — the *input*
side of the voice pipeline — see
[**Polyglot Assist**](https://github.com/Nosdave/hass-polyglot-assist).
A custom HA conversation agent that does deterministic Hassil matching
across multiple languages with LLM fallback proxy, solving HA's
single-language sentence-trigger limit. Pairs naturally with Polyglot
TTS on the output side.

## Why fork

This project builds on [araa47/wyoming_pocket_tts](https://github.com/araa47/wyoming_pocket_tts), which added the Wyoming protocol layer on top of [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts). Polyglot TTS adds:

- Multi-language same-voice loading (one voice across all six languages)
- Lingua-based automatic language detection per sentence
- HA-aware sentence-buffering for streaming conversational agents
- num2words-driven text normalization (de / en / fr)
- Mukser-Fix for click-free streaming audio
- CUDA support across amd64 + arm64 (including Blackwell)
- OpenAI-Speech-compatible HTTP endpoint (the “universal connector”)
- File-watcher voice-cloning workflow
- REST API for voice management

See [NOTICE](NOTICE) for full attribution.

---

## License

Code: MIT — see [LICENSE](LICENSE).
Models: CC-BY 4.0 — Kyutai's models retain their original licence (attribution required).

---

## Contributing & support

This is a personal project, maintained best-effort. Bug reports, PRs, and feature requests are welcome at [github.com/Nosdave/polyglot-tts/issues](https://github.com/Nosdave/polyglot-tts/issues). See [CONTRIBUTING.md](CONTRIBUTING.md) for the basics. For security-relevant reports see [SECURITY.md](SECURITY.md).
