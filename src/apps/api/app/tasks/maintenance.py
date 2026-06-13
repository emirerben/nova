"""Periodic maintenance tasks.

`sweep_stale_jobs` runs on Celery Beat (default every 5 min — see worker.py
beat_schedule). It wraps the existing `reap_orphans` reaper so the same
"is_live?" cross-check applies — workers running real jobs are never
reaped.

Why both this AND the on-boot reaper in worker.py:
  - On-boot reaper catches orphans created by SIGKILL'd workers, but only
    at the next deploy / worker restart. In a steady-state system without
    deploys, stuck rows can sit for hours.
  - This periodic sweep closes the gap: orphans get marked failed within
    ~5 min, regardless of deploy cadence.

Failure mode of the sweep itself is best-effort: any exception is logged
and swallowed so a transient DB blip during the sweep doesn't kill
Beat or crash the worker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text

from app.tasks.reaper import reap_orphans, reconcile_stuck_variants
from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(
    name="tasks.sweep_stale_jobs",
    bind=True,
    # If the sweep itself hits a DB blip, retry once with backoff. Don't
    # retry indefinitely — Beat re-fires every 5 min anyway.
    autoretry_for=(),  # Beat handles re-firing; no autoretry needed
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def sweep_stale_jobs(self) -> int:
    """Mark stale, unowned non-terminal jobs as processing_failed.

    Returns the number of rows updated. Logs are written by `reap_orphans`
    itself when count > 0.
    """
    try:
        count = reap_orphans(celery_app)
        # Also reconcile variants frozen on already-terminal jobs (dead
        # single-variant re-renders) — reap_orphans only covers jobs whose
        # JOB-level status is still non-terminal. Independent try so one
        # failing doesn't skip the other.
        try:
            reconcile_stuck_variants(celery_app)
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile_stuck_variants_failed", error=str(exc))
        return count
    except Exception as exc:  # noqa: BLE001
        log.warning("sweep_stale_jobs_failed", error=str(exc))
        return 0


# Job-scoped prefixes that may hold a job's temp uploads / intermediate
# encodes. Matches the GCS lifecycle rule in infra/gcs-lifecycle.json —
# anything outside these prefixes either persists (templates/, music/
# library tracks) or is handled by the lifecycle rule's 24h delete.
_JOB_TEMP_PREFIXES = ("dev-user/", "music-jobs/")


@celery_app.task(
    name="tasks.cleanup_cancelled_job",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def cleanup_cancelled_job(self, job_id: str) -> int:
    """Best-effort delete of GCS objects under `<prefix>/{job_id}/`.

    The 24h bucket lifecycle rule (see CLAUDE.md "Storage retention") is
    the real backstop — this task just removes temp files sooner when
    an admin cancels. Failures are logged and swallowed; never raises.
    Returns the number of objects deleted.
    """
    from app.config import settings  # noqa: PLC0415
    from app.storage import _get_client  # noqa: PLC0415

    try:
        bucket = _get_client().bucket(settings.storage_bucket)
    except Exception as exc:  # noqa: BLE001
        log.warning("cleanup_cancelled_job_client_failed", job_id=job_id, error=str(exc))
        return 0

    deleted = 0
    for prefix_root in _JOB_TEMP_PREFIXES:
        prefix = f"{prefix_root}{job_id}/"
        try:
            blobs = list(bucket.list_blobs(prefix=prefix))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cleanup_cancelled_job_list_failed",
                job_id=job_id,
                prefix=prefix,
                error=str(exc),
            )
            continue

        for blob in blobs:
            try:
                blob.delete()
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "cleanup_cancelled_job_delete_failed",
                    job_id=job_id,
                    blob=blob.name,
                    error=str(exc),
                )

    if deleted:
        log.info("cleanup_cancelled_job_done", job_id=job_id, deleted=deleted)
    return deleted


# Per-batch ceiling for the agent_run pruner. Caps the row count held by any
# single DELETE so the table never sees a long-running ACCESS EXCLUSIVE lock,
# even on a first-run backfill against months of accumulated rows.
_AGENT_RUN_DELETE_BATCH = 10_000

# Hard upper bound on iteration count per task run. With the batch above,
# one Beat firing can prune up to 1M rows; if there's more than that backed
# up, the next day's run picks up where this one left off. This is a fuse
# against runaway loops, not a steady-state expectation.
_AGENT_RUN_DELETE_MAX_BATCHES = 100


@celery_app.task(
    name="tasks.cleanup_agent_runs",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    # Soft/hard limits match the budget of a midnight-quiet pruning window.
    # If we're hitting the hard limit it's a sign of either a backfill in
    # progress (acceptable, next run resumes) or a stuck statement (which
    # we want killed, not retried).
    soft_time_limit=600,
    time_limit=900,
)
def cleanup_agent_runs(self, retention_days: int | None = None) -> dict:
    """Delete job-scoped agent_run rows older than the retention window.

    Returns a dict {deleted, cutoff, batches} for observability.

    Why job_id-scoped: template- and track-scoped agent_run rows (job_id
    NULL) back the per-template / per-track debug views, are looked up
    by parent fk, and are bounded by template/track count rather than
    job volume. Pruning them would surprise admins reviewing template
    history. The job-scoped rows are the ones that grow with traffic
    and are useful for at most a few weeks.

    Why a batched DELETE: a single unbounded DELETE on a large table
    would hold its locks for the full duration. The batched form
    keeps each transaction short and lets other queries make progress
    between batches.
    """
    from app.config import settings  # noqa: PLC0415
    from app.database import sync_engine  # noqa: PLC0415

    days = retention_days if retention_days is not None else settings.agent_run_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=days)

    total_deleted = 0
    batches = 0
    # Each batch runs in its own short transaction so the table never
    # accumulates lock duration across iterations.
    while batches < _AGENT_RUN_DELETE_MAX_BATCHES:
        with sync_engine.begin() as conn:
            res = conn.execute(
                text(
                    """
                    DELETE FROM agent_run
                     WHERE id IN (
                       SELECT id FROM agent_run
                        WHERE job_id IS NOT NULL
                          AND created_at < :cutoff
                        LIMIT :batch
                     )
                    """
                ),
                {"cutoff": cutoff, "batch": _AGENT_RUN_DELETE_BATCH},
            )
            deleted = res.rowcount or 0
        total_deleted += deleted
        batches += 1
        if deleted < _AGENT_RUN_DELETE_BATCH:
            # Final batch: fewer rows than the limit means nothing left.
            break

    if total_deleted:
        log.info(
            "cleanup_agent_runs_done",
            deleted=total_deleted,
            cutoff=cutoff.isoformat(),
            batches=batches,
            retention_days=days,
        )
    return {
        "deleted": total_deleted,
        "cutoff": cutoff.isoformat(),
        "batches": batches,
    }
