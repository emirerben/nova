"""Exact, creator-requested spoken-phrase removal on the source timeline.

This is intentionally separate from automatic silence and retake detection.
It accepts only an exact normalized phrase plus an optional 1-based occurrence,
then composes that source interval with the render's existing ``CutPlan``.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.pipeline.silence_cut import CutPlan, Removal


def speech_cut_tokens(text: object) -> list[str]:
    """Case/punctuation folding only: destructive matches retain accents and numbers.

    Turkish uppercase İ casefolds to i + combining dot; normalize that spelling
    without collapsing distinct words such as kar/kâr or sik/sık. Reaction-text
    matching is deliberately broader and must not authorize deleting speech.
    """
    folded = unicodedata.normalize("NFC", str(text or "").casefold().replace("i\u0307", "i"))
    return re.findall(r"[^\W_]+", folded, flags=re.UNICODE)


_EPS = 1e-9
_MIN_OUTPUT_S = 2.0


@dataclass(frozen=True)
class ExplicitSpeechCutRequest:
    """A creator's exact spoken phrase and optional 1-based duplicate selector."""

    phrase: str
    occurrence: int | None = None


@dataclass(frozen=True)
class ExplicitSpeechCutReceipt:
    """Stable source evidence for an explicit spoken-phrase removal."""

    reason: str
    status: str
    phrase: str
    occurrence: int
    source_span: dict[str, float]
    source_start_s: float
    source_end_s: float
    word_indexes: list[int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "status": self.status,
            "phrase": self.phrase,
            "occurrence": self.occurrence,
            "source_span": self.source_span,
            "source_start_s": self.source_start_s,
            "source_end_s": self.source_end_s,
            "word_indexes": self.word_indexes,
        }


@dataclass(frozen=True)
class ExplicitSpeechCutResult:
    plan: CutPlan
    receipt: dict[str, Any]


@dataclass(frozen=True)
class _SourceToken:
    token: str
    word_index: int
    token_index: int
    tokens_in_word: int
    start_s: float
    end_s: float
    confidence: float | None


def _row(word: Any) -> tuple[str, float, float]:
    if isinstance(word, dict):
        text, start, end = word.get("text", ""), word.get("start_s"), word.get("end_s")
    else:
        text = getattr(word, "text", "")
        start = getattr(word, "start_s", None)
        end = getattr(word, "end_s", None)
    try:
        return str(text or ""), float(start), float(end)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_source_word_timing") from exc


