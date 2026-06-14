"""File-watcher for voices-extra/ → auto-embed pipeline.

User drops audio into the mounted voices-extra/ directory; this watcher
notices, waits until the file is fully written (mtime stable for
WAIT_STABLE_SECONDS), then (re)builds the affected voice and registers it on
the shared PolyglotCore.

Per-language references are supported via the filename convention
`<voice>.<bcp47>.<ext>` (e.g. `EL_Jarvis.de.mp3`). All files sharing a voice
name form ONE voice: each language-tagged file is encoded only against the
matching model, an untagged `<voice>.<ext>` is the fallback. Any change to any
of a voice's files re-builds the whole voice from its current files; deleting
the last file drops the voice.

On failure, writes a sidecar `<file>.error` describing the reason.

Runs in its own thread (watchdog's Observer is thread-based, not asyncio).
Encoding is synchronous and CPU/GPU-heavy, so it runs in a background thread —
never blocks any endpoint's event loop.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .voice_loader import AUDIO_EXT, parse_voice_file, reload_voice

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
    """Block until path's size+mtime have been stable for WAIT_STABLE_SECONDS."""
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


class _VoiceFolderHandler(FileSystemEventHandler):
    def __init__(self, core, voices_dir: Path) -> None:
        super().__init__()
        self.core = core
        self.voices_dir = voices_dir
        # De-dup: the same path event can fire twice (create + modify).
        self._processing: set[str] = set()
        self._processing_lock = threading.Lock()
        # Per-voice locks so concurrent file events for the SAME voice (e.g.
        # dropping .de/.en/.fr together) serialize — each reload re-reads the
        # dir, so the last one sees the final state.
        self._voice_locks: dict[str, threading.Lock] = {}
        self._voice_locks_guard = threading.Lock()
        # path -> mtime of the last FAILED attempt. A file that already failed
        # and hasn't changed is skipped (no retry-storm on a broken file).
        self._failed: dict[str, float] = {}
        self._failed_lock = threading.Lock()

    def _voice_lock(self, name: str) -> threading.Lock:
        with self._voice_locks_guard:
            lock = self._voice_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._voice_locks[name] = lock
            return lock

    def _already_failed_unchanged(self, path: Path) -> bool:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        with self._failed_lock:
            return self._failed.get(str(path)) == mtime

    def _record_failure(self, path: Path) -> None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        with self._failed_lock:
            self._failed[str(path)] = mtime

    def _clear_failure(self, path: Path) -> None:
        with self._failed_lock:
            self._failed.pop(str(path), None)

    # ── core work ─────────────────────────────────────────────────────────
    def _process_upsert(self, path: Path) -> None:
        """A file was created/modified → validate it, then rebuild its voice."""
        if not _is_audio_file(path):
            return
        if self._already_failed_unchanged(path):
            return
        with self._processing_lock:
            if str(path) in self._processing:
                return
            self._processing.add(str(path))
        try:
            # Size sanity check — reject obvious mistakes before CPU/GPU work.
            try:
                size = path.stat().st_size
            except OSError:
                return
            if size > MAX_VOICE_FILE_BYTES:
                _LOGGER.warning("Voice file too large (%d bytes): %s",
                                size, path.name)
                _write_error_sidecar(
                    path,
                    f"File is {size // (1024*1024)} MB — too large for a voice "
                    f"sample (max {MAX_VOICE_FILE_BYTES // (1024*1024)} MB). "
                    "A voice sample is 10-30 seconds of speech.")
                self._record_failure(path)
                return
            if not _wait_for_stable(path):
                _LOGGER.warning("Voice file did not stabilize: %s", path)
                _write_error_sidecar(path, "File did not stabilize within 30s")
                self._record_failure(path)
                return

            name, lang = parse_voice_file(path)
            _LOGGER.info("Rebuilding voice '%s' (trigger: %s, lang=%s) ...",
                         name, path.name, lang or "fallback")
            t0 = time.time()
            with self._voice_lock(name):
                try:
                    n = reload_voice(self.core, self.voices_dir, name)
                except Exception as e:  # noqa: BLE001
                    _LOGGER.warning("Voice rebuild failed for %s: %s", name, e)
                    _write_error_sidecar(path, f"Could not process file: {e}")
                    self._record_failure(path)
                    return
            if n > 0:
                _clear_error_sidecar(path)
                self._clear_failure(path)
                _LOGGER.info("Voice '%s' ready in %d language(s) (%d ms)",
                             name, n, int((time.time() - t0) * 1000))
            elif path.exists():
                _write_error_sidecar(
                    path,
                    "Embedding produced no usable language. Possible causes: "
                    "not actually audio, too short, too noisy, or multiple "
                    "speakers.")
                self._record_failure(path)
        finally:
            with self._processing_lock:
                self._processing.discard(str(path))

    def _process_remove(self, path: Path) -> None:
        """A file was deleted/moved away → rebuild its voice from whatever
        files remain, or drop the voice if none are left."""
        self._clear_failure(path)
        name, _lang = parse_voice_file(path)
        with self._voice_lock(name):
            try:
                n = reload_voice(self.core, self.voices_dir, name)
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("Voice rebuild-after-delete failed for %s: %s",
                                name, e)
                return
        if n > 0:
            _LOGGER.info("Voice '%s' rebuilt after removal of %s (%d language(s))",
                         name, path.name, n)
        else:
            _clear_error_sidecar(path)
            _LOGGER.info("Voice '%s' dropped (last reference removed)", name)

    def _spawn(self, target, path: Path, tag: str) -> None:
        threading.Thread(target=target, args=(path,), daemon=True,
                         name=f"{tag}-{path.name}").start()

    # ── watchdog events ───────────────────────────────────────────────────
    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._spawn(self._process_upsert, Path(event.src_path), "embed")

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not _is_audio_file(path):
            return
        with self._processing_lock:
            if str(path) in self._processing:
                return
        self._spawn(self._process_upsert, path, "reembed")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in AUDIO_EXT:
            return
        self._spawn(self._process_remove, path, "remove")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        old = Path(event.src_path)
        new = Path(event.dest_path)
        if old.suffix.lower() in AUDIO_EXT:
            self._spawn(self._process_remove, old, "remove")
        if _is_audio_file(new):
            self._spawn(self._process_upsert, new, "embed")


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
