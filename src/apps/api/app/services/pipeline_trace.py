"""Append-only log of non-LLM pipeline decisions for the admin debug view.

Agents already get persisted to ``agent_run`` from
``app.agents._persistence``. This module captures the *other* half of the
question "why is this video bad?": the assembler-side choices —
interstitial picks, transition types, beat-snap offsets, font-cycle
accel decisions. None of those go through an LLM; they're FFmpeg /
geometry / heuristic code paths.

Mechanism: orchestrators set the current ``job_id`` once at task entry
via the ``pipeline_trace_for`` context manager. Pipeline modules call
``record_pipeline_event(stage, event, data)`` at decision points. The
event is appended to ``jobs.pipeline_trace`` (JSONB array).

Failure modes — all swallowed:
  - No job_id in context (e.g. template analysis pre-job, eval) → skip.
  - DB write fails → log + continue. Pipeline must not break.
  - Concurrent appends from parallel FFmpeg tasks → server-side
    ``jsonb_set`` / ``||`` append in one UPDATE, so individual writes
    are atomic; we accept that interleaving order may differ slightly
    from wall-clock order. Events carry ``ts`` for client-side sort.
"""

from __future__ import annotations

import contextlib
import copy
import re
import time
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

log = structlog.get_logger()

_current_job_id: ContextVar[str | None] = ContextVar("pipeline_trace_job_id", default=None)

# Soft cap on the number of events appended per job. Real workloads
# produce well under this; the cap exists so a runaway loop can't blow
# the JSONB column up. Past the cap, events are dropped (with a single
# warning per job).
_MAX_EVENTS = 500
_MAX_SPEECH_CLEANUP_EVENT_BYTES = 16 * 1024

SpeechCleanupTraceStatus = Literal[
    "persisted",
    "dropped_no_context",
    "dropped_invalid_job",
    "dropped_job_missing",
    "dropped_cancelled",
    "dropped_cap",
    "error",
]


def set_pipeline_job_id(job_id: str | uuid.UUID | None) -> object:
    """Bind ``job_id`` to the current execution context. Returns a token
    that ``reset_pipeline_job_id`` consumes. Most callers should use
    ``pipeline_trace_for`` instead.
    """
    return _current_job_id.set(str(job_id) if job_id else None)


def reset_pipeline_job_id(token: object) -> None:
    _current_job_id.reset(token)  # type: ignore[arg-type]


def current_pipeline_job_id() -> str | None:
    return _current_job_id.get()


@contextlib.contextmanager
def pipeline_trace_for(job_id: str | uuid.UUID | None) -> Iterator[None]:
    """Bind ``job_id`` for the duration of a `with` block. Use at task
    entry so every ``record_pipeline_event`` call inside attributes
    correctly. Always restores prior context on exit, including on
    exception — prevents leaking a stale job_id into the next Celery
    task running on the same worker process.
    """
    token = set_pipeline_job_id(job_id)
    try:
        yield
    finally:
        reset_pipeline_job_id(token)


