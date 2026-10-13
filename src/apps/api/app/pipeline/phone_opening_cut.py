"""Start a phone Talking take at the line the creator quoted (KRI-628).

"Cut the intro ... start the video at 'Abonelerimize bir şey demek ister misin'" is a
cut of SPEECH, which speech cleanup (filler / long pause / retake) never makes. The
phone Talking worker reads the ask off the approved brief (`app.kria.opening_cut_ask`),
finds the quoted line in the take's own word timings -- the same words the cleanup
`CutPlan` and the captions were built from -- and cuts everything before it by
intersecting the plan's keep segments with ``[line start, end)``. The compiler then
carries the cut to captions, title, lanes and cutaways exactly like a cleanup cut.

Never guessed: a line the take doesn't say, or a cut that would leave too little video,
leaves the plan untouched and the receipt says why.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from app.pipeline.silence_cut import CutPlan, Removal
from app.services.speech_segments import ground_excerpt

OPENING_CUT_REASON = "opening_cut"
# The cut lands this far before the line's first word, never into the word before it.
LEAD_S = 0.15
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


def cut_before(plan: CutPlan, start_s: float, *, duration_s: float) -> CutPlan:
    """``plan`` with everything before ``start_s`` removed too (keep/removed re-partitioned)."""
    keep = _clip_keep(plan.keep_segments, start_s)
    removed = [
        dataclasses.replace(r, start_s=max(r.start_s, start_s))
        for r in plan.removed
        if r.end_s > start_s + _EPS
    ]
    removed.insert(0, Removal(start_s=0.0, end_s=start_s, reason=OPENING_CUT_REASON))
    saved = sum(r.end_s - r.start_s for r in removed)
    return dataclasses.replace(
        plan,
        keep_segments=keep,
        removed=removed,
        time_saved_s=min(saved, duration_s),
    )


def _end_s(word: Any) -> float:
    return float(word["end_s"] if isinstance(word, dict) else word.end_s)


def _start_before_word(words: Sequence[Any], first_word_start_s: float) -> float:
    previous_end = max(
        (_end_s(w) for w in words if _end_s(w) <= first_word_start_s + _EPS), default=0.0
    )
    return max(first_word_start_s - LEAD_S, (previous_end + first_word_start_s) / 2.0, 0.0)


def apply_opening_cut(
    plan: CutPlan,
    words: Sequence[Any],
    quote: str,
    *,
    duration_s: float,
    requirement_ids: Sequence[str] = (),
) -> tuple[CutPlan, dict[str, Any]]:
    """The plan cut to start at ``quote``, and the receipt of what happened.

    ``receipt["status"]`` is ``"applied"`` or ``"not_applied"`` (with a ``reason`` of
    ``not_found`` / ``implausible`` / ``too_short``, and the untouched plan).
    """
    receipt: dict[str, Any] = {
        "version": 1,
        "quote": quote,
        "requirement_ids": list(requirement_ids),
        "status": "not_applied",
    }
    grounded = ground_excerpt(list(words), quote, duration_s=duration_s)
    if grounded is None:
        return plan, {**receipt, "reason": "not_found"}
    start_s = round(_start_before_word(words, grounded.word_start_s), 3)
    if start_s <= _EPS:
        # The take already opens on the line: nothing to cut, and the ask is met.
        return plan, {**receipt, "status": "applied", "start_s": 0.0, "removed_s": 0.0}
    if start_s > duration_s * MAX_START_FRACTION:
        return plan, {**receipt, "reason": "implausible", "start_s": start_s}
    cut = cut_before(plan, start_s, duration_s=duration_s)
    kept = sum(e - s for s, e in cut.keep_segments)
    if kept < MIN_KEPT_S:
        return plan, {**receipt, "reason": "too_short", "start_s": start_s}
    return cut, {
        **receipt,
        "status": "applied",
        "start_s": start_s,
        "removed_s": round(start_s, 3),
        "exact": bool(grounded.exact),
    }


__all__ = ["OPENING_CUT_REASON", "apply_opening_cut", "cut_before"]
