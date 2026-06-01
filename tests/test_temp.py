import importlib
import os

import pytest


def _resolve():
    # _resolve_temp lives in dispatcher; import lazily so a missing torch/
    # pocket_tts only skips THIS test instead of breaking collection.
    try:
        disp = importlib.import_module("polyglot_tts.dispatcher")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"dispatcher import unavailable: {e}")
    return disp._resolve_temp


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("POCKET_TTS_TEMP", raising=False)
    assert _resolve()() == 0.7


def test_valid_value(monkeypatch):
    monkeypatch.setenv("POCKET_TTS_TEMP", "0.4")
    assert _resolve()() == 0.4


def test_comma_decimal(monkeypatch):
    monkeypatch.setenv("POCKET_TTS_TEMP", "0,9")
    assert _resolve()() == 0.9


def test_out_of_range_falls_back(monkeypatch):
    monkeypatch.setenv("POCKET_TTS_TEMP", "3.0")
    assert _resolve()() == 0.7


def test_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("POCKET_TTS_TEMP", "hot")
    assert _resolve()() == 0.7


def test_config_store_exposes_temp():
    from polyglot_tts import config_store as cs
    assert "POCKET_TTS_TEMP" in cs.EDITABLE_KEYS
    # Temperature applies live now — it is NOT restart-required.
    assert "POCKET_TTS_TEMP" not in cs.RESTART_REQUIRED_KEYS
    os.environ["POCKET_TTS_TEMP"] = "0.5"
    eff = cs.effective_config()
    assert eff["POCKET_TTS_TEMP"]["value"] == "0.5"
    assert eff["POCKET_TTS_TEMP"]["restart_required"] is False
    assert eff["POCKET_TTS_TEMP"]["type"] == "number"


# ── live temperature (clamp + set_temperature) ───────────────────────────────

def test_clamp_temperature():
    from polyglot_tts.core import clamp_temperature
    assert clamp_temperature(None) == 0.7
    assert clamp_temperature("") == 0.7
    assert clamp_temperature("0.4") == 0.4
    assert clamp_temperature("0,9") == 0.9        # comma decimal
    assert clamp_temperature(1.2) == 1.2
    assert clamp_temperature("3.0") == 0.7        # out of range → default
    assert clamp_temperature("hot") == 0.7        # unparseable → default
    assert clamp_temperature("0.05") == 0.7       # below min → default


def test_set_temperature_applies_to_all_models():
    from polyglot_tts.core import PolyglotCore

    class FakeModel:
        sample_rate = 24000
        temp = 0.7

    m1, m2 = FakeModel(), FakeModel()
    core = PolyglotCore(models={"german_24l": m1, "english_2026-04": m2},
                        voice_states={}, default_voice="eve",
                        advertised_bcp47=["de", "en"])
    core.set_temperature(1.1)
    assert m1.temp == 1.1 and m2.temp == 1.1
