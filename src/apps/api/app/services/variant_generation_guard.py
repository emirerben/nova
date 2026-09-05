"""Server-side write barriers for active speech-cut operations."""

from __future__ import annotations

from typing import Any

PRIVATE_SPEECH_CLEANUP_KEY = "_speech_cleanup_internal"
REQUIRED_SPEECH_LOCKS_KEY = "required_speech_generation_locks"
SPEECH_CUT_CONTROL_KEY = "speech_cut_control"


class VariantInitialRenderInProgress(RuntimeError):
    """Raised when a public/editor write races an active speech-cut operation."""


def _assembly_plan(job: Any, variant_id: str) -> dict[str, Any]:
    raw = getattr(job, "assembly_plan", None)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise VariantInitialRenderInProgress(variant_id)
    return raw


def required_speech_generation_lock(job: Any, variant_id: str) -> str | None:
    """Return the active generation lock, failing closed on malformed lock state."""

    plan = _assembly_plan(job, variant_id)
    internal = plan.get(PRIVATE_SPEECH_CLEANUP_KEY)
    if internal is None:
        return None
    if not isinstance(internal, dict):
        raise VariantInitialRenderInProgress(variant_id)
    locks = internal.get(REQUIRED_SPEECH_LOCKS_KEY)
    if locks is None:
        return None
    if not isinstance(locks, dict):
        raise VariantInitialRenderInProgress(variant_id)
    raw = locks.get(variant_id)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 128:
        raise VariantInitialRenderInProgress(variant_id)
    return raw


def _has_active_speech_cut_control(job: Any, variant_id: str) -> bool:
    """Return whether the job has a committed speech-cut operation.

    ``speech_cut_control`` is singular for the whole job, so it is a job-wide
    mutation barrier from the route commit until the operation clears it.  In
    particular, this closes the window before a worker creates the private
    per-variant generation lock.
    """

    plan = _assembly_plan(job, variant_id)
    control = plan.get(SPEECH_CUT_CONTROL_KEY)
    if control is None or control == {}:
        return False
    if not isinstance(control, dict):
        raise VariantInitialRenderInProgress(variant_id)
    for field in ("variant_id", "operation_id", "render_generation_id"):
        value = control.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise VariantInitialRenderInProgress(variant_id)
    return True


def assert_variant_generation_editable(job: Any, variant_id: str) -> None:
    """Reject public mutations while any speech-cut operation owns the job."""

    if required_speech_generation_lock(job, variant_id) is not None:
        raise VariantInitialRenderInProgress(variant_id)
    if _has_active_speech_cut_control(job, variant_id):
        raise VariantInitialRenderInProgress(variant_id)


def assert_required_speech_dispatch_quiescent(job: Any, variant_id: str) -> None:
    """Reject a required-v1 dispatch while a sibling render can change its snapshot.

    Required speech-cut rollback retains an exact public-vector snapshot.  An
    editor worker that was already running before the speech control commits is
    not stopped by the control barrier and may still publish a sibling row.  Do
    not take that snapshot until every sibling is terminal, otherwise rollback
    could replace a newer sibling result with the stale vector.

    Legacy/off contracts keep their established dispatch behavior; only
    ``required_v1`` uses the publication-atomic full-vector rollback contract.
    """

    plan = _assembly_plan(job, variant_id)
    if plan.get("speech_cleanup_contract") != "required_v1":
        return
    variants = plan.get("variants")
    if not isinstance(variants, list) or any(not isinstance(row, dict) for row in variants):
        raise VariantInitialRenderInProgress(variant_id)
    variant_ids = [row.get("variant_id") for row in variants]
    if (
        any(not isinstance(value, str) or not value.strip() for value in variant_ids)
        or len(variant_ids) != len(set(variant_ids))
        or variant_ids.count(variant_id) != 1
    ):
        raise VariantInitialRenderInProgress(variant_id)
    if any(
        row.get("variant_id") != variant_id and row.get("render_status") not in {"ready", "failed"}
        for row in variants
    ):
        raise VariantInitialRenderInProgress(variant_id)
