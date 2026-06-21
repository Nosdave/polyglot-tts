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
    (re.compile(r"^\s{0,3}[-*+•‣◦]\s+", re.M), ""),      # bullet → strip
    (re.compile(r"^\s{0,3}>\s+", re.M), ""),            # blockquote → strip
    (re.compile(r"\|+"), " "),                          # table pipes → space
    (re.compile(r"^[-:|\s]+$", re.M), ""),              # table separators
]

# Emoji / pictographs are removed before synthesis — the model otherwise mumbles
# over them. Deliberately scoped to the emoji blocks: the math/arrow symbols we
# actually speak (→ U+2192, ← ↑ ↓ ↔, < > = ~) live OUTSIDE these ranges and are
# handled later by _expand_symbols, so they are NOT stripped here.
_EMOJI_RE: Final = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoticons, pictographs, transport, symbols ext-A, cards
    "\U00002600-\U000026FF"   # miscellaneous symbols (☀ ☂ ⚡ ☎ …)
    "\U00002700-\U000027BF"   # dingbats (✅ ❤ ✂ ✈ …)
    "\U00002B00-\U00002BFF"   # stars/arrows-as-emoji (⭐ ⬆ ⬇ …)
    "\U0000FE00-\U0000FE0F"   # variation selectors (emoji presentation)
    "\U0001F3FB-\U0001F3FF"   # skin-tone modifiers
    "‍♀♂"      # ZWJ + gender signs (emoji sequences)
    "™©®"      # trademark, copyright, registered
    "]+"
)


# Punctuation that already ends a clause/sentence with a pause or stop. A line
# ending in one of these is NOT given an extra period by _terminate_lines.
_TERMINAL_PUNCT: Final[frozenset[str]] = frozenset(".!?:;,…")


def _terminate_lines(text: str) -> str:
    """Add a period at structural line breaks (paragraphs, list items) that
    don't already end with punctuation, then join everything onto one line.

    Without this, the final whitespace-collapse turns

        Milch
        Brot
        Eier

    into "Milch Brot Eier" — spoken as one breathless run. Each line becomes
    its own sentence instead: "Milch. Brot. Eier."

    Single-line input is returned untouched (we don't force a period onto a
    short prompt). The one false-positive is genuinely soft-wrapped prose (a
    sentence broken across lines), which gets an extra pause at the wrap — far
    less jarring than the run-on it prevents, and rare for TTS input that is
    usually one line per paragraph.
    """
    if "\n" not in text:
        return text
    out: list[str] = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            continue
        if s[-1] not in _TERMINAL_PUNCT:
            s += "."
        out.append(s)
    return " ".join(out)


# Per-language punctuation/whitespace maps
# We replace certain Unicode-punct with ASCII equivalents that TTS handles
# better as pauses (commas, periods).
_PUNCT_MAP: Final[dict[str, str]] = {
    "…": ", ",       # ellipsis → comma+pause
    "–": ", ",       # en-dash (Gedankenstrich) → comma+pause
    "—": ", ",       # em-dash (Gedankenstrich) → comma+pause
    # "→" is spoken ("daraus folgt"), handled in _expand_symbols — not here.
    "←": " ",
    "↑": " ",
    "↓": " ",
    "↔": " ",
    # Quotation marks are delimiters, not spoken — drop them entirely. In-word
    # apostrophes (l'eau, don't) are kept as a straight ' below.
    "„": "",          # German opening quote
    "“": "",          # German/English closing/opening quote
    "”": "",
    "«": "",          # French guillemets
    "»": "",
    '"': "",          # ASCII double quote
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
# Dates, clock times, numbered-list markers, math symbols
#
# These run BEFORE the generic number/decimal step in normalize(): otherwise
# "14.06" is read as the decimal "vierzehn Komma null sechs", "22:57" keeps its
# colon, list markers become "eins. zwei.", and "=" is dropped.
# ─────────────────────────────────────────────────────────────────────────

_MONTHS: Final[dict[str, list[str]]] = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
           "August", "September", "Oktober", "November", "Dezember"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"],
}

