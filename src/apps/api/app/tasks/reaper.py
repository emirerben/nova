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

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from celery import Celery
from sqlalchemy import and_, func, literal_column, or_, select, update

from app.database import sync_session
from app.models import Job
from app.services.durable_attempt_cleanup import reconcile_storage_attempt_cleanup
from app.services.queue_state import get_live_job_index
from app.services.speech_cleanup_terminal import terminalize_required_speech_generations

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
# never grow a stuck variant out of nowhere. The PostgreSQL JSONB predicate
# below then limits the bounded ID-only discovery page to rows with repairable
# state instead of loading every recent terminal assembly plan.
_RECONCILE_LOOKBACK_DAYS = 7

# Keep the terminal-job watchdog bounded even when the seven-day window contains
# a large fleet of completed jobs. Discovery reads IDs only; each candidate is
# then independently locked, revalidated, and committed.
_STUCK_VARIANT_RECONCILE_BATCH = 50


def _terminal_reconcile_state_predicate() -> Any:
    """Return the JSONB predicate for state this watchdog can repair."""

    private = Job.assembly_plan.op("->")(literal_column("'_speech_cleanup_internal'"))
    required_speech_state = and_(
        func.jsonb_typeof(private) == literal_column("'object'"),
        or_(
            private.op("?")(literal_column("'required_speech_generation_locks'")),
            private.op("?")(literal_column("'staged_render_results'")),
            private.op("?")(literal_column("'working_render_variants'")),
            private.op("?")(literal_column("'terminal_pending'")),
        ),
    )
    stuck_variant = Job.assembly_plan.op("@?")(
        literal_column(
            '\'$.variants[*] ? (@.render_status == "rendering" '
            '|| @.render_status == "pending")\'::jsonpath'
        )
    )
    return or_(stuck_variant, required_speech_state)


@dataclass(frozen=True)
class CancelledRequiredSpeechReconciliation:
    status: Literal[
        "absent",
        "terminalized",
        "deferred",
        "unavailable",
        "not_cancelled",
    ]
    reason: str | None = None

    @property
    def cleanup_safe(self) -> bool:
        return self.status in {"absent", "terminalized"}


def _append_required_speech_terminal_outcomes(
    pipeline_trace: list[Any] | None,
    terminal_contexts: tuple[dict[str, Any], ...],
    *,
    outcome: str,
    failure_phase: str | None = None,
    failure_class: str | None = None,
) -> list[Any] | None:
    """Append exact-capsule hard-kill outcomes without exposing media data.

    ``terminal_contexts`` comes only from a successful ownership terminalization;
    a blocked transition returns none.  Building or appending observability is
    deliberately fail-open: lifecycle recovery remains authoritative even when a
    legacy/malformed capsule cannot produce a bounded outcome.
    """

    from app.services.speech_cleanup_outcome import (  # noqa: PLC0415
        append_speech_cleanup_render_outcome_locked,
        build_speech_cleanup_render_outcome,
    )

    holder = SimpleNamespace(pipeline_trace=pipeline_trace)
    for context in terminal_contexts:
        try:
            payload = build_speech_cleanup_render_outcome(
                outcome=outcome,
                analysis_attempt_id=str(context["analysis_attempt_id"]),
                analysis_view=context["analysis_view"],
                detector_version=str(context["detector_version"]),
                source_tag=context.get("source_tag"),
                variant_id=str(context["variant_id"]),
                render_generation_id=str(context["render_generation_id"]),
                selected_plan=context.get("selected_plan"),
                candidate_status=context.get("candidate_status"),
                output_removal_count=context.get("output_removal_count"),
                output_removed_ms=context.get("output_removed_ms"),
                failure_phase=failure_phase,
                failure_class=failure_class,
            )
        except Exception as exc:  # noqa: BLE001 - state recovery must remain fail-open
            log.warning(
                "required_speech_reaper_outcome_build_failed",
                error_class=type(exc).__name__,
            )
            continue
        append_speech_cleanup_render_outcome_locked(holder, payload)
    return holder.pipeline_trace


