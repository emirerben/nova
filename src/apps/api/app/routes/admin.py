"""Admin endpoints for managing video templates.

POST   /admin/templates                     — register a curated TikTok as a template
GET    /admin/templates                     — list all templates (paginated)
GET    /admin/templates/:id                 — check template analysis status
PATCH  /admin/templates/:id                 — update metadata / publish / archive
POST   /admin/templates/:id/reanalyze       — re-run Gemini analysis
POST   /admin/templates/:id/test-job        — create a test job (SYNTHETIC_USER_ID)
GET    /admin/templates/:id/metrics          — usage stats
GET    /admin/templates/:id/recipe-history   — paginated recipe version list
POST   /admin/upload-presigned               — presigned URL for templates/ prefix

Auth: X-Admin-Token header (static key from settings.admin_api_key).
"""

import hmac
import re
import uuid
from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Job, MusicTrack, TemplateRecipeVersion, VideoTemplate
from app.services.template_validation import (
    get_template_or_404,
    require_ready,
    validate_clip_count,
)

log = structlog.get_logger()

router = APIRouter()

# Synthetic user for admin test jobs (same as template_jobs.py)
SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# ── Auth dependency ────────────────────────────────────────────────────────────


def _require_admin(x_admin_token: str = Header(...)) -> None:
    """FastAPI dependency: validates X-Admin-Token header."""
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


# ── Request / Response schemas ─────────────────────────────────────────────────


class CreateTemplateRequest(BaseModel):
    name: str
    gcs_path: str
    required_clips_min: int = 5
    required_clips_max: int = 10
    description: str | None = None
    source_url: str | None = None

    @field_validator("gcs_path")
    @classmethod
    def validate_gcs_path(cls, v: str) -> str:
        if not v.startswith("templates/"):
            raise ValueError("gcs_path must start with 'templates/'")
        return v

    @field_validator("required_clips_min")
    @classmethod
    def validate_min(cls, v: int) -> int:
        if v < 1:
            raise ValueError("required_clips_min must be ≥ 1")
        return v

    @field_validator("required_clips_max")
    @classmethod
    def validate_max(cls, v: int) -> int:
        if v > 30:
            raise ValueError("required_clips_max must be ≤ 30")
        return v


class CreateTemplateFromUrlRequest(BaseModel):
    name: str
    url: str
    required_clips_min: int = 5
    required_clips_max: int = 10
    description: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        from app.services.url_download import is_supported_url  # noqa: PLC0415

        if not is_supported_url(v):
            raise ValueError(
                "URL must be a TikTok, Instagram, or YouTube link"
            )
        return v.strip()

    @field_validator("required_clips_min")
    @classmethod
    def validate_min(cls, v: int) -> int:
        if v < 1:
            raise ValueError("required_clips_min must be ≥ 1")
        return v

    @field_validator("required_clips_max")
    @classmethod
    def validate_max(cls, v: int) -> int:
        if v > 30:
            raise ValueError("required_clips_max must be ≤ 30")
        return v


class TemplateResponse(BaseModel):
    id: str
    name: str
    gcs_path: str | None
    analysis_status: str
    required_clips_min: int
    required_clips_max: int
    published_at: datetime | None
    archived_at: datetime | None
    description: str | None
    source_url: str | None
    thumbnail_gcs_path: str | None
    error_detail: str | None = None
    template_type: str = "standard"
    parent_template_id: str | None = None
    music_track_id: str | None = None
    created_at: datetime


class UpdateTemplateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    source_url: str | None = None
    required_clips_min: int | None = None
    required_clips_max: int | None = None
    publish: bool | None = None   # set True to publish (sets published_at)
    archive: bool | None = None   # set True to archive (sets archived_at)
    template_type: str | None = None  # "standard" | "music_parent"

    @field_validator("template_type")
    @classmethod
    def validate_template_type(cls, v: str | None) -> str | None:
        if v is not None and v not in ("standard", "music_parent"):
            raise ValueError("template_type must be 'standard' or 'music_parent'")
        return v


class TemplateListItem(BaseModel):
    id: str
    name: str
    analysis_status: str
    published_at: datetime | None
    archived_at: datetime | None
    description: str | None
    thumbnail_gcs_path: str | None
    error_detail: str | None = None
    template_type: str = "standard"
    job_count: int
    created_at: datetime


class TemplateListResponse(BaseModel):
    templates: list[TemplateListItem]
    total: int


class TemplateMetricsResponse(BaseModel):
    template_id: str
    total_jobs: int
    successful_jobs: int
    failed_jobs: int
    last_job_at: datetime | None
    # Bucketed counts of Job.failure_reason for failed jobs. Empty dict when
    # there are no failures, or when all failures predate the failure_reason
    # column (NULL counts are excluded).
    failure_reasons: dict[str, int] = {}


class TemplateAssetHealth(BaseModel):
    """Health of one GCS asset referenced by a template."""

    role: str  # "reference_video" | "audio" | "voiceover"
    gcs_path: str | None
    exists: bool


class TemplateHealthResponse(BaseModel):
    template_id: str
    template_kind: str
    healthy: bool  # True iff all required assets exist
    assets: list[TemplateAssetHealth]


class TestJobRequest(BaseModel):
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]
    subject: str = ""

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clip_count(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("At least 1 clip is required")
        if len(v) > 20:
            raise ValueError("Maximum 20 clips allowed")
        return v


class TestJobResponse(BaseModel):
    job_id: str
    status: str
    template_id: str


class PresignedUploadRequest(BaseModel):
    filename: str
    content_type: str = "video/mp4"

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str) -> str:
        allowed = {"video/mp4", "video/quicktime", "video/webm"}
        if v not in allowed:
            raise ValueError(f"content_type must be one of {allowed}")
        return v


class PresignedUploadResponse(BaseModel):
    upload_url: str
    gcs_path: str


class RecipeVersionItem(BaseModel):
    id: str
    trigger: str
    created_at: datetime
    slot_count: int
    total_duration_s: float


class RecipeHistoryResponse(BaseModel):
    versions: list[RecipeVersionItem]
    total: int


# ── Recipe editor schemas (strict validation) ────────────────────────────────

