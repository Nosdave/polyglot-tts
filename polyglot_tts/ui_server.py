"""Web UI: dashboard, voice management, and settings.

Mounted onto the existing FastAPI app (the OpenAI-Speech HTTP endpoint).
Serves a static single-page UI at `/ui` and a small `/api/ui/*` surface
the page talks to. Reuses the existing /v1/audio/* endpoints for voice
list/upload/delete and test-synthesis.

Auth: the page, the status and the read-only GETs are open, so the dashboard
and the test player work without logging in. Every call that changes state
(settings, pronunciation dictionary, restart — and voice upload/delete in
http_server) needs the management password, see auth.py. The page has a
login that keeps the password in the browser and sends it as a Bearer header,
a first-run "set password" form (POST /api/ui/setup, only while no password
exists and only for a few minutes after start) and a "change password" form.

Restart: POST /api/ui/restart exits the process; with a Docker restart
policy (unless-stopped / always) the container comes back and re-reads the
settings file. Documented in docs/WEB_UI.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import __version__
from . import auth, config_store
from .core import LANGUAGE_TO_BCP47, PolyglotCore
from .timing_server import get_timing

_LOGGER = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_START_TIME = time.time()


# Headers for the UI page. The page keeps the management password in the browser,
# so script injection must not be able to run there: only same-origin scripts,
# no inline handlers. no-referrer keeps a legacy `?token=` URL out of the
# Referer header of outgoing links.
_UI_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
}

# Dependency for every state-changing route below.
_MANAGEMENT = [Depends(auth.require_token)]


class _UIStaticFiles(StaticFiles):
    """Static assets with the same protective headers as the page itself
    (index.html is reachable under /ui/static/ too)."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.update(_UI_PAGE_HEADERS)
        return response


_MAX_AUTH_BODY_BYTES = 4096