def _confidence(word: Any) -> float | None:
    value = word.get("confidence") if isinstance(word, dict) else getattr(word, "confidence", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _tokenize_source(words: Sequence[Any]) -> list[_SourceToken]:
    """Keep every original word index while applying the shared TR-aware fold."""
    stream: list[_SourceToken] = []
    for word_index, raw in enumerate(words):
        text, start_s, end_s = _row(raw)
        confidence = _confidence(raw)
        tokens = speech_cut_tokens(text)
        for token_index, token in enumerate(tokens):
            stream.append(
                _SourceToken(
                    token=token,
                    word_index=word_index,
                    token_index=token_index,
                    tokens_in_word=len(tokens),
                    start_s=start_s,
                    end_s=end_s,
                    confidence=confidence,
                )
            )
    return stream


def _validate_request(request: ExplicitSpeechCutRequest) -> tuple[list[str], int | None]:
    if not isinstance(request, ExplicitSpeechCutRequest):
        raise ValueError("invalid_explicit_speech_cut_request")
    target = speech_cut_tokens(request.phrase)
    if not target:
        raise ValueError("missing_phrase")
    occurrence = request.occurrence
    if occurrence is not None and (type(occurrence) is not int or occurrence < 1):
        raise ValueError("invalid_phrase_occurrence")
    return target, occurrence


def _matches(stream: list[_SourceToken], target: list[str]) -> list[tuple[int, int]]:
    return [
        (start, start + len(target) - 1)
        for start in range(len(stream) - len(target) + 1)
        if [item.token for item in stream[start : start + len(target)]] == target
    ]


def _selected_interval(
    stream: list[_SourceToken], first: int, last: int
) -> tuple[float, float, list[int]]:
    selected = stream[first : last + 1]
    # A phrase that begins or ends inside a multi-token saved word would cut an
    # unrequested neighbour sharing that word's timestamp. Refuse it instead.
    if selected[0].token_index != 0 or selected[-1].token_index != selected[-1].tokens_in_word - 1:
        raise ValueError("ambiguous_source_phrase")
    word_indexes = list(dict.fromkeys(item.word_index for item in selected))
    start_s = selected[0].start_s
    end_s = selected[-1].end_s
    if not math.isfinite(start_s) or not math.isfinite(end_s) or start_s < 0 or end_s <= start_s:
        raise ValueError("invalid_source_word_timing")
    if any(
        not math.isfinite(item.start_s)
        or not math.isfinite(item.end_s)
        or item.end_s <= item.start_s
        for item in selected
    ):
        raise ValueError("invalid_source_word_timing")
    if any(
        item.confidence is not None and math.isfinite(item.confidence) and item.confidence < 0.5
        for item in selected
    ):
        raise ValueError("low_confidence_source_phrase")
    # A selected phrase cannot be grounded safely when its words overlap.
    if any(right.start_s < left.end_s - _EPS for left, right in zip(selected, selected[1:])):
        raise ValueError("invalid_source_word_timing")
    return start_s, end_s, word_indexes


def _assert_no_unselected_word_overlap(
    words: Sequence[Any], *, start_s: float, end_s: float, selected_indexes: list[int]
) -> None:
    """Never remove another word sharing the requested phrase's interval."""
    selected = set(selected_indexes)
    for index, raw in enumerate(words):
        if index in selected:
            continue
        _text, word_start, word_end = _row(raw)
        # Persisted ASR has occasional zero-length rows. They have no audible
        # interval and cannot be damaged by the cut, so do not reject them.
        if not math.isfinite(word_start) or not math.isfinite(word_end):
            continue
        if word_end <= word_start:
            continue
        if word_start < end_s - _EPS and word_end > start_s + _EPS:
            raise ValueError("ambiguous_source_word_overlap")


def _existing_removals(existing_plan: CutPlan | None, duration_s: float) -> list[Removal]:
    if existing_plan is None:
        return []
    rows: list[Removal] = []
    for removal in existing_plan.removed:
        start_s, end_s = removal.start_s, removal.end_s
        if not math.isfinite(start_s) or not math.isfinite(end_s) or end_s <= start_s:
            raise ValueError("invalid_existing_removal")
        if start_s < -_EPS or end_s > duration_s + _EPS:
            raise ValueError("invalid_existing_removal")
        rows.append(Removal(max(0.0, start_s), min(duration_s, end_s), removal.reason))
    return rows


def _same_segments(actual: list[tuple[float, float]], expected: list[tuple[float, float]]) -> bool:
    return len(actual) == len(expected) and all(
        abs(actual_start - expected_start) <= _EPS and abs(actual_end - expected_end) <= _EPS
        for (actual_start, actual_end), (expected_start, expected_end) in zip(actual, expected)
    )


def _union_removals(removals: list[Removal], duration_s: float) -> list[Removal]:
    merged: list[Removal] = []
    for removal in sorted(removals, key=lambda item: (item.start_s, item.end_s, item.reason)):
        if merged and removal.start_s <= merged[-1].end_s + _EPS:
            previous = merged[-1]
            merged[-1] = Removal(
                previous.start_s, max(previous.end_s, removal.end_s), previous.reason
            )
        else:
            merged.append(removal)
    return merged


def _keep_segments(removals: list[Removal], duration_s: float) -> list[tuple[float, float]]:
    cursor = 0.0
    keep: list[tuple[float, float]] = []
    for removal in removals:
        if removal.start_s > cursor + _EPS:
            keep.append((cursor, removal.start_s))
        cursor = max(cursor, removal.end_s)
    if duration_s > cursor + _EPS:
        keep.append((cursor, duration_s))
    return keep


def build_explicit_speech_cut(
    words: Sequence[Any],
    request: ExplicitSpeechCutRequest,
    *,
    duration_s: float,
    existing_plan: CutPlan | None = None,
) -> ExplicitSpeechCutResult:
    """Apply one exact source phrase to an existing plan without fuzzy matching.

    ``ValueError`` messages are stable, user-safe refusal codes for the worker.
    The source stream is never pre-cut: occurrence selection and receipt indexes
    always refer to the original timed words.
    """
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("invalid_source_duration")
    target, requested_occurrence = _validate_request(request)
    stream = _tokenize_source(words)
    hits = _matches(stream, target)
    if not hits:
        raise ValueError("phrase_not_found_in_source")
    if requested_occurrence is None:
        if len(hits) != 1:
            raise ValueError("ambiguous_phrase_occurrence")
        occurrence, (first, last) = 1, hits[0]
    else:
        if requested_occurrence > len(hits):
            raise ValueError("phrase_occurrence_not_found")
        occurrence, (first, last) = requested_occurrence, hits[requested_occurrence - 1]
    start_s, end_s, word_indexes = _selected_interval(stream, first, last)
    if end_s > duration_s + _EPS:
        raise ValueError("invalid_source_word_timing")
    _assert_no_unselected_word_overlap(
        words, start_s=start_s, end_s=end_s, selected_indexes=word_indexes
    )

    prior = _existing_removals(existing_plan, duration_s)
    if existing_plan is not None and not _same_segments(
        existing_plan.keep_segments, _keep_segments(_union_removals(prior, duration_s), duration_s)
    ):
        raise ValueError("invalid_existing_cut_plan")
    already_removed = any(
        start_s >= item.start_s - _EPS and end_s <= item.end_s + _EPS for item in prior
    )
    removals = _union_removals(
        [*prior, Removal(start_s, end_s, "explicit_speech_phrase")], duration_s
    )
    kept = _keep_segments(removals, duration_s)
    saved = sum(item.end_s - item.start_s for item in removals)
    if duration_s - saved < _MIN_OUTPUT_S - _EPS:
        raise ValueError("explicit_speech_cut_output_too_short")

    plan = replace(
        existing_plan
        if existing_plan is not None
        else CutPlan(keep_segments=[(0.0, duration_s)], removed=[], time_saved_s=0.0),
        keep_segments=kept,
        removed=removals,
        time_saved_s=saved,
    )
    receipt = ExplicitSpeechCutReceipt(
        reason=(
            "explicit_speech_phrase_already_removed"
            if already_removed
            else "explicit_speech_phrase_removed_now"
        ),
        status="applied",
        phrase=request.phrase,
        occurrence=occurrence,
        source_span={"start_s": start_s, "end_s": end_s},
        source_start_s=start_s,
        source_end_s=end_s,
        word_indexes=word_indexes,
    )
    return ExplicitSpeechCutResult(plan=plan, receipt=receipt.as_dict())
