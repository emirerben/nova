"""Admin endpoints for managing music tracks.

POST   /admin/music-tracks              — add track from YouTube/SoundCloud URL
POST   /admin/music-tracks/upload       — add track from direct audio file upload
POST   /admin/music-tracks/templated    — create templated track (typed-slot recipe)
GET    /admin/music-tracks              — list all tracks (including unpublished)
GET    /admin/music-tracks/{id}         — full track detail + beat count
PATCH  /admin/music-tracks/{id}         — update config, title, artist, publish/archive
POST   /admin/music-tracks/{id}/reanalyze — re-run beat detection
DELETE /admin/music-tracks/{id}         — soft-archive only

Auth: X-Admin-Token header (same as admin.py).
"""

import asyncio
import hmac
import json
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._schemas.song_sections import SongSection
from app.config import settings
from app.database import get_db
from app.models import MusicTrack
from app.services.audio_download import (
    DownloadError,
    download_audio_and_upload,
    is_supported_audio_url,
)

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
                "Only YouTube (youtube.com, youtu.be) and "
                "SoundCloud (soundcloud.com) URLs are supported."
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
    beat_timestamps_s: list[float] | None
    analysis_status: str
    error_detail: str | None
    thumbnail_url: str | None
    published_at: datetime | None
    archived_at: datetime | None
    track_config: dict | None
    # Output of the song_sections agent — 1-3 ranked edit-worthy windows.
    # `section_version` mirrors CURRENT_SECTION_VERSION so the admin UI can
    # spot stale rows scored under an older prompt version at a glance.
    best_sections: list[SongSection] | None
    section_version: str | None
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
    # Coerce best_sections per-row. SongSection has strict Literal unions; one
    # row with a drifted enum (agent retry, manual psql edit, post-bump stale
    # row) would otherwise raise ValidationError and 500 the entire list
    # endpoint, locking admin out of /admin/music. Bad rows are dropped and
    # logged; the frontend's "no agent sections" placeholder surfaces the gap.
    coerced_sections: list[SongSection] | None = None
    if t.best_sections:
        coerced_sections = []
        for raw in t.best_sections:
            try:
                coerced_sections.append(SongSection.model_validate(raw))
            except Exception as exc:
                log.warning(
                    "invalid_song_section_dropped",
                    track_id=t.id,
                    error=str(exc),
                )
        if not coerced_sections:
            coerced_sections = None
    return MusicTrackResponse(
        id=t.id,
        title=t.title,
        artist=t.artist,
        source_url=t.source_url,
        audio_gcs_path=t.audio_gcs_path,
        duration_s=t.duration_s,
        beat_count=len(beats),
        beat_timestamps_s=beats or None,
        analysis_status=t.analysis_status,
        error_detail=t.error_detail,
        thumbnail_url=t.thumbnail_url,
        published_at=t.published_at,
        archived_at=t.archived_at,
        track_config=t.track_config,
        best_sections=coerced_sections,
        section_version=t.section_version,
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


@router.post(
    "/upload",
    response_model=CreateMusicTrackResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def upload_music_track(
    file: UploadFile = File(...),
    title: str = Form(""),
    artist: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> CreateMusicTrackResponse:
    """Upload an audio file directly (bypasses yt-dlp). Accepts m4a, mp3, wav, ogg, aac."""
    allowed = {
        "audio/mp4",
        "audio/mpeg",
        "audio/wav",
        "audio/ogg",
        "audio/aac",
        "audio/x-m4a",
        "audio/m4a",
        "audio/mp3",
        "audio/x-wav",
        "video/mp4",  # some browsers report m4a as video/mp4
    }
    ct = (file.content_type or "").lower()
    ext = Path(file.filename or "audio.m4a").suffix.lower()
    if ct not in allowed and ext not in {".m4a", ".mp3", ".wav", ".ogg", ".aac", ".mp4"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported audio format: {ct} / {ext}. Use m4a, mp3, wav, ogg, or aac.",
        )

    track_id = str(uuid.uuid4())
    gcs_path = f"music/{track_id}/audio{ext or '.m4a'}"

    # Save upload to temp file, probe duration, upload to GCS
    with tempfile.NamedTemporaryFile(suffix=ext or ".m4a", delete=True) as tmp:
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:  # 50 MB limit
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Audio file too large. Maximum 50 MB.",
            )
        tmp.write(content)
        tmp.flush()

        # Probe duration via ffprobe
        from app.services.audio_download import _probe_duration  # noqa: PLC0415

        duration_s = _probe_duration(tmp.name)

        # Upload to GCS
        from app.storage import _get_client  # noqa: PLC0415

        bucket = _get_client().bucket(settings.storage_bucket)
        blob = bucket.blob(gcs_path)
        blob.upload_from_filename(tmp.name, content_type=ct or "audio/mp4")

    track = MusicTrack(
        id=track_id,
        title=title.strip() or file.filename or f"Track {track_id[:8]}",
        artist=artist.strip(),
        source_url=f"upload://{file.filename}",
        audio_gcs_path=gcs_path,
        duration_s=duration_s,
        analysis_status="queued",
    )
    db.add(track)
    await db.commit()
    await db.refresh(track)

    from app.tasks.music_orchestrate import analyze_music_track_task  # noqa: PLC0415

    analyze_music_track_task.delay(track_id)

    log.info("music_track_uploaded", track_id=track_id, filename=file.filename)
    return CreateMusicTrackResponse(id=track_id, analysis_status="queued")


# ── Templated track create ────────────────────────────────────────────────────


_IMAGE_CT = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def _validate_templated_recipe(recipe: dict) -> tuple[list[dict], list[dict]]:
    """Return (fixed_slots, user_slots). Raise 422 if shape is wrong."""
    if not isinstance(recipe, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recipe must be a JSON object",
        )
    slots = recipe.get("slots")
    if not isinstance(slots, list) or not slots:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recipe.slots must be a non-empty list",
        )
    fixed: list[dict] = []
    user: list[dict] = []
    seen_positions: set[int] = set()
    for slot in slots:
        if not isinstance(slot, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="every recipe slot must be an object",
            )
        position = slot.get("position")
        if not isinstance(position, int) or position < 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="every slot needs an integer position >= 1",
            )
        if position in seen_positions:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"duplicate slot position {position}",
            )
        seen_positions.add(position)
        slot_type = slot.get("slot_type")
        if slot_type == "fixed_asset":
            fixed.append(slot)
        elif slot_type == "user_upload":
            user.append(slot)
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"slot {position} has unsupported slot_type {slot_type!r}; "
                    "expected 'fixed_asset' or 'user_upload'"
                ),
            )
        if not isinstance(slot.get("target_duration_s"), (int, float)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"slot {position} requires numeric target_duration_s",
            )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recipe must contain at least one user_upload slot",
        )
    return fixed, user


