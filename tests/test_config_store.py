from polyglot_tts.config_store import _normalize_languages


def test_dedup_keeps_one_per_language():
    # both german variants -> first one wins, only one survives
    assert _normalize_languages("german,german_24l") == "german"
    assert _normalize_languages("german_24l,german") == "german_24l"


def test_dedup_across_languages_preserves_order():
    out = _normalize_languages("english_2026-04,german_24l,italian_24l,german")
    assert out == "english_2026-04,german_24l,italian_24l"


def test_strips_blanks_and_whitespace():
    assert _normalize_languages(" english_2026-04 , , german_24l ") == \
        "english_2026-04,german_24l"


def test_single_unaffected():
    assert _normalize_languages("french_24l") == "french_24l"
