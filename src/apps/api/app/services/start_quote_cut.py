"""Start the video at a line the creator quotes (KRI-628).

"Cut the intro and start the video at 'Abonelerimize bir şey demek ister misin'" names
the spoken line the edit must open on. Speech cleanup only removes pauses, fillers and
retakes, so the intro stayed and nothing said so. This module:

* ``start_quote_request`` -- the quote a brief requirement wants the video to START at
  (not a quote it merely mentions, like the intro it wants cut).
* ``apply_start_quote_cut`` -- trims a ``CutPlan`` so it keeps only what plays from that
  line on, grounded in the take's word timings. Anything that cannot be grounded safely
  leaves the plan untouched and says why, so the caller never guesses a cut.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from app.pipeline.silence_cut import CutPlan, Removal
from app.services.spoken_quotes import (
    _QUOTED_RE,
    MIN_QUOTE_TOKENS,
    fold_tokens,
    locate_in_words,
)

START_CUT_REASON = "start_quote"
# "start the video at", "begin with", "open on", "başlasın", "başla" -- folded stems.
_START_STEMS = ("start", "begin", "open", "kick", "basla", "baslat")
# The verb and its quote sit in one clause: this many folded tokens at most between them.
_MAX_TOKENS_TO_VERB = 6
# A start line this early is already the start; this late it would cut most of the take.
_MIN_CUT_S = 0.6
_MAX_CUT_FRACTION = 0.6
_MIN_KEPT_S = 3.0
# Breath kept before the first word of the line, never reaching back into the previous word.
_LEAD_S = 0.2
_PREVIOUS_WORD_GAP_S = 0.03


def start_quote_request(descriptions: Sequence[str]) -> str | None:
    """The quoted line a requirement asks the video to start at, else ``None``.

    The quote must sit right next to a start verb in the text OUTSIDE other quotes
    ("video “X” sorusuyla başlasın", "start the video at “X”"). A quote that merely
    contains a start-like word ("Hazırlıklara başladın mı") is never read as the verb.
    """
    for description in descriptions:
        text = str(description or "")
        # The text as a flat list: folded words outside quotes, and each quote as a dict.
        flat: list[str | dict[str, str]] = []
        cursor = 0
        for match in _QUOTED_RE.finditer(text):
            flat.extend(fold_tokens(text[cursor : match.start()]))
            phrase = next(group for group in match.groups() if group is not None).strip()
            flat.append({"quote": phrase})
            cursor = match.end()
        flat.extend(fold_tokens(text[cursor:]))
        for slot, item in enumerate(flat):
            if not isinstance(item, dict) or len(fold_tokens(item["quote"])) < MIN_QUOTE_TOKENS:
                continue
            window = flat[max(0, slot - _MAX_TOKENS_TO_VERB) : slot + 1 + _MAX_TOKENS_TO_VERB]
            if any(isinstance(t, str) and t.startswith(_START_STEMS) for t in window):
                return item["quote"]
    return None


def _word_value(word: Any, key: str) -> float:
    return float(word[key] if isinstance(word, dict) else getattr(word, key))


def apply_start_quote_cut(
    plan: CutPlan,
    words: Sequence[Any],
    quote: str,
    *,
    duration_s: float,
) -> tuple[CutPlan, dict[str, Any]]:
    """``plan`` trimmed to start at ``quote``, plus a receipt (``status`` says what happened).

    Receipt ``status``: ``applied``; ``not_found`` (the take never says the line);
    ``too_early`` (it already opens the video); ``too_late`` (cutting to it would drop
    most of the take, so it was probably the wrong match); ``too_short`` (almost nothing
    would be left). Every status but ``applied`` returns ``plan`` unchanged.
    """
    receipt: dict[str, Any] = {"quote": quote, "status": "not_found"}
    span = locate_in_words(quote, list(words), duration_s)
    if span is None:
        return plan, receipt
    line_start = span[0]
    previous_end = max(
        (_word_value(w, "end_s") for w in words if _word_value(w, "end_s") <= line_start + 1e-6),
        default=0.0,
    )
    boundary = max(previous_end + _PREVIOUS_WORD_GAP_S, line_start - _LEAD_S, 0.0)
    boundary = min(boundary, line_start)
    receipt["start_s"] = round(boundary, 3)
    if boundary < _MIN_CUT_S:
        receipt["status"] = "too_early"
        return plan, receipt
    if boundary > duration_s * _MAX_CUT_FRACTION:
        receipt["status"] = "too_late"
        return plan, receipt

    keep = [
        (max(start, boundary), end) for start, end in plan.keep_segments if end - 1e-6 > boundary
    ]
    keep = [(start, end) for start, end in keep if end - start > 1e-3]
    if sum(end - start for start, end in keep) < _MIN_KEPT_S:
        receipt["status"] = "too_short"
        return plan, receipt

    removed = [Removal(start_s=0.0, end_s=round(boundary, 3), reason=START_CUT_REASON)]
    for r in plan.removed:
        if r.end_s <= boundary + 1e-6:
            continue  # inside the cut-off intro: the start cut already removes it
        removed.append(dataclasses.replace(r, start_s=max(r.start_s, boundary)))
    receipt["status"] = "applied"
    receipt["removed_text"] = " ".join(
        str(w["text"] if isinstance(w, dict) else w.text).strip()
        for w in words
        if _word_value(w, "start_s") < boundary
    )[:300]
    trimmed = dataclasses.replace(
        plan,
        keep_segments=keep,
        removed=removed,
        time_saved_s=sum(r.end_s - r.start_s for r in removed),
        # The pause analysis itself is unchanged; its diagnostics describe the uncut take.
    )
    return trimmed, receipt