# Ordinal adverbs for numbered-list enumerators (1-based). Outside this range
# the bare marker is dropped (the item text is just read on its own).
_LIST_ORDINALS: Final[dict[str, list[str]]] = {
    "de": ["Erstens", "Zweitens", "Drittens", "Viertens", "Fünftens",
           "Sechstens", "Siebtens", "Achtens", "Neuntens", "Zehntens"],
    "en": ["First", "Second", "Third", "Fourth", "Fifth", "Sixth",
           "Seventh", "Eighth", "Ninth", "Tenth"],
    "fr": ["Premièrement", "Deuxièmement", "Troisièmement", "Quatrièmement",
           "Cinquièmement", "Sixièmement", "Septièmement", "Huitièmement",
           "Neuvièmement", "Dixièmement"],
}

# Math / relation symbols, spoken per language. Multi-character tokens
# ("->", "=>") MUST be applied before the bare "<"/">"/"=" — _expand_symbols
# sorts by length so the arrow wins. An arrow ("→", "->", "=>", "⇒") is read as
# a directional connector ("zu"/"to"/"vers") — the most universal rendering
# across the senses an LLM uses it in (path, mapping, sequence, implication),
# none of which a single word fits perfectly but a direction word is never
# jarring. A tilde before a number ("~10") is an approximation (via _APPROX).
_SYMBOLS: Final[dict[str, dict[str, str]]] = {
    "de": {"->": "zu", "=>": "zu", "→": "zu", "⇒": "zu",
           "<": "kleiner als", ">": "größer als", "=": "gleich"},
    "en": {"->": "to", "=>": "to", "→": "to", "⇒": "to",
           "<": "less than", ">": "greater than", "=": "equals"},
    "fr": {"->": "vers", "=>": "vers", "→": "vers", "⇒": "vers",
           "<": "inférieur à", ">": "supérieur à", "=": "égale"},
    "it": {"->": "verso", "=>": "verso", "→": "verso", "⇒": "verso",
           "<": "minore di", ">": "maggiore di", "=": "uguale"},
    "es": {"->": "hacia", "=>": "hacia", "→": "hacia", "⇒": "hacia",
           "<": "menor que", ">": "mayor que", "=": "igual"},
    "pt": {"->": "para", "=>": "para", "→": "para", "⇒": "para",
           "<": "menor que", ">": "maior que", "=": "igual"},
}

# "~" immediately before a number is an approximation: "~10" → "circa 10".
# A lone "~" or "~~strikethrough~~" (not before a digit) is left untouched.
_APPROX: Final[dict[str, str]] = {
    "de": "circa", "en": "about", "fr": "environ",
    "it": "circa", "es": "aproximadamente", "pt": "aproximadamente",
}

# Currency symbols spoken as a word AFTER the amount, per language: (singular,
# plural) — plural unless the amount is exactly 1. German forms are invariant.
# "$12" → "12 Dollar", "50 €" → "50 Euro". The digits stay for the number step.
_CURRENCY: Final[dict[str, dict[str, tuple[str, str]]]] = {
    "$": {"de": ("Dollar", "Dollar"), "en": ("dollar", "dollars"),
          "fr": ("dollar", "dollars"), "it": ("dollaro", "dollari"),
          "es": ("dólar", "dólares"), "pt": ("dólar", "dólares")},
    "€": {"de": ("Euro", "Euro"), "en": ("euro", "euros"),
          "fr": ("euro", "euros"), "it": ("euro", "euro"),
          "es": ("euro", "euros"), "pt": ("euro", "euros")},
    "£": {"de": ("Pfund", "Pfund"), "en": ("pound", "pounds"),
          "fr": ("livre", "livres"), "it": ("sterlina", "sterline"),
          "es": ("libra", "libras"), "pt": ("libra", "libras")},
}
_CUR_NUM = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_CUR_BEFORE: Final = re.compile(r"([$€£])\s?(" + _CUR_NUM + r")")
_CUR_AFTER: Final = re.compile(r"(?<![A-Za-z0-9])(" + _CUR_NUM + r")\s?([$€£])")


