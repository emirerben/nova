"""Admin endpoints for managing music tracks.

POST   /admin/music-tracks                  — add track from YouTube/SoundCloud URL
POST   /admin/music-tracks/upload           — add track from direct audio file upload
POST   /admin/music-tracks/templated        — create templated track (typed-slot recipe)
GET    /admin/music-tracks                  — list all tracks (including unpublished)
GET    /admin/music-tracks/{id}             — full track detail + beat count
PATCH  /admin/music-tracks/{id}             — update config, title, artist, publish/archive
POST   /admin/music-tracks/{id}/reanalyze   — re-run beat detection
DELETE /admin/music-tracks/{id}             — soft-archive only
POST   /admin/music-tracks/{id}/test-job    — render a beat-sync job (skips publish gates)
POST   /admin/music-tracks/{id}/upload-slot-presigned — mint direct GCS PUT URLs for test clips
POST   /admin/music-tracks/{id}/rerender-job — re-render a prior job's clips against current config
GET    /admin/music-tracks/{id}/test-jobs   — list recent admin test jobs for this track
GET    /admin/music-tracks/{id}/jobs/{job_id}/status — admin-gated status poll

Auth: X-Admin-Token header (same as admin.py).
"""

import asyncio
import hmac
import json
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

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
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.agents._schemas.song_sections import SongSection
from app.config import settings
from app.database import get_db
from app.models import Job, MusicTrack
from app.routes.music_jobs import (
    _SLOT_UPLOAD_MAX_BYTES,
    SYNTHETIC_USER_ID,
    MusicJobResponse,
    MusicJobStatusResponse,
    classify_slot_kind,
    validate_clip_count,
)
from app.schemas.lyrics_config_override import LyricsConfigOverride
from app.services.audio_download import (
    DownloadError,
    download_audio_and_upload,
    is_supported_audio_url,
)
from app.services.lyrics_config_effective import (
    deep_merge_dict,
    effective_lyrics_config,
    non_null_model_dict,
    normalize_lyrics_config,
)
from app.services.lyrics_config_validation import validate_lyrics_config_dict

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

        # Lyrics config is nested under track_config (one JSON column for all
        # per-song admin tuning). Same validator runs at the template-level
        # PATCH endpoint so a stale schema can't sneak in via either path.
        lyrics_cfg = cfg.get("lyrics_config")
        if lyrics_cfg is not None:
            validate_lyrics_config_dict(lyrics_cfg)
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
    # Lyrics fields — see app.agents.lyrics + app.models.MusicTrack for shape.
    # `lyrics_cached` is full per-line + per-word JSON; in list responses we
    # still surface it so the frontend can preview without an extra fetch.
    lyrics_status: str
    lyrics_source: str | None
    lyrics_error_detail: str | None
    lyrics_cached: dict | None
    lyrics_extracted_at: datetime | None
    # Output of the song_sections agent — 1-3 ranked edit-worthy windows.
    # `section_version` mirrors CURRENT_SECTION_VERSION so the admin UI can
    # spot stale rows scored under an older prompt version at a glance.
    best_sections: list[SongSection] | None
    section_version: str | None
    created_at: datetime


class MusicTrackListItem(BaseModel):
    """Strict admin-list projection; keep in sync with list_music_tracks load_only."""

    id: str
    title: str
    artist: str
    analysis_status: str
    thumbnail_url: str | None
    beat_count: int
    published_at: datetime | None
    archived_at: datetime | None
    created_at: datetime


class MusicTrackListResponse(BaseModel):
    tracks: list[MusicTrackListItem]
    total: int


class CreateMusicTrackResponse(BaseModel):
    id: str
    analysis_status: str


class ReanalyzeResponse(BaseModel):
    track_id: str
    analysis_status: str


class LyricsConfigPatchResponse(BaseModel):
    lyrics_config: dict


class LyricsPreviewRequest(BaseModel):
    lyrics_config_override: LyricsConfigOverride | None = None


class LyricsPreviewResponse(BaseModel):
    job_id: str


class LyricsPreviewStatusResponse(BaseModel):
    job_id: str
    status: str
    output_url: str | None = None
    error_detail: str | None = None
    lyrics_config_effective: dict | None = None
    created_at: datetime
    updated_at: datetime


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
        lyrics_status=t.lyrics_status,
        lyrics_source=t.lyrics_source,
        lyrics_error_detail=t.lyrics_error_detail,
        lyrics_cached=t.lyrics_cached,
        lyrics_extracted_at=t.lyrics_extracted_at,
        best_sections=coerced_sections,
        section_version=t.section_version,
        created_at=t.created_at,
    )