TransitionIn = Literal[
    "hard-cut", "whip-pan", "zoom-in", "dissolve", "curtain-close", "none"
]
ColorHint = Literal[
    "warm", "cool", "high-contrast", "desaturated", "vintage", "none"
]
SlotType = Literal["hook", "broll", "outro"]
MediaType = Literal["video", "photo"]
OverlayEffect = Literal[
    "pop-in", "fade-in", "scale-up", "font-cycle", "typewriter",
    "glitch", "bounce", "slide-in", "slide-up", "static", "none",
    "player-card",  # giant kit number + italic red name overlay
]
OverlayPosition = Literal["top", "center", "center-above", "center-label", "center-below", "bottom"]
FontStyle = Literal["display", "sans", "serif", "serif_italic", "script"]
TextSize = Literal["small", "medium", "large", "xlarge", "xxlarge", "jumbo"]
OverlayRole = Literal["hook", "reaction", "cta", "label"]
SyncStyle = Literal[
    "cut-on-beat", "transition-on-beat", "energy-match", "freeform"
]
InterstitialType = Literal["curtain-close", "fade-black-hold", "flash-white"]


class TextSpanSchema(BaseModel):
    text: str
    font_family: str | None = None
    text_color: str | None = None
    text_size: TextSize | None = None

    @field_validator("text_color")
    @classmethod
    def validate_hex_color(cls, v: str | None) -> str | None:
        import re  # noqa: PLC0415

        if v is None:
            return v
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError(f"span text_color must be a hex color (#RRGGBB), got '{v}'")
        return v.upper()


class RecipeTextOverlaySchema(BaseModel):
    role: OverlayRole
    text: str = ""
    position: OverlayPosition = "center"
    effect: OverlayEffect = "none"
    font_style: FontStyle = "sans"
    font_family: str | None = None  # Overrides font_style when set (real font name from registry)
    text_size: TextSize = "medium"
    text_size_px: int | None = None  # Exact pixel override (takes priority over text_size name)
    text_color: str = "#FFFFFF"
    start_s: float = 0.0
    end_s: float = 1.0
    start_s_override: float | None = None
    end_s_override: float | None = None
    has_darkening: bool = False
    has_narrowing: bool = False
    sample_text: str = ""
    font_cycle_accel_at_s: float | None = None
    position_y_frac: float | None = None
    stroke_width: int = 0  # 0 = no outline; 3-5 = TikTok-style black outline
    emoji_prefix: str = ""  # e.g. "🗣️" — Twemoji PNG composited left of first line
    spans: list[TextSpanSchema] | None = None
    outline_px: int | None = None  # Black outline thickness in pixels (for legibility)
    # Subject substitution opt-in. When set, the renderer replaces this
    # overlay's text with a slice of the user's `inputs.location` value:
    # "first_half"/"second_half" split at midpoint (ceil), "full" replaces
    # entirely. Casing is matched to sample_text. Lets one user input drive
    # multiple staggered overlays (e.g. "lon"+"don" → "par"+"is" for "Paris").
    subject_part: Literal["first_half", "second_half", "full"] | None = None
    # Typewriter/embedded substitution. Format string with `{subject}` slot
    # (e.g. "that one trip to {subject}"). Renderer substitutes the user's
    # location into the slot, optionally sliced to the first `subject_chars`
    # characters for a partial-reveal beat in a typewriter sequence.
    subject_template: str | None = None
    subject_chars: int | None = None
    # Player-card overlay fields (consumed when effect == "player-card").
    # Both must be non-empty for the overlay to render.
    jersey_no: str | None = None
    player_name: str | None = None

    @field_validator("text_color")
    @classmethod
    def validate_hex_color(cls, v: str) -> str:
        import re  # noqa: PLC0415

        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError(f"text_color must be a hex color (#RRGGBB), got '{v}'")
        return v.upper()

    @model_validator(mode="after")
    def validate_timing(self) -> "RecipeTextOverlaySchema":
        if self.end_s <= self.start_s:
            raise ValueError(
                f"Overlay end_s ({self.end_s}) must be > start_s ({self.start_s})"
            )
        return self


class RecipeInterstitialSchema(BaseModel):
    type: InterstitialType
    after_slot: int
    hold_s: float
    hold_color: str = "#000000"
    animate_s: float = 0.0

    @field_validator("hold_color")
    @classmethod
    def validate_hex_color(cls, v: str) -> str:
        import re  # noqa: PLC0415

        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError(f"hold_color must be a hex color (#RRGGBB), got '{v}'")
        return v.upper()

    @field_validator("hold_s")
    @classmethod
    def validate_hold(cls, v: float) -> float:
        if v < 0:
            raise ValueError("hold_s must be >= 0")
        return v

    @field_validator("after_slot")
    @classmethod
    def validate_after_slot(cls, v: int) -> int:
        if v < 1:
            raise ValueError("after_slot must be >= 1")
        return v

    @field_validator("animate_s")
    @classmethod
    def validate_animate(cls, v: float) -> float:
        if v < 0:
            raise ValueError("animate_s must be >= 0")
        return v


