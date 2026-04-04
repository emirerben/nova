"""Template job endpoints.

POST /template-jobs              — create a template-mode job
GET  /template-jobs              — list template jobs (QA dashboard)
GET  /template-jobs/:id/status   — poll job status + result
POST /template-jobs/:id/reroll   — re-run assembly with same clips
GET  /template-jobs/:id/debug    — admin debug endpoint
"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Job, VideoTemplate
from app.services.template_validation import (
    get_template_or_404,
    require_ready,
    validate_clip_count,
)

log = structlog.get_logger()

router = APIRouter()

# Synthetic user for MVP (Phase 2 adds auth)
SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# ── Schemas ────────────────────────────────────────────────────────────────────


class CreateTemplateJobRequest(BaseModel):
    template_id: str
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]
    # e.g. "Puerto Rico" — replaces template placeholder text
    subject: str = Field(default="", max_length=50)

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clip_count(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("At least 1 clip is required")
        if len(v) > 20:
            raise ValueError("Maximum 20 clips allowed")
        return v

    @field_validator("selected_platforms")
    @classmethod
    def validate_platforms(cls, v: list[str]) -> list[str]:
        valid = {"tiktok", "instagram", "youtube"}
        for p in v:
            if p not in valid:
                raise ValueError(f"Unknown platform: {p}")
        return v


class TemplateJobResponse(BaseModel):
    job_id: str
    status: str
    template_id: str


class TemplateJobStatusResponse(BaseModel):
    job_id: str
    status: str
    template_id: str | None
    assembly_plan: dict | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime


class TemplateJobListItem(BaseModel):
    job_id: str
    status: str
    template_id: str | None
    created_at: datetime
    updated_at: datetime


class TemplateJobListResponse(BaseModel):
    jobs: list[TemplateJobListItem]
    total: int


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("", response_model=TemplateJobResponse, status_code=status.HTTP_201_CREATED)
async def create_template_job(
    req: CreateTemplateJobRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateJobResponse:
    """Create a template-mode job. Validates template existence and clip count."""
    template = await get_template_or_404(req.template_id, db)
    require_ready(template)
    validate_clip_count(template, len(req.clip_gcs_paths))

    job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="template",
        template_id=req.template_id,
        raw_storage_path=req.clip_gcs_paths[0],
        selected_platforms=req.selected_platforms,
        all_candidates={"clip_paths": req.clip_gcs_paths, "subject": req.subject},
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    job_id = str(job.id)

    from app.tasks.template_orchestrate import orchestrate_template_job  # noqa: PLC0415
    orchestrate_template_job.delay(job_id)

    log.info(
        "template_job_created",
        job_id=job_id,
        template_id=req.template_id,
        clips=len(req.clip_gcs_paths),
        subject=req.subject,
    )
    return TemplateJobResponse(job_id=job_id, status="queued", template_id=req.template_id)


@router.get("", response_model=TemplateJobListResponse)
async def list_template_jobs(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> TemplateJobListResponse:
    """List template jobs ordered by created_at DESC. Scoped to synthetic user.

    Used by the QA Dashboard for internal review of all template job outputs.
    """
    base_query = (
        select(Job)
        .where(Job.user_id == SYNTHETIC_USER_ID)
        .where(Job.job_type == "template")
    )

    # Count total
    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar() or 0

    # Fetch page
    result = await db.execute(
        base_query.order_by(Job.created_at.desc()).offset(offset).limit(limit)
    )
    jobs = result.scalars().all()

    return TemplateJobListResponse(
        jobs=[
            TemplateJobListItem(
                job_id=str(j.id),
                status=j.status,
                template_id=j.template_id,
                created_at=j.created_at,
                updated_at=j.updated_at,
            )
            for j in jobs
        ],
        total=total,
    )


@router.post(
    "/{job_id}/reroll",
    response_model=TemplateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def reroll_template_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateJobResponse:
    """Re-run template assembly with the same clips. Creates a new job.

    Guard: original job must be in 'template_ready' status.
    The matcher naturally produces different results on re-run due to
    ThreadPoolExecutor ordering + moment tiebreakers.
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = await db.execute(select(Job).where(Job.id == job_uuid))
    original = result.scalar_one_or_none()

    if original is None or original.job_type != "template":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if original.status != "template_ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Can only re-roll completed jobs (current status: {original.status})",
        )

    # Extract clip paths from original job
    clip_paths = (original.all_candidates or {}).get("clip_paths", [])
    if not clip_paths:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Original job has no clip paths to re-roll",
        )

    # Create new job with same clips and template
    new_job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="template",
        template_id=original.template_id,
        raw_storage_path=clip_paths[0],
        selected_platforms=original.selected_platforms or ["tiktok", "instagram", "youtube"],
        all_candidates={
            "clip_paths": clip_paths,
            "subject": (original.all_candidates or {}).get("subject", ""),
        },
        status="queued",
    )
    db.add(new_job)
    await db.commit()
    await db.refresh(new_job)

    new_job_id = str(new_job.id)

    from app.tasks.template_orchestrate import orchestrate_template_job  # noqa: PLC0415
    orchestrate_template_job.delay(new_job_id)

    log.info("template_job_rerolled", new_job_id=new_job_id, original_job_id=job_id)
    return TemplateJobResponse(
        job_id=new_job_id,
        status="queued",
        template_id=original.template_id or "",
    )


