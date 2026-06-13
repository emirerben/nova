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
from sqlalchemy import select, update

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

# Per-variant statuses that mean "still working" — eligible for reconciliation
# when a variant is stuck in one past the threshold.
_STUCK_VARIANT_STATUSES = ("rendering", "pending")

# `reconcile_stuck_variants` only looks back this far. A long-completed job will
# never grow a stuck variant out of nowhere, and bounding the scan to a recent
# window keeps it cheap WITHOUT a Postgres-only JSONB path query (so the sweep
# stays dialect-agnostic and unit-testable with a mocked session).
_RECONCILE_LOOKBACK_DAYS = 7


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

    _ERROR_DETAIL = "Worker died with no recovery; reaped on worker startup. Resubmit your job."

    with sync_session() as db:
        # Flip job-level status and collect reaped rows for variant reconciliation.
        stmt = (
            update(Job)
            .where(*where_clauses)
            .values(
                status="processing_failed",
                failure_reason="unknown",
                error_detail=_ERROR_DETAIL,
            )
            .returning(Job.id, Job.assembly_plan)  # type: ignore[attr-defined]
        )
        reaped_rows = db.execute(stmt).fetchall()
        count = len(reaped_rows)

        # Reconcile per-variant render_status.  When the worker is SIGKILL'd
        # mid-render the job-level status is now fixed, but any variant still at
        # "rendering" or "pending" in assembly_plan["variants"] is permanently
        # frozen — the frontend poll-stop predicate (anyRendering check) keeps
        # polling forever on that frozen variant.  Flip those variants to
        # "failed" here so the UI shows a terminal state immediately.
        for job_id_val, assembly_plan in reaped_rows:
            if not isinstance(assembly_plan, dict):
                continue
            variants = assembly_plan.get("variants")
            if not variants:
                continue
            new_variants = [
                {
                    **v,
                    "render_status": "failed",
                    "error": v.get("error") or "render interrupted: worker died",
                }
                if v.get("render_status") in ("rendering", "pending")
                else v
                for v in variants
            ]
            if new_variants != variants:
                db.execute(
                    update(Job)
                    .where(Job.id == job_id_val)
                    .values(assembly_plan={**assembly_plan, "variants": new_variants})
                )

        db.commit()

    if count:
        log.info(
            "reaper_swept",
            count=count,
            threshold_min=threshold_min,
            live_job_count=len(live),
        )
    return count


def _finalize_stuck_variant(v: dict) -> dict:
    """Flip a single stuck variant to a terminal render_status.

    A variant that already has a last-good rendered video (`video_path`) is
    flipped to "ready" — the file is playable; only the status was frozen.
    One with no output is a "failed" render.
    """
    if not isinstance(v, dict) or v.get("render_status") not in _STUCK_VARIANT_STATUSES:
        return v
    if v.get("video_path"):
        return {**v, "render_status": "ready", "ok": True}
    return {
        **v,
        "render_status": "failed",
        "ok": False,
        "error": v.get("error") or "render interrupted: worker died (reaped as stuck)",
    }


def reconcile_stuck_variants(
    celery_app: Celery,
    *,
    threshold_min: int = THRESHOLD_MIN,
) -> int:
    """Flip variants frozen at "rendering"/"pending" on TERMINAL-status jobs.

    `reap_orphans` only reconciles variants on jobs whose JOB-level status is
    still worker-owned non-terminal. It misses the common case where the job is
    already terminal (e.g. `variants_ready`) but a single-variant re-render
    (swap-song / retext / instant edit) died mid-flight, leaving that one tile
    stuck "rendering". The frontend's `anyRendering` poll-stop predicate then
    polls the frozen tile forever — exactly the "stuck in rendering even though
    it's ready" symptom. This sweep closes that gap.

    Returns the number of jobs whose variants were reconciled. No-op (0) when
    inspect() fails — same "don't act on unknown" safety as `reap_orphans`.
    """
    live = _live_job_ids(celery_app)
    if live is None:
        return 0

    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=threshold_min)
    lookback = now - timedelta(days=_RECONCILE_LOOKBACK_DAYS)

    fixed = 0
    with sync_session() as db:
        rows = db.execute(
            select(Job.id, Job.assembly_plan).where(
                Job.status.notin_(_NON_TERMINAL_STATUSES),
                Job.updated_at < cutoff,
                Job.updated_at >= lookback,
                Job.assembly_plan.isnot(None),
            )
        ).fetchall()

        for job_id_val, assembly_plan in rows:
            # A re-render actively running on a live worker is NEVER reaped.
            if live and str(job_id_val) in live:
                continue
            if not isinstance(assembly_plan, dict):
                continue
            variants = assembly_plan.get("variants")
            if not variants:
                continue
            new_variants = [_finalize_stuck_variant(v) for v in variants]
            if new_variants != variants:
                db.execute(
                    update(Job)
                    .where(Job.id == job_id_val)
                    .values(assembly_plan={**assembly_plan, "variants": new_variants})
                )
                fixed += 1

        db.commit()

    if fixed:
        log.info(
            "stuck_variant_reconcile",
            count=fixed,
            threshold_min=threshold_min,
        )
    return fixed