def _append_reaper_failed_owned_outcomes(
    pipeline_trace: list[Any] | None,
    terminal_contexts: tuple[dict[str, Any], ...],
) -> list[Any] | None:
    return _append_required_speech_terminal_outcomes(
        pipeline_trace,
        terminal_contexts,
        outcome="failed_owned",
        failure_phase="render",
        failure_class="WorkerDied",
    )


def reconcile_terminal_storage_attempts(job_ids: list[object]) -> int:
    """Run one bounded cleanup step after a reaper terminal transition.

    The state transition commits first. Storage latency therefore cannot hold
    the reaper's bulk-update transaction, and a failure merely leaves the
    durable receipt for the indexed Beat pass.
    """
    receipts_seen = 0
    for job_id in dict.fromkeys(job_ids):
        try:
            result = reconcile_storage_attempt_cleanup(
                job_id,
                source_limit=1,
                render_limit=1,
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — durable receipt remains queued
            log.warning(
                "reaper_storage_attempt_cleanup_failed",
                job_id=str(job_id),
                error_class=type(exc).__name__,
            )
            continue
        receipts_seen += result.receipts_seen
    return receipts_seen


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
    live: set[str] | None = None,
) -> int:
    """Mark stale, unowned non-terminal jobs as processing_failed.

    Returns the number of rows updated. Returns 0 (no-op) when:
      - inspect() fails (treated as "unknown — skip this cycle")
      - no orphans match the criteria

    `live`: pass a pre-computed live-job-id set (from `_live_job_ids`) to
    skip this function's own inspect() call — used by `sweep_stale_jobs`
    (app/tasks/maintenance.py) so one Beat firing issues a SINGLE inspect()
    round-trip shared with `reconcile_stuck_variants`, instead of each
    function independently re-querying the broker. Omit (default None) to
    compute it internally, unchanged from before — the on-boot reaper in
    app/worker.py and ad-hoc/test callers rely on this default.

    Safe to call concurrently from multiple workers: candidate rows are
    claimed with ``FOR UPDATE SKIP LOCKED`` and each terminal write retains
    the non-terminal status predicate as defense in depth.
    """
    if live is None:
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
        # Lock before changing job status. Required-speech terminalization can
        # fail closed when its owner/receipt capsule is incomplete; in that case
        # the job and its variants must remain byte-for-byte unchanged so a later
        # retry can recover them. A bulk UPDATE-before-recovery would already have
        # made that job terminal and violated the publication barrier.
        candidate_rows = db.execute(
            select(Job.id, Job.assembly_plan, Job.pipeline_trace)
            .where(*where_clauses)
            .with_for_update(skip_locked=True)
        ).fetchall()

        count = 0
        reaped_job_ids: list[object] = []
        for job_id_val, assembly_plan, pipeline_trace in candidate_rows:
            final_plan = assembly_plan
            final_trace = pipeline_trace
            if isinstance(assembly_plan, dict):
                recovery = terminalize_required_speech_generations(
                    assembly_plan,
                    job_id=str(job_id_val),
                    error="render interrupted: worker died",
                )
                if recovery.status == "blocked":
                    log.warning(
                        "required_speech_terminalization_blocked",
                        job_id=str(job_id_val),
                        reason=recovery.reason,
                        reaper="orphan_job",
                    )
                    # Ownership/debt ambiguity must not fall through to the
                    # generic path or alter the enclosing job status.
                    continue

                recovered_plan = recovery.plan
                final_trace = _append_reaper_failed_owned_outcomes(
                    pipeline_trace,
                    recovery.terminal_contexts,
                )
                variants = recovered_plan.get("variants")
                if variants:
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
                    final_plan = (
                        {**recovered_plan, "variants": new_variants}
                        if new_variants != variants
                        else recovered_plan
                    )
                else:
                    final_plan = recovered_plan

            terminal_values: dict[str, object] = {
                "status": "processing_failed",
                "failure_reason": "unknown",
                "error_detail": _ERROR_DETAIL,
            }
            if final_plan != assembly_plan:
                terminal_values["assembly_plan"] = final_plan
            if final_trace != pipeline_trace:
                terminal_values["pipeline_trace"] = final_trace
            result = db.execute(
                update(Job)
                .where(
                    Job.id == job_id_val,
                    Job.status.in_(_NON_TERMINAL_STATUSES),
                )
                .values(**terminal_values)
            )
            if result.rowcount:
                count += 1
                reaped_job_ids.append(job_id_val)

        db.commit()

    reconcile_terminal_storage_attempts(reaped_job_ids)

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
    live: set[str] | None = None,
    batch_limit: int = _STUCK_VARIANT_RECONCILE_BATCH,
) -> int:
    """Flip variants frozen at "rendering"/"pending" on TERMINAL-status jobs.

    `reap_orphans` only reconciles variants on jobs whose JOB-level status is
    still worker-owned non-terminal. It misses the common case where the job is
    already terminal (e.g. `variants_ready`) but a single-variant re-render
    (swap-song / retext / instant edit) died mid-flight, leaving that one tile
    stuck "rendering". The frontend's `anyRendering` poll-stop predicate then
    polls the frozen tile forever — exactly the "stuck in rendering even though
    it's ready" symptom. This sweep closes that gap.

    `live`: see `reap_orphans` — same pre-computed-set / shared-inspect()
    optimization, same default-None (compute internally) behavior.

    Returns the number of jobs whose variants were reconciled. No-op (0) when
    inspect() fails — same "don't act on unknown" safety as `reap_orphans`.
    """
    if live is None:
        live = _live_job_ids(celery_app)
    if live is None:
        return 0
    if batch_limit < 1:
        return 0

    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=threshold_min)
    lookback = now - timedelta(days=_RECONCILE_LOOKBACK_DAYS)

    state_predicate = _terminal_reconcile_state_predicate()
    discovery_clauses = [
        Job.status.notin_(_NON_TERMINAL_STATUSES),
        Job.status != "cancelled",
        Job.updated_at < cutoff,
        Job.updated_at >= lookback,
        Job.assembly_plan.isnot(None),
        state_predicate,
    ]
    live_job_uuids: list[uuid.UUID] = []
    for raw_job_id in live:
        try:
            live_job_uuids.append(uuid.UUID(str(raw_job_id)))
        except (TypeError, ValueError, AttributeError):
            # Celery queues can contain tasks whose first positional argument
            # is not a Job UUID. Keep those out of the UUID SQL bind while the
            # exact string set remains authoritative for the locked re-check.
            continue
    if live_job_uuids:
        discovery_clauses.append(Job.id.notin_(live_job_uuids))

    fixed = 0
    fixed_job_ids: list[object] = []
    with sync_session() as db:
        # Discovery is deliberately read-only and ID-only. Loading the JSONB
        # documents (and taking a row lock) is deferred until a bounded set of
        # rows has proved it contains repairable state.
        candidate_ids = list(
            db.execute(
                select(Job.id)
                .where(*discovery_clauses)
                .order_by(Job.updated_at.asc(), Job.id.asc())
                .limit(batch_limit)
            )
            .scalars()
            .all()
        )

        for candidate_id in candidate_ids:
            # One short transaction per candidate. The same predicates are
            # repeated under the lock because status/state may have changed
            # after discovery. SKIP LOCKED prevents concurrent sweepers from
            # queueing behind a creator mutation or another watchdog.
            locked_row = db.execute(
                select(Job.id, Job.assembly_plan, Job.pipeline_trace)
                .where(
                    Job.id == candidate_id,
                    Job.status.notin_(_NON_TERMINAL_STATUSES),
                    Job.status != "cancelled",
                    Job.updated_at < cutoff,
                    Job.updated_at >= lookback,
                    Job.assembly_plan.isnot(None),
                    _terminal_reconcile_state_predicate(),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            ).fetchone()
            if locked_row is None:
                db.commit()
                continue
            job_id_val, assembly_plan, pipeline_trace = locked_row
            # A re-render actively running on a live worker is NEVER reaped.
            if live and str(job_id_val) in live:
                db.commit()
                continue
            if not isinstance(assembly_plan, dict):
                db.commit()
                continue
            recovery = terminalize_required_speech_generations(
                assembly_plan,
                job_id=str(job_id_val),
                error="render interrupted: worker died (reaped as stuck)",
            )
            if recovery.status == "blocked":
                log.warning(
                    "required_speech_terminalization_blocked",
                    job_id=str(job_id_val),
                    reason=recovery.reason,
                    reaper="terminal_job",
                )
                db.commit()
                continue
            recovered_plan = recovery.plan
            final_trace = _append_reaper_failed_owned_outcomes(
                pipeline_trace,
                recovery.terminal_contexts,
            )
            variants = recovered_plan.get("variants")
            if not variants:
                if recovered_plan != assembly_plan:
                    values: dict[str, Any] = {"assembly_plan": recovered_plan}
                    if final_trace != pipeline_trace:
                        values["pipeline_trace"] = final_trace
                    db.execute(
                        update(Job)
                        .where(Job.id == job_id_val, Job.status != "cancelled")
                        .values(**values)
                    )
                    fixed += 1
                    fixed_job_ids.append(job_id_val)
                db.commit()
                continue
            new_variants = [_finalize_stuck_variant(v) for v in variants]
            final_plan = (
                {**recovered_plan, "variants": new_variants}
                if new_variants != variants
                else recovered_plan
            )
            if final_plan != assembly_plan:
                values = {"assembly_plan": final_plan}
                if final_trace != pipeline_trace:
                    values["pipeline_trace"] = final_trace
                db.execute(
                    update(Job)
                    .where(Job.id == job_id_val, Job.status != "cancelled")
                    .values(**values)
                )
                fixed += 1
                fixed_job_ids.append(job_id_val)
            db.commit()

    reconcile_terminal_storage_attempts(fixed_job_ids)

    if fixed:
        log.info(
            "stuck_variant_reconcile",
            count=fixed,
            threshold_min=threshold_min,
        )
    return fixed


def reconcile_cancelled_required_speech_job(
    job_id: str | uuid.UUID,
) -> CancelledRequiredSpeechReconciliation:
    """Release one cancelled private owner after upload/claim proof is safe.

    The caller obtains IDs from the existing indexed cleanup-debt sweep.  This
    helper then locks the exact row and mutates only its private plan and bounded
    trace: cancellation status/timestamps remain authoritative and unchanged.
    A fresh upload lease or finalizer claim returns ``deferred`` byte-for-byte, so
    the following cleanup pass can only delete a prefix after terminalization.
    """

    try:
        job_uuid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    except (TypeError, ValueError):
        return CancelledRequiredSpeechReconciliation("unavailable", "invalid_job_id")

    with sync_session() as db:
        job = db.get(Job, job_uuid, with_for_update=True)
        if job is None:
            return CancelledRequiredSpeechReconciliation("unavailable", "job_missing")
        if job.status != "cancelled":
            return CancelledRequiredSpeechReconciliation("not_cancelled")
        if not isinstance(job.assembly_plan, dict):
            return CancelledRequiredSpeechReconciliation(
                "deferred",
                "assembly_plan_not_object",
            )
        recovery = terminalize_required_speech_generations(
            job.assembly_plan,
            job_id=str(job_uuid),
            error="render cancelled before private generation publication",
        )
        if recovery.status == "blocked":
            log.info(
                "cancelled_required_speech_terminalization_deferred",
                job_id=str(job_uuid),
                reason=recovery.reason,
            )
            return CancelledRequiredSpeechReconciliation("deferred", recovery.reason)
        if recovery.status == "unchanged":
            return CancelledRequiredSpeechReconciliation("absent")

        job.assembly_plan = recovery.plan
        job.pipeline_trace = _append_required_speech_terminal_outcomes(
            job.pipeline_trace,
            recovery.terminal_contexts,
            outcome="cancelled_owned",
        )
        db.commit()
        return CancelledRequiredSpeechReconciliation("terminalized")
