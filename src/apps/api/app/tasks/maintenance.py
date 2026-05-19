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

import structlog

from app.tasks.reaper import reap_orphans
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
