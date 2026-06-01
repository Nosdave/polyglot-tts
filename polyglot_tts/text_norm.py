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
    "it": {
        "kWh": "chilowattora", "MWh": "megawattora", "Wh": "wattora",
        "kW": "chilowatt", "MW": "megawatt", "W": "watt",
        "°C": "gradi Celsius", "°F": "gradi Fahrenheit", "K": "kelvin",
        "kV": "chilovolt", "V": "volt", "mV": "millivolt",
        "A": "ampere", "mA": "milliampere",
        "GHz": "gigahertz", "MHz": "megahertz", "kHz": "chilohertz", "Hz": "hertz",
        "lx": "lux", "lm": "lumen", "dB": "decibel",
        "%": "per cento", "‰": "per mille",
        "km/h": "chilometri orari", "m/s": "metri al secondo",
        "km": "chilometri", "m": "metri", "cm": "centimetri", "mm": "millimetri",
        "ms": "millisecondi", "s": "secondi", "min": "minuti", "h": "ore",
        "hPa": "ettopascal", "mbar": "millibar", "bar": "bar",
    },
    "es": {
        "kWh": "kilovatios hora", "MWh": "megavatios hora", "Wh": "vatios hora",
        "kW": "kilovatios", "MW": "megavatios", "W": "vatios",
        "°C": "grados Celsius", "°F": "grados Fahrenheit", "K": "kelvin",
        "kV": "kilovoltios", "V": "voltios", "mV": "milivoltios",
        "A": "amperios", "mA": "miliamperios",
        "GHz": "gigahercios", "MHz": "megahercios", "kHz": "kilohercios", "Hz": "hercios",
        "lx": "lux", "lm": "lúmenes", "dB": "decibelios",
        "%": "por ciento", "‰": "por mil",
        "km/h": "kilómetros por hora", "m/s": "metros por segundo",
        "km": "kilómetros", "m": "metros", "cm": "centímetros", "mm": "milímetros",
        "ms": "milisegundos", "s": "segundos", "min": "minutos", "h": "horas",
        "hPa": "hectopascales", "mbar": "milibares", "bar": "bar",
    },
    "pt": {
        "kWh": "quilowatt-hora", "MWh": "megawatt-hora", "Wh": "watt-hora",
        "kW": "quilowatts", "MW": "megawatts", "W": "watts",
        "°C": "graus Celsius", "°F": "graus Fahrenheit", "K": "kelvin",
        "kV": "quilovolts", "V": "volts", "mV": "milivolts",
        "A": "amperes", "mA": "miliamperes",
        "GHz": "giga-hertz", "MHz": "megahertz", "kHz": "quilohertz", "Hz": "hertz",
        "lx": "lux", "lm": "lúmens", "dB": "decibéis",
        "%": "por cento", "‰": "por mil",
        "km/h": "quilômetros por hora", "m/s": "metros por segundo",
        "km": "quilômetros", "m": "metros", "cm": "centímetros", "mm": "milímetros",
        "ms": "milissegundos", "s": "segundos", "min": "minutos", "h": "horas",
        "hPa": "hectopascais", "mbar": "milibares", "bar": "bar",
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
# German ordinals — "20." → "zwanzigste(n)" instead of "zwanzig" + sentence end
# ─────────────────────────────────────────────────────────────────────────
#
# "<number>." is ambiguous in German: it's an ordinal ("am 20. Juni" =
# "am zwanzigsten Juni") OR a cardinal at a sentence end ("Es waren 20.").
# We can't fully disambiguate without parsing, so we use two high-precision
# signals and leave everything else as a cardinal:
#   (a) an article / preposition immediately before  ("der 1.", "am 20.")
#   (b) a month name or "Jahrhundert" immediately after ("20. Juni")
# Dative-triggering prepositions get the "-n" ending ("am zwanzigsten").

_DE_MONTHS: Final[str] = (
    "Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|"
    "September|Oktober|November|Dezember"
)
# Words before the number that signal an ordinal. Dative ones take "-n".
_DE_DATIVE_BEFORE: Final[frozenset[str]] = frozenset({
    "am", "im", "zum", "zur", "vom", "beim", "dem", "den", "seit",
    "mit", "nach", "bei", "von", "zu", "ab",
})
_DE_NOMINATIVE_BEFORE: Final[frozenset[str]] = frozenset({
    "der", "die", "das", "des", "ein", "eine", "einen", "jeder", "jede",
})

# (a) preposition/article + "<num>."
_DE_ORD_BEFORE: Final[re.Pattern] = re.compile(
    r"\b(" + "|".join(sorted(_DE_DATIVE_BEFORE | _DE_NOMINATIVE_BEFORE)) + r")\s+(\d{1,4})\.",
    re.IGNORECASE,
)
# (b) "<num>." + month / Jahrhundert
_DE_ORD_AFTER: Final[re.Pattern] = re.compile(
    r"\b(\d{1,4})\.\s+(" + _DE_MONTHS + r"|Jahrhunderts?|Jahrtausends?)",
)


def _german_ordinals(text: str, n2w) -> str:
    """Convert clear German ordinals "<num>." → ordinal words. High precision."""
    def _ord(n: int, dative: bool) -> str | None:
        try:
            base = n2w(n, lang="de", to="ordinal")  # e.g. "zwanzigste"
        except Exception:  # noqa: BLE001
            return None
        if dative and base.endswith("e"):
            return base + "n"  # zwanzigste → zwanzigsten
        return base

    def _sub_before(m: re.Match) -> str:
        word, num = m.group(1), int(m.group(2))
        dative = word.lower() in _DE_DATIVE_BEFORE
        o = _ord(num, dative)
        return f"{word} {o}" if o else m.group(0)

    def _sub_after(m: re.Match) -> str:
        num, follow = int(m.group(1)), m.group(2)
        # Dates ("20. Juni") and centuries read most naturally dative-ish;
        # use the "-n" form which is correct after the common "am/im".
        o = _ord(num, dative=True)
        return f"{o} {follow}" if o else m.group(0)

    text = _DE_ORD_BEFORE.sub(_sub_before, text)
    text = _DE_ORD_AFTER.sub(_sub_after, text)
    return text


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
        lang: BCP47 language code (de/en/fr/it/es/pt have unit maps;
              any other language num2words supports still gets correct
              number-to-words).

    Returns:
        Normalized text ready for `model.generate_audio_stream()`.

    Behavior:
        - If `POCKET_TTS_TEXT_NORM=false`, returns input unchanged.
        - If num2words is not installed, numbers are left as digits.
        - Numbers always use the REQUESTED language. If neither our unit map
          nor num2words knows the language, the number is left as digits —
          never silently read in German. Unit symbols are only expanded for
          languages we have a unit map for; otherwise they're left as-is.
    """
    if not _ENABLED or not text:
        return text

    # NOTE: we deliberately do NOT fall back to German for unknown languages.
    # Numbers go through num2words in the requested language (digits if
    # unsupported); units expand only where we have a localized map.

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

    # 5b: German ordinals ("am 20. Juni" → "am zwanzigsten Juni") BEFORE the
    # standalone-number step turns "20" into the cardinal "zwanzig".
    if n2w and lang == "de":
        out = _german_ordinals(out, n2w)

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