def record_pipeline_event(stage: str, event: str, data: dict[str, Any] | None = None) -> None:
    """Append one event to the current job's pipeline_trace.

    Args:
        stage: Coarse bucket — "interstitial", "transition", "overlay",
            "assembly", "beat_snap", "reframe", "audio_mix".
        event: Specific decision name — "curtain_close_detected",
            "xfade_picked", "font_cycle_accel_set", "beat_snap_offset",
            etc.
        data: Arbitrary JSON-safe payload with the decision details.
    """
    job_id_str = _current_job_id.get()
    if not job_id_str:
        # Not in a tracked job (e.g. template analysis runs before any
        # Job row exists, or this is an off-job pipeline run). Drop.
        return

    try:
        job_uuid = uuid.UUID(job_id_str)
    except (ValueError, AttributeError):
        return

    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "stage": stage,
        "event": event,
        "data": data or {},
    }

    try:
        from sqlalchemy import text  # noqa: PLC0415

        from app.database import sync_engine  # noqa: PLC0415

        # Concurrency note: this single UPDATE statement is safe under
        # concurrent writers without an explicit row lock. Postgres
        # READ COMMITTED + EvalPlanQual recheck guarantees that when two
        # transactions UPDATE the same row, the second one re-reads
        # ``pipeline_trace`` after acquiring the row lock — so
        # ``col = col || event`` sees the already-appended value, never
        # the stale snapshot. Verified empirically with 50 threads × 10
        # events: 500/500 events landed, zero lost.
        #
        # COALESCE handles the NULL initial state on legacy/new jobs.
        # The ``jsonb_array_length`` guard caps unbounded growth.
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE jobs
                    SET pipeline_trace = CASE
                        WHEN jsonb_array_length(COALESCE(pipeline_trace, '[]'::jsonb)) >= :cap
                            THEN pipeline_trace
                        ELSE COALESCE(pipeline_trace, '[]'::jsonb)
                             || CAST(:event_json AS JSONB)
                    END
                    WHERE id = :job_id
                      AND status <> 'cancelled'
                    """
                ),
                {
                    "job_id": str(job_uuid),
                    "event_json": _json_dumps([payload]),
                    "cap": _MAX_EVENTS,
                },
            )
    except Exception as exc:  # noqa: BLE001 — never break pipeline work
        # `event` is structlog's reserved name for the log message — pass it
        # as `event_name` instead.
        log.warning(
            "pipeline_trace_persist_failed",
            stage=stage,
            event_name=event,
            job_id=job_id_str,
            error=str(exc),
        )


_SAFE_RENDER_KEYS = {
    "attempt",
    "cache",
    "counts",
    "error_class",
    "render_generation_id",
    "retry",
    "status",
    "trace_id",
    "variant_id",
}


def record_render_stage(
    stage: str,
    *,
    elapsed_ms: int | None = None,
    status: str = "ok",
    trace_id: str | None = None,
    variant_id: str | None = None,
    render_generation_id: str | None = None,
    attempt: int | None = None,
    cache: dict[str, Any] | None = None,
    retry: dict[str, Any] | None = None,
    counts: dict[str, Any] | None = None,
    error_class: str | None = None,
) -> None:
    """Record one content-safe render timing event.

    This is intentionally narrower than ``record_pipeline_event``: render timing
    payloads must not carry transcripts, prompts, signed URLs, user notes, or
    media contents. Keep the payload to IDs, counts, booleans, enums, and
    durations so admin debug can summarize performance safely.
    """
    payload = _sanitize_render_payload(
        {
            "trace_id": trace_id,
            "variant_id": variant_id,
            "render_generation_id": render_generation_id,
            "stage": stage,
            "elapsed_ms": int(elapsed_ms) if elapsed_ms is not None else None,
            "status": status,
            "attempt": int(attempt) if attempt is not None else None,
            "cache": cache,
            "retry": retry,
            "counts": counts,
            "error_class": error_class,
        }
    )
    record_pipeline_event("render_stage", stage, payload)


def record_speech_cleanup_detection(data: dict[str, Any]) -> SpeechCleanupTraceStatus:
    """Persist one bounded, timing-only mixed-gap analysis receipt.

    Unlike :func:`record_pipeline_event`, this helper reports the exact best-effort
    persistence branch so the worker can emit fleet-safe scalar telemetry without
    assuming that an admin receipt landed. It never raises and never logs receipt
    contents or exception messages.
    """

    job_id_str = _current_job_id.get()
    if not job_id_str:
        return "dropped_no_context"
    try:
        job_uuid = uuid.UUID(job_id_str)
    except (ValueError, AttributeError):
        return "dropped_invalid_job"

    try:
        safe = _sanitize_speech_cleanup_detection_payload(data)
        encoded = _json_dumps(safe)
        if encoded == "[]" or len(encoded.encode("utf-8")) > _MAX_SPEECH_CLEANUP_EVENT_BYTES:
            return "dropped_cap"

        from sqlalchemy import text  # noqa: PLC0415

        from app.database import sync_engine  # noqa: PLC0415

        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "stage": "silence_cut",
            "event": "silence_cut_mixed_gap_analysis",
            "data": safe,
        }
        with sync_engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT status,
                           jsonb_array_length(COALESCE(pipeline_trace, '[]'::jsonb)) AS trace_len
                    FROM jobs
                    WHERE id = :job_id
                    FOR UPDATE
                    """
                    ),
                    {"job_id": str(job_uuid)},
                )
                .mappings()
                .first()
            )
            if row is None:
                return "dropped_job_missing"
            if row["status"] == "cancelled":
                return "dropped_cancelled"
            if int(row["trace_len"] or 0) >= _MAX_EVENTS:
                return "dropped_cap"
            conn.execute(
                text(
                    """
                    UPDATE jobs
                    SET pipeline_trace = COALESCE(pipeline_trace, '[]'::jsonb)
                                         || CAST(:event_json AS JSONB)
                    WHERE id = :job_id
                    """
                ),
                {
                    "job_id": str(job_uuid),
                    "event_json": _json_dumps([payload]),
                },
            )
        return "persisted"
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break rendering
        log.warning(
            "speech_cleanup_trace_persist_failed",
            job_id=job_id_str,
            error_class=type(exc).__name__,
        )
        return "error"


