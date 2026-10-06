"""Shared job-cancellation service (admin cancel + user cancel-render, KRI-443).

Extracted from `routes/admin_jobs.cancel_job` so both callers share one body:

1. `lock_and_cancel_job` -- SELECT FOR UPDATE the Job, terminalize speech, flip the
   status and append the audit event, then COMMIT (releasing every lock).
2. `revoke_and_cleanup_job` -- only after the locks are released: revoke the Celery
   task(s), delete provably-unsent TikTok snapshots, enqueue the cleanup task.

Callers must not hold any other row lock across (1) beyond what the canonical order
(`app/db_locks.py`) allows before Job.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.models import Job, TikTokPublication
from app.services.speech_cleanup_terminal import (
    active_speech_claim_task_id,
    terminalize_required_speech_generations,
)

log = structlog.get_logger()

# Mirror of CANCELLABLE_STATUSES in src/apps/web/src/app/admin/jobs/[id]/page.tsx.
# Keep this list intentional -- broader is dangerous.
CANCELLABLE_STATUSES = (
    "importing",
    "queued",
    "processing",
    "matching",
    "rendering",
    "posting",
)


class JobCancelError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class JobCancelOutcome:
    job_id: str
    previous_status: str
    task_id: str | None
    speech_task_id: str | None
    revoke_task_ids: list[str] = field(default_factory=list)
    publication_snapshots: list[str] = field(default_factory=list)


async def lock_and_cancel_job(
    db: AsyncSession,
    job_id: str,
    *,
    source: str = "admin",
    failure_reason: str = "cancelled_by_admin",
    error_detail: str = "Cancelled via admin UI",
    speech_error: str = "render cancelled by administrator",
    require_celery_task: bool = False,
) -> JobCancelOutcome:
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, TypeError) as exc:
        raise JobCancelError(400, f"Invalid job_id: {exc}") from exc

    job_res = await db.execute(select(Job).where(Job.id == job_uuid).with_for_update())
    job = job_res.scalar_one_or_none()
    if job is None:
        raise JobCancelError(404, "Job not found")

    # Capture the speech worker identity from the same locked snapshot used for
    # cancellation. Terminalization may legitimately clear this control below.
    speech_task_id = active_speech_claim_task_id(job.assembly_plan)

    terminalization = terminalize_required_speech_generations(
        job.assembly_plan or {},
        job_id=job_id,
        error=speech_error,
    )
    private_internal = (
        (job.assembly_plan or {}).get("_speech_cleanup_internal")
        if isinstance(job.assembly_plan, dict)
        else None
    )
    private_locks = (
        private_internal.get("required_speech_generation_locks")
        if isinstance(private_internal, dict)
        else None
    )
    terminal_gap_cancellable = job.status in {
        "variants_ready",
        "variants_ready_partial",
        "variants_failed",
    } and (
        (terminalization.status == "terminalized" and terminalization.restored_last_good)
        # Cancellation is an immediate tombstone even when fresh/malformed
        # private ownership cannot yet be safely released. Preserve the exact
        # owner on the cancelled row; the cancelled-row reaper will retry after
        # claim/upload leases expire. Never turn ambiguity into a public swap.
        or (
            terminalization.status == "blocked"
            and isinstance(private_locks, dict)
            and private_locks
        )
    )
    if job.status not in CANCELLABLE_STATUSES and not terminal_gap_cancellable:
        raise JobCancelError(
            409,
            f"Job status is '{job.status}' — only "
            f"{', '.join(CANCELLABLE_STATUSES)} jobs can be cancelled.",
        )
    if require_celery_task and not job.celery_task_id:
        await db.rollback()
        raise JobCancelError(409, "This render has no cancellable task.")

    previous_status = job.status
    task_id = job.celery_task_id
    revoke_task_ids = list(
        dict.fromkeys(value for value in (task_id, speech_task_id) if isinstance(value, str))
    )
    terminal_plan = (
        terminalization.plan if terminalization.status == "terminalized" else job.assembly_plan
    )
    if terminalization.status == "blocked":
        # Cancellation remains immediate, but ambiguous ownership is retained
        # on the cancelled row so the bounded Beat reconciler can retry safely.
        log.warning(
            f"{source}_cancel_required_speech_terminalization_blocked",
            job_id=job_id,
            reason=terminalization.reason,
        )

    # Lock and fail receipts that provably have not crossed the provider
    # boundary. ``submitting`` is deliberately excluded: that state can mean
    # TikTok already received an ambiguous request and must retain its audit
    # receipt. Lock order is Job -> TikTokPublication, matching the submit task.
    publication_rows = (
        (
            await db.execute(
                select(TikTokPublication)
                .where(
                    TikTokPublication.job_id == job_uuid,
                    TikTokPublication.processing_status.in_(["queued", "snapshotting"]),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    publication_snapshots = [
        row.snapshot_object_path for row in publication_rows if row.snapshot_object_path
    ]
    for publication in publication_rows:
        publication.processing_status = "failed"
        publication.retryable = False
        publication.next_poll_at = None
        publication.failure_code = "source_job_cancelled"
        publication.failure_detail = "The source video was cancelled before TikTok submission"
        publication.snapshot_object_path = None
        publication.media_token_hash = None
        publication.media_expires_at = None

    # Append the audit event while the row lock is held. Broker/network work is
    # deliberately deferred until after commit so a slow Celery control plane
    # cannot extend this database critical section.
    cancelled_at = datetime.now(UTC)
    cancel_event = {
        "ts": cancelled_at.isoformat(),
        "stage": "cancel",
        "event": f"{source}_cancel",
        "data": {
            "previous_status": previous_status,
            "task_id": task_id,
            "speech_task_id": speech_task_id,
            "revoke_requested": bool(revoke_task_ids),
        },
    }
    if terminalization.status == "terminalized":
        internal = (job.assembly_plan or {}).get("_speech_cleanup_internal")
        stages = internal.get("staged_render_results") if isinstance(internal, dict) else None
        if isinstance(stages, dict):
            from app.services.speech_cleanup_outcome import (  # noqa: PLC0415
                append_speech_cleanup_render_outcome_locked,
                build_speech_cleanup_render_outcome,
            )

            for staged in stages.values():
                if not isinstance(staged, dict):
                    continue
                context = staged.get("_speech_cleanup_outcome_context")
                generation = staged.get("render_generation_id")
                variant_id = staged.get("variant_id")
                if not isinstance(context, dict) or not generation or not variant_id:
                    continue
                try:
                    append_speech_cleanup_render_outcome_locked(
                        job,
                        build_speech_cleanup_render_outcome(
                            outcome="cancelled_owned",
                            analysis_attempt_id=str(context["analysis_attempt_id"]),
                            analysis_view=context["analysis_view"],
                            detector_version=str(context["detector_version"]),
                            source_tag=context.get("source_tag"),
                            variant_id=str(variant_id),
                            render_generation_id=str(generation),
                            selected_plan=context.get("selected_plan"),
                            candidate_status=context.get("candidate_status"),
                            output_removal_count=int(context.get("output_removal_count") or 0),
                            output_removed_ms=int(context.get("output_removed_ms") or 0),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - cancellation is authoritative
                    log.warning(
                        f"{source}_cancel_speech_outcome_build_failed",
                        job_id=job_id,
                        error_class=type(exc).__name__,
                    )

    trace = list(job.pipeline_trace or [])
    if len(trace) < 500:
        trace.append(cancel_event)
    result = await db.execute(
        update(Job)
        .where(Job.id == job_uuid, Job.status == previous_status)
        .values(
            status="cancelled",
            finished_at=cancelled_at,
            failure_reason=failure_reason,
            error_detail=error_detail,
            pipeline_trace=trace,
            assembly_plan=terminal_plan,
        )
    )
    if result.rowcount == 0:
        await db.rollback()
        raise JobCancelError(409, "Job reached a terminal status before cancellation could apply.")
    await db.commit()

    return JobCancelOutcome(
        job_id=job_id,
        previous_status=previous_status,
        task_id=task_id,
        speech_task_id=speech_task_id,
        revoke_task_ids=revoke_task_ids,
        publication_snapshots=publication_snapshots,
    )


def revoke_and_cleanup_job(outcome: JobCancelOutcome, *, source: str = "admin") -> bool:
    """Revoke + cleanup after the cancel committed. Returns whether a revoke was sent."""
    job_id = outcome.job_id
    revoke_task_ids = outcome.revoke_task_ids
    publication_snapshots = outcome.publication_snapshots
    from app.worker import celery_app  # noqa: PLC0415

    revoke_dispatched = False
    for revoke_task_id in revoke_task_ids:
        try:
            # terminate=True sends the configured signal to the worker
            # process running the task. SIGTERM lets a Python try/except
            # SoftTimeLimitExceeded-style handler run; SIGKILL would
            # drop pending DB writes and FFmpeg subprocesses uncleanly.
            celery_app.control.revoke(revoke_task_id, terminate=True, signal="SIGTERM")
            revoke_dispatched = True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"{source}_cancel_revoke_failed",
                job_id=job_id,
                task_id=revoke_task_id,
                error=str(exc),
            )

    # DB state is already terminal, so a cleanup outage cannot resurrect a
    # publication. Revoke first, then delete exact snapshot keys after locks
    # are released so storage latency cannot delay the worker stop request.
    for snapshot_path in publication_snapshots:
        storage.delete_object_best_effort(snapshot_path)

    # Best-effort cleanup. Lifecycle rule is the backstop, so a failure
    # to enqueue this task is non-fatal.
    #
    # countdown=30: SIGTERM doesn't synchronously kill the worker's
    # ffmpeg subprocess. The worker may keep writing to GCS for a few
    # seconds after revoke. Delaying cleanup by 30s avoids deleting a
    # clip the dying worker is still uploading, which would otherwise
    # produce orphaned partial blobs (harmless — lifecycle clears them
    # in 24h — but noisy).
    try:
        from app.tasks.maintenance import cleanup_cancelled_job  # noqa: PLC0415

        cleanup_cancelled_job.apply_async(args=[job_id], countdown=30)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            f"{source}_cancel_cleanup_enqueue_failed",
            job_id=job_id,
            error=str(exc),
        )

    return revoke_dispatched
