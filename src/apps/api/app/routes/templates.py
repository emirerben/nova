"""Public template endpoints.

GET /templates                  — list all ready templates (public, no auth)
GET /templates/:id              — single template by id (public, no auth)
GET /templates/:id/playback-url — signed GCS URL for template video playback
"""

import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import VideoTemplate

log = structlog.get_logger()
router = APIRouter()


# ── Response schemas ────────────────────────────────────────────────────────


class SlotSummary(BaseModel):
    """Lightweight slot info for the upload UI: which media to collect per slot."""
    position: int
    target_duration_s: float
    media_type: str  # "video" | "photo"


class TemplateListItem(BaseModel):
    id: str
    name: str
    gcs_path: str
    analysis_status: str
    slot_count: int
    total_duration_s: float
    copy_tone: str
    thumbnail_url: str | None
    required_clips_min: int
    required_clips_max: int
    slots: list[SlotSummary]
    # Always [] on this branch. The required_inputs feature lives on a
    # not-yet-merged branch with a JSONB column + migration. The deployed
    # frontend reads `.length` and `.map` on this field, so we surface an
    # empty array here to keep it from crashing with TypeError.
    required_inputs: list[dict] = []


class PlaybackUrlResponse(BaseModel):
    url: str
    expires_in_s: int


# ── Helpers ─────────────────────────────────────────────────────────────────


def _template_to_item(t: VideoTemplate) -> TemplateListItem | None:
    """Project a VideoTemplate row into the public list/detail response.

    Returns None when recipe_cached is missing or corrupt — callers decide
    whether that means "skip this row" (list) or "404" (detail).
    """
    if t.recipe_cached is None:
        return None

    try:
        recipe = t.recipe_cached
        raw_slots = recipe.get("slots", [])
        slot_summaries = [
            SlotSummary(
                position=int(s.get("position", i + 1)),
                target_duration_s=float(s.get("target_duration_s", 5.0)),
                media_type=str(s.get("media_type", "video")),
            )
            for i, s in enumerate(raw_slots)
        ]
        return TemplateListItem(
            id=t.id,
            name=t.name,
            gcs_path=t.gcs_path,
            analysis_status=t.analysis_status,
            slot_count=len(raw_slots),
            total_duration_s=float(recipe.get("total_duration_s", 0)),
            copy_tone=str(recipe.get("copy_tone", "casual")),
            thumbnail_url=None,  # v1: no thumbnails
            required_clips_min=t.required_clips_min,
            required_clips_max=t.required_clips_max,
            slots=slot_summaries,
        )
    except (TypeError, ValueError, AttributeError):
        log.warning("template_skipped_corrupt_recipe", template_id=t.id)
        return None


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[TemplateListItem])
async def list_templates(
    db: AsyncSession = Depends(get_db),
) -> list[TemplateListItem]:
    """Return all published, non-archived templates.

    Filters by published_at IS NOT NULL AND archived_at IS NULL.
    Derives slot_count, total_duration_s, copy_tone from recipe_cached JSONB.
    Silently skips templates with None or corrupt recipe_cached.
    """
    result = await db.execute(
        select(VideoTemplate).where(
            VideoTemplate.published_at.isnot(None),
            VideoTemplate.archived_at.is_(None),
            VideoTemplate.template_type != "music_child",
        )
    )
    templates = result.scalars().all()

    items = [item for item in (_template_to_item(t) for t in templates) if item is not None]
    log.info("templates_listed", count=len(items))
    return items


@router.get("/{template_id}", response_model=TemplateListItem)
async def get_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateListItem:
    """Return a single published, non-archived template by id.

    Used by the per-template detail/upload page on the deployed frontend
    (`/template/[id]`), which expects a 404 with `{"detail": "Template not
    found"}` when the id is missing — distinct from FastAPI's default
    `{"detail": "Not Found"}` for an unrouted path.
    """
    result = await db.execute(
        select(VideoTemplate).where(
            VideoTemplate.id == template_id,
            VideoTemplate.published_at.isnot(None),
            VideoTemplate.archived_at.is_(None),
            VideoTemplate.template_type != "music_child",
        )
    )
    template = result.scalar_one_or_none()
    item = _template_to_item(template) if template is not None else None
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )
    return item


@router.get("/{template_id}/playback-url", response_model=PlaybackUrlResponse)
async def get_playback_url(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> PlaybackUrlResponse:
    """Return a time-limited signed GCS URL for the template video.

    Used by the side-by-side comparison view (original template vs generated output).
    """
    result = await db.execute(
        select(VideoTemplate).where(VideoTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()

    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )

    if template.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template is not ready yet",
        )

    # Generate a 1-hour signed GET URL for the template video
    from app.config import settings

    try:
        from app.storage import _get_client

        client = _get_client()
        bucket = client.bucket(settings.storage_bucket)
        blob = bucket.blob(template.gcs_path)

        expires_in_s = 3600
        url = blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(seconds=expires_in_s),
            method="GET",
        )
    except Exception as exc:
        log.error("playback_url_failed", template_id=template_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not generate playback URL",
        ) from exc

    return PlaybackUrlResponse(url=url, expires_in_s=expires_in_s)
