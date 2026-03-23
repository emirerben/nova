"""Admin endpoints for managing video templates.

POST /admin/templates  — register a curated TikTok as a template
GET  /admin/templates/:id — check template analysis status

Auth: X-Admin-Token header (static key from settings.admin_api_key).
"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import VideoTemplate

log = structlog.get_logger()

router = APIRouter()

# ── Auth dependency ────────────────────────────────────────────────────────────


def _require_admin(x_admin_token: str = Header(...)) -> None:
    """FastAPI dependency: validates X-Admin-Token header."""
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API not configured",
        )
    if x_admin_token != settings.admin_api_key:
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
    created_at: datetime


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post(
    "",
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

    # Verify the GCS object exists before registering
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
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    # Enqueue eager Gemini analysis
    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415
    analyze_template_task.delay(template_id)

    log.info("template_created", template_id=template_id, name=req.name)
    return TemplateResponse(
        id=template.id,
        name=template.name,
        gcs_path=template.gcs_path,
        analysis_status=template.analysis_status,
        required_clips_min=template.required_clips_min,
        required_clips_max=template.required_clips_max,
        created_at=template.created_at,
    )


@router.post(
    "/{template_id}/reanalyze",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def reanalyze_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Re-run Gemini analysis on an existing template (e.g. to populate beat data)."""
    from sqlalchemy import select  # noqa: PLC0415

    result = await db.execute(select(VideoTemplate).where(VideoTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    template.analysis_status = "analyzing"
    await db.commit()
    await db.refresh(template)

    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415
    analyze_template_task.delay(template_id)

    log.info("template_reanalyze_enqueued", template_id=template_id)
    return TemplateResponse(
        id=template.id,
        name=template.name,
        gcs_path=template.gcs_path,
        analysis_status=template.analysis_status,
        required_clips_min=template.required_clips_min,
        required_clips_max=template.required_clips_max,
        created_at=template.created_at,
    )


@router.get(
    "/{template_id}",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Get template status (analyzing → ready)."""
    from sqlalchemy import select  # noqa: PLC0415

    result = await db.execute(select(VideoTemplate).where(VideoTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    return TemplateResponse(
        id=template.id,
        name=template.name,
        gcs_path=template.gcs_path,
        analysis_status=template.analysis_status,
        required_clips_min=template.required_clips_min,
        required_clips_max=template.required_clips_max,
        created_at=template.created_at,
    )
