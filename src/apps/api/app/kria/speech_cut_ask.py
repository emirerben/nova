"""Read a narrowly scoped request to remove one spoken phrase occurrence.

Structured callers may save ``facts.speech_remove``.  The small legacy reader exists
for approved briefs that lack it, and deliberately refuses anything it cannot bind to
one quoted phrase and, where named, one ordinal occurrence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.kria.brief_route import loose_text
from app.pipeline.explicit_speech_cut import speech_cut_tokens


@dataclass(frozen=True)
class SpeechCutAsk:
    quote: str
    occurrence: int | None
    requirement_ids: tuple[str, ...] = ()


_REMOVE = re.compile(r"\b(?:cut|remove|delete|trim|excise|drop|kes\w*|sil\w*|cikar\w*|kaldir\w*)\b")
_NEGATED = re.compile(
    r"\b(?:don't|dont|do not|never|not|kesme\w*|silme\w*|cikarma\w*|kaldirma\w*)\b"
    r".{0,28}\b(?:cut|remove|delete|trim|excise|drop|kes\w*|sil\w*|cikar\w*|kaldir\w*)\b"
    r"|\b(?:cut|remove|delete|trim|excise|drop)\b.{0,16}\b(?:not|never)\b"
)
_NEGATED_REMOVE = re.compile(r"\b(?:kesme\w*|silme\w*|cikarma\w*|kaldirma\w*)\b")
_CAPTION_ONLY = re.compile(
    r"\b(?:captions?|subtitles?|title|titles|text|overlay|label|altyazi\w*|basli[kg]\w*|yazi\w*|metin\w*)\b"
)
_BROAD = re.compile(r"\b(?:all|every|entire|whole|any|hepsi|tum(?:unu|u)?|butun(?:unu|u)?)\b")
_UNCERTAIN = re.compile(r"\b(?:maybe|perhaps|if possible|sanirim|belki)\b|\?")
_ORDINALS = (
    (1, re.compile(r"\b(?:first|1st|birinci|ilk)\b")),
    (2, re.compile(r"\b(?:second|2nd|ikinci)\b")),
    (3, re.compile(r"\b(?:third|3rd|ucuncu)\b")),
    (4, re.compile(r"\b(?:fourth|4th|dorduncu)\b")),
    (5, re.compile(r"\b(?:fifth|5th|besinci)\b")),
)
_UNSUPPORTED_ORDINAL = re.compile(
    r"\b(?:[6-9](?:st|nd|rd|th)|[1-9]\d+(?:st|nd|rd|th)|sixth|seventh|eighth|ninth|tenth|"
    r"altinci|yedinci|sekizinci|dokuzuncu|onuncu|sonuncu)\b"
)
_TAKE_WINDOW_CUE = re.compile(
    r"\b(?:start|begin|open|ending?|end|before|after|until|from|at)\b"
    r"|\b(?:basla\w*|acil\w*|bit\w*|oncesi\w*|sonrasi\w*|itibaren)\b"
)
_QUOTED = re.compile(
    r'"([^"\n]{1,200})"'
    r"|“([^”\n]{1,200})”"
    r"|„([^“”\n]{1,200})[“”]"
    r"|«([^»\n]{1,200})»"
    r"|‘([^’\n]{1,200})’"
    r"|(?<![\w'])'((?:[^'\n]|'(?=[^\W\d_]))+?)'(?!\w)"
)


def _clean_quote(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    quote = " ".join(value.split())
    return quote if quote else None


def _quoted_spans(text: str) -> list[tuple[int, int, str]]:
    """Quote parsing stays in this leaf; importing speech grounding loads phone rendering."""
    return [
        (match.start(), match.end(), phrase)
        for match in _QUOTED.finditer(text)
        if (phrase := _clean_quote(next(group for group in match.groups() if group is not None)))
        is not None
    ]


def _fact_ask(req: object) -> tuple[bool, SpeechCutAsk | None]:
    facts = getattr(req, "facts", None)
    if not isinstance(facts, Mapping) or "speech_remove" not in facts:
        return False, None
    raw = facts.get("speech_remove") if isinstance(facts, Mapping) else None
    if not isinstance(raw, Mapping) or set(raw) != {"quote", "occurrence"}:
        return True, None
    quote = _clean_quote(raw.get("quote"))
    occurrence = raw.get("occurrence")
    if (
        quote is None
        or not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 1
    ):
        return True, None
    return True, SpeechCutAsk(quote=quote, occurrence=occurrence)


def _outside_quotes(description: str, quotes: list[tuple[int, int, str]]) -> str:
    """Do not read ordinal/uncertainty words inside the phrase being removed as instructions."""
    pieces: list[str] = []
    cursor = 0
    for start, end, _quote in quotes:
        pieces.extend((description[cursor:start], " quoted phrase "))
        cursor = end
    pieces.append(description[cursor:])
    return loose_text("".join(pieces))


def _legacy_ask(req: object) -> SpeechCutAsk | None:
    if getattr(req, "kind", None) not in {"timing", "select"}:
        return None
    description = str(getattr(req, "description", None) or "")
    quotes = _quoted_spans(description)
    text = _outside_quotes(description, quotes)
    if len(quotes) != 1 or not _REMOVE.search(text):
        return None
    # A cut that merely gets to a quoted opening/end line is a take-window request, not a
    # request to delete that line.  This must run before the legacy removal fallback.
    facts = getattr(req, "facts", None)
    if (
        isinstance(facts, Mapping)
        and any(
            isinstance(facts.get(key), str) and facts[key].strip()
            for key in ("start", "end", "start_line", "end_line")
        )
    ) or _TAKE_WINDOW_CUE.search(text):
        return None
    if (
        _NEGATED.search(text)
        or _NEGATED_REMOVE.search(text)
        or _CAPTION_ONLY.search(text)
        or _BROAD.search(text)
        or _UNCERTAIN.search(text)
        or _UNSUPPORTED_ORDINAL.search(text)
    ):
        return None
    found = [number for number, pattern in _ORDINALS if pattern.search(text)]
    if len(found) > 1:
        return None
    return SpeechCutAsk(quote=quotes[0][2], occurrence=found[0] if found else None)


def requirement_speech_cut(req: object) -> SpeechCutAsk | None:
    """One safe explicit phrase-removal ask, or ``None``.

    An unnumbered quote is intentionally allowed.  The worker may apply it only if its
    source resolver finds exactly one occurrence; this reader never chooses one itself.
    """
    if (
        not getattr(req, "live", True)
        or getattr(req, "kind", None) not in {"timing", "select"}
        or getattr(req, "scope", "global") in {"title", "per_clip"}
    ):
        return None
    has_fact, fact = _fact_ask(req)
    return fact if has_fact else _legacy_ask(req)


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
