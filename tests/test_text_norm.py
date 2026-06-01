"""Tests for German ordinal handling in text normalization.

"<number>." is ambiguous in German (ordinal vs. sentence-ending cardinal).
We only convert to an ordinal with a high-precision signal (article/prep
before, or month/century after), and leave bare sentence-ending numbers as
cardinals.
"""

from __future__ import annotations

import pytest

from polyglot_tts.text_norm import normalize

# Skip the whole module if num2words isn't installed (number-to-words is
# what produces ordinals).
pytest.importorskip("num2words")


def n(s: str) -> str:
    return normalize(s, lang="de")


def test_ordinal_after_article():
    assert "zwanzigste" in n("Der 20. Juni ist ein Donnerstag.")
    assert "erste" in n("Der 1. Platz geht an Anna.")


def test_ordinal_dative_declension():
    out = n("Am 1. Mai feiern wir.")
    assert "ersten" in out  # dative "-n" after "Am"


def test_ordinal_before_century():
    assert "zwanzigsten Jahrhundert" in n("Im 20. Jahrhundert.")


def test_sentence_ending_number_stays_cardinal():
    # No ordinal signal → stays a cardinal, period is a sentence break.
    out = n("Es waren 20.")
    assert "zwanzig" in out
    assert "zwanzigste" not in out


def test_bis_number_stays_cardinal():
    out = n("Ich zähle bis 20.")
    assert "zwanzig" in out
    assert "zwanzigste" not in out


def test_full_date():
    out = n("am 3. Dezember 2024")
    assert "dritten Dezember" in out
    assert "zweitausendvierundzwanzig" in out


def test_numbers_use_the_requested_language_not_german():
    # Regression: it/es used to fall back to German number-to-words.
    it = normalize("Ho 20 mele.", lang="it")
    assert "venti" in it and "zwanzig" not in it

    es = normalize("Tengo 20 manzanas.", lang="es")
    assert "veinte" in es and "zwanzig" not in es

    pt = normalize("Tenho 20 maçãs.", lang="pt")
    assert "vinte" in pt and "zwanzig" not in pt


def test_units_localized_per_language():
    assert "per cento" in normalize("50 % di umidità", lang="it")
    assert "por ciento" in normalize("50 % de humedad", lang="es")


def test_no_german_fallback_for_unknown_language():
    # A language num2words supports but we have no unit map for → its own words.
    nl = normalize("Het is 20 graden.", lang="nl")
    assert "twintig" in nl and "zwanzig" not in nl

    # A language num2words does NOT support → the digit is left as-is,
    # never silently read in German.
    xx = normalize("Value is 20 here.", lang="xx")
    assert "20" in xx and "zwanzig" not in xx
