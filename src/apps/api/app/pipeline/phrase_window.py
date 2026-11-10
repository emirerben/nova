"""The phrase a spoken cue sits in, and the window a visual cued by it should cover (KRI-1342).

A creator who asks "when I mention the billboards, show that picture" means the picture belongs
to the words about the billboards ("like car brands fighting on billboards."), not to the one
word the planner listens for. `phrase_window` finds that phrase in the timed words around a
heard cue and sizes a window to it:

- **Start**: the first word of the clause holding the cue. Clauses end at sentence punctuation
  (``. ! ? …``), clause punctuation (``, ; : —``), a cut-off word (``bill...``) or a pause. Words
  without punctuation (raw whisper) fall back to pauses. The start walks back at most
  ``max_lookback_s``; a longer clause keeps the cue's own start. A small lead uses only the
  silence before the first word, never the previous word.
- **End**: the end of the clause, then of its sentence when the clause alone is shorter than
  ``min_s``. A phrase still shorter than ``min_s`` grows by WHOLE following sentences that fit
  within ``max_s``; only when none fits does it end at ``start + min_s``. A phrase longer than
  ``max_s`` ends at the last word end that fits. The end may run on into the silence after the
  last word (``tail_s``) but never over the next word.

Pure: no I/O, no settings. Every time is in the words' own timeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PhraseWindow",
    "phrase_window",
    "sentence_cut_inside",
]

# A gap this long between two words ends a clause even with no punctuation.
PAUSE_BOUNDARY_S = 0.45
# Walk back at most this far from the cue to its clause start.
MAX_LOOKBACK_S = 2.5
# The lead before the first word, taken from silence only.
LEAD_S = 0.1
# How far a window may run on into the silence after its last word.
TAIL_S = 0.2

_TRAILING_CLOSERS = "\"'”’»)]}"
_SENTENCE_MARKS = (".", "!", "?", "…")
_CLAUSE_MARKS = (",", ";", ":", "—", "–")


@dataclass(frozen=True)
class _Word:
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class PhraseWindow:
    """Where the visual goes, and why.

    ``basis`` is ``phrase`` (the start moved to the clause start, or the cue already starts it)
    or ``trigger`` (no usable phrase: the cue's own start is kept)."""

    start_s: float
    end_s: float
    phrase_start_s: float
    phrase_end_s: float
    basis: str
    # The first word after the cue's own sentence, None when the talk ends there.
    next_sentence_start_s: float | None = None
    # The window grew over this many whole following sentences to reach ``min_s``.
    extended_sentences: int = 0


def _words(words: Sequence[Any]) -> list[_Word]:
    out: list[_Word] = []
    for raw in words or ():
        if isinstance(raw, dict):
            text_v, start_v, end_v = raw.get("text"), raw.get("start_s"), raw.get("end_s")
        else:  # a transcript word object (``.text`` / ``.start_s`` / ``.end_s``)
            text_v = getattr(raw, "text", None)
            start_v = getattr(raw, "start_s", None)
            end_v = getattr(raw, "end_s", None)
        text = str(text_v or "").strip()
        try:
            start = float(start_v)  # type: ignore[arg-type]
            end = float(end_v) if end_v is not None else start
        except (TypeError, ValueError):
            continue
        if not text or start != start or end != end:  # NaN
            continue
        out.append(_Word(text, start, max(start, end)))
    out.sort(key=lambda w: (w.start_s, w.end_s))
    return out


def _mark(text: str) -> str | None:
    """``sentence`` / ``clause`` for the punctuation a word ends with, else None."""
    core = text.rstrip(_TRAILING_CLOSERS)
    if core.endswith("...") or core.endswith(_SENTENCE_MARKS):
        return "sentence"
    if core.endswith(_CLAUSE_MARKS):
        return "clause"
    return None


def _boundary_after(words: list[_Word], k: int) -> str | None:
    """The kind of break between word ``k`` and ``k + 1`` (``sentence``/``clause``/None)."""
    if k >= len(words) - 1:
        return "sentence"
    mark = _mark(words[k].text)
    if mark == "sentence" or words[k + 1].start_s - words[k].end_s >= PAUSE_BOUNDARY_S:
        # A real pause ends the thought even after a comma ("phone, <pause> Vila posted").
        return "sentence"
    return mark


def _hit_span(words: list[_Word], hit_start_s: float, hit_end_s: float) -> tuple[int, int] | None:
    eps = 0.02
    first = next((i for i, w in enumerate(words) if w.start_s >= hit_start_s - eps), None)
    if first is None:
        return None
    last = first
    for i in range(first, len(words)):
        if words[i].start_s <= hit_end_s - eps or i == first:
            last = i
        else:
            break
    return first, last


def phrase_window(
    words: Sequence[Any],
    hit_start_s: float,
    hit_end_s: float,
    *,
    min_s: float,
    max_s: float,
    size_to_phrase: bool = True,
    hold_s: float | None = None,
    max_lookback_s: float = MAX_LOOKBACK_S,
    lead_s: float = LEAD_S,
    tail_s: float = TAIL_S,
    duration_s: float | None = None,
    not_before_s: float | None = None,
) -> PhraseWindow | None:
    """The window for a visual cued by the words heard at ``[hit_start_s, hit_end_s]``.

    ``size_to_phrase`` False keeps a fixed length ``hold_s`` from the phrase start (an explicit
    hold, a video's own length, a photo-then-clip sequence). ``not_before_s`` keeps the start
    after an earlier visual's own cue word. None when the cue is not among ``words``."""
    timed = _words(words)
    span = _hit_span(timed, hit_start_s, hit_end_s)
    if span is None:
        return None
    first, last = span
    cue_start = timed[first].start_s
    cue_end = timed[last].end_s

    # --- start: walk back to the clause start ---------------------------------------------
    j = first
    while j > 0 and _boundary_after(timed, j - 1) is None:
        j -= 1
    basis = "phrase"
    if cue_start - timed[j].start_s > max_lookback_s:
        j, basis = first, "trigger"
    floor = 0.0 if not_before_s is None else max(0.0, not_before_s)
    if timed[j].start_s < floor:
        # An earlier visual's cue sits inside this phrase: start after it, on a word start.
        later = next((i for i in range(j, first + 1) if timed[i].start_s >= floor), first)
        j = later
        basis = "phrase" if j < first else basis
    phrase_start = timed[j].start_s
    silence_from = timed[j - 1].end_s if j > 0 else 0.0
    start = max(phrase_start - lead_s, min(silence_from, phrase_start), floor, 0.0)

    # --- the clause / sentence ends ---------------------------------------------------------
    k = last
    while _boundary_after(timed, k) is None:
        k += 1
    clause_end_idx = k
    s = clause_end_idx
    while _boundary_after(timed, s) != "sentence":
        s += 1
    sentence_end_idx = s
    next_sentence = (
        timed[sentence_end_idx + 1].start_s if sentence_end_idx + 1 < len(timed) else None
    )

    def _end_at(idx: int) -> float:
        """Word ``idx``'s end plus the silence after it (at most ``tail_s``), trimmed back to
        the bare word end when the tail alone would pass ``max_s``."""
        nxt = timed[idx + 1].start_s if idx + 1 < len(timed) else None
        end = timed[idx].end_s + tail_s
        if nxt is not None:
            end = min(end, max(timed[idx].end_s, nxt))
        if end - start > max_s >= timed[idx].end_s - start:
            end = timed[idx].end_s
        return end

    limit = duration_s if duration_s is not None else float("inf")

    def _covering_cue(begin: float, finish: float) -> tuple[float, float]:
        """The same-length window moved later just enough to still be on screen while the cue
        is said (it ends no earlier than the cue's last word, or starts on the cue when it is
        shorter than the cue): a short fixed-length visual never ends before its own word."""
        if finish >= cue_end:
            return begin, finish
        length = finish - begin
        moved = min(cue_start, max(begin, cue_end - length))
        return moved, min(moved + length, limit)

    if not size_to_phrase:
        length = hold_s if hold_s is not None else min_s
        start, end = _covering_cue(start, min(start + length, limit))
        return PhraseWindow(
            start_s=round(start, 3),
            end_s=round(end, 3),
            phrase_start_s=round(phrase_start, 3),
            phrase_end_s=round(timed[clause_end_idx].end_s, 3),
            basis=basis,
            next_sentence_start_s=next_sentence,
        )

    extended = 0
    end_idx = clause_end_idx
    if _end_at(end_idx) - start < min_s:
        end_idx = sentence_end_idx
    end = _end_at(end_idx)
    if end - start < min_s:
        # Grow by whole sentences while they fit within the max.
        idx = sentence_end_idx
        grown = None
        while idx + 1 < len(timed):
            nxt_end = idx + 1
            while _boundary_after(timed, nxt_end) != "sentence":
                nxt_end += 1
            candidate = _end_at(nxt_end)
            if candidate - start > max_s:
                break
            extended += 1
            idx = nxt_end
            if candidate - start >= min_s:
                grown = candidate
                break
        if grown is not None:
            end = grown
        else:
            # No whole sentence fits: end at the last clause break (comma, pause) that does,
            # else at the minimum.
            extended = 0
            breaks = [
                _end_at(i)
                for i in range(sentence_end_idx + 1, len(timed))
                if _boundary_after(timed, i) is not None and min_s <= _end_at(i) - start <= max_s
            ]
            end = max(breaks) if breaks else start + min_s
    elif end - start > max_s:
        # Too long: end at the last word end that fits (never under the minimum).
        fits = [
            _end_at(i) for i in range(last, end_idx + 1) if min_s <= _end_at(i) - start <= max_s
        ]
        end = max(fits) if fits else start + max_s
    start, end = _covering_cue(start, min(end, limit))
    return PhraseWindow(
        start_s=round(start, 3),
        end_s=round(end, 3),
        phrase_start_s=round(phrase_start, 3),
        phrase_end_s=round(timed[clause_end_idx].end_s, 3),
        basis=basis,
        next_sentence_start_s=next_sentence,
        extended_sentences=extended,
    )


def sentence_cut_inside(
    words: Sequence[Any], start_s: float, end_s: float, *, cue_end_s: float
) -> bool:
    """True when a window ``[start_s, end_s]`` ends part-way through a sentence that began
    after its cue was said (the visual cuts away in the middle of the NEXT sentence).

    The measurement the KRI-1342 corpus check uses; also handy for tests."""
    timed = _words(words)
    sentences: list[tuple[float, float]] = []
    begin = 0
    for k in range(len(timed)):
        if _boundary_after(timed, k) == "sentence":
            sentences.append((timed[begin].start_s, timed[k].end_s))
            begin = k + 1
    eps = 0.05
    return any(s >= cue_end_s - eps and s < end_s - eps and e > end_s + eps for s, e in sentences)