@router.post(
    "/templated",
    response_model=CreateMusicTrackResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_templated_music_track(
    audio: UploadFile = File(..., description="Audio file (mp3/m4a/wav)"),
    recipe_json: str = Form(..., description="JSON recipe with typed slots"),
    title: str = Form(""),
    artist: str = Form(""),
    publish: bool = Form(False),
    asset_files: list[UploadFile] = File(
        default_factory=list,
        description="Slot asset files in the same order as recipe.fixed_asset slots",
    ),
    db: AsyncSession = Depends(get_db),
) -> CreateMusicTrackResponse:
    """Create a templated music track in one shot.

    Templated tracks bypass beat detection (the audio is usually a spoken quote
    or a clip whose beat structure is not load-bearing). Instead, the admin
    supplies a recipe with `fixed_asset` and `user_upload` slots; this endpoint
    uploads the audio + each fixed-asset image to GCS, patches the recipe with
    the GCS paths, and stores it on `MusicTrack.recipe_cached` with
    `analysis_status='ready'`.
    """
    try:
        recipe = json.loads(recipe_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"recipe_json is not valid JSON: {exc}",
        )

    fixed_slots, _user_slots = _validate_templated_recipe(recipe)

    if len(asset_files) != len(fixed_slots):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Recipe declares {len(fixed_slots)} fixed_asset slot(s); "
                f"received {len(asset_files)} asset_files. Order must match "
                "ascending slot.position."
            ),
        )

    # Validate audio extension/content-type
    audio_ext = Path(audio.filename or "audio.m4a").suffix.lower()
    if audio_ext not in {".m4a", ".mp3", ".wav", ".ogg", ".aac", ".mp4"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported audio extension {audio_ext!r}",
        )

    track_id = str(uuid.uuid4())
    audio_gcs = f"music/{track_id}/audio{audio_ext}"

    # Upload audio to GCS, probe duration
    with tempfile.NamedTemporaryFile(suffix=audio_ext, delete=True) as tmp:
        content = await audio.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Audio file too large. Maximum 50 MB.",
            )
        tmp.write(content)
        tmp.flush()

        from app.services.audio_download import _probe_duration  # noqa: PLC0415

        duration_s = _probe_duration(tmp.name)

        from app.storage import _get_client  # noqa: PLC0415

        bucket = _get_client().bucket(settings.storage_bucket)
        bucket.blob(audio_gcs).upload_from_filename(
            tmp.name, content_type=audio.content_type or "audio/mp4"
        )

    # Upload each fixed-asset file to GCS, patch the recipe slot
    fixed_sorted = sorted(fixed_slots, key=lambda s: int(s["position"]))
    for slot, asset in zip(fixed_sorted, asset_files):
        position = int(slot["position"])
        ct = (asset.content_type or "").lower()
        ext = Path(asset.filename or "asset").suffix.lower()
        if ct not in _IMAGE_CT and ext not in _IMAGE_EXT:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Slot {position} asset must be an image (got content_type={ct!r}, ext={ext!r})"
                ),
            )
        ext = ext or ".jpg"
        asset_gcs = f"music/{track_id}/assets/slot_{position}{ext}"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
            data = await asset.read()
            if len(data) > 20 * 1024 * 1024:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Slot {position} asset too large. Maximum 20 MB.",
                )
            tmp.write(data)
            tmp.flush()
            from app.storage import _get_client  # noqa: PLC0415

            bucket = _get_client().bucket(settings.storage_bucket)
            bucket.blob(asset_gcs).upload_from_filename(tmp.name, content_type=ct or "image/jpeg")
        slot["asset_gcs_path"] = asset_gcs
        slot.setdefault("asset_kind", "image")

    # Persist track
    now = datetime.now(UTC)
    track = MusicTrack(
        id=track_id,
        title=title.strip() or audio.filename or f"Track {track_id[:8]}",
        artist=artist.strip(),
        source_url=f"templated://{audio.filename or 'audio'}",
        audio_gcs_path=audio_gcs,
        duration_s=duration_s,
        analysis_status="ready",  # bypass beat detection
        recipe_cached=recipe,
        recipe_cached_at=now,
        beat_timestamps_s=[],
        track_config={},
        published_at=now if publish else None,
    )
    db.add(track)
    await db.commit()
    await db.refresh(track)

    log.info(
        "templated_music_track_created",
        track_id=track_id,
        slots=len(recipe.get("slots", [])),
        fixed=len(fixed_sorted),
        published=publish,
    )
    return CreateMusicTrackResponse(id=track_id, analysis_status="ready")


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

    count_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
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


@router.get(
    "/{track_id}/audio-url",
    dependencies=[Depends(_require_admin)],
)
async def get_audio_url(
    track_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return a short-lived signed URL for the track's audio file."""
    track = await _get_track_or_404(track_id, db)
    if not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Track has no audio file yet.",
        )
    from app.storage import _get_client  # noqa: PLC0415

    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(track.audio_gcs_path)
    import datetime as _dt  # noqa: PLC0415

    url = blob.generate_signed_url(
        version="v4",
        expiration=_dt.timedelta(hours=1),
        method="GET",
    )
    return {"audio_url": url}


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
