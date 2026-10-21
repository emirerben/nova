"""The one Turkish-aware text fold and Turkish-aware casing (KRI-781).

Dependency-free on purpose (stdlib only): agents, pipeline, services and ``app.kria``
all import it, and ``app.kria.brief_route`` re-exports the fold under its historical
names (``fold_text`` / ``loose_text``).

FOLD (matching only, never shown): the four Turkish i letters (I, İ, ı, i) all fold to
plain "i" BEFORE casefold. "KIRMIZI", "Kırmızı", "kırmızı" and ASCII-typed "kirmizi"
fold alike; English capitals stay English ("DO IT AGAIN" -> "do it again"); "İ" never
becomes "i" + combining dot; "ı" is never deleted. Do not add private fold tables in
other modules -- wrap these (e.g. split ``loose_text`` into tokens) instead.

CASE (shown to the creator): ``upper`` / ``lower`` / ``capitalize`` / ``title_words``.
Turkish rules (i <-> İ, ı <-> I) apply to the whole text when the language is Turkish
("tr"/"az"); with no language, only to whitespace-delimited words that contain a
Turkish-only letter (ı İ ğ Ğ ş Ş). Every other word is cased exactly as ``str.upper`` /
``str.lower``, so English never changes ("Visiting Beşiktaş" -> "VISITING BEŞİKTAŞ"),
and casing word by word equals casing the whole line.
Mirrors: ``applyTextCase`` in src/apps/web/src/lib/overlay-layout.ts and
``KriaTextCase`` in src/apps/ios/Kria/Core/NativeEditorRenderCompiler.swift
(shared fixture: tests/fixtures/text-element-parity/text_case.json).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache


def fold_case(value: str) -> str:
    """NFC + Turkish-aware case fold, whitespace kept as written (positions/lines)."""
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("\u0130", "i").replace("I", "i").replace("\u0131", "i")
    return text.casefold()


def strip_marks(value: str) -> str:
    """NFKD with combining marks dropped: "ş" -> "s", "ö" -> "o" ("ı" is already "i")."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold_text(value: str) -> str:
    """NFC + Turkish-aware case fold ("KIRMIZI" == "Kırmızı"), whitespace collapsed."""
    return " ".join(fold_case(value).split())


def loose_case(value: str) -> str:
    """``fold_case`` with diacritics stripped, whitespace kept as written."""
    return strip_marks(fold_case(value))


def loose_text(value: str) -> str:
    """``fold_text`` with diacritics stripped too ("Eminönü" == "eminonu").

    Creators type place names without Turkish letters as often as with them; a route
    match must not depend on which they used.
    """
    return strip_marks(fold_text(value))


_WORD_RUN = re.compile(r"[^\W_]+")


def loose_words(value: str) -> list[str]:
    """``loose_text`` split into letter/digit runs of any script, in order."""
    return _WORD_RUN.findall(loose_text(value))


@dataclass(frozen=True)
class Marker:
    """A marker phrase whose ending matters (a plain string allows any ending).

    ``whole_word``: the last word ends there ("asla" never, not "aslan" lion).
    ``not_before``: endings that change the meaning ("her zaman" always, not "her zamanki"
    usual). ``gap``: up to that many words may sit between the first word and the rest
    ("artık (bu fontu) kullanma", Turkish puts the verb last).
    """

    phrase: str
    whole_word: bool = False
    not_before: tuple[str, ...] = ()
    gap: int = 0


@lru_cache(maxsize=2048)
def _marker_pattern(marker: str | Marker) -> re.Pattern[str]:
    spec = marker if isinstance(marker, Marker) else Marker(marker)
    words = loose_text(spec.phrase).split()
    if spec.gap and len(words) > 1:
        between = r"\s+(?:\S+\s+){0," + str(spec.gap) + "}?"
        body = re.escape(words[0]) + between + r"\s+".join(map(re.escape, words[1:]))
    else:
        body = r"\s+".join(map(re.escape, words))
    # Starts at a word boundary; any ending unless the spec pins it (Turkish suffixes:
    # "bu video" in "bu videoda").
    tail = r"(?![^\W_])" if spec.whole_word else ""
    if spec.not_before:
        tail += "(?!" + "|".join(re.escape(loose_text(end)) for end in spec.not_before) + ")"
    return re.compile(r"(?<![^\W_])" + body + tail)


