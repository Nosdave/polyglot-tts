"""Tests for the live-tunable default language + LID threshold (v0.7.3).

Regression context: the effective default used to be "first loaded
checkpoint", so a UI save that reordered POCKET_TTS_LANGUAGES silently
flipped every short reply (below the LID threshold) to another language.
"""

import polyglot_tts.core as core_mod
from polyglot_tts.core import (
    MIN_LID_CHARS_DEFAULT,
    MIN_LID_CHARS_MAX,
    MIN_LID_CHARS_MIN,
    min_lid_chars,
    resolve_default_language,
)

# english-first map — the exact load order that triggered the original bug.
BCP_MAP = {"en": "english_2026-04", "fr": "french_24l", "de": "german_24l"}
FIRST = "english_2026-04"


def _clear_warned():
    core_mod._warned_default_language.clear()


# ── resolve_default_language ───────────────────────────────────────────────

def test_unset_keeps_legacy_first_loaded(monkeypatch):
    monkeypatch.delenv("POCKET_TTS_DEFAULT_LANGUAGE", raising=False)
    assert resolve_default_language(BCP_MAP, FIRST) == \
        ("en", "english_2026-04", "first-loaded")


def test_empty_and_whitespace_keep_legacy(monkeypatch):
    for raw in ("", "   "):
        monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", raw)
        assert resolve_default_language(BCP_MAP, FIRST)[2] == "first-loaded"


def test_explicit_de_wins_over_english_first_load_order(monkeypatch):
    # THE regression case: english loads first, but the user's primary is de.
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "de")
    assert resolve_default_language(BCP_MAP, FIRST) == \
        ("de", "german_24l", "explicit")


def test_full_bcp47_tag_and_case_are_normalized(monkeypatch):
    for raw in ("de-DE", "DE", "De-at"):
        monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", raw)
        bcp47, ckpt, source = resolve_default_language(BCP_MAP, FIRST)
        assert (bcp47, ckpt, source) == ("de", "german_24l", "explicit"), raw


def test_unloaded_language_falls_back_to_first_loaded(monkeypatch):
    _clear_warned()
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "it")  # not loaded
    assert resolve_default_language(BCP_MAP, FIRST) == \
        ("en", "english_2026-04", "first-loaded")


def test_garbage_value_falls_back(monkeypatch):
    _clear_warned()
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "klingon")
    assert resolve_default_language(BCP_MAP, FIRST)[2] == "first-loaded"


def test_unloaded_language_warns_once_per_value(monkeypatch, caplog):
    _clear_warned()
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "pt")
    with caplog.at_level("WARNING"):
        resolve_default_language(BCP_MAP, FIRST)
        resolve_default_language(BCP_MAP, FIRST)
    warnings = [r for r in caplog.records
                if "POCKET_TTS_DEFAULT_LANGUAGE" in r.getMessage()]
    assert len(warnings) == 1


def test_live_change_applies_without_restart(monkeypatch):
    # The properties read env per call — simulate a UI save mid-flight.
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "fr")
    assert resolve_default_language(BCP_MAP, FIRST)[0] == "fr"
    monkeypatch.setenv("POCKET_TTS_DEFAULT_LANGUAGE", "de")
    assert resolve_default_language(BCP_MAP, FIRST)[0] == "de"
    monkeypatch.delenv("POCKET_TTS_DEFAULT_LANGUAGE")
    assert resolve_default_language(BCP_MAP, FIRST)[0] == "en"


# ── min_lid_chars ──────────────────────────────────────────────────────────

def test_min_lid_chars_default(monkeypatch):
    monkeypatch.delenv("POCKET_TTS_MIN_LID_CHARS", raising=False)
    assert min_lid_chars() == MIN_LID_CHARS_DEFAULT


def test_min_lid_chars_reads_env_live(monkeypatch):
    monkeypatch.setenv("POCKET_TTS_MIN_LID_CHARS", "8")
    assert min_lid_chars() == 8
    monkeypatch.setenv("POCKET_TTS_MIN_LID_CHARS", "40")
    assert min_lid_chars() == 40


def test_min_lid_chars_clamps_to_bounds(monkeypatch):
    monkeypatch.setenv("POCKET_TTS_MIN_LID_CHARS", "0")
    assert min_lid_chars() == MIN_LID_CHARS_MIN
    monkeypatch.setenv("POCKET_TTS_MIN_LID_CHARS", "-5")
    assert min_lid_chars() == MIN_LID_CHARS_MIN
    monkeypatch.setenv("POCKET_TTS_MIN_LID_CHARS", "99999")
    assert min_lid_chars() == MIN_LID_CHARS_MAX


def test_min_lid_chars_bad_input_returns_default(monkeypatch):
    for raw in ("abc", "", "  "):
        monkeypatch.setenv("POCKET_TTS_MIN_LID_CHARS", raw)
        assert min_lid_chars() == MIN_LID_CHARS_DEFAULT, raw
    monkeypatch.setenv("POCKET_TTS_MIN_LID_CHARS", "12.7")
    assert min_lid_chars() == 12  # floats truncate, not error


# ── config_store integration ───────────────────────────────────────────────

def test_new_keys_are_editable_and_live():
    from polyglot_tts import config_store
    for key in ("POCKET_TTS_DEFAULT_LANGUAGE", "POCKET_TTS_MIN_LID_CHARS"):
        assert key in config_store.EDITABLE_KEYS
        assert key not in config_store.RESTART_REQUIRED_KEYS
        assert key in config_store.FIELD_META


def test_save_settings_applies_live_and_clears(tmp_path, monkeypatch):
    from polyglot_tts import config_store
    monkeypatch.setenv("POCKET_TTS_CONFIG_FILE", str(tmp_path / "settings.json"))
    monkeypatch.delenv("POCKET_TTS_DEFAULT_LANGUAGE", raising=False)

    config_store.save_settings({"POCKET_TTS_DEFAULT_LANGUAGE": "de",
                                "POCKET_TTS_MIN_LID_CHARS": "8"})
    import os
    assert os.environ.get("POCKET_TTS_DEFAULT_LANGUAGE") == "de"
    assert os.environ.get("POCKET_TTS_MIN_LID_CHARS") == "8"

    # Empty value = remove key = back to legacy behaviour (also cleared live).
    config_store.save_settings({"POCKET_TTS_DEFAULT_LANGUAGE": ""})
    assert "POCKET_TTS_DEFAULT_LANGUAGE" not in os.environ
    assert "POCKET_TTS_DEFAULT_LANGUAGE" not in config_store._read_file()