def _to_list_item(t: MusicTrack, beat_count: int) -> MusicTrackListItem:
    return MusicTrackListItem(
        id=t.id,
        title=t.title,
        artist=t.artist,
        analysis_status=t.analysis_status,
        thumbnail_url=t.thumbnail_url,
        beat_count=beat_count,
        published_at=t.published_at,
        archived_at=t.archived_at,
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
    beat_count_expr = func.coalesce(
        func.jsonb_array_length(MusicTrack.beat_timestamps_s),
        0,
    ).label("beat_count")
    base_query = select(MusicTrack, beat_count_expr).options(
        load_only(
            MusicTrack.id,
            MusicTrack.title,
            MusicTrack.artist,
            MusicTrack.analysis_status,
            MusicTrack.thumbnail_url,
            MusicTrack.published_at,
            MusicTrack.archived_at,
            MusicTrack.created_at,
        )
    )

    count_result = await db.execute(
        select(func.count()).select_from(select(MusicTrack.id).subquery())
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        base_query.order_by(MusicTrack.created_at.desc()).offset(offset).limit(limit)
    )
    rows = result.all()

    return MusicTrackListResponse(
        tracks=[_to_list_item(t, beat_count) for (t, beat_count) in rows],
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


@router.patch(
    "/{track_id}/lyrics-config",
    response_model=LyricsConfigPatchResponse,
    dependencies=[Depends(_require_admin)],
)
async def update_music_track_lyrics_config(
    track_id: str,
    req: LyricsConfigOverride,
    db: AsyncSession = Depends(get_db),
) -> LyricsConfigPatchResponse:
    """Persist lyric timing defaults without disturbing other track_config keys."""
    track = await _get_track_or_404(track_id, db)
    override = non_null_model_dict(req)
    track_config = dict(track.track_config or {})
    merged = deep_merge_dict(track_config.get("lyrics_config") or {}, override)
    try:
        validate_lyrics_config_dict(merged)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    normalized = normalize_lyrics_config(merged)
    track_config["lyrics_config"] = normalized
    track.track_config = track_config
    await db.commit()
    await db.refresh(track)
    return LyricsConfigPatchResponse(lyrics_config=normalized)


@router.post(
    "/{track_id}/lyrics-preview",
    response_model=LyricsPreviewResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_require_admin)],
)
async def create_admin_lyrics_preview(
    track_id: str,
    req: LyricsPreviewRequest,
    db: AsyncSession = Depends(get_db),
) -> LyricsPreviewResponse:
    track = await _get_track_or_404(track_id, db)
    if track.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Music track analysis is not complete (status: {track.analysis_status}).",
        )
    if not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Music track has no audio file to preview.",
        )
    if not track.lyrics_cached:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Music track has no cached lyrics to preview.",
        )

    override = non_null_model_dict(req.lyrics_config_override)
    try:
        effective = {
            **effective_lyrics_config(track.track_config, override),
            "enabled": True,
            "style": "line",
        }
        validate_lyrics_config_dict(effective)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="lyrics_preview",
        music_track_id=track_id,
        raw_storage_path=track.audio_gcs_path or "",
        selected_platforms=["admin"],
        all_candidates={"lyrics_config_effective": effective},
        assembly_plan={"lyrics_config_effective": effective},
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.services.job_dispatch import enqueue_orchestrator  # noqa: PLC0415
    from app.tasks.lyrics_preview_task import render_lyrics_preview_task  # noqa: PLC0415

    await enqueue_orchestrator(render_lyrics_preview_task, job.id, db)
    return LyricsPreviewResponse(job_id=str(job.id))


