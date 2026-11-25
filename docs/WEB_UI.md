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
anything needs the management password** — adding or deleting a voice, saving
settings or the pronunciation dictionary, storing a HuggingFace token,
restarting.

### First start: set a password

A new server has no password, and until it has one the management functions
are closed (not open). To set it:

1. Start the server.
2. Within **10 minutes**, open `http://<host>:10201/ui` — on a phone is fine.
   The page shows **Set password**.
3. Enter a password twice (at least 10 characters; letters, digits and
   common symbols — no umlauts or emoji) and confirm. You are logged in, and
   your browser or password manager offers to save it (user name `admin`).

After the 10 minutes the form is gone and the server answers 403 to setup
attempts. **Restart the server** to get another 10 minutes.

> **Trade-off:** during the setup window, the first client that reaches the
> UI sets the password. On a home network right after a deliberate restart
> this is usually fine. Where it is not, set the password through the
> environment (below) — then nothing can be set from a browser at all — or
> disable the window with `POCKET_TTS_UI_SETUP_MINUTES=0`.

The password is stored as a salted hash (scrypt) in `ui_password.json`
next to the settings file (default `/app/config/`), readable only by the
container user. Mount `/app/config` (see below) so it survives re-creating
the container; without the mount every new container asks for a password
again.

### Password from the environment

```
POCKET_TTS_UI_TOKEN=<your password>
```

If this variable is set, its value is the password. It takes precedence over
a password set in the browser, and the browser can then neither set nor
change the password.

### Logging in, logging out, changing the password

- **Login** (header, or automatically when you try a management action):
  enter the password. The browser remembers it (localStorage) until you click
  **Logout**; if the password changes on the server, the page asks again.
- **Change password:** Settings → *Password*. Asks for the current and the
  new password. Other devices have to log in again afterwards.
- The forms work with password managers on phone and desktop (user name
  `admin`).
- After 10 failed attempts within a minute, further attempts are refused for
  a moment (HTTP 429). A browser that is already logged in keeps working.

### Forgotten password

Delete the hash file and restart — the setup window opens again:

```bash
docker exec polyglot-tts rm /app/config/ui_password.json
docker restart polyglot-tts
```

Then open the UI within 10 minutes and set a new password. (If the password
comes from `POCKET_TTS_UI_TOKEN`, look it up or change it there.)

### Using the password from scripts

Send it as a Bearer header:

```bash
curl -X POST http://<host>:10201/api/ui/restart \
  -H "Authorization: Bearer <password>"
```

The header `X-UI-Token: <password>` and the query parameter
`?token=<password>` of earlier versions are still accepted. Prefer the
Bearer header: a password in a URL ends up in browser history and proxy logs.

### What needs the password

| Route | Password |
|---|---|
| Wyoming (`:10200`) | no |
| `POST /v1/audio/speech`, `POST /v1/text/normalize` | no |
| `GET` — `/health`, `/v1/audio/voices`, `/v1/audio/languages`, `/ui`, `/api/ui/status`, `/api/ui/config`, `/api/ui/replacements` | no |
| `POST /api/ui/setup` | no — but it only works while no password exists and the setup window is open |
| `POST /v1/audio/voices`, `DELETE /v1/audio/voices/{name}` | **yes** |
| `POST /api/ui/config`, `POST /api/ui/replacements`, `POST /api/ui/restart` | **yes** |
| `POST /api/ui/password` (change password) | **yes**, plus the current password in the body |
| `GET /api/ui/session` (password check used by the login) | **yes** |

Any other request that is not a `GET` is rejected without the password as
well.

### Special cases

- **Config directory not writable:** no hash can be stored. The server then
  generates a one-time password for that run and prints it once in the log.
  Fix the mount (writable by UID 10001) or use `POCKET_TTS_UI_TOKEN`.
- **`ui_password.json` damaged:** the management functions stay closed and
  the log says so. Delete the file and restart.

> **Upgrading from 0.9.0 or older:** the management routes above were open
> (or, with `POCKET_TTS_UI_TOKEN` set, only `/api/ui/*` was protected). After
> the upgrade they return 401 until a password is set and sent. Home
> Assistant, OpenClaw and other synthesis clients need no change.

> **Scope:** the password protects *changes*. Synthesis and the read-only
> routes are reachable by everyone who can reach the port, and over plain
> `http://` the password travels unencrypted. If that is not acceptable on
> your network, put the server behind a reverse proxy with TLS and
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
