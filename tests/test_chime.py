"""The chime marker is answered with a short tone instead of speech."""
import numpy as np

from polyglot_tts.core import CHIME_MARKER, SAMPLE_RATE, chime_pcm16, chime_samples, is_chime, is_chime_prefix


def test_marker_detection():
    assert is_chime(CHIME_MARKER)
    assert is_chime(f"  {CHIME_MARKER}\n")
    assert not is_chime("Licht an.")
    assert not is_chime(f"{CHIME_MARKER} und Text")
    assert not is_chime("")


def test_chime_audio_shape_and_level():
    samples = chime_samples(SAMPLE_RATE)
    assert 0.2 < len(samples) / SAMPLE_RATE < 0.35
    assert np.max(np.abs(samples)) <= 0.33
    pcm = chime_pcm16(SAMPLE_RATE)
    assert len(pcm) == len(samples) * 2
    # soft start (no click) and quiet end
    assert abs(samples[0]) < 0.01 and abs(samples[-1]) < 0.05


def test_marker_prefix_and_gain():
    assert is_chime_prefix("[[poly")
    assert is_chime_prefix(CHIME_MARKER)
    assert not is_chime_prefix("Licht")
    assert not is_chime_prefix("[[polyglot:chime]] und")
    silent = chime_pcm16(SAMPLE_RATE, 0.0)
    assert not any(silent)