class RecipeSlotSchema(BaseModel):
    position: int
    target_duration_s: float
    priority: int = 5
    slot_type: SlotType
    transition_in: TransitionIn = "hard-cut"
    color_hint: ColorHint = "none"
    speed_factor: float = 1.0
    energy: float = 5.0
    media_type: MediaType = "video"
    text_overlays: list[RecipeTextOverlaySchema] = []
    # Lock this slot to a fixed range of the original template video instead
    # of filling it with a user clip (e.g. Morocco's "This is AFRICA" hook).
    locked: bool = False
    source_start_s: float | None = None
    source_end_s: float | None = None
    # Rule-of-thirds grid overlay (drawn after scale/crop, before text overlays)
    has_grid: bool = False
    grid_color: str = "#FFFFFF"
    grid_opacity: float = 0.6
    grid_thickness: int = 3
    # Per-intersection highlight on top of the base grid. The highlight picks
    # ONE vertical + ONE horizontal line forming an "L" pointing at the chosen
    # rule-of-thirds intersection corner where the subject sits, and renders
    # those two lines in highlight_color during the given windows.
    # intersection options:
    #   "top-left"     -> left-vertical + top-horizontal
    #   "top-right"    -> right-vertical + top-horizontal
    #   "bottom-left"  -> left-vertical + bottom-horizontal
    #   "bottom-right" -> right-vertical + bottom-horizontal
    grid_highlight_intersection: str | None = None
    grid_highlight_color: str = "#D9435A"
    grid_highlight_windows: list[tuple[float, float]] | None = None

    @field_validator("target_duration_s")
    @classmethod
    def validate_duration(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("target_duration_s must be positive")
        return v

    @field_validator("position")
    @classmethod
    def validate_position(cls, v: int) -> int:
        if v < 1:
            raise ValueError("position must be >= 1")
        return v

    @field_validator("speed_factor")
    @classmethod
    def validate_speed(cls, v: float) -> float:
        if v <= 0 or v > 10:
            raise ValueError("speed_factor must be between 0 (exclusive) and 10")
        return v

    @field_validator("energy")
    @classmethod
    def validate_energy(cls, v: float) -> float:
        if v < 0 or v > 10:
            raise ValueError("energy must be between 0 and 10")
        return v

    @field_validator("grid_opacity")
    @classmethod
    def validate_grid_opacity(cls, v: float) -> float:
        if v < 0 or v > 1:
            raise ValueError("grid_opacity must be between 0 and 1")
        return v

    @field_validator("grid_thickness")
    @classmethod
    def validate_grid_thickness(cls, v: int) -> int:
        if v < 1 or v > 20:
            raise ValueError("grid_thickness must be between 1 and 20")
        return v

    @field_validator("grid_color")
    @classmethod
    def validate_grid_color(cls, v: str) -> str:
        # Strict 6-digit hex only. RGBA (#RRGGBBAA) is rejected because
        # the rendering code already specifies alpha via @opacity — combining
        # both produces an invalid FFmpeg filter.
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError("grid_color must be a 6-digit hex color like #FFFFFF")
        return v

    @field_validator("grid_highlight_color")
    @classmethod
    def validate_grid_highlight_color(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError(
                "grid_highlight_color must be a 6-digit hex color like #D9435A",
            )
        return v

    @field_validator("grid_highlight_intersection")
    @classmethod
    def validate_grid_highlight_intersection(cls, v: str | None) -> str | None:
        if v is None:
            return None
        valid = {"top-left", "top-right", "bottom-left", "bottom-right"}
        if v not in valid:
            raise ValueError(
                f"grid_highlight_intersection must be one of {sorted(valid)}",
            )
        return v

    @field_validator("grid_highlight_windows")
    @classmethod
    def validate_grid_highlight_windows(
        cls, v: list[tuple[float, float]] | None,
    ) -> list[tuple[float, float]] | None:
        if v is None:
            return None
        for start, end in v:
            if start < 0 or end <= start:
                raise ValueError(
                    "grid_highlight_windows entries must be (start, end) with 0 <= start < end",
                )
        return v


class RecipeSchema(BaseModel):
    """Full recipe structure — used for PUT validation."""
    shot_count: int
    total_duration_s: float
    hook_duration_s: float = 0.0
    slots: list[RecipeSlotSchema]
    copy_tone: str = ""
    caption_style: str = ""
    beat_timestamps_s: list[float] = []
    creative_direction: str = ""
    transition_style: str = ""
    color_grade: ColorHint = "none"
    pacing_style: str = ""
    sync_style: SyncStyle = "freeform"
    interstitials: list[RecipeInterstitialSchema] = []
    # Snappy-pacing floor — consolidate won't merge below this when set.
    min_slots: int = 0
    # Render-side controls (admin-tunable per template).
    # output_fit: "crop" (center-crop sides on 16:9 source — default),
    #   "letterbox" / "letterbox_blur" (preserve full frame, blurred bg),
    #   "letterbox_black" (preserve full frame, black bars).
    output_fit: Literal["crop", "letterbox", "letterbox_blur", "letterbox_black"] = "crop"
    clip_filter_hint: str = ""          # natural-language Gemini bias for best_moments

    @field_validator("slots")
    @classmethod
    def validate_slots_nonempty(cls, v: list[RecipeSlotSchema]) -> list[RecipeSlotSchema]:
        if len(v) == 0:
            raise ValueError("Recipe must have at least one slot")
        return v

    @field_validator("total_duration_s")
    @classmethod
    def validate_total_duration(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("total_duration_s must be positive")
        return v


class RerenderJobRequest(BaseModel):
    source_job_id: str

    @field_validator("source_job_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError("source_job_id must be a valid UUID") from exc
        return v


class LatestTestJobResponse(BaseModel):
    job_id: str
    output_url: str | None
    base_output_url: str | None = None
    clip_paths: list[str]
    has_rerender_data: bool
    created_at: datetime


class RecipeResponse(BaseModel):
    recipe: dict
    version_id: str
    version_number: int


class SaveRecipeRequest(BaseModel):
    recipe: RecipeSchema
    base_version_id: str | None = None


# ── Helper ─────────────────────────────────────────────────────────────────────


def _template_response(t: VideoTemplate) -> TemplateResponse:
    return TemplateResponse(
        id=t.id,
        name=t.name,
        gcs_path=t.gcs_path,
        analysis_status=t.analysis_status,
        required_clips_min=t.required_clips_min,
        required_clips_max=t.required_clips_max,
        published_at=t.published_at,
        archived_at=t.archived_at,
        description=t.description,
        source_url=t.source_url,
        thumbnail_gcs_path=t.thumbnail_gcs_path,
        error_detail=t.error_detail,
        template_type=t.template_type,
        parent_template_id=t.parent_template_id,
        music_track_id=t.music_track_id,
        created_at=t.created_at,
    )


# ── Template CRUD endpoints ───────────────────────────────────────────────────


@router.get(
    "/templates",
    response_model=TemplateListResponse,
    dependencies=[Depends(_require_admin)],
)
async def list_templates(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    exclude_children: bool = Query(default=True),
) -> TemplateListResponse:
    """List all templates with job counts (admin view, includes unpublished).

    By default, music_child templates are hidden (they appear under their parent's
    Music tab). Pass exclude_children=false to include them.
    """
    # Subquery for job counts per template
    job_count_sq = (
        select(
            Job.template_id,
            func.count(Job.id).label("job_count"),
        )
        .where(Job.template_id.isnot(None))
        .group_by(Job.template_id)
        .subquery()
    )

    base_filter = select(VideoTemplate)
    if exclude_children:
        base_filter = base_filter.where(VideoTemplate.template_type != "music_child")

    query = (
        select(VideoTemplate, func.coalesce(job_count_sq.c.job_count, 0).label("job_count"))
        .outerjoin(job_count_sq, VideoTemplate.id == job_count_sq.c.template_id)
        .order_by(VideoTemplate.created_at.desc())
    )
    if exclude_children:
        query = query.where(VideoTemplate.template_type != "music_child")

    # Total count
    count_query = select(func.count()).select_from(base_filter.subquery())
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # Fetch page
    result = await db.execute(query.offset(offset).limit(limit))
    rows = result.all()

    return TemplateListResponse(
        templates=[
            TemplateListItem(
                id=t.id,
                name=t.name,
                analysis_status=t.analysis_status,
                published_at=t.published_at,
                archived_at=t.archived_at,
                description=t.description,
                thumbnail_gcs_path=t.thumbnail_gcs_path,
                error_detail=t.error_detail,
                template_type=t.template_type,
                job_count=job_count,
                created_at=t.created_at,
            )
            for t, job_count in rows
        ],
        total=total,
    )


@router.post(
    "/templates",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_template(
    req: CreateTemplateRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Register a curated TikTok as a template and enqueue analysis."""
    from app.storage import object_exists  # noqa: PLC0415

    if not object_exists(req.gcs_path):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"GCS object not found: {req.gcs_path}",
        )

    template_id = str(uuid.uuid4())
    template = VideoTemplate(
        id=template_id,
        name=req.name,
        gcs_path=req.gcs_path,
        analysis_status="analyzing",
        required_clips_min=req.required_clips_min,
        required_clips_max=req.required_clips_max,
        description=req.description,
        source_url=req.source_url,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415
    analyze_template_task.delay(template_id)

    log.info("template_created", template_id=template_id, name=req.name)
    return _template_response(template)


@router.post(
    "/templates/from-url",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_template_from_url(
    req: CreateTemplateFromUrlRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Download a video from a URL (TikTok, IG, YT) and create a template from it."""
    from app.services.url_download import DownloadError, download_and_upload  # noqa: PLC0415

    try:
        gcs_path = download_and_upload(req.url)
    except DownloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    template_id = str(uuid.uuid4())
    template = VideoTemplate(
        id=template_id,
        name=req.name,
        gcs_path=gcs_path,
        analysis_status="analyzing",
        required_clips_min=req.required_clips_min,
        required_clips_max=req.required_clips_max,
        description=req.description,
        source_url=req.url,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415
    analyze_template_task.delay(template_id)

    log.info("template_created_from_url", template_id=template_id, url=req.url)
    return _template_response(template)


@router.get(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Get template status and metadata."""
    template = await get_template_or_404(template_id, db)
    return _template_response(template)


@router.patch(
    "/templates/{template_id}",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def update_template(
    template_id: str,
    req: UpdateTemplateRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Update template metadata, publish, or archive."""
    if req.publish and req.archive:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot publish and archive in the same request",
        )

    template = await get_template_or_404(template_id, db)

    if req.name is not None:
        template.name = req.name
    if req.description is not None:
        template.description = req.description
    if req.source_url is not None:
        template.source_url = req.source_url
    if req.required_clips_min is not None:
        template.required_clips_min = req.required_clips_min
    if req.required_clips_max is not None:
        template.required_clips_max = req.required_clips_max

    # Validate min <= max after applying partial updates
    if template.required_clips_min > template.required_clips_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"required_clips_min ({template.required_clips_min}) "
                f"must be <= required_clips_max ({template.required_clips_max})"
            ),
        )

    # Handle template_type transitions
    if req.template_type is not None and req.template_type != template.template_type:
        if template.template_type == "music_child":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot change template_type of a music_child template",
            )
        if req.template_type == "standard" and template.template_type == "music_parent":
            # Check for existing children
            child_count = await db.execute(
                select(func.count()).select_from(
                    select(VideoTemplate)
                    .where(VideoTemplate.parent_template_id == template_id)
                    .subquery()
                )
            )
            if (child_count.scalar() or 0) > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot switch to standard — template has music "
                        "children. Delete them first."
                    ),
                )
        if req.template_type == "music_parent" and template.parent_template_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot make a child template into a music_parent",
            )
        template.template_type = req.template_type

    if req.publish:
        if template.analysis_status != "ready":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot publish a template that is not ready",
            )
        template.published_at = datetime.now(UTC)
        template.archived_at = None  # unarchive if re-publishing
        log.info("template_published", template_id=template_id)

    if req.archive:
        template.archived_at = datetime.now(UTC)
        log.info("template_archived", template_id=template_id)

    await db.commit()
    await db.refresh(template)
    return _template_response(template)


