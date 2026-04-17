"""Music beat-sync job endpoints.

POST /music-jobs         — create a music-mode job
GET  /music-jobs/{id}/status — poll job status
"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Job, MusicTrack

log = structlog.get_logger()
router = APIRouter()

# Synthetic user for MVP (matches template_jobs.py)
SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ── Schemas ────────────────────────────────────────────────────────────────────


class CreateMusicJobRequest(BaseModel):
    music_track_id: str
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clips(cls, v: list[str]) -> list[str]:
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


class MusicJobResponse(BaseModel):
    job_id: str
    status: str
    music_track_id: str


class MusicJobStatusResponse(BaseModel):
    job_id: str
    status: str
    music_track_id: str | None
    assembly_plan: dict | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _get_published_ready_track(track_id: str, db: AsyncSession) -> MusicTrack:
    """Load track; raise 404 if not found, 409 if not ready, 422 if not published."""
    result = await db.execute(select(MusicTrack).where(MusicTrack.id == track_id))
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Music track not found")
    if track.published_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Music track is not published yet.",
        )
    if track.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Music track has been archived.",
        )
    if track.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Music track analysis is not complete (status: {track.analysis_status}).",
        )
    if not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Music track has no audio file — contact admin.",
        )
    return track


def _validate_clip_count(track: MusicTrack, n_clips: int) -> None:
    cfg = track.track_config or {}
    req_min = int(cfg.get("required_clips_min", 1))
    req_max = int(cfg.get("required_clips_max", 20))
    if n_clips < req_min:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This track requires at least {req_min} clips, got {n_clips}.",
        )
    if n_clips > req_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This track allows at most {req_max} clips, got {n_clips}.",
        )


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("", response_model=MusicJobResponse, status_code=status.HTTP_201_CREATED)
async def create_music_job(
    req: CreateMusicJobRequest,
    db: AsyncSession = Depends(get_db),
) -> MusicJobResponse:
    """Create a music beat-sync job."""
    track = await _get_published_ready_track(req.music_track_id, db)
    _validate_clip_count(track, len(req.clip_gcs_paths))

    job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="music",
        music_track_id=req.music_track_id,
        raw_storage_path=req.clip_gcs_paths[0],
        selected_platforms=req.selected_platforms,
        all_candidates={"clip_paths": req.clip_gcs_paths},
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    job_id = str(job.id)

    from app.tasks.music_orchestrate import orchestrate_music_job  # noqa: PLC0415
    orchestrate_music_job.delay(job_id)

    log.info(
        "music_job_created",
        job_id=job_id,
        track_id=req.music_track_id,
        clips=len(req.clip_gcs_paths),
    )
    return MusicJobResponse(job_id=job_id, status="queued", music_track_id=req.music_track_id)


@router.get("/{job_id}/status", response_model=MusicJobStatusResponse)
async def get_music_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> MusicJobStatusResponse:
    """Poll music job status."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()

    if job is None or job.job_type != "music":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return MusicJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        music_track_id=job.music_track_id,
        assembly_plan=job.assembly_plan,
        error_detail=job.error_detail,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
