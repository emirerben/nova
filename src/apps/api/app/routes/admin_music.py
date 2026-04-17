"""Admin endpoints for managing music tracks.

POST   /admin/music-tracks              — add track from YouTube/SoundCloud URL
GET    /admin/music-tracks              — list all tracks (including unpublished)
GET    /admin/music-tracks/{id}         — full track detail + beat count
PATCH  /admin/music-tracks/{id}         — update config, title, artist, publish/archive
POST   /admin/music-tracks/{id}/reanalyze — re-run beat detection
DELETE /admin/music-tracks/{id}         — soft-archive only

Auth: X-Admin-Token header (same as admin.py).
"""

import asyncio
import hmac
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import MusicTrack
from app.services.audio_download import DownloadError, download_audio_and_upload, is_supported_audio_url

log = structlog.get_logger()
router = APIRouter()


# ── Auth ───────────────────────────────────────────────────────────────────────


def _require_admin(x_admin_token: str = Header(...)) -> None:
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


# ── Schemas ────────────────────────────────────────────────────────────────────


class CreateMusicTrackRequest(BaseModel):
    source_url: str
    title: str | None = None
    artist: str | None = None

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not is_supported_audio_url(v.strip()):
            raise ValueError(
                "Only YouTube (youtube.com, youtu.be) and SoundCloud (soundcloud.com) URLs are supported."
            )
        return v.strip()


class UpdateMusicTrackRequest(BaseModel):
    title: str | None = None
    artist: str | None = None
    thumbnail_url: str | None = None
    track_config: dict | None = None
    publish: bool | None = None
    archive: bool | None = None

    @model_validator(mode="after")
    def validate_track_config_bounds(self) -> "UpdateMusicTrackRequest":
        cfg = self.track_config or {}
        if "slot_every_n_beats" in cfg and int(cfg["slot_every_n_beats"]) < 1:
            raise ValueError("slot_every_n_beats must be >= 1")
        if "best_end_s" in cfg and "best_start_s" in cfg:
            if float(cfg["best_end_s"]) <= float(cfg["best_start_s"]):
                raise ValueError("best_end_s must be greater than best_start_s")
        return self


class MusicTrackResponse(BaseModel):
    id: str
    title: str
    artist: str
    source_url: str
    audio_gcs_path: str | None
    duration_s: float | None
    beat_count: int
    analysis_status: str
    error_detail: str | None
    thumbnail_url: str | None
    published_at: datetime | None
    archived_at: datetime | None
    track_config: dict | None
    created_at: datetime


class MusicTrackListResponse(BaseModel):
    tracks: list[MusicTrackResponse]
    total: int


class CreateMusicTrackResponse(BaseModel):
    id: str
    analysis_status: str


class ReanalyzeResponse(BaseModel):
    track_id: str
    analysis_status: str


# ── Helpers ────────────────────────────────────────────────────────────────────


def _to_response(t: MusicTrack) -> MusicTrackResponse:
    beats = t.beat_timestamps_s or []
    return MusicTrackResponse(
        id=t.id,
        title=t.title,
        artist=t.artist,
        source_url=t.source_url,
        audio_gcs_path=t.audio_gcs_path,
        duration_s=t.duration_s,
        beat_count=len(beats),
        analysis_status=t.analysis_status,
        error_detail=t.error_detail,
        thumbnail_url=t.thumbnail_url,
        published_at=t.published_at,
        archived_at=t.archived_at,
        track_config=t.track_config,
        created_at=t.created_at,
    )


