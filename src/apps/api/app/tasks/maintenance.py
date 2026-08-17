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

import threading
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, case, or_, select, text

from app.database import sync_session
from app.models import Job, PlanItem, PlanItemAsset
from app.tasks.reaper import _live_job_ids, reap_orphans, reconcile_stuck_variants
from app.worker import celery_app

log = structlog.get_logger()

# A full 20-file batch can legitimately wait behind ten waves at concurrency 2;
# each task has a 5-minute hard limit. Do not invalidate healthy broker backlog.
_POOL_QUEUED_STALE_AFTER = timedelta(minutes=60)
_POOL_ANALYZING_STALE_AFTER = timedelta(minutes=10)
_POOL_MAX_ATTEMPTS = 3
_POOL_HEIF_DECODER_RECOVERY_MAX_ATTEMPTS = 2
_POOL_RECONCILE_BATCH = 100
_POOL_RESERVATION_TTL = timedelta(minutes=15)
_POOL_RESERVATION_CLEANUP_GRACE = timedelta(minutes=15)
# Bounded pass: pre-fix `ready` rows (video, or HEIC/HEIF image) that never got
# a browser-safe preview generated (`preview_gcs_path IS NULL`). Small batch —
# this is a backfill, not the fast path; new uploads get their preview inline
# in `analyze_pool_asset`.
_POOL_PREVIEW_BACKFILL_BATCH = 25


def _heif_decoder_recovery_predicate():
    """Rows terminalized by the missing HEIF decoder, eligible for one retry."""
    return and_(
        PlanItemAsset.status == "failed",
        PlanItemAsset.error_code == "analysis_unreadable",
        PlanItemAsset.upload_content_type.in_({"image/heic", "image/heif"}),
        PlanItemAsset.analysis_attempt_count < _POOL_HEIF_DECODER_RECOVERY_MAX_ATTEMPTS,
    )


def _pool_reconcile_priority():
    """Current creator work sorts ahead of historical decoder recovery."""
    return case((PlanItemAsset.status == "failed", 1), else_=0)


def _preview_backfill_predicate():
    """`ready` rows that predate the preview pipeline and never got one.

    Video (any codec — a poster helps everywhere) or HEIC/HEIF image.
    `preview_gcs_path IS NULL` excludes both already-previewed rows AND the
    ""-sentinel (attempted-and-failed) rows — a failed preview attempt is not
    retried by this bounded sweep.
    """
    return and_(
        PlanItemAsset.status == "ready",
        PlanItemAsset.preview_gcs_path.is_(None),
        or_(
            PlanItemAsset.kind == "video",
            PlanItemAsset.upload_content_type.in_({"image/heic", "image/heif"}),
        ),
    )


