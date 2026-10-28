"""Typed, Job-independent speech-cleanup analysis.

The preflight worker owns media acquisition and persistence.  This module owns
only the reusable analysis boundary: transcribe one already-bounded local media
artifact, obtain silence signals, run the existing V1/#960 V2 detector, and
return a fully serializable immutable result.  It imports no ORM model, Celery
task, or render orchestrator.

The detector comparison semantics intentionally match the existing render path:
V1 is always constructed first; V2 failure cannot poison that valid baseline;
``shadow`` records but never selects V2; and ``apply`` selects V2 only when its
candidate and the status-bearing silence pass are both ready.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Any, Literal, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.pipeline.caption_language import crosscheck_detected_language, infer_language_from_text
from app.pipeline.silence_cut import (
    SILENCE_CUT_VERBATIM_PROMPT,
    CutPlan,
    Removal,
    build_cut_plan,
    build_cut_plan_comparison,
    is_filler_token,
    squeezed_filler_indices,
)
from app.pipeline.speech_cleanup_alignment import AlignerUnavailable, align_words
from app.pipeline.speech_voice_guard import apply_voice_guard
from app.pipeline.transcribe import TranscribeError, Transcript, Word
from app.services.mixed_language_transcript import (
    MAX_EXTRA_PASSES,
    MixedPass,
    merge_passes,
    reference_languages,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION

MixedGapMode = Literal["off", "shadow", "apply"]
SelectedPlan = Literal["baseline", "candidate"]
CandidateStatus = Literal[
    "not_run",
    "ready",
    "build_failed",
    "validation_failed",
    "tool_unavailable",
]
SilenceDetectionStatus = Literal[
    "ok",
    "probe_failed",
    "invalid_duration",
    "no_audio",
    "ffmpeg_timeout",
    "ffmpeg_failed",
    "ffmpeg_nonzero",
    "parse_failed",
]
FindingCategory = Literal["filler", "long_pause", "retake"]
_SILENCE_DETECTION_STATUSES = frozenset(
    {
        "ok",
        "probe_failed",
        "invalid_duration",
        "no_audio",
        "ffmpeg_timeout",
        "ffmpeg_failed",
        "ffmpeg_nonzero",
        "parse_failed",
    }
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpeechCleanupTimedWord(_FrozenModel):
    text: str = Field(min_length=1)
    start_s: float = Field(ge=0, allow_inf_nan=False)
    end_s: float = Field(ge=0, allow_inf_nan=False)
    confidence: float | None = Field(default=None, allow_inf_nan=False)
    segment_avg_logprob: float | None = Field(default=None, allow_inf_nan=False)
    segment_no_speech_prob: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_interval(self) -> SpeechCleanupTimedWord:
        if self.end_s < self.start_s:
            raise ValueError("word end_s must be >= start_s")
        return self


class SpeechCleanupTimeRange(_FrozenModel):
    start_s: float = Field(ge=0, allow_inf_nan=False)
    end_s: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_interval(self) -> SpeechCleanupTimeRange:
        if self.end_s < self.start_s:
            raise ValueError("range end_s must be >= start_s")
        return self


class SpeechCleanupFinding(_FrozenModel):
    start_s: float = Field(ge=0, allow_inf_nan=False)
    end_s: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1)
    category: FindingCategory

    @model_validator(mode="after")
    def validate_interval(self) -> SpeechCleanupFinding:
        if self.end_s <= self.start_s:
            raise ValueError("finding end_s must be > start_s")
        return self


class SpeechCleanupRemovalInput(_FrozenModel):
    """Optional pre-approved removal fed into the existing plan allocator."""

    start_s: float = Field(ge=0, allow_inf_nan=False)
    end_s: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_interval(self) -> SpeechCleanupRemovalInput:
        if self.end_s <= self.start_s:
            raise ValueError("removal end_s must be > start_s")
        return self


class SerializableCutPlan(_FrozenModel):
    """JSON-safe execution subset of :class:`silence_cut.CutPlan`."""

    keep_segments: tuple[SpeechCleanupTimeRange, ...]
    removed: tuple[SpeechCleanupFinding, ...]
    time_saved_s: float = Field(ge=0, allow_inf_nan=False)
    version: int = Field(ge=1)
    bailout_reason: str | None = None
    clamped: bool = False
    proposed_removed_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    clamp_budget_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    def to_cut_plan(self) -> CutPlan:
        """Hydrate the existing renderer type without re-running detection."""

        return CutPlan(
            keep_segments=[(segment.start_s, segment.end_s) for segment in self.keep_segments],
            removed=[
                Removal(
                    start_s=finding.start_s,
                    end_s=finding.end_s,
                    reason=finding.reason,
                )
                for finding in self.removed
            ],
            time_saved_s=self.time_saved_s,
            version=self.version,
            bailout_reason=self.bailout_reason,
            clamped=self.clamped,
            proposed_removed_s=self.proposed_removed_s,
            clamp_budget_s=self.clamp_budget_s,
        )


class SpeechCleanupCategoryCounts(_FrozenModel):
    filler_sounds: int = Field(default=0, ge=0)
    long_pauses: int = Field(default=0, ge=0)
    retakes: int = Field(default=0, ge=0)


class SpeechCleanupPublicReceipt(_FrozenModel):
    """Bounded creator-safe evidence; contains no words, paths, or intervals."""

    candidate_count: int = Field(ge=0)
    category_counts: SpeechCleanupCategoryCounts
    estimated_removed_ms: int = Field(ge=0)
    # Consent needs the outcome, not just the delta: an explicit-consent plan is
    # capped only by MIN_OUTPUT_S, so a removal can be most of the take. These
    # are timing-only, exactly like estimated_removed_ms. They are optional so a
    # snapshot persisted before this field existed still hydrates on the render
    # path across a deploy; every analysis this build produces sets both.
    source_duration_ms: int | None = Field(default=None, ge=0)
    result_duration_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_durations(self) -> SpeechCleanupPublicReceipt:
        if (self.source_duration_ms is None) != (self.result_duration_ms is None):
            raise ValueError("public receipt durations must be present together")
        if (
            self.source_duration_ms is not None
            and self.result_duration_ms != self.source_duration_ms - self.estimated_removed_ms
        ):
            raise ValueError("public result duration does not match the removed estimate")
        return self


class SpeechCleanupSafetySignals(_FrozenModel):
    transcript_low_confidence: bool
    silence_detection_status: SilenceDetectionStatus
    selected_plan: SelectedPlan
    candidate_status: CandidateStatus
    bailout_reason: str | None = None
    clamped: bool = False


class SpeechCleanupAnalysisInput(_FrozenModel):
    """One exact local source/window and the policy used to analyze it."""

    source_fingerprint: str = Field(min_length=1, max_length=256)
    local_media_path: str = Field(min_length=1)
    duration_s: float = Field(gt=0, allow_inf_nan=False)
    source_window_start_s: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    source_window_end_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    detector_version: str = Field(default=DETECTOR_VERSION, min_length=1, max_length=100)
    mixed_gap_mode: MixedGapMode = "apply"
    include_silence_and_fillers: bool = True
    over_budget_policy: Literal["bailout", "clamp"] = "clamp"
    # Fraction-of-runtime cap for the clamp budget. 1.0 (the default) leaves
    # MIN_OUTPUT_S as the only rail, which is what explicit consent asks for;
    # an operator can restore a fraction cap without touching the detector.
    max_removal_frac_required: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    # Re-time filler cuts on the sound and drop a filler cut that takes a real word with it
    # (``_filler_aligned_words`` / ``_relistened_plan``). The worker passes
    # settings.speech_cleanup_filler_check_enabled; off keeps every plan as before.
    filler_check: bool = False
    retake_spans: tuple[tuple[int, int], ...] = ()
    forced_removals: tuple[SpeechCleanupRemovalInput, ...] = ()
    # Gemini's transcript of the same narration: the clip's clip_metadata
    # analysis when it has landed, else the worker's own transcript of the
    # opening (``reference_source``). An independent listener for whisper's
    # language detection — see `_crosschecked_transcript`. Never cut evidence itself.
    reference_transcript: str | None = None
    reference_source: Literal["clip_analysis", "gemini_audio"] | None = None
    # How much of the take the reference heard, in seconds (None: all of it). Gemini's own
    # transcript of the narration covers only its opening 30 s; on a mixed take no other
    # pass's words are taken from past it (see `_mixed_language_transcript`).
    reference_covers_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_window_and_spans(self) -> SpeechCleanupAnalysisInput:
        end = self.source_window_end_s
        if end is not None:
            if end <= self.source_window_start_s:
                raise ValueError("source window end must follow its start")
            if not math.isclose(
                end - self.source_window_start_s,
                self.duration_s,
                rel_tol=0,
                abs_tol=1e-3,
            ):
                raise ValueError("source window duration must match duration_s")
        if any(start < 0 or end_index < start for start, end_index in self.retake_spans):
            raise ValueError("retake spans must be non-negative inclusive index ranges")
        return self


class SpeechCleanupAnalysisResult(_FrozenModel):
    """Private authoritative snapshot plus its bounded public projection."""

    schema_version: Literal[1] = 1
    source_fingerprint: str
    detector_version: str
    source_window_start_s: float = Field(ge=0, allow_inf_nan=False)
    source_window_end_s: float = Field(gt=0, allow_inf_nan=False)
    language: str = Field(default="", max_length=32)
    timed_words: tuple[SpeechCleanupTimedWord, ...]
    cut_plan: SerializableCutPlan
    findings: tuple[SpeechCleanupFinding, ...]
    safety_signals: SpeechCleanupSafetySignals
    diagnostics: dict[str, Any]
    public_receipt: SpeechCleanupPublicReceipt

    @model_validator(mode="after")
    def validate_snapshot_consistency(self) -> SpeechCleanupAnalysisResult:
        """Reject corrupt/tampered payloads before render-side hydration."""

        duration = self.source_window_end_s - self.source_window_start_s
        if duration <= 0:
            raise ValueError("analysis source window must have positive duration")

        intervals = [
            *(("keep", item.start_s, item.end_s) for item in self.cut_plan.keep_segments),
            *(("removed", item.start_s, item.end_s) for item in self.cut_plan.removed),
        ]
        intervals.sort(key=lambda item: (item[1], item[2], item[0]))
        cursor = 0.0
        for _kind, start, end in intervals:
            if start < cursor - 1e-6 or not math.isclose(start, cursor, rel_tol=0, abs_tol=1e-6):
                raise ValueError("cut plan does not exactly partition its source window")
            if end <= start or end > duration + 1e-6:
                raise ValueError("cut plan interval is outside its source window")
            cursor = end
        if not math.isclose(cursor, duration, rel_tol=0, abs_tol=1e-6):
            raise ValueError("cut plan does not cover its source window")

        removed_s = sum(item.end_s - item.start_s for item in self.cut_plan.removed)
        if not math.isclose(
            removed_s,
            self.cut_plan.time_saved_s,
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("cut plan removed duration does not match time_saved_s")
        if any(
            finding.end_s > duration + 1e-6 or finding.category != _category(finding.reason)
            for finding in self.findings
        ):
            raise ValueError("cleanup finding is inconsistent with the source window")

        receipt = self.public_receipt
        expected_counts = {
            "filler_sounds": sum(item.category == "filler" for item in self.findings),
            "long_pauses": sum(item.category == "long_pause" for item in self.findings),
            "retakes": sum(item.category == "retake" for item in self.findings),
        }
        if receipt.candidate_count != len(self.findings):
            raise ValueError("public candidate count does not match private findings")
        if receipt.category_counts.model_dump() != expected_counts:
            raise ValueError("public category counts do not match private findings")
        if receipt.estimated_removed_ms != max(0, int(round(removed_s * 1000))):
            raise ValueError("public removed duration does not match the CutPlan")
        if receipt.source_duration_ms is not None and receipt.source_duration_ms != max(
            0, int(round(duration * 1000))
        ):
            raise ValueError("public source duration does not match the analysis window")
        return self

    def to_payload(self) -> dict[str, Any]:
        """Return the private JSONB snapshot shape."""

        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SpeechCleanupAnalysisResult:
        """Validate a persisted private snapshot before render-side use."""

        return cls.model_validate(payload)


class _TranscriptFn(Protocol):
    def __call__(
        self,
        path: str,
        *,
        language: str | None,
        verbatim_prompt: str | None,
    ) -> Any: ...


class _SilenceFn(Protocol):
    def __call__(self, path: str, *, min_silence_s: float) -> Any: ...


class _RetakeDetectFn(Protocol):
    """Map the exact normalized transcript words to abandoned-take indexes."""

    def __call__(
        self, words: Sequence[SpeechCleanupTimedWord], language: str
    ) -> Sequence[tuple[int, int]]: ...


def _default_transcribe(
    path: str,
    *,
    language: str | None,
    verbatim_prompt: str | None,
) -> Any:
    from app.pipeline.transcribe import transcribe_whisper

    return transcribe_whisper(path, language=language, verbatim_prompt=verbatim_prompt)


def _default_silence_detect(path: str, *, min_silence_s: float) -> Any:
    from app.services.clip_speech import detect_silences_with_status

    return detect_silences_with_status(path, min_silence_s=min_silence_s, ambient_adaptive=True)


def _value(record: Any, *names: str) -> Any:
    for name in names:
        value = record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)
        if value is not None:
            return value
    return None


def _timed_words(transcript: Any) -> tuple[SpeechCleanupTimedWord, ...]:
    result: list[SpeechCleanupTimedWord] = []
    for raw in _value(transcript, "words") or ():
        text = str(_value(raw, "text", "word") or "").strip()
        start = _value(raw, "start_s", "start")
        end = _value(raw, "end_s", "end")
        if not text or start is None or end is None:
            continue
        result.append(
            SpeechCleanupTimedWord(
                text=text,
                start_s=float(start),
                end_s=float(end),
                confidence=(
                    None if _value(raw, "confidence") is None else float(_value(raw, "confidence"))
                ),
                segment_avg_logprob=(
                    None
                    if _value(raw, "segment_avg_logprob") is None
                    else float(_value(raw, "segment_avg_logprob"))
                ),
                segment_no_speech_prob=(
                    None
                    if _value(raw, "segment_no_speech_prob") is None
                    else float(_value(raw, "segment_no_speech_prob"))
                ),
            )
        )
    return tuple(sorted(result, key=lambda word: (word.start_s, word.end_s)))


def _valid_detected_retake_spans(
    raw_spans: Sequence[tuple[int, int]], *, n_words: int
) -> tuple[tuple[int, int], ...]:
    """Keep only an all-valid detector result; ambiguous output cannot cut speech.

    The agent already normalizes its own response. This second boundary protects
    the pure engine's injectable seam, and makes a malformed integration fail
    open instead of passing an unsafe word index to the cut planner.
    """

    spans: list[tuple[int, int]] = []
    for span in raw_spans:
        if (
            not isinstance(span, tuple)
            or len(span) != 2
            or isinstance(span[0], bool)
            or isinstance(span[1], bool)
            or not isinstance(span[0], int)
            or not isinstance(span[1], int)
        ):
            return ()
        start, end = span
        # A kept re-delivery must follow a discarded retake. Never permit a
        # detector result to consume the transcript's final word.
        if not (0 <= start <= end < n_words - 1):
            return ()
        spans.append((start, end))
    return tuple(spans)


def _category(reason: str) -> FindingCategory:
    if reason in {"filler_lexical", "filler_acoustic"}:
        return "filler"
    if reason == "retake":
        return "retake"
    return "long_pause"


def _finding(removal: Removal) -> SpeechCleanupFinding:
    return SpeechCleanupFinding(
        start_s=float(removal.start_s),
        end_s=float(removal.end_s),
        reason=str(removal.reason),
        category=_category(str(removal.reason)),
    )


def _findings(plan: CutPlan) -> tuple[SpeechCleanupFinding, ...]:
    """Retain selected V2 atoms even when allocation merges their cut ranges.

    A tokenless filler can sit inside a larger pause component.  The executable
    CutPlan quite correctly stores that component once, often with ``silence``
    as its reason, but creator evidence must still say that the filler was
    detected.  V2's atomic dispositions are the authoritative source for those
    category counts; V1 falls back to final removal components.
    """

    diagnostics = plan.diagnostics
    if diagnostics is None:
        return tuple(_finding(removal) for removal in plan.removed)

    # ``plan.diagnostics`` describes the detector's output, not what survived after it: a
    # later stage (the filler re-listen, the voice guard) can drop or trim a removal and keep
    # the old diagnostics. An atom is evidence of a cut only while the cut is still in the
    # plan, so it must sit inside the merged surviving removals; the planner guarantees that
    # for a plan nothing has changed.
    surviving: list[list[float]] = []
    for removal in sorted(plan.removed, key=lambda item: (item.start_s, item.end_s)):
        if surviving and removal.start_s <= surviving[-1][1] + 1e-6:
            surviving[-1][1] = max(surviving[-1][1], removal.end_s)
        else:
            surviving.append([removal.start_s, removal.end_s])

    def survives(start: float, end: float) -> bool:
        return any(lo <= start + 1e-6 and end - 1e-6 <= hi for lo, hi in surviving)

    selected_dispositions = {"selected_full", "promoted_protected"}
    findings = [
        SpeechCleanupFinding(
            start_s=item.atom_start_s,
            end_s=item.atom_end_s,
            reason=item.atom_kind,
            category=_category(item.atom_kind),
        )
        for item in diagnostics.atomic_dispositions
        if item.disposition in selected_dispositions
        and survives(item.atom_start_s, item.atom_end_s)
    ]
    # Pause components do not have atomic-disposition records. Keep them as
    # separate evidence even when they geometrically contain a filler atom.
    findings.extend(_finding(removal) for removal in plan.removed if removal.reason == "silence")
    findings.sort(key=lambda item: (item.start_s, item.end_s, item.reason))
    return tuple(findings)


def _serializable_plan(plan: CutPlan) -> SerializableCutPlan:
    return SerializableCutPlan(
        keep_segments=tuple(
            SpeechCleanupTimeRange(start_s=float(start), end_s=float(end))
            for start, end in plan.keep_segments
        ),
        removed=tuple(_finding(removal) for removal in plan.removed),
        time_saved_s=float(plan.time_saved_s),
        version=int(plan.version),
        bailout_reason=plan.bailout_reason,
        clamped=bool(plan.clamped),
        proposed_removed_s=plan.proposed_removed_s,
        clamp_budget_s=plan.clamp_budget_s,
    )


def _diagnostics(plan: CutPlan, *, candidate_error_class: str | None) -> dict[str, Any]:
    diagnostics = plan.diagnostics
    if diagnostics is None:
        payload: dict[str, Any] = {}
    else:
        payload = {
            "lexical_candidates": [
                {"start_s": item.start_s, "end_s": item.end_s, "reason": item.reason}
                for item in diagnostics.lexical_candidates
            ],
            "lexical_candidates_total": diagnostics.lexical_candidates_total,
            "lexical_candidates_omitted": diagnostics.lexical_candidates_omitted,
            "acoustic_candidates": [
                {"start_s": item.start_s, "end_s": item.end_s, "reason": item.reason}
                for item in diagnostics.acoustic_candidates
            ],
            "acoustic_decisions": [
                {
                    "window_start_s": item.window_start_s,
                    "window_end_s": item.window_end_s,
                    "island_start_s": item.island_start_s,
                    "island_end_s": item.island_end_s,
                    "left_silence_s": item.left_silence_s,
                    "right_silence_s": item.right_silence_s,
                    "detection": item.detection,
                    "reason": item.reason,
                }
                for item in diagnostics.acoustic_decisions
            ],
            "acoustic_decisions_total": diagnostics.acoustic_decisions_total,
            "acoustic_decisions_omitted": diagnostics.acoustic_decisions_omitted,
            "acoustic_eligible_total": diagnostics.acoustic_eligible_total,
            "mixed_gap_decision_dispositions": [
                list(item) for item in diagnostics.mixed_gap_decision_dispositions
            ],
            "atomic_dispositions": [
                {
                    "atom_start_s": item.atom_start_s,
                    "atom_end_s": item.atom_end_s,
                    "group_start_s": item.group_start_s,
                    "group_end_s": item.group_end_s,
                    "atom_kind": item.atom_kind,
                    "priority": item.priority,
                    "disposition": item.disposition,
                }
                for item in diagnostics.atomic_dispositions
            ],
            "atomic_dispositions_total": diagnostics.atomic_dispositions_total,
            "atomic_dispositions_omitted": diagnostics.atomic_dispositions_omitted,
            "proposed_removals": [
                {"start_s": item.start_s, "end_s": item.end_s, "reason": item.reason}
                for item in diagnostics.proposed_removals
            ],
            "mixed_gap_full_total": diagnostics.mixed_gap_full_total,
            "mixed_gap_partial_total": diagnostics.mixed_gap_partial_total,
            "mixed_gap_dropped_total": diagnostics.mixed_gap_dropped_total,
            # Rule 0 (token reconciliation) evidence: timing only, no text.
            "token_adjustments": [
                {
                    "original_start_s": item.original_start_s,
                    "original_end_s": item.original_end_s,
                    "pieces": [list(piece) for piece in item.pieces],
                    "carved_s": item.carved_s,
                    "kind": item.kind,
                }
                for item in diagnostics.token_adjustments
            ],
            "token_adjustments_total": diagnostics.token_adjustments_total,
            "token_adjustments_omitted": diagnostics.token_adjustments_omitted,
            "token_carved_s": diagnostics.token_carved_s,
        }
    if candidate_error_class:
        payload["candidate_error_class"] = candidate_error_class[:160]
    return payload


def _public_receipt(
    findings: Sequence[SpeechCleanupFinding],
    *,
    time_saved_s: float,
    source_duration_s: float,
) -> SpeechCleanupPublicReceipt:
    filler = sum(finding.category == "filler" for finding in findings)
    pauses = sum(finding.category == "long_pause" for finding in findings)
    retakes = sum(finding.category == "retake" for finding in findings)
    # Findings can overlap when a detected filler lies inside a pause cut.
    # The executable plan is the sole source for actual removed duration.
    removed_ms = max(0, int(round(time_saved_s * 1000)))
    source_ms = max(0, int(round(source_duration_s * 1000)))
    return SpeechCleanupPublicReceipt(
        candidate_count=len(findings),
        category_counts=SpeechCleanupCategoryCounts(
            filler_sounds=filler,
            long_pauses=pauses,
            retakes=retakes,
        ),
        estimated_removed_ms=removed_ms,
        source_duration_ms=source_ms,
        # Deliberately not clamped: a plan removing more than its own window is
        # corrupt, and ge=0 must reject it here rather than round it away.
        result_duration_ms=source_ms - removed_ms,
    )


# How a transcription failure is told from a bug. The task adapter classifies the first pass with
# these (`_run_engine_once`); an optional extra pass that fails like this is skipped, anything
# else escapes.
PROGRAMMING_ERRORS = (
    AssertionError,
    AttributeError,
    ImportError,
    KeyError,
    NameError,
    TypeError,
    ValueError,
)
TRANSCRIPTION_PROVIDER_MODULES = frozenset({"httpcore", "httpx", "openai", "requests", "urllib3"})
# A receipt lists at most this many stretches (a long take can change language often).
_MAX_RECEIPT_SEGMENTS = 50


def _optional_pass_unavailable(exc: Exception) -> bool:
    """True when ``exc`` is a transcription/provider failure (skip the pass), False for a bug."""

    if isinstance(exc, PROGRAMMING_ERRORS):
        return False
    if isinstance(exc, TranscribeError | TimeoutError | OSError):
        return True
    return type(exc).__module__.partition(".")[0] in TRANSCRIPTION_PROVIDER_MODULES


def _word_rows(transcript: Any) -> list[dict[str, Any]]:
    return [word.model_dump() for word in _timed_words(transcript)]


def _mixed_language_transcript(
    transcript: Any,
    analysis_input: SpeechCleanupAnalysisInput,
    transcribe: _TranscriptFn,
    receipt: dict[str, Any],
    languages: list[str],
) -> tuple[Any, dict[str, Any]]:
    """Per-stretch transcript of a take whose reference speaks several languages (KRI-843).

    No single whisper language is right for such a take (an English question, a Spanish answer
    and Turkish chatter in one clip): auto-detect translates two of the three, and the EN/TR
    cross-check can only force one language over all of it. So the take is transcribed once more
    in each other language the reference shows (at most `MAX_EXTRA_PASSES`, same verbatim
    prompt) and `merge_passes` takes, stretch by stretch, the pass the reference confirms. The
    first pass stands wherever no other pass replaces it: a stretch nothing confirms is NOT
    dropped, because wordless sound reads as a pause to the cleanup cut, which removes it.
    An extra pass that fails to transcribe or hears nothing is skipped (the receipt lists
    ``failed`` and ``heard``); when no pass confirms anything the first pass stands untouched.
    """

    first_language = str(receipt["whisper_language"]).lower()
    wanted = [language for language in languages if language != first_language]
    wanted = wanted[:MAX_EXTRA_PASSES]
    extras: list[MixedPass] = []
    failed: list[str] = []
    for language in wanted:
        try:
            again = transcribe(
                analysis_input.local_media_path,
                language=language,
                verbatim_prompt=SILENCE_CUT_VERBATIM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 - only a transcription failure skips the pass
            if not _optional_pass_unavailable(exc):
                raise
            failed.append(language)
            continue
        rows = _word_rows(again)
        if rows:
            extras.append(MixedPass(language, rows))
    receipt = {
        **receipt,
        "mixed": True,
        "reference_languages": languages,
        # Asked for / returned words / raised a transcription or provider error.
        "tried": wanted,
        "heard": [extra.language for extra in extras],
        "failed": failed,
    }
    merged = (
        merge_passes(
            MixedPass(first_language, _word_rows(transcript)),
            extras,
            str(analysis_input.reference_transcript),
            duration_s=analysis_input.duration_s,
            covers_s=analysis_input.reference_covers_s,
        )
        if extras
        else None
    )
    if merged is None:
        return transcript, {**receipt, "segments": []}
    words = [
        Word(
            text=row["text"],
            start_s=row["start_s"],
            end_s=row["end_s"],
            confidence=1.0 if row["confidence"] is None else row["confidence"],
            segment_avg_logprob=row["segment_avg_logprob"],
            segment_no_speech_prob=row["segment_no_speech_prob"],
        )
        for row in merged.words
    ]
    combined = Transcript(
        words=words,
        full_text=" ".join(word.text for word in words),
        low_confidence=bool(_value(transcript, "low_confidence") or False),
        language=merged.language[:32],
    )
    segments = list(merged.segments)[:_MAX_RECEIPT_SEGMENTS]
    return combined, {**receipt, "applied": True, "segments": segments}


def _crosschecked_transcript(
    transcript: Any,
    analysis_input: SpeechCleanupAnalysisInput,
    transcribe: _TranscriptFn,
) -> tuple[Any, dict[str, Any] | None]:
    """Re-transcribe when whisper's detected language contradicts Gemini's.

    whisper-1 detects the language from the audio alone and misreads accented
    speech (Turkish-accented English -> "tr"), then TRANSLATES instead of
    transcribing. Filler and pause decisions made on a translation cut the wrong
    audio, so on a clear EN/TR disagreement with ``reference_transcript`` the
    clip is transcribed again in the language Gemini heard; an empty second pass
    keeps the first. A reference that speaks two or more languages is a mixed
    take, where one language cannot be right: it is merged per stretch instead
    (`_mixed_language_transcript`). Returns the transcript to analyze plus a
    receipt, recorded whenever a reference was available (it tells a later
    reschedule that this run already had one, see ``speech_cleanup_preflight``);
    None without one.
    """

    reference = (analysis_input.reference_transcript or "").strip()
    if not reference:
        return transcript, None
    whisper_language = str(_value(transcript, "language") or "")[:32]
    receipt: dict[str, Any] = {
        "whisper_language": whisper_language,
        "reference_language": infer_language_from_text(reference),
        "applied": False,
    }
    if analysis_input.reference_source is not None:
        receipt["reference_source"] = analysis_input.reference_source
    if settings.speech_cleanup_mixed_language_enabled:
        languages = reference_languages(reference)
        if len(languages) >= 2 and _timed_words(transcript):
            return _mixed_language_transcript(
                transcript, analysis_input, transcribe, receipt, languages
            )
    heard_language = crosscheck_detected_language(whisper_language, reference_text=reference)
    if heard_language is None:
        return transcript, receipt
    retranscribed = transcribe(
        analysis_input.local_media_path,
        language=heard_language,
        verbatim_prompt=SILENCE_CUT_VERBATIM_PROMPT,
    )
    if not _timed_words(retranscribed):
        return transcript, receipt
    return retranscribed, {**receipt, "applied": True}


# ---------------------------------------------------------------------------------
# Filler check: where a filler was said, and what its cut takes with it
# ---------------------------------------------------------------------------------
# whisper-1 can stamp a whole stretch late (Merope café, KRI-790: every word from 3.8 s to 12 s
# sits 0.3-0.5 s after its sound), so rule 1 cut "we are" and "tell" and left the "uh"/"um" in.
# ``filler_check`` re-times the words with a forced aligner before planning, then re-listens around
# every filler cut and drops a cut that takes a real word with it.
FILLER_ALIGN_MIN_ROOM_S = 0.12  # audio a filler needs between its aligned real neighbours
FILLER_ALIGN_WORD_MARGIN_S = 0.06  # CTC word edges are tight; without this a cut clips a word end
FILLER_ALIGN_MIN_SCORE = 0.15  # median word confidence under this: the alignment is not trusted
RELISTEN_PAD_S = 1.5  # audio kept on each side of a cut when re-listening
RELISTEN_JUDGE_S = 0.5  # only words this close to the cut are judged (farther ones are half-heard)
RELISTEN_MATCH_RATIO = 0.75  # a judged word survives when the cut window has a word this similar
RELISTEN_MAX_CUTS = 8  # bounds the paid transcription: two short calls per checked cut
_RELISTEN_WORKERS = 4
_RELISTEN_FFMPEG_TIMEOUT_S = 60
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "twenty": "20",
}  # fmt: skip


def _filler_aligned_words(
    words: tuple[SpeechCleanupTimedWord, ...],
    *,
    audio_path: str,
    duration_s: float,
    language: str,
) -> tuple[tuple[SpeechCleanupTimedWord, ...], dict[str, Any]]:
    """The words re-timed on the sound for planning, or ``words`` unchanged and why.

    Only the times change: the same tokens in the same order, so retake spans (word indices)
    and the stored transcript stay consistent with the plan built from them.
    """

    report: dict[str, Any] = {"aligned": False}
    if not any(is_filler_token(word.text) for word in words):
        report["reason"] = "no_fillers"
        return words, report
    if not language.lower().startswith("en"):
        report["reason"] = "not_english"  # the aligner is an English-only model
        return words, report
    if squeezed_filler_indices(words):
        # KRI-970: a filler stamped over a squeezed word is not there. Re-timing would give the
        # word its real length, hide that evidence, and put the cut on "the" instead.
        report["reason"] = "squeezed_filler"
        return words, report
    try:
        aligned = align_words(
            audio_path,
            [word.text for word in words],
            model_dir=settings.speech_cleanup_aligner_model_dir,
        )
    except AlignerUnavailable:
        report["reason"] = "aligner_unavailable"
        return words, report
    except (RuntimeError, OSError) as exc:  # AlignmentFailed, torch runtime errors
        report["reason"] = f"aligner_error:{type(exc).__name__}"[:64]
        return words, report
    report["median_score"] = round(aligned.median_score, 3)
    if aligned.median_score < FILLER_ALIGN_MIN_SCORE:
        report["reason"] = "low_alignment_score"
        return words, report

    is_filler = [is_filler_token(word.text) for word in words]
    spans: list[tuple[float, float] | None] = list(aligned.spans)
    real = [i for i, span in enumerate(spans) if span is not None and not is_filler[i]]
    for n, i in enumerate(real):
        start, end = spans[i]  # type: ignore[misc]
        lo = spans[real[n - 1]][1] if n else 0.0  # type: ignore[index]
        hi = spans[real[n + 1]][0] if n + 1 < len(real) else duration_s  # type: ignore[index]
        spans[i] = (
            min(start, max(start - FILLER_ALIGN_WORD_MARGIN_S, (lo + start) / 2)),
            max(end, min(end + FILLER_ALIGN_WORD_MARGIN_S, (end + hi) / 2)),
        )
    for i in range(len(words)):  # a token with no letters ("20") sits in the gap around it
        if spans[i] is None and not is_filler[i]:
            lo = next((spans[j][1] for j in range(i - 1, -1, -1) if spans[j]), 0.0)  # type: ignore[index]
            hi = next(
                (spans[j][0] for j in range(i + 1, len(words)) if spans[j] and not is_filler[j]), lo
            )  # type: ignore[index]
            spans[i] = (lo, max(lo, hi))
    moved: list[list[float]] = []
    for i, word in enumerate(words):
        if not is_filler[i]:
            continue
        lo = next((spans[j][1] for j in range(i - 1, -1, -1) if not is_filler[j]), 0.0)  # type: ignore[index]
        hi = next((spans[j][0] for j in range(i + 1, len(words)) if not is_filler[j]), duration_s)  # type: ignore[index]
        if hi - lo < FILLER_ALIGN_MIN_ROOM_S:
            report["reason"] = "filler_without_room"
            return words, report
        spans[i] = (lo + 0.02, hi - 0.02)
        moved.append([round(word.start_s, 2), round(lo + 0.02, 2)])
    starts = [span[0] for span in spans]  # type: ignore[index]
    if any(b < a - 1e-6 for a, b in zip(starts, starts[1:], strict=False)) or any(
        span[1] < span[0] or span[1] > duration_s + 1e-6
        for span in spans  # type: ignore[index]
    ):
        report["reason"] = "non_monotonic"
        return words, report
    report.update(aligned=True, reason="aligned", fillers_moved=moved[:20])
    retimed = tuple(
        word.model_copy(update={"start_s": round(span[0], 3), "end_s": round(span[1], 3)})  # type: ignore[index]
        for word, span in zip(words, spans, strict=True)
    )
    return retimed, report


def _relisten_key(text: str) -> str:
    key = re.sub(r"[^\w']", "", text.lower())
    return _NUMBER_WORDS.get(key, key)


def _window_words(
    audio_path: str,
    start: float,
    end: float,
    cut: tuple[float, float] | None,
    *,
    language: str,
    transcribe: _TranscriptFn,
    workdir: str,
) -> list[tuple[str, float]]:
    """(key, middle) of every word heard in ``[start, end)`` of the source, minus ``cut``."""

    if cut is None:
        graph = f"[0:a]atrim={start:.3f}:{end:.3f},asetpts=PTS-STARTPTS[o]"
    else:
        graph = (
            f"[0:a]atrim={start:.3f}:{cut[0]:.3f},asetpts=PTS-STARTPTS[x];"
            f"[0:a]atrim={cut[1]:.3f}:{end:.3f},asetpts=PTS-STARTPTS[y];[x][y]concat=n=2:v=0:a=1[o]"
        )
    path = os.path.join(workdir, f"{start:.3f}-{'cut' if cut else 'orig'}.wav")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            audio_path,
            "-filter_complex",
            graph,
            "-map",
            "[o]",
            "-ac",
            "1",
            "-ar",
            "16000",
            path,
        ],
        capture_output=True,
        check=True,
        timeout=_RELISTEN_FFMPEG_TIMEOUT_S,
    )
    transcript = transcribe(
        path, language=language or None, verbatim_prompt=SILENCE_CUT_VERBATIM_PROMPT
    )
    heard = ((_relisten_key(w.text), (w.start_s + w.end_s) / 2) for w in _timed_words(transcript))
    return [(key, middle) for key, middle in heard if key]


def _words_lost(
    original: list[tuple[str, float]], after_cut: list[tuple[str, float]], lo: float, hi: float
) -> list[str]:
    """Real words heard near the cut in ``original`` with no close match left in ``after_cut``.

    Fillers are never judged: the verbatim prompt makes whisper write a real "um" as "um", and
    removing it is the point of the cut.
    """

    pool = [key for key, _ in after_cut if not is_filler_token(key)]
    lost: list[str] = []
    for key, middle in original:
        if is_filler_token(key) or not lo <= middle <= hi:
            continue
        best = max(pool, key=lambda other: SequenceMatcher(a=key, b=other).ratio(), default=None)
        if best is None or SequenceMatcher(a=key, b=best).ratio() < RELISTEN_MATCH_RATIO:
            lost.append(key)
        else:
            pool.remove(best)
    return lost


def _relistened_plan(
    plan: CutPlan,
    words: Sequence[SpeechCleanupTimedWord],
    *,
    audio_path: str,
    duration_s: float,
    language: str,
    transcribe: _TranscriptFn,
) -> tuple[CutPlan, dict[str, Any]]:
    """``plan`` without the filler cuts that take a real word with them.

    Dropping a removal only grows the keep segments around it, so no other cut moves, no
    micro-fragment appears and the budget can only loosen.
    """

    fillers = [(w.start_s, w.end_s) for w in words if is_filler_token(w.text)]
    targets = [
        removal
        for removal in plan.removed
        if removal.reason.startswith("filler")
        or any(a < removal.end_s and b > removal.start_s for a, b in fillers)
    ]
    report: dict[str, Any] = {
        "checked": 0,
        "dropped": [],
        "unchecked": max(0, len(targets) - RELISTEN_MAX_CUTS),
    }
    if not targets:
        return plan, report

    def check(removal: Removal) -> tuple[Removal, list[str] | None]:
        start = max(0.0, removal.start_s - RELISTEN_PAD_S)
        end = min(duration_s, removal.end_s + RELISTEN_PAD_S)
        kwargs = {"language": language, "transcribe": transcribe, "workdir": workdir}
        try:
            original = _window_words(audio_path, start, end, None, **kwargs)
            after = _window_words(
                audio_path, start, end, (removal.start_s, removal.end_s), **kwargs
            )
        except subprocess.SubprocessError:
            return removal, None
        except Exception as exc:
            if not _optional_pass_unavailable(exc):
                raise
            return removal, None  # unheard: keep the cut, as before this check
        lo = removal.start_s - start - RELISTEN_JUDGE_S
        hi = removal.end_s - start + RELISTEN_JUDGE_S
        return removal, _words_lost(original, after, lo, hi)

    with tempfile.TemporaryDirectory(prefix="speech-relisten-") as workdir:
        with ThreadPoolExecutor(max_workers=_RELISTEN_WORKERS) as pool:
            verdicts = list(pool.map(check, targets[:RELISTEN_MAX_CUTS]))
    drop: list[Removal] = []
    for removal, lost in verdicts:
        if lost is None:
            report["unchecked"] += 1
            continue
        report["checked"] += 1
        if lost:
            drop.append(removal)
            report["dropped"].append(
                {"start_s": removal.start_s, "end_s": removal.end_s, "lost": lost[:6]}
            )
    if not drop:
        return plan, report
    removed = [removal for removal in plan.removed if removal not in drop]
    spans = sorted([*plan.keep_segments, *((r.start_s, r.end_s) for r in drop)])
    keep: list[tuple[float, float]] = []
    for start, end in spans:
        if keep and start <= keep[-1][1] + 1e-9:
            keep[-1] = (keep[-1][0], max(keep[-1][1], end))
        else:
            keep.append((start, end))
    return (
        replace(
            plan,
            removed=removed,
            keep_segments=keep,
            time_saved_s=sum(r.end_s - r.start_s for r in removed),
        ),
        report,
    )


def analyze_speech_cleanup(
    analysis_input: SpeechCleanupAnalysisInput,
    *,
    transcribe_fn: _TranscriptFn | None = None,
    silence_detect_fn: _SilenceFn | None = None,
    retake_detect_fn: _RetakeDetectFn | None = None,
    pcm_loader: Callable[[str], np.ndarray] | None = None,
) -> SpeechCleanupAnalysisResult:
    """Analyze one exact, already-bounded local narration artifact.

    Known media/provider failures deliberately remain visible to the task
    adapter; this function does not broadly catch exceptions.  In particular,
    programming errors escape unchanged instead of becoming ``no_findings``.

    ``pcm_loader`` decodes the artifact for the audible-speech guard (default
    ``audio_pcm.decode_pcm_f32``); tests inject it so a placeholder path need not be media.
    """

    transcribe = transcribe_fn or _default_transcribe
    detect_silences = silence_detect_fn or _default_silence_detect
    transcript = transcribe(
        analysis_input.local_media_path,
        language=None,
        verbatim_prompt=SILENCE_CUT_VERBATIM_PROMPT,
    )
    transcript, language_crosscheck = _crosschecked_transcript(
        transcript, analysis_input, transcribe
    )
    words = _timed_words(transcript)
    detected_retakes: tuple[tuple[int, int], ...] = ()
    if retake_detect_fn is not None:
        try:
            detected_retakes = _valid_detected_retake_spans(
                retake_detect_fn(words, str(_value(transcript, "language") or "")),
                n_words=len(words),
            )
        except Exception:  # Detector failures must never block base cleanup.
            detected_retakes = ()
    silence_result = detect_silences(
        analysis_input.local_media_path,
        min_silence_s=0.1,
    )
    silence_status = str(_value(silence_result, "status") or "parse_failed")
    if silence_status not in _SILENCE_DETECTION_STATUSES:
        silence_status = "parse_failed"
    language = str(_value(transcript, "language") or "")[:32]
    filler_report: dict[str, Any] | None = None
    if analysis_input.filler_check:
        words, filler_report = _filler_aligned_words(
            words,
            audio_path=analysis_input.local_media_path,
            duration_s=analysis_input.duration_s,
            language=language,
        )
    silences = tuple(_value(silence_result, "spans") or ())

    forced = tuple(
        {
            "start_s": finding.start_s,
            "end_s": finding.end_s,
            "reason": finding.reason,
        }
        for finding in analysis_input.forced_removals
    )
    selected_plan: SelectedPlan = "baseline"
    candidate_status: CandidateStatus = "not_run"
    candidate_error_class: str | None = None
    diagnostic_plan: CutPlan
    if analysis_input.mixed_gap_mode == "off":
        plan = build_cut_plan(
            words,
            silences,
            analysis_input.duration_s,
            retake_spans=(*analysis_input.retake_spans, *detected_retakes),
            forced_removals=forced,
            include_silence_and_fillers=analysis_input.include_silence_and_fillers,
            over_budget_policy=analysis_input.over_budget_policy,
            max_removal_frac_required=analysis_input.max_removal_frac_required,
        )
        diagnostic_plan = plan
    else:
        comparison = build_cut_plan_comparison(
            words,
            silences,
            analysis_input.duration_s,
            retake_spans=(*analysis_input.retake_spans, *detected_retakes),
            forced_removals=forced,
            include_silence_and_fillers=analysis_input.include_silence_and_fillers,
            over_budget_policy=analysis_input.over_budget_policy,
            max_removal_frac_required=analysis_input.max_removal_frac_required,
        )
        candidate_status = comparison.candidate_status
        candidate_error_class = comparison.candidate_error_class
        diagnostic_plan = comparison.candidate or comparison.baseline
        if silence_status != "ok":
            candidate_status = "tool_unavailable"
        # A candidate that bailed out is a no-op plan: preferring it over a
        # baseline that still has cuts would ship ZERO cleanup (rule 0 exposes
        # more dead air, so the bailout policy's 40% rail trips more often).
        candidate_is_no_op_regression = (
            comparison.candidate is not None
            and comparison.candidate.bailout_reason is not None
            and comparison.baseline.bailout_reason is None
            and bool(comparison.baseline.removed)
        )
        if (
            analysis_input.mixed_gap_mode == "apply"
            and candidate_status == "ready"
            and comparison.candidate is not None
            and not candidate_is_no_op_regression
        ):
            plan = comparison.candidate
            selected_plan = "candidate"
        else:
            plan = comparison.baseline

    # KRI-1017: a silence/filler cut that holds sustained voicing is not dead air, whatever the
    # transcript and the silence spans agreed on. Runs before the re-listen so it never spends
    # a paid transcription on a cut it is about to drop anyway.
    plan, voice_guard_report = apply_voice_guard(
        plan,
        analysis_input.local_media_path,
        words=words,
        retake_spans=(*analysis_input.retake_spans, *detected_retakes),
        forced=forced,
        pcm_loader=pcm_loader,
    )

    if filler_report is not None:
        plan, filler_report["relisten"] = _relistened_plan(
            plan,
            words,
            audio_path=analysis_input.local_media_path,
            duration_s=analysis_input.duration_s,
            language=language,
            transcribe=transcribe,
        )
    serializable_plan = _serializable_plan(plan)
    findings = _findings(plan)
    window_end = (
        analysis_input.source_window_end_s
        if analysis_input.source_window_end_s is not None
        else analysis_input.source_window_start_s + analysis_input.duration_s
    )
    diagnostics = _diagnostics(diagnostic_plan, candidate_error_class=candidate_error_class)
    if language_crosscheck is not None:
        diagnostics["language_crosscheck"] = language_crosscheck
    if (
        voice_guard_report["status"] != "ok"
        or voice_guard_report["dropped"]
        or voice_guard_report["trimmed"]
    ):
        # Only when it acted: a clean pass leaves the diagnostics as they were (the off-mode
        # contract is an empty payload).
        diagnostics["voice_guard"] = voice_guard_report
    if filler_report is not None:
        diagnostics["filler_check"] = filler_report
    return SpeechCleanupAnalysisResult(
        source_fingerprint=analysis_input.source_fingerprint,
        detector_version=analysis_input.detector_version,
        source_window_start_s=analysis_input.source_window_start_s,
        source_window_end_s=window_end,
        language=language,
        timed_words=words,
        cut_plan=serializable_plan,
        findings=findings,
        safety_signals=SpeechCleanupSafetySignals(
            transcript_low_confidence=bool(_value(transcript, "low_confidence") or False),
            silence_detection_status=silence_status,
            selected_plan=selected_plan,
            candidate_status=candidate_status,
            bailout_reason=plan.bailout_reason,
            clamped=bool(plan.clamped),
        ),
        diagnostics=diagnostics,
        public_receipt=_public_receipt(
            findings,
            time_saved_s=plan.time_saved_s,
            source_duration_s=window_end - analysis_input.source_window_start_s,
        ),
    )