@router.post(
    "/templates/{template_id}/reanalyze",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def reanalyze_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Re-run Gemini analysis on an existing template."""
    template = await get_template_or_404(template_id, db)

    template.analysis_status = "analyzing"
    template.error_detail = None  # clear stale error
    await db.commit()
    await db.refresh(template)

    # Clear requeue guard counter so manual reanalysis gets fresh attempts
    import redis as redis_lib  # noqa: PLC0415

    _redis = redis_lib.from_url(settings.redis_url)
    _redis.delete(f"analyze_attempts:{template_id}")
    _redis.close()

    from app.tasks.template_orchestrate import analyze_template_task  # noqa: PLC0415
    analyze_template_task.delay(template_id)

    log.info("template_reanalyzed", template_id=template_id)
    return _template_response(template)


# ── Test job endpoint ──────────────────────────────────────────────────────────


@router.post(
    "/templates/{template_id}/test-job",
    response_model=TestJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_test_job(
    template_id: str,
    req: TestJobRequest,
    db: AsyncSession = Depends(get_db),
) -> TestJobResponse:
    """Create a test job for a template using SYNTHETIC_USER_ID."""
    template = await get_template_or_404(template_id, db)
    require_ready(template)
    validate_clip_count(template, len(req.clip_gcs_paths))

    job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="template",
        template_id=template_id,
        raw_storage_path=req.clip_gcs_paths[0],
        selected_platforms=req.selected_platforms,
        all_candidates={"clip_paths": req.clip_gcs_paths, "subject": req.subject},
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    job_id = str(job.id)

    from app.tasks.template_orchestrate import orchestrate_template_job  # noqa: PLC0415
    orchestrate_template_job.delay(job_id)

    log.info("test_job_created", job_id=job_id, template_id=template_id)
    return TestJobResponse(job_id=job_id, status="queued", template_id=template_id)


# ── Re-render endpoint ─────────────────────────────────────────────────────


@router.post(
    "/templates/{template_id}/rerender-job",
    response_model=TestJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_rerender_job(
    template_id: str,
    req: RerenderJobRequest,
    db: AsyncSession = Depends(get_db),
) -> TestJobResponse:
    """Re-render a template with locked clip assignments from a previous job.

    Skips Gemini analysis and clip matching. Uses the current recipe (with
    user edits) but keeps the same clips in the same slots.
    """
    template = await get_template_or_404(template_id, db)
    require_ready(template)

    if not template.recipe_cached:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Template has no recipe",
        )

    # Load source job
    source_job = await db.get(Job, uuid.UUID(req.source_job_id))
    if source_job is None or source_job.template_id != template_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source job not found for this template",
        )
    if source_job.status != "template_ready":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Source job is not in template_ready status",
        )

    # Validate assembly plan has steps with clip_gcs_path
    source_plan = source_job.assembly_plan or {}
    source_steps = source_plan.get("steps", [])
    if not source_steps or not all(s.get("clip_gcs_path") for s in source_steps):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Source job does not have re-render data (missing clip_gcs_path in steps)",
        )

    # Validate slot count matches current recipe
    current_slots = template.recipe_cached.get("slots", [])
    if len(current_slots) != len(source_steps):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Slot count changed ({len(current_slots)} slots in recipe vs "
                f"{len(source_steps)} steps in source job). Use full pipeline instead."
            ),
        )

    # Build new steps: replace slot data with current recipe, keep clip assignments
    current_slots_sorted = sorted(current_slots, key=lambda s: s.get("position", 0))
    new_steps = [
        {
            "slot": current_slots_sorted[i],
            "clip_id": step["clip_id"],
            "clip_gcs_path": step["clip_gcs_path"],
            "moment": step["moment"],
        }
        for i, step in enumerate(source_steps)
    ]

    # Create job with locked assembly plan
    job = Job(
        user_id=SYNTHETIC_USER_ID,
        job_type="template",
        template_id=template_id,
        raw_storage_path=source_steps[0].get("clip_gcs_path", ""),
        selected_platforms=source_job.selected_platforms or ["tiktok", "instagram", "youtube"],
        all_candidates=source_job.all_candidates,
        assembly_plan={"steps": new_steps, "locked": True},
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    job_id = str(job.id)

    from app.tasks.template_orchestrate import orchestrate_template_job  # noqa: PLC0415
    orchestrate_template_job.delay(job_id)

    log.info("rerender_job_created", job_id=job_id, template_id=template_id,
             source_job_id=req.source_job_id)
    return TestJobResponse(job_id=job_id, status="queued", template_id=template_id)


# ── Metrics endpoint ───────────────────────────────────────────────────────────


@router.get(
    "/templates/{template_id}/metrics",
    response_model=TemplateMetricsResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_template_metrics(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateMetricsResponse:
    """Aggregate job stats for a template (single query, not N+1)."""
    await get_template_or_404(template_id, db)

    result = await db.execute(
        select(
            func.count(Job.id).label("total"),
            func.count(Job.id).filter(Job.status == "template_ready").label("successful"),
            func.count(Job.id).filter(Job.status == "processing_failed").label("failed"),
            func.max(Job.created_at).label("last_job_at"),
        ).where(Job.template_id == template_id)
    )
    row = result.one()

    # Group failed jobs by failure_reason so the admin UI can spot patterns
    # ("12 jobs failed in the last week, all with `template_assets_missing`").
    breakdown_result = await db.execute(
        select(Job.failure_reason, func.count(Job.id))
        .where(
            Job.template_id == template_id,
            Job.status == "processing_failed",
            Job.failure_reason.is_not(None),
        )
        .group_by(Job.failure_reason)
    )
    failure_reasons = {reason: count for reason, count in breakdown_result.all()}

    return TemplateMetricsResponse(
        template_id=template_id,
        total_jobs=row.total,
        successful_jobs=row.successful,
        failed_jobs=row.failed,
        last_job_at=row.last_job_at,
        failure_reasons=failure_reasons,
    )


# ── Asset health endpoint ──────────────────────────────────────────────────────


@router.get(
    "/templates/{template_id}/health",
    response_model=TemplateHealthResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_template_health(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> TemplateHealthResponse:
    """GCS-stat each asset referenced by the template.

    Surfaces "music asset not uploaded to prod bucket" *before* a single
    user job runs. Cheap (~1 GCS HEAD per asset) so the admin UI can call
    it on template-page open. The admin UI is also free to surface a
    badge that turns red the moment any asset is missing.
    """
    template = await get_template_or_404(template_id, db)
    from app.storage import object_exists  # noqa: PLC0415

    template_kind = "multiple_videos"
    if isinstance(template.recipe_cached, dict):
        template_kind = template.recipe_cached.get("template_kind", "multiple_videos")

    asset_specs: list[tuple[str, str | None]] = [
        ("reference_video", template.gcs_path),
        ("audio", template.audio_gcs_path),
        ("voiceover", template.voiceover_gcs_path),
    ]
    assets: list[TemplateAssetHealth] = []
    healthy = True
    for role, path in asset_specs:
        if not path:
            # No path means the template doesn't reference an asset of this
            # role. That's only a problem for required assets — currently
            # only `reference_video` is required for every template kind.
            assets.append(
                TemplateAssetHealth(role=role, gcs_path=None, exists=False)
            )
            if role == "reference_video":
                healthy = False
            continue
        try:
            exists = object_exists(path)
        except Exception as exc:
            log.warning(
                "template_health_gcs_stat_failed",
                template_id=template_id,
                role=role,
                gcs_path=path,
                error=str(exc),
            )
            exists = False
        assets.append(TemplateAssetHealth(role=role, gcs_path=path, exists=exists))
        if not exists and role in ("reference_video", "audio"):
            # `audio` is required for music-bearing templates. Flagging it
            # here tells the admin "this template will always render with
            # silent body" before any user discovers it the hard way.
            healthy = False

    return TemplateHealthResponse(
        template_id=template_id,
        template_kind=template_kind,
        healthy=healthy,
        assets=assets,
    )


# ── Latest test job endpoint ───────────────────────────────────────────────────


@router.get(
    "/templates/{template_id}/latest-test-job",
    response_model=LatestTestJobResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_latest_test_job(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> LatestTestJobResponse:
    """Return the most recent completed test job for a template."""
    await get_template_or_404(template_id, db)

    result = await db.execute(
        select(Job)
        .where(
            Job.template_id == template_id,
            Job.job_type == "template",
            Job.status == "template_ready",
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No completed test jobs for this template",
        )

    output_url = (
        job.assembly_plan.get("output_url")
        if isinstance(job.assembly_plan, dict)
        else None
    )
    clip_paths = (
        job.all_candidates.get("clip_paths", [])
        if isinstance(job.all_candidates, dict)
        else []
    )

    # Check if assembly plan has clip_gcs_path in all steps (needed for re-render)
    has_rerender = False
    if isinstance(job.assembly_plan, dict):
        steps = job.assembly_plan.get("steps", [])
        has_rerender = bool(steps) and all(
            s.get("clip_gcs_path") for s in steps
        )

    base_output_url = (
        job.assembly_plan.get("base_output_url")
        if isinstance(job.assembly_plan, dict)
        else None
    )

    return LatestTestJobResponse(
        job_id=str(job.id),
        output_url=output_url,
        base_output_url=base_output_url,
        clip_paths=clip_paths,
        has_rerender_data=has_rerender,
        created_at=job.created_at,
    )


# ── Recipe history endpoint ────────────────────────────────────────────────────


@router.get(
    "/templates/{template_id}/recipe-history",
    response_model=RecipeHistoryResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_recipe_history(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RecipeHistoryResponse:
    """Paginated list of recipe versions for a template."""
    await get_template_or_404(template_id, db)

    base = select(TemplateRecipeVersion).where(
        TemplateRecipeVersion.template_id == template_id
    )

    count_result = await db.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        base.order_by(TemplateRecipeVersion.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    versions = result.scalars().all()

    return RecipeHistoryResponse(
        versions=[
            RecipeVersionItem(
                id=str(v.id),
                trigger=v.trigger,
                created_at=v.created_at,
                slot_count=(
                    len(v.recipe.get("slots", []))
                    if isinstance(v.recipe, dict) else 0
                ),
                total_duration_s=(
                    float(v.recipe.get("total_duration_s", 0))
                    if isinstance(v.recipe, dict) else 0
                ),
            )
            for v in versions
        ],
        total=total,
    )


# ── Recipe GET/PUT endpoints ──────────────────────────────────────────────────


@router.get(
    "/templates/{template_id}/recipe",
    response_model=RecipeResponse,
    dependencies=[Depends(_require_admin)],
)
async def get_recipe(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> RecipeResponse:
    """Return the current recipe JSON with version metadata."""
    template = await get_template_or_404(template_id, db)
    require_ready(template)

    if not template.recipe_cached:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No recipe found for this template",
        )

    # Find latest version for metadata
    result = await db.execute(
        select(TemplateRecipeVersion)
        .where(TemplateRecipeVersion.template_id == template_id)
        .order_by(TemplateRecipeVersion.created_at.desc())
        .limit(1)
    )
    latest_version = result.scalar_one_or_none()

    # Count total versions
    count_result = await db.execute(
        select(func.count()).select_from(
            select(TemplateRecipeVersion)
            .where(TemplateRecipeVersion.template_id == template_id)
            .subquery()
        )
    )
    version_count = count_result.scalar() or 0

    return RecipeResponse(
        recipe=template.recipe_cached,
        version_id=str(latest_version.id) if latest_version else "",
        version_number=version_count,
    )


@router.put(
    "/templates/{template_id}/recipe",
    response_model=RecipeResponse,
    dependencies=[Depends(_require_admin)],
)
async def save_recipe(
    template_id: str,
    req: SaveRecipeRequest,
    db: AsyncSession = Depends(get_db),
) -> RecipeResponse:
    """Save a manually edited recipe, creating a new version."""
    template = await get_template_or_404(template_id, db)
    require_ready(template)

    # Optimistic lock: reject if a newer version exists
    if req.base_version_id:
        result = await db.execute(
            select(TemplateRecipeVersion)
            .where(TemplateRecipeVersion.template_id == template_id)
            .order_by(TemplateRecipeVersion.created_at.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        if latest and str(latest.id) != req.base_version_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Recipe was modified since you loaded it "
                    f"(latest version: {latest.id}, your base: {req.base_version_id}). "
                    "Reload and try again."
                ),
            )

    # Pydantic already validated the schema — convert to dict
    recipe_dict = req.recipe.model_dump()

    # Create new version (follows pattern from template_orchestrate.py:191-204)
    version = TemplateRecipeVersion(
        template_id=template_id,
        recipe=recipe_dict,
        trigger="manual_edit",
    )
    db.add(version)

    # Update cached recipe
    template.recipe_cached = recipe_dict
    template.recipe_cached_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(version)

    # Count total versions
    count_result = await db.execute(
        select(func.count()).select_from(
            select(TemplateRecipeVersion)
            .where(TemplateRecipeVersion.template_id == template_id)
            .subquery()
        )
    )
    version_count = count_result.scalar() or 0

    log.info(
        "recipe_manual_edit",
        template_id=template_id,
        version_id=str(version.id),
        slot_count=len(recipe_dict.get("slots", [])),
    )

    return RecipeResponse(
        recipe=recipe_dict,
        version_id=str(version.id),
        version_number=version_count,
    )


# ── Music variant (children) schemas ──────────────────────────────────────────


class CreateChildRequest(BaseModel):
    music_track_id: str


class ChildTemplateItem(BaseModel):
    id: str
    name: str
    music_track_id: str
    track_title: str
    track_artist: str
    beat_count: int
    analysis_status: str
    published_at: datetime | None
    created_at: datetime


class ChildrenListResponse(BaseModel):
    children: list[ChildTemplateItem]
    total: int


class RemergeResponse(BaseModel):
    updated: int
    skipped: int = 0
    skipped_ids: list[str] = []


# ── Music variant (children) endpoints ────────────────────────────────────────


@router.post(
    "/templates/{template_id}/children",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_child_template(
    template_id: str,
    req: CreateChildRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Create a music sub-template by merging parent recipe with a track's beats."""
    from app.pipeline.music_recipe import merge_template_with_track  # noqa: PLC0415

    parent = await get_template_or_404(template_id, db)

    if parent.template_type != "music_parent":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Parent template must have template_type='music_parent'",
        )
    if not parent.recipe_cached:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parent template has no recipe — analyze it first",
        )

    # Load music track
    result = await db.execute(
        select(MusicTrack).where(MusicTrack.id == req.music_track_id)
    )
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Music track not found",
        )
    if track.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Music track is not ready (status: {track.analysis_status})",
        )

    # Check for duplicate parent+track
    dup_result = await db.execute(
        select(func.count()).select_from(
            select(VideoTemplate)
            .where(
                VideoTemplate.parent_template_id == template_id,
                VideoTemplate.music_track_id == req.music_track_id,
            )
            .subquery()
        )
    )
    if (dup_result.scalar() or 0) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A sub-template for this track already exists",
        )

    # Build track_data for merge
    track_data = {
        "beat_timestamps_s": track.beat_timestamps_s or [],
        "track_config": track.track_config or {},
        "duration_s": track.duration_s or 0.0,
    }

    try:
        merged_recipe = merge_template_with_track(parent.recipe_cached, track_data)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    child_id = str(uuid.uuid4())
    child = VideoTemplate(
        id=child_id,
        name=f"{parent.name} — {track.title}",
        gcs_path=parent.gcs_path,
        template_type="music_child",
        parent_template_id=parent.id,
        music_track_id=track.id,
        recipe_cached=merged_recipe,
        recipe_cached_at=datetime.now(UTC),
        analysis_status="ready",
        audio_gcs_path=track.audio_gcs_path,
        required_clips_min=merged_recipe.get("required_clips_min", parent.required_clips_min),
        required_clips_max=merged_recipe.get("required_clips_max", parent.required_clips_max),
    )
    db.add(child)

    # Create initial recipe version
    version = TemplateRecipeVersion(
        template_id=child_id,
        recipe=merged_recipe,
        trigger="initial_analysis",
    )
    db.add(version)

    await db.commit()
    await db.refresh(child)

    log.info(
        "child_template_created",
        child_id=child_id,
        parent_id=template_id,
        track_id=track.id,
    )
    return _template_response(child)