def _expand_currency(text: str, lang: str) -> str:
    """Currency symbol next to an amount → spoken word after it ('$12' → '12
    Dollar', '50 €' → '50 Euro'). The digits stay for the later number step,
    except German '1', which is spelled here as 'ein' (not 'eins') since all
    currency nouns are masculine/neuter ('ein Euro', 'ein Dollar', 'ein Pfund')."""
    def _spoken(sym: str, num: str) -> str | None:
        forms = _CURRENCY.get(sym, {}).get(lang)
        if not forms:
            return None
        if num.strip() == "1":
            # de: 'ein <currency>'. Other languages spell 1 correctly in the
            # number step (one / un / uno / um), so leave the digit.
            amount = "ein" if lang == "de" else num
            return f"{amount} {forms[0]}"
        return f"{num} {forms[1]}"

    def _before(m: "re.Match") -> str:
        return _spoken(m.group(1), m.group(2)) or m.group(0)

    def _after(m: "re.Match") -> str:
        return _spoken(m.group(2), m.group(1)) or m.group(0)

    return _CUR_AFTER.sub(_after, _CUR_BEFORE.sub(_before, text))

# Clock-time connector: "22:57" -> "<h> <connector> <m>". Languages without an
# entry just get "<h> <m>".
_TIME_CONNECTOR: Final[dict[str, str]] = {"de": "Uhr", "fr": "heures"}

# A digit:digit colon that is NOT a clock time (e.g. score "2:1", ratio "16:9")
# → spoken separator. Languages without an entry just lose the colon.
_RATIO_SEP: Final[dict[str, str]] = {"de": " zu ", "en": " to ", "fr": " à "}

# A hyphen / en-dash / em-dash between two integers is a "von-bis" range:
# "10-20" → "10 bis 20". The lookarounds skip dash chains (e.g. ISO "2026-06-14").
_RANGE_SEP: Final[dict[str, str]] = {"de": "bis", "en": "to", "fr": "à",
                                     "it": "a", "es": "a", "pt": "a"}
_RANGE_RE: Final = re.compile(r"(?<![\d.\-–—])(\d+)\s*[-–—]\s*(\d+)(?![\d.\-–—])")

_DATE_RE: Final = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d)")
# Optionally swallow a trailing unit ("22:57 Uhr" → no doubled "Uhr").
_TIME_RE: Final = re.compile(
    r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)(?:\s*(?:Uhr|heures?|h)\b)?",
    re.IGNORECASE)
_LIST_RE: Final = re.compile(r"(?m)^[ \t]*(\d{1,2})\.[ \t]+")
# German dative triggers: "am 14.06." → "am vierzehntEN Juni" (not -er).
_DE_DATIVE_WORDS: Final = {"am", "vom", "zum", "beim", "dem"}


def _expand_list_markers(text: str, lang: str) -> str:
    """Leading 'N. ' list enumerators -> spoken ordinal ('1.' -> 'Erstens, ').

    Must run while line breaks are still present (before `_terminate_lines`).
    """
    table = _LIST_ORDINALS.get(lang)

    def _sub(m: "re.Match") -> str:
        n = int(m.group(1))
        if table and 1 <= n <= len(table):
            return f"{table[n - 1]}, "
        return ""  # unknown index -> drop the bare marker

    return _LIST_RE.sub(_sub, text)


def _expand_dates(text: str, lang: str, n2w) -> str:
    """Numeric dates 'DD.MM[.]' -> 'vierzehnter Juni' (de) / '14 juin' (fr).

    Only de/fr (German decimals use ',', so 'DD.MM' is unambiguously a date).
    To avoid eating version numbers like '3.5', a dotless 'DD.MM' is treated as
    a date only when the day is > 12 (can't be a month) or a trailing dot marks
    it explicitly.
    """
    months = _MONTHS.get(lang)
    if not months or not n2w:
        return text

    def _sub(m: "re.Match") -> str:
        d, mo, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return m.group(0)
        # Only treat as a date when unambiguous: day > 12 (can't be a month) or
        # an explicit 4-digit year. Otherwise "3.5" / "1.12" stay numbers — a
        # version like "Mistral 3.5." must NOT become "dritter Mai".
        if d <= 12 and not yr:
            return m.group(0)
        if lang == "de":
            try:
                day = n2w(d, lang="de", to="ordinal")  # 'vierzehnte'
            except Exception:  # noqa: BLE001
                return m.group(0)
            # Dative after am/vom/zum/... ("am vierzehnten"), else nominative
            # citation form ("vierzehnter Juni").
            prev = m.string[:m.start()].rstrip()
            last = prev.rsplit(None, 1)[-1].lower() if prev else ""
            if day.endswith("e"):
                day = day[:-1] + ("en" if last in _DE_DATIVE_WORDS else "er")
            res = f"{day} {months[mo - 1]}"
        else:  # fr
            try:
                day = "premier" if d == 1 else n2w(d, lang="fr")
            except Exception:  # noqa: BLE001
                return m.group(0)
            res = f"{day} {months[mo - 1]}"
        if yr:
            try:
                res += " " + n2w(int(yr), lang=lang)
            except Exception:  # noqa: BLE001
                res += " " + yr
        return res

    return _DATE_RE.sub(_sub, text)


