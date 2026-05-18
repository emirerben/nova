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