@router.get(
    "/templates/{template_id}/children",
    response_model=ChildrenListResponse,
    dependencies=[Depends(_require_admin)],
)
async def list_children(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> ChildrenListResponse:
    """List all music sub-templates for a parent template."""
    await get_template_or_404(template_id, db)

    result = await db.execute(
        select(VideoTemplate, MusicTrack)
        .join(MusicTrack, VideoTemplate.music_track_id == MusicTrack.id)
        .where(VideoTemplate.parent_template_id == template_id)
        .order_by(VideoTemplate.created_at.desc())
    )
    rows = result.all()

    children = [
        ChildTemplateItem(
            id=child.id,
            name=child.name,
            music_track_id=child.music_track_id or "",
            track_title=track.title,
            track_artist=track.artist,
            beat_count=len(track.beat_timestamps_s or []),
            analysis_status=child.analysis_status,
            published_at=child.published_at,
            created_at=child.created_at,
        )
        for child, track in rows
    ]

    return ChildrenListResponse(children=children, total=len(children))


@router.post(
    "/templates/{template_id}/remerge-children",
    response_model=RemergeResponse,
    dependencies=[Depends(_require_admin)],
)
async def remerge_children(
    template_id: str,
    db: AsyncSession = Depends(get_db),
) -> RemergeResponse:
    """Re-merge all children with the parent's latest recipe."""
    from app.pipeline.music_recipe import merge_template_with_track  # noqa: PLC0415

    parent = await get_template_or_404(template_id, db)

    if parent.template_type != "music_parent":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only music_parent templates can remerge children",
        )
    if not parent.recipe_cached:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parent template has no recipe",
        )

    # Load children with their tracks
    result = await db.execute(
        select(VideoTemplate, MusicTrack)
        .join(MusicTrack, VideoTemplate.music_track_id == MusicTrack.id)
        .where(VideoTemplate.parent_template_id == template_id)
    )
    rows = result.all()

    updated = 0
    skipped_ids: list[str] = []
    for child, track in rows:
        track_data = {
            "beat_timestamps_s": track.beat_timestamps_s or [],
            "track_config": track.track_config or {},
            "duration_s": track.duration_s or 0.0,
        }
        try:
            merged = merge_template_with_track(parent.recipe_cached, track_data)
        except ValueError:
            log.warning(
                "remerge_skip_child",
                child_id=child.id,
                track_id=track.id,
                reason="merge produced 0 slots",
            )
            skipped_ids.append(child.id)
            continue

        child.recipe_cached = merged
        child.recipe_cached_at = datetime.now(UTC)
        child.required_clips_min = merged.get("required_clips_min", child.required_clips_min)
        child.required_clips_max = merged.get("required_clips_max", child.required_clips_max)

        version = TemplateRecipeVersion(
            template_id=child.id,
            recipe=merged,
            trigger="remerge",
        )
        db.add(version)
        updated += 1

    await db.commit()
    log.info(
        "remerge_children_done",
        parent_id=template_id,
        updated=updated,
        skipped=len(skipped_ids),
    )
    return RemergeResponse(updated=updated, skipped=len(skipped_ids), skipped_ids=skipped_ids)


