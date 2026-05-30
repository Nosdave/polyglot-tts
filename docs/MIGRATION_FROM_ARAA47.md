# Migrating from araa47/wyoming_pocket_tts

Polyglot TTS forks [araa47/wyoming_pocket_tts](https://github.com/araa47/wyoming_pocket_tts)
and adds substantial features (see [README.md](../README.md) for the full
list). This guide helps you move an existing araa47-based deployment over.

## Switch the image

```diff
- image: araa47/wyoming-pocket-tts:latest
+ image: ghcr.io/nosdave/polyglot-tts:latest      # CPU
+ image: ghcr.io/nosdave/polyglot-tts:cuda        # CUDA
```

## Ports

Polyglot now exposes **two** TCP ports by default:

| Port | Purpose | araa47 had this? |
|---|---|---|
| `10200` | Wyoming protocol (HA, Rhasspy) | Yes |
| `10201` | OpenAI-Speech-compatible HTTP | **New** |
| `10299` | Side-channel timing endpoint (optional) | **New** |

If you don't need the HTTP endpoint:

```yaml
environment:
  POCKET_TTS_HTTP_PORT: ""
```

## Environment variables

araa47's fork used these — most are preserved verbatim, a few have moved:

| Old (araa47) | New (Polyglot) | Note |
|---|---|---|
| `POCKET_TTS_LANGUAGE` | `POCKET_TTS_LANGUAGES` | Now comma-separated. The old singular form is read as a back-compat fallback. |
| `POCKET_TTS_VOICE` | `POCKET_TTS_VOICE` | unchanged |
| `POCKET_TTS_TIMING_PORT` | `POCKET_TTS_TIMING_PORT` | unchanged |
| `POCKET_TTS_WARMUP` | `POCKET_TTS_WARMUP` | unchanged |
| `POCKET_TTS_TEXT_NORM` | `POCKET_TTS_TEXT_NORM` | unchanged |
| `POCKET_TTS_AUTO_LID` | `POCKET_TTS_AUTO_LID` | unchanged |
| `POCKET_TTS_MIN_SYNTH_CHARS` | `POCKET_TTS_MIN_SYNTH_CHARS` | unchanged |
| `HF_TOKEN` | `HF_TOKEN` | unchanged |
| *(was hard-coded path)* | `POCKET_TTS_VOICES_DIR` + `POCKET_TTS_VOICES_EXTRA_DIR` | Split into two: built-in and user-mounted. |

See [docs/CONFIGURATION.md](CONFIGURATION.md) for the full list.

## Voices

Your existing voices directory (typically mounted at `/share/tts-voices`)
should be remounted as `/app/voices-extra`:

```diff
volumes:
-  - /share/tts-voices:/share/tts-voices
+  - /share/tts-voices:/app/voices-extra
```

The new file-watcher will pick up your existing voices on startup and
re-watch the directory for new additions, so drops at runtime now work
without restart.

## Home Assistant integration

No change required. The Wyoming endpoint stays at the same port and
protocol. Your existing HA "Wyoming Protocol" integration entry keeps
working.

If your HA's voice list looks different after the swap, restart the
Wyoming integration: Settings → Devices & Services → Wyoming Protocol →
3-dot menu → Reload.

## What's new you might want to use

After migrating, three new things are available that weren't before:

1. **OpenAI-Speech HTTP endpoint on `:10201`** —
   [docs/INTEGRATIONS/OPENCLAW.md](INTEGRATIONS/OPENCLAW.md) for the
   OpenClaw setup; the same endpoint works for LangChain, custom scripts,
   anything that speaks the OpenAI Speech API.
2. **File-watcher voice cloning** — drop a WAV and the voice is live
   without restart. See [docs/VOICE_CLONING.md](VOICE_CLONING.md).
3. **Voice REST API** — `POST /v1/audio/voices` with a multipart upload
   to add a voice programmatically; `DELETE /v1/audio/voices/{name}` to
   remove. Same endpoint as #1.

## Rolling back

The araa47 image and the Polyglot image use compatible voice-file
formats. To roll back, swap the image tag back, restore the old volume
mount path, and restart. No data migration is required.
