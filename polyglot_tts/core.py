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

# Sampling temperature bounds. pocket-tts stores `temp` as a plain mutable
# attribute on the model and reads it live at every decode step, so it can be
# changed at runtime (globally or per request) without reloading the model.
TEMP_MIN, TEMP_MAX, TEMP_DEFAULT = 0.1, 1.5, 0.7


def clamp_temperature(raw, default: float = TEMP_DEFAULT) -> float:
    """Parse + clamp a temperature into [TEMP_MIN, TEMP_MAX].

    Accepts a float or a string (incl. comma decimals). Empty, unparseable, or
    out-of-range input returns `default`. Used by the env loader and the live
    config path; the per-request HTTP path clamps to the bounds directly.
    """
    if raw is None or raw == "":
        return default
    try:
        val = float(str(raw).replace(",", "."))
    except (ValueError, TypeError):
        return default
    if not TEMP_MIN <= val <= TEMP_MAX:
        return default
    return val


# Output gain (linear multiplier applied to the generated audio before encoding).
# 1.0 = unchanged, <1 quieter, >1 louder (clipped to [-1, 1] after scaling).
GAIN_MIN, GAIN_MAX, GAIN_DEFAULT = 0.0, 4.0, 1.0


def clamp_gain(raw, default: float = GAIN_DEFAULT) -> float:
    """Parse + clamp an output gain into [GAIN_MIN, GAIN_MAX]. Bad/empty/out-of
    range input returns `default`. Accepts float or string (comma decimals)."""
    if raw is None or raw == "":
        return default
    try:
        val = float(str(raw).replace(",", "."))
    except (ValueError, TypeError):
        return default
    if not GAIN_MIN <= val <= GAIN_MAX:
        return default
    return val


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
        if not models:
            raise ValueError("PolyglotCore requires at least one loaded model")
        self.models = models
        self.voice_states = voice_states
        self.default_voice = default_voice
        self.advertised_bcp47 = advertised_bcp47
        self.voices_extra_dir = voices_extra_dir
        self.default_checkpoint = next(iter(models.keys()))
        self.default_bcp47 = LANGUAGE_TO_BCP47.get(
            self.default_checkpoint.split("_")[0], "en"
        )

        # Global output gain, read live by both synthesis paths (HTTP + Wyoming).
        # A plain float attribute → live-adjustable from the UI without a reload.
        self.output_gain: float = clamp_gain(os.environ.get("POCKET_TTS_OUTPUT_GAIN"))

        # Map each loaded language (bcp47) to the checkpoint actually loaded
        # for it — built from `models`, NOT a hardcoded table. This makes the
        # lighter variants work: if the user loaded "german" (fast) instead of
        # "german_24l", "de" resolves to "german". First-loaded wins if two
        # checkpoints share a language (the UI prevents that, but be safe).
        self.bcp47_to_checkpoint: dict[str, str] = {}
        for ckpt in models:
            bcp = LANGUAGE_TO_BCP47.get(ckpt.split("_")[0], ckpt[:2])
            self.bcp47_to_checkpoint.setdefault(bcp, ckpt)

        # Mutex guarding voice_states. File-watcher writes (add/remove),
        # endpoints read + on-demand-write. threading.RLock works from both
        # the watcher thread AND from asyncio paths via asyncio.to_thread.
        self._voice_lock = threading.RLock()

        # Per-model serialization lock. Pocket-TTS mutates per-model state
        # (pad_with_spaces_for_short_inputs) during generation and is not
        # thread-safe. Both endpoints (Wyoming + HTTP) acquire this BEFORE
        # calling model.generate_audio_stream. threading.Lock because the
        # HTTP path runs synthesis in asyncio.to_thread; the Wyoming path
        # has its own asyncio.Lock layer (legacy) but acquires this one
        # too when it offloads to an executor.
        self._model_locks: dict[int, threading.Lock] = {}
        self._model_locks_guard = threading.Lock()

    def get_model_lock(self, model) -> threading.Lock:
        """Return the shared sync-lock for this model. Safe from any thread."""
        key = id(model)
        with self._model_locks_guard:
            lock = self._model_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._model_locks[key] = lock
        return lock

    def set_temperature(self, temp: float) -> None:
        """Set the sampling temperature on every loaded model, live.

        `model.temp` is read at each decode step, so this takes effect on the
        next synthesis — no reload. Set under each model's lock so it can't race
        with an in-flight generation. This is the global value; the per-request
        HTTP path overrides + restores it around a single synthesis.
        """
        for model in self.models.values():
            with self.get_model_lock(model):
                model.temp = temp
        _LOGGER.info("Sampling temperature set to %.2f on %d model(s)",
                     temp, len(self.models))

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

    def set_voice_state(self, voice_name: str, checkpoint: str, state) -> None:
        """Atomically attach a single per-language state to a voice.

        Used by the HTTP and Wyoming on-demand-encode paths so they don't
        mutate `voice_states` directly under an unguarded lock.
        """
        with self._voice_lock:
            self.voice_states.setdefault(voice_name, {})[checkpoint] = state

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


def available_checkpoints() -> list[dict]:
    """Enumerate the language checkpoints the installed pocket-tts ships.

    Returns a list of {checkpoint, bcp47, quality} dicts, e.g.
        {"checkpoint": "german_24l", "bcp47": "de", "quality": "high"}
        {"checkpoint": "german",     "bcp47": "de", "quality": "fast"}

    Reads the bundled config/*.yaml names so future Kyutai languages appear
    automatically. Falls back to a static list if enumeration fails.
    """
    import glob

    names: list[str] = []
    try:
        import pocket_tts  # noqa: PLC0415

        base = os.path.dirname(pocket_tts.__file__)
        for pat in (os.path.join(base, "config", "*.yaml"),
                    os.path.join(base, "**", "*.yaml")):
            for f in glob.glob(pat, recursive=True):
                names.append(os.path.basename(f)[:-5])  # strip .yaml
    except Exception:  # noqa: BLE001
        pass

    names = sorted(set(names))
    if not names:
        # Static fallback (pocket-tts 2.1.0).
        names = [
            "english_2026-04", "english_2026-01", "english",
            "french_24l", "german", "german_24l",
            "italian", "italian_24l", "spanish", "spanish_24l",
            "portuguese", "portuguese_24l",
        ]

    out = []
    for n in names:
        lang_key = n.split("_")[0]
        bcp47 = LANGUAGE_TO_BCP47.get(lang_key, lang_key[:2])
        # Heuristic quality label: *_24l = high (24-layer), else fast/default.
        if n.endswith("_24l"):
            quality = "high (24-layer)"
        elif n.startswith("english"):
            quality = "latest" if "2026-04" in n else "older"
        else:
            quality = "fast (smaller)"
        out.append({"checkpoint": n, "bcp47": bcp47, "quality": quality})
    return out
