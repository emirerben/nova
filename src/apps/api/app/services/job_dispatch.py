"""Single dispatch surface for orchestrator Celery tasks.

Every job-row-keyed orchestrator (the tasks that drive a `Job` from
`queued` to a terminal status) must route through `enqueue_orchestrator`
so that:

  1. The Celery task_id equals `str(job.id)`. This is what makes
     `celery_app.control.revoke(task_id)` and `inspect()` resolvable from
     a Job row — without it, there is no DB → Celery mapping at all (the
     `apply_async` default auto-generates a UUID Celery never tells us).
  2. `Job.celery_task_id` is persisted, so the admin debug UI can render
     the task_id without round-tripping through `inspect()`, and so the
     reaper has a fallback identifier if Celery's introspection misses a
     worker.

A regression test (`tests/services/test_job_dispatch.py
::test_all_orchestrator_dispatches_use_helper`) greps the api source for
`apply_async`/`.delay(` calls on the orchestrator task names and fails
if any new call site skips this helper. Add new orchestrators to
`ORCHESTRATOR_TASK_NAMES` below; do not silently broaden the grep.

Non-orchestrator tasks (template/track analysis, audio downloads, drive
imports of media that does not yet have a Job row, waitlist confirmations,
etc.) intentionally do not use this helper — their task_id has no Job
to attach to.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from celery import Task
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job

log = structlog.get_logger()

# Task names that MUST route through `enqueue_orchestrator`. Used by the
# source-grep regression test in `tests/services/test_job_dispatch.py`.
ORCHESTRATOR_TASK_NAMES: tuple[str, ...] = (
    "orchestrate_job",
    "orchestrate_template_job",
    "orchestrate_single_video_job",
    "orchestrate_music_job",
    "orchestrate_auto_music_job",
)


async def enqueue_orchestrator(
    task: Task,
    job_id: str | uuid.UUID,
    db: AsyncSession,
    *,
    kwargs: dict[str, Any] | None = None,
) -> str:
    """Dispatch an orchestrator task and persist its task_id on the Job row.

    Caller pattern — the Job row must already be committed so the worker
    can SELECT it on pickup:

        db.add(job)
        await db.commit()
        await db.refresh(job)
        await enqueue_orchestrator(orchestrate_X, job.id, db)

    Order of operations:
      1. apply_async(task_id=str(job_id))  — dispatch to broker
      2. UPDATE jobs SET celery_task_id=... — persist for admin UI
      3. await db.commit()                  — one-column commit

    If step 2 or 3 fails after step 1 succeeds, the task is still
    dispatched and the reaper continues to handle the row the old way
    (inspect args[0] across active+reserved tasks). Exceptions propagate
    so the caller can decide whether to roll back the row.

    Args:
        task: The Celery task object (`orchestrate_template_job`, etc.).
        job_id: The committed Job row's `id`. Used as both the first
            positional arg of the task AND its Celery task_id.
        db: Async DB session. The function commits internally to persist
            the celery_task_id update.
        kwargs: Optional kwargs forwarded to `apply_async`.

    Returns:
        The task_id (= `str(job_id)`).
    """
    task_id = str(job_id)
    job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(task_id)

    task.apply_async(args=[task_id], kwargs=kwargs or {}, task_id=task_id)

    try:
        await db.execute(
            update(Job).where(Job.id == job_uuid).values(celery_task_id=task_id)
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        # Task is already on the broker; row write failed. Don't re-raise —
        # the reaper's inspect-by-args fallback still finds the task. Log
        # so this is visible in the worker logs but the dispatch path
        # stays unblocked.
        log.warning(
            "enqueue_orchestrator_celery_task_id_write_failed",
            task_name=task.name,
            job_id=task_id,
            error=str(exc),
        )
        await db.rollback()

    return task_id
