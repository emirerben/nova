"""Music beat-sync job endpoints.

POST /music-jobs                — create a music-mode job
POST /music-jobs/upload-slot    — direct upload of a video/image for a templated slot
GET  /music-jobs/{id}/status    — poll job status
"""

import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Job, MusicTrack

log = structlog.get_logger()
router = APIRouter()

# Synthetic user for MVP (matches template_jobs.py).
# TODO: replace with get_current_user(db) once auth infrastructure is built.
#       POST /music-jobs is currently unauthenticated — any caller can trigger
#       Gemini API calls and GCS reads. Acceptable for internal MVP; must be
#       fixed before public launch.
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
    """Validate the user's clip count against the track's recipe.

    Templated tracks (recipe_cached has typed slots) require exactly one upload
    per `user_upload` slot. Legacy beat-sync tracks fall back to the
    track_config min/max bounds.
    """
    recipe = track.recipe_cached or {}
    user_slots = [
        s for s in recipe.get("slots", []) if s.get("slot_type") == "user_upload"
    ]
    if user_slots:
        expected = len(user_slots)
        if n_clips != expected:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"This template expects exactly {expected} upload"
                    f"{'s' if expected != 1 else ''}, got {n_clips}."
                ),
            )
        return

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


_SLOT_UPLOAD_VIDEO_CT = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/x-m4v"}
_SLOT_UPLOAD_IMAGE_CT = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif",
}
_SLOT_UPLOAD_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi"}
_SLOT_UPLOAD_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_SLOT_UPLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200 MB — short user clips, not full source


class SlotUploadResponse(BaseModel):
    gcs_path: str
    kind: str  # "video" | "image"


@router.post(
    "/upload-slot",
    response_model=SlotUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_slot_clip(
    file: UploadFile = File(..., description="Video or image for a templated slot"),
) -> SlotUploadResponse:
    """Upload a short clip (video) or still (image) for a templated music slot.

    Used by the user-facing music page when the selected track is templated
    (e.g. Love-From-Moon). Returns a GCS path that the caller passes to
    `POST /music-jobs` as the slot's clip.
    """
    ct = (file.content_type or "").lower()
    ext = Path(file.filename or "upload").suffix.lower()

    if ct in _SLOT_UPLOAD_VIDEO_CT or ext in _SLOT_UPLOAD_VIDEO_EXT:
        kind = "video"
    elif ct in _SLOT_UPLOAD_IMAGE_CT or ext in _SLOT_UPLOAD_IMAGE_EXT:
        kind = "image"
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported file type {ct!r} / {ext!r}. "
                "Use mp4/mov for video or jpg/png/webp for image."
            ),
        )

    upload_id = uuid.uuid4().hex[:12]
    gcs_path = f"music-uploads/{upload_id}/slot{ext or ('.mp4' if kind == 'video' else '.jpg')}"

    with tempfile.NamedTemporaryFile(suffix=ext or ".bin", delete=True) as tmp:
        content = await file.read()
        if len(content) > _SLOT_UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File too large. Maximum 200 MB.",
            )
        if not content:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Empty file",
            )
        tmp.write(content)
        tmp.flush()

        from app.storage import _get_client  # noqa: PLC0415
        bucket = _get_client().bucket(settings.storage_bucket)
        bucket.blob(gcs_path).upload_from_filename(
            tmp.name,
            content_type=ct or ("video/mp4" if kind == "video" else "image/jpeg"),
        )

    log.info("slot_upload_done", gcs_path=gcs_path, kind=kind, bytes=len(content))
    return SlotUploadResponse(gcs_path=gcs_path, kind=kind)


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
