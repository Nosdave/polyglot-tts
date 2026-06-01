import importlib

import pytest


def _mod():
    try:
        return importlib.import_module("polyglot_tts.wyoming_handler")
    except Exception as e:  # noqa: BLE001 — pocket_tts/wyoming may be absent
        pytest.skip(f"wyoming_handler import unavailable: {e}")


def test_program_named_polyglot():
    m = _mod()
    info = m.get_wyoming_info(["eve"], ["de", "en"])
    assert info.tts[0].name == "polyglot-tts"


def test_info_reflects_given_voice_list():
    m = _mod()
    info = m.get_wyoming_info(["eve", "newly_cloned"], ["de"])
    names = {v.name for v in info.tts[0].voices}
    assert {"eve", "newly_cloned"} <= names


def test_streaming_advertised():
    m = _mod()
    info = m.get_wyoming_info(["eve"], ["de"])
    assert info.tts[0].supports_synthesize_streaming is True