def _expand_times(text: str, lang: str, n2w) -> str:
    """Clock times 'HH:MM' -> '<h> Uhr <m>' (de) / '<h> heures <m>' (fr) /
    '<h> <m>' (others). Whole hours drop the minute ('22:00' -> 'zwölf Uhr')."""
    if not n2w:
        return text
    conn = _TIME_CONNECTOR.get(lang)

    def _sub(m: "re.Match") -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        try:
            hw = n2w(h, lang=lang)
            mw = n2w(mi, lang=lang) if mi else ""
        except Exception:  # noqa: BLE001
            return m.group(0)
        if conn:
            return f"{hw} {conn} {mw}".strip()
        return f"{hw} {mw}".strip()

    return _TIME_RE.sub(_sub, text)


def _expand_ranges(text: str, lang: str) -> str:
    """A hyphen / en- / em-dash between two integers → spoken range:
    "10-20" → "10 bis 20" (de) / "10 to 20" (en) / "10 à 20" (fr). Runs BEFORE
    the punctuation map turns dashes into commas; skips dash chains (ISO dates).
    Leaves the digits for the later number step to read as words.
    """
    word = _RANGE_SEP.get(lang)
    if not word:
        return text
    return _RANGE_RE.sub(lambda m: f"{m.group(1)} {word} {m.group(2)}", text)


def _expand_symbols(text: str, lang: str) -> str:
    """Spoken math/relation symbols: '=' → 'gleich', '<' → 'kleiner als',
    '>' → 'größer als', an arrow ('→', '->', '=>') → directional 'zu'/'to'/'vers',
    and a tilde before a number ('~10') → 'circa 10'. Multi-character tokens are
    applied longest-first so '->' / '=>' win over the bare '>' / '='.
    """
    table = _SYMBOLS.get(lang)
    if table:
        for sym in sorted(table, key=len, reverse=True):
            text = re.sub(r"\s*" + re.escape(sym) + r"\s*",
                          f" {table[sym]} ", text)
    approx = _APPROX.get(lang)
    if approx:
        text = re.sub(r"~\s*(?=[.,]?\d)", approx + " ", text)
    return text


def _expand_ratios(text: str, lang: str, n2w) -> str:
    """A digit:digit colon left after time-matching is a score/ratio, not a
    clock time → spell both numbers as cardinals so the trailing one isn't later
    mis-read as an ordinal ("2:1" → "zwei zu eins", "16:9" → "sechzehn zu neun").
    """
    sep = _RATIO_SEP.get(lang, " ")
    if not n2w:
        return re.sub(r"(?<=\d)\s*:\s*(?=\d)", sep, text)

    def _sub(m: "re.Match") -> str:
        try:
            a = n2w(int(m.group(1)), lang=lang)
            b = n2w(int(m.group(2)), lang=lang)
        except Exception:  # noqa: BLE001
            return m.group(0)
        return f"{a}{sep}{b}"

    return re.sub(r"(?<!\d)(\d+)\s*:\s*(\d+)(?!\d)", _sub, text)


# ─────────────────────────────────────────────────────────────────────────
# Abbreviations — sprachspezifisch
# "z. B." spells "z", "B" and reads the dots as sentence-ends. Expand the
# common dotted abbreviations to full words. `\s*` between tokens tolerates a
# normal space, a non-breaking space, or none ("z.B."). Matched case-
# insensitively; the trailing dot is consumed.
# ─────────────────────────────────────────────────────────────────────────

