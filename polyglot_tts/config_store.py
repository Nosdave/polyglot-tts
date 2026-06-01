"""Persisted settings overlay for the web UI.

Most configuration is environment-driven and read once at startup. To let
the web UI edit settings, we keep a small JSON file that is *overlaid onto
the process environment at boot* — before anything reads `os.environ`. This
means every existing `os.environ.get(...)` call automatically picks up
UI-saved values without refactoring the read sites.

Precedence: UI settings file > compose/shell env > image default.
(Once you save a setting in the UI it wins over compose env; delete the
settings file to fall back to compose.)

The file lives at POCKET_TTS_CONFIG_FILE (default /app/config/settings.json),
which should be on a writable mount/volume to persist across restarts.

Also handles the HuggingFace token written from the UI: it's stored to the
settings file AND applied to the live process so the *next* voice encode
picks it up without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Keys the UI is allowed to write. Anything else is ignored (defence against
# a tampered settings file injecting arbitrary env).
EDITABLE_KEYS: set[str] = {
    "POCKET_TTS_LANGUAGES",       # restart required
    "POCKET_TTS_VOICE",           # live (default voice)
    "POCKET_TTS_DEVICE",          # restart required
    "POCKET_TTS_AUTO_LID",        # live-ish (read per request)
    "POCKET_TTS_TEXT_NORM",       # live-ish
    "POCKET_TTS_LAZY_LOAD",       # restart required (and not yet implemented)
    "POCKET_TTS_MIN_SYNTH_CHARS", # live-ish
    "POCKET_TTS_WARMUP",          # restart required
    "HF_TOKEN",                   # live (next encode)
}

# Which keys only take effect after a container restart.
RESTART_REQUIRED_KEYS: set[str] = {
    "POCKET_TTS_LANGUAGES",
    "POCKET_TTS_DEVICE",
    "POCKET_TTS_LAZY_LOAD",
    "POCKET_TTS_WARMUP",
}

# Per-field UI metadata: a short help text, an input type, and (optionally)
# select options. Drives the settings form rendering.
FIELD_META: dict[str, dict] = {
    "POCKET_TTS_LANGUAGES": {
        "type": "text",
        "help": "Comma-separated language checkpoints to load. Each adds ~1.3 GB RAM.",
        "placeholder": "english_2026-04,german_24l,french_24l",
        "options": [
            "english_2026-04", "german_24l", "french_24l",
            "italian_24l", "spanish_24l", "portuguese_24l",
        ],
    },
    "POCKET_TTS_VOICE": {
        "type": "voice-select",
        "help": "Default voice when a request doesn't specify one.",
        "placeholder": "eve",
    },
    "POCKET_TTS_DEVICE": {
        "type": "select",
        "help": "auto picks CUDA if a GPU is visible, else CPU.",
        "options": ["auto", "cpu", "cuda"],
    },
    "POCKET_TTS_AUTO_LID": {
        "type": "bool",
        "help": "Detect the language of each request automatically (Lingua).",
    },
    "POCKET_TTS_TEXT_NORM": {
        "type": "bool",
        "help": "Expand numbers, units, dates, ordinals, and strip Markdown before synthesis.",
    },
    "POCKET_TTS_LAZY_LOAD": {
        "type": "bool",
        "help": "(Not yet implemented) load a missing language on first use.",
    },
    "POCKET_TTS_MIN_SYNTH_CHARS": {
        "type": "number",
        "help": "Streaming first-flush threshold. Lower = faster first audio, less natural prosody.",
        "placeholder": "30",
    },
    "POCKET_TTS_WARMUP": {
        "type": "bool",
        "help": "Run a short synth per language at startup to warm CUDA kernels.",
    },
    "HF_TOKEN": {
        "type": "secret",
        "help": "Only needed for voice cloning (gated Kyutai model). Stored, never shown.",
    },
}

_LOCK = threading.Lock()


def config_path() -> Path:
    return Path(os.environ.get("POCKET_TTS_CONFIG_FILE", "/app/config/settings.json"))


def _read_file() -> dict:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k in EDITABLE_KEYS}
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Could not read settings file %s: %s", p, e)
    return {}


def apply_overlay() -> None:
    """Overlay the settings file onto os.environ. Call once, early at startup."""
    data = _read_file()
    for k, v in data.items():
        os.environ[k] = str(v)
    if data:
        # Don't log the HF token value.
        loud = {k: ("***" if k == "HF_TOKEN" else v) for k, v in data.items()}
        _LOGGER.info("Applied %d setting(s) from %s: %s",
                     len(data), config_path(), loud)


def save_settings(updates: dict) -> dict:
    """Merge UI updates into the settings file. Returns the new effective dict.

    Only EDITABLE_KEYS are persisted. HF_TOKEN is also applied live to the
    current process so the next voice encode uses it without a restart.
    """
    with _LOCK:
        current = _read_file()
        for k, v in updates.items():
            if k not in EDITABLE_KEYS:
                continue
            if v is None or v == "":
                current.pop(k, None)
                # Also clear from live env where it makes sense
                if k != "HF_TOKEN":
                    os.environ.pop(k, None)
            else:
                current[k] = str(v)

        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2)
        tmp.replace(p)

    # Apply live keys immediately (those that are read per-request or per-encode)
    if "HF_TOKEN" in updates and updates["HF_TOKEN"]:
        os.environ["HF_TOKEN"] = str(updates["HF_TOKEN"])
    for live_key in ("POCKET_TTS_VOICE", "POCKET_TTS_AUTO_LID",
                     "POCKET_TTS_TEXT_NORM", "POCKET_TTS_MIN_SYNTH_CHARS"):
        if live_key in updates and updates[live_key] not in (None, ""):
            os.environ[live_key] = str(updates[live_key])

    return current


def effective_config() -> dict:
    """Current effective values for every editable key, plus restart-required flags.

    The HF token is reported as a boolean 'set' — never the value.
    """
    out = {}
    for k in sorted(EDITABLE_KEYS):
        meta = FIELD_META.get(k, {})
        common = {
            "restart_required": k in RESTART_REQUIRED_KEYS,
            "type": meta.get("type", "text"),
            "help": meta.get("help", ""),
            "placeholder": meta.get("placeholder", ""),
            "options": meta.get("options", []),
        }
        if k == "HF_TOKEN":
            out[k] = {"value": bool(os.environ.get("HF_TOKEN")),
                      "is_secret": True, **common}
        else:
            out[k] = {"value": os.environ.get(k, ""),
                      "is_secret": False, **common}
    return out
