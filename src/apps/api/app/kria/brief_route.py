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


# "just the hour, not the minutes": the filming-time label format the creator wants.
_HOUR_ONLY_TEXT = re.compile(
    r"\b(just|only|sadece|yalnizca)\b.{0,25}\b(hours?|saat\w*)\b"
    r"|\bafter the hours?\b"
    r"|\b(don.?t|do not|dont|not|no|without|exclude|omit|hide|remove|olmadan|gosterme)\b"
    r".{0,30}\b(minutes?|dakika\w*)\b"
)


def wants_hour_only_text(description: str | None, literal: str | None = None) -> bool:
    """True when the wording asks for the hour WITHOUT the minutes."""
    return bool(_HOUR_ONLY_TEXT.search(loose_text(f"{description or ''} {literal or ''}")))


# KRI-545: "Bölüm başlıkları koy: Sabah, Üniversite, Öğle arası, Spor, Akşam". The brief keeps
# the creator's whole list as ONE text literal while each name is printed on its own chapter's
# clips. One reader for the montage planner (no opening title from it), the receipt checker and
# the phone recipe verifier (the names on the clips are that text), so the three cannot disagree.
# Whitespace alone never separates names: "Hello world" is not a list of "Hello" and "world".
_LIST_GAP = re.compile(r"\s*(?:[,;/|·•→&+–—]|->|\s-\s)\s*|\s+(?:and|ve|then|sonra)\s+")
_LIST_END = re.compile(r"[\s.!…]*")
_LIST_QUOTES = str.maketrans("", "", "\"'“”‘’«»")


def chapter_list(literal: str | None, texts: Iterable[str]) -> tuple[str, ...] | None:
    """The ``texts`` that ``literal`` lists, in its order, when it is nothing but that list.

    ``texts`` are whole on-screen strings (the labels on the clips). The literal is a list of
    them when it is two or more different ones joined only by list separators (comma,
    semicolon, slash, arrow, dash, "and" / "ve" / "then" / "sonra"), compared with the
    Turkish-aware fold. Returns the matched texts as written in ``texts``; None for anything
    else (a single name, a word that is not one of them, or names run together by spaces).
    """
    known: dict[str, str] = {}
    for text in texts:
        key = fold_text(str(text or "").translate(_LIST_QUOTES))
        if key:
            known.setdefault(key, str(text))
    wanted = fold_text(str(literal or "").replace("\n", ",").translate(_LIST_QUOTES))
    if len(known) < 2 or not wanted:
        return None
    keys = sorted(known, key=len, reverse=True)  # "Öğle arası" before "Öğle"
    memo: dict[int, tuple[str, ...] | None] = {}

    def walk(start: int) -> tuple[str, ...] | None:
        if start not in memo:
            memo[start] = None
            for key in keys:
                if not wanted.startswith(key, start):
                    continue
                end = start + len(key)
                if _LIST_END.fullmatch(wanted, end):
                    memo[start] = (known[key],)
                    break
                gap = _LIST_GAP.match(wanted, end)
                rest = walk(gap.end()) if gap and gap.end() < len(wanted) else None
                if rest is not None:
                    memo[start] = (known[key], *rest)
                    break
        return memo[start]

    parts = walk(0)
    return parts if parts is not None and len(set(parts)) >= 2 else None
