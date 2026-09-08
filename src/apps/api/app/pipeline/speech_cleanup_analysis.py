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
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.pipeline.silence_cut import (
    SILENCE_CUT_VERBATIM_PROMPT,
    CutPlan,
    Removal,
    build_cut_plan,
    build_cut_plan_comparison,
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
    retake_spans: tuple[tuple[int, int], ...] = ()
    forced_removals: tuple[SpeechCleanupRemovalInput, ...] = ()

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

    return detect_silences_with_status(path, min_silence_s=min_silence_s)


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


def analyze_speech_cleanup(
    analysis_input: SpeechCleanupAnalysisInput,
    *,
    transcribe_fn: _TranscriptFn | None = None,
    silence_detect_fn: _SilenceFn | None = None,
) -> SpeechCleanupAnalysisResult:
    """Analyze one exact, already-bounded local narration artifact.

    Known media/provider failures deliberately remain visible to the task
    adapter; this function does not broadly catch exceptions.  In particular,
    programming errors escape unchanged instead of becoming ``no_findings``.
    """

    transcribe = transcribe_fn or _default_transcribe
    detect_silences = silence_detect_fn or _default_silence_detect
    transcript = transcribe(
        analysis_input.local_media_path,
        language=None,
        verbatim_prompt=SILENCE_CUT_VERBATIM_PROMPT,
    )
    words = _timed_words(transcript)
    silence_result = detect_silences(
        analysis_input.local_media_path,
        min_silence_s=0.1,
    )
    silence_status = str(_value(silence_result, "status") or "parse_failed")
    if silence_status not in _SILENCE_DETECTION_STATUSES:
        silence_status = "parse_failed"
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
            retake_spans=analysis_input.retake_spans,
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
            retake_spans=analysis_input.retake_spans,
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

    serializable_plan = _serializable_plan(plan)
    findings = _findings(plan)
    window_end = (
        analysis_input.source_window_end_s
        if analysis_input.source_window_end_s is not None
        else analysis_input.source_window_start_s + analysis_input.duration_s
    )
    language = str(_value(transcript, "language") or "")[:32]
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
        diagnostics=_diagnostics(diagnostic_plan, candidate_error_class=candidate_error_class),
        public_receipt=_public_receipt(
            findings,
            time_saved_s=plan.time_saved_s,
            source_duration_s=window_end - analysis_input.source_window_start_s,
        ),
    )
