"""Fail-closed promotion gate for chat speech-cleanup cohorts.

The gate deliberately consumes aggregate, scalar-only evidence.  Raw media
locations, transcripts, timed words, cut intervals, signed URLs, and provider
diagnostics have no field in the input or output schema.  The operator script
signs the resulting receipt only after this module has validated the exact
ramp transition, the dedicated-worker canary, safety thresholds, and the
required source/outcome canaries.

This module does not change runtime flags.  A passing receipt is evidence that
an operator may make the separately reviewed configuration change; a missing,
malformed, stale, or failing receipt always halts the promotion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

ROLLOUT_STAGES = (1, 10, 25, 50, 100)
REQUIRED_SOURCE_RECEIPTS = (
    "embedded_video",
    "narrated_upload_or_recording",
    "audio_only_then_video",
)
REQUIRED_OUTCOME_RECEIPTS = (
    "applied",
    "checked_no_change",
    "declined",
    "bypassed_unchecked",
    "failed",
)

MAX_EVIDENCE_BYTES = 64 * 1024
MAX_EVIDENCE_WINDOW_S = 48 * 60 * 60
MAX_EVIDENCE_AGE_S = 15 * 60
MAX_CLOCK_SKEW_S = 5 * 60
MIN_ANALYSIS_SAMPLE = 5
MAX_UNEXPECTED_FAILURE_RATE = 0.05
MAX_QUEUE_LATENCY_P95_MS = 120_000
MAX_RUN_LATENCY_P95_MS = 900_000
MIN_RECEIPT_SECRET_BYTES = 32

_MAX_COUNT = 1_000_000_000
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_OUTCOME_STATUSES = frozenset(
    {"applied", "checked_no_change", "declined", "bypassed_unchecked", "failed"}
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "observed_mode",
        "observed_rollout_percent",
        "observed_mixed_gap_mode",
        "observed_mixed_gap_rollout_percent",
        "window_started_at",
        "window_ended_at",
        "worker_canary",
        "metrics",
        "source_receipts",
        "outcome_receipts",
        "rollback_drill",
    }
)
_WORKER_KEYS = frozenset(
    {
        "queue",
        "consumer_count",
        "published_count",
        "completed_count",
        "api_revision",
        "worker_revision",
    }
)
_METRIC_KEYS = frozenset(
    {
        "analysis_count",
        "unexpected_failure_count",
        "stuck_count",
        "snapshot_mismatch_count",
        "invalidation_error_count",
        "duplicate_dispatch_count",
        "private_payload_leak_count",
        "queue_latency_p95_ms",
        "run_latency_p95_ms",
    }
)
_ROLLBACK_KEYS = frozenset(
    {
        "completed",
        "completed_at",
        "restored_preflight_mode",
        "restored_preflight_percent",
        "restored_mixed_gap_mode",
        "restored_mixed_gap_percent",
        "inflight_contracts_preserved",
    }
)


class RolloutEvidenceError(ValueError):
    """Evidence was not safely parseable, so no promotion may occur."""


@dataclass(frozen=True)
class RolloutAudit:
    receipt: dict[str, Any]
    promotion_allowed: bool
    halt_reasons: tuple[str, ...]


def _exact_mapping(value: object, *, field: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RolloutEvidenceError(f"{field} must contain the exact supported fields")
    if any(not isinstance(key, str) for key in value):
        raise RolloutEvidenceError(f"{field} contains an invalid key")
    return value


def _bounded_count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        raise RolloutEvidenceError(f"{field} must be a bounded non-negative integer")
    return value


def _bounded_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise RolloutEvidenceError(f"{field} must be a bounded token")
    return value


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise RolloutEvidenceError(f"{field} must be a bounded ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RolloutEvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RolloutEvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _expected_previous_stage(target_percent: int) -> int:
    try:
        index = ROLLOUT_STAGES.index(target_percent)
    except ValueError as exc:
        raise RolloutEvidenceError("target_percent must be a supported rollout stage") from exc
    return 0 if index == 0 else ROLLOUT_STAGES[index - 1]


def audit_rollout_evidence(
    value: Mapping[str, Any],
    *,
    target_percent: int,
    expected_queue: str = "speech-analysis",
    now: datetime | None = None,
) -> RolloutAudit:
    """Validate one promotion window and return a content-minimized receipt.

    ``shadow`` evidence may promote only to the first 1% enforcement cohort and
    must cover 100% of the internal canary sources.  Later promotions require
    exact stage adjacency for both preflight enforcement and mixed-gap apply.
    Every stage requires controlled receipts for all source and outcome branches;
    the 100% stage additionally requires a completed off/0 rollback drill.
    """

    raw = _exact_mapping(value, field="evidence", keys=_TOP_LEVEL_KEYS)
    if raw["schema_version"] != 1:
        raise RolloutEvidenceError("unsupported evidence schema_version")
    if target_percent not in ROLLOUT_STAGES:
        raise RolloutEvidenceError("target_percent must be a supported rollout stage")
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    expected_previous = _expected_previous_stage(target_percent)

    observed_mode = raw["observed_mode"]
    if observed_mode not in {"off", "shadow", "enforce"}:
        raise RolloutEvidenceError("observed_mode is invalid")
    observed_percent = _bounded_count(
        raw["observed_rollout_percent"], field="observed_rollout_percent"
    )
    observed_mixed_mode = raw["observed_mixed_gap_mode"]
    if observed_mixed_mode not in {"off", "shadow", "apply"}:
        raise RolloutEvidenceError("observed_mixed_gap_mode is invalid")
    observed_mixed_percent = _bounded_count(
        raw["observed_mixed_gap_rollout_percent"],
        field="observed_mixed_gap_rollout_percent",
    )
    if observed_percent > 100 or observed_mixed_percent > 100:
        raise RolloutEvidenceError("rollout percentages cannot exceed 100")

    started = _timestamp(raw["window_started_at"], field="window_started_at")
    ended = _timestamp(raw["window_ended_at"], field="window_ended_at")
    if ended <= started:
        raise RolloutEvidenceError("evidence window must end after it starts")
    duration_s = int((ended - started).total_seconds())
    if duration_s > MAX_EVIDENCE_WINDOW_S:
        raise RolloutEvidenceError("evidence window exceeds the bounded maximum")
    if ended > current_time + timedelta(seconds=MAX_CLOCK_SKEW_S):
        raise RolloutEvidenceError("evidence window ends in the future")

    worker = _exact_mapping(raw["worker_canary"], field="worker_canary", keys=_WORKER_KEYS)
    queue = _bounded_token(worker["queue"], field="worker_canary.queue")
    consumer_count = _bounded_count(worker["consumer_count"], field="worker_canary.consumer_count")
    published_count = _bounded_count(
        worker["published_count"], field="worker_canary.published_count"
    )
    completed_count = _bounded_count(
        worker["completed_count"], field="worker_canary.completed_count"
    )
    api_revision = _bounded_token(worker["api_revision"], field="worker_canary.api_revision")
    worker_revision = _bounded_token(
        worker["worker_revision"], field="worker_canary.worker_revision"
    )

    metrics = _exact_mapping(raw["metrics"], field="metrics", keys=_METRIC_KEYS)
    metric_values = {
        key: _bounded_count(metrics[key], field=f"metrics.{key}") for key in _METRIC_KEYS
    }
    sources = _exact_mapping(
        raw["source_receipts"],
        field="source_receipts",
        keys=frozenset(REQUIRED_SOURCE_RECEIPTS),
    )
    source_counts = {
        key: _bounded_count(sources[key], field=f"source_receipts.{key}")
        for key in REQUIRED_SOURCE_RECEIPTS
    }
    outcomes = _exact_mapping(
        raw["outcome_receipts"],
        field="outcome_receipts",
        keys=frozenset(REQUIRED_OUTCOME_RECEIPTS),
    )
    outcome_counts = {
        key: _bounded_count(outcomes[key], field=f"outcome_receipts.{key}")
        for key in REQUIRED_OUTCOME_RECEIPTS
    }
    rollback = _exact_mapping(raw["rollback_drill"], field="rollback_drill", keys=_ROLLBACK_KEYS)
    rollback_completed = rollback["completed"]
    inflight_preserved = rollback["inflight_contracts_preserved"]
    if not isinstance(rollback_completed, bool) or not isinstance(inflight_preserved, bool):
        raise RolloutEvidenceError("rollback drill booleans are invalid")
    rollback_completed_at = (
        _timestamp(rollback["completed_at"], field="rollback_drill.completed_at")
        if rollback["completed_at"] is not None
        else None
    )
    restored_mode = rollback["restored_preflight_mode"]
    restored_mixed_mode = rollback["restored_mixed_gap_mode"]
    if restored_mode not in {None, "off"} or restored_mixed_mode not in {None, "off"}:
        raise RolloutEvidenceError("rollback drill modes must be null or off")
    restored_percent = _bounded_count(
        rollback["restored_preflight_percent"],
        field="rollback_drill.restored_preflight_percent",
    )
    restored_mixed_percent = _bounded_count(
        rollback["restored_mixed_gap_percent"],
        field="rollback_drill.restored_mixed_gap_percent",
    )
    if restored_percent > 100 or restored_mixed_percent > 100:
        raise RolloutEvidenceError("rollback percentages cannot exceed 100")

    reasons: list[str] = []
    if current_time - ended > timedelta(seconds=MAX_EVIDENCE_AGE_S):
        reasons.append("evidence_window_stale")

    if target_percent == 1:
        if observed_mode != "shadow" or observed_percent != 100:
            reasons.append("initial_shadow_coverage_incomplete")
        if observed_mixed_mode != "shadow" or observed_mixed_percent != 100:
            reasons.append("initial_mixed_gap_shadow_coverage_incomplete")
    else:
        if observed_mode != "enforce" or observed_percent != expected_previous:
            reasons.append("preflight_ramp_transition_invalid")
        if observed_mixed_mode != "apply" or observed_mixed_percent != expected_previous:
            reasons.append("mixed_gap_ramp_transition_invalid")

    if queue != expected_queue:
        reasons.append("dedicated_worker_queue_mismatch")
    if consumer_count < 1:
        reasons.append("dedicated_worker_consumer_missing")
    if published_count < 1:
        reasons.append("worker_canary_publish_missing")
    if completed_count < 1 or completed_count > published_count:
        reasons.append("worker_canary_completion_missing")
    if api_revision != worker_revision:
        reasons.append("api_worker_revision_mismatch")

    analysis_count = metric_values["analysis_count"]
    unexpected_failures = metric_values["unexpected_failure_count"]
    if analysis_count < MIN_ANALYSIS_SAMPLE:
        reasons.append("analysis_sample_too_small")
    if unexpected_failures > analysis_count:
        reasons.append("unexpected_failure_count_invalid")
    elif analysis_count and unexpected_failures / analysis_count > MAX_UNEXPECTED_FAILURE_RATE:
        reasons.append("unexpected_failure_rate_exceeded")
    if metric_values["queue_latency_p95_ms"] > MAX_QUEUE_LATENCY_P95_MS:
        reasons.append("queue_latency_p95_exceeded")
    if metric_values["run_latency_p95_ms"] > MAX_RUN_LATENCY_P95_MS:
        reasons.append("run_latency_p95_exceeded")
    for field, reason in (
        ("stuck_count", "stuck_analyses_present"),
        ("snapshot_mismatch_count", "snapshot_mismatches_present"),
        ("invalidation_error_count", "invalidation_errors_present"),
        ("duplicate_dispatch_count", "duplicate_dispatches_present"),
        ("private_payload_leak_count", "private_payload_leaks_present"),
    ):
        if metric_values[field] > 0:
            reasons.append(reason)

    reasons.extend(
        f"source_receipt_missing:{name}" for name, count in source_counts.items() if count < 1
    )
    reasons.extend(
        f"outcome_receipt_missing:{name}" for name, count in outcome_counts.items() if count < 1
    )

    if target_percent == 100:
        if not rollback_completed:
            reasons.append("rollback_drill_missing")
        elif (
            rollback_completed_at is None
            or rollback_completed_at < started
            or rollback_completed_at > ended
            or restored_mode != "off"
            or restored_percent != 0
            or restored_mixed_mode != "off"
            or restored_mixed_percent != 0
            or not inflight_preserved
        ):
            reasons.append("rollback_drill_invalid")

    reasons = list(dict.fromkeys(reasons))
    unexpected_failure_rate = (
        round(unexpected_failures / analysis_count, 6) if analysis_count else None
    )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "receipt_type": "speech_cleanup_rollout_gate",
        "evaluated_at": _utc_text(current_time),
        "window_started_at": _utc_text(started),
        "window_ended_at": _utc_text(ended),
        "window_duration_s": duration_s,
        "observed_mode": observed_mode,
        "observed_rollout_percent": observed_percent,
        "observed_mixed_gap_mode": observed_mixed_mode,
        "observed_mixed_gap_rollout_percent": observed_mixed_percent,
        "target_percent": target_percent,
        "worker_canary": {
            "queue": queue,
            "consumer_count": consumer_count,
            "published_count": published_count,
            "completed_count": completed_count,
            "api_revision": api_revision,
            "worker_revision": worker_revision,
        },
        "metrics": {
            **metric_values,
            "unexpected_failure_rate": unexpected_failure_rate,
        },
        "source_receipts": source_counts,
        "outcome_receipts": outcome_counts,
        "rollback_drill": {
            "completed": rollback_completed,
            "completed_at": (
                _utc_text(rollback_completed_at) if rollback_completed_at is not None else None
            ),
            "restored_preflight_mode": restored_mode,
            "restored_preflight_percent": restored_percent,
            "restored_mixed_gap_mode": restored_mixed_mode,
            "restored_mixed_gap_percent": restored_mixed_percent,
            "inflight_contracts_preserved": inflight_preserved,
        },
        "thresholds": {
            "minimum_analysis_sample": MIN_ANALYSIS_SAMPLE,
            "maximum_unexpected_failure_rate": MAX_UNEXPECTED_FAILURE_RATE,
            "maximum_queue_latency_p95_ms": MAX_QUEUE_LATENCY_P95_MS,
            "maximum_run_latency_p95_ms": MAX_RUN_LATENCY_P95_MS,
        },
        "promotion_allowed": not reasons,
        "halt_reasons": reasons,
    }
    return RolloutAudit(receipt, not reasons, tuple(reasons))


def canonical_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    """Return stable bytes for archival signing."""

    return json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def validate_receipt_secret(secret: str, *, forbidden: tuple[str, ...] = ()) -> bytes:
    if not isinstance(secret, str):
        raise RolloutEvidenceError("rollout receipt secret is not configured")
    encoded = secret.encode("utf-8")
    if len(encoded) < MIN_RECEIPT_SECRET_BYTES:
        raise RolloutEvidenceError("rollout receipt secret must contain at least 32 bytes")
    if any(value and hmac.compare_digest(secret, value) for value in forbidden):
        raise RolloutEvidenceError(
            "rollout receipt secret must not reuse an application credential"
        )
    return encoded


def sign_rollout_receipt(
    receipt: Mapping[str, Any],
    *,
    secret: str,
    key_id: str,
    forbidden_secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Attach a detached-shape HMAC signature to one sanitized audit receipt."""

    key = validate_receipt_secret(secret, forbidden=forbidden_secrets)
    bounded_key_id = _bounded_token(key_id, field="receipt key_id")
    unsigned = dict(receipt)
    unsigned.pop("signature", None)
    digest = hmac.new(key, canonical_receipt_bytes(unsigned), hashlib.sha256).hexdigest()
    return {
        **unsigned,
        "signature": {
            "algorithm": "hmac-sha256",
            "key_id": bounded_key_id,
            "digest": digest,
        },
    }