@router.get(
    "/{track_id}/lyrics-preview-jobs/{job_id}/status",
    response_model=LyricsPreviewStatusResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_admin_lyrics_preview_status(
    track_id: str,
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> LyricsPreviewStatusResponse:
    await _get_track_or_404(track_id, db)
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.job_type != "lyrics_preview" or job.music_track_id != track_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    plan = job.assembly_plan or {}
    raw_url = plan.get("output_url")
    output_url = (
        raw_url
        if isinstance(raw_url, str) and raw_url.startswith(("http://", "https://"))
        else None
    )
    return LyricsPreviewStatusResponse(
        job_id=str(job.id),
        status=job.status,
        output_url=output_url,
        error_detail=job.error_detail,
        lyrics_config_effective=plan.get("lyrics_config_effective")
        if isinstance(plan.get("lyrics_config_effective"), dict)
        else None,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.post(
    "/{track_id}/extract-lyrics",
    response_model=ReanalyzeResponse,
    dependencies=[Depends(_require_admin)],
)
async def extract_track_lyrics(
    track_id: str,
    db: AsyncSession = Depends(get_db),
) -> ReanalyzeResponse:
    """Re-run lyric extraction without touching beat detection.

    Useful when the admin updates the title/artist (so Genius can find a
    better match) or when the Whisper / Genius services were down at the
    original analyze time.
    """
    track = await _get_track_or_404(track_id, db)

    if not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Track has no audio file — re-upload the track first.",
        )
    if track.lyrics_status == "extracting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lyric extraction is already in progress for this track.",
        )

    track.lyrics_status = "extracting"
    track.lyrics_error_detail = None
    await db.commit()

    from app.tasks.music_orchestrate import extract_track_lyrics_task  # noqa: PLC0415

    extract_track_lyrics_task.delay(track_id)

    log.info("music_track_lyrics_dispatched", track_id=track_id)
    return ReanalyzeResponse(track_id=track_id, analysis_status="extracting")


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


# ── Admin test jobs ───────────────────────────────────────────────────────────
#
# Mirrors POST /music-jobs but lets admins target unpublished / archived tracks
# so a track can be smoke-tested before it appears in the public gallery.
# Still requires analysis_status == "ready" and a non-null audio_gcs_path —
# without those, the orchestrator has nothing to render against.


# Prefixes that the upload endpoints write to. Anything else (raw bucket paths,
# /clips/, processed outputs, internal artifacts) is rejected so an attacker
# can't smuggle an arbitrary object key into the render pipeline and exfiltrate
# it through the signed assembly_plan.output_url.
_ALLOWED_CLIP_PREFIXES = ("music-uploads/", "slot-uploads/")


def _validate_clip_path_prefixes(paths: list[str]) -> list[str]:
    for p in paths:
        if not isinstance(p, str) or ".." in p or p.startswith("/"):
            raise ValueError(f"Invalid clip path: {p!r}")
        if not any(p.startswith(prefix) for prefix in _ALLOWED_CLIP_PREFIXES):
            allowed = ", ".join(_ALLOWED_CLIP_PREFIXES)
            raise ValueError(f"Clip path must start with one of: {allowed}. Got: {p!r}")
    return paths


class SlotPresignItem(BaseModel):
    client_id: str
    filename: str
    content_type: str
    file_size_bytes: int


class SlotPresignRequest(BaseModel):
    files: list[SlotPresignItem]


class SlotPresignSuccess(BaseModel):
    ok: Literal[True] = True
    client_id: str
    filename: str
    upload_url: str
    gcs_path: str
    kind: Literal["video", "image"]
    content_type: str


class SlotPresignError(BaseModel):
    ok: Literal[False] = False
    client_id: str
    filename: str
    error: str


SlotPresignResult = Annotated[
    SlotPresignSuccess | SlotPresignError,
    Field(discriminator="ok"),
]


class SlotPresignResponse(BaseModel):
    batch_id: str
    results: list[SlotPresignResult]


_MAX_SLOT_PRESIGN_FILES_HARD = 25


def _track_slot_upload_cap(track: MusicTrack) -> int:
    recipe = track.recipe_cached or {}
    user_slots = [s for s in recipe.get("slots", []) if s.get("slot_type") == "user_upload"]
    if user_slots:
        return len(user_slots)

    cfg = track.track_config or {}
    for key in ("slot_count", "required_clips_max"):
        raw = cfg.get(key)
        if raw is None:
            continue
        try:
            cap = int(raw)
        except (TypeError, ValueError):
            continue
        if cap > 0:
            return cap
    return _MAX_SLOT_PRESIGN_FILES_HARD


def _sign_slot_put(object_path: str, content_type: str) -> str:
    """Create a signed browser PUT URL using the exact Content-Type to echo back."""
    from app.storage import _get_client  # noqa: PLC0415

    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        method="PUT",
        content_type=content_type,
        expiration=timedelta(minutes=60),
    )


class CreateAdminMusicJobRequest(BaseModel):
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]
    lyrics_config_override: LyricsConfigOverride | None = None

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clips(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("At least 1 clip is required")
        if len(v) > 20:
            raise ValueError("Maximum 20 clips allowed")
        return _validate_clip_path_prefixes(v)

    @field_validator("selected_platforms")
    @classmethod
    def validate_platforms(cls, v: list[str]) -> list[str]:
        valid = {"tiktok", "instagram", "youtube"}
        for p in v:
            if p not in valid:
                raise ValueError(f"Unknown platform: {p}")
        return v


class RerenderMusicJobRequest(BaseModel):
    source_job_id: str
    lyrics_config_override: LyricsConfigOverride | None = None


class AdminMusicJobSummary(BaseModel):
    job_id: str
    status: str
    error_detail: str | None
    output_url: str | None
    clip_count: int
    created_at: datetime
    updated_at: datetime


class AdminMusicJobListResponse(BaseModel):
    jobs: list[AdminMusicJobSummary]


def _require_ready_track_for_admin(track: MusicTrack) -> None:
    """Admin-side gate: skips publish/archive checks, still requires audio + ready analysis."""
    if track.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Music track analysis is not complete (status: {track.analysis_status}).",
        )
    if not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Music track has no audio file — re-analyze the track first.",
        )


