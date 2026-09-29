"""The route a creator states in a brief ("Arnavutköy → Eminönü"): one shared reader.

The unified montage planner writes the route into the title and the receipt checker
compares it with where the first and last clips were actually filmed (KRI-208). Both
must read the same keys, so the key tuples live here and nowhere else.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


def fold_text(value: str) -> str:
    """NFC + Turkish-aware case fold ("KIRMIZI" == "Kırmızı"), whitespace collapsed."""
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u0130", "i").replace("I", "i").replace("\u0131", "i")
    return " ".join(text.casefold().split())


def loose_text(value: str) -> str:
    """``fold_text`` with diacritics stripped too ("Eminönü" == "eminonu").

    Creators type place names without Turkish letters as often as with them; a route
    match must not depend on which they used.
    """
    decomposed = unicodedata.normalize("NFKD", fold_text(value))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


START_KEYS = ("start", "from", "origin")
END_KEYS = ("end", "to", "destination", "finish")


def first_text(facts: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    """The first non-empty string/number among ``facts[key]`` for ``keys``, NFC-stripped."""
    for key in keys:
        value = facts.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = unicodedata.normalize("NFC", str(value or "")).strip()
            if text:
                return text
    return None


def route_endpoints(facts: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(start, end) the creator stated, or None for a side they did not."""
    return first_text(facts, START_KEYS), first_text(facts, END_KEYS)
