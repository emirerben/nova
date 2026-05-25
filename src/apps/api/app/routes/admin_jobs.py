"""Admin endpoints for the job-debug view.

GET /admin/jobs                        — paginated list of jobs (music + template + auto-music)
GET /admin/jobs/{job_id}/debug         — full debug payload for one job

Lets admins answer "why is this video bad?" without reading logs or
re-running a job: surfaces every agent's full input + raw LLM response +
parsed output (from ``agent_run``), every non-LLM pipeline decision
(from ``Job.pipeline_trace``), and all the JSONB columns already
populated on Job / JobClip / VideoTemplate / MusicTrack.

Auth: X-Admin-Token header (same gate as the rest of admin.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.agents._runtime import SUCCESS_OUTCOMES
from app.database import get_db
from app.models import AgentRun, Job, JobClip, MusicTrack, VideoTemplate
from app.routes._admin_schemas import (
    AgentRunPayload,
    AgentRunSummaryPayload,
    agent_run_to_payload,
    agent_run_to_payload_summary,
)
from app.routes.admin import _require_admin
from app.services.pipeline_trace import pipeline_trace_for, record_pipeline_event
from app.services.queue_state import (
    get_job_runtime_state,
    get_queue_position,
    get_queue_snapshot,
)

log = structlog.get_logger()

router = APIRouter()

# Status values eligible for cancellation. Anything outside this set is
# either already terminal (done, *_failed, *_ready, cancelled) or a
# status we don't expect to ever see on a live row (importing happens
# pre-queue). Keep this list intentional — broader is dangerous.
#
# Mirror of CANCELLABLE_STATUSES in
# src/apps/web/src/app/admin/jobs/[id]/page.tsx. Update both when
# adding or removing a status. The frontend uses this to decide
# whether to show the Cancel button; if the two lists drift, the
# button can show for a status the backend rejects with 409 (or
# hide for a status it would accept).
_CANCELLABLE_STATUSES = (
    "queued",
    "processing",
    "matching",
    "rendering",
    "posting",
)

# Paired with src/apps/web/src/lib/admin-jobs-api.ts. Bump both if changed.
CONTEXT_RUNS_CAP = 200


# ── Response schemas ─────────────────────────────────────────────────────────


class AdminJobListItem(BaseModel):
    job_id: str
    job_type: str
    mode: str | None
    status: str
    template_id: str | None
    music_track_id: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    # Pipeline wall-clock start (set when the orchestrator picks up the
    # task). Distinct from created_at (queue insert). Drives the
    # Running-for column on the admin jobs list.
    started_at: datetime | None
    # Seconds since started_at — computed server-side so the UI doesn't
    # have to handle clock skew. Null for queued / unstarted rows.
    time_in_processing_s: float | None
    celery_task_id: str | None
    agent_run_count: int
    failure_count: int


class AdminJobListResponse(BaseModel):
    items: list[AdminJobListItem]
    total: int
    limit: int
    offset: int


class JobClipPayload(BaseModel):
    id: str
    rank: int
    hook_score: float
    engagement_score: float
    combined_score: float
    start_s: float
    end_s: float
    hook_text: str | None
    platform_copy: Any
    copy_status: str
    video_path: str | None
    render_status: str
    error_detail: str | None
    music_track_id: str | None
    match_score: float | None
    match_rationale: str | None


class JobPayload(BaseModel):
    id: str
    user_id: str
    status: str
    job_type: str
    mode: str | None
    template_id: str | None
    music_track_id: str | None
    failure_reason: str | None
    error_detail: str | None
    current_phase: str | None
    phase_log: Any
    raw_storage_path: str | None
    selected_platforms: list[str] | None
    probe_metadata: Any
    transcript: Any
    scene_cuts: Any
    all_candidates: Any
    assembly_plan: Any
    pipeline_trace: Any
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    celery_task_id: str | None


class TemplateSummary(BaseModel):
    id: str
    name: str
    analysis_status: str
    recipe_cached: Any
    audio_gcs_path: str | None
    error_detail: str | None


class MusicTrackSummary(BaseModel):
    id: str
    title: str
    artist: str
    recipe_cached: Any


class JobRuntimePayload(BaseModel):
    """Live worker/queue state for one job, derived from Celery inspect()."""

    state: Literal["active", "reserved", "not_found", "unknown"]
    worker: str | None
    task_id: str | None
    # Queue position when state == "reserved". 0 = next up. None when not
    # in the broker queue or when the scan cap was exceeded.
    queue_position: int | None


class JobDebugResponse(BaseModel):
    job: JobPayload
    job_clips: list[JobClipPayload]
    template: TemplateSummary | None
    music_track: MusicTrackSummary | None
    agent_runs: list[AgentRunPayload]
    # Template/track-level analysis runs that shaped the template's recipe
    # but ran outside this job's lifecycle. Empty arrays when the job has
    # no linked template/track or when those entities were analyzed before
    # the agent_run.template_id / music_track_id columns existed.
    template_agent_runs: list[AgentRunSummaryPayload]
    track_agent_runs: list[AgentRunSummaryPayload]
    template_agent_runs_has_more: bool
    track_agent_runs_has_more: bool
    context_runs_cap: int
    runtime: JobRuntimePayload


class CancelJobResponse(BaseModel):
    job_id: str
    previous_status: str
    status: str
    task_id: str | None
    # True when we successfully published the revoke message to the
    # broker. NOT a guarantee any worker received or honored it —
    # `celery_app.control.revoke()` is a fire-and-forget broadcast that
    # returns as soon as the message is published, with no per-worker
    # ack. The real cancellation mechanism is the conditional status
    # flip below. `revoke_dispatched` is here for the audit trail and
    # to tell the operator we did try.
    revoke_dispatched: bool


class QueueInfoPayload(BaseModel):
    name: str
    depth: int
    oldest_pending_job_id: str | None


class QueueSnapshotResponse(BaseModel):
    queues: list[QueueInfoPayload]
    active_workers: list[str]
    ok: bool


# ── Endpoints ────────────────────────────────────────────────────────────────


JobTypeFilter = Literal["all", "music", "template", "auto_music", "generative", "default"]


@router.get("", response_model=AdminJobListResponse)
async def list_jobs(
    job_type: JobTypeFilter = Query("all"),
    status_filter: str | None = Query(None, alias="status"),
    only_failures: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> AdminJobListResponse:
    """List jobs with optional filters and agent-run counts.

    Counts join the agent_run table via a subquery rather than a JOIN to
    avoid row-multiplication on the main listing.
    """
    # Defer the heavy JSONB columns — the list response only surfaces metadata
    # (id, status, type, fk ids, timestamps, celery_task_id). Loading
    # assembly_plan / pipeline_trace / transcript / etc. per row turned the
    # list into a multi-megabyte payload. The detail endpoint
    # (/admin/jobs/{id}/debug) builds its own query and is unaffected.
    base = select(Job).options(
        defer(Job.assembly_plan),
        defer(Job.probe_metadata),
        defer(Job.transcript),
        defer(Job.scene_cuts),
        defer(Job.all_candidates),
        defer(Job.phase_log),
        defer(Job.pipeline_trace),
    )
    if job_type != "all":
        base = base.where(Job.job_type == job_type)
    if status_filter:
        base = base.where(Job.status == status_filter)
    if only_failures:
        base = base.where(Job.status.like("%_failed"))

    # The count query reuses the WHERE filters but not the column loads —
    # COUNT(*) doesn't materialize the deferred columns either way.
    total_res = await db.execute(select(func.count()).select_from(base.subquery()))
    total = int(total_res.scalar() or 0)

    rows_res = await db.execute(base.order_by(Job.created_at.desc()).limit(limit).offset(offset))
    jobs = list(rows_res.scalars().all())

    job_ids = [j.id for j in jobs]
    counts: dict[uuid.UUID, tuple[int, int]] = {}
    if job_ids:
        counts_q = text(
            """
            SELECT job_id,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (
                     WHERE outcome <> ALL(:success_outcomes)
                   ) AS failures
              FROM agent_run
             WHERE job_id = ANY(:ids)
          GROUP BY job_id
            """
        )
        counts_res = await db.execute(
            counts_q,
            {"ids": job_ids, "success_outcomes": list(SUCCESS_OUTCOMES)},
        )
        for row in counts_res.fetchall():
            counts[row.job_id] = (int(row.total), int(row.failures))

    now = datetime.now(UTC)
    items = [
        AdminJobListItem(
            job_id=str(j.id),
            job_type=j.job_type,
            mode=j.mode,
            status=j.status,
            template_id=j.template_id,
            music_track_id=j.music_track_id,
            failure_reason=j.failure_reason,
            created_at=j.created_at,
            updated_at=j.updated_at,
            started_at=j.started_at,
            time_in_processing_s=_time_in_processing_s(j, now),
            celery_task_id=j.celery_task_id,
            agent_run_count=counts.get(j.id, (0, 0))[0],
            failure_count=counts.get(j.id, (0, 0))[1],
        )
        for j in jobs
    ]
    return AdminJobListResponse(items=items, total=total, limit=limit, offset=offset)


def _time_in_processing_s(job: Job, now: datetime) -> float | None:
    """Seconds since started_at when the job is still moving.

    Returns None when:
      - started_at is unset (still queued, or legacy row)
      - status is terminal (anything ending in _ready, _failed, done, cancelled)
    The list UI uses this to color-amber/red rows that are stuck.
    """
    if job.started_at is None:
        return None
    if job.status in {"cancelled", "done"} or job.status.endswith(("_ready", "_failed")):
        return None
    started = job.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - started).total_seconds()


# ── un-reap ──────────────────────────────────────────────────────────────────


class UnReapResponse(BaseModel):
    restored: int
    ids: list[str]


@router.post(
    "/un-reap",
    response_model=UnReapResponse,
    dependencies=[Depends(_require_admin)],
)
async def un_reap_falsely_failed_jobs(
    db: AsyncSession = Depends(get_db),
) -> UnReapResponse:
    """Restore jobs that the pre-#243 reaper falsely flipped to processing_failed.

    Matches the exact reaper UPDATE fingerprint at
    `src/apps/api/app/tasks/reaper.py:120-127` (status=processing_failed +
    failure_reason=unknown + error_detail beginning with "Worker died with no
    recovery" + assembly_plan carries an output_url proving the run actually
    succeeded). For template jobs the status is restored to template_ready;
    for music jobs to music_ready.

    Idempotent: re-running returns {"restored": 0, "ids": []} because the
    restored rows no longer match the fingerprint.
    """
    from sqlalchemy import case, update  # noqa: PLC0415

    # Use a JSONB existence check (assembly_plan ? 'output_url') so we only
    # restore rows that actually have a usable output. SQLAlchemy renders this
    # via the .has_key() / op("?") accessor.
    stmt = (
        update(Job)
        .where(
            Job.status == "processing_failed",
            Job.failure_reason == "unknown",
            Job.error_detail.like("Worker died with no recovery%"),
            Job.assembly_plan.op("?")("output_url"),
        )
        .values(
            status=case(
                (Job.job_type == "template", "template_ready"),
                (Job.job_type == "music", "music_ready"),
                else_=Job.status,
            ),
            failure_reason=None,
            error_detail=None,
        )
        .returning(Job.id)
    )
    result = await db.execute(stmt)
    restored_ids = [str(row[0]) for row in result.fetchall()]
    await db.commit()

    log.info("admin_un_reap", restored=len(restored_ids))
    return UnReapResponse(restored=len(restored_ids), ids=restored_ids)


@router.get("/{job_id}/debug", response_model=JobDebugResponse)
async def get_job_debug(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> JobDebugResponse:
    """Return the full debug payload for one job: every JSONB column on
    the Job row, the JobClip rows, the linked template/music_track
    summary, and every agent_run row ordered chronologically.
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid job_id: {exc}",
        ) from exc

    # The debug UI currently renders every Job JSONB field below:
    # phase_log, pipeline_trace, assembly_plan, probe_metadata, transcript,
    # scene_cuts, and all_candidates. Keep the row fully loaded here.
    job_res = await db.execute(select(Job).where(Job.id == job_uuid))
    job = job_res.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    clips_res = await db.execute(
        select(JobClip).where(JobClip.job_id == job_uuid).order_by(JobClip.rank)
    )
    clips = list(clips_res.scalars().all())

    template: TemplateSummary | None = None
    if job.template_id:
        tpl_res = await db.execute(select(VideoTemplate).where(VideoTemplate.id == job.template_id))
        tpl = tpl_res.scalar_one_or_none()
        if tpl is not None:
            template = TemplateSummary(
                id=tpl.id,
                name=tpl.name,
                analysis_status=tpl.analysis_status,
                recipe_cached=tpl.recipe_cached,
                audio_gcs_path=tpl.audio_gcs_path,
                error_detail=tpl.error_detail,
            )

    music: MusicTrackSummary | None = None
    if job.music_track_id:
        mt_res = await db.execute(select(MusicTrack).where(MusicTrack.id == job.music_track_id))
        mt = mt_res.scalar_one_or_none()
        if mt is not None:
            music = MusicTrackSummary(
                id=mt.id,
                title=mt.title,
                artist=mt.artist,
                recipe_cached=mt.recipe_cached,
            )

    runs_res = await db.execute(
        select(AgentRun).where(AgentRun.job_id == job_uuid).order_by(AgentRun.created_at)
    )
    runs = list(runs_res.scalars().all())

    template_runs: list[AgentRun] = []
    template_agent_runs_has_more = False
    if job.template_id is not None:
        tpl_runs_res = await db.execute(
            select(AgentRun)
            .options(
                defer(AgentRun.input_json),
                defer(AgentRun.output_json),
                defer(AgentRun.raw_text),
            )
            .where(AgentRun.template_id == job.template_id)
            .order_by(AgentRun.created_at.desc())
            .limit(CONTEXT_RUNS_CAP + 1)
        )
        fetched = list(tpl_runs_res.scalars().all())
        template_agent_runs_has_more = len(fetched) > CONTEXT_RUNS_CAP
        template_runs = fetched[:CONTEXT_RUNS_CAP]

    track_runs: list[AgentRun] = []
    track_agent_runs_has_more = False
    if job.music_track_id is not None:
        track_runs_res = await db.execute(
            select(AgentRun)
            .options(
                defer(AgentRun.input_json),
                defer(AgentRun.output_json),
                defer(AgentRun.raw_text),
            )
            .where(AgentRun.music_track_id == job.music_track_id)
            .order_by(AgentRun.created_at.desc())
            .limit(CONTEXT_RUNS_CAP + 1)
        )
        fetched = list(track_runs_res.scalars().all())
        track_agent_runs_has_more = len(fetched) > CONTEXT_RUNS_CAP
        track_runs = fetched[:CONTEXT_RUNS_CAP]

    job_payload = JobPayload(
        id=str(job.id),
        user_id=str(job.user_id),
        status=job.status,
        job_type=job.job_type,
        mode=job.mode,
        template_id=job.template_id,
        music_track_id=job.music_track_id,
        failure_reason=job.failure_reason,
        error_detail=job.error_detail,
        current_phase=job.current_phase,
        phase_log=job.phase_log,
        raw_storage_path=job.raw_storage_path,
        selected_platforms=job.selected_platforms,
        probe_metadata=job.probe_metadata,
        transcript=job.transcript,
        scene_cuts=job.scene_cuts,
        all_candidates=job.all_candidates,
        assembly_plan=job.assembly_plan,
        pipeline_trace=job.pipeline_trace,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        celery_task_id=job.celery_task_id,
    )

    runtime = _resolve_runtime(job)

    return JobDebugResponse(
        job=job_payload,
        job_clips=[
            JobClipPayload(
                id=str(c.id),
                rank=c.rank,
                hook_score=c.hook_score,
                engagement_score=c.engagement_score,
                combined_score=c.combined_score,
                start_s=c.start_s,
                end_s=c.end_s,
                hook_text=c.hook_text,
                platform_copy=c.platform_copy,
                copy_status=c.copy_status,
                video_path=c.video_path,
                render_status=c.render_status,
                error_detail=c.error_detail,
                music_track_id=c.music_track_id,
                match_score=c.match_score,
                match_rationale=c.match_rationale,
            )
            for c in clips
        ],
        template=template,
        music_track=music,
        agent_runs=[agent_run_to_payload(r) for r in runs],
        template_agent_runs=[agent_run_to_payload_summary(r) for r in template_runs],
        track_agent_runs=[agent_run_to_payload_summary(r) for r in track_runs],
        template_agent_runs_has_more=template_agent_runs_has_more,
        track_agent_runs_has_more=track_agent_runs_has_more,
        context_runs_cap=CONTEXT_RUNS_CAP,
        runtime=runtime,
    )