# ── Text preview endpoint ─────────────────────────────────────────────────────


class TextPreviewRequest(BaseModel):
    """Parameters for rendering a text overlay preview image."""
    subject_text: str = "PERU"
    subject_size_px: int = 199
    subject_y_frac: float = 0.45
    subject_color: str = "#F4D03F"
    prefix_text: str = "Welcome to"
    prefix_size_px: int = 36
    prefix_y_frac: float = 0.4720
    prefix_color: str = "#FFFFFF"


@router.post(
    "/templates/{template_id}/text-preview",
    dependencies=[Depends(_require_admin)],
)
async def text_preview(
    template_id: str,
    req: TextPreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Render a static PNG preview of text overlay positioning.

    Returns a base64-encoded PNG image for the admin text tuning UI.
    """
    import base64  # noqa: PLC0415
    import io  # noqa: PLC0415

    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    from app.pipeline.text_overlay import (  # noqa: PLC0415
        CANVAS_H,
        CANVAS_W,
        FONTS_DIR,
    )

    await get_template_or_404(template_id, db)

    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (30, 40, 32, 255))
    draw = ImageDraw.Draw(img)

    # Parse hex colors
    def hex_to_rgb(h: str) -> tuple[int, int, int, int]:
        h = h.lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)

    # Render subject text (e.g. PERU)
    import os  # noqa: PLC0415

    subject_font_path = os.path.join(FONTS_DIR, "Montserrat-ExtraBold.ttf")
    subject_font = ImageFont.truetype(subject_font_path, req.subject_size_px)
    s_bbox = draw.textbbox((0, 0), req.subject_text, font=subject_font)
    s_tw = s_bbox[2] - s_bbox[0]
    s_th = s_bbox[3] - s_bbox[1]
    s_x = (CANVAS_W - s_tw) // 2
    s_y = int(CANVAS_H * req.subject_y_frac - s_th / 2)
    draw.text((s_x, s_y), req.subject_text, fill=hex_to_rgb(req.subject_color), font=subject_font)

    # Render prefix text (e.g. Welcome to)
    prefix_font_path = os.path.join(FONTS_DIR, "PlayfairDisplay-Regular.ttf")
    prefix_font = ImageFont.truetype(prefix_font_path, req.prefix_size_px)
    p_bbox = draw.textbbox((0, 0), req.prefix_text, font=prefix_font)
    p_tw = p_bbox[2] - p_bbox[0]
    p_th = p_bbox[3] - p_bbox[1]
    p_x = (CANVAS_W - p_tw) // 2
    p_y = int(CANVAS_H * req.prefix_y_frac - p_th / 2)
    draw.text((p_x, p_y), req.prefix_text, fill=hex_to_rgb(req.prefix_color), font=prefix_font)

    # Encode as base64 PNG
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    return {"image_base64": b64, "width": CANVAS_W, "height": CANVAS_H}


# ── Presigned upload endpoint ──────────────────────────────────────────────────


@router.post(
    "/upload-presigned",
    response_model=PresignedUploadResponse,
    dependencies=[Depends(_require_admin)],
)
async def upload_presigned(
    req: PresignedUploadRequest,
) -> PresignedUploadResponse:
    """Generate a presigned PUT URL for uploading a template video to GCS."""
    import datetime as dt  # noqa: PLC0415
    import os  # noqa: PLC0415

    from app.storage import _get_client  # noqa: PLC0415

    # Sanitize filename: strip path components to prevent path traversal
    safe_filename = os.path.basename(req.filename)
    if not safe_filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid filename",
        )

    template_upload_id = str(uuid.uuid4())
    gcs_path = f"templates/{template_upload_id}/{safe_filename}"

    bucket = _get_client().bucket(settings.storage_bucket)
    blob = bucket.blob(gcs_path)
    url = blob.generate_signed_url(
        version="v4",
        expiration=dt.timedelta(minutes=30),
        method="PUT",
        content_type=req.content_type,
    )

    return PresignedUploadResponse(upload_url=url, gcs_path=gcs_path)


# ── Create template from music track ─────────────────────────────────────────


class CreateTemplateFromMusicTrackRequest(BaseModel):
    music_track_id: str
    name: str | None = None


@router.post(
    "/templates/from-music-track",
    response_model=TemplateResponse,
    dependencies=[Depends(_require_admin)],
)
async def create_template_from_music_track(
    req: CreateTemplateFromMusicTrackRequest,
    db: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Create an audio-only template from a music track's cached recipe."""
    track = await db.get(MusicTrack, req.music_track_id)
    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Music track {req.music_track_id} not found",
        )

    if track.analysis_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Music track is not ready (status: {track.analysis_status})",
        )

    if not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Music track has no audio file",
        )

    # Load recipe: prefer cached Gemini recipe, fall back to beat-only
    recipe = track.recipe_cached
    if recipe is None:
        from app.pipeline.music_recipe import generate_music_recipe  # noqa: PLC0415

        track_data = {
            "beat_timestamps_s": track.beat_timestamps_s or [],
            "track_config": track.track_config or {},
            "duration_s": track.duration_s,
        }
        try:
            recipe = generate_music_recipe(track_data)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot generate recipe: {exc}",
            ) from exc

    # Derive clip counts from recipe
    n_slots = len(recipe.get("slots", []))
    req_min = recipe.get("required_clips_min", max(1, n_slots // 2))
    req_max = recipe.get("required_clips_max", max(1, n_slots))

    template_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    template = VideoTemplate(
        id=template_id,
        name=req.name or track.title,
        gcs_path=None,
        template_type="audio_only",
        audio_gcs_path=track.audio_gcs_path,
        music_track_id=track.id,
        recipe_cached=recipe,
        recipe_cached_at=now,
        analysis_status="ready",
        required_clips_min=req_min,
        required_clips_max=req_max,
        created_at=now,
    )
    db.add(template)

    # Create initial recipe version
    version = TemplateRecipeVersion(
        template_id=template_id,
        recipe=recipe,
        trigger="initial_analysis",
    )
    db.add(version)

    await db.commit()
    await db.refresh(template)

    log.info(
        "template_from_music_track_created",
        template_id=template_id,
        track_id=req.music_track_id,
        slot_count=n_slots,
    )

    return TemplateResponse(
        id=template.id,
        name=template.name,
        gcs_path=template.gcs_path or "",
        analysis_status=template.analysis_status,
        required_clips_min=template.required_clips_min,
        required_clips_max=template.required_clips_max,
        published_at=template.published_at,
        archived_at=template.archived_at,
        description=template.description,
        source_url=template.source_url,
        thumbnail_gcs_path=template.thumbnail_gcs_path,
        error_detail=template.error_detail,
        template_type=template.template_type,
        parent_template_id=template.parent_template_id,
        music_track_id=template.music_track_id,
        created_at=template.created_at,
    )
