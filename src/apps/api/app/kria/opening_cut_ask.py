"""Did the creator ask the video to start at a line they quote? (KRI-628)

"Cut the 'Hazırlıklara başladın mı' part and the long pauses; start the video with the
'Abonelerimize bir şey demek ister misin' question" lands in the Creative Brief as a
``select`` requirement with no facts. The Main Creator strategy has no field for it, so
(like `speaker_framing_ask`) the ask is read deterministically off the requirement's own
words by the phone Talking worker (to cut everything before the line,
`app.pipeline.phone_opening_cut`) and by `app.kria.brief_checks` (to answer the
requirement from the render's receipt).

What counts: a quoted line of at least two words that sits right next to a "start" cue --
"start the video at/with “…”", "begin with “…”", "open on “…”", "cut everything before
“…”", "“…” sorusuyla başlasın", "“…” ile başlasın", "“…” öncesini kes". A quote beside no
such cue (the part to cut, a title, a caption word) is never the start line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.kria.brief_route import loose_text
from app.services.spoken_quotes import MIN_QUOTE_TOKENS, fold_tokens, quoted_spans

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# Other kinds (text, order, audio) have their own checkers; a literal title is not speech.
OPENING_CUT_KINDS = frozenset({"select", "timing", "style"})

# A cue and its quote are at most this many words apart ("start the video at “…”",
# "“…” sorusuyla başlasın").
_MAX_GAP_WORDS = 4

_CUE_PATTERNS = (
    # English
    re.compile(r"^(?:start|starts|starting|begin|begins|beginning|kick|kicks)$"),
    re.compile(r"^(?:open|opens|opening)$"),
    # Turkish: başla / başlasın / başlat / başlayalım; açılsın / açılmalı
    re.compile(r"^basla\w*$"),
    re.compile(r"^(?:acilsin|acilmali|acilacak)$"),
)
# "cut everything before “…”", "“…” öncesini kes", "“…” öncesi"
_BEFORE_CUT = re.compile(
    r"\b(?:cut|trim|remove|delete|drop|skip|chop)\b(?:\s+\w+){0,3}?\s+(?:before|until|up\s+to)\s+$"
)
_TR_BEFORE = re.compile(r"^(?:oncesi\w*|onceki\w*)$")
# A quote beside "title"/"caption" is on-screen text, not a line to start the take at.
_ON_SCREEN_WORDS = frozenset(
    {"title", "titles", "caption", "captions", "text", "label", "badge", "baslik", "basligi"}
    | {"altyazi", "yazi", "yazisi", "rozet", "rozeti", "metin", "metni"}
)
_PLACEHOLDER = re.compile(r"^qq(\d+)qq$")


@dataclass(frozen=True)
class OpeningCutAsk:
    """The line the video must start at, and the brief requirements that asked for it."""

    quote: str
    requirement_ids: tuple[str, ...]


def opening_quote(text: str | None) -> str | None:
    """The quoted line ``text`` says the video should start at, else None."""
    raw = str(text or "")
    spans = quoted_spans(raw)
    if not spans:
        return None
    pieces: list[str] = []
    cursor = 0
    for index, (start, end, _quote) in enumerate(spans):
        pieces.append(raw[cursor:start])
        pieces.append(f" qq{index}qq ")
        cursor = end
    pieces.append(raw[cursor:])
    folded = loose_text("".join(pieces))
    words = re.findall(r"qq\d+qq|\w+", folded)
    holders = {
        int(match.group(1)): i
        for i, word in enumerate(words)
        if (match := _PLACEHOLDER.match(word)) is not None
    }
    best: tuple[int, int] | None = None  # (distance, quote index)
    for i, word in enumerate(words):
        if not any(pattern.match(word) for pattern in _CUE_PATTERNS) and not _TR_BEFORE.match(word):
            continue
        for index, position in holders.items():
            gap = abs(position - i) - 1
            if gap > _MAX_GAP_WORDS:
                continue
            between = words[min(i, position) + 1 : max(i, position)]
            before_quote = words[max(0, position - 3) : position]
            if _ON_SCREEN_WORDS.intersection(between) or _ON_SCREEN_WORDS.intersection(
                before_quote
            ):
                continue
            if best is None or (gap, index) < best:
                best = (gap, index)
    for position in holders.values():
        lead = " ".join(words[max(0, position - 6) : position]) + " "
        if _BEFORE_CUT.search(lead):
            index = next(idx for idx, pos in holders.items() if pos == position)
            if best is None or (0, index) < best:
                best = (0, index)
    if best is None:
        return None
    quote = spans[best[1]][2].strip()
    return quote if len(fold_tokens(quote)) >= MIN_QUOTE_TOKENS else None


def requirement_opening_quote(req: BriefRequirement) -> str | None:
    """A live requirement's start line, when its words ask the video to start at one."""
    if not req.live or req.kind not in OPENING_CUT_KINDS:
        return None
    return opening_quote(req.description)


def opening_cut_ask(requirements: Iterable[BriefRequirement]) -> OpeningCutAsk | None:
    """The first start-at-a-line ask among ``requirements`` (every requirement with that
    same line is listed, so each gets its receipt)."""
    found = [
        (req.id, quote)
        for req in requirements
        if (quote := requirement_opening_quote(req)) is not None
    ]
    if not found:
        return None
    quote = found[0][1]
    key = fold_tokens(quote)
    ids = tuple(req_id for req_id, other in found if fold_tokens(other) == key)
    return OpeningCutAsk(quote=quote, requirement_ids=ids)


def brief_opening_cut(brief: CreativeBrief | None) -> OpeningCutAsk | None:
    """`opening_cut_ask` over a brief's live requirements (None without one)."""
    return opening_cut_ask(brief.live()) if brief is not None else None


__all__ = [
    "OPENING_CUT_KINDS",
    "OpeningCutAsk",
    "brief_opening_cut",
    "opening_cut_ask",
    "opening_quote",
    "requirement_opening_quote",
]
