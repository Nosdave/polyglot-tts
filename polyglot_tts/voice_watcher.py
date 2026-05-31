"""File-watcher for voices-extra/ → auto-embed pipeline.

User drops a WAV into the mounted voices-extra/ directory; this watcher
notices the new file, waits until it's fully written (mtime stable for
WAIT_STABLE_SECONDS), encodes it against every loaded language model,
and registers the resulting voice on the shared PolyglotCore.

On failure, writes a sidecar `<name>.wav.error` describing the reason.

On removal of a source file, drops the voice from the registry.

Runs in its own thread (watchdog's Observer is thread-based, not asyncio).
The encoding step is synchronous and CPU/GPU-heavy, so it runs in this
background thread — never blocks any endpoint's event loop.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .voice_loader import AUDIO_EXT, encode_voice_file

_LOGGER = logging.getLogger(__name__)

WAIT_STABLE_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.5
# Reject absurdly large drops before doing any work. A voice sample is
# seconds of speech — anything over this is almost certainly a mistake
# (someone dropped a movie / disk image into voices-extra by accident).
MAX_VOICE_FILE_BYTES = 100 * 1024 * 1024  # 100 MB


def _is_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXT


def _wait_for_stable(path: Path, timeout: float = 30.0) -> bool:
    """Block until path's mtime has been stable for WAIT_STABLE_SECONDS."""
    deadline = time.time() + timeout
    last_size = -1
    last_mtime = -1.0
    stable_since: float | None = None
    while time.time() < deadline:
        try:
            st = path.stat()
        except FileNotFoundError:
            return False
        if st.st_size == last_size and st.st_mtime == last_mtime:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= WAIT_STABLE_SECONDS:
                return True
        else:
            last_size = st.st_size
            last_mtime = st.st_mtime
            stable_since = None
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def _write_error_sidecar(audio_path: Path, message: str) -> None:
    try:
        err_path = audio_path.with_suffix(audio_path.suffix + ".error")
        err_path.write_text(message + "\n", encoding="utf-8")
    except Exception as e:
        _LOGGER.warning("Could not write error sidecar for %s: %s", audio_path, e)


def _clear_error_sidecar(audio_path: Path) -> None:
    try:
        err_path = audio_path.with_suffix(audio_path.suffix + ".error")
        if err_path.exists():
            err_path.unlink()
    except Exception:
        pass


def _embed_one(audio_path: Path, core) -> bool:
    """Embed one voice file. Returns True if a voice was registered."""
    name = audio_path.stem

    # Size sanity check — reject obvious mistakes (a non-voice file dumped
    # into voices-extra) before spending CPU/GPU on it.
    try:
        size = audio_path.stat().st_size
    except OSError:
        return False
    if size > MAX_VOICE_FILE_BYTES:
        _LOGGER.warning("Voice file too large (%d bytes, max %d): %s",
                        size, MAX_VOICE_FILE_BYTES, audio_path.name)
        _write_error_sidecar(
            audio_path,
            f"File is {size // (1024*1024)} MB — too large for a voice sample "
            f"(max {MAX_VOICE_FILE_BYTES // (1024*1024)} MB). "
            "A voice sample is 10-30 seconds of speech."
        )
        return False

    if not _wait_for_stable(audio_path):
        _LOGGER.warning("Voice file did not stabilize: %s", audio_path)
        _write_error_sidecar(audio_path, "File did not stabilize within 30s")
        return False

    _LOGGER.info("Embedding voice '%s' from %s ...", name, audio_path.name)
    t0 = time.time()
    try:
        # encode_voice_file transcodes non-native formats (m4a/aac/...) to
        # a temp wav via ffmpeg before encoding.
        per_lang = encode_voice_file(audio_path, core)
    except Exception as e:
        _LOGGER.warning("Voice embedding failed for %s: %s", audio_path.name, e)
        _write_error_sidecar(audio_path, f"Could not process file: {e}")
        return False

    if not per_lang:
        _write_error_sidecar(
            audio_path,
            "Embedding failed against every loaded language model. "
            "Possible causes: not actually audio, too short, too noisy, "
            "or multiple speakers."
        )
        return False

    core.add_voice(name, per_lang)
    _clear_error_sidecar(audio_path)
    dt_ms = int((time.time() - t0) * 1000)
    _LOGGER.info("Voice '%s' ready in %d language(s) (%d ms)",
                 name, len(per_lang), dt_ms)
    return True


