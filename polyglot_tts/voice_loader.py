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
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Extensions the watcher will pick up.
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus"}

# Formats libsndfile (soundfile, used by pocket-tts) reads natively.
# Everything else (m4a/aac, iPhone voice memos, ...) is transcoded to wav
# via ffmpeg before encoding.
_NATIVE_EXT = {".wav", ".flac", ".ogg", ".mp3"}


def ensure_decodable(path: Path) -> tuple[Path, bool]:
    """Return a path that pocket-tts/soundfile can open.

    If `path` is a natively-readable format (wav/flac/ogg/mp3) it's
    returned unchanged. Otherwise (m4a/aac/opus/...) it's transcoded to a
    temporary 24 kHz mono WAV via ffmpeg.

    Returns (decodable_path, is_temp). Caller must delete the path when
    is_temp is True.

    Raises RuntimeError if transcoding is needed but ffmpeg is missing or
    fails.
    """
    if path.suffix.lower() in _NATIVE_EXT:
        return path, False

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            f"{path.suffix} needs transcoding but ffmpeg is not installed"
        )

    fd, tmp_name = tempfile.mkstemp(prefix="polyglot_voice_", suffix=".wav")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(path), "-ar", "24000", "-ac", "1", str(tmp)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        stderr = (e.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"ffmpeg failed for {path.name}: {stderr}") from e
    return tmp, True


def _normalize_enabled() -> bool:
    return os.environ.get("POCKET_TTS_VOICE_NORMALIZE", "true").lower() in (
        "1", "true", "yes",
    )


def normalize_loudness(path: Path) -> tuple[Path, bool]:
    """Loudness-normalize a voice sample to a consistent level via ffmpeg.

    A quiet recording (a soft mic, a distant phone memo) makes a weak voice
    prompt; an over-hot one clips. EBU R128 `loudnorm` pulls both toward a
    common target so cloned voices are consistent regardless of how the sample
    was captured. Output is 24 kHz mono WAV (what pocket-tts wants anyway).

    Returns (path, is_temp). Best-effort: if disabled, ffmpeg is missing, or the
    filter fails, the original `path` is returned unchanged (is_temp False).
    """
    if not _normalize_enabled():
        return path, False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return path, False

    fd, tmp_name = tempfile.mkstemp(prefix="polyglot_norm_", suffix=".wav")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(path),
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ar", "24000", "-ac", "1", str(tmp)],
            check=True, capture_output=True,
        )
        return tmp, True
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        stderr = (e.stderr or b"").decode("utf-8", "replace")[-300:]
        _LOGGER.warning("loudnorm failed for %s (%s) — using original level",
                        path.name, stderr)
        return path, False


def encode_voice_file(path: Path, core) -> dict:
    """Decode (transcode if needed), loudness-normalize, then encode against
    every loaded model. Returns the per-language state dict (empty on failure).
    """
    decodable, dec_temp = ensure_decodable(path)
    normalized, norm_temp = normalize_loudness(decodable)
    try:
        return core.encode_voice(normalized)
    finally:
        if norm_temp:
            normalized.unlink(missing_ok=True)
        if dec_temp:
            decodable.unlink(missing_ok=True)


def load_voices_from_dir(voices_dir: Path, core) -> int:
    """Walk a directory and register each found voice.

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
        try:
            per_lang = encode_voice_file(f, core)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Voice load failed for %s: %s", f.name, e)
            continue
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