async def _read_small_json(request: Request) -> dict:
    """Body of the password forms. /api/ui/setup is reachable without
    credentials, so the body is read with a hard size limit — and only as
    `application/json`: a page on another site cannot send that content type
    without the browser asking this server first (no cross-site form post)."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_AUTH_BODY_BYTES:
            raise HTTPException(413, "Request body too large")
    try:
        data = json.loads(body)
    except ValueError as e:
        raise HTTPException(400, "Body must be JSON") from e
    if not isinstance(data, dict):
        raise HTTPException(400, "Body must be a JSON object")
    return data


def mount_ui(app: FastAPI, core: PolyglotCore) -> None:
    """Attach the UI routes + static files to an existing FastAPI app."""

    # Static assets (css/js) under /ui/static
    if _WEB_DIR.is_dir():
        app.mount("/ui/static", _UIStaticFiles(directory=str(_WEB_DIR)), name="ui-static")

    @app.get("/ui")
    async def ui_index():
        index = _WEB_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(404, "UI not bundled in this image")
        return FileResponse(str(index), headers=_UI_PAGE_HEADERS)

    @app.get("/api/ui/session", dependencies=_MANAGEMENT)
    async def ui_session():
        """Lets the page check a password: 200 if valid, 401 otherwise
        (429 after too many failed attempts)."""
        return {"authenticated": True}

    @app.post("/api/ui/setup")
    async def ui_setup(request: Request):
        """First-run setup: set the management password from the browser.

        Reachable without credentials by design — and therefore only while
        no password exists (and POCKET_TTS_UI_TOKEN is unset) and only during
        the setup window after start. auth.set_initial_password enforces it.
        """
        body = await _read_small_json(request)
        try:
            await asyncio.to_thread(
                auth.set_initial_password, body.get("password"), auth.client_host(request))
        except auth.AuthError as e:
            raise HTTPException(e.status, e.detail) from e
        return {"authenticated": True}

    @app.post("/api/ui/password", dependencies=_MANAGEMENT)
    async def ui_change_password(request: Request):
        """Change the stored password (logged in + current password)."""
        body = await _read_small_json(request)
        try:
            await asyncio.to_thread(
                auth.change_password, body.get("current_password"),
                body.get("new_password"), auth.client_host(request))
        except auth.AuthError as e:
            raise HTTPException(e.status, e.detail) from e
        return {"changed": True}

    @app.get("/api/ui/status")
    async def ui_status():
        t = get_timing()

        def _port(name: str, default: str) -> str:
            v = os.environ.get(name, default)
            return v if v != "" else "off"

        return {
            "version": __version__,
            "uptime_s": int(time.time() - _START_TIME),
            "device": os.environ.get("POCKET_TTS_DEVICE", "auto"),
            "host": os.environ.get("POCKET_TTS_HOST", "0.0.0.0"),
            "endpoints": [
                {"name": "Wyoming (Home Assistant)",
                 "port": _port("POCKET_TTS_WYOMING_PORT", "10200")},
                {"name": "OpenAI-Speech HTTP",
                 "port": _port("POCKET_TTS_HTTP_PORT", "10201")},
                {"name": "Timing (observability)",
                 "port": _port("POCKET_TTS_TIMING_PORT", "10299")},
            ],
            "languages": [
                {"checkpoint": c,
                 "bcp47": LANGUAGE_TO_BCP47.get(c.split("_")[0], "??")}
                for c in core.models.keys()
            ],
            "default_voice": core.default_voice,
            "voice_count": len(core.voice_names()),
            # Management calls always need the password. auth_mode tells the
            # page which form to offer: "setup" (set a password now), "password"
            # / "env" / "memory" (log in), "closed" (no password, setup over).
            "auth_enabled": True,
            "auth_mode": auth.mode(),
            "setup_seconds_left": auth.setup_seconds_left(),
            "min_password_length": auth.MIN_PASSWORD_CHARS,
            "last_synth": t,
        }

    @app.get("/api/ui/config")
    async def ui_get_config():
        from .core import available_checkpoints
        return {
            "config": config_store.effective_config(),
            # Real, installed checkpoints (incl. fast non-24l variants) so the
            # UI can offer lighter models for weak hardware. Dynamic — future
            # Kyutai languages show up automatically.
            "checkpoints": available_checkpoints(),
        }

    @app.post("/api/ui/config", dependencies=_MANAGEMENT)
    async def ui_set_config(request: Request):
        try:
            body = await request.json()
        except ValueError as e:
            raise HTTPException(400, "Body must be JSON") from e
        updates = body.get("updates", {}) if isinstance(body, dict) else None
        if not isinstance(updates, dict):
            raise HTTPException(400, "updates must be an object")
        # Languages and the default voice end up as a file name / voice source
        # inside pocket-tts — only known names are accepted (no paths, no URLs).
        try:
            updates = config_store.validate_updates(updates, core.voice_names())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        # If the default voice changed live, reflect it on core immediately.
        new_default = updates.get("POCKET_TTS_VOICE")
        try:
            config_store.save_settings(updates)
        except OSError as e:
            # Almost always a mount-permission issue: the config dir isn't
            # writable by the container user (UID 10001). Surface it clearly
            # instead of an opaque 500 — a silent failure looks like the UI
            # "ignoring" saved settings.
            raise HTTPException(
                500,
                f"Could not persist settings to {config_store.config_path()}: "
                f"{e}. The config volume must be writable by the container user "
                f"(UID 10001) — use a named volume, or chown the mounted dir.",
            ) from e
        if new_default:
            core.default_voice = str(new_default)
        # Sampling temperature applies live (model.temp is read per decode step).
        if "POCKET_TTS_TEMP" in updates:
            from .core import clamp_temperature
            core.set_temperature(clamp_temperature(updates["POCKET_TTS_TEMP"]))
        # Output gain applies live (read per synthesis).
        if "POCKET_TTS_OUTPUT_GAIN" in updates:
            from .core import clamp_gain
            core.output_gain = clamp_gain(updates["POCKET_TTS_OUTPUT_GAIN"])
        # Report which changed keys need a restart.
        restart_keys = sorted(
            k for k in updates
            if k in config_store.RESTART_REQUIRED_KEYS
        )
        return {
            "saved": True,
            "restart_required_for": restart_keys,
            "config": config_store.effective_config(),
        }

    @app.get("/api/ui/replacements")
    async def ui_get_replacements():
        return {"replacements": config_store.read_replacements()}

    @app.post("/api/ui/replacements", dependencies=_MANAGEMENT)
    async def ui_set_replacements(request: Request):
        try:
            body = await request.json()
        except ValueError as e:
            raise HTTPException(400, "Body must be JSON") from e
        repl = body.get("replacements", {}) if isinstance(body, dict) else None
        if not isinstance(repl, dict):
            raise HTTPException(400, "replacements must be an object {token: spoken}")
        try:
            clean = config_store.save_replacements(repl)
        except OSError as e:
            raise HTTPException(
                500,
                f"Could not persist replacements to "
                f"{config_store.replacements_path()}: {e}. The config volume must "
                f"be writable by the container user (UID 10001).",
            ) from e
        # Apply live — no restart needed (text_norm reads a module-global map).
        from .text_norm import set_replacements
        set_replacements(clean)
        return {"saved": True, "count": len(clean), "replacements": clean}

    @app.post("/api/ui/restart", dependencies=_MANAGEMENT)
    async def ui_restart():
        _LOGGER.warning("Restart requested via web UI — re-execing the process "
                        "(re-reads settings; no Docker restart policy needed).")

        def _restart():
            time.sleep(0.4)
            try:
                # Replace this process image with a fresh `python -m polyglot_tts`.
                # The container's PID 1 is re-exec'd in place, so the container
                # stays alive and settings.json is re-read — works regardless of
                # the Docker restart policy.
                import sys
                os.execv(sys.executable, [sys.executable, "-m", "polyglot_tts"])
            except Exception:  # noqa: BLE001
                # Fall back to exit; needs a restart policy to come back.
                os._exit(0)

        threading.Thread(target=_restart, daemon=True).start()
        return JSONResponse(
            {"status": "restarting",
             "note": "The server is re-execing now; it will be back in ~30–90 s "
                     "(model reload + warmup). No Docker restart policy required."}
        )

    _LOGGER.info("Web UI mounted at /ui (management calls need the password)")
