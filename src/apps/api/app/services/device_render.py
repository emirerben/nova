"""Immutable phone recipe receipts. Call mutations while holding the Job row lock."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from app.kria.device_render import (
    DeviceFailureReasonCode,
    DeviceRenderRequest,
    DeviceRenderStatus,
    make_device_request,
)

DEVICE_RENDER_FIELD = "_device_render_v1"

# Human-readable fallback when the reporter (phone client or reaper) sends an
# empty detail string. Keyed by reason_code; "unknown" also backs the reaper's
# own stale-timeout report (app/tasks/device_render_reaper.py).
_DEFAULT_FAILURE_DETAIL: dict[str, str] = {
    "export_failed": "The export failed on your device. Open the project to retry.",
    "insufficient_storage": "Not enough storage on your device to finish the export.",
    "thermal": "Your device paused rendering to cool down. Open the project to retry.",
    "unsupported_recipe": "This edit isn't supported by on-device rendering yet.",
    "cancelled_by_user": "The export was cancelled on your device.",
    "unknown": "Something went wrong rendering on your device. Open the project to retry.",
}

# Phases from which a device render may transition to needs_attention — a
# published record is done, and a record already needs_attention has no
# forward transition here (retry re-pins a brand new identity instead).
_FAILABLE_PHASES = ("awaiting_device", "syncing")


def device_record(job: Any, variant_id: str) -> dict:
    records = (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD) or {}
    record = records.get(variant_id)
    if not isinstance(record, dict):
        raise KeyError("device recipe unavailable")
    return copy.deepcopy(record)


def device_status(job: Any, variant_id: str) -> DeviceRenderStatus:
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
    if status.phase != "published":
        return status.model_copy(update={"published_generation": None})
    published_attempt = record.get("published_attempt")
    return status.model_copy(
        update={"published_generation": str(published_attempt) if published_attempt else None}
    )


def save_device_record(job: Any, variant_id: str, record: dict) -> None:
    assembly = copy.deepcopy(job.assembly_plan or {})
    records = assembly.setdefault(DEVICE_RENDER_FIELD, {})
    records[variant_id] = copy.deepcopy(record)
    job.assembly_plan = assembly


def pin_device_request(job: Any, request: DeviceRenderRequest, *, base_generation: str) -> bool:
    """Pin one approved recipe once. A redelivery cannot rewrite that revision's decisions."""
    if request.identity.job_id != job.id:
        raise ValueError("recipe job identity mismatch")
    try:
        previous = device_status(job, request.identity.variant_id).request
    except KeyError:
        previous = None
    if previous is not None:
        if previous == request:
            return False
        if request.identity.recipe_revision <= previous.identity.recipe_revision:
            raise ValueError("recipe revision already pinned")
    save_device_record(
        job,
        request.identity.variant_id,
        {
            "status": DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(
                mode="json"
            ),
            "base_generation": base_generation,
            "attempts": {},
            # ISO pin timestamp — the reaper's staleness fallback when a record
            # has never been polled (`last_polled_at` absent). See
            # app/tasks/device_render_reaper.py.
            "pinned_at": datetime.now(UTC).isoformat(),
        },
    )
    return True


def mark_device_failed(
    job: Any,
    variant_id: str,
    *,
    reason_code: DeviceFailureReasonCode,
    detail: str,
) -> DeviceRenderStatus:
    """Transition a device render to needs_attention from a reported/detected failure.

    Callable from `awaiting_device` or `syncing` only — a published record is
    already terminal-success and a record already `needs_attention` should be
    reported idempotently by the caller rather than re-failed here.
    """
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
    if status.phase not in _FAILABLE_PHASES:
        raise ValueError(f"device render cannot fail from phase '{status.phase}'")
    resolved_detail = detail or _DEFAULT_FAILURE_DETAIL.get(reason_code, "")
    status.phase = "needs_attention"
    status.reason = resolved_detail
    status.reason_code = reason_code
    record["status"] = status.model_dump(mode="json")
    failed_at = datetime.now(UTC).isoformat()
    record["failed_at"] = failed_at
    record["failure"] = {
        "reason_code": reason_code,
        "detail": resolved_detail,
        "failed_at": failed_at,
    }
    save_device_record(job, variant_id, record)
    return status


def touch_device_poll(job: Any, variant_id: str, now: datetime) -> None:
    """Record the latest client poll time — the reaper's staleness signal.

    Callers are expected to throttle their own write cadence (see the
    60s-per-record throttle in `app/routes/device_render.py::get_device_render`);
    this function itself always writes when called.
    """
    record = device_record(job, variant_id)
    record["last_polled_at"] = now.isoformat()
    save_device_record(job, variant_id, record)


def retry_device_render(job: Any, variant_id: str) -> DeviceRenderStatus:
    """Re-pin a needs_attention device recipe under a fresh identity.

    Preserves the exact recipe and `base_generation` from the failed record
    verbatim — this is a pure re-delivery vehicle, never a decision point.
    `pin_device_request` always starts the new record with empty `attempts`,
    so a retry naturally forgets the prior (failed) upload attempts.

    Caller must have already fenced ownership/identity and is responsible for
    deciding whether a currently `awaiting_device`/`syncing` record should be
    force-failed first (the admin unstick path) or rejected (the user path).
    """
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
    if status.phase != "needs_attention":
        raise ValueError("device render is not awaiting a retry")
    new_request = make_device_request(
        job_id=job.id,
        variant_id=variant_id,
        revision=status.request.identity.recipe_revision + 1,
        recipe=status.request.recipe,
    )
    pin_device_request(job, new_request, base_generation=record["base_generation"])
    return device_status(job, variant_id)


def apply_device_failure_variant_update(
    job: Any, variant_id: str, *, reason_code: str, detail: str
) -> None:
    """Mirror a failed device record onto the public `variants[]` + job failure surface."""
    assembly = dict(job.assembly_plan or {})
    assembly["variants"] = [
        {**v, "ok": False, "render_status": "needs_attention"}
        if v.get("variant_id") == variant_id
        else v
        for v in assembly.get("variants", [])
    ]
    job.assembly_plan = assembly
    job.failure_reason = reason_code
    job.error_detail = detail[:1000] or None


def apply_retry_variant_reset(job: Any, variant_id: str) -> None:
    """Flip the assembly_plan variant + job failure fields back to awaiting_device.

    Call AFTER `retry_device_render` has re-pinned the new identity so the
    per-variant status and the job-level failure surface reset together.
    """
    assembly = dict(job.assembly_plan or {})
    assembly["variants"] = [
        {**v, "ok": False, "render_status": "awaiting_device"}
        if v.get("variant_id") == variant_id
        else v
        for v in assembly.get("variants", [])
    ]
    job.assembly_plan = assembly
    job.failure_reason = None
    job.error_detail = None