_ABBREV: Final[dict[str, dict[str, str]]] = {
    "de": {
        r"i\.\s*d\.\s*R\.": "in der Regel",
        r"u\.\s*v\.\s*m\.": "und vieles mehr",
        r"z\.\s*B\.": "zum Beispiel",
        r"z\.\s*T\.": "zum Teil",
        r"u\.\s*a\.": "unter anderem",
        r"u\.\s*U\.": "unter Umständen",
        r"d\.\s*h\.": "das heißt",
        r"o\.\s*Ä\.": "oder Ähnliches",
        r"\busw\.": "und so weiter",
        r"\bbzw\.": "beziehungsweise",
        r"\bggf\.": "gegebenenfalls",
        r"\bevtl\.": "eventuell",
        r"\bvgl\.": "vergleiche",
        r"\binkl\.": "inklusive",
        r"\bexkl\.": "exklusive",
        r"\bca\.": "circa",
        r"\betc\.": "et cetera",
        r"\bNr\.": "Nummer",
        r"\bMio\.": "Millionen",
        r"\bMrd\.": "Milliarden",
        r"\bProf\.": "Professor",
        r"\bDr\.": "Doktor",
        r"\bTel\.": "Telefon",
        r"\bStr\.": "Straße",
    },
    "en": {
        r"\be\.\s*g\.": "for example",
        r"\bi\.\s*e\.": "that is",
        r"\betc\.": "et cetera",
        r"\bvs\.": "versus",
        r"\bapprox\.": "approximately",
        r"\bincl\.": "including",
        r"\bMrs\.": "Misses",
        r"\bMr\.": "Mister",
        r"\bDr\.": "Doctor",
        r"\bProf\.": "Professor",
    },
    "fr": {
        r"\bp\.\s*ex\.": "par exemple",
        r"\bc\.-?\s*à\.?-?\s*d\.": "c'est-à-dire",
        r"\betc\.": "et cetera",
        r"\bcf\.": "voir",
        r"\benv\.": "environ",
    },
    "it": {
        r"\bp\.\s*es\.": "per esempio",
        r"\becc\.": "eccetera",
        r"\bsig\.": "signor",
        r"\bdott\.": "dottore",
    },
    "es": {
        r"\bp\.\s*ej\.": "por ejemplo",
        r"\betc\.": "etcétera",
        r"\baprox\.": "aproximadamente",
        r"\bSra\.": "señora",
        r"\bSr\.": "señor",
        r"\bnúm\.": "número",
    },
    "pt": {
        r"\bp\.\s*ex\.": "por exemplo",
        r"\betc\.": "etcetera",
        r"\bSra\.": "senhora",
        r"\bSr\.": "senhor",
        r"\bnúm\.": "número",
    },
}

