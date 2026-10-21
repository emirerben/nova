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
Turkish rules (i <-> İ, ı <-> I) apply when the language is Turkish ("tr"/"az") or
the text itself contains a Turkish-only letter (ı İ ğ Ğ ş Ş); otherwise the result is
exactly ``str.upper`` / ``str.lower``, so English never changes ("title" -> "TITLE").
Mirrors: ``applyTextCase`` in src/apps/web/src/lib/overlay-layout.ts and
``KriaTextCase`` in src/apps/ios/Kria/Core/NativeEditorRenderCompiler.swift
(shared fixture: tests/fixtures/text-element-parity/text_case.json).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
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


@lru_cache(maxsize=2048)
def _marker_pattern(marker: str) -> re.Pattern[str]:
    # Starts at a word boundary; any ending (Turkish suffixes: "bu video" in "bu videoda").
    return re.compile(r"(?<![^\W_])" + re.escape(loose_text(marker)))


def has_marker(text: str | None, markers: Iterable[str]) -> bool:
    """True when a marker phrase starts at a word boundary in ``text``, over the shared
    loose fold: "TÜM VİDEOLARIMDA" and ASCII "tum videolarimda" both hit "tüm
    videolarımda", but "asla" never hits inside "başlasın" / "baslasin"."""
    if not text:
        return False
    hay = loose_text(text)
    return any(_marker_pattern(marker).search(hay) for marker in markers)


# ── Turkish-aware casing ─────────────────────────────────────────────────────

_TURKISH_LANGUAGES = ("tr", "az")
# Letters only Turkish (and Azerbaijani) use; their presence settles the casing rules.
_TURKISH_ONLY_LETTERS = frozenset("ıİğĞşŞ")


def uses_turkish_case(text: str, language: str | None = None) -> bool:
    """True when ``text`` must be cased with Turkish i rules."""
    code = (language or "").strip().lower().replace("_", "-")
    if code.split("-", 1)[0] in _TURKISH_LANGUAGES:
        return True
    return any(ch in _TURKISH_ONLY_LETTERS for ch in text or "")


def upper(text: str, language: str | None = None) -> str:
    """``str.upper`` with Turkish i rules when Turkish: "istanbul" -> "İSTANBUL"."""
    if not uses_turkish_case(text, language):
        return text.upper()
    text = unicodedata.normalize("NFC", text)
    return text.replace("i", "\u0130").replace("\u0131", "I").upper()


def lower(text: str, language: str | None = None) -> str:
    """``str.lower`` with Turkish i rules when Turkish: "IŞIK" -> "ışık"."""
    if not uses_turkish_case(text, language):
        return text.lower()
    text = unicodedata.normalize("NFC", text)
    return text.replace("\u0130", "i").replace("I", "\u0131").lower()


def capitalize(text: str, language: str | None = None) -> str:
    """First character upper-cased, the rest kept as written: "ırmak" -> "Irmak"."""
    if not text:
        return text
    lang = "tr" if uses_turkish_case(text, language) else None
    return upper(text[:1], lang) + text[1:]


_NON_SPACE_RUN = re.compile(r"\S+")


def title_words(text: str, language: str | None = None) -> str:
    """First character of each whitespace-delimited run upper, the rest lower.

    Deliberately not ``str.title()``, which capitalizes after apostrophes
    ("Emir'in" -> "Emir'In", "don't" -> "Don'T") and uses English i rules.
    """
    if not text:
        return text
    lang = "tr" if uses_turkish_case(text, language) else None
    return _NON_SPACE_RUN.sub(
        lambda m: upper(m.group(0)[:1], lang) + lower(m.group(0)[1:], lang), text
    )
