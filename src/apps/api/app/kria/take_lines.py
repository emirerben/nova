"""The lines a take says, for the "which line?" question (KRI-844).

"Open straight on "Here we have traditional Argentinian sauces"" over a take Lucas speaks in
Spanish finds nothing: `ground_line` matches same-language words only. Rather than ship the
whole take, the creator is asked to pick the line of THEIR take to open / end on
(`app.services.choice_questions`, ``take_line_start`` / ``take_line_end``). This module is the
pure half of that question: the take's timed speech segments cut into short spoken lines, and
the handful of them worth showing.

A segment is a whole sentence-ish run (`speech_segments.words_to_segments`), often several
lines glued together: whisper leaves no punctuation after a lone name or a final "Gracias",
so "Lucas Acá tenemos salsas tradicionales argentinas, salsas criollas, chivichurros y
sardines Gracias" is ONE segment. A line therefore ends at clause punctuation, and in a
segment the ASR left unpunctuated (no full stop at its end) it also starts before a
capitalised word (whisper capitalises sentence starts). Well-punctuated text keeps its names
whole ("Barcelona at La Boqueria."). Over-splitting is the safe side: the first piece of a
sentence opens where the sentence does, the last one ends where it ends. A line is the
speaker's own text, so grounding it in the take's words is an exact, same-language match --
but only at its FIRST occurrence at / after the start, so the text a pick is stored as is
widened with its neighbours until it names one place (`pinned_line_text`).

No times are shown to the creator: inside a merged segment a line's time is an estimate by
character share (`opening_cut_ask.segment_words`), good enough to order lines and to bound
"lines after the start", not to print.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.kria.opening_cut_ask import segment_words
from app.services.spoken_quotes import fold_tokens

# A clause ends on one of these (after any closing quote / bracket)...
_CLAUSE_END_CHARS = ".,;:?!…"
_CLAUSE_END = tuple(_CLAUSE_END_CHARS)
_CLOSERS = "\"'”’»)]}"
# A segment ending like this is punctuated: its names are not sentence starts.
_SENTENCE_END = (".", "?", "!", "…")
# ...and a Spanish question / exclamation opens a new one.
_OPENERS = ("¿", "¡")
# English "I" forms are capitalised mid-sentence ("so I think I can"): never a sentence start.
_OPENING_PUNCTUATION = "\"'“‘([«"
_I_FORMS = frozenset({"I", "I'm", "I'll", "I've", "I'd"})

# A line is "clearly" the best match for a quote when it scores at least this much and at
# least twice the runner-up (character-trigram Dice; cognates like "traditional" /
# "tradicionales" or a shared name or number are what it catches).
RECOMMEND_MIN_SCORE = 0.2
RECOMMEND_LEAD = 2.0


@dataclass(frozen=True)
class TakeLine:
    """One spoken line of the take, in order. ``start_s`` / ``end_s`` are estimates (see the
    module docstring), only ever compared with each other."""

    index: int
    text: str
    start_s: float
    end_s: float
    # The line's first / last word, as positions in `take_words(segments)`.
    first: int = 0
    last: int = 0


@dataclass(frozen=True)
class LineOffer:
    """The lines to show for a quote (time order) and the one clearly best match, if any."""

    lines: tuple[TakeLine, ...]
    recommended: TakeLine | None


def _starts_clause(token: str) -> bool:
    """Does ``token`` open a new spoken line mid-segment: a ``¿`` / ``¡``, or a Title-case word."""
    if token.startswith(_OPENERS):
        return True
    bare = token.lstrip(_OPENING_PUNCTUATION)
    word = bare.replace("’", "'").rstrip(_CLAUSE_END_CHARS + _CLOSERS)
    if not bare or not bare[0].isalpha() or not bare[0].isupper() or word in _I_FORMS:
        return False
    letters = "".join(ch for ch in bare if ch.isalpha())
    return not (
        len(letters) > 1 and letters.isupper()
    )  # an acronym ("NASA") is not a sentence start


def _ends_clause(token: str) -> bool:
    return token.rstrip(_CLOSERS).endswith(_CLAUSE_END)


def split_clauses(text: str) -> list[str]:
    """``text`` as spoken lines: cut after clause punctuation, before a ``¿`` / ``¡``, and --
    only in a run that does not end with sentence punctuation -- before a Title-case word."""
    raw = str(text or "")
    unpunctuated = not raw.rstrip().rstrip(_CLOSERS).endswith(_SENTENCE_END)
    clauses: list[str] = []
    current: list[str] = []
    for token in raw.split():
        if current and (token.startswith(_OPENERS) or (unpunctuated and _starts_clause(token))):
            clauses.append(" ".join(current))
            current = []
        current.append(token)
        if _ends_clause(token):
            clauses.append(" ".join(current))
            current = []
    if current:
        clauses.append(" ".join(current))
    return clauses


def _timed_words(segments: Iterable[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], list]]:
    """Each usable segment with its words (an untimed or zero-length segment has nothing to
    ground in)."""
    rows = []
    for segment in segments:
        words = segment_words([segment])
        if words and len(words) == len(str(segment.get("text") or "").split()):
            rows.append((segment, words))
    return rows


def take_words(segments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The words every `TakeLine.first` / ``last`` points into: the usable segments' words
    (`opening_cut_ask.segment_words`), segment after segment."""
    return [word for _segment, words in _timed_words(segments) for word in words]


