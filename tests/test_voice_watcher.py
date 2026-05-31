"""Unit tests for the voice file-watcher's path→voice tracking and the
audio-format passthrough helper. These cover the two bugs found during the
first real Spark deployment:

  1. Deleting davidneu.m4a wrongly removed the voice registered from
     davidneu.wav (stem collision in on_deleted).
  2. m4a files were accepted by the watcher but failed to decode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polyglot_tts.voice_loader import ensure_decodable
from polyglot_tts.voice_watcher import _VoiceFolderHandler


class _FakeCore:
    def __init__(self) -> None:
        self.voices: dict[str, dict] = {}
        self.models = {"german_24l": object()}

    def encode_voice(self, source):
        return {"german_24l": "state"}

    def add_voice(self, name: str, per_lang: dict) -> None:
        self.voices[name] = per_lang

    def remove_voice(self, name: str) -> bool:
        existed = name in self.voices
        self.voices.pop(name, None)
        return existed


def test_delete_unrelated_stem_keeps_voice(tmp_path: Path) -> None:
    core = _FakeCore()
    handler = _VoiceFolderHandler(core, tmp_path)

    # davidneu.wav registered the voice
    core.add_voice("davidneu", {"de": "state"})
    handler._voice_source["davidneu"] = str(tmp_path / "davidneu.wav")

    # Deleting davidneu.m4a (a DIFFERENT file with the same stem) must NOT
    # drop the voice that came from davidneu.wav.
    handler._remove_if_source(tmp_path / "davidneu.m4a")
    assert "davidneu" in core.voices

    # Deleting the actual source file does remove it.
    handler._remove_if_source(tmp_path / "davidneu.wav")
    assert "davidneu" not in core.voices


def test_delete_untracked_voice_is_noop(tmp_path: Path) -> None:
    core = _FakeCore()
    handler = _VoiceFolderHandler(core, tmp_path)
    # No source recorded → deleting anything is a safe no-op.
    handler._remove_if_source(tmp_path / "ghost.wav")
    assert core.voices == {}


def test_ensure_decodable_native_passthrough(tmp_path: Path) -> None:
    for ext in (".wav", ".flac", ".ogg", ".mp3"):
        p = tmp_path / f"sample{ext}"
        p.write_bytes(b"\x00")
        out, is_temp = ensure_decodable(p)
        assert out == p
        assert is_temp is False


def test_ensure_decodable_m4a_requires_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    # Simulate ffmpeg being absent → clear error, no silent failure.
    monkeypatch.setattr("polyglot_tts.voice_loader.shutil.which", lambda _: None)
    p = tmp_path / "memo.m4a"
    p.write_bytes(b"\x00")
    with pytest.raises(RuntimeError, match="ffmpeg"):
        ensure_decodable(p)


def test_failure_tracking_skips_unchanged_file(tmp_path: Path) -> None:
    import os
    import time as _time

    core = _FakeCore()
    handler = _VoiceFolderHandler(core, tmp_path)
    p = tmp_path / "broken.wav"
    p.write_bytes(b"\x00")

    # Not yet failed.
    assert handler._already_failed_unchanged(p) is False
    # Record a failure → subsequent unchanged check skips it (no retry storm).
    handler._record_failure(p)
    assert handler._already_failed_unchanged(p) is True
    # Replacing the file (new mtime) clears the skip — it gets a fresh try.
    _time.sleep(0.01)
    os.utime(p, None)
    assert handler._already_failed_unchanged(p) is False
