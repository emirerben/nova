"""Public music-track endpoints.

GET /music-tracks  — list published music tracks for the gallery
"""

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import MusicTrack

log = structlog.get_logger()
router = APIRouter()


class MusicTrackSummary(BaseModel):
    id: str
    title: str
    artist: str
    thumbnail_url: str | None
    section_duration_s: float
    required_clips_min: int
    required_clips_max: int
    # When the track has a typed-slot recipe (Love-From-Moon style), the
    # frontend uses these to render a per-slot upload UI instead of the
    # generic clip-list textarea.
    template_kind: str = "beat_sync"  # "beat_sync" | "templated"
    user_slot_count: int = 0
    user_slot_accepts: list[str] = []  # ["video","image"] per templated slot


class MusicTrackListResponse(BaseModel):
    tracks: list[MusicTrackSummary]


@router.get("", response_model=MusicTrackListResponse)
async def list_music_tracks(
    db: AsyncSession = Depends(get_db),
) -> MusicTrackListResponse:
    """Return all published, non-archived music tracks for the gallery."""
    result = await db.execute(
        select(MusicTrack)
        .where(MusicTrack.published_at.isnot(None))
        .where(MusicTrack.archived_at.is_(None))
        .where(MusicTrack.analysis_status == "ready")
        .order_by(MusicTrack.published_at.desc())
    )
    tracks = result.scalars().all()

    summaries = []
    for t in tracks:
        cfg = t.track_config or {}
        recipe = t.recipe_cached or {}
        user_slots = [
            s for s in recipe.get("slots", [])
            if s.get("slot_type") == "user_upload"
        ]
        is_templated = bool(user_slots)

        if is_templated:
            section_duration_s = round(
                sum(float(s.get("target_duration_s", 0.0)) for s in recipe.get("slots", [])),
                1,
            )
            req_min = req_max = len(user_slots)
            accepts: list[str] = []
            for s in sorted(user_slots, key=lambda x: int(x.get("position", 0))):
                a = s.get("accepts") or ["video", "image"]
                accepts.append(",".join(a))
            template_kind = "templated"
        else:
            start_s = float(cfg.get("best_start_s", 0.0))
            end_s = float(cfg.get("best_end_s", 0.0))
            section_duration_s = round(max(0.0, end_s - start_s), 1)
            req_min = int(cfg.get("required_clips_min", 1))
            req_max = int(cfg.get("required_clips_max", 10))
            accepts = []
            template_kind = "beat_sync"

        summaries.append(
            MusicTrackSummary(
                id=t.id,
                title=t.title,
                artist=t.artist,
                thumbnail_url=t.thumbnail_url,
                section_duration_s=section_duration_s,
                required_clips_min=req_min,
                required_clips_max=req_max,
                template_kind=template_kind,
                user_slot_count=len(user_slots),
                user_slot_accepts=accepts,
            )
        )

    return MusicTrackListResponse(tracks=summaries)
