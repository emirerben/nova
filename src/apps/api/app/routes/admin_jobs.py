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
from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._runtime import SUCCESS_OUTCOMES
from app.database import get_db
from app.models import AgentRun, Job, JobClip, MusicTrack, VideoTemplate
from app.routes._admin_schemas import AgentRunPayload, agent_run_to_payload
from app.routes.admin import _require_admin

log = structlog.get_logger()

router = APIRouter()


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
    analysis_status: str
    audio_gcs_path: str | None
    track_config: Any
    recipe_cached: Any
    beat_timestamps_s: Any
    ai_labels: Any
    best_sections: Any
    error_detail: str | None


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
    template_agent_runs: list[AgentRunPayload]
    track_agent_runs: list[AgentRunPayload]


# ── Endpoints ────────────────────────────────────────────────────────────────


JobTypeFilter = Literal["all", "music", "template", "auto_music", "default"]


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
    base = select(Job)
    if job_type != "all":
        base = base.where(Job.job_type == job_type)
    if status_filter:
        base = base.where(Job.status == status_filter)
    if only_failures:
        base = base.where(Job.status.like("%_failed"))

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
            agent_run_count=counts.get(j.id, (0, 0))[0],
            failure_count=counts.get(j.id, (0, 0))[1],
        )
        for j in jobs
    ]
    return AdminJobListResponse(items=items, total=total, limit=limit, offset=offset)


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
                analysis_status=mt.analysis_status,
                audio_gcs_path=mt.audio_gcs_path,
                track_config=mt.track_config,
                recipe_cached=mt.recipe_cached,
                beat_timestamps_s=mt.beat_timestamps_s,
                ai_labels=mt.ai_labels,
                best_sections=mt.best_sections,
                error_detail=mt.error_detail,
            )

    runs_res = await db.execute(
        select(AgentRun).where(AgentRun.job_id == job_uuid).order_by(AgentRun.created_at)
    )
    runs = list(runs_res.scalars().all())

    template_runs: list[AgentRun] = []
    if job.template_id:
        tpl_runs_res = await db.execute(
            select(AgentRun)
            .where(AgentRun.template_id == job.template_id)
            .order_by(AgentRun.created_at)
        )
        template_runs = list(tpl_runs_res.scalars().all())

    track_runs: list[AgentRun] = []
    if job.music_track_id:
        track_runs_res = await db.execute(
            select(AgentRun)
            .where(AgentRun.music_track_id == job.music_track_id)
            .order_by(AgentRun.created_at)
        )
        track_runs = list(track_runs_res.scalars().all())

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
    )

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
        template_agent_runs=[agent_run_to_payload(r) for r in template_runs],
        track_agent_runs=[agent_run_to_payload(r) for r in track_runs],
    )