# Compile once. Longest patterns first so multi-token forms (i. d. R.) win
# before any shorter prefix could match.
_ABBREV_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {
    lang: [
        (re.compile(pat, re.IGNORECASE), repl)
        for pat, repl in sorted(mapping.items(), key=lambda kv: -len(kv[0]))
    ]
    for lang, mapping in _ABBREV.items()
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
    # Either a grouped-thousands number (1.000 / 1.250.000 / 1.250,50 — at least
    # one ".NNN" group) or a plain/decimal number (42 / 3.5 / 14,06). The
    # trailing lookahead rejects the grouped alt for separator-less integers so
    # "12345" still matches whole via the second alt.
    r"(?<![A-Za-z0-9.])-?(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)(?![A-Za-z0-9])"
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


# Languages that write '.' as the thousands separator and ',' as the decimal
# point ("1.000,50"). English (and unlisted langs) use the opposite ("1,000.50").
_DOT_THOUSANDS_LANGS: Final = {"de", "fr", "es", "it", "pt"}


def _parse_number(s: str, lang: str) -> float | None:
    """Parse a localized number string to float.

    Locale-aware: de/fr/es/it/pt use ',' as decimal and '.' as the thousands
    separator (so "1.000" is one thousand, not 1.0); English the other way.
    A lone '.' that is NOT a 3-digit grouping (e.g. "3.5") is read as a decimal
    point in every locale — LLMs are inconsistent and a version like "3.5"
    should stay "drei Komma fünf", not become 35.
    """
    s = s.strip().rstrip(".")  # trailing '.' is a sentence end, not a decimal
    if not s:
        return None
    if lang in _DOT_THOUSANDS_LANGS and re.fullmatch(
            r"-?\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):
        s_norm = s.replace(".", "").replace(",", ".")   # 1.250,50 -> 1250.50
    elif lang == "en":
        s_norm = s.replace(",", "")                      # ',' = thousands
    else:
        s_norm = s.replace(",", ".")                     # ',' = decimal
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

    # 1b: Strip emoji / pictographs (the model mumbles over them). Done after the
    # Markdown strip; the spoken symbols (→ < > = ~) are outside the emoji ranges.
    out = _EMOJI_RE.sub("", out)

    # 3b: Expand dotted abbreviations ("z. B." → "zum Beispiel") before the
    # dots get read as sentence-ends or the letters spelled out.
    for pat, repl in _ABBREV_PATTERNS.get(lang, ()):
        out = pat.sub(repl, out)

    # 3b2: Numbered-list enumerators ("1. " at line start) → spoken ordinal
    # ("Erstens, ") — must run while line breaks are still present, i.e. BEFORE
    # _terminate_lines joins the lines.
    out = _expand_list_markers(out, lang)

    # 3c: Terminate paragraphs / list items so they don't run together. Must
    # come AFTER the markdown strip (bullets gone, newlines still present) and
    # BEFORE the whitespace-collapse that would erase the line structure.
    out = _terminate_lines(out)

    # 3d: Numeric ranges "10-20" / "10–20" → "10 bis 20" BEFORE the punctuation
    # map turns en/em-dashes into commas (and before the number step).
    out = _expand_ranges(out, lang)

    # 4: Punctuation / special character map
    for k, v in _PUNCT_MAP.items():
        if k in out:
            out = out.replace(k, v)

    # 4b: A free-standing hyphen used as a dash (spaces on both sides) becomes a
    # comma pause. In-word hyphens (E-Auto) and signed numbers (-5) are spared.
    out = re.sub(r"\s+-\s+", ", ", out)

    # 4c: Dates, clock times and math symbols BEFORE any number/decimal handling
    # — otherwise "14.06" reads as the decimal "vierzehn Komma null sechs",
    # "22:57" keeps its colon, and "=" is dropped.
    _n2w_dt = _try_num2words()
    out = _expand_dates(out, lang, _n2w_dt)
    out = _expand_times(out, lang, _n2w_dt)
    out = _expand_currency(out, lang)
    out = _expand_symbols(out, lang)
    out = _expand_ratios(out, lang, _n2w_dt)

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

    # 8: Tidy punctuation artifacts left by the symbol maps (e.g. "(x)" → ", x, "
    # leaves a dangling comma) and by line-termination meeting existing punct.
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)        # " ," → ","
    out = re.sub(r"[,;:]+(\s*[.!?])", r"\1", out)      # ", ." → "."
    out = re.sub(r"([,;:])[,;:]+", r"\1", out)         # ", ," → ","
    out = re.sub(r"([.!?])[.!?]+", r"\1", out)         # ".." / "?!" → first
    out = re.sub(r"^[\s,;:]+", "", out)                # leading dangling punct
    out = re.sub(r"[\s,;:]+$", ".", out)               # trailing comma → period
    return out.strip()


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
        # dates / times / lists / '=' (and their false-positive guards)
        "Notiz für 14.06:\n1. Party um 22:57.\n2. Eurosatory = Arbeit.",
        "Am 14.06.2026 um 9:05 Uhr.",            # date+year (dative) + time
        "Heute ist der 14.06.",                  # date, nominative
        "Mistral 3.5 und Python 3.13 laufen.",   # versions stay decimals
        "Kostet 1.000 Euro, also 1.250.000 Cent.",  # thousands
        "Wert 14,06 Grad; Pi ist 3.14.",         # comma-decimal + dotted decimal
        "Es steht 2:1, Bild 16:9.",              # scores/ratios, not times
        "2 + 2 = 4 und x = 5.",                  # '=' -> gleich
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