def verify_rollout_receipt(receipt: Mapping[str, Any], *, secret: str) -> bool:
    signature = receipt.get("signature")
    if not isinstance(signature, Mapping) or set(signature) != {"algorithm", "key_id", "digest"}:
        return False
    if signature.get("algorithm") != "hmac-sha256":
        return False
    digest = signature.get("digest")
    if not isinstance(digest, str) or not _HEX_SIGNATURE_RE.fullmatch(digest):
        return False
    try:
        key = validate_receipt_secret(secret)
        _bounded_token(signature.get("key_id"), field="receipt key_id")
    except RolloutEvidenceError:
        return False
    unsigned = dict(receipt)
    unsigned.pop("signature", None)
    expected = hmac.new(key, canonical_receipt_bytes(unsigned), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def project_admin_speech_cleanup_trace(value: object) -> dict[str, Any] | None:
    """Project one bounded preflight/render trace from a Job assembly plan.

    The immutable Job snapshot is private because it contains storage identity,
    timed words, and CutPlan intervals.  Admin Job Debug needs only enough scalar
    correlation to answer whether the render reused the preflight and how it
    finished.  Parse those values directly rather than returning any nested
    snapshot object.
    """

    if not isinstance(value, Mapping):
        return None
    contract = value.get("speech_cleanup_contract")
    if contract not in {"required_v1", "off_v1", "legacy_auto", None}:
        contract = None
    internal = value.get("_speech_cleanup_internal")
    snapshot = internal.get("preflight_snapshot") if isinstance(internal, Mapping) else None
    snapshot = snapshot if isinstance(snapshot, Mapping) else None
    source = snapshot.get("source") if snapshot is not None else None
    source = source if isinstance(source, Mapping) else None

    analysis_id: str | None = None
    detector_version: str | None = None
    source_kind: str | None = None
    if snapshot is not None:
        raw_analysis_id = snapshot.get("analysis_id")
        if isinstance(raw_analysis_id, str) and len(raw_analysis_id) <= 64:
            try:
                analysis_id = str(uuid.UUID(raw_analysis_id))
            except ValueError:
                analysis_id = None
        raw_detector = snapshot.get("detector_version")
        if isinstance(raw_detector, str) and _TOKEN_RE.fullmatch(raw_detector):
            detector_version = raw_detector
        if source is not None and source.get("kind") in {"voiceover", "embedded_spine"}:
            source_kind = source["kind"]

    outcome = value.get("speech_cleanup_outcome")
    safe_outcome: dict[str, Any] | None = None
    outcome_status: str | None = None
    if isinstance(outcome, Mapping) and outcome.get("status") in _PUBLIC_OUTCOME_STATUSES:
        outcome_status = outcome["status"]
        safe_outcome = {"status": outcome_status}
        for key in ("removal_count", "removed_ms"):
            raw_count = outcome.get(key)
            safe_outcome[key] = (
                raw_count
                if isinstance(raw_count, int)
                and not isinstance(raw_count, bool)
                and 0 <= raw_count <= _MAX_COUNT
                else 0
            )
        error = outcome.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            if isinstance(code, str) and _TOKEN_RE.fullmatch(code):
                safe_outcome["failure_code"] = code

    explicit_choice: str | None = None
    if contract == "required_v1":
        explicit_choice = "clean"
    elif outcome_status == "declined":
        explicit_choice = "keep_original"
    elif outcome_status == "bypassed_unchecked":
        explicit_choice = "create_without_cleanup"

    if contract is None and analysis_id is None and safe_outcome is None:
        return None
    return {
        "analysis_id": analysis_id,
        "source_kind": source_kind,
        "detector_version": detector_version,
        "explicit_choice": explicit_choice,
        "preflight_snapshot_reused": analysis_id is not None,
        "execution_contract": contract,
        "final_outcome": safe_outcome,
    }
