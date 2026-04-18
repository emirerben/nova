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
        start_s = float(cfg.get("best_start_s", 0.0))
        end_s = float(cfg.get("best_end_s", 0.0))
        section_duration_s = round(max(0.0, end_s - start_s), 1)
        req_min = int(cfg.get("required_clips_min", 1))
        req_max = int(cfg.get("required_clips_max", 10))

        summaries.append(
            MusicTrackSummary(
                id=t.id,
                title=t.title,
                artist=t.artist,
                thumbnail_url=t.thumbnail_url,
                section_duration_s=section_duration_s,
                required_clips_min=req_min,
                required_clips_max=req_max,
            )
        )

    return MusicTrackListResponse(tracks=summaries)
