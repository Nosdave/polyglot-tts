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


# ── Per-language references: voice.<bcp47>.ext ────────────────────────────
#
# A single voice can be backed by several reference files, one per language,
# each coupled to the matching language model:
#
#   EL_Jarvis.de.mp3   -> encoded ONLY against the German checkpoint(s)
#   EL_Jarvis.en.mp3   -> encoded ONLY against the English checkpoint(s)
#   EL_Jarvis.fr.mp3   -> encoded ONLY against the French checkpoint(s)
#   EL_Jarvis.mp3      -> fallback for any checkpoint without a tagged file
#
# This removes cross-language accent: each model speaks the voice from a
# native-language reference. A voice with only one untagged file behaves
# exactly as before (one embedding shared across all checkpoints).

_lang_tags_cache: set[str] | None = None


def _known_lang_tags() -> set[str]:
    """bcp47 codes we accept as filename tags (de/en/fr/it/es/pt …).

    Derived from pocket-tts' language map so it tracks whatever the engine
    knows. Lazy + cached to avoid a circular import at module load.
    """
    global _lang_tags_cache
    if _lang_tags_cache is None:
        try:
            from .core import LANGUAGE_TO_BCP47
            _lang_tags_cache = {b.lower() for b in LANGUAGE_TO_BCP47.values()}
        except Exception:  # noqa: BLE001
            _lang_tags_cache = {"de", "en", "fr", "it", "es", "pt"}
    return _lang_tags_cache


def split_voice_lang(stem: str) -> tuple[str, str | None]:
    """Split a file stem into (voice_name, bcp47-or-None).

    'EL_Jarvis.de' -> ('EL_Jarvis', 'de');  'eve' -> ('eve', None).
    The trailing dotted component is a language tag ONLY if it's a known
    bcp47 code — so 'EL_Louisa_poly' stays intact ('poly' is not a language).
    """
    if "." in stem:
        base, maybe = stem.rsplit(".", 1)
        if base and maybe.lower() in _known_lang_tags():
            return base, maybe.lower()
    return stem, None


def parse_voice_file(path: Path) -> tuple[str, str | None]:
    """(voice_name, bcp47-or-None) for an audio file path."""
    return split_voice_lang(path.stem)


def group_voice_files(voices_dir: Path) -> dict[str, dict[str | None, Path]]:
    """Group audio files in a dir by voice name → {bcp47-or-None: path}."""
    groups: dict[str, dict[str | None, Path]] = {}
    if not voices_dir.exists():
        return groups
    for f in sorted(voices_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXT:
            continue
        name, lang = parse_voice_file(f)
        groups.setdefault(name, {})[lang] = f
    return groups


def build_voice_states(files: dict[str | None, Path], core) -> dict:
    """Build {checkpoint: state} for one voice from its (tagged) reference files.

    For each loaded checkpoint, use the file tagged with that checkpoint's
    bcp47 if present, else the untagged fallback. Each chosen file is decoded +
    loudness-normalized once and encoded only against the checkpoints that use
    it. Returns {} if nothing could be built.
    """
    # Which source file serves which checkpoints?
    file_to_ckpts: dict[Path, list[str]] = {}
    for ckpt in core.models:
        bcp = core.checkpoint_bcp47.get(ckpt)
        src = files.get(bcp)
        if src is None:
            src = files.get(None)  # untagged fallback
        if src is None:
            continue
        file_to_ckpts.setdefault(src, []).append(ckpt)

    per_lang: dict = {}
    for src, ckpts in file_to_ckpts.items():
        decodable, dec_temp = ensure_decodable(src)
        normalized, norm_temp = normalize_loudness(decodable)
        try:
            per_lang.update(core.encode_voice_for(normalized, ckpts))
        finally:
            if norm_temp:
                normalized.unlink(missing_ok=True)
            if dec_temp:
                decodable.unlink(missing_ok=True)
    return per_lang


def reload_voice(core, voices_dir: Path, voice_name: str) -> int:
    """Re-build one voice from ALL its current files, or remove it if none
    remain. Returns the number of languages registered (0 = removed/failed).
    Used by the file-watcher on any change to a voice's reference files.
    """
    files = group_voice_files(voices_dir).get(voice_name, {})
    if not files:
        core.remove_voice(voice_name)
        return 0
    per_lang = build_voice_states(files, core)
    if per_lang:
        core.add_voice(voice_name, per_lang)
    return len(per_lang)


def load_voices_from_dir(voices_dir: Path, core) -> int:
    """Walk a directory and register each found voice (grouping per-language
    reference files into a single multi-language voice).

    Returns count of successfully loaded voices.
    """
    if not voices_dir.exists():
        _LOGGER.info("voices dir not present: %s (skipping)", voices_dir)
        return 0

    count = 0
    for name, files in group_voice_files(voices_dir).items():
        tags = sorted(lang for lang in files if lang)
        desc = ("langs: " + ", ".join(tags)) if tags else "single file"
        _LOGGER.info("Loading voice '%s' (%s) ...", name, desc)
        try:
            per_lang = build_voice_states(files, core)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Voice load failed for %s: %s", name, e)
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