class RenderStageTimer:
    """Best-effort context manager for stage timing.

    On success it records ``status="ok"``. On exception it records
    ``status="failed"`` plus the exception class, then lets the exception
    propagate so render behavior remains unchanged.
    """

    def __init__(
        self,
        stage: str,
        *,
        trace_id: str | None = None,
        variant_id: str | None = None,
        render_generation_id: str | None = None,
        attempt: int | None = None,
        cache: dict[str, Any] | None = None,
        retry: dict[str, Any] | None = None,
        counts: dict[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.trace_id = trace_id
        self.variant_id = variant_id
        self.render_generation_id = render_generation_id
        self.attempt = attempt
        self.cache = cache
        self.retry = retry
        self.counts = counts
        self._t0 = 0.0

    def __enter__(self) -> RenderStageTimer:
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed_ms = int((time.monotonic() - self._t0) * 1000)
        record_render_stage(
            self.stage,
            elapsed_ms=elapsed_ms,
            status="failed" if exc_type is not None else "ok",
            trace_id=self.trace_id,
            variant_id=self.variant_id,
            render_generation_id=self.render_generation_id,
            attempt=self.attempt,
            cache=self.cache,
            retry=self.retry,
            counts=self.counts,
            error_class=getattr(exc_type, "__name__", None) if exc_type is not None else None,
        )


def render_stage_timer(
    stage: str,
    *,
    trace_id: str | None = None,
    variant_id: str | None = None,
    render_generation_id: str | None = None,
    attempt: int | None = None,
    cache: dict[str, Any] | None = None,
    retry: dict[str, Any] | None = None,
    counts: dict[str, Any] | None = None,
) -> RenderStageTimer:
    return RenderStageTimer(
        stage,
        trace_id=trace_id,
        variant_id=variant_id,
        render_generation_id=render_generation_id,
        attempt=attempt,
        cache=cache,
        retry=retry,
        counts=counts,
    )


def _sanitize_render_payload(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _SAFE_RENDER_KEYS | {"stage", "elapsed_ms"}:
        raw = value.get(key)
        if raw is None:
            continue
        if key in {"cache", "counts", "retry"} and isinstance(raw, dict):
            out[key] = _safe_shallow_dict(raw)
        elif isinstance(raw, (str, int, float, bool)):
            out[key] = raw
    return out


def _safe_shallow_dict(value: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        if any(blocked in key.lower() for blocked in ("text", "prompt", "url", "path", "note")):
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            safe[key] = raw
    return safe


_DETECTION_TOP_KEYS = frozenset(
    {
        "schema_version",
        "detector_version",
        "analysis_attempt_id",
        "analysis_view",
        "source_slot",
        "assignment_status",
        "source_tag",
        "analysis_policy",
        "configured_mode",
        "effective_mode",
        "candidate_status",
        "rollout_percent",
        "rollout_bucket",
        "duration_ms",
        "thresholds_ms",
        "inputs",
        "mixed_gap_scan",
        "allocator",
        "baseline_plan",
        "candidate_plan",
        "selected_plan",
    }
)
_THRESHOLD_KEYS = frozenset(
    {"silence_min", "island_min", "island_max", "flank_silence_min", "min_cut"}
)
_INPUT_KEYS = frozenset(
    {
        "silence_detection_status",
        "asr_word_count",
        "asr_word_spans_ms",
        "asr_word_spans_omitted",
        "silence_spans_total",
        "silence_spans_ms",
        "silence_spans_omitted",
        "lexical_candidate_spans_ms",
        "lexical_candidates_omitted",
    }
)
_SCAN_KEYS = frozenset(
    {"word_windows_total", "islands_total", "eligible_total", "records", "records_omitted"}
)
_DECISION_KEYS = frozenset(
    {
        "window_start_ms",
        "window_end_ms",
        "island_start_ms",
        "island_end_ms",
        "left_silence_ms",
        "right_silence_ms",
        "detection",
        "reason",
        "plan_disposition",
    }
)
_ALLOCATOR_KEYS = frozenset(
    {
        "atomic_disposition_fields",
        "atomic_dispositions_total",
        "atomic_dispositions",
        "atomic_dispositions_omitted",
    }
)
_PLAN_KEYS = frozenset(
    {
        "removed_count",
        "removed_ms",
        "removed_spans_ms",
        "removed_spans_omitted",
        "clamped",
        "bailout_reason",
        "mixed_gap_full",
        "mixed_gap_partial",
        "mixed_gap_dropped",
    }
)
_OUTCOME_KEYS = frozenset(
    {
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
)
_ASSIGNMENT_STATUSES = frozenset(
    {
        "assigned",
        "missing_source_instance",
        "cardinality_mismatch",
        "invalid_source_instance",
        "duplicate_source_instance",
        "unmapped_clip_id",
        "ambiguous_clip_id",
        "identity_cache_unavailable",
    }
)
_CANDIDATE_STATUSES = frozenset(
    {
        "analysis_failed",
        "analysis_not_started",
        "build_failed",
        "outer_media_probe_failed",
        "precheck_clip_too_short",
        "precheck_no_audio",
        "ready",
        "receipt_build_failed",
        "tool_unavailable",
        "validation_failed",
    }
)
_SILENCE_STATUSES = frozenset(
    {
        "ok",
        "not_run",
        "probe_failed",
        "invalid_duration",
        "no_audio",
        "ffmpeg_timeout",
        "ffmpeg_failed",
        "ffmpeg_nonzero",
        "parse_failed",
    }
)
_DECISION_REASONS = frozenset(
    {
        "bilateral_silence",
        "touches_window_boundary",
        "left_silence_too_short",
        "right_silence_too_short",
        "island_too_short",
        "island_too_long",
    }
)
_DISPOSITIONS = frozenset(
    {
        "selected_full",
        "promoted_protected",
        "dropped_budget",
        "dropped_max_removals",
        "dropped_min_cut",
        "dropped_micro_gap",
        "dropped_safety_bailout",
        "not_candidate",
    }
)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SOURCE_TAG_RE = re.compile(r"^[0-9a-f]{16}$")
_ATOMIC_FIELDS = [
    "atom_start_ms",
    "atom_end_ms",
    "group_start_ms",
    "group_end_ms",
    "atom_kind",
    "priority",
    "disposition",
]


def _exact_keys(value: object, allowed: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} contains unsupported fields")
    return value


def _bounded_int(
    value: object,
    *,
    label: str,
    maximum: int = 86_400_000,
    nullable: bool = False,
) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be a bounded non-negative integer")
    return value


def _enum(value: object, allowed: frozenset[str], label: str, *, nullable: bool = False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{label} is not supported")
    return value


def _token(value: object, label: str, *, nullable: bool = False, maximum: int = 128):
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not _TOKEN_RE.fullmatch(value)
    ):
        raise ValueError(f"{label} must be a bounded token")
    return value


def _timing_spans(value: object, *, label: str, limit: int) -> list[list[int]]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError(f"{label} exceeds its span cap")
    spans: list[list[int]] = []
    for raw in value:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError(f"{label} contains an invalid span")
        lo = _bounded_int(raw[0], label=f"{label}.start")
        hi = _bounded_int(raw[1], label=f"{label}.end")
        assert lo is not None and hi is not None
        if hi < lo:
            raise ValueError(f"{label} contains a reversed span")
        spans.append([lo, hi])
    return spans


def _sanitize_plan(value: object, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    plan = _exact_keys(value, _PLAN_KEYS, label)
    safe: dict[str, Any] = {}
    for key in plan:
        raw = plan[key]
        if key == "removed_spans_ms":
            safe[key] = _timing_spans(raw, label=f"{label}.{key}", limit=100)
        elif key == "clamped":
            if not isinstance(raw, bool):
                raise ValueError(f"{label}.{key} must be boolean")
            safe[key] = raw
        elif key == "bailout_reason":
            safe[key] = _enum(
                raw,
                frozenset(
                    {"no_words", "clip_too_short", "max_removal_exceeded", "output_too_short"}
                ),
                f"{label}.{key}",
                nullable=True,
            )
        else:
            safe[key] = _bounded_int(
                raw,
                label=f"{label}.{key}",
                maximum=86_400_000 if key == "removed_ms" else 1_000_000,
            )
    return safe


def _sanitize_speech_cleanup_detection_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact timing-only mixed-gap receipt schema."""

    receipt = _exact_keys(value, _DETECTION_TOP_KEYS, "speech cleanup receipt")
    if receipt.get("schema_version") != 1:
        raise ValueError("speech cleanup receipt schema version is not supported")
    safe: dict[str, Any] = {"schema_version": 1}
    scalar_enums = {
        "analysis_view": frozenset({"full_clip", "talking_head_spine_capped"}),
        "assignment_status": _ASSIGNMENT_STATUSES,
        "analysis_policy": frozenset({"required_v1", "legacy_auto", "off_v1"}),
        "configured_mode": frozenset({"off", "shadow", "apply"}),
        "effective_mode": frozenset({"off", "shadow", "apply"}),
        "candidate_status": _CANDIDATE_STATUSES,
        "selected_plan": frozenset({"baseline", "candidate"}),
    }
    for key, allowed in scalar_enums.items():
        if key in receipt:
            safe[key] = _enum(receipt[key], allowed, key)
    if "detector_version" in receipt:
        safe["detector_version"] = _token(
            receipt["detector_version"], "detector_version", maximum=80
        )
    if "analysis_attempt_id" in receipt:
        safe["analysis_attempt_id"] = _token(
            receipt["analysis_attempt_id"], "analysis_attempt_id", nullable=True
        )
    if "source_tag" in receipt:
        source_tag = receipt["source_tag"]
        if source_tag is not None and (
            not isinstance(source_tag, str) or not _SOURCE_TAG_RE.fullmatch(source_tag)
        ):
            raise ValueError("source_tag is invalid")
        safe["source_tag"] = source_tag
    for key, maximum in (
        ("source_slot", 1_000_000),
        ("rollout_percent", 100),
        ("rollout_bucket", 99),
        ("duration_ms", 86_400_000),
    ):
        if key in receipt:
            safe[key] = _bounded_int(receipt[key], label=key, maximum=maximum, nullable=True)

    if "thresholds_ms" in receipt:
        thresholds = _exact_keys(receipt["thresholds_ms"], _THRESHOLD_KEYS, "thresholds_ms")
        safe["thresholds_ms"] = {
            key: _bounded_int(value, label=f"thresholds_ms.{key}", maximum=60_000)
            for key, value in thresholds.items()
        }
    if "inputs" in receipt:
        inputs = _exact_keys(receipt["inputs"], _INPUT_KEYS, "inputs")
        safe_inputs: dict[str, Any] = {}
        for key, raw in inputs.items():
            if key == "silence_detection_status":
                safe_inputs[key] = _enum(raw, _SILENCE_STATUSES, f"inputs.{key}")
            elif key == "asr_word_spans_ms":
                safe_inputs[key] = _timing_spans(raw, label=f"inputs.{key}", limit=128)
            elif key == "silence_spans_ms":
                safe_inputs[key] = _timing_spans(raw, label=f"inputs.{key}", limit=128)
            elif key == "lexical_candidate_spans_ms":
                safe_inputs[key] = _timing_spans(raw, label=f"inputs.{key}", limit=32)
            else:
                safe_inputs[key] = _bounded_int(raw, label=f"inputs.{key}", maximum=1_000_000)
        safe["inputs"] = safe_inputs
    if "mixed_gap_scan" in receipt:
        scan = _exact_keys(receipt["mixed_gap_scan"], _SCAN_KEYS, "mixed_gap_scan")
        safe_scan: dict[str, Any] = {}
        for key, raw in scan.items():
            if key != "records":
                safe_scan[key] = _bounded_int(raw, label=f"mixed_gap_scan.{key}", maximum=1_000_000)
                continue
            if not isinstance(raw, list) or len(raw) > 32:
                raise ValueError("mixed_gap_scan.records exceeds its cap")
            records: list[dict[str, Any]] = []
            for item in raw:
                record = _exact_keys(item, _DECISION_KEYS, "mixed_gap_scan.record")
                safe_record: dict[str, Any] = {}
                for field, value in record.items():
                    if field == "detection":
                        safe_record[field] = _enum(
                            value, frozenset({"eligible", "rejected"}), field
                        )
                    elif field == "reason":
                        safe_record[field] = _enum(value, _DECISION_REASONS, field)
                    elif field == "plan_disposition":
                        safe_record[field] = _enum(value, _DISPOSITIONS, field)
                    else:
                        safe_record[field] = _bounded_int(value, label=field)
                records.append(safe_record)
            safe_scan[key] = records
        safe["mixed_gap_scan"] = safe_scan
    if "allocator" in receipt:
        allocator = _exact_keys(receipt["allocator"], _ALLOCATOR_KEYS, "allocator")
        safe_allocator: dict[str, Any] = {}
        for key, raw in allocator.items():
            if key == "atomic_disposition_fields":
                if raw != _ATOMIC_FIELDS:
                    raise ValueError("allocator fields are invalid")
                safe_allocator[key] = list(_ATOMIC_FIELDS)
            elif key == "atomic_dispositions":
                if not isinstance(raw, list) or len(raw) > 64:
                    raise ValueError("atomic dispositions exceed their cap")
                rows: list[list[Any]] = []
                for row in raw:
                    if not isinstance(row, (list, tuple)) or len(row) != 7:
                        raise ValueError("atomic disposition row is invalid")
                    rows.append(
                        [
                            *[
                                _bounded_int(value, label="atomic disposition timing")
                                for value in row[:4]
                            ],
                            _enum(
                                row[4],
                                frozenset({"filler_lexical", "filler_acoustic", "retake"}),
                                "atom_kind",
                            ),
                            _enum(
                                row[5],
                                frozenset({"protected", "filler", "retake"}),
                                "priority",
                            ),
                            _enum(row[6], _DISPOSITIONS - {"not_candidate"}, "disposition"),
                        ]
                    )
                safe_allocator[key] = rows
            else:
                safe_allocator[key] = _bounded_int(raw, label=f"allocator.{key}", maximum=1_000_000)
        safe["allocator"] = safe_allocator
    for key in ("baseline_plan", "candidate_plan"):
        if key in receipt:
            safe[key] = _sanitize_plan(receipt[key], key)
    return copy.deepcopy(safe)


def _sanitize_speech_cleanup_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Validate an exact scalar terminal-outcome payload."""

    payload = _exact_keys(value, _OUTCOME_KEYS, "speech cleanup outcome")
    if set(payload) != _OUTCOME_KEYS or payload.get("schema_version") != 1:
        raise ValueError("speech cleanup outcome has an invalid shape")
    safe: dict[str, Any] = {}
    for key, raw in payload.items():
        if raw is None:
            safe[key] = None
        elif isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ValueError("speech cleanup outcome must contain scalar values")
        elif isinstance(raw, int):
            safe[key] = _bounded_int(raw, label=key, maximum=86_400_000)
        else:
            safe[key] = _token(raw, key, maximum=128)
    return safe


def sanitize_speech_cleanup_trace_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Return the shared privacy-safe speech-cleanup trace representation.

    Terminal speech-cleanup state transitions append to an already row-locked
    ``Job`` instead of opening the independent transaction used by
    :func:`record_speech_cleanup_detection`.  Exposing the same sanitizer keeps
    both persistence paths on one content/privacy boundary.

    Raises ``ValueError`` for content-bearing, oversized, nested, or unsupported
    values.  Callers that participate in a terminal state transition must catch
    failures so diagnostics remain fail-open.
    """

    return _sanitize_speech_cleanup_payload(value)


def _json_dumps(value: Any) -> str:
    import json  # noqa: PLC0415

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "[]"