# ── runtime helpers ──────────────────────────────────────────────────────────


def _resolve_runtime(job: Job) -> JobRuntimePayload:
    """One Celery inspect() call → JobRuntimePayload for the admin detail view.

    Only fills `queue_position` when the worker reports the job as
    reserved (queued and waiting on a worker). For everything else the
    UI doesn't need it and the LRANGE call would be wasted broker work.
    """
    # Import inline so the route module stays importable in environments
    # without a configured Celery broker (e.g. unit tests).
    from app.worker import celery_app  # noqa: PLC0415

    state = get_job_runtime_state(celery_app, job.id, job.celery_task_id)
    queue_position: int | None = None
    if state.state == "reserved":
        queue_position = get_queue_position(celery_app, job.id)

    return JobRuntimePayload(
        state=state.state,
        worker=state.worker,
        task_id=state.task_id,
        queue_position=queue_position,
    )


# ── queue-state endpoint ─────────────────────────────────────────────────────


@router.get("/queue-state", response_model=QueueSnapshotResponse)
async def get_queue_state(
    _: None = Depends(_require_admin),
) -> QueueSnapshotResponse:
    """Broker-level queue depth + active workers.

    Powers the admin queue-summary panel. Single inspect() + LLEN calls;
    safe to poll every 10s. Returns ok=False when the broker is
    unreachable (UI renders "broker unreachable" instead of "0 queued").
    """
    from app.worker import celery_app  # noqa: PLC0415

    snapshot = get_queue_snapshot(celery_app)
    return QueueSnapshotResponse(
        queues=[
            QueueInfoPayload(
                name=q.name,
                depth=q.depth,
                oldest_pending_job_id=q.oldest_pending_job_id,
            )
            for q in snapshot.queues
        ],
        active_workers=snapshot.active_workers,
        ok=snapshot.ok,
    )