@router.post(
    "/{track_id}/upload-slot-presigned",
    response_model=SlotPresignResponse,
    dependencies=[Depends(_require_admin)],
)
async def create_slot_upload_urls(
    track_id: str,
    body: SlotPresignRequest,
    db: AsyncSession = Depends(get_db),
) -> SlotPresignResponse:
    """Mint signed GCS PUT URLs for admin Test tab clip uploads."""
    if not body.files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="files must not be empty",
        )
    if len(body.files) > _MAX_SLOT_PRESIGN_FILES_HARD:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"max {_MAX_SLOT_PRESIGN_FILES_HARD} files per batch",
        )

    track = await _get_track_or_404(track_id, db)
    track_cap = _track_slot_upload_cap(track)
    if len(body.files) > track_cap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"this track expects up to {track_cap} clips per batch (got {len(body.files)})",
        )

    batch_id = uuid.uuid4().hex[:12]
    results: list[SlotPresignResult] = []

    for i, f in enumerate(body.files):
        try:
            kind = classify_slot_kind(f.filename, f.content_type)
            if f.file_size_bytes <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="file_size_bytes must be positive",
                )
            if f.file_size_bytes > _SLOT_UPLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"exceeds 200 MB ({f.file_size_bytes} bytes)",
                )

            ext = Path(f.filename).suffix.lower() or (".mp4" if kind == "video" else ".jpg")
            gcs_path = f"music-uploads/{track_id}/{batch_id}/clip_{i:03d}{ext}"
            content_type = f.content_type or ("video/mp4" if kind == "video" else "image/jpeg")
            upload_url = _sign_slot_put(gcs_path, content_type)
            results.append(
                SlotPresignSuccess(
                    client_id=f.client_id,
                    filename=f.filename,
                    upload_url=upload_url,
                    gcs_path=gcs_path,
                    kind=kind,
                    content_type=content_type,
                )
            )
        except HTTPException as exc:
            results.append(
                SlotPresignError(
                    client_id=f.client_id,
                    filename=f.filename,
                    error=str(exc.detail),
                )
            )
        except Exception as exc:
            log.error(
                "slot_presign_failed",
                track_id=track_id,
                batch_id=batch_id,
                filename=f.filename,
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Upload service unavailable — try again",
            ) from exc

    log.info(
        "slot_presign_created",
        track_id=track_id,
        batch_id=batch_id,
        total=len(body.files),
        ok=sum(1 for r in results if r.ok),
        bad=sum(1 for r in results if not r.ok),
    )
    return SlotPresignResponse(batch_id=batch_id, results=results)


@router.post(
    "/{track_id}/test-job",
    response_model=MusicJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_admin_music_test_job(
    track_id: str,
    req: CreateAdminMusicJobRequest,
    db: AsyncSession = Depends(get_db),
) -> MusicJobResponse:
    """Create a music beat-sync job against any ready track (skips publish gates)."""
    track = await _get_track_or_404(track_id, db)
    _require_ready_track_for_admin(track)
    validate_clip_count(track, len(req.clip_gcs_paths))
    override = non_null_model_dict(req.lyrics_config_override)
    try:
        effective_lyrics_config(track.track_config, override)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="music",
        music_track_id=track_id,
        raw_storage_path=req.clip_gcs_paths[0],
        selected_platforms=req.selected_platforms,
        all_candidates={
            "clip_paths": req.clip_gcs_paths,
            **({"lyrics_config_override": override} if override else {}),
        },
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
        "admin_music_test_job_created",
        job_id=job_id,
        track_id=track_id,
        clips=len(req.clip_gcs_paths),
    )
    return MusicJobResponse(job_id=job_id, status="queued", music_track_id=track_id)


