"""PolyglotCore — shared TTS-engine state used by every endpoint.

Owns:
  - Loaded TTS models (one per language checkpoint)
  - Voice-state registry (per voice, per language)
  - Lingua language detector
  - asyncio.Lock for thread-safe voice-state mutation (file-watcher writes,
    handler reads)

All endpoints (Wyoming, OpenAI-HTTP, file-watcher) take a reference to a
single PolyglotCore instance, built once by the dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# BCP47 maps live in core because both endpoints reach for them.
LANGUAGE_TO_BCP47: dict[str, str] = {
    "german":     "de",
    "french":     "fr",
    "italian":    "it",
    "spanish":    "es",
    "portuguese": "pt",
    "english":    "en",
}

BCP47_TO_CHECKPOINT: dict[str, str] = {
    "de": "german_24l",
    "fr": "french_24l",
    "en": "english_2026-04",
    "it": "italian_24l",
    "es": "spanish_24l",
    "pt": "portuguese_24l",
}

ALL_PRESET_VOICES: list[str] = [
    "alba", "anna", "vera", "fantine", "charles", "paul", "eponine", "azelma",
    "george", "mary", "jane", "michael", "eve",
    "bill_boerst", "peter_yearsley", "stuart_bell", "caro_davy",
    "cosette", "marius", "javert", "jean",
    "estelle", "giovanni", "lola", "juergen", "rafael",
]

SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2
CHANNELS = 1


class PolyglotCore:
    """Shared TTS-engine state. Built once, used by every endpoint."""

    def __init__(
        self,
        models: dict,                       # {checkpoint_name: TTSModel}
        voice_states: dict,                 # {voice_name: {checkpoint: state}}
        default_voice: str,
        advertised_bcp47: list[str],
        voices_extra_dir: Path | None = None,
    ) -> None:
        self.models = models
        self.voice_states = voice_states
        self.default_voice = default_voice
        self.advertised_bcp47 = advertised_bcp47
        self.voices_extra_dir = voices_extra_dir
        self.default_checkpoint = next(iter(models.keys()))
        self.default_bcp47 = LANGUAGE_TO_BCP47.get(
            self.default_checkpoint.split("_")[0], "en"
        )

        # Mutex guarding voice_states. File-watcher writes (add/remove),
        # endpoints read. Threading.Lock (sync) because watchdog runs in a
        # thread, not the asyncio loop.
        self._voice_lock = threading.RLock()

    # ── Voice-state access ────────────────────────────────────────────────

    def get_voice_state(self, voice_name: str, checkpoint: str):
        """Return state-vector for (voice, checkpoint) or None."""
        with self._voice_lock:
            per_lang = self.voice_states.get(voice_name)
            if per_lang and checkpoint in per_lang:
                return per_lang[checkpoint]
            return None

    def add_voice(self, voice_name: str, per_lang_state: dict) -> None:
        """Register a new voice (or replace existing). Called by file-watcher."""
        with self._voice_lock:
            self.voice_states[voice_name] = per_lang_state
        _LOGGER.info("Voice registered: %s (%d language(s))",
                     voice_name, len(per_lang_state))

    def remove_voice(self, voice_name: str) -> bool:
        """Drop a voice from the registry. Returns True if it existed."""
        with self._voice_lock:
            existed = voice_name in self.voice_states
            self.voice_states.pop(voice_name, None)
        if existed:
            _LOGGER.info("Voice removed: %s", voice_name)
        return existed

    def voice_names(self) -> list[str]:
        """All currently available voice names (presets + custom)."""
        with self._voice_lock:
            custom = list(self.voice_states.keys())
        return sorted(set(ALL_PRESET_VOICES) | set(custom))

    def voice_info(self) -> list[dict]:
        """Structured voice list for /v1/audio/voices endpoint."""
        result = []
        with self._voice_lock:
            custom = set(self.voice_states.keys())
        for v in sorted(set(ALL_PRESET_VOICES) | custom):
            kind = "custom" if v in custom else "preset"
            result.append({"name": v, "kind": kind})
        return result

    # ── Encoding helper for new voices (called by file-watcher) ────────────

    def encode_voice(self, source) -> dict:
        """Encode a voice (Path or preset-name) against every loaded model.

        Returns dict mapping checkpoint-name -> state-vector. Empty dict on
        total failure.
        """
        per_lang_state: dict = {}
        for ckpt, model in self.models.items():
            try:
                arg = str(source) if isinstance(source, Path) else source
                per_lang_state[ckpt] = model.get_state_for_audio_prompt(arg)
            except Exception as e:
                _LOGGER.warning("Voice-encoding failed for %s vs %s: %s",
                                source, ckpt, e)
        return per_lang_state


def auto_lid_enabled() -> bool:
    return os.environ.get("POCKET_TTS_AUTO_LID", "true").lower() in ("1", "true", "yes")
