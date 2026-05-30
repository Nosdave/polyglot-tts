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
Tailscale, equivalent). The HTTP endpoint does **no authentication** by
default — anyone who can reach the port can synthesize audio and
upload/delete voices.

If you expose Polyglot TTS to a less-trusted network, put it behind a
reverse proxy with auth (e.g. Caddy + basic-auth, or your existing
ingress).

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