@router.post(
    "/{track_id}/rerender-job",
    response_model=MusicJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_admin_music_rerender_job(
    track_id: str,
    req: RerenderMusicJobRequest,
    db: AsyncSession = Depends(get_db),
) -> MusicJobResponse:
    """Re-render a prior music job's clips against the current track config.

    Beat-sync recipes are regenerated from `track_config` on every run (see
    `_run_music_job` → `generate_music_recipe`), so changing best_start_s /
    slot_every_n_beats on the track and then hitting this endpoint is enough
    to produce a fresh cut. No recipe-cache invalidation needed for the
    beat-sync path; the cache is only load-bearing for templated tracks.
    """
    track = await _get_track_or_404(track_id, db)
    _require_ready_track_for_admin(track)
    override = non_null_model_dict(req.lyrics_config_override)
    try:
        effective_lyrics_config(track.track_config, override)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    try:
        source_uuid = uuid.UUID(req.source_job_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source job not found",
        )

    source_result = await db.execute(select(Job).where(Job.id == source_uuid))
    source = source_result.scalar_one_or_none()
    if source is None or source.job_type != "music" or source.music_track_id != track_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source job not found for this track",
        )

    clip_paths: list[str] = (source.all_candidates or {}).get("clip_paths", [])
    if not clip_paths:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Source job has no clip paths to re-use. Upload clips and create a fresh test job."
            ),
        )

    # Re-apply prefix gating: a source job from a pre-validator era could carry
    # arbitrary GCS paths; we still refuse to re-render anything outside the
    # upload allowlist.
    try:
        _validate_clip_path_prefixes(clip_paths)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Source job clip path is not allowlisted: {exc}",
        )

    validate_clip_count(track, len(clip_paths))

    new_job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="music",
        music_track_id=track_id,
        raw_storage_path=clip_paths[0],
        selected_platforms=source.selected_platforms or ["tiktok", "instagram", "youtube"],
        all_candidates={
            "clip_paths": clip_paths,
            **({"lyrics_config_override": override} if override else {}),
        },
        status="queued",
    )
    db.add(new_job)
    await db.commit()
    await db.refresh(new_job)

    job_id = str(new_job.id)

    from app.services.job_dispatch import enqueue_orchestrator  # noqa: PLC0415
    from app.tasks.music_orchestrate import orchestrate_music_job  # noqa: PLC0415

    await enqueue_orchestrator(orchestrate_music_job, new_job.id, db)

    log.info(
        "admin_music_rerender_job_created",
        job_id=job_id,
        track_id=track_id,
        source_job_id=req.source_job_id,
        clips=len(clip_paths),
    )
    return MusicJobResponse(job_id=job_id, status="queued", music_track_id=track_id)


@router.get(
    "/{track_id}/jobs/{job_id}/status",
    response_model=MusicJobStatusResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_admin_music_job_status(
    track_id: str,
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> MusicJobStatusResponse:
    """Admin-gated status poll for a music job belonging to this track.

    The public GET /music-jobs/{id}/status has no auth — admin-created job IDs
    leaked through it would expose status + signed output URLs to anyone with
    the UUID. This endpoint requires the admin token and scopes the lookup to
    the track, so a stray admin job_id can't be mixed with public jobs.
    """
    await _get_track_or_404(track_id, db)

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.job_type != "music" or job.music_track_id != track_id:
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


@router.get(
    "/{track_id}/test-jobs",
    response_model=AdminMusicJobListResponse,
    dependencies=[Depends(_require_admin)],
)
async def list_admin_music_test_jobs(
    track_id: str,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=10, ge=1, le=50),
) -> AdminMusicJobListResponse:
    """List recent music jobs against this track (admin testing history)."""
    await _get_track_or_404(track_id, db)

    result = await db.execute(
        select(Job)
        .where(Job.music_track_id == track_id)
        .where(Job.job_type == "music")
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    jobs = result.scalars().all()

    summaries: list[AdminMusicJobSummary] = []
    for j in jobs:
        plan = j.assembly_plan or {}
        clip_paths = (j.all_candidates or {}).get("clip_paths", [])
        # Pre-fix rows stored a relative GCS path here instead of a signed URL.
        # If we passed that through, the admin UI would render a broken
        # `<video src="music-jobs/.../output.mp4">`. Only forward http(s) URLs;
        # legacy rows show up in the list with output_url=null so the UI
        # surfaces them as "rerender to view" rather than a dead link.
        raw_url = plan.get("output_url")
        is_http = isinstance(raw_url, str) and raw_url.startswith(("http://", "https://"))
        safe_url = raw_url if is_http else None
        summaries.append(
            AdminMusicJobSummary(
                job_id=str(j.id),
                status=j.status,
                error_detail=j.error_detail,
                output_url=safe_url,
                clip_count=len(clip_paths),
                created_at=j.created_at,
                updated_at=j.updated_at,
            )
        )

    return AdminMusicJobListResponse(jobs=summaries)
