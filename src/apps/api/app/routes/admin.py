"""Admin endpoints for managing video templates.

POST   /admin/templates                     — register a curated TikTok as a template
GET    /admin/templates                     — list all templates (paginated)
GET    /admin/templates/:id                 — check template analysis status
PATCH  /admin/templates/:id                 — update metadata / publish / archive
POST   /admin/templates/:id/reanalyze       — re-run Gemini analysis
POST   /admin/templates/:id/test-job        — create a test job (SYNTHETIC_USER_ID)
GET    /admin/templates/:id/metrics          — usage stats
GET    /admin/templates/:id/recipe-history   — paginated recipe version list
POST   /admin/upload-presigned               — presigned URL for templates/ prefix

Auth: X-Admin-Token header (static key from settings.admin_api_key).
"""

import hmac
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Job, TemplateRecipeVersion, VideoTemplate
from app.services.template_validation import (
    get_template_or_404,
    require_ready,
    validate_clip_count,
)

log = structlog.get_logger()

router = APIRouter()

# Synthetic user for admin test jobs (same as template_jobs.py)
SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# ── Auth dependency ────────────────────────────────────────────────────────────


def _require_admin(x_admin_token: str = Header(...)) -> None:
    """FastAPI dependency: validates X-Admin-Token header."""
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API not configured",
        )
    if not hmac.compare_digest(x_admin_token, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
        )


# ── Request / Response schemas ─────────────────────────────────────────────────


class CreateTemplateRequest(BaseModel):
    name: str
    gcs_path: str
    required_clips_min: int = 5
    required_clips_max: int = 10
    description: str | None = None
    source_url: str | None = None

    @field_validator("gcs_path")
    @classmethod
    def validate_gcs_path(cls, v: str) -> str:
        if not v.startswith("templates/"):
            raise ValueError("gcs_path must start with 'templates/'")
        return v

    @field_validator("required_clips_min")
    @classmethod
    def validate_min(cls, v: int) -> int:
        if v < 1:
            raise ValueError("required_clips_min must be ≥ 1")
        return v

    @field_validator("required_clips_max")
    @classmethod
    def validate_max(cls, v: int) -> int:
        if v > 30:
            raise ValueError("required_clips_max must be ≤ 30")
        return v


class TemplateResponse(BaseModel):
    id: str
    name: str
    gcs_path: str
    analysis_status: str
    required_clips_min: int
    required_clips_max: int
    published_at: datetime | None
    archived_at: datetime | None
    description: str | None
    source_url: str | None
    thumbnail_gcs_path: str | None
    created_at: datetime


class UpdateTemplateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    source_url: str | None = None
    required_clips_min: int | None = None
    required_clips_max: int | None = None
    publish: bool | None = None   # set True to publish (sets published_at)
    archive: bool | None = None   # set True to archive (sets archived_at)


class TemplateListItem(BaseModel):
    id: str
    name: str
    analysis_status: str
    published_at: datetime | None
    archived_at: datetime | None
    description: str | None
    thumbnail_gcs_path: str | None
    job_count: int
    created_at: datetime


class TemplateListResponse(BaseModel):
    templates: list[TemplateListItem]
    total: int


class TemplateMetricsResponse(BaseModel):
    template_id: str
    total_jobs: int
    successful_jobs: int
    failed_jobs: int
    last_job_at: datetime | None


class TestJobRequest(BaseModel):
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]
    subject: str = ""

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clip_count(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("At least 1 clip is required")
        if len(v) > 20:
            raise ValueError("Maximum 20 clips allowed")
        return v


class TestJobResponse(BaseModel):
    job_id: str
    status: str
    template_id: str


class PresignedUploadRequest(BaseModel):
    filename: str
    content_type: str = "video/mp4"

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str) -> str:
        allowed = {"video/mp4", "video/quicktime", "video/webm"}
        if v not in allowed:
            raise ValueError(f"content_type must be one of {allowed}")
        return v


class PresignedUploadResponse(BaseModel):
    upload_url: str
    gcs_path: str


class RecipeVersionItem(BaseModel):
    id: str
    trigger: str
    created_at: datetime
    slot_count: int
    total_duration_s: float


