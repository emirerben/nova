"""Shared template validation logic.

Extracted from template_jobs.py so both the public template-job endpoint
and the admin test-job endpoint can reuse the same checks.
"""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VideoTemplate


async def get_template_or_404(
    template_id: str,
    db: AsyncSession,
) -> VideoTemplate:
    """Fetch a template by ID or raise 404."""
    result = await db.execute(
        select(VideoTemplate).where(VideoTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Template not found",
        )
    return template


def require_ready(template: VideoTemplate) -> None:
    """Raise 409 if template analysis is not complete."""
    if template.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Template is still being analyzed (status: {template.analysis_status}). "
                "Try again in a few seconds."
            ),
        )


def validate_clip_count(template: VideoTemplate, n_clips: int) -> None:
    """Raise 422 if clip count is outside template bounds."""
    if n_clips < template.required_clips_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Template requires at least {template.required_clips_min} clips, "
                f"got {n_clips}."
            ),
        )
    if n_clips > template.required_clips_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Template allows at most {template.required_clips_max} clips, "
                f"got {n_clips}."
            ),
        )