@router.get("/{job_id}/debug")
async def get_template_job_debug(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin debug endpoint — returns full template recipe, assembly plan, and clip diagnostics."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.job_type != "template":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    template_recipe = None
    if job.template_id:
        tpl_result = await db.execute(
            select(VideoTemplate).where(VideoTemplate.id == job.template_id)
        )
        tpl = tpl_result.scalar_one_or_none()
        if tpl:
            template_recipe = tpl.recipe_cached

    assembly_plan = job.assembly_plan or {}
    steps = assembly_plan.get("steps", [])
    clip_ids_used = [s.get("clip_id") for s in steps]
    clips_used_unique = len(set(clip_ids_used))

    return {
        "job_id": job_id,
        "status": job.status,
        "error_detail": job.error_detail,
        "template_recipe": template_recipe,
        "assembly_plan": {
            "steps": steps,
            "clips_used_unique": clips_used_unique,
            "total_slots": len(steps),
            "clip_ids_in_order": clip_ids_used,
        },
    }


@router.get("/{job_id}/eval")
async def get_template_job_eval(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Visual evaluation harness — per-slot comparison data for QA.

    Returns per-slot video URLs (when EVAL_HARNESS_ENABLED) alongside template
    reference timestamps for side-by-side visual comparison.
    """
    from app.config import settings as app_settings  # noqa: PLC0415

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.job_type != "template":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    assembly_plan = job.assembly_plan or {}
    steps = assembly_plan.get("steps", [])

    # Build per-slot eval data
    slots_eval = []
    cumulative_s = 0.0
    for i, step in enumerate(steps):
        slot = step.get("slot", {})
        dur = float(slot.get("target_duration_s", 5.0))
        slot_url = None
        if app_settings.eval_harness_enabled:
            slot_url = assembly_plan.get("slot_urls", {}).get(str(i))

        slots_eval.append({
            "position": slot.get("position", i + 1),
            "slot_url": slot_url,
            "template_start_s": round(cumulative_s, 3),
            "template_end_s": round(cumulative_s + dur, 3),
            "transition_in": slot.get("transition_in", "none"),
            "speed_factor": slot.get("speed_factor", 1.0),
            "text_overlays": [
                {"role": ov.get("role"), "effect": ov.get("effect")}
                for ov in slot.get("text_overlays", [])
            ],
        })
        cumulative_s += dur

    # Template URL
    template_url = None
    if job.template_id:
        tpl_result = await db.execute(
            select(VideoTemplate).where(VideoTemplate.id == job.template_id)
        )
        tpl = tpl_result.scalar_one_or_none()
        if tpl:
            template_url = tpl.gcs_path

    return {
        "job_id": job_id,
        "slots": slots_eval,
        "template_url": template_url,
        "output_url": assembly_plan.get("output_url"),
        "comparison_grid_url": assembly_plan.get("comparison_grid_url"),
    }


@router.get("/{job_id}/status", response_model=TemplateJobStatusResponse)
async def get_template_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateJobStatusResponse:
    """Poll template job status."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.job_type != "template":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return TemplateJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        template_id=job.template_id,
        assembly_plan=job.assembly_plan,
        error_detail=job.error_detail,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