def take_lines(
    segments: Iterable[Mapping[str, Any]], *, keep: Literal["first", "last"] = "first"
) -> list[TakeLine]:
    """The take's spoken lines in order, each once.

    A line said twice word for word is kept at one place only: its first (a start line is
    grounded at the first match) or, for ``keep="last"``, its last (the end line a creator
    means by "Gracias" is the one they end on).
    """
    found: list[tuple[str, TakeLine]] = []
    offset = 0
    for segment, words in _timed_words(segments):
        cursor = 0
        for clause in split_clauses(str(segment.get("text") or "")):
            count = len(clause.split())
            first, last = words[cursor], words[cursor + count - 1]
            key = " ".join(fold_tokens(clause))
            if key:
                found.append(
                    (
                        key,
                        TakeLine(
                            index=0,
                            text=clause,
                            start_s=float(first["start_s"]),
                            end_s=float(last["end_s"]),
                            first=offset + cursor,
                            last=offset + cursor + count - 1,
                        ),
                    )
                )
            cursor += count
        offset += len(words)
    chosen = {key: i for i, (key, _line) in enumerate(found)} if keep == "last" else {}
    if keep != "last":
        for i, (key, _line) in enumerate(found):
            chosen.setdefault(key, i)
    kept = [line for i, (key, line) in enumerate(found) if chosen[key] == i]
    return [replace(line, index=position) for position, line in enumerate(kept)]


def pinned_line_text(
    words: Sequence[Mapping[str, Any]],
    line: TakeLine,
    *,
    side: Literal["start", "end"],
    count: Callable[[str], int],
    max_chars: int,
) -> str:
    """The text a pick of ``line`` is stored as: one place in the take, still this line.

    Grounding finds a text at its FIRST occurrence at / after the start, so "Gracias" would
    land inside an earlier "Y muchas gracias a todos por venir." Words are added until
    ``count(text)`` (how often the text is said where the cut will look) is 1: an END pick
    takes the words before it (its last word stays the end of the match); a START pick the
    words after it (its first word stays the start). A line over ``max_chars`` keeps its head
    (start) or its tail (end) and is not widened; a text that cannot be made unique inside
    the bound (or that the count cannot find at all) is stored as far as it got.
    """
    texts = [str(word.get("text") or "") for word in words]
    lo, hi = line.first, line.last

    def join(a: int, b: int) -> str:
        return " ".join(texts[a : b + 1])

    while len(join(lo, hi)) > max_chars and hi > lo:
        if side == "start":
            hi -= 1
        else:
            lo += 1
    text = join(lo, hi)[:max_chars]
    while count(text) > 1:
        nlo, nhi = (lo - 1, hi) if side == "end" else (lo, hi + 1)
        if nlo < 0 or nhi >= len(texts):
            break
        wider = join(nlo, nhi)
        if len(wider) > max_chars or count(wider) == 0:
            break  # past the bound, or out of where the cut looks
        lo, hi, text = nlo, nhi, wider
    return text


def _trigrams(text: str) -> set[str]:
    folded = " ".join(fold_tokens(text))
    padded = f" {folded} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)} if folded else set()


def similarity(a: str, b: str) -> float:
    """Character-trigram Dice score of two texts, accents and case folded (0.0-1.0)."""
    x, y = _trigrams(a), _trigrams(b)
    if not x or not y:
        return 0.0
    return 2 * len(x & y) / (len(x) + len(y))


def offer_lines(
    lines: Iterable[TakeLine],
    quote: str,
    *,
    side: Literal["start", "end"],
    limit: int,
    after_s: float | None = None,
) -> LineOffer:
    """At most ``limit`` of ``lines`` to offer for ``quote``, in time order.

    ``after_s`` keeps only lines that start at / after it (the end line is looked for from the
    start line's first word on, so a line the start words sit inside cannot be ground).
    When more remain than fit, the ones most like the quote stay; a tie (nothing like
    it, e.g. "Thank you" over a Spanish "Gracias") goes to the earliest lines for a start and
    the latest for an end, where such a line usually is. ``recommended`` is set only when one
    line clearly beats the rest (`RECOMMEND_MIN_SCORE`, `RECOMMEND_LEAD`).
    """
    pool = [line for line in lines if after_s is None or line.start_s >= after_s - 1e-6]
    scored = [(similarity(line.text, quote), line) for line in pool]
    if len(scored) > limit:
        late = side == "end"
        ranked = sorted(
            scored, key=lambda pair: (-pair[0], -pair[1].index if late else pair[1].index)
        )
        keep = {line.index for _score, line in ranked[:limit]}
        scored = [pair for pair in scored if pair[1].index in keep]
    scored.sort(key=lambda pair: pair[1].index)
    by_score = sorted(scored, key=lambda pair: -pair[0])
    best = by_score[0] if by_score else None
    runner_up = by_score[1][0] if len(by_score) > 1 else 0.0
    recommended = (
        best[1]
        if best is not None
        and best[0] >= RECOMMEND_MIN_SCORE
        and best[0] >= RECOMMEND_LEAD * runner_up
        else None
    )
    return LineOffer(lines=tuple(line for _score, line in scored), recommended=recommended)


__all__ = [
    "LineOffer",
    "TakeLine",
    "offer_lines",
    "pinned_line_text",
    "similarity",
    "split_clauses",
    "take_lines",
    "take_words",
]
