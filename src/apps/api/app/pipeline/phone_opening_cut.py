"""Cut a phone Talking take to the lines the creator quoted (KRI-628 start, KRI-790 end).

"Cut the intro ... start the video at 'Abonelerimize bir şey demek ister misin'" and "start
at 'Hi, I'm Karolina, I'm from Ecuador' and end right after 'I'm gonna be here all week'"
are cuts of SPEECH, which speech cleanup (filler / long pause / retake) never makes. The
phone Talking worker reads the asks off the approved brief (`app.kria.opening_cut_ask`),
finds each quoted line in the take's own word timings -- the same words the cleanup
`CutPlan` and the captions were built from -- and keeps only ``[start line, end of end
line]`` by intersecting the plan's keep segments with it. The compiler then carries the
cut to captions, title, lanes and cutaways exactly like a cleanup cut.

Never guessed: a line the take doesn't say, or a cut that would leave too little video,
leaves that side of the plan untouched and its receipt says why.

KRI-844: a quote in another language than the take ("Here we have traditional Argentinian
sauces" over Spanish speech) is not in the words, so the creator is asked which line of the
take to open / end on and the pick comes in as ``start_spoken`` / ``end_spoken``: the take's
own words, grounded first. The creator's quote stays the receipt's ``quote`` (what the cards
name); the pick is recorded beside it as ``spoken``. A pick the words don't hold either (a
re-transcribed take) falls back to the quote, then to the honest ``not_found``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from app.pipeline.silence_cut import CutPlan, Removal
from app.services.speech_segments import ground_line

OPENING_CUT_REASON = "opening_cut"
END_CUT_REASON = "end_cut"
# Removals the quoted lines make; every other removal is speech cleanup's.
TAKE_WINDOW_REASONS = frozenset({OPENING_CUT_REASON, END_CUT_REASON})
# The cut lands this far before the line's first word, never into the word before it.
LEAD_S = 0.15
# ...and this far after the end line's last word, never into the word after it.
TAIL_S = 0.25
# A start line later than this share of the take, or leaving less than this much
# video, is a mis-heard quote rather than a request.
MAX_START_FRACTION = 0.8
MIN_KEPT_S = 2.0
_EPS = 1e-6


def _clip_keep(
    keep_segments: Sequence[tuple[float, float]], start_s: float
) -> list[tuple[float, float]]:
    clipped = [(max(s, start_s), e) for s, e in keep_segments if e > start_s + _EPS]
    return [(s, e) for s, e in clipped if e - s > _EPS]


def _saved(removed: Sequence[Removal], duration_s: float) -> float:
    return min(sum(r.end_s - r.start_s for r in removed), duration_s)


def cut_before(plan: CutPlan, start_s: float, *, duration_s: float) -> CutPlan:
    """``plan`` with everything before ``start_s`` removed too (keep/removed re-partitioned)."""
    keep = _clip_keep(plan.keep_segments, start_s)
    removed = [
        dataclasses.replace(r, start_s=max(r.start_s, start_s))
        for r in plan.removed
        if r.end_s > start_s + _EPS
    ]
    removed.insert(0, Removal(start_s=0.0, end_s=start_s, reason=OPENING_CUT_REASON))
    return dataclasses.replace(
        plan, keep_segments=keep, removed=removed, time_saved_s=_saved(removed, duration_s)
    )


def cut_after(plan: CutPlan, end_s: float, *, duration_s: float) -> CutPlan:
    """``plan`` with everything after ``end_s`` removed too (keep/removed re-partitioned)."""
    keep = [(s, min(e, end_s)) for s, e in plan.keep_segments if s < end_s - _EPS]
    keep = [(s, e) for s, e in keep if e - s > _EPS]
    removed = [
        dataclasses.replace(r, end_s=min(r.end_s, end_s))
        for r in plan.removed
        if r.start_s < end_s - _EPS
    ]
    removed.append(Removal(start_s=end_s, end_s=duration_s, reason=END_CUT_REASON))
    return dataclasses.replace(
        plan, keep_segments=keep, removed=removed, time_saved_s=_saved(removed, duration_s)
    )


def _start_s(word: Any) -> float:
    return float(word["start_s"] if isinstance(word, dict) else word.start_s)


def _end_s(word: Any) -> float:
    return float(word["end_s"] if isinstance(word, dict) else word.end_s)


def _start_before_word(words: Sequence[Any], first_word_start_s: float) -> float:
    previous_end = max(
        (_end_s(w) for w in words if _end_s(w) <= first_word_start_s + _EPS), default=0.0
    )
    return max(first_word_start_s - LEAD_S, (previous_end + first_word_start_s) / 2.0, 0.0)


def _end_after_word(words: Sequence[Any], last_word_end_s: float, duration_s: float) -> float:
    next_start = min(
        (_start_s(w) for w in words if _start_s(w) >= last_word_end_s - _EPS), default=None
    )
    end = last_word_end_s + TAIL_S
    if next_start is not None:
        end = min(end, (last_word_end_s + next_start) / 2.0)
    return min(max(end, last_word_end_s), duration_s)


def _kept(plan: CutPlan) -> float:
    return sum(e - s for s, e in plan.keep_segments)


def _receipt(
    quote: str, requirement_ids: Sequence[str], spoken: str | None = None
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "version": 1,
        "quote": quote,
        "requirement_ids": list(requirement_ids),
        "status": "not_applied",
    }
    if spoken:
        receipt["spoken"] = spoken
    return receipt


def ground_take_line(
    words: Sequence[Any],
    spoken: str | None,
    quote: str | None,
    *,
    duration_s: float,
    min_after_s: float = 0.0,
):
    """Where a side's line is said: the picked spoken line first, else the creator's quote."""
    for line in (spoken, quote):
        if line:
            grounded = ground_line(words, line, duration_s=duration_s, min_after_s=min_after_s)
            if grounded is not None:
                return grounded
    return None


def apply_take_window(
    plan: CutPlan,
    words: Sequence[Any],
    *,
    start_quote: str | None,
    end_quote: str | None,
    duration_s: float,
    start_ids: Sequence[str] = (),
    end_ids: Sequence[str] = (),
    start_spoken: str | None = None,
    end_spoken: str | None = None,
) -> tuple[CutPlan, dict[str, Any] | None, dict[str, Any] | None]:
    """The plan cut to ``[start_quote, end of end_quote]``, and a receipt per asked line.

    Each receipt's ``status`` is ``"applied"`` or ``"not_applied"``, with a ``reason`` of
    ``not_found`` / ``implausible`` / ``too_short`` (and ``before_start`` for an end line
    said only before the start line); that side of the plan is then left untouched. A line
    not asked for has no receipt (``None``). The end line is looked for after the start
    line, so a phrase said twice ends the video the first time it follows the start.
    ``start_spoken`` / ``end_spoken`` (KRI-844) are lines of the take the creator picked for a
    quote it does not hold; they are grounded before the quote and added to the receipt.
    """
    words = list(words)
    cut = plan
    start_receipt: dict[str, Any] | None = None
    end_receipt: dict[str, Any] | None = None
    line_start_s = 0.0
    if start_quote:
        start_receipt = _receipt(start_quote, start_ids, start_spoken)
        grounded = ground_take_line(words, start_spoken, start_quote, duration_s=duration_s)
        if grounded is None:
            start_receipt["reason"] = "not_found"
        else:
            line_start_s = grounded.word_start_s
            start_s = round(_start_before_word(words, grounded.word_start_s), 3)
            candidate = cut_before(cut, start_s, duration_s=duration_s)
            if start_s <= _EPS:
                # The take already opens on the line: nothing to cut, and the ask is met.
                start_receipt.update(status="applied", start_s=0.0, removed_s=0.0)
            elif start_s > duration_s * MAX_START_FRACTION:
                start_receipt.update(reason="implausible", start_s=start_s)
            elif _kept(candidate) < MIN_KEPT_S:
                start_receipt.update(reason="too_short", start_s=start_s)
            else:
                cut = candidate
                start_receipt.update(
                    status="applied",
                    start_s=start_s,
                    removed_s=start_s,
                    exact=bool(grounded.exact),
                )
    if end_quote:
        end_receipt = _receipt(end_quote, end_ids, end_spoken)
        grounded = ground_take_line(
            words, end_spoken, end_quote, duration_s=duration_s, min_after_s=line_start_s
        )
        if grounded is None:
            said_before = line_start_s > 0 and ground_take_line(
                words, end_spoken, end_quote, duration_s=duration_s
            )
            end_receipt["reason"] = "before_start" if said_before else "not_found"
        else:
            end_s = round(_end_after_word(words, grounded.word_end_s, duration_s), 3)
            candidate = cut_after(cut, end_s, duration_s=duration_s)
            if end_s >= duration_s - _EPS:
                # The take already ends on the line.
                end_receipt.update(status="applied", end_s=round(duration_s, 3), removed_s=0.0)
            elif _kept(candidate) < MIN_KEPT_S:
                end_receipt.update(reason="too_short", end_s=end_s)
            else:
                cut = candidate
                end_receipt.update(
                    status="applied",
                    end_s=end_s,
                    removed_s=round(duration_s - end_s, 3),
                    exact=bool(grounded.exact),
                )
    return cut, start_receipt, end_receipt


def apply_opening_cut(
    plan: CutPlan,
    words: Sequence[Any],
    quote: str,
    *,
    duration_s: float,
    requirement_ids: Sequence[str] = (),
) -> tuple[CutPlan, dict[str, Any]]:
    """`apply_take_window` with a start line only (KRI-628's shape)."""
    cut, receipt, _end = apply_take_window(
        plan,
        words,
        start_quote=quote,
        end_quote=None,
        duration_s=duration_s,
        start_ids=requirement_ids,
    )
    assert receipt is not None  # noqa: S101 - a start line was asked
    return cut, receipt


__all__ = [
    "END_CUT_REASON",
    "OPENING_CUT_REASON",
    "TAKE_WINDOW_REASONS",
    "apply_opening_cut",
    "apply_take_window",
    "cut_after",
    "cut_before",
    "ground_take_line",
]
