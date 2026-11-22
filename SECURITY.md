# Security

## Reporting a vulnerability

If you find a security issue please **do not open a public GitHub
issue**. Instead:

- Open a private "Security Advisory" on the GitHub repo, or
- Email the maintainer (see GitHub profile) with the details.

A reply within a week is the goal, with no promise. This is a personal
project.

## Threat model

Polyglot TTS is intended to run on **trusted local networks** (LAN,
Tailscale, equivalent).

- **Synthesis and read-only routes are unauthenticated**: anyone who can
  reach the ports can synthesize audio (Wyoming, `POST /v1/audio/speech`)
  and list voices, languages, status and settings.
- **Everything that changes state requires the management token** (since
  0.9.1): voice upload/delete, settings, pronunciation dictionary, restart.
  The token is `POCKET_TTS_UI_TOKEN`, or a random one generated on first
  start and stored as `ui_token` (mode 0600) in the config directory. See
  [docs/WEB_UI.md](docs/WEB_UI.md#authentication).

If you expose Polyglot TTS to a less-trusted network, put it behind a
reverse proxy with auth (e.g. Caddy + basic-auth, or your existing
ingress) — the token does not limit who may synthesize.

## What Polyglot does not do

- It does not phone home.
- It does not transmit audio or text anywhere outside the host network.
- It does not log audio data to disk (only timing and metadata).

## Hard limits in place (since 0.5.1)

- The OpenAI-Speech `input` field is capped at 4000 characters. Longer
  bodies are rejected with HTTP 400.
- Voice uploads through `POST /v1/audio/voices` are streamed to disk
  with a hard 50 MB cap. Larger uploads are rejected with HTTP 413.
- Voice names sent to upload/delete endpoints are validated to forbid
  path separators, `..`, and leading dots. Path-traversal attempts are
  rejected with HTTP 400.
- The container ships running as UID 10001 with `HOME=/app`. Bind
  mounts for `voices-extra` and the HuggingFace cache must be writable
  by UID 10001 — if you see permission errors, `chown 10001:10001` the
  mount root.

## Hard limits in place (since 0.9.1)

- Requests with a method other than `GET`/`HEAD`/`OPTIONS` are rejected
  without the management token before their body is read; only
  `POST /v1/audio/speech` and `POST /v1/text/normalize` are exempt.
- Uploaded voice files must carry a supported audio extension; at most 100
  voices can be created through the API.
- `POCKET_TTS_LANGUAGES` and `POCKET_TTS_VOICE` only accept known names
  (installed checkpoints; built-in or loaded voices) — never a path or URL —
  both through the API and when read at startup.
- ffmpeg is invoked on voice files with the `file` protocol only and with
  the audio containers behind the supported extensions only.
- The web UI is served with a Content-Security-Policy and inserts values
  from the server as text.

## Mounts and symlinks

`voices-extra/` is read via a filesystem watcher. Do not mount this
directory from a location writable by less-trusted users (multi-tenant
volumes, public Samba shares) — a malicious symlink can make the
embedder read arbitrary host files.

## What Polyglot does do that you should know about

- It downloads Pocket-TTS model weights from HuggingFace on first boot.
  This requires outbound HTTPS to `huggingface.co`. Downloads are
  cached to `~/.cache/huggingface` (or wherever you mount that volume).
- It listens on the configured TCP ports for incoming traffic.
- If `voices-extra/` is mounted, it persists uploaded voice audio
  there. Treat that directory as personal data.

## Dependencies

The project pins major dependencies (PyTorch, Kyutai Pocket TTS,
Wyoming, FastAPI, Lingua) and relies on their maintainers for upstream
security patches. We do not vendor third-party code; updates roll in
via `dependabot` (where applicable) and manual pyproject bumps.
