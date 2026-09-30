"""The route a creator states in a brief ("Arnavutköy → Eminönü"): one shared reader.

The unified montage planner writes the route into the title and the receipt checker
compares it with where the first and last clips were actually filmed (KRI-208). Both
must read the same keys, so the key tuples live here and nowhere else.
"""

from __future__ import annotations

import re
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


# KRI-219: per-clip text that is the HOUR each clip was filmed ("add the hour to each
# video"). One reader for the router, the montage planner and the receipt checker, so
# a place name can never quietly stand in for it.
CLIP_TIME_KEYS = frozenset(
    {"capture_time", "time", "hour", "clock", "filming_time", "capture_hour"}
)
# Deliberately narrow: "the time" alone could mean a clip's duration.
_TIME_LABEL_TEXT = re.compile(
    r"\b(hours?|saat\w*|clock|time of day|what time|(filming|capture|shot) times?|"
    r"time\b.{0,20}\b(filmed|shot|recorded|captured)|"
    r"(filmed|shot|recorded|captured)\b.{0,20}\btime)\b"
)


def wants_filming_time_text(
    kind: str,
    scope: str,
    literal: str | None,
    description: str | None,
    facts: Mapping[str, Any] | None,
) -> bool:
    """A described per-clip text requirement whose text is the filming hour."""
    if kind != "text" or scope != "per_clip" or literal:
        return False
    key = str((facts or {}).get("key") or "").casefold()
    if key in CLIP_TIME_KEYS:
        return True
    return bool(_TIME_LABEL_TEXT.search(loose_text(description or "")))
