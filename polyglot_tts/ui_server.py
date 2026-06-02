"""Web UI: dashboard, voice management, and settings.

Mounted onto the existing FastAPI app (the OpenAI-Speech HTTP endpoint).
Serves a static single-page UI at `/ui` and a small `/api/ui/*` surface
the page talks to. Reuses the existing /v1/audio/* endpoints for voice
list/upload/delete and test-synthesis.

Auth: if POCKET_TTS_UI_TOKEN is set, the UI page and all /api/ui/* calls
require it (sent as `X-UI-Token` header or `?token=` query). When unset,
the UI is open (LAN-only, matching the rest of the server's default).

Restart: POST /api/ui/restart exits the process; with a Docker restart
policy (unless-stopped / always) the container comes back and re-reads the
settings file. Documented in docs/WEB_UI.md.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import __version__
from . import config_store
from .core import LANGUAGE_TO_BCP47, PolyglotCore
from .timing_server import get_timing

_LOGGER = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_START_TIME = time.time()


def _ui_token() -> str | None:
    tok = os.environ.get("POCKET_TTS_UI_TOKEN", "").strip()
    return tok or None


def _check_auth(request: Request) -> None:
    expected = _ui_token()
    if not expected:
        return  # auth disabled
    provided = request.headers.get("X-UI-Token") or request.query_params.get("token")
    if provided != expected:
        raise HTTPException(401, "Invalid or missing UI token")


def mount_ui(app: FastAPI, core: PolyglotCore) -> None:
    """Attach the UI routes + static files to an existing FastAPI app."""

    # Static assets (css/js) under /ui/static
    if _WEB_DIR.is_dir():
        app.mount("/ui/static", StaticFiles(directory=str(_WEB_DIR)), name="ui-static")

    @app.get("/ui")
    async def ui_index(request: Request):
        _check_auth(request)
        index = _WEB_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(404, "UI not bundled in this image")
        return FileResponse(str(index))

    @app.get("/api/ui/status")
    async def ui_status(request: Request):
        _check_auth(request)
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
            "auth_enabled": _ui_token() is not None,
            "last_synth": t,
        }

    @app.get("/api/ui/config")
    async def ui_get_config(request: Request):
        _check_auth(request)
        from .core import available_checkpoints
        return {
            "config": config_store.effective_config(),
            # Real, installed checkpoints (incl. fast non-24l variants) so the
            # UI can offer lighter models for weak hardware. Dynamic — future
            # Kyutai languages show up automatically.
            "checkpoints": available_checkpoints(),
        }

    @app.post("/api/ui/config")
    async def ui_set_config(request: Request):
        _check_auth(request)
        body = await request.json()
        updates = body.get("updates", {})
        if not isinstance(updates, dict):
            raise HTTPException(400, "updates must be an object")
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

    @app.post("/api/ui/restart")
    async def ui_restart(request: Request):
        _check_auth(request)
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

    _LOGGER.info("Web UI mounted at /ui (auth=%s)",
                 "on" if _ui_token() else "off")