def has_marker(text: str | None, markers: Iterable[str | Marker]) -> bool:
    """True when a marker phrase starts at a word boundary in ``text``, over the shared
    loose fold: "TÜM VİDEOLARIMDA" and ASCII "tum videolarimda" both hit "tüm
    videolarımda", but "asla" never hits inside "başlasın" / "baslasin". Use ``Marker``
    when an ending changes the meaning ("aslan", "her zamanki")."""
    if not text:
        return False
    hay = loose_text(text)
    return any(_marker_pattern(marker).search(hay) for marker in markers)


# ── Turkish-aware casing ─────────────────────────────────────────────────────

_TURKISH_LANGUAGES = ("tr", "az")
# Letters only Turkish (and Azerbaijani) use; their presence settles the casing rules.
_TURKISH_ONLY_LETTERS = frozenset("ıİğĞşŞ")


def _is_turkish_language(language: str | None) -> bool:
    code = (language or "").strip().lower().replace("_", "-")
    return code.split("-", 1)[0] in _TURKISH_LANGUAGES


def _has_turkish_letter(text: str) -> bool:
    return any(ch in _TURKISH_ONLY_LETTERS for ch in text or "")


def uses_turkish_case(text: str, language: str | None = None) -> bool:
    """True when the language is Turkish or ``text`` has a Turkish-only letter anywhere.

    Matching helpers use this whole-string answer; the casing below decides per word
    when no language is given (see ``upper``).
    """
    return _is_turkish_language(language) or _has_turkish_letter(text)


def _tr_upper(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return text.replace("i", "\u0130").replace("\u0131", "I").upper()


def _tr_lower(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return text.replace("\u0130", "i").replace("I", "\u0131").lower()


_NON_SPACE_RUN = re.compile(r"\S+")


def _per_word(text: str, language: str | None, turkish, english) -> str:
    """Turkish rules for the whole text when the language is Turkish; otherwise only for
    whitespace-delimited words that carry a Turkish-only letter, so an English caption
    naming "Beşiktaş" keeps its English i's and per-word casing (karaoke word timings)
    always equals whole-text casing."""
    if _is_turkish_language(language):
        return turkish(text)
    if not _has_turkish_letter(text):
        return english(text)
    return _NON_SPACE_RUN.sub(
        lambda m: turkish(m.group(0)) if _has_turkish_letter(m.group(0)) else english(m.group(0)),
        text,
    )


def upper(text: str, language: str | None = None) -> str:
    """``str.upper`` with Turkish i rules for Turkish words: "ışıklı" -> "IŞIKLI";
    with ``language="tr"`` also "istanbul" -> "İSTANBUL"."""
    return _per_word(text, language, _tr_upper, str.upper)


def lower(text: str, language: str | None = None) -> str:
    """``str.lower`` with Turkish i rules for Turkish words: "IŞIK" -> "ışık"."""
    return _per_word(text, language, _tr_lower, str.lower)


def _turkish_word(word: str, language: str | None) -> str | None:
    return "tr" if _is_turkish_language(language) or _has_turkish_letter(word) else None


def capitalize(text: str, language: str | None = None) -> str:
    """First character upper-cased, the rest kept as written: "ırmak" -> "Irmak"."""
    if not text:
        return text
    first = _NON_SPACE_RUN.search(text)
    lang = _turkish_word(first.group(0) if first else text, language)
    return upper(text[:1], lang) + text[1:]


def title_words(text: str, language: str | None = None) -> str:
    """First character of each whitespace-delimited run upper, the rest lower.

    Deliberately not ``str.title()``, which capitalizes after apostrophes
    ("Emir'in" -> "Emir'In", "don't" -> "Don'T") and uses English i rules.
    """
    if not text:
        return text

    def _word(m: re.Match[str]) -> str:
        word = m.group(0)
        lang = _turkish_word(word, language)
        return upper(word[:1], lang) + lower(word[1:], lang)

    return _NON_SPACE_RUN.sub(_word, text)


def title_like_str(text: str) -> str:
    """``str.title()`` for text with no Turkish-only letter (English output unchanged:
    "winston-salem" -> "Winston-Salem"); ``title_words`` otherwise, so Turkish words keep
    İ/ı and no capital lands after an apostrophe ("kadıköy'de" -> "Kadıköy'de")."""
    return title_words(text) if _has_turkish_letter(text) else text.title()
