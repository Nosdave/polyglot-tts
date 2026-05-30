"""Voice loader — built-in voices + voices-extra mount.

At startup we walk two directories:

  voices/         — built-in voices baked into the image (read-only).
                    NOT used by Polyglot TTS — Kyutai presets are loaded
                    from the pocket-tts package automatically. This dir
                    is reserved for future shipped customs.

  voices-extra/   — user-mountable host volume. Drop a WAV here and the
                    file-watcher will encode it on the fly.

Functions here are used by the dispatcher at startup AND by the
file-watcher at runtime, so they're stateless.
"""

from __future__ import annotations

import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

AUDIO_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}


def load_voices_from_dir(voices_dir: Path, core) -> int:
    """Walk a directory and add each found voice via core.encode_voice + core.add_voice.

    Returns count of successfully loaded voices.
    """
    if not voices_dir.exists():
        _LOGGER.info("voices dir not present: %s (skipping)", voices_dir)
        return 0

    count = 0
    for f in sorted(voices_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXT:
            continue
        name = f.stem
        _LOGGER.info("Loading voice from %s ...", f.name)
        per_lang = core.encode_voice(f)
        if per_lang:
            core.add_voice(name, per_lang)
            count += 1
    return count


def load_initial_voices(core, voices_dirs: list[Path]) -> None:
    """Walk every configured voices-dir in order; later dirs override earlier."""
    total = 0
    for d in voices_dirs:
        total += load_voices_from_dir(d, core)
    _LOGGER.info("Initial voice load: %d voice(s) total", total)
