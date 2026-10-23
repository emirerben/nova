"""Read a narrowly scoped request to remove one spoken phrase occurrence.

The model that reads the creator's message records it as ``facts.speech_remove``
(``{"quote": "Neredesin", "occurrence": 2}``, Main Creator v75 / Brief Extractor v21). This
module only checks that structured field (KRI-667): it never guesses a cut, an ordinal or a
negation from the description's words, so a brief stored before the field existed authorizes
no source cut.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.pipeline.explicit_speech_cut import speech_cut_tokens

# A spoken-cut row: the brief writer files a named cut as `timing`, `select` or `audio`
# (KRI-792's `cut_lines` rows are `audio`). Text rows never authorize a source cut.
_SPEECH_CUT_KINDS = frozenset({"timing", "select", "audio"})


@dataclass(frozen=True)
class SpeechCutAsk:
    quote: str
    occurrence: int | None
    requirement_ids: tuple[str, ...] = ()


def _clean_quote(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    quote = " ".join(value.split())
    return quote if quote else None


def _fact_ask(req: object) -> SpeechCutAsk | None:
    facts = getattr(req, "facts", None)
    raw = facts.get("speech_remove") if isinstance(facts, Mapping) else None
    if not isinstance(raw, Mapping) or set(raw) != {"quote", "occurrence"}:
        return None
    quote = _clean_quote(raw.get("quote"))
    occurrence = raw.get("occurrence")
    if (
        quote is None
        or not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 1
    ):
        return None
    return SpeechCutAsk(quote=quote, occurrence=occurrence)


def requirement_speech_cut(req: object) -> SpeechCutAsk | None:
    """One safe explicit phrase-removal ask from ``facts.speech_remove``, or ``None``.

    A malformed fact is ``None`` too: a destructive source edit needs the exact quote and
    the 1-based occurrence the model recorded, never a fallback to the words.
    """
    if (
        not getattr(req, "live", True)
        or getattr(req, "kind", None) not in _SPEECH_CUT_KINDS
        or getattr(req, "scope", "global") in {"title", "per_clip"}
    ):
        return None
    return _fact_ask(req)


def speech_cut_asks(requirements: Iterable[object]) -> tuple[SpeechCutAsk, ...]:
    """Group equivalent live asks so one applied receipt satisfies every duplicate row."""
    grouped: dict[tuple[tuple[str, ...], int | None], list[str]] = {}
    display: dict[tuple[tuple[str, ...], int | None], str] = {}
    for req in requirements:
        ask = requirement_speech_cut(req)
        req_id = getattr(req, "id", None)
        if ask is None or not isinstance(req_id, str):
            continue
        # This key authorizes destructive source edits, so it must retain accents and digits.
        key = (tuple(speech_cut_tokens(ask.quote)), ask.occurrence)
        grouped.setdefault(key, []).append(req_id)
        display.setdefault(key, ask.quote)
    return tuple(SpeechCutAsk(display[key], key[1], tuple(ids)) for key, ids in grouped.items())


def brief_speech_cuts(brief: object | None) -> tuple[SpeechCutAsk, ...]:
    """The explicit phrase removals still live in a Creative Brief."""
    live = getattr(brief, "live", None)
    return speech_cut_asks(live()) if callable(live) else ()
