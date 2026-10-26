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
from app.services.speech_segments import GroundedExcerpt, ground_line

OPENING_CUT_REASON = "opening_cut"
END_CUT_REASON = "end_cut"
# KRI-821: a line the creator named to cut out of the middle of the take.
NAMED_CUT_REASON = "named_cut"
# Removals the quoted lines make; every other removal is speech cleanup's.
TAKE_WINDOW_REASONS = frozenset({OPENING_CUT_REASON, END_CUT_REASON, NAMED_CUT_REASON})
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


def cut_span(plan: CutPlan, start_s: float, end_s: float, *, duration_s: float) -> CutPlan:
    """``plan`` with ``[start_s, end_s]`` removed too (keep/removed re-partitioned)."""

    def _outside(a: float, b: float) -> list[tuple[float, float]]:
        pieces = [(a, min(b, start_s)), (max(a, end_s), b)]
        return [(x, y) for x, y in pieces if y - x > _EPS]

    keep = [piece for s, e in plan.keep_segments for piece in _outside(s, e)]
    removed = [
        dataclasses.replace(r, start_s=x, end_s=y)
        for r in plan.removed
        for x, y in _outside(r.start_s, r.end_s)
    ]
    removed.append(Removal(start_s=start_s, end_s=end_s, reason=NAMED_CUT_REASON))
    removed.sort(key=lambda r: r.start_s)
    return dataclasses.replace(
        plan, keep_segments=keep, removed=removed, time_saved_s=_saved(removed, duration_s)
    )


def _kept_words(plan: CutPlan, words: Sequence[Any]) -> list[Any]:
    """The words whose middle is still inside ``plan``'s kept video."""
    keep = plan.keep_segments
    return [
        w
        for w in words
        if any(s - _EPS <= (_start_s(w) + _end_s(w)) / 2.0 <= e + _EPS for s, e in keep)
    ]


# A take rarely repeats one line more often than this; the search stops there.
_MAX_OCCURRENCES = 8


def line_occurrences(
    words: Sequence[Any], quote: str, *, duration_s: float
) -> list[GroundedExcerpt]:
    """Every place ``quote`` is said in ``words``, in time order (KRI-821).

    Each search uses the same exact-then-fuzzy grounding (`ground_line`), then searches the
    words before and after that match again, so a retake transcribed slightly differently
    each time ("with bread no" / "with the bread, no") still counts as said twice.
    """
    found: list[GroundedExcerpt] = []
    pending: list[list[Any]] = [list(words)]
    while pending and len(found) < _MAX_OCCURRENCES:
        segment = pending.pop()
        if not segment:
            continue
        hit = ground_line(segment, quote, duration_s=duration_s)
        if hit is None:
            continue
        found.append(hit)
        pending.append([w for w in segment if _end_s(w) <= hit.word_start_s + _EPS])
        pending.append([w for w in segment if _start_s(w) >= hit.word_end_s - _EPS])
    return sorted(found, key=lambda g: g.word_start_s)


def _pick(occurrences: Sequence[GroundedExcerpt], occurrence: int | None):
    if len(occurrences) == 1 and occurrence in (None, 1, -1):
        return occurrences[0]
    if occurrence is None:
        return None
    index = occurrence - 1 if occurrence > 0 else len(occurrences) + occurrence
    return occurrences[index] if 0 <= index < len(occurrences) else None


def apply_named_line_cuts(
    plan: CutPlan,
    words: Sequence[Any],
    cuts: Sequence[Sequence[Any]],
    *,
    duration_s: float,
) -> tuple[CutPlan, list[dict[str, Any]]]:
    """KRI-821: cut each line the creator named out of the take, and a receipt per line.

    ``cuts`` is ``(quote, requirement_ids[, occurrence[, pin]])`` per named line
    (`opening_cut_ask.brief_named_cuts`). Each line is grounded in the words still in the
    kept video and the span ``[first word - LEAD_S, last word + TAIL_S]`` is removed, never
    reaching into the neighbouring words. A line said more than once (a retake) is cut only
    at the occurrence the creator named (first / second / last); never guessed: a line the
    take doesn't say (``not_found``), says more than once with no occurrence named
    (``said_more_than_once``), names an occurrence the take doesn't have
    (``occurrence_not_found``) or a cut that would leave less than ``MIN_KEPT_S``
    (``too_short``) leaves the plan untouched and its receipt says why.

    KRI-724: ``pin`` is the creator's answer to "which one?" (`opening_cut_ask.OccurrencePin`,
    source seconds): that occurrence (`select_occurrence`), or every one for "every time";
    a pick the words don't hold is ``pinned_occurrence_missing``, never a guess.
    """
    from app.kria.opening_cut_ask import select_occurrence  # noqa: PLC0415

    words = list(words)
    receipts: list[dict[str, Any]] = []
    cut = plan
    for entry in cuts:
        quote, requirement_ids = entry[0], entry[1]
        occurrence = entry[2] if len(entry) > 2 else None
        pin = entry[3] if len(entry) > 3 else None
        receipt = _receipt(quote, requirement_ids)
        if occurrence is not None:
            receipt["occurrence"] = occurrence
        if pin is not None:
            receipt["pin"] = {"ordinal": pin.ordinal, "of": pin.of, "approx_s": pin.approx_s}
        receipts.append(receipt)
        hits = line_occurrences(_kept_words(cut, words), quote, duration_s=duration_s)
        if not hits:
            receipt["reason"] = "pinned_occurrence_missing" if pin is not None else "not_found"
            continue
        receipt["occurrences_found"] = len(hits)
        if pin is not None and occurrence is None:
            if pin.ordinal is None:
                chosen = list(hits)
            else:
                window = select_occurrence([(g.word_start_s, g.word_end_s) for g in hits], pin)
                chosen = [g for g in hits if (g.word_start_s, g.word_end_s) == window]
            if not chosen:
                receipt["reason"] = "pinned_occurrence_missing"
                continue
        else:
            grounded = _pick(hits, occurrence)
            if grounded is None:
                receipt["reason"] = (
                    "said_more_than_once" if occurrence is None else "occurrence_not_found"
                )
                continue
            chosen = [grounded]
        candidate = cut
        spans: list[tuple[float, float]] = []
        for grounded in chosen:
            start_s = round(_start_before_word(words, grounded.word_start_s), 3)
            end_s = round(_end_after_word(words, grounded.word_end_s, duration_s), 3)
            candidate = cut_span(candidate, start_s, end_s, duration_s=duration_s)
            spans.append((start_s, end_s))
        receipt.update(start_s=spans[0][0], end_s=spans[-1][1])
        if len(spans) > 1:
            receipt["spans"] = [list(span) for span in spans]
        if _kept(candidate) < MIN_KEPT_S:
            receipt["reason"] = "too_short"
            continue
        removed_s = round(_kept(cut) - _kept(candidate), 3)
        cut = candidate
        receipt.update(
            status="applied", removed_s=removed_s, exact=all(bool(g.exact) for g in chosen)
        )
    return cut, receipts


__all__ = [
    "END_CUT_REASON",
    "NAMED_CUT_REASON",
    "OPENING_CUT_REASON",
    "TAKE_WINDOW_REASONS",
    "apply_named_line_cuts",
    "apply_opening_cut",
    "apply_take_window",
    "cut_after",
    "cut_before",
    "cut_span",
    "line_occurrences",
    "ground_take_line",
]
