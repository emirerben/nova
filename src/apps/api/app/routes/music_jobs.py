"""Music beat-sync job endpoints.

POST /music-jobs                — create a music-mode job
POST /music-jobs/upload-slot    — legacy API-passthrough slot upload
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

from app.auth import CurrentUserOrSynthetic
from app.config import settings
from app.database import get_db
from app.models import Job, MusicTrack

log = structlog.get_logger()
router = APIRouter()


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


def validate_clip_count(track: MusicTrack, n_clips: int) -> None:
    """Validate the user's clip count against the track's recipe.

    Templated tracks (recipe_cached has typed slots) require exactly one upload
    per `user_upload` slot. Legacy beat-sync tracks fall back to the
    track_config min/max bounds.
    """
    recipe = track.recipe_cached or {}
    user_slots = [s for s in recipe.get("slots", []) if s.get("slot_type") == "user_upload"]
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
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> MusicJobResponse:
    """Create a music beat-sync job."""
    track = await _get_published_ready_track(req.music_track_id, db)
    validate_clip_count(track, len(req.clip_gcs_paths))

    job = Job(
        user_id=current_user.id,
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

    from app.services.job_dispatch import enqueue_orchestrator  # noqa: PLC0415
    from app.tasks.music_orchestrate import orchestrate_music_job  # noqa: PLC0415

    await enqueue_orchestrator(orchestrate_music_job, job.id, db)

    log.info(
        "music_job_created",
        job_id=job_id,
        track_id=req.music_track_id,
        clips=len(req.clip_gcs_paths),
    )
    return MusicJobResponse(job_id=job_id, status="queued", music_track_id=req.music_track_id)


_SLOT_UPLOAD_VIDEO_CT = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/x-m4v"}
_SLOT_UPLOAD_IMAGE_CT = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
# Voiceover audio (generative voiceover edits). Lands under voiceover-uploads/, not
# music-uploads/, and never qualifies as a footage clip — see _validate_voiceover_path.
_SLOT_UPLOAD_AUDIO_CT = {
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/x-m4a",
    "audio/aac",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/webm",
    "audio/ogg",
}
_SLOT_UPLOAD_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi"}
_SLOT_UPLOAD_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_SLOT_UPLOAD_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".webm", ".ogg", ".aac"}
_SLOT_UPLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200 MB — short user clips, not full source
_UNKNOWN_CONTENT_TYPES = {"", "application/octet-stream"}


def _kind_from_extension(filename: str) -> str | None:
    ext = Path(filename or "").suffix.lower()
    if ext in _SLOT_UPLOAD_VIDEO_EXT:
        return "video"
    if ext in _SLOT_UPLOAD_IMAGE_EXT:
        return "image"
    if ext in _SLOT_UPLOAD_AUDIO_EXT:
        return "audio"
    return None


def _kind_from_content_type(content_type: str) -> str | None:
    ct = (content_type or "").lower()
    if ct in _SLOT_UPLOAD_VIDEO_CT:
        return "video"
    if ct in _SLOT_UPLOAD_IMAGE_CT:
        return "image"
    if ct in _SLOT_UPLOAD_AUDIO_CT:
        return "audio"
    return None


def classify_slot_kind(filename: str, content_type: str) -> str:
    """Resolve a slot upload's media kind, failing closed on MIME/extension drift."""
    ext = Path(filename or "").suffix.lower()
    ext_kind = _kind_from_extension(filename)
    # Strip MIME parameters (browser MediaRecorder emits "audio/webm;codecs=opus")
    # so the allowlist matches on the bare media type, not the codec-qualified string.
    ct_raw = (content_type or "").split(";", 1)[0].strip().lower()

    if ct_raw in _UNKNOWN_CONTENT_TYPES:
        if ext_kind is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"unsupported file type {content_type!r} / {ext!r}. "
                    "Use mp4/mov for video, jpg/png/webp/heic for image, "
                    "or mp3/m4a/wav for audio."
                ),
            )
        return ext_kind

    ct_kind = _kind_from_content_type(ct_raw)
    if ct_kind is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported content_type {content_type!r}",
        )

    if ext and (ext_kind is None or ext_kind != ct_kind):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"content_type {content_type!r} disagrees with extension {ext!r}",
        )

    return ct_kind


class SlotUploadResponse(BaseModel):
    gcs_path: str
    kind: str  # "video" | "image" | "audio"


@router.post(
    "/upload-slot",
    response_model=SlotUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_slot_clip(
    file: UploadFile = File(..., description="Video or image for a templated slot"),
) -> SlotUploadResponse:
    """Upload a short clip (video) or still (image) for a templated music slot.

    Deprecated API-passthrough path kept for one rollout. Admin Test tab
    uploads should use the track-scoped presigned route instead.
    """
    log.warning(
        "slot_upload_legacy_called",
        note="deprecated — use POST /admin/music-tracks/{id}/upload-slot-presigned",
    )
    ct = (file.content_type or "").lower()
    ext = Path(file.filename or "upload").suffix.lower()
    kind = classify_slot_kind(file.filename or "upload", ct)

    upload_id = uuid.uuid4().hex[:12]
    # Audio voiceovers land under voiceover-uploads/ (PII-swept by the GCS lifecycle
    # rule + validated by _validate_voiceover_path); video/image stay under music-uploads/.
    if kind == "audio":
        prefix, stem, default_ext, default_ct = "voiceover-uploads", "voice", ".webm", "audio/webm"
    elif kind == "video":
        prefix, stem, default_ext, default_ct = "music-uploads", "slot", ".mp4", "video/mp4"
    else:
        prefix, stem, default_ext, default_ct = "music-uploads", "slot", ".jpg", "image/jpeg"
    gcs_path = f"{prefix}/{upload_id}/{stem}{ext or default_ext}"

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
            content_type=ct or default_ct,
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
