"""Orphan-job reaper.

Marks jobs as `processing_failed` when:
  1. status is a worker-owned non-terminal status (see
     `_NON_TERMINAL_STATUSES` — `processing`, `matching`, `rendering`,
     `posting`; NOT `template_ready`, which is a success terminal)
  2. updated_at is older than `THRESHOLD_MIN`
  3. no live Celery task references the job_id (cross-checked via
     `celery_app.control.inspect()`)

Designed to run on Celery `worker_ready` signal — see
[`app/worker.py`](../worker.py) for the wiring. The function is also
importable for tests and ad-hoc admin invocation.

Why this exists: even with `task_acks_late=True` + `visibility_timeout=1900`
(see worker.py and PR #70), workers SIGKILL'd by deploys/OOM occasionally
leave jobs in non-terminal status with `failure_reason=None`. Without a
sweeper, those orphans stay in the DB forever and the frontend shows users
a perpetual loading state.

Threshold rationale: 60 min = 2× the multi-clip hard `time_limit` (1800s).
A legitimately slow task at the boundary (e.g. 35 min in) will not be
reaped — it gets to finish or fail naturally. Only truly abandoned jobs
trip the sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from celery import Celery
from sqlalchemy import update

from app.database import sync_session
from app.models import Job
from app.services.queue_state import get_live_job_index

log = structlog.get_logger()

# Reap jobs whose status hasn't moved in THRESHOLD_MIN minutes. Set to
# 2× the multi-clip orchestrate_template_job hard time_limit (1800s) so
# a legitimately slow finisher always wins the race against the reaper.
THRESHOLD_MIN = 60

# Status values that are non-terminal AND worker-owned — eligible for
# reaping when stale. Each is set while a Celery task is actively executing;
# if the worker is SIGKILL'd mid-flight (deploy/OOM), the row stays stuck in
# that status forever with failure_reason=None and the frontend shows a
# perpetual loading state.
#
# This MUST stay in sync with the worker-owned subset of
# `_CANCELLABLE_STATUSES` in app/routes/admin_jobs.py:
#   - `processing` : template + music + generative jobs, entering the worker
#   - `matching`   : reserved mid-pipeline status
#   - `rendering`  : auto_music_orchestrate.py + generative_build.py flip to
#                    this once they start rendering variants. Adding it here
#                    is the fix for prod job 5ae0142f (generative edit killed
#                    by a deploy mid-render, stuck "rendering" forever — the
#                    reaper used to only know `processing`).
#   - `posting`    : reserved post-render status
#
# Deliberately EXCLUDES `queued`: a queued job not yet prefetched by a worker
# is invisible to inspect() (get_live_job_index only sees active+reserved), so
# reaping `queued` would false-positive a job legitimately waiting in a deep
# broker backlog. acks_late re-delivery is the recovery path for those.
#
# Do NOT include `template_ready` here. It looks "intermediate" by name but
# template_orchestrate.py sets it at the FINALIZE step (after assemble +
# audio mix + upload). It is the SUCCESS terminal state — every successful
# template job ends in `template_ready` and stays there. Reaping it would
# silently flip every completed job to `processing_failed` after the
# 60-minute threshold, which is what happened to prod job e3804f62.
_NON_TERMINAL_STATUSES = ("processing", "matching", "rendering", "posting")


def _live_job_ids(celery_app: Celery) -> set[str] | None:
    """Return job_ids currently held by live Celery workers, or None on failure.

    Thin wrapper over `app.services.queue_state.get_live_job_index` so the
    reaper and the admin job-debug UI use the same definition of "live".
    None means inspect() didn't return — the safe interpretation is "I don't
    know, don't reap anything" rather than "no jobs are live, reap them all".
    """
    index = get_live_job_index(celery_app)
    if not index.ok:
        return None
    return index.all_job_ids()


def reap_orphans(
    celery_app: Celery,
    *,
    threshold_min: int = THRESHOLD_MIN,
) -> int:
    """Mark stale, unowned non-terminal jobs as processing_failed.

    Returns the number of rows updated. Returns 0 (no-op) when:
      - inspect() fails (treated as "unknown — skip this cycle")
      - no orphans match the criteria

    Safe to call concurrently from multiple workers — the SQL UPDATE with
    a WHERE clause on `status` is atomic in postgres, and the same row
    won't be double-reaped because the second UPDATE filters on
    `status IN _NON_TERMINAL_STATUSES`.
    """
    live = _live_job_ids(celery_app)
    if live is None:
        # Don't reap on inspection failure — false positives (killing a
        # legitimately-running job) are worse than waiting for the next
        # worker startup to try again.
        return 0

    cutoff = datetime.now(UTC) - timedelta(minutes=threshold_min)

    # Build the WHERE clause. When `live` is empty, skip the NOT IN clause
    # entirely — SQLAlchemy issues an empty-IN warning AND some Postgres
    # query planners short-circuit `NOT IN ()` to false. Empty live set
    # means "no workers own any job," so every stale row is fair game.
    where_clauses = [
        Job.status.in_(_NON_TERMINAL_STATUSES),
        Job.updated_at < cutoff,
    ]
    if live:
        where_clauses.append(Job.id.notin_(live))

    with sync_session() as db:
        stmt = (
            update(Job)
            .where(*where_clauses)
            .values(
                status="processing_failed",
                failure_reason="unknown",
                error_detail=(
                    "Worker died with no recovery; reaped on worker startup. Resubmit your job."
                ),
            )
        )
        result = db.execute(stmt)
        db.commit()
        count = result.rowcount or 0

    if count:
        log.info(
            "reaper_swept",
            count=count,
            threshold_min=threshold_min,
            live_job_count=len(live),
        )
    return count
