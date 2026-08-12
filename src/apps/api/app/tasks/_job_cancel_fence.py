"""Shared terminal-cancellation fences for long-running Job orchestrators.

Render workers release their database transaction before doing network, model,
or FFmpeg work.  Every later Job mutation must therefore re-lock the row and
re-check the terminal ``cancelled`` state.  Output objects use a per-attempt
``task-runs`` namespace so a losing worker can delete only bytes it created;
pre-existing Job objects and user uploads are never cleanup targets.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

import structlog

from app.models import Job

log = structlog.get_logger()

CANCELLED_JOB_STATUS = "cancelled"
TASK_RUN_PATH_SEGMENT = "/task-runs/"


def new_task_run_id() -> str:
    """Return an unguessable per-attempt identifier for task-owned outputs."""
    return uuid.uuid4().hex


def load_job_for_update(db: Any, job_id: str | uuid.UUID) -> Job | None:
    """Load a Job under ``SELECT ... FOR UPDATE`` for an atomic RMW."""
    job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    return db.get(Job, job_uuid, with_for_update=True)


def active_job_for_update(
    db: Any,
    job_id: str | uuid.UUID,
    *,
    operation: str,
) -> Job | None:
    """Return the locked Job unless it is absent or terminally cancelled."""
    job = load_job_for_update(db, job_id)
    if job is None:
        return None
    if getattr(job, "status", None) == CANCELLED_JOB_STATUS:
        log.info(
            "cancelled_job_worker_write_skipped",
            job_id=str(job_id),
            operation=operation,
        )
        return None
    return job


def delete_task_owned_outputs(job_id: str, object_paths: Iterable[str]) -> None:
    """Best-effort delete exact per-attempt outputs, never stable/user keys.

    Callers invoke this only after releasing any Job row lock.  Refusing paths
    outside the private ``task-runs`` namespace makes an accidental future call
    with a stable Job key fail closed instead of deleting forensic or user data.
    """
    from app.storage import delete_object_best_effort  # noqa: PLC0415

    for object_path in dict.fromkeys(path for path in object_paths if path):
        owns_job_segment = f"/{job_id}/" in f"/{object_path.lstrip('/')}"
        if TASK_RUN_PATH_SEGMENT not in object_path or not owns_job_segment:
            log.error(
                "task_output_cleanup_refused_non_owned_path",
                job_id=job_id,
                object_path=object_path,
            )
            continue
        if not delete_object_best_effort(object_path):
            log.warning(
                "cancelled_job_task_output_cleanup_failed",
                job_id=job_id,
                object_path=object_path,
            )
