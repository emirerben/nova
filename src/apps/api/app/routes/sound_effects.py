"""Public sound-effects endpoints.

GET /sound-effects  — list published sound effects for the glossary picker
"""

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import SoundEffect
from app.storage import signed_get_url

log = structlog.get_logger()
router = APIRouter()

# Audio-preview signed-URL TTL. Short on purpose: this exposes raw audio
# behind a public URL.
_PREVIEW_AUDIO_TTL_MIN = 60


def _preview_audio_url(audio_gcs_path: str | None) -> str | None:
    """Signed GET URL for an effect's audio, or None. Never raises."""
    if not audio_gcs_path:
        return None
    try:
        return signed_get_url(audio_gcs_path, expiration_minutes=_PREVIEW_AUDIO_TTL_MIN)
    except Exception:  # noqa: BLE001 — best-effort; gallery must still render
        log.warning("sfx_preview_sign_failed", audio_gcs_path=audio_gcs_path)
        return None


class SoundEffectSummary(BaseModel):
    id: str
    name: str
    duration_s: float | None
    # Short-lived signed URL for audio preview in the picker. None when
    # signing fails or the effect has no audio yet.
    preview_audio_url: str | None = None


class SoundEffectListResponse(BaseModel):
    effects: list[SoundEffectSummary]


@router.get("", response_model=SoundEffectListResponse)
async def list_sound_effects(
    db: AsyncSession = Depends(get_db),
) -> SoundEffectListResponse:
    """Return all published, non-archived sound effects for the glossary picker."""
    result = await db.execute(
        select(SoundEffect)
        .where(SoundEffect.published_at.isnot(None))
        .where(SoundEffect.archived_at.is_(None))
        .where(SoundEffect.status == "ready")
        .order_by(SoundEffect.created_at.desc())
    )
    effects = result.scalars().all()

    return SoundEffectListResponse(
        effects=[
            SoundEffectSummary(
                id=e.id,
                name=e.name,
                duration_s=e.duration_s,
                preview_audio_url=_preview_audio_url(e.audio_gcs_path),
            )
            for e in effects
        ]
    )
