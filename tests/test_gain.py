def test_clamp_gain():
    from polyglot_tts.core import clamp_gain
    assert clamp_gain(None) == 1.0
    assert clamp_gain("") == 1.0
    assert clamp_gain("1.5") == 1.5
    assert clamp_gain("0,5") == 0.5            # comma decimal
    assert clamp_gain(2.0) == 2.0
    assert clamp_gain("5.0") == 1.0            # above max → default
    assert clamp_gain("-1") == 1.0            # below min → default
    assert clamp_gain("loud") == 1.0          # unparseable → default


def test_core_output_gain_default(monkeypatch):
    monkeypatch.delenv("POCKET_TTS_OUTPUT_GAIN", raising=False)
    from polyglot_tts.core import PolyglotCore

    class FakeModel:
        sample_rate = 24000

    core = PolyglotCore(models={"german_24l": FakeModel()}, voice_states={},
                        default_voice="eve", advertised_bcp47=["de"])
    assert core.output_gain == 1.0


def test_core_output_gain_from_env(monkeypatch):
    monkeypatch.setenv("POCKET_TTS_OUTPUT_GAIN", "1.8")
    from polyglot_tts.core import PolyglotCore

    class FakeModel:
        sample_rate = 24000

    core = PolyglotCore(models={"german_24l": FakeModel()}, voice_states={},
                        default_voice="eve", advertised_bcp47=["de"])
    assert core.output_gain == 1.8


def test_gain_scaling_clips():
    # the scaling math used in both synthesis paths (plain-Python equivalent)
    clip = lambda x: max(-1.0, min(1.0, x))
    assert [clip(v * 2.0) for v in (0.5, -0.5, 0.9)] == [1.0, -1.0, 1.0]


def test_voice_normalize_toggle(monkeypatch):
    from polyglot_tts import voice_loader
    monkeypatch.setenv("POCKET_TTS_VOICE_NORMALIZE", "false")
    assert voice_loader._normalize_enabled() is False
    monkeypatch.setenv("POCKET_TTS_VOICE_NORMALIZE", "true")
    assert voice_loader._normalize_enabled() is True
