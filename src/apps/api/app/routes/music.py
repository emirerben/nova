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
from app.storage import signed_get_url

log = structlog.get_logger()
router = APIRouter()

# Audio-preview signed-URL TTL. Short on purpose: this exposes full-track audio
# behind a public URL, so we trade a little convenience (a long edit session may
# need a gallery re-fetch) for a tight exposure window. If rights/abuse ever
# bite, swap to a pre-rendered ~6s hook clip stored under the non-expiring
# `music/` prefix (see plan failure-modes table).
_PREVIEW_AUDIO_TTL_MIN = 60


def _preview_audio_url(audio_gcs_path: str | None) -> str | None:
    """Signed GET URL for a track's audio, or None. Never raises — a signing
    failure (missing creds locally, transient GCS error) must not 500 the
    public gallery, so the picker just hides the play button for that track."""
    if not audio_gcs_path:
        return None
    try:
        return signed_get_url(audio_gcs_path, expiration_minutes=_PREVIEW_AUDIO_TTL_MIN)
    except Exception:  # noqa: BLE001 — best-effort; gallery must still render
        log.warning("preview_audio_sign_failed", audio_gcs_path=audio_gcs_path)
        return None


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
    # Audio preview for the song picker: a short-lived signed URL to the track
    # audio, plus the second to seek to (the matched hook). Lets a user HEAR a
    # song before committing a re-render. TTL is deliberately short (60 min) —
    # this is full-track audio behind a public URL, so we cap exposure. None when
    # the track has no stored audio or signing fails (picker hides the play btn).
    preview_audio_url: str | None = None
    preview_start_s: float = 0.0


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
        user_slots = [s for s in recipe.get("slots", []) if s.get("slot_type") == "user_upload"]
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
                preview_audio_url=_preview_audio_url(t.audio_gcs_path),
                preview_start_s=round(float(cfg.get("best_start_s", 0.0)), 2),
            )
        )

    return MusicTrackListResponse(tracks=summaries)
