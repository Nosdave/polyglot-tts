"""Text Normalization Pre-Synth Layer (v1.6.0).

Pocket-TTS / Mimi-Decoder spricht Sonderzeichen, Markdown, Abkürzungen und
Zahlen mit Dezimalkomma oft unsauber ("k-w-h" buchstabiert, "23,5" als
Wortsalat). Dieser Layer normalisiert Text VOR `generate_audio_stream()`:

1. Code-Blöcke maskieren (Placeholder), Inhalt entfernen
2. Markdown-Strip (Bold/Italic/Header/Links/Tabellen/Bullets)
3. Sonderzeichen-Map (Pfeile, Quotes, Em-Dash, Klammern → Komma/Space)
4. Unit-Expansion mit Längste-Erst-Match (kWh→Kilowattstunden, °C→Grad
   Celsius, W→Watt) — sprachspezifisch
5. Number-to-Words via num2words (23,5 → dreiundzwanzig Komma fünf)
6. Whitespace-Kollaps

Multi-Language (Phase 1: de, en, fr — direkt aus Pocket-TTS-BCP47-Map).
Phase 2 (später): it, es, pt nachziehen.

Aktivierung: `POCKET_TTS_TEXT_NORM=true` Env-Variable (default: true).
Deaktivieren falls num2words fehlt oder bei Tests Originaltext gewünscht.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final

_LOGGER = logging.getLogger(__name__)

# Toggle per env-var
_ENABLED: Final[bool] = os.environ.get(
    "POCKET_TTS_TEXT_NORM", "true"
).lower() in ("1", "true", "yes")

# Lazy num2words import — let normalize() degrade gracefully if missing
_NUM2WORDS_AVAILABLE: bool | None = None


def _try_num2words():
    """Lazy-import num2words. Returns the callable or None."""
    global _NUM2WORDS_AVAILABLE
    if _NUM2WORDS_AVAILABLE is False:
        return None
    try:
        from num2words import num2words  # type: ignore[import-not-found]
        _NUM2WORDS_AVAILABLE = True
        return num2words
    except ImportError:
        if _NUM2WORDS_AVAILABLE is None:
            _LOGGER.warning(
                "num2words not installed — number-to-words disabled. "
                "pip install num2words for better TTS pronunciation."
            )
        _NUM2WORDS_AVAILABLE = False
        return None


# ─────────────────────────────────────────────────────────────────────────
# Markdown / structural-character strip
# ─────────────────────────────────────────────────────────────────────────

# Order matters: code-blocks first to protect their content from later strips
_MD_PATTERNS: Final[list[tuple[re.Pattern, str]]] = [
    (re.compile(r"```[\s\S]*?```"), " "),               # fenced code → space
    (re.compile(r"`([^`]+)`"), r"\1"),                  # inline code → content
    (re.compile(r"!\[[^\]]*\]\([^)]+\)"), ""),          # images → nothing
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),      # links → text
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),       # headers → strip prefix
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"\1"),          # bold ** → content
    (re.compile(r"__([^_\n]+)__"), r"\1"),              # bold __ → content
    (re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)"), r"\1"), # italic * → content
    (re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)"), r"\1"),   # italic _ → content
    (re.compile(r"^\s{0,3}[-*+]\s+", re.M), ""),        # bullet → strip
    (re.compile(r"^\s{0,3}>\s+", re.M), ""),            # blockquote → strip
    (re.compile(r"\|+"), " "),                          # table pipes → space
    (re.compile(r"^[-:|\s]+$", re.M), ""),              # table separators
]


# Per-language punctuation/whitespace maps
# We replace certain Unicode-punct with ASCII equivalents that TTS handles
# better as pauses (commas, periods).
_PUNCT_MAP: Final[dict[str, str]] = {
    "…": ", ",       # ellipsis → comma+pause
    "–": "-",         # en-dash → hyphen
    "—": ", ",       # em-dash → comma+pause
    "→": " ",
    "←": " ",
    "↑": " ",
    "↓": " ",
    "↔": " ",
    "„": '"',         # German opening quote
    "“": '"',         # German closing quote (= English opening)
    "”": '"',
    "«": '"',
    "»": '"',
    "‚": "'",
    "‘": "'",
    "’": "'",
    "(": ", ",
    ")": ", ",
    "[": " ",
    "]": " ",
    "{": " ",
    "}": " ",
    "#": "",
    "•": ", ",
    "·": ", ",
    "★": "",
    "☆": "",
}


# ─────────────────────────────────────────────────────────────────────────
# Unit expansion — sprachspezifisch
# Composite-Units (z.B. km/h) müssen explizit drinstehen, sonst werden
# sie an "/" zerlegt und einzelne Token wandern durch.
# ─────────────────────────────────────────────────────────────────────────

_UNITS: Final[dict[str, dict[str, str]]] = {
    "de": {
        # Energy / Power
        "kWh": "Kilowattstunden", "MWh": "Megawattstunden", "Wh": "Wattstunden",
        "kW": "Kilowatt", "MW": "Megawatt", "W": "Watt",
        # Temperature
        "°C": "Grad Celsius", "°F": "Grad Fahrenheit", "K": "Kelvin",
        # Electrical
        "kV": "Kilovolt", "V": "Volt", "mV": "Millivolt",
        "kA": "Kiloampere", "A": "Ampere", "mA": "Milliampere",
        "GHz": "Gigahertz", "MHz": "Megahertz", "kHz": "Kilohertz", "Hz": "Hertz",
        # Light / Sound
        "lx": "Lux", "lm": "Lumen", "dB": "Dezibel",
        # Percent / Promille
        "%": "Prozent", "‰": "Promille",
        # Length
        "km/h": "Kilometer pro Stunde", "m/s": "Meter pro Sekunde",
        "km": "Kilometer", "m": "Meter", "cm": "Zentimeter", "mm": "Millimeter",
        # Time
        "ms": "Millisekunden", "s": "Sekunden", "min": "Minuten", "h": "Stunden",
        # Pressure
        "hPa": "Hektopascal", "mbar": "Millibar", "bar": "Bar",
    },
    "en": {
        "kWh": "kilowatt hours", "MWh": "megawatt hours", "Wh": "watt hours",
        "kW": "kilowatts", "MW": "megawatts", "W": "watts",
        "°C": "degrees Celsius", "°F": "degrees Fahrenheit", "K": "Kelvin",
        "kV": "kilovolts", "V": "volts", "mV": "millivolts",
        "A": "amps", "mA": "milliamps",
        "GHz": "gigahertz", "MHz": "megahertz", "kHz": "kilohertz", "Hz": "hertz",
        "lx": "lux", "lm": "lumens", "dB": "decibels",
        "%": "percent", "‰": "per mille",
        "km/h": "kilometers per hour", "m/s": "meters per second",
        "km": "kilometers", "m": "meters", "cm": "centimeters", "mm": "millimeters",
        "ms": "milliseconds", "s": "seconds", "min": "minutes", "h": "hours",
        "hPa": "hectopascals", "mbar": "millibars", "bar": "bar",
    },
    "fr": {
        "kWh": "kilowattheures", "MWh": "mégawattheures", "Wh": "wattheures",
        "kW": "kilowatts", "MW": "mégawatts", "W": "watts",
        "°C": "degrés Celsius", "°F": "degrés Fahrenheit", "K": "kelvin",
        "kV": "kilovolts", "V": "volts", "mV": "millivolts",
        "A": "ampères", "mA": "milliampères",
        "GHz": "gigahertz", "MHz": "mégahertz", "kHz": "kilohertz", "Hz": "hertz",
        "lx": "lux", "lm": "lumens", "dB": "décibels",
        "%": "pour cent", "‰": "pour mille",
        "km/h": "kilomètres par heure", "m/s": "mètres par seconde",
        "km": "kilomètres", "m": "mètres", "cm": "centimètres", "mm": "millimètres",
        "ms": "millisecondes", "s": "secondes", "min": "minutes", "h": "heures",
        "hPa": "hectopascals", "mbar": "millibars", "bar": "bars",
    },
}


def _build_unit_pattern(lang: str) -> re.Pattern | None:
    """Compile a regex matching `<number><unit>` for the given language.

    Longest-first sorting so kWh matches before W, km/h before km.
    """
    units = _UNITS.get(lang)
    if not units:
        return None
    sorted_keys = sorted(units.keys(), key=len, reverse=True)
    # Pattern: optional sign, integer or decimal (German: 23,5 or 23.5),
    # optional space, unit token. Word-boundary at end if unit ends in letter.
    return re.compile(
        r"(-?\d+(?:[.,]\d+)?)\s*("
        + "|".join(re.escape(u) for u in sorted_keys)
        + r")(?![A-Za-z])"
    )


# Cached compiled patterns
_UNIT_PATTERNS: dict[str, re.Pattern] = {
    lang: p for lang in _UNITS.keys()
    if (p := _build_unit_pattern(lang)) is not None
}

# Standalone number pattern — matches numbers NOT followed by a unit
# (units are handled in the previous step). German uses , as decimal,
# English uses . as decimal. We handle both for robustness.
_NUMBER_PATTERN: Final[re.Pattern] = re.compile(
    r"(?<![A-Za-z0-9.])-?\d+(?:[.,]\d+)?(?![A-Za-z0-9])"
)


# ─────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────


def _parse_number(s: str, lang: str) -> float | None:
    """Parse a localized number string to float.

    German/French use ',' as decimal. English uses '.' as decimal.
    We accept BOTH because LLMs are inconsistent.
    """
    s = s.strip()
    if not s:
        return None
    # If ends in . it's not a decimal (could be sentence-end), strip
    s = s.rstrip(".")
    if not s:
        return None
    # Normalize: replace comma with point for float parsing
    s_norm = s.replace(",", ".")
    try:
        return float(s_norm)
    except ValueError:
        return None


def normalize(text: str, lang: str = "de") -> str:
    """Normalize text for TTS synthesis.

    Args:
        text: raw input (may contain Markdown, units, special chars)
        lang: BCP47 language code (de/en/fr — others fall back to de)

    Returns:
        Normalized text ready for `model.generate_audio_stream()`.

    Behavior:
        - If `POCKET_TTS_TEXT_NORM=false`, returns input unchanged.
        - If num2words not installed, skip number-to-words step (logs warning).
        - Unknown language falls back to 'de' for unit map.
    """
    if not _ENABLED or not text:
        return text

    # Default to German if language not supported by our unit map
    if lang not in _UNITS:
        lang = "de"

    # 1+2+3: Markdown strip
    out = text
    for pat, repl in _MD_PATTERNS:
        out = pat.sub(repl, out)

    # 4: Punctuation / special character map
    for k, v in _PUNCT_MAP.items():
        if k in out:
            out = out.replace(k, v)

    # 5: Unit expansion (number + unit-token together)
    unit_pat = _UNIT_PATTERNS.get(lang)
    n2w = _try_num2words()
    if unit_pat and n2w:
        def _expand_unit(m: re.Match) -> str:
            num_str, unit_key = m.group(1), m.group(2)
            n = _parse_number(num_str, lang)
            if n is None:
                return m.group(0)
            try:
                # num2words handles integer vs float
                if n == int(n):
                    word = n2w(int(n), lang=lang)
                else:
                    word = n2w(n, lang=lang)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("num2words(%r, lang=%r) failed: %s", n, lang, e)
                return m.group(0)
            return f"{word} {_UNITS[lang][unit_key]}"
        out = unit_pat.sub(_expand_unit, out)

    # 6: Standalone numbers (not attached to a known unit)
    if n2w:
        def _expand_number(m: re.Match) -> str:
            n = _parse_number(m.group(0), lang)
            if n is None:
                return m.group(0)
            try:
                if n == int(n):
                    return n2w(int(n), lang=lang)
                return n2w(n, lang=lang)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("num2words(%r) failed: %s", n, e)
                return m.group(0)
        out = _NUMBER_PATTERN.sub(_expand_number, out)

    # 7: Collapse whitespace
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ─────────────────────────────────────────────────────────────────────────
# Smoke-test entry point — run via `python -m wyoming_pocket_tts.text_norm`
# ─────────────────────────────────────────────────────────────────────────


def _smoke_test() -> None:
    """Print normalize() output for a hand-curated test-suite. Manual review."""
    cases_de = [
        "Die PV liefert **4500 W** bei 23,5 °C.",
        "Batterie: 87 % SoC, 1,2 kWh übrig",
        "# Heizung\n- Vorlauf: 42°C",
        "Verbrauch 1707 kWh in 24 h",
        "*Ladung* bei -5 °C → fail",
        "`sensor.temp` = 19,8",
        "[Doku](http://x) lesen",
        "50 Hz / 230 V / 16 A",
        "Druck 1013 hPa, Wind 12,5 km/h",
        "Spannung «schwankt» stark",
    ]
    cases_en = [
        "PV delivers **4500 W** at 23.5 °C.",
        "Battery: 87 % SoC, 1.2 kWh left",
        "Wind: 12.5 km/h, pressure 1013 hPa",
    ]
    cases_fr = [
        "Le PV délivre **4500 W** à 23,5 °C.",
        "Batterie: 87 %, 1,2 kWh restant",
    ]

    print(f"text_norm.py — num2words available: {_try_num2words() is not None}")
    print(f"             POCKET_TTS_TEXT_NORM enabled: {_ENABLED}\n")

    for lang, cases in (("de", cases_de), ("en", cases_en), ("fr", cases_fr)):
        print(f"════════ [{lang}] ════════")
        for inp in cases:
            out = normalize(inp, lang=lang)
            print(f"  IN : {inp!r}")
            print(f"  OUT: {out!r}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    _smoke_test()
