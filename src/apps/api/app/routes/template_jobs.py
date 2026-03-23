"""Template job endpoints.

POST /template-jobs       — create a template-mode job
GET  /template-jobs/:id/status — poll job status + result
"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Job, VideoTemplate

log = structlog.get_logger()

router = APIRouter()

# ── Schemas ────────────────────────────────────────────────────────────────────


class CreateTemplateJobRequest(BaseModel):
    template_id: str
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clip_count(cls, v: list[str]) -> list[str]:
        # Hard limits — template-level min/max checked after DB lookup
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


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("", response_model=TemplateJobResponse, status_code=status.HTTP_201_CREATED)
async def create_template_job(
    req: CreateTemplateJobRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateJobResponse:
    """Create a template-mode job. Validates template existence and clip count."""
    # Look up template
    result = await db.execute(
        select(VideoTemplate).where(VideoTemplate.id == req.template_id)
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    if template.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Template is still being analyzed (status: {template.analysis_status}). "
                   "Try again in a few seconds.",
        )

    # Validate clip count against template requirements
    n_clips = len(req.clip_gcs_paths)
    if n_clips < template.required_clips_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Template requires at least {template.required_clips_min} clips, "
                   f"got {n_clips}.",
        )
    if n_clips > template.required_clips_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Template allows at most {template.required_clips_max} clips, "
                   f"got {n_clips}.",
        )

    # Use a synthetic user_id for now (Phase 2 adds auth)
    synthetic_user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    job = Job(
        user_id=synthetic_user_id,
        job_type="template",
        template_id=req.template_id,
        # raw_storage_path stores the first clip path for schema compat; full list in all_candidates
        raw_storage_path=req.clip_gcs_paths[0],
        selected_platforms=req.selected_platforms,
        all_candidates={"clip_paths": req.clip_gcs_paths},
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    job_id = str(job.id)

    # Enqueue Celery task
    from app.tasks.template_orchestrate import orchestrate_template_job  # noqa: PLC0415
    orchestrate_template_job.delay(job_id)

    log.info("template_job_created", job_id=job_id, template_id=req.template_id, clips=n_clips)
    return TemplateJobResponse(job_id=job_id, status="queued", template_id=req.template_id)


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

    # Load template recipe
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
