"""Admin sound-effects endpoints.

Manages the admin-curated SFX glossary (upload, list, publish, archive).
Pattern mirrors admin_music.py upload-init-file / upload-confirm / CRUD.

No Celery analysis stage — SFX needs no beat detection. upload-confirm
ffprobes the file, sets status="ready", done.

3-phase signed-URL upload (matches admin_music.py pattern, dodges Vercel 4.5 MB cap):
  1. POST /upload-init-file  → mint pending row + 15-min signed PUT URL
  2. Client PUTs bytes directly to GCS
  3. POST /{id}/upload-confirm → HEAD+ffprobe, set ready, no Celery

All routes require X-Admin-Token header.
"""

from __future__ import annotations

import asyncio
import datetime
import hmac
import tempfile
import uuid
from datetime import UTC
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import SoundEffect

log = structlog.get_logger()
router = APIRouter()

# ── Constants ─────────────────────────────────────────────────────────────────

_BROWSER_AUDIO_PUT_TTL = datetime.timedelta(minutes=15)
_BROWSER_AUDIO_MIN_BYTES = 1_024  # 1 KB
_BROWSER_AUDIO_MAX_BYTES = 100 * 1024 * 1024  # 100 MB

_BROWSER_AUDIO_EXT_ALLOWLIST = frozenset(
    {".mp3", ".m4a", ".mp4", ".wav", ".aac", ".ogg", ".opus", ".webm"}
)

_BROWSER_AUDIO_EXT_TO_CONTENT_TYPE: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg; codecs=opus",
    ".webm": "audio/webm",
}


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


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_effect_or_404(effect_id: str, db: AsyncSession) -> SoundEffect:
    result = await db.execute(select(SoundEffect).where(SoundEffect.id == effect_id))
    effect = result.scalar_one_or_none()
    if effect is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sound effect {effect_id!r} not found.",
        )
    return effect


def _sign_sfx_audio_put(gcs_path: str, content_type: str) -> str:
    """Mint a 15-minute signed PUT URL scoped to gcs_path + content_type."""
    from app.storage import _get_client  # noqa: PLC0415

    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(gcs_path)
    return blob.generate_signed_url(
        version="v4",
        method="PUT",
        content_type=content_type,
        expiration=_BROWSER_AUDIO_PUT_TTL,
    )


# ── Schemas ────────────────────────────────────────────────────────────────────


class SoundEffectResponse(BaseModel):
    id: str
    name: str
    audio_gcs_path: str | None
    duration_s: float | None
    status: str
    error_detail: str | None
    source_filename: str | None
    published_at: datetime.datetime | None
    archived_at: datetime.datetime | None
    created_at: datetime.datetime


class SoundEffectListResponse(BaseModel):
    effects: list[SoundEffectResponse]
    total: int


def _to_response(effect: SoundEffect) -> SoundEffectResponse:
    return SoundEffectResponse(
        id=effect.id,
        name=effect.name,
        audio_gcs_path=effect.audio_gcs_path,
        duration_s=effect.duration_s,
        status=effect.status,
        error_detail=effect.error_detail,
        source_filename=effect.source_filename,
        published_at=effect.published_at,
        archived_at=effect.archived_at,
        created_at=effect.created_at,
    )


class FileUploadInitRequest(BaseModel):
    """Mint a signed PUT URL for the admin SPA's "Upload file" form."""

    filename: str
    name: str | None = None
    ext: str
    byte_count: int

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("filename cannot be empty")
        if "/" in v or "\\" in v:
            raise ValueError("filename must not contain slashes")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("filename must not contain control characters")
        return v

    @field_validator("ext")
    @classmethod
    def validate_ext(cls, v: str) -> str:
        v = v.lower().strip()
        if not v.startswith("."):
            v = "." + v
        if v not in _BROWSER_AUDIO_EXT_ALLOWLIST:
            allowed = ", ".join(sorted(_BROWSER_AUDIO_EXT_ALLOWLIST))
            raise ValueError(f"Unsupported audio extension: {v}. Allowed: {allowed}")
        return v

    @field_validator("byte_count")
    @classmethod
    def validate_byte_count(cls, v: int) -> int:
        if v < _BROWSER_AUDIO_MIN_BYTES:
            raise ValueError(
                f"byte_count too small ({v}). Must be at least {_BROWSER_AUDIO_MIN_BYTES}."
            )
        if v > _BROWSER_AUDIO_MAX_BYTES:
            raise ValueError(f"byte_count exceeds limit ({v} > {_BROWSER_AUDIO_MAX_BYTES}).")
        return v


