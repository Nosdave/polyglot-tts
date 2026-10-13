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


# ── paragraph / list segmentation ────────────────────────────────────────────

def test_list_items_get_periods():
    # bullets are stripped, each item becomes its own sentence
    out = normalize("- Milch\n- Brot\n- Eier", lang="de")
    assert out == "Milch. Brot. Eier."


def test_paragraph_break_gets_period():
    out = normalize("Erster Absatz ohne Punkt\n\nZweiter Absatz", lang="de")
    assert out == "Erster Absatz ohne Punkt. Zweiter Absatz."


def test_existing_terminal_punct_not_doubled():
    # a colon lead-in keeps its colon; items still get periods
    out = normalize("Einkaufsliste:\n- Milch\n- Brot", lang="de")
    assert out == "Einkaufsliste: Milch. Brot."


def test_single_line_untouched_by_segmentation():
    # no newline → no forced period appended
    out = normalize("Hallo Welt", lang="de")
    assert out == "Hallo Welt"


def test_parentheses_become_clean_commas():
    # parens map to commas; cleanup removes the dangling space-before-comma.
    # Single-line input gets no forced terminal period (by design).
    out = normalize("Die Heizung (Vorlauf) läuft", lang="de")
    assert out == "Die Heizung, Vorlauf, läuft"


def test_parenthetical_at_line_end_terminates_cleanly():
    # in a multi-line context the line is terminated; ", ." collapses to "."
    out = normalize("Das ist gut (wirklich)\nNächster Punkt", lang="de")
    assert out == "Das ist gut, wirklich. Nächster Punkt."


def test_numbered_list_items_segment():
    out = normalize("1. eins\n2. zwei", lang="de")
    # leading "1." / "2." markers are list markers; items segmented
    assert out.endswith("zwei.")
    assert "." in out[:-1]


# ── quotes, dashes, abbreviations ────────────────────────────────────────────

def test_quotes_are_dropped():
    assert normalize('Er sagte "Hallo" und ging.', lang="de") == "Er sagte Hallo und ging."
    assert normalize("Er sagte „Hallo“ und ging.", lang="de") == "Er sagte Hallo und ging."


def test_apostrophe_in_word_kept():
    # in-word apostrophe must survive (French elision)
    assert "l'eau" in normalize("Bois de l'eau.", lang="fr").lower()


def test_spaced_hyphen_becomes_comma():
    assert normalize("Das Haus - schön - teuer.", lang="de") == "Das Haus, schön, teuer."


def test_inword_hyphen_and_negative_kept():
    out = normalize("Das E-Auto bei -5 Grad.", lang="de")
    assert "E-Auto" in out
    assert "minus fünf" in out


def test_de_abbreviations():
    assert normalize("Nimm z. B. Milch.", lang="de") == "Nimm zum Beispiel Milch."
    assert normalize("u. a. Brot", lang="de").startswith("unter anderem")
    assert normalize("d. h. später", lang="de").startswith("das heißt")
    assert "und so weiter" in normalize("Äpfel, Birnen usw.", lang="de")


def test_en_abbreviations():
    assert normalize("Use e.g. milk.", lang="en") == "Use for example milk."
    assert "that is" in normalize("dairy, i.e. milk", lang="en")


def test_fr_abbreviations():
    assert normalize("Prends p. ex. du lait.", lang="fr") == "Prends par exemple du lait."


# ── Dutch (pocket-tts 3.3 ships dutch / dutch_24l) ─────────────────────────


def nl(s: str) -> str:
    return normalize(s, lang="nl")


def test_nl_units_and_decimal_comma():
    out = nl("Het is 21,5 °C en de batterij staat op 87 %.")
    assert "eenentwintig komma vijf graden Celsius" in out
    assert "zevenentachtig procent" in out


def test_nl_thousands_dot():
    assert "duizendtweehonderdvijftig" in nl("Dat kost 1.250 euro.")


def test_nl_time_date_range():
    out = nl("Om 14:30 uur op 14.06.2026, 3-5 dagen.")
    assert "veertien uur dertig" in out
    assert "uur uur" not in out
    assert "veertien juni tweeduizendzesentwintig" in out
    assert "drie tot vijf" in out


def test_nl_list_symbols_abbreviations():
    out = nl("1. Licht aan\n2. Rolluik dicht")
    assert out.startswith("Ten eerste, Licht aan.")
    assert "Ten tweede" in out
    assert nl("Neem bijv. melk.") == "Neem bijvoorbeeld melk."
    assert "dat wil zeggen" in nl("d.w.z. later")
    assert "ongeveer tien kilowattuur" in nl("~10 kWh")
    assert "twee tegen één" in nl("Score 2:1")
    assert "drie kleiner dan vijf" in nl("3 < 5")


def test_nl_numbers_never_german():
    out = nl("Er zijn 31 lampen aan.")
    assert "eenendertig" in out
    assert "dreißig" not in out
