"""ffmpeg is only ever pointed at a local audio file: no network or other
protocols, no playlist/script formats. The argument tests always run; the
tests against a real ffmpeg are skipped where none is installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from polyglot_tts import voice_loader
from polyglot_tts.voice_loader import AUDIO_EXT, ensure_decodable, normalize_loudness

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def _option(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


@pytest.mark.parametrize("func", [ensure_decodable, normalize_loudness])
def test_ffmpeg_calls_restrict_protocols_and_formats(tmp_path, monkeypatch, func):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(voice_loader.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(voice_loader.subprocess, "run", fake_run)
    monkeypatch.setenv("POCKET_TTS_VOICE_NORMALIZE", "true")
    src = tmp_path / "memo.m4a"
    src.write_bytes(b"\x00")

    out, is_temp = func(src)
    if is_temp:
        out.unlink(missing_ok=True)

    (cmd,) = calls
    # restrictions are input options: they must come before the input
    i = cmd.index("-i")
    assert cmd.index("-protocol_whitelist") < i and cmd.index("-format_whitelist") < i
    assert _option(cmd, "-protocol_whitelist") == "file"
    assert cmd[i + 1] == f"file:{src}"
    formats = set(_option(cmd, "-format_whitelist").split(","))
    assert formats == {"wav", "mp3", "flac", "ogg", "aac", "mov"}
    for forbidden in ("concat", "hls", "http", "tcp", "lavfi"):
        assert forbidden not in formats


def _make_sample(path: Path) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ac", "1", str(path)],
        check=True,
        capture_output=True,
    )


@needs_ffmpeg
@pytest.mark.parametrize("ext", sorted(AUDIO_EXT))
def test_every_supported_extension_still_decodes(tmp_path, monkeypatch, ext):
    """The format list must not lock out anything the watcher accepts."""
    import soundfile as sf

    monkeypatch.setenv("POCKET_TTS_VOICE_NORMALIZE", "true")
    src = tmp_path / f"sample{ext}"
    _make_sample(src)

    decodable, dec_temp = ensure_decodable(src)
    normalized, norm_temp = normalize_loudness(decodable)
    try:
        assert norm_temp is True, "loudness normalization fell back to the original"
        info = sf.info(str(normalized))
        assert info.samplerate == 24000 and info.channels == 1
        assert info.frames > 12000
    finally:
        if norm_temp:
            normalized.unlink(missing_ok=True)
        if dec_temp:
            decodable.unlink(missing_ok=True)


@needs_ffmpeg
def test_script_format_behind_an_audio_name_is_refused(tmp_path):
    """A concat script named *.m4a must not make ffmpeg open the files it lists."""
    real = tmp_path / "real.wav"
    _make_sample(real)
    fake = tmp_path / "voice.m4a"
    fake.write_text(f"ffconcat version 1.0\nfile '{real}'\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        ensure_decodable(fake)
    # and the best-effort normalizer returns the input instead of a transcode
    fake_wav = tmp_path / "voice.wav"
    fake_wav.write_text(fake.read_text(encoding="utf-8"), encoding="utf-8")
    assert normalize_loudness(fake_wav) == (fake_wav, False)
