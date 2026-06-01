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
    assert "POCKET_TTS_TEMP" in cs.RESTART_REQUIRED_KEYS
    os.environ["POCKET_TTS_TEMP"] = "0.5"
    eff = cs.effective_config()
    assert eff["POCKET_TTS_TEMP"]["value"] == "0.5"
    assert eff["POCKET_TTS_TEMP"]["restart_required"] is True
    assert eff["POCKET_TTS_TEMP"]["type"] == "number"
