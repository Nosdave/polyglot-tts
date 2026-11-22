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

Viewing and listening are open: the page itself, the dashboard, the voice
list, the settings view and the test player need no login. **Changing
anything needs the management token** — adding or deleting a voice, saving
settings or the pronunciation dictionary, storing a HuggingFace token,
restarting.

### Where the token comes from

- **Generated (default).** On first start the server creates a random token
  and stores it as `ui_token` next to the settings file (default
  `/app/config/ui_token`, readable only by the container user). The log
  shows the path, not the value:

  ```
  Management token: stored in /app/config/ui_token (mode 0600). …
  ```

  Read it with:

  ```bash
  docker exec polyglot-tts cat /app/config/ui_token
  ```

  Mount `/app/config` (see below) so the token survives re-creating the
  container. To get a new token, delete the file and restart.

- **Your own.** Set `POCKET_TTS_UI_TOKEN` — it takes precedence over the
  file. Use a long random value, e.g. `openssl rand -hex 32`.

  ```
  POCKET_TTS_UI_TOKEN=<long random value>
  ```

- If the config directory is not writable and no token is set, the server
  generates a token for that run only and prints it once in the log.

### Logging in

Click **Login** in the header (the dialog also opens by itself when you try
a management action), paste the token, confirm. The browser remembers it
(localStorage) until you click **Logout**. If the token changes on the
server, the page drops the stored one and asks again.

### Using the token from scripts

Send it as a Bearer header:

```bash
curl -X POST http://<host>:10201/api/ui/restart \
  -H "Authorization: Bearer <token>"
```

The header `X-UI-Token: <token>` and the query parameter `?token=<token>`
of earlier versions are still accepted. Prefer the header: a token in a URL
ends up in browser history and proxy logs.

### What needs the token

| Route | Token |
|---|---|
| Wyoming (`:10200`) | no |
| `POST /v1/audio/speech`, `POST /v1/text/normalize` | no |
| `GET` — `/health`, `/v1/audio/voices`, `/v1/audio/languages`, `/ui`, `/api/ui/status`, `/api/ui/config`, `/api/ui/replacements` | no |
| `POST /v1/audio/voices`, `DELETE /v1/audio/voices/{name}` | **yes** |
| `POST /api/ui/config`, `POST /api/ui/replacements`, `POST /api/ui/restart` | **yes** |
| `GET /api/ui/session` (token check used by the login) | **yes** |

Any other request that is not a `GET` is rejected without a token as well.

> **Upgrading from 0.9.0 or older:** the management routes above were open
> (or, with `POCKET_TTS_UI_TOKEN` set, only `/api/ui/*` was protected). After
> the upgrade they return 401 until the token is sent. Home Assistant,
> OpenClaw and other synthesis clients need no change.

> **Scope:** the token protects *changes*. Synthesis and the read-only
> routes are reachable by everyone who can reach the port. If that is not
> acceptable on your network, put the server behind a reverse proxy with
> authentication (see below).

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

The **Save & Restart** button writes the settings and then **re-execs the
server process in place** — the container stays alive, the dispatcher
re-reads the settings file, and models reload + warm up (~30–90 s). The UI
polls and reconnects automatically. This works **regardless of the Docker
restart policy** (no `restart: unless-stopped` required for the button —
though that policy is still recommended for normal container resilience).

Live settings (default voice, text-norm, auto-LID, min-synth-chars) and the
HF token apply immediately without any restart.

## HuggingFace token

The token is only needed for **voice cloning** (the gated Kyutai model).
Enter it in Settings → it's stored to the settings file and applied to the
running process, so the **next voice you add** uses it — no restart needed.
The token value is never displayed back or logged.