# ── cancel endpoint ──────────────────────────────────────────────────────────


@router.post("/{job_id}/cancel", response_model=CancelJobResponse)
async def cancel_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> CancelJobResponse:
    """Cancel a queued or processing job. Revokes the Celery task and flips status.

    Flow:
      1. SELECT FOR UPDATE — 404 if missing, 409 if already terminal.
      2. Revoke the Celery task (terminate=True, SIGTERM). Idempotent —
         revoking an unknown task_id is a no-op. Skipped when
         celery_task_id is NULL (legacy row / dispatch failed).
      3. Conditional UPDATE: only succeed if status is STILL cancellable.
         Wins the race against a worker that finished naturally in the
         microseconds since step 1.
      4. Record a pipeline_trace event for audit.
      5. Enqueue cleanup_cancelled_job as a Celery task (best-effort GCS
         temp delete — 24h lifecycle is the real backstop).
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid job_id: {exc}",
        ) from exc

    job_res = await db.execute(select(Job).where(Job.id == job_uuid).with_for_update())
    job = job_res.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job.status not in _CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Job status is '{job.status}' — only "
                f"{', '.join(_CANCELLABLE_STATUSES)} jobs can be cancelled."
            ),
        )

    previous_status = job.status
    task_id = job.celery_task_id

    from app.worker import celery_app  # noqa: PLC0415

    revoke_dispatched = False
    if task_id:
        try:
            # terminate=True sends the configured signal to the worker
            # process running the task. SIGTERM lets a Python try/except
            # SoftTimeLimitExceeded-style handler run; SIGKILL would
            # drop pending DB writes and FFmpeg subprocesses uncleanly.
            celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
            revoke_dispatched = True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "admin_cancel_revoke_failed",
                job_id=job_id,
                task_id=task_id,
                error=str(exc),
            )

    # Conditional UPDATE: only flip if still cancellable. Returns 0 rows
    # if a worker finalized between our SELECT and this UPDATE, in which
    # case we 409 rather than overwriting a terminal status.
    result = await db.execute(
        update(Job)
        .where(Job.id == job_uuid, Job.status.in_(_CANCELLABLE_STATUSES))
        .values(
            status="cancelled",
            finished_at=datetime.now(UTC),
            failure_reason="cancelled_by_admin",
            error_detail="Cancelled via admin UI",
        )
    )
    if result.rowcount == 0:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job reached a terminal status before cancellation could apply.",
        )
    await db.commit()

    # Pipeline trace event (audit trail). The trace contextvar is
    # per-task in normal pipeline code; we bind it inline here.
    with pipeline_trace_for(job_id):
        record_pipeline_event(
            "cancel",
            "admin_cancel",
            {
                "previous_status": previous_status,
                "task_id": task_id,
                "revoke_dispatched": revoke_dispatched,
            },
        )

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
            "admin_cancel_cleanup_enqueue_failed",
            job_id=job_id,
            error=str(exc),
        )

    log.info(
        "admin_cancel_done",
        job_id=job_id,
        previous_status=previous_status,
        task_id=task_id,
        revoke_dispatched=revoke_dispatched,
    )
    return CancelJobResponse(
        job_id=job_id,
        previous_status=previous_status,
        status="cancelled",
        task_id=task_id,
        revoke_dispatched=revoke_dispatched,
    )