class UploadInitResponse(BaseModel):
    effect_id: str
    upload_url: str
    gcs_path: str
    content_type: str
    expires_in_s: int


class UploadConfirmResponse(BaseModel):
    effect_id: str
    status: str
    duration_s: float | None


class UpdateSoundEffectRequest(BaseModel):
    name: str | None = None
    publish: bool | None = None
    archive: bool | None = None


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post(
    "/upload-init-file",
    response_model=UploadInitResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def upload_init_file(
    req: FileUploadInitRequest,
    db: AsyncSession = Depends(get_db),
) -> UploadInitResponse:
    """Phase 1: create a pending SoundEffect row + mint signed PUT URL.

    The admin SPA PUTs the blob straight to GCS using the returned signed URL,
    then calls /{id}/upload-confirm to verify + mark ready.
    """
    effect_id = str(uuid.uuid4()).replace("-", "")  # hex, no dashes
    gcs_path = f"sound-effects/{effect_id}/audio{req.ext}"
    content_type = _BROWSER_AUDIO_EXT_TO_CONTENT_TYPE[req.ext]
    upload_url = _sign_sfx_audio_put(gcs_path, content_type)

    display_name = (req.name or "").strip() or req.filename or f"Effect {effect_id[:8]}"
    effect = SoundEffect(
        id=effect_id,
        name=display_name,
        audio_gcs_path=gcs_path,
        source_filename=req.filename,
        status="pending",
    )
    db.add(effect)
    await db.commit()

    log.info("sfx_upload_init", effect_id=effect_id, filename=req.filename, ext=req.ext)
    return UploadInitResponse(
        effect_id=effect_id,
        upload_url=upload_url,
        gcs_path=gcs_path,
        content_type=content_type,
        expires_in_s=int(_BROWSER_AUDIO_PUT_TTL.total_seconds()),
    )


@router.post(
    "/{effect_id}/upload-confirm",
    response_model=UploadConfirmResponse,
    dependencies=[Depends(_require_admin)],
)
async def upload_confirm(
    effect_id: str,
    db: AsyncSession = Depends(get_db),
) -> UploadConfirmResponse:
    """Phase 3: verify GCS blob + ffprobe + mark ready (NO Celery dispatch).

    Unlike music tracks, SFX needs no beat analysis — just probe and mark ready.
    """
    effect = await _get_effect_or_404(effect_id, db)

    # Idempotency: if already past pending with an audio path, echo current state.
    if effect.status not in ("pending", "failed") and effect.audio_gcs_path:
        return UploadConfirmResponse(
            effect_id=effect.id,
            status=effect.status,
            duration_s=effect.duration_s,
        )

    expected_path = effect.audio_gcs_path
    if not expected_path:
        effect.status = "failed"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No audio_gcs_path set on this effect. Did upload-init-file succeed?",
        )

    from app.storage import _get_client  # noqa: PLC0415

    bucket = _get_client().bucket(settings.storage_bucket)
    found_blob = bucket.blob(expected_path)
    if not await asyncio.to_thread(found_blob.exists):
        effect.status = "failed"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No audio blob found at the expected GCS path. Did the PUT succeed?",
        )

    await asyncio.to_thread(found_blob.reload)

    if found_blob.size and found_blob.size > _BROWSER_AUDIO_MAX_BYTES:
        try:
            await asyncio.to_thread(found_blob.delete)
        except Exception:  # noqa: BLE001
            log.warning("sfx_oversize_delete_failed", effect_id=effect_id)
        effect.status = "failed"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Uploaded blob exceeds 100 MB cap ({found_blob.size} bytes).",
        )

    # ffprobe to get duration and verify it's audio.
    ext = Path(expected_path).suffix
    duration_s: float | None = None
    with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
        await asyncio.to_thread(found_blob.download_to_filename, tmp.name)
        from app.services.audio_download import probe_duration as _probe_dur  # noqa: PLC0415
        from app.services.audio_download import probe_has_audio_stream  # noqa: PLC0415

        is_audio = await asyncio.to_thread(probe_has_audio_stream, tmp.name)
        if not is_audio:
            effect.status = "failed"
            await db.commit()
            try:
                await asyncio.to_thread(found_blob.delete)
            except Exception:  # noqa: BLE001
                pass
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded blob is not decodable audio (no audio stream found by ffprobe).",
            )
        duration_s = await asyncio.to_thread(_probe_dur, tmp.name)

    effect.audio_gcs_path = expected_path
    effect.duration_s = duration_s
    effect.status = "ready"
    await db.commit()
    await db.refresh(effect)

    log.info(
        "sfx_upload_confirmed",
        effect_id=effect_id,
        gcs_path=expected_path,
        duration_s=duration_s,
    )
    return UploadConfirmResponse(effect_id=effect_id, status="ready", duration_s=duration_s)