class _VoiceFolderHandler(FileSystemEventHandler):
    def __init__(self, core, voices_dir: Path) -> None:
        super().__init__()
        self.core = core
        self.voices_dir = voices_dir
        self._processing: set[str] = set()
        self._processing_lock = threading.Lock()
        # voice_name -> source file path that produced it. Lets on_deleted
        # avoid removing a voice when an UNRELATED file sharing the same
        # stem is deleted (e.g. delete davidneu.m4a must not drop the voice
        # that was registered from davidneu.wav).
        self._voice_source: dict[str, str] = {}
        self._source_lock = threading.Lock()
        # path -> mtime of the last attempt that FAILED. A file that already
        # failed and hasn't changed since is skipped — no retry-storm when a
        # genuinely broken / non-voice file sits in the folder. Cleared when
        # the file's mtime changes (user replaced it) or on success.
        self._failed: dict[str, float] = {}

    def _already_failed_unchanged(self, path: Path) -> bool:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        with self._source_lock:
            return self._failed.get(str(path)) == mtime

    def _record_failure(self, path: Path) -> None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        with self._source_lock:
            self._failed[str(path)] = mtime

    def _clear_failure(self, path: Path) -> None:
        with self._source_lock:
            self._failed.pop(str(path), None)

    def _maybe_process_create(self, path: Path) -> None:
        if not _is_audio_file(path):
            return
        # Skip a file that already failed and hasn't changed since.
        if self._already_failed_unchanged(path):
            return
        # De-dup: same path event can fire twice (create + modify)
        with self._processing_lock:
            if str(path) in self._processing:
                return
            self._processing.add(str(path))
        try:
            if _embed_one(path, self.core):
                with self._source_lock:
                    self._voice_source[path.stem] = str(path)
                self._clear_failure(path)
            else:
                self._record_failure(path)
        finally:
            with self._processing_lock:
                self._processing.discard(str(path))

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        # Run embedding in a separate thread so the observer thread stays
        # responsive to further events.
        threading.Thread(
            target=self._maybe_process_create,
            args=(path,),
            daemon=True,
            name=f"embed-{path.name}",
        ).start()

    def on_modified(self, event: FileSystemEvent) -> None:
        # A modify on an already-embedded file means the user replaced it.
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not _is_audio_file(path):
            return
        # Only re-embed if not currently being processed.
        with self._processing_lock:
            if str(path) in self._processing:
                return
        threading.Thread(
            target=self._maybe_process_create,
            args=(path,),
            daemon=True,
            name=f"reembed-{path.name}",
        ).start()

    def _remove_if_source(self, path: Path) -> None:
        """Drop the voice only if THIS path was the one that registered it."""
        name = path.stem
        # Whatever happens, stop tracking this path as a known failure.
        self._clear_failure(path)
        with self._source_lock:
            source = self._voice_source.get(name)
            if source != str(path):
                # A different file (or none) produced the current voice —
                # deleting this path must not remove it.
                return
            self._voice_source.pop(name, None)
        if self.core.remove_voice(name):
            _clear_error_sidecar(path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in AUDIO_EXT:
            return
        self._remove_if_source(path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Treat move as delete + create
        if event.is_directory:
            return
        old = Path(event.src_path)
        new = Path(event.dest_path)
        if old.suffix.lower() in AUDIO_EXT:
            self._remove_if_source(old)
        if _is_audio_file(new):
            threading.Thread(
                target=self._maybe_process_create,
                args=(new,),
                daemon=True,
                name=f"embed-{new.name}",
            ).start()


def start_watcher(core, voices_extra_dir: Path) -> Observer:
    """Start a background watchdog Observer on voices-extra/.

    Returns the Observer (caller can .stop() it on shutdown).
    """
    voices_extra_dir.mkdir(parents=True, exist_ok=True)
    handler = _VoiceFolderHandler(core, voices_extra_dir)
    observer = Observer()
    observer.schedule(handler, str(voices_extra_dir), recursive=False)
    observer.start()
    _LOGGER.info("Voice file-watcher started on %s", voices_extra_dir)
    return observer
