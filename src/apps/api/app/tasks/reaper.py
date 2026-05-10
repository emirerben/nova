"""Orphan-job reaper.

Marks template jobs as `processing_failed` when:
  1. status is non-terminal (`processing` or `template_ready`)
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

log = structlog.get_logger()

# Reap jobs whose status hasn't moved in THRESHOLD_MIN minutes. Set to
# 2× the multi-clip orchestrate_template_job hard time_limit (1800s) so
# a legitimately slow finisher always wins the race against the reaper.
THRESHOLD_MIN = 60

# Celery inspect() timeout in seconds. Generous because the broker can
# be slow under load; we'd rather wait than skip a sweep.
_INSPECT_TIMEOUT_S = 5

# Status values that are non-terminal — eligible for reaping when stale.
# `template_ready` is set immediately after the recipe is loaded (still
# very early in the pipeline); `processing` is set as the job enters the
# worker. Both can be "stuck" if the worker dies mid-flight.
_NON_TERMINAL_STATUSES = ("processing", "template_ready")


def _live_job_ids(celery_app: Celery) -> set[str] | None:
    """Return job_ids currently held by live Celery workers, or None on failure.

    None means inspect() didn't return — the safe interpretation is "I don't
    know, don't reap anything" rather than "no jobs are live, reap them all".

    A job_id is the first positional arg of every orchestrate-style task,
    e.g. `orchestrate_template_job.delay(job_id)`.
    """
    try:
        inspector = celery_app.control.inspect(timeout=_INSPECT_TIMEOUT_S)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("reaper_inspect_failed", error=str(exc))
        return None

    live: set[str] = set()
    for tasks in (*active.values(), *reserved.values()):
        for task in tasks:
            args = task.get("args") or []
            if args:
                live.add(str(args[0]))
    return live


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
                    "Worker died with no recovery; reaped on worker startup. "
                    "Resubmit your job."
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
