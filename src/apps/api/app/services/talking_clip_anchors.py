"""S3 Yorum2 (2026-11-08): a Talking edit's stated first / last take, matched by the server.

A Talking edit plans no clip intents (`CLIP_INTENT_FREE_EDIT_FORMATS`), so nothing resolved
the brief's ``first_clip`` / ``last_clip`` and the render contract refused every wording the
creator tried (prod thread cc0e3706): "20-second home clip", "the clip that starts “Wait, the
light”", "the car clip". The edit's clip rows already carry what tells takes apart: their
length (``duration_s``), their words (the analysis transcripts) and where they were filmed
(clip understanding's ``setting``). Every cue the words hold must single out the same one
take; no cue, no take or two takes seats nothing (the contract's "which clip" reply stands).

Pure functions over snapshot rows: no model, no settings, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.clip_order_sequence import SequenceRow
from app.services.spoken_quotes import fold_tokens, quoted_spans, transcript_says

# "20-second", "7 sec", "20s", "20 saniyelik", "7 sn".
_SECONDS_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*-?\s*(?:seconds?|secs?|s|sn|saniye(?:lik)?)\b", re.IGNORECASE
)
# A stated length names a take within a second, or within 15% of a long one.
_SECONDS_SLACK_S = 1.0
_SECONDS_SLACK_SHARE = 0.15
# Place words the creator says (folded) -> words a take's ``setting`` says for that place.
_PLACES: dict[frozenset[str], frozenset[str]] = {
    frozenset({"car", "araba", "arabada", "arac", "aracta"}): frozenset(
        {"car", "vehicle", "araba", "arac"}
    ),
    frozenset({"home", "house", "ev", "evde"}): frozenset(
        {"home", "house", "living", "bedroom", "kitchen", "apartment", "ev", "salon"}
    ),
    frozenset({"office", "ofis", "ofiste"}): frozenset({"office", "ofis"}),
    frozenset({"outside", "outdoors", "street", "disarida", "sokakta"}): frozenset(
        {"outdoor", "outdoors", "outside", "street", "sidewalk", "park"}
    ),
}


def _analysis(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("analysis")
    return value if isinstance(value, Mapping) else {}


def _duration_s(row: Mapping[str, Any]) -> float | None:
    for value in (row.get("duration_s"), _analysis(row).get("duration_s")):
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def _setting(row: Mapping[str, Any]) -> set[str]:
    understanding = _analysis(row).get("understanding")
    setting = understanding.get("setting") if isinstance(understanding, Mapping) else None
    return set(fold_tokens(setting)) if isinstance(setting, str) else set()


def _unquoted(words: str) -> str:
    """``words`` without what they quote: a quote is what a take says, not where it was."""
    out, at = [], 0
    for start, end, _phrase in quoted_spans(words):
        out.append(words[at:start])
        at = end
    return " ".join([*out, words[at:]])


def _by_length(words: str, rows: Sequence[Mapping[str, Any]]) -> set[str] | None:
    found = _SECONDS_RE.search(_unquoted(words))
    if found is None:
        return None
    wanted = float(found.group(1).replace(",", "."))
    slack = max(_SECONDS_SLACK_S, wanted * _SECONDS_SLACK_SHARE)
    return {
        str(row["media_id"])
        for row in rows
        if (length := _duration_s(row)) is not None and abs(length - wanted) <= slack
    }


def _by_words(words: str, rows: Sequence[Mapping[str, Any]]) -> set[str] | None:
    quotes = [phrase for _start, _end, phrase in quoted_spans(words)]
    if not quotes:
        return None
    return {
        str(row["media_id"])
        for row in rows
        if all(transcript_says(_analysis(row), quote) for quote in quotes)
    }


def _by_place(words: str, rows: Sequence[Mapping[str, Any]]) -> set[str] | None:
    said = set(fold_tokens(_unquoted(words)))
    places = [setting for cues, setting in _PLACES.items() if said & cues]
    if len(places) != 1:
        return None  # no place, or two ("from home to the car"), is no cue
    return {str(row["media_id"]) for row in rows if _setting(row) & places[0]}


# KRI-1340 (S3 Yorum1): "two at home and one in the car" names a GROUP of takes by how many
# there are. A stated count seats a group only when exactly that many takes match.
_COUNTS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "bir": 1, "iki": 2, "uc": 3, "dort": 4, "bes": 5,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
}  # fmt: skip


def _stated_count(words: str) -> int | None:
    """The one count word ``words`` state outside quotes ("two at home"), else None."""
    free = _SECONDS_RE.sub(" ", _unquoted(words))  # "the 5-second clip" states no count
    counts = {_COUNTS[t] for t in fold_tokens(free) if t in _COUNTS}
    return next(iter(counts)) if len(counts) == 1 else None


def _matched(words: str, rows: Sequence[Mapping[str, Any]]) -> set[str] | None:
    takes = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("media_id") and row.get("kind", "video") == "video"
    ]
    matched: set[str] | None = None
    for cue in (_by_length, _by_words, _by_place):
        found = cue(words, takes)
        if found is not None:
            matched = found if matched is None else matched & found
    return matched


def talking_clip(words: str, rows: Sequence[Mapping[str, Any]]) -> str | None:
    """The one take ``words`` name by length, quoted words and place, else None."""
    matched = _matched(words, rows)
    return next(iter(matched)) if matched is not None and len(matched) == 1 else None


def talking_clips(words: str, rows: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """The takes ``words`` name: one take (`talking_clip`), or a group whose stated count is
    exactly how many takes match ("two at home" with two home takes), in row order. None when
    nothing, or a different number of takes than stated, matches."""
    matched = _matched(words, rows)
    if not matched:
        return None
    count = _stated_count(words)
    if len(matched) == 1 and count in (None, 1):
        return list(matched)
    if count is None or count != len(matched):
        return None
    return [str(row["media_id"]) for row in rows if str(row.get("media_id")) in matched]


def talking_anchor_rows(
    anchors: Mapping[str, str], rows: Sequence[Mapping[str, Any]]
) -> list[SequenceRow]:
    """The brief's first / last take (or counted group, KRI-1340) seated as sequence rows,
    each one only when matched.

    ``rows`` are the edit's clip rows (`creator_render_contract._edit_rows`). Two ends naming
    the same take seat the first end alone: the other stays unplaced, so the contract still
    asks about it by name."""
    out: list[SequenceRow] = []
    taken: set[str] = set()
    for position in ("first", "last"):
        words = anchors.get(position)
        members = talking_clips(words, rows) if words else None
        if not members or taken & set(members):
            continue
        taken.update(members)
        out.append((position, words, members, "resolved"))
    return out
