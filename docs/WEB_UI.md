# Web UI

Polyglot TTS ships a small built-in web interface for managing voices,
viewing status, and editing settings. It's served by the HTTP endpoint, so
it's available wherever that endpoint is.

```
http://<host>:10201/ui
```

(If you changed `POCKET_TTS_HTTP_PORT`, use that port.)

## What it does

Three tabs:

- **Dashboard** — device, loaded languages, voice count, uptime, the last
  synthesis timing (with RTF), and a quick text-to-speech test player.
- **Voices** — list voices; add a voice by drag-and-drop, file picker, or
  microphone recording; delete custom voices.
- **Settings** — view and edit the runtime settings, enter a HuggingFace
  token (for voice cloning), and restart the container to apply
  restart-only settings.

## Authentication

By default the UI is **open** (LAN-only, like the rest of the server).

To require a token, set:

```
POCKET_TTS_UI_TOKEN=your-secret-here
```

When set, the `/ui` page and the `/api/ui/*` endpoints require the token
(sent as an `X-UI-Token` header; the page prompts for it and remembers it
in your browser). 

> **Scope of the token:** it gates the UI page and the settings / HF-token /
> restart / status API. It does **not** gate the `/v1/audio/*` integration
> API (voice upload/delete/synthesis), which stays open like the rest of
> the OpenAI-Speech surface. For full lockdown, put the server behind a
> reverse proxy with auth (see below).

## Microphone recording needs a secure context

Browsers only allow microphone access (`getUserMedia`) on a **secure
context**: `https://` or `http://localhost`. Over a plain
`http://<lan-ip>:10201/ui` the browser **blocks the mic** — the UI detects
this and shows a hint. File upload always works.

To enable mic recording from another machine, expose the UI over HTTPS via
a reverse proxy. Two easy options:

### Tailscale serve

```bash
tailscale serve --bg --https=443 http://localhost:10201
```

Then open `https://<your-tailnet-name>.ts.net/ui` — mic works.

### Caddy

```caddyfile
tts.example.com {
    reverse_proxy localhost:10201
}
```

Caddy auto-provisions a TLS cert; open `https://tts.example.com/ui`.

## Settings and restart behaviour

Settings are written to a JSON file (`POCKET_TTS_CONFIG_FILE`, default
`/app/config/settings.json`). To persist edits across restarts, mount that
path:

```yaml
volumes:
  - ./config:/app/config
```

Precedence: **UI-saved settings > compose/shell env > image default.** Once
you save a key in the UI it overrides your compose env; clear it in the UI
(empty the field and save) to fall back to compose.

Some settings only take effect after a restart — they're marked
`restart` in the UI:

- `POCKET_TTS_LANGUAGES` (models load at boot)
- `POCKET_TTS_DEVICE`
- `POCKET_TTS_WARMUP`
- `POCKET_TTS_LAZY_LOAD`

The **Save & Restart** button writes the settings and then exits the
process. For the container to come back, it **must have a Docker restart
policy**:

```yaml
services:
  polyglot-tts:
    restart: unless-stopped
```

Without that policy the container will simply stop. Live settings
(default voice, text-norm, auto-LID, min-synth-chars) and the HF token
apply immediately without a restart.

## HuggingFace token

The token is only needed for **voice cloning** (the gated Kyutai model).
Enter it in Settings → it's stored to the settings file and applied to the
running process, so the **next voice you add** uses it — no restart needed.
The token value is never displayed back or logged.