async def _get_track_or_404(track_id: str, db: AsyncSession) -> MusicTrack:
    result = await db.execute(select(MusicTrack).where(MusicTrack.id == track_id))
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Music track not found")
    return track


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=CreateMusicTrackResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_music_track(
    req: CreateMusicTrackRequest,
    db: AsyncSession = Depends(get_db),
) -> CreateMusicTrackResponse:
    """Download audio from URL, upload to GCS, queue analysis task."""
    track_id = str(uuid.uuid4())

    # Run the blocking yt-dlp download in a thread so the uvicorn event loop
    # is not frozen for the duration of the download (30-180s for a full track).
    try:
        gcs_path, duration_s, thumbnail_url = await asyncio.to_thread(
            download_audio_and_upload, req.source_url
        )
    except DownloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    track = MusicTrack(
        id=track_id,
        title=req.title or f"Track {track_id[:8]}",
        artist=req.artist or "",
        source_url=req.source_url,
        audio_gcs_path=gcs_path,
        duration_s=duration_s,
        thumbnail_url=thumbnail_url,
        analysis_status="queued",
    )
    db.add(track)
    await db.commit()
    await db.refresh(track)

    # Dispatch analysis task
    from app.tasks.music_orchestrate import analyze_music_track_task  # noqa: PLC0415
    analyze_music_track_task.delay(track_id)

    log.info("music_track_created", track_id=track_id, source_url=req.source_url)
    return CreateMusicTrackResponse(id=track_id, analysis_status="queued")


@router.get(
    "",
    response_model=MusicTrackListResponse,
    dependencies=[Depends(_require_admin)],
)
async def list_music_tracks(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MusicTrackListResponse:
    """List all music tracks (including unpublished and archived)."""
    base_query = select(MusicTrack)

    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        base_query.order_by(MusicTrack.created_at.desc()).offset(offset).limit(limit)
    )
    tracks = result.scalars().all()

    return MusicTrackListResponse(
        tracks=[_to_response(t) for t in tracks],
        total=total,
    )


@router.get(
    "/{track_id}",
    response_model=MusicTrackResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_music_track(
    track_id: str,
    db: AsyncSession = Depends(get_db),
) -> MusicTrackResponse:
    track = await _get_track_or_404(track_id, db)
    return _to_response(track)


@router.patch(
    "/{track_id}",
    response_model=MusicTrackResponse,
    dependencies=[Depends(_require_admin)],
)
async def update_music_track(
    track_id: str,
    req: UpdateMusicTrackRequest,
    db: AsyncSession = Depends(get_db),
) -> MusicTrackResponse:
    """Update metadata, publish, or archive a music track."""
    track = await _get_track_or_404(track_id, db)

    if req.title is not None:
        track.title = req.title
    if req.artist is not None:
        track.artist = req.artist
    if req.thumbnail_url is not None:
        track.thumbnail_url = req.thumbnail_url
    if req.track_config is not None:
        merged_config = {**(track.track_config or {}), **req.track_config}
        if merged_config.get("best_end_s", 0) <= merged_config.get("best_start_s", 0):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="best_end_s must be greater than best_start_s",
            )
        track.track_config = merged_config

    now = datetime.now(UTC)
    if req.publish is True and track.published_at is None:
        track.published_at = now
    elif req.publish is False:
        track.published_at = None

    if req.archive is True and track.archived_at is None:
        track.archived_at = now
    elif req.archive is False:
        track.archived_at = None

    await db.commit()
    await db.refresh(track)
    log.info("music_track_updated", track_id=track_id)
    return _to_response(track)


@router.post(
    "/{track_id}/reanalyze",
    response_model=ReanalyzeResponse,
    dependencies=[Depends(_require_admin)],
)
async def reanalyze_music_track(
    track_id: str,
    db: AsyncSession = Depends(get_db),
) -> ReanalyzeResponse:
    """Re-run beat detection on a music track."""
    track = await _get_track_or_404(track_id, db)

    if not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Track has no audio file — re-upload the track first.",
        )

    track.analysis_status = "queued"
    track.error_detail = None
    await db.commit()

    from app.tasks.music_orchestrate import analyze_music_track_task  # noqa: PLC0415
    analyze_music_track_task.delay(track_id)

    log.info("music_track_reanalyze_dispatched", track_id=track_id)
    return ReanalyzeResponse(track_id=track_id, analysis_status="queued")


@router.delete(
    "/{track_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_require_admin)],
)
async def archive_music_track(
    track_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-archive a music track (hides from gallery; jobs referencing it continue)."""
    track = await _get_track_or_404(track_id, db)
    track.archived_at = datetime.now(UTC)
    await db.commit()
    log.info("music_track_archived", track_id=track_id)
