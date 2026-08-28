"""Fresh-read proofs for render finalization commit ambiguity.

PostgreSQL clients can raise while the server-side COMMIT has nevertheless
completed (connection loss, task soft-timeout delivered at the boundary, and
similar failures).  A render worker must therefore never infer "not committed"
from a commit exception and delete the attempt-owned objects immediately.

These helpers deliberately distinguish three outcomes:

* ``CONFIRMED`` -- the fresh row has the exact terminal status and attempt
  references that the worker tried to persist;
* ``NOT_COMMITTED`` -- the fresh row definitively has none of this attempt's
  references, so its private task-run objects are safe to delete; and
* ``UNKNOWN`` -- the verification read failed or only part of the attempted
  state is visible.  Callers must fail closed and retain the objects.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from typing import Any

from app.models import Job, JobClip


class FinalizationCommitState(Enum):
    CONFIRMED = "confirmed"
    NOT_COMMITTED = "not_committed"
    UNKNOWN = "unknown"


def _matches_fields(value: object, expected_fields: Mapping[str, object]) -> bool:
    if not isinstance(value, dict):
        return False
    return all(value.get(key) == expected for key, expected in expected_fields.items())


def _contains_reference(value: object, references: frozenset[str]) -> bool:
    if not references:
        return False
    if isinstance(value, str):
        return value in references
    if isinstance(value, dict):
        return any(_contains_reference(item, references) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reference(item, references) for item in value)
    return False


def _reference_values(
    values: Mapping[str, object] | Iterable[object],
) -> frozenset[str]:
    if isinstance(values, Mapping):
        candidates = values.values()
    else:
        candidates = values
    return frozenset(value for value in candidates if isinstance(value, str) and value)


def confirm_job_plan_finalization(
    session_factory: Callable[[], Any],
    *,
    job_id: str | uuid.UUID,
    expected_status: str,
    expected_plan_fields: Mapping[str, object],
    attempt_references: Mapping[str, object] | Iterable[object],
) -> FinalizationCommitState:
    """Prove whether a top-level Job finalization committed.

    The read intentionally uses a brand-new session supplied by the caller.
    ``populate_existing`` prevents an accidentally reused test/session identity
    map from turning this into a stale in-memory check.  The row lock is equally
    important: a plain READ COMMITTED select can observe the pre-commit version
    while the original finalizer is still resolving its COMMIT.  ``FOR UPDATE``
    waits for that transaction, so ``NOT_COMMITTED`` is only returned after the
    competing write has definitively committed or rolled back.
    """

    references = _reference_values(attempt_references)
    try:
        job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
        with session_factory() as db:
            job = db.get(
                Job,
                job_uuid,
                populate_existing=True,
                with_for_update=True,
            )
            if job is None:
                return FinalizationCommitState.NOT_COMMITTED
            plan = getattr(job, "assembly_plan", None)
            if getattr(job, "status", None) == expected_status and _matches_fields(
                plan,
                expected_plan_fields,
            ):
                return FinalizationCommitState.CONFIRMED
            if getattr(job, "status", None) == expected_status:
                # A different attempt may have won while this worker was
                # resolving its commit error. Do not let the losing worker's
                # outer failure handler overwrite that terminal success.
                return FinalizationCommitState.UNKNOWN
            if _contains_reference(plan, references):
                return FinalizationCommitState.UNKNOWN
            return FinalizationCommitState.NOT_COMMITTED
    except Exception:  # noqa: BLE001 -- an inconclusive proof must retain media
        return FinalizationCommitState.UNKNOWN


def confirm_job_clip_finalization(
    session_factory: Callable[[], Any],
    *,
    job_id: str | uuid.UUID,
    clip_id: str | uuid.UUID,
    expected_clip_fields: Mapping[str, object],
    attempt_references: Mapping[str, object] | Iterable[object],
) -> FinalizationCommitState:
    """Prove whether a legacy JobClip finalization committed."""

    references = _reference_values(attempt_references)
    try:
        job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
        clip_uuid = clip_id if isinstance(clip_id, uuid.UUID) else uuid.UUID(str(clip_id))
        with session_factory() as db:
            # Match the legacy finalizer's lock order (Job -> JobClip) to avoid
            # introducing an inversion while we wait out an in-flight COMMIT.
            job = db.get(
                Job,
                job_uuid,
                populate_existing=True,
                with_for_update=True,
            )
            clip = db.get(
                JobClip,
                clip_uuid,
                populate_existing=True,
                with_for_update=True,
            )
            if (
                job is not None
                and clip is not None
                and getattr(clip, "job_id", None) == job_uuid
                and all(
                    getattr(clip, key, None) == expected
                    for key, expected in expected_clip_fields.items()
                )
            ):
                return FinalizationCommitState.CONFIRMED
            if (
                clip is not None
                and any(
                    getattr(clip, key, None) == expected
                    for key, expected in expected_clip_fields.items()
                    if isinstance(expected, str) and expected
                )
            ) or _contains_reference(
                getattr(job, "assembly_plan", None) if job is not None else None,
                references,
            ):
                return FinalizationCommitState.UNKNOWN
            return FinalizationCommitState.NOT_COMMITTED
    except Exception:  # noqa: BLE001 -- an inconclusive proof must retain media
        return FinalizationCommitState.UNKNOWN