@router.get(
    "",
    response_model=SoundEffectListResponse,
    dependencies=[Depends(_require_admin)],
)
async def list_sound_effects(
    db: AsyncSession = Depends(get_db),
) -> SoundEffectListResponse:
    """List all sound effects (including unpublished and archived)."""
    count_result = await db.execute(
        select(func.count()).select_from(select(SoundEffect.id).subquery())
    )
    total = count_result.scalar() or 0

    result = await db.execute(select(SoundEffect).order_by(SoundEffect.created_at.desc()))
    effects = result.scalars().all()

    return SoundEffectListResponse(
        effects=[_to_response(e) for e in effects],
        total=total,
    )


@router.get(
    "/{effect_id}",
    response_model=SoundEffectResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_sound_effect(
    effect_id: str,
    db: AsyncSession = Depends(get_db),
) -> SoundEffectResponse:
    effect = await _get_effect_or_404(effect_id, db)
    return _to_response(effect)


@router.patch(
    "/{effect_id}",
    response_model=SoundEffectResponse,
    dependencies=[Depends(_require_admin)],
)
async def update_sound_effect(
    effect_id: str,
    req: UpdateSoundEffectRequest,
    db: AsyncSession = Depends(get_db),
) -> SoundEffectResponse:
    """Rename, publish, unpublish, or archive a sound effect."""
    effect = await _get_effect_or_404(effect_id, db)

    if req.name is not None:
        effect.name = req.name.strip() or effect.name

    now = datetime.datetime.now(UTC)
    if req.publish is True and effect.published_at is None:
        effect.published_at = now
    elif req.publish is False:
        effect.published_at = None

    if req.archive is True and effect.archived_at is None:
        effect.archived_at = now
    elif req.archive is False:
        effect.archived_at = None

    await db.commit()
    await db.refresh(effect)
    log.info("sfx_updated", effect_id=effect_id)
    return _to_response(effect)


@router.get(
    "/{effect_id}/audio-url",
    dependencies=[Depends(_require_admin)],
)
async def get_audio_url(
    effect_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return a short-lived signed URL for the effect's audio file."""
    effect = await _get_effect_or_404(effect_id, db)
    if not effect.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Effect has no audio file yet.",
        )
    import datetime as _dt  # noqa: PLC0415

    from app.storage import _get_client  # noqa: PLC0415

    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(effect.audio_gcs_path)
    url = blob.generate_signed_url(
        version="v4",
        expiration=_dt.timedelta(hours=1),
        method="GET",
    )
    return {"audio_url": url}


@router.delete(
    "/{effect_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_require_admin)],
)
async def archive_sound_effect(
    effect_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-archive a sound effect (hides from picker; existing placements keep working)."""
    effect = await _get_effect_or_404(effect_id, db)
    effect.archived_at = datetime.datetime.now(UTC)
    await db.commit()
    log.info("sfx_archived", effect_id=effect_id)