def reconcile_stale_pool_assets(*, now: datetime | None = None) -> int:
    """Requeue unclaimed analyses and terminalize exhausted/stuck attempts.

    A fresh token fences a late worker from overwriting the retry. Publication
    happens after commit so a worker can always observe and atomically claim its
    queued row.
    """
    from app.config import settings  # noqa: PLC0415
    from app.tasks.autoplace import analyze_pool_asset  # noqa: PLC0415

    current = now or datetime.now(UTC)
    queued_cutoff = current - _POOL_QUEUED_STALE_AFTER
    analyzing_cutoff = current - _POOL_ANALYZING_STALE_AFTER
    to_publish: list[tuple[str, str, str | None]] = []
    expired_reservations: list[
        tuple[
            uuid.UUID,
            uuid.UUID,
            str,
            str | None,
            list[tuple[str, str | None]],
            str | None,
        ]
    ] = []
    touched = 0

    with sync_session() as db:
        rows = (
            db.execute(
                select(PlanItemAsset)
                .where(
                    or_(
                        and_(
                            PlanItemAsset.status.in_({"preparing", "promoting"}),
                            or_(
                                PlanItemAsset.upload_expires_at
                                <= current - _POOL_RESERVATION_CLEANUP_GRACE,
                                and_(
                                    PlanItemAsset.upload_expires_at.is_(None),
                                    PlanItemAsset.created_at
                                    <= current
                                    - (_POOL_RESERVATION_TTL + _POOL_RESERVATION_CLEANUP_GRACE),
                                ),
                            ),
                        ),
                        PlanItemAsset.status == "cleanup_pending",
                        and_(
                            PlanItemAsset.status == "queued",
                            or_(
                                PlanItemAsset.analysis_last_dispatched_at <= queued_cutoff,
                                and_(
                                    PlanItemAsset.analysis_last_dispatched_at.is_(None),
                                    PlanItemAsset.created_at <= queued_cutoff,
                                ),
                            ),
                        ),
                        and_(
                            PlanItemAsset.status == "uploaded",
                            PlanItemAsset.created_at <= queued_cutoff,
                        ),
                        and_(
                            PlanItemAsset.status == "analyzing",
                            or_(
                                PlanItemAsset.analysis_started_at <= analyzing_cutoff,
                                and_(
                                    PlanItemAsset.analysis_started_at.is_(None),
                                    PlanItemAsset.created_at <= analyzing_cutoff,
                                ),
                            ),
                        ),
                        # HEIC/HEIF pool analysis shipped without registering
                        # Pillow's decoder, so every valid iPhone photo was
                        # terminalized as unreadable on its first attempt. Give
                        # those rows exactly one post-fix recovery attempt; a
                        # genuinely corrupt HEIF then stops at attempt 2 rather
                        # than looping forever.
                        _heif_decoder_recovery_predicate(),
                    )
                )
                # Current uploads and stale in-flight work stay ahead of the
                # historical repair so rollout recovery cannot delay creators
                # who are uploading now.
                .order_by(
                    _pool_reconcile_priority(),
                    PlanItemAsset.created_at,
                )
                .with_for_update(skip_locked=True)
                .limit(_POOL_RECONCILE_BATCH)
            )
            .scalars()
            .all()
        )
        for asset in rows:
            touched += 1
            if asset.status in {"preparing", "promoting", "cleanup_pending"}:
                # Claim cleanup before releasing the row lock. Registration
                # accepts only preparing/promoting rows, so it cannot write after
                # this transaction commits and before storage deletion.
                generation = getattr(asset, "gcs_generation", None)
                cleanup_targets: list[tuple[str, str | None]] = [(asset.gcs_path, generation)]
                promotion = (
                    (getattr(asset, "analysis", None) or {}).get("_upload_promotion")
                    if isinstance(getattr(asset, "analysis", None), dict)
                    else None
                )
                cleanup_previous_status = (
                    (getattr(asset, "analysis", None) or {}).get("_pool_cleanup_previous_status")
                    if isinstance(getattr(asset, "analysis", None), dict)
                    else None
                )
                if isinstance(promotion, dict):
                    source_path = promotion.get("source_path")
                    source_generation = promotion.get("source_generation")
                    destination_path = promotion.get("destination_path")
                    if isinstance(source_path, str) and source_path:
                        cleanup_targets[0] = (
                            source_path,
                            str(source_generation) if source_generation else None,
                        )
                    if isinstance(destination_path, str) and destination_path:
                        cleanup_targets.append((destination_path, None))
                asset.status = "cleanup_pending"
                expired_reservations.append(
                    (
                        asset.id,
                        asset.plan_item_id,
                        asset.gcs_path,
                        generation,
                        cleanup_targets,
                        str(cleanup_previous_status) if cleanup_previous_status else None,
                    )
                )
                continue
            attempts = int(asset.analysis_attempt_count or 0)
            if attempts >= _POOL_MAX_ATTEMPTS:
                asset.status = "failed"
                asset.error_code = "analysis_timed_out"
                asset.error_detail = "Kria couldn't finish analyzing this file. Try again."
                asset.error_retryable = True
                asset.analysis_started_at = None
                continue
            token = uuid.uuid4().hex
            asset.status = "queued"
            asset.analysis_attempt_token = token
            asset.analysis_attempt_count = attempts + 1
            asset.analysis_last_dispatched_at = current
            asset.analysis_started_at = None
            asset.error_code = None
            asset.error_detail = None
            asset.error_retryable = False
            to_publish.append((str(asset.id), token, getattr(asset, "correlation_id", None)))
        db.commit()

    expired_cleaned = 0
    if expired_reservations:
        from app.services.pool_asset_refs import (  # noqa: PLC0415
            item_references_pool_path,
            job_references_pool_asset,
        )
        from app.storage import (  # noqa: PLC0415
            delete_object_best_effort,
            delete_object_generation_best_effort,
        )

        for (
            asset_id,
            plan_item_id,
            path,
            generation,
            cleanup_targets,
            previous_status,
        ) in expired_reservations:
            with sync_session() as db:
                item = (
                    db.get(PlanItem, plan_item_id, with_for_update=True)
                    if previous_status
                    else None
                )
                asset = db.get(PlanItemAsset, asset_id, with_for_update=True)
                if not (
                    asset is not None
                    and asset.status == "cleanup_pending"
                    and asset.gcs_path == path
                    and str(getattr(asset, "gcs_generation", None) or "") == str(generation or "")
                ):
                    continue
                if previous_status:
                    job = (
                        db.get(Job, item.current_job_id, with_for_update=True)
                        if item is not None and item.current_job_id is not None
                        else None
                    )
                    if item is not None and (
                        item_references_pool_path(item, asset.gcs_path)
                        or (
                            job is not None
                            and job.status != "cancelled"
                            and job_references_pool_asset(
                                job,
                                asset_id=str(asset.id),
                                gcs_path=asset.gcs_path,
                            )
                        )
                    ):
                        restored = dict(asset.analysis) if isinstance(asset.analysis, dict) else {}
                        restored.pop("_pool_cleanup_previous_status", None)
                        asset.analysis = restored or None
                        asset.status = previous_status
                        db.commit()
                        continue
                cleaned = True
                for cleanup_path, cleanup_generation in dict.fromkeys(cleanup_targets):
                    target_cleaned = (
                        delete_object_generation_best_effort(
                            cleanup_path, generation=str(cleanup_generation)
                        )
                        if cleanup_generation
                        else delete_object_best_effort(cleanup_path)
                    )
                    cleaned = target_cleaned and cleaned
                if not cleaned:
                    log.warning("pool_asset_reservation_cleanup_deferred", asset_id=str(asset_id))
                    continue
                if asset.gcs_path != path or str(
                    getattr(asset, "gcs_generation", None) or ""
                ) != str(generation or ""):
                    continue
                db.delete(asset)
                db.commit()
                expired_cleaned += 1

    for asset_id, token, correlation_id in to_publish:
        try:
            task_headers = {"pool_asset_attempt_token": token}
            if correlation_id:
                task_headers["x-correlation-id"] = correlation_id
            analyze_pool_asset.apply_async(
                args=[asset_id, False],
                queue=settings.pool_asset_analysis_queue,
                headers=task_headers,
            )
        except Exception as exc:  # noqa: BLE001
            with sync_session() as db:
                asset = db.get(PlanItemAsset, uuid.UUID(asset_id), with_for_update=True)
                if asset is not None and asset.analysis_attempt_token == token:
                    asset.status = "failed"
                    asset.error_code = "analysis_temporarily_unavailable"
                    asset.error_detail = "Kria couldn't restart analyzing this file. Try again."
                    asset.error_retryable = True
                    db.commit()
            log.warning(
                "pool_asset_requeue_failed",
                asset_id=asset_id,
                error_type=type(exc).__name__,
            )
    if touched:
        log.info(
            "pool_asset_reconciled",
            count=touched,
            republished=len(to_publish),
            expired_reservations=expired_cleaned,
        )

    from app.tasks.autoplace import generate_pool_asset_preview  # noqa: PLC0415

    with sync_session() as db:
        preview_backfill_ids = (
            db.execute(
                select(PlanItemAsset.id)
                .where(_preview_backfill_predicate())
                .order_by(PlanItemAsset.created_at.desc())
                .limit(_POOL_PREVIEW_BACKFILL_BATCH)
            )
            .scalars()
            .all()
        )
    for asset_id in preview_backfill_ids:
        try:
            generate_pool_asset_preview.apply_async(
                args=[str(asset_id)],
                queue=settings.pool_asset_analysis_queue,
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort backfill: dispatch failure just means this row waits
            # for the next reconcile pass. Never fail the whole sweep over it.
            log.warning(
                "pool_asset_preview_backfill_dispatch_failed",
                asset_id=str(asset_id),
                error_type=type(exc).__name__,
            )
    if preview_backfill_ids:
        log.info("pool_asset_preview_backfill_dispatched", count=len(preview_backfill_ids))

    return touched


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

    Computes the live-job-id set ONCE and shares it with both
    `reap_orphans` and `reconcile_stuck_variants` — each of those calls
    issues 3 separate broker broadcasts internally (active/reserved/ping),
    so calling them independently cost 6 broadcasts per sweep (~30s
    observed in prod at a 5s timeout each). One shared inspect() call
    cuts that to 3.
    """
    try:
        try:
            reconcile_stale_pool_assets()
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile_stale_pool_assets_failed", error_type=type(exc).__name__)
        live = _live_job_ids(celery_app)
        if live is None:
            # inspect() failed — both functions would no-op anyway; skip
            # the DB work and log once instead of twice.
            log.warning("sweep_stale_jobs_inspect_unavailable")
            return 0

        count = reap_orphans(celery_app, live=live)
        # Also reconcile variants frozen on already-terminal jobs (dead
        # single-variant re-renders) — reap_orphans only covers jobs whose
        # JOB-level status is still non-terminal. Independent try so one
        # failing doesn't skip the other.
        try:
            reconcile_stuck_variants(celery_app, live=live)
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


# ---------------------------------------------------------------------------
# Render-worker autostop: stop-when-idle + start-backstop (Fly cost-cut plan)
#
# Runs on the `light` process (see task_routes in app/worker.py — this task
# is itself listed in MAINTENANCE_TASK_NAMES, since it obviously must not run
# ON the machine it's managing). Gated entirely by RENDER_AUTOSTOP_ENABLED —
# a complete no-op when off, matching the kill-switch convention used
# throughout this codebase.
#
# Two responsibilities in one task, not two separate ones, because they
# share the same render_worker_idle() read and the same idle-duration
# tracking state — splitting them would mean computing idle state twice per
# tick for no benefit.
#   1. STOP: the machine has been continuously idle for
#      RENDER_IDLE_GRACE_MIN minutes → ask Fly to stop it.
#   2. START (backstop): there's real render-queue work AND the machine
#      isn't already started → ask Fly to start it. This is what bounds a
#      missed/failed before_task_publish wake-hook call (worker.py) to at
#      most one lifecycle poll interval, instead of an indefinite hang.
# ---------------------------------------------------------------------------

_RENDER_WORKER_IDLE_SINCE_KEY = "render_worker:idle_since"

_lifecycle_redis_lock = threading.Lock()
_lifecycle_redis_client = None


def _get_lifecycle_redis():
    """Module-singleton Redis client for idle-duration tracking.

    Matches the per-module pooled-client pattern already used throughout
    this codebase (clip_cache.py, worker.py's wake-hook debounce, etc.) —
    there is no shared "app redis client" utility to reuse instead. Returns
    None on connection failure; callers degrade to "can't track duration,
    treat every idle tick as the start of a fresh grace period" rather than
    raising — see `_decide_lifecycle_action`.
    """
    global _lifecycle_redis_client
    if _lifecycle_redis_client is not None:
        return _lifecycle_redis_client
    with _lifecycle_redis_lock:
        if _lifecycle_redis_client is not None:
            return _lifecycle_redis_client
        try:
            import redis as redis_lib  # noqa: PLC0415

            from app.config import settings  # noqa: PLC0415

            client = redis_lib.from_url(
                settings.redis_url, socket_connect_timeout=2, socket_timeout=2
            )
            client.ping()
            _lifecycle_redis_client = client
        except Exception as exc:  # noqa: BLE001
            log.warning("render_worker_lifecycle_redis_unavailable", error=str(exc))
            return None
    return _lifecycle_redis_client


def _decide_lifecycle_action(
    idle: bool | None,
    idle_since: float | None,
    now: float,
    grace_min: int,
) -> tuple[str, float | None]:
    """Pure decision logic — no Celery/Redis/Fly I/O, fully unit-testable.

    Returns (action, new_idle_since_to_persist):
      "unknown"  — render_worker_idle() couldn't determine state (broker
                   hiccup). Do nothing at all — never act on missing
                   information, same principle as the reaper.
      "not_idle" — there's active/queued render work. Caller should ensure
                   the machine is started (the backstop). idle_since is
                   cleared (None) so a future idle period starts a fresh
                   grace timer, not a stale one.
      "grace"    — idle, but hasn't been continuously idle for grace_min
                   minutes yet. Caller does nothing but persist idle_since
                   so the NEXT tick knows when this idle period started.
      "stop"     — idle for >= grace_min minutes. Caller should stop the
                   machine. idle_since is cleared so we don't ask Fly to
                   stop an already-stopped machine on every subsequent tick
                   (harmless since Fly's stop is idempotent, but noisy).
    """
    if idle is None:
        return "unknown", idle_since
    if not idle:
        return "not_idle", None
    since = idle_since if idle_since is not None else now
    if (now - since) >= grace_min * 60:
        return "stop", None
    return "grace", since


@celery_app.task(
    name="tasks.manage_render_worker_lifecycle",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=30,
    time_limit=45,
)
def manage_render_worker_lifecycle(self) -> str:
    """Stop the render worker when idle past the grace period; start it
    back up (backstop) if there's work waiting and it isn't already
    running. Returns the action taken, for observability in the task result
    backend / admin job-debug view.
    """
    from app.config import settings  # noqa: PLC0415
    from app.services.fly_machines import (  # noqa: PLC0415
        get_render_worker_state,
        start_render_worker,
        stop_render_worker,
    )
    from app.services.queue_state import render_worker_idle  # noqa: PLC0415

    if not settings.RENDER_AUTOSTOP_ENABLED:
        return "disabled"

    idle = render_worker_idle(celery_app)
    now = datetime.now(UTC).timestamp()

    redis_client = _get_lifecycle_redis()
    idle_since: float | None = None
    if redis_client is not None:
        try:
            raw = redis_client.get(_RENDER_WORKER_IDLE_SINCE_KEY)
            idle_since = float(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            log.warning("render_worker_lifecycle_redis_read_failed", error=str(exc))

    action, new_idle_since = _decide_lifecycle_action(
        idle, idle_since, now, settings.RENDER_IDLE_GRACE_MIN
    )

    if redis_client is not None:
        try:
            if new_idle_since is None:
                redis_client.delete(_RENDER_WORKER_IDLE_SINCE_KEY)
            else:
                redis_client.set(_RENDER_WORKER_IDLE_SINCE_KEY, str(new_idle_since))
        except Exception as exc:  # noqa: BLE001
            log.warning("render_worker_lifecycle_redis_write_failed", error=str(exc))

    if action == "stop":
        ok = stop_render_worker()
        log.info("render_worker_lifecycle_stop", ok=ok)
    elif action == "not_idle":
        # Backstop: confirm the machine is actually started every tick,
        # regardless of whether the wake hook already handled it — this is
        # the mechanism that bounds a missed/failed wake to one poll
        # interval instead of an indefinite hang.
        state = get_render_worker_state()
        if state != "started":
            ok = start_render_worker()
            log.info("render_worker_lifecycle_backstop_start", ok=ok, prior_state=state)

    return action
