"""Bounded terminal outcomes for required speech-cleanup renders.

Analysis receipts describe what the detector proposed.  This module records the
separate authoritative answer: which correlated render generation was published,
failed, cancelled, superseded, or rolled back.  The locked append helper mutates an
already row-locked ``Job`` so the outcome and the state transition commit together.

Only scalar identifiers, enums, counts, and timings are accepted.  Speech text,
paths, URLs, exception messages, and arbitrary metadata have no input channel.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from app.services.pipeline_trace import sanitize_speech_cleanup_trace_payload

SpeechCleanupRenderOutcomeName = Literal[
    # Transaction-oriented names used by terminal coordinators.
    "published_candidate",
    "published_baseline",
    "failed_before_publish",
    "cancelled",
    "superseded",
    "restored_last_good",
    # More detailed names from the rollout/debug contract.  Keeping them in the
    # same schema lets callers preserve the strongest fact they can prove.
    "published_applied",
    "published_no_change",
    "published_baseline_fallback",
    "discarded_superseded",
    "discarded_finalization_rejected",
    "failed_owned",
    "cancelled_owned",
]
SpeechCleanupAnalysisView = Literal["full_clip", "talking_head_spine_capped"]
SpeechCleanupSelectedPlan = Literal["candidate", "baseline"]
SpeechCleanupOutcomeAppendStatus = Literal["persisted", "dropped_cap", "error"]

_OUTCOMES = {
    "published_candidate",
    "published_baseline",
    "failed_before_publish",
    "cancelled",
    "superseded",
    "restored_last_good",
    "published_applied",
    "published_no_change",
    "published_baseline_fallback",
    "discarded_superseded",
    "discarded_finalization_rejected",
    "failed_owned",
    "cancelled_owned",
}
_ANALYSIS_VIEWS = {"full_clip", "talking_head_spine_capped"}
_SELECTED_PLANS = {"candidate", "baseline"}
_PUBLISHED_CANDIDATE_OUTCOMES = {"published_candidate", "published_applied"}
_PUBLISHED_BASELINE_OUTCOMES = {
    "published_baseline",
    "published_baseline_fallback",
}
_FAILURE_CONTEXT_OUTCOMES = {
    "failed_before_publish",
    "failed_owned",
    "restored_last_good",
}

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_LOWER_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]*$")
_EXCEPTION_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SOURCE_TAG_RE = re.compile(r"^[0-9a-f]{16}$")
_OUTCOME_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_SCHEMA_VERSION = 1
_EVENT_STAGE = "silence_cut"
_EVENT_NAME = "speech_cleanup_render_outcome"
_MAX_TRACE_EVENTS = 500
_MAX_IDENTIFIER_LEN = 128
_MAX_ENUM_LEN = 64
_MAX_EXCEPTION_CLASS_LEN = 96
_MAX_REMOVAL_COUNT = 10_000
_MAX_REMOVED_MS = 86_400_000

_PAYLOAD_KEYS = {
    "schema_version",
    "outcome_id",
    "outcome",
    "analysis_attempt_id",
    "analysis_view",
    "detector_version",
    "source_tag",
    "variant_id",
    "render_generation_id",
    "selected_plan",
    "candidate_status",
    "output_removal_count",
    "output_removed_ms",
    "failure_phase",
    "failure_class",
}


class _TraceJob(Protocol):
    pipeline_trace: list[Any] | None


def _bounded_token(value: str, *, field: str, max_len: int = _MAX_IDENTIFIER_LEN) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{field} must be a non-empty bounded token")
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters")
    return value


def _optional_lower_token(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_ENUM_LEN:
        raise ValueError(f"{field} must be a bounded enum")
    if not _LOWER_TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters")
    return value


def _optional_non_negative_int(
    value: int | None,
    *,
    field: str,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{field} must be a bounded non-negative integer")
    return value


def speech_cleanup_outcome_id(
    *,
    analysis_attempt_id: str,
    variant_id: str,
    render_generation_id: str,
    analysis_view: SpeechCleanupAnalysisView,
    detector_version: str,
) -> str:
    """Return the deterministic per-generation terminal-event identity.

    Per the observability contract, the hash deliberately excludes the outcome,
    source tag, timings, and failure metadata.  A correlated generation may have
    exactly one terminal disposition; retries therefore deduplicate even if a
    caller supplies different incidental diagnostics.
    """

    attempt = _bounded_token(analysis_attempt_id, field="analysis_attempt_id")
    variant = _bounded_token(variant_id, field="variant_id")
    generation = _bounded_token(render_generation_id, field="render_generation_id")
    detector = _bounded_token(
        detector_version,
        field="detector_version",
        max_len=_MAX_ENUM_LEN,
    )
    if analysis_view not in _ANALYSIS_VIEWS:
        raise ValueError("analysis_view is not supported")
    canonical = json.dumps(
        [attempt, variant, generation, analysis_view, detector],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"speech-cleanup-render-outcome-v1:{canonical}".encode()).hexdigest()
    return digest[:32]


def build_speech_cleanup_render_outcome(
    *,
    outcome: SpeechCleanupRenderOutcomeName,
    analysis_attempt_id: str,
    analysis_view: SpeechCleanupAnalysisView,
    detector_version: str,
    variant_id: str,
    render_generation_id: str,
    source_tag: str | None = None,
    selected_plan: SpeechCleanupSelectedPlan | None = None,
    candidate_status: str | None = None,
    output_removal_count: int | None = None,
    output_removed_ms: int | None = None,
    failure_phase: str | None = None,
    failure_class: str | None = None,
) -> dict[str, Any]:
    """Build and validate one scalar-only terminal payload.

    Build this before entering the terminal transaction.  Validation errors are
    programming/data errors; the locked append helper independently revalidates
    and converts them to ``"error"`` so publication never depends on telemetry.
    """

    if outcome not in _OUTCOMES:
        raise ValueError("outcome is not supported")
    attempt = _bounded_token(analysis_attempt_id, field="analysis_attempt_id")
    variant = _bounded_token(variant_id, field="variant_id")
    generation = _bounded_token(render_generation_id, field="render_generation_id")
    detector = _bounded_token(
        detector_version,
        field="detector_version",
        max_len=_MAX_ENUM_LEN,
    )
    if analysis_view not in _ANALYSIS_VIEWS:
        raise ValueError("analysis_view is not supported")
    if source_tag is not None and not _SOURCE_TAG_RE.fullmatch(source_tag):
        raise ValueError("source_tag must be a 16-character lowercase hex tag")
    if selected_plan is not None and selected_plan not in _SELECTED_PLANS:
        raise ValueError("selected_plan is not supported")
    if outcome in _PUBLISHED_CANDIDATE_OUTCOMES and selected_plan != "candidate":
        raise ValueError("candidate publication must select the candidate plan")
    if outcome in _PUBLISHED_BASELINE_OUTCOMES and selected_plan != "baseline":
        raise ValueError("baseline publication must select the baseline plan")

    status = _optional_lower_token(candidate_status, field="candidate_status")
    phase = _optional_lower_token(failure_phase, field="failure_phase")
    if failure_class is not None:
        if (
            not isinstance(failure_class, str)
            or not failure_class
            or len(failure_class) > _MAX_EXCEPTION_CLASS_LEN
            or not _EXCEPTION_CLASS_RE.fullmatch(failure_class)
        ):
            raise ValueError("failure_class must be a bounded exception class")
    if outcome not in _FAILURE_CONTEXT_OUTCOMES and (
        phase is not None or failure_class is not None
    ):
        raise ValueError("failure context is only valid on failure/restore outcomes")

    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "outcome_id": speech_cleanup_outcome_id(
            analysis_attempt_id=attempt,
            variant_id=variant,
            render_generation_id=generation,
            analysis_view=analysis_view,
            detector_version=detector,
        ),
        "outcome": outcome,
        "analysis_attempt_id": attempt,
        "analysis_view": analysis_view,
        "detector_version": detector,
        "source_tag": source_tag,
        "variant_id": variant,
        "render_generation_id": generation,
        "selected_plan": selected_plan,
        "candidate_status": status,
        "output_removal_count": _optional_non_negative_int(
            output_removal_count,
            field="output_removal_count",
            maximum=_MAX_REMOVAL_COUNT,
        ),
        "output_removed_ms": _optional_non_negative_int(
            output_removed_ms,
            field="output_removed_ms",
            maximum=_MAX_REMOVED_MS,
        ),
        "failure_phase": phase,
        "failure_class": failure_class,
    }
    safe = sanitize_speech_cleanup_trace_payload(payload)
    if any(isinstance(item, (dict, list, tuple)) for item in safe.values()):
        raise ValueError("terminal outcome payload must contain scalar values only")
    return safe


def _normalize_built_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _PAYLOAD_KEYS:
        raise ValueError("terminal outcome payload has an invalid shape")
    normalized = build_speech_cleanup_render_outcome(
        outcome=payload["outcome"],
        analysis_attempt_id=payload["analysis_attempt_id"],
        analysis_view=payload["analysis_view"],
        detector_version=payload["detector_version"],
        variant_id=payload["variant_id"],
        render_generation_id=payload["render_generation_id"],
        source_tag=payload["source_tag"],
        selected_plan=payload["selected_plan"],
        candidate_status=payload["candidate_status"],
        output_removal_count=payload["output_removal_count"],
        output_removed_ms=payload["output_removed_ms"],
        failure_phase=payload["failure_phase"],
        failure_class=payload["failure_class"],
    )
    if payload["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("terminal outcome schema version is not supported")
    if not isinstance(payload["outcome_id"], str) or not _OUTCOME_ID_RE.fullmatch(
        payload["outcome_id"]
    ):
        raise ValueError("terminal outcome id is invalid")
    if payload["outcome_id"] != normalized["outcome_id"]:
        raise ValueError("terminal outcome id does not match its correlation fields")
    return normalized


def append_speech_cleanup_render_outcome_locked(
    job: _TraceJob,
    payload: Mapping[str, Any],
) -> SpeechCleanupOutcomeAppendStatus:
    """Append a terminal event to an already row-locked ``Job``.

    This helper performs no I/O and never raises.  Assignment to
    ``job.pipeline_trace`` ensures SQLAlchemy sees the JSONB replacement.  The
    caller owns the surrounding transaction and must first prove the generation
    is the winner (or exact owned loser) represented by ``payload``.
    """

    try:
        safe = _normalize_built_payload(payload)
        trace_value = job.pipeline_trace
        if trace_value is None:
            trace: list[Any] = []
        elif isinstance(trace_value, list):
            trace = list(trace_value)
        else:
            return "error"

        outcome_id = safe["outcome_id"]
        for event in trace:
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if (
                event.get("stage") == _EVENT_STAGE
                and event.get("event") == _EVENT_NAME
                and isinstance(data, dict)
                and data.get("outcome_id") == outcome_id
            ):
                return "persisted"

        if len(trace) >= _MAX_TRACE_EVENTS:
            return "dropped_cap"
        trace.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": _EVENT_STAGE,
                "event": _EVENT_NAME,
                "data": safe,
            }
        )
        job.pipeline_trace = trace
        return "persisted"
    except Exception:  # noqa: BLE001 - observability must never block publication
        return "error"