class RecipeHistoryResponse(BaseModel):
    versions: list[RecipeVersionItem]
    total: int


# ── Helper ─────────────────────────────────────────────────────────────────────


def _template_response(t: VideoTemplate) -> TemplateResponse:
    return TemplateResponse(
        id=t.id,
        name=t.name,
        gcs_path=t.gcs_path,
        analysis_status=t.analysis_status,
        required_clips_min=t.required_clips_min,
        required_clips_max=t.required_clips_max,
        published_at=t.published_at,
        archived_at=t.archived_at,
        description=t.description,
        source_url=t.source_url,
        thumbnail_gcs_path=t.thumbnail_gcs_path,
        created_at=t.created_at,
    )


# ── Template CRUD endpoints ───────────────────────────────────────────────────


@router.get(
    "/templates",
    response_model=TemplateListResponse,
    dependencies=[Depends(_require_admin)],
)
async def list_templates(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> TemplateListResponse:
    """List all templates with job counts (admin view, includes unpublished)."""
    # Subquery for job counts per template
    job_count_sq = (
        select(
            Job.template_id,
            func.count(Job.id).label("job_count"),
        )
        .where(Job.template_id.isnot(None))
        .group_by(Job.template_id)
        .subquery()
    )

    query = (
        select(VideoTemplate, func.coalesce(job_count_sq.c.job_count, 0).label("job_count"))
        .outerjoin(job_count_sq, VideoTemplate.id == job_count_sq.c.template_id)
        .order_by(VideoTemplate.created_at.desc())
    )

    # Total count
    count_result = await db.execute(
        select(func.count()).select_from(select(VideoTemplate).subquery())
    )
    total = count_result.scalar() or 0

    # Fetch page
    result = await db.execute(query.offset(offset).limit(limit))
    rows = result.all()

    return TemplateListResponse(
        templates=[
            TemplateListItem(
                id=t.id,
                name=t.name,
                analysis_status=t.analysis_status,
                published_at=t.published_at,
                archived_at=t.archived_at,
                description=t.description,
                thumbnail_gcs_path=t.thumbnail_gcs_path,
                job_count=job_count,
                created_at=t.created_at,
            )
            for t, job_count in rows
        ],
        total=total,
    )


@router.post(
    "/templates",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_template(
    req: CreateTemplateRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Register a curated TikTok as a template and enqueue analysis."""
    from app.storage import object_exists  # noqa: PLC0415

    if not object_exists(req.gcs_path):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"GCS object not found: {req.gcs_path}",
        )

    template_id = str(uuid.uuid4())
    template = VideoTemplate(
        id=template_id,
        name=req.name,
        gcs_path=req.gcs_path,
        analysis_status="analyzing",
        required_clips_min=req.required_clips_min,
        required_clips_max=req.required_clips_max,
        description=req.description,
        source_url=req.source_url,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415
    analyze_template_task.delay(template_id)

    log.info("template_created", template_id=template_id, name=req.name)
    return _template_response(template)


@router.get(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Get template status and metadata."""
    template = await get_template_or_404(template_id, db)
    return _template_response(template)


@router.patch(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def update_template(
    template_id: str,
    req: UpdateTemplateRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Update template metadata, publish, or archive."""
    if req.publish and req.archive:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot publish and archive in the same request",
        )

    template = await get_template_or_404(template_id, db)

    if req.name is not None:
        template.name = req.name
    if req.description is not None:
        template.description = req.description
    if req.source_url is not None:
        template.source_url = req.source_url
    if req.required_clips_min is not None:
        template.required_clips_min = req.required_clips_min
    if req.required_clips_max is not None:
        template.required_clips_max = req.required_clips_max

    # Validate min <= max after applying partial updates
    if template.required_clips_min > template.required_clips_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"required_clips_min ({template.required_clips_min}) must be <= required_clips_max ({template.required_clips_max})",
        )

    if req.publish:
        if template.analysis_status != "ready":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot publish a template that is not ready",
            )
        template.published_at = datetime.now(UTC)
        template.archived_at = None  # unarchive if re-publishing
        log.info("template_published", template_id=template_id)

    if req.archive:
        template.archived_at = datetime.now(UTC)
        log.info("template_archived", template_id=template_id)

    await db.commit()
    await db.refresh(template)
    return _template_response(template)


@router.post(
    "/templates/{template_id}/reanalyze",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def reanalyze_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Re-run Gemini analysis on an existing template."""
    template = await get_template_or_404(template_id, db)

    template.analysis_status = "analyzing"
    await db.commit()
    await db.refresh(template)

    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415
    analyze_template_task.delay(template_id)

    log.info("template_reanalyzed", template_id=template_id)
    return _template_response(template)


# ── Test job endpoint ──────────────────────────────────────────────────────────


@router.post(
    "/templates/{template_id}/test-job",
    response_model=TestJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_test_job(
    template_id: str,
    req: TestJobRequest,
    db: AsyncSession = Depends(get_db),
) -> TestJobResponse:
    """Create a test job for a template using SYNTHETIC_USER_ID."""
    template = await get_template_or_404(template_id, db)
    require_ready(template)
    validate_clip_count(template, len(req.clip_gcs_paths))

    job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="template",
        template_id=template_id,
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

    log.info("test_job_created", job_id=job_id, template_id=template_id)
    return TestJobResponse(job_id=job_id, status="queued", template_id=template_id)


# ── Metrics endpoint ───────────────────────────────────────────────────────────


@router.get(
    "/templates/{template_id}/metrics",
    response_model=TemplateMetricsResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_template_metrics(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateMetricsResponse:
    """Aggregate job stats for a template (single query, not N+1)."""
    await get_template_or_404(template_id, db)

    result = await db.execute(
        select(
            func.count(Job.id).label("total"),
            func.count(Job.id).filter(Job.status == "template_ready").label("successful"),
            func.count(Job.id).filter(Job.status == "processing_failed").label("failed"),
            func.max(Job.created_at).label("last_job_at"),
        ).where(Job.template_id == template_id)
    )
    row = result.one()

    return TemplateMetricsResponse(
        template_id=template_id,
        total_jobs=row.total,
        successful_jobs=row.successful,
        failed_jobs=row.failed,
        last_job_at=row.last_job_at,
    )


# ── Recipe history endpoint ────────────────────────────────────────────────────


@router.get(
    "/templates/{template_id}/recipe-history",
    response_model=RecipeHistoryResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_recipe_history(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RecipeHistoryResponse:
    """Paginated list of recipe versions for a template."""
    await get_template_or_404(template_id, db)

    base = select(TemplateRecipeVersion).where(
        TemplateRecipeVersion.template_id == template_id
    )

    count_result = await db.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        base.order_by(TemplateRecipeVersion.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    versions = result.scalars().all()

    return RecipeHistoryResponse(
        versions=[
            RecipeVersionItem(
                id=str(v.id),
                trigger=v.trigger,
                created_at=v.created_at,
                slot_count=len(v.recipe.get("slots", [])) if isinstance(v.recipe, dict) else 0,
                total_duration_s=float(v.recipe.get("total_duration_s", 0)) if isinstance(v.recipe, dict) else 0,
            )
            for v in versions
        ],
        total=total,
    )


# ── Presigned upload endpoint ──────────────────────────────────────────────────


@router.post(
    "/upload-presigned",
    response_model=PresignedUploadResponse,
    dependencies=[Depends(_require_admin)],
)
async def upload_presigned(
    req: PresignedUploadRequest,
) -> PresignedUploadResponse:
    """Generate a presigned PUT URL for uploading a template video to GCS."""
    import datetime as dt  # noqa: PLC0415
    import os  # noqa: PLC0415

    from app.storage import _get_client  # noqa: PLC0415

    # Sanitize filename: strip path components to prevent path traversal
    safe_filename = os.path.basename(req.filename)
    if not safe_filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid filename",
        )

    template_upload_id = str(uuid.uuid4())
    gcs_path = f"templates/{template_upload_id}/{safe_filename}"

    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(gcs_path)
    url = blob.generate_signed_url(
        version="v4",
        expiration=dt.timedelta(minutes=30),
        method="PUT",
        content_type=req.content_type,
    )

    return PresignedUploadResponse(upload_url=url, gcs_path=gcs_path)
