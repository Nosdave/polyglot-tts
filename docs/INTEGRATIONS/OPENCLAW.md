# OpenClaw Integration

Polyglot TTS exposes an OpenAI-Speech-compatible HTTP endpoint on
TCP `10201`, which is exactly what
[OpenClaw](https://docs.openclaw.ai/tools/tts)'s OpenAI TTS provider expects
when its base URL is overridden.

Two ways to wire it up — both supported by OpenClaw. Pick whichever is
more convenient for your deployment.

## Variant A — Environment variables (recommended for now)

Set these on the OpenClaw process or container:

```bash
OPENAI_API_KEY=dummy-not-checked
OPENAI_TTS_BASE_URL=http://polyglot-tts:10201/v1
```

OpenClaw routes its TTS calls through the OpenAI provider's `baseUrl`, and
the `OPENAI_TTS_BASE_URL` env wins over the default `https://api.openai.com/v1`.

OpenClaw still requires *some* value in `OPENAI_API_KEY` — Polyglot ignores
it, but it must be non-empty.

## Variant B — `openclaw.json` configuration

Edit your OpenClaw configuration (typically `~/.openclaw/openclaw.json` or
`/config/.openclaw/openclaw.json` when running as a Home Assistant add-on):

```json
{
  "messages": {
    "tts": {
      "auto": "always",
      "provider": "openai",
      "openai": {
        "apiKey": "dummy",
        "baseUrl": "http://polyglot-tts:10201/v1",
        "model": "polyglot-1",
        "speakerVoice": "eve"
      }
    }
  }
}
```

Restart OpenClaw to pick up the change.

> **Heads-up — known OpenClaw bug** [openclaw/openclaw #57506](https://github.com/openclaw/openclaw/issues/57506):
> in some recent OpenClaw builds the `messages.tts.openai.baseUrl` key is
> ignored and the TTS tool falls back to Edge TTS. If Variant B doesn't
> appear to take effect, switch to Variant A — the env-var path doesn't
> go through that code branch.

## Test the route end-to-end

```bash
curl -X POST http://polyglot-tts:10201/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"polyglot-1","input":"Hello from OpenClaw","voice":"eve","response_format":"mp3"}' \
  --output test.mp3
```

If `test.mp3` plays cleanly, OpenClaw will work too.

## Multi-language

OpenClaw doesn't tell Polyglot the language explicitly, so Polyglot
detects it per request via Lingua-based language ID. If most of your
prompts are in one language, set `POCKET_TTS_LANGUAGES` to *only* that
language for tighter LID + lower RAM usage.

If you want explicit control per request, OpenClaw's `extraBody` field
can pass a `language` parameter (Polyglot-specific extension):

```json
"openai": {
  "extraBody": { "language": "de" }
}
```

## Voice changes

To change the speaking voice:

- Variant A: set `POCKET_TTS_VOICE=<name>` on the Polyglot container and
  restart Polyglot.
- Variant B: change `speakerVoice` in `openclaw.json` and restart OpenClaw.

For custom voices, see [docs/VOICE_CLONING.md](../VOICE_CLONING.md) — just
drop a WAV in `voices-extra/` and reference the file stem as the voice
name.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| TTS silent, OpenClaw log shows Edge TTS | Known bug #57506; use Variant A |
| HTTP 404 on `/v1/audio/speech` | Pointed at `:10200` (Wyoming) instead of `:10201` (HTTP) |
| HTTP 401/403 | Some OpenClaw versions validate the API-key format; set `OPENAI_API_KEY=sk-dummy0000000000000000000000000000` |
| Voice not found | Use `GET http://polyglot-tts:10201/v1/audio/voices` to list available voices |
