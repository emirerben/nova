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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job

log = structlog.get_logger()

_DISPATCH_FAILURE_REASON = "dispatch_publish_failed"
_DISPATCH_FAILURE_DETAIL = "The job couldn't be handed to the queue. Please try again."

# Task names that MUST route through `enqueue_orchestrator`. Used by the
# source-grep regression test in `tests/services/test_job_dispatch.py`.
ORCHESTRATOR_TASK_NAMES: tuple[str, ...] = (
    "orchestrate_job",
    "orchestrate_template_job",
    "orchestrate_single_video_job",
    "orchestrate_music_job",
    "orchestrate_auto_music_job",
    "orchestrate_generative_job",
    "render_lyrics_preview_task",
)


async def _recover_async_publish_failure(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    task_name: str,
    publish_error: Exception,
) -> bool:
    """Terminalize only a still-queued Job after broker publication raises.

    ``apply_async`` can raise after the broker accepted the message.  The
    worker's queued -> processing claim therefore wins this compare-and-set;
    cancellation and every other newer state are immutable here.  This UPDATE
    runs only after broker I/O has returned, so no database lock spans the
    network call.
    """

    try:
        result = await db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(
                status="processing_failed",
                failure_reason=_DISPATCH_FAILURE_REASON,
                error_detail=_DISPATCH_FAILURE_DETAIL,
            )
        )
        await db.commit()
        recovered = int(getattr(result, "rowcount", 0) or 0) == 1
    except Exception as recovery_error:  # noqa: BLE001
        await db.rollback()
        log.error(
            "enqueue_orchestrator_publish_recovery_failed",
            task_name=task_name,
            job_id=str(job_id),
            publish_error=str(publish_error),
            recovery_error=str(recovery_error),
        )
        return False

    log.error(
        "enqueue_orchestrator_publish_failed",
        task_name=task_name,
        job_id=str(job_id),
        recovered=recovered,
        error=str(publish_error),
    )
    return recovered


def _recover_sync_publish_failure(
    *,
    job_id: uuid.UUID,
    task_name: str,
    publish_error: Exception,
) -> bool:
    """Synchronous twin of :func:`_recover_async_publish_failure`."""

    from app.database import sync_session  # noqa: PLC0415

    try:
        with sync_session() as db:
            result = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(
                    status="processing_failed",
                    failure_reason=_DISPATCH_FAILURE_REASON,
                    error_detail=_DISPATCH_FAILURE_DETAIL,
                )
            )
            db.commit()
            recovered = int(getattr(result, "rowcount", 0) or 0) == 1
    except Exception as recovery_error:  # noqa: BLE001
        log.error(
            "enqueue_orchestrator_publish_recovery_failed",
            task_name=task_name,
            job_id=str(job_id),
            publish_error=str(publish_error),
            recovery_error=str(recovery_error),
        )
        return False

    log.error(
        "enqueue_orchestrator_publish_failed",
        task_name=task_name,
        job_id=str(job_id),
        recovered=recovered,
        error=str(publish_error),
    )
    return recovered


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
      1. Read current status and return without publishing if cancelled.
      2. apply_async(task_id=str(job_id)) — dispatch to broker.
      3. Conditionally persist celery_task_id while status != cancelled.

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

    try:
        current_status = (
            await db.execute(select(Job.status).where(Job.id == job_uuid))
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        current_status = None
        log.warning(
            "enqueue_orchestrator_status_check_failed",
            task_name=task.name,
            job_id=task_id,
            error=str(exc),
        )
    if current_status == "cancelled":
        log.info(
            "enqueue_orchestrator_cancelled_job_skipped",
            task_name=task.name,
            job_id=task_id,
        )
        return task_id

    try:
        task.apply_async(args=[task_id], kwargs=kwargs or {}, task_id=task_id)
    except Exception as exc:
        await _recover_async_publish_failure(
            db,
            job_id=job_uuid,
            task_name=task.name,
            publish_error=exc,
        )
        raise

    try:
        await db.execute(
            update(Job)
            .where(Job.id == job_uuid, Job.status != "cancelled")
            .values(celery_task_id=task_id)
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


def enqueue_orchestrator_sync(
    task: Task,
    job_id: str | uuid.UUID,
    *,
    queue: str | None = None,
    kwargs: dict[str, Any] | None = None,
) -> str:
    """Sync-context analogue of `enqueue_orchestrator` for Celery tasks that
    dispatch a sub-orchestrator (e.g. content-plan per-item generation).

    A Celery task runs sync and holds a sync Session, so it can't await the
    async helper. This dispatches with `task_id=str(job_id)` (same contract:
    Celery task_id == Job id, so the reaper/admin can correlate) and routes to
    `queue` when given (the throttled `plan-jobs` queue, plan T3). The calling
    task commits first so the worker can load the row; this helper then performs
    a fresh status read and skips broker publication when cancellation is
    already visible.

    Returns the task_id (= `str(job_id)`).
    """
    task_id = str(job_id)
    job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(task_id)
    from app.database import sync_session  # noqa: PLC0415

    try:
        with sync_session() as db:
            current_status = db.execute(
                select(Job.status).where(Job.id == job_uuid)
            ).scalar_one_or_none()
        if current_status == "cancelled":
            log.info(
                "enqueue_orchestrator_cancelled_job_skipped",
                task_name=task.name,
                job_id=task_id,
            )
            return task_id
    except Exception as exc:  # noqa: BLE001
        # Preserve the historical availability contract: a status-read outage
        # cannot strand a freshly committed job. The worker's immutable-status
        # guard remains the race/backstop after publication.
        log.warning(
            "enqueue_orchestrator_status_check_failed",
            task_name=task.name,
            job_id=task_id,
            error=str(exc),
        )
    opts: dict[str, Any] = {"args": [task_id], "kwargs": kwargs or {}, "task_id": task_id}
    if queue:
        opts["queue"] = queue
    try:
        task.apply_async(**opts)
    except Exception as exc:
        _recover_sync_publish_failure(
            job_id=job_uuid,
            task_name=task.name,
            publish_error=exc,
        )
        raise
    return task_id
