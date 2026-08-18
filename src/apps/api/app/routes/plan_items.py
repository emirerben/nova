"""Plan-item endpoints + shared item serialization (content-plan Phase 4).

PATCH /plan-items/{id} — hand-edit a plan item (theme / idea / filming_suggestion).

Also the home of `derive_item_status` + `plan_item_response`, used here and by
content_plans.py. Live render state is DERIVED from the linked Job.status at read
time (plan T2): `item_status` on the row only ever holds `idea` | `awaiting_clips`;
generating / ready / failed come from the Job so a reaper-killed job can never
leave an item stuck "generating" forever.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, NoReturn

import structlog
from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, status
from fastapi import UploadFile as MultipartFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app import storage
from app.agents._schemas.edit_format import coerce_edit_format
from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.models import ContentPlan, Job, MusicTrack, Persona, PlanItem, PlanItemAsset
from app.routes._copilot import CopilotTurnBody, CopilotTurnResponse, run_copilot_turn
from app.routes._director import (
    DirectorFeedbackBody,
    DirectorSuggestionsBody,
    DirectorSuggestionsResponse,
    record_director_feedback,
    run_director,
)
from app.routes._omni import (
    OmniAssetClaimBody,
    OmniAssetResponse,
    OmniAssetStartBody,
    cancel_omni_asset,
    claim_omni_asset,
    get_omni_asset,
    start_omni_asset,
)
from app.routes.generative_jobs import (
    CAPTION_EDIT_ARCHETYPES,
    BedLevelRequest,
    CaptionFontRequest,
    CaptionLanguageRequest,
    CaptionPositionRequest,
    CaptionsEnabledRequest,
    CaptionsRequest,
    CaptionStyleRequest,
    ChangeStyleRequest,
    CustomEffectRequest,
    EditorCommitRequest,
    EditorCommitResponse,
    EditorCommitSections,
    EditVariantRequest,
    LyricSeedsResponse,
    LyricsSectionRequest,
    OrientationRequest,
    PatchSceneTimingRequest,
    RetextRequest,
    SetIntroSizeRequest,
    SetIntroTimingRequest,
    SwapSongRequest,
    TextElementsRequest,
    TimelineEditRequest,
    TimelineResponse,
    _publish_committed_variant_render,
    cascade_removed_overlay_effect_groups,
    dispatch_apply_captions,
    dispatch_apply_custom_effect,
    dispatch_apply_speech_cut_candidate,
    dispatch_change_style,
    dispatch_edit_timeline,
    dispatch_edit_variant,
    dispatch_get_lyric_seeds,
    dispatch_get_timeline,
    dispatch_patch_scene_timing,
    dispatch_reset_timeline,
    dispatch_restore_original_timing,
    dispatch_retext,
    dispatch_retranscribe_captions,
    dispatch_set_caption_position,
    dispatch_set_intro_size,
    dispatch_set_intro_timing,
    dispatch_set_lyrics,
    dispatch_set_media_overlays,
    dispatch_set_narrated_bed_level,
    dispatch_set_orientation,
    dispatch_set_sound_effects,
    dispatch_set_text_elements,
    dispatch_swap_song,
    enqueue_editor_commit_render,
    persist_variant_caption_font,
    persist_variant_caption_style,
    persist_variant_captions,
    persist_variant_captions_enabled,
    prepare_editor_commit,
    require_editable_variant,
    require_guided_story_editor_commit,
    rollback_speech_cut_dispatch,
    speech_cut_director_context,
    validate_media_overlays_for_user,
    validate_sound_effects_for_user,
    visual_block_variant_duration,
)
from app.routes.waitlist import get_real_ip
from app.schemas.edit_proposal import (
    EditProposalResponse,
    EditProposalSnapshot,
    ProposalBrief,
    StoryBeat,
    parse_edit_proposal,
)
from app.schemas.montage_preset import (
    MontagePreset,
    coerce_montage_preset,
    is_collage_montage_preset,
)
from app.services.content_plan_persona import (
    PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
    PlanPersonaOwnershipError,
    load_owned_plan_persona,
    require_plan_persona_owned,
)
from app.services.job_status import PLAN_ITEM_JOB_FAILED, PLAN_ITEM_JOB_READY
from app.services.media_overlay_preview import (
    convert_heif_overlay_preview,
    is_heif_overlay,
    nonblank_str,
)


async def _load_plan_persona_or_409(
    db: AsyncSession,
    plan: ContentPlan,
    *,
    for_update: bool = False,
) -> Persona:
    try:
        return await load_owned_plan_persona(db, plan, for_update=for_update)
    except PlanPersonaOwnershipError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        ) from None


def _cache_owned_item_context(item: PlanItem, plan: ContentPlan, persona: Persona) -> None:
    # Request-local cache only.  The three serializer helpers otherwise repeat
    # the same plan/persona reads back-to-back for every item response.
    item.__dict__["_owned_plan_persona_context"] = (plan, persona)


async def _load_item_plan_persona(
    item: PlanItem,
    db: AsyncSession,
) -> tuple[ContentPlan, Persona]:
    cached = item.__dict__.get("_owned_plan_persona_context")
    if isinstance(cached, tuple) and len(cached) == 2:
        plan, persona = cached
        try:
            require_plan_persona_owned(plan, persona)
        except PlanPersonaOwnershipError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            ) from None
        return plan, persona

    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        )
    persona = await _load_plan_persona_or_409(db, plan)
    _cache_owned_item_context(item, plan, persona)
    return plan, persona


log = structlog.get_logger()
router = APIRouter()

# Themed plan uploads land under the persistent `users/` prefix (NOT swept by the
# 24h GCS delete rule). Allowlisted in admin_music._ALLOWED_CLIP_PREFIXES.
_MAX_CLIPS_PER_ITEM = 20
_MAX_BYTES_PER_FILE = 4 * 1024 * 1024 * 1024  # 4GB
_ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime"}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
_IMAGE_FILE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

# Allowed content types for media-overlay card uploads (images + short video).
_OVERLAY_ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "video/mp4",
    "video/quicktime",
}
_MAX_OVERLAY_CARDS = 10
_MAX_OVERLAY_FILE_BYTES = 512 * 1024 * 1024  # 512 MB per card (video card upper bound)

# Allowed content types for sound-effect uploads.
_SFX_ALLOWED_CONTENT_TYPES = {
    "audio/mpeg",  # .mp3
    "audio/mp4",  # .m4a, .m4b
    "audio/wav",  # .wav
    "audio/x-wav",  # .wav (alternative)
    "audio/aac",  # .aac
    "audio/ogg",  # .ogg, .opus
    "audio/webm",  # .webm audio
}
_MAX_SFX_CARDS = 20  # max placements per variant
_MAX_SFX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB per effect

# Job.status buckets — single-sourced with the dispatch-time active-render
# re-check in tasks/content_plan_build.py (plans/014): the two must never drift.
_JOB_READY = PLAN_ITEM_JOB_READY
_JOB_FAILED = PLAN_ITEM_JOB_FAILED


def _is_image_clip_path(path: str) -> bool:
    """Best-effort uploaded-clip kind check from the durable object name."""
    clean = (path or "").split("?", 1)[0].lower()
    return any(clean.endswith(ext) for ext in _IMAGE_FILE_EXTS)


def _item_uses_collage_preset(item: PlanItem) -> bool:
    return coerce_edit_format(
        getattr(item, "edit_format", None)
    ) == "montage" and is_collage_montage_preset(getattr(item, "montage_preset", None))


def _allowed_item_upload_content_types(item: PlanItem) -> set[str]:
    if _item_uses_collage_preset(item):
        return _ALLOWED_CONTENT_TYPES | _IMAGE_CONTENT_TYPES
    return _ALLOWED_CONTENT_TYPES


def derive_item_status(item: PlanItem) -> str:
    """idea | awaiting_clips | generating | ready | failed — derived, never stored."""
    job = item.current_job
    if job is None:
        # No job minted yet: row state is the source of truth (idea/awaiting_clips).
        return item.item_status
    if job.status in _JOB_READY:
        return "ready"
    if job.status in _JOB_FAILED:
        return "failed"
    return "generating"


def _edit_proposal_response(item: PlanItem) -> dict | None:
    proposal = parse_edit_proposal(getattr(item, "edit_proposal", None))
    if proposal is None:
        return None
    payload = proposal.model_dump(mode="json")
    # Admin/debug-only diagnostic (exception type + short reason) — never
    # surfaced to end users. See ProposalFailure.detail / _exc_detail().
    if isinstance(payload.get("failure"), dict):
        payload["failure"].pop("detail", None)
    attempt = proposal.conversation_attempt
    if attempt is not None:
        from app.services.edit_proposals import (  # noqa: PLC0415
            EDIT_CONVERSATION_ATTEMPT_TTL_S,
        )

        age_s = max(0.0, (datetime.now(UTC) - attempt.started_at).total_seconds())
        payload["conversation_in_progress"] = age_s < EDIT_CONVERSATION_ATTEMPT_TTL_S
        payload["conversation_retry_required"] = age_s >= EDIT_CONVERSATION_ATTEMPT_TTL_S
    payload["conversation_attempt"] = None
    clip_paths_by_id = {
        str(assignment.get("media_id")): str(assignment.get("gcs_path"))
        for assignment in (item.clip_assignments or [])
        if isinstance(assignment, dict)
        and assignment.get("media_id")
        and assignment.get("gcs_path")
    }
    asset_path_fragment = f"/plan/{item.id}/pool/"
    # Preview URLs are response-only decoration. Immutable identity remains the
    # stored gcs_path + generation; PATCH validation ignores this extra key.
    for holder in (payload.get("draft"), (payload.get("last_approved") or {}).get("snapshot")):
        if not isinstance(holder, dict):
            continue
        for ref in holder.get("media") or []:
            if not isinstance(ref, dict) or not ref.get("gcs_path"):
                continue
            path = str(ref["gcs_path"])
            if ref.get("lane") == "clip":
                safe_to_sign = clip_paths_by_id.get(str(ref.get("media_id"))) == path
            else:
                # Pool objects are always promoted under this item's durable
                # namespace. Never sign an arbitrary path merely because it
                # appeared in legacy/corrupt proposal JSONB.
                safe_to_sign = path.startswith("users/") and asset_path_fragment in path
            if not safe_to_sign:
                ref["preview_url"] = None
                continue
            try:
                ref["preview_url"] = storage.signed_get_url(path, expiration_minutes=60)
            except Exception:  # noqa: BLE001 - filename tile remains usable
                ref["preview_url"] = None
    return payload


class FilmingShotResponse(BaseModel):
    """One shot from the plan item's filming guide.

    All fields default to safe values so a hand-corrupted or legacy JSONB row
    with missing keys never 500s the read path.
    """

    shot_id: str | None = None  # stable server-assigned uuid; null for legacy pre-0052 rows
    what: str = ""
    how: str = ""
    duration_s: int = 1  # matches MIN_SHOT_DURATION_S; 0 would render as confusing "0s" badge
    clip_count: int = 1  # Number of clips the creator should film for this shot


class ClipAssignmentResponse(BaseModel):
    """One clip assignment in the response (mirrors the DB JSONB shape)."""

    gcs_path: str
    shot_id: str | None = None  # null = extra-footage pool
    # Optional creator context about the clip; "" when unset.
    user_note: str = ""
    # True = the footage-pool matcher placed this clip (provisional chip in the
    # UI; conformance suppressed until the user keeps/swaps/replaces it).
    machine_matched: bool = False
    # Stable server-owned identity used by guided-edit proposals. Null only on
    # legacy rows that have never been processed by Plan edit.
    media_id: str | None = None


class PlanItemResponse(BaseModel):
    id: str
    # Idea-centric (0055+): day_index and theme are nullable; position is the sort key.
    day_index: int | None
    theme: str | None
    idea: str
    position: int
    scheduled_date: str | None = None  # ISO date string (YYYY-MM-DD) or None
    notes: str | None = None
    scenes: list = []
    filming_suggestion: str | None
    # The AI's "why this works", surfaced read-only in the dashboard.
    rationale: str | None
    # Structured shot list (2–4 shots). Always a list; empty for legacy items
    # whose plans predate this field (frontend falls back to filming_suggestion).
    filming_guide: list[FilmingShotResponse]
    clip_gcs_paths: list[str]
    # Per-shot clip assignments. Shape: [{gcs_path, shot_id}]; shot_id=null = pool.
    # Populated since migration 0052; empty list for items with no clips yet.
    clip_assignments: list[ClipAssignmentResponse] = []
    # Versioned Plan edit envelope. Invalid legacy/corrupt JSON is serialized
    # as null by _edit_proposal_response; valid state stays OpenAPI-discoverable.
    edit_proposal: EditProposalResponse | None = None
    guided_edit_available: bool = False
    guided_edit_conversation_available: bool = False
    # GUIDED_AUTO_DESIGN_ENABLED (config.py). False on an old API that predates
    # this field — the frontend gates the AI-designs-by-default Generate
    # button behavior on this exact default so a new web build against an old
    # API deploy keeps today's strict-gate behavior (deploy-skew safe).
    guided_edit_auto_design: bool = False
    status: str
    current_job_id: str | None
    finished_at: datetime | None = None
    user_edited: bool
    # Creator Agent M4: instruction level from the owning user's style entity.
    # Drives the instructed/uninstructed upload split on the item page:
    #   "full" or "light" → single-file replace mode when filming_guide is present
    #   "none" → keep existing bulk-append behaviour unchanged
    # Default "full": applies to items whose plan predates M1 or when style is absent.
    instruction_level: str = "full"
    # ConformanceFeedbackAgent verdict (best-effort, display-only). NULL until the
    # agent runs (flag on + clip attached). Never blocks Generate.
    conformance: dict | None = None
    # Persona content mode (direction fork, 2026-06-11): drives the film-card
    # header copy — "HOW TO FILM THIS" (create_new/legacy) vs "WHAT TO LOOK FOR"
    # (existing_footage) vs "FIND IT OR FILM IT" (mixed). Resolved on the item
    # GET and on plan (list) responses via content_plans._resolve_item_content_mode
    # so the two read paths agree; bare mutation responses default to create_new.
    content_mode: str = "create_new"
    # Render archetype assigned at plan-generation time (e.g. "montage",
    # "talking_head"). Null for items generated before this field shipped.
    edit_format: str | None = None
    # Per-video choice plus server-authoritative capability.  Availability is
    # never derived from a NEXT_PUBLIC flag and does not expose preset identity.
    # Some legacy aggregate/mutation responses do not enrich capability yet and
    # return null rather than asserting a false reason.
    smart_captions_enabled: bool = False
    smart_sound_design_enabled: bool = True
    smart_captions_available: bool | None = None
    smart_captions_unavailable_reason: str | None = None
    # Montage visual preset. "classic" preserves today's sequential montage;
    # collage presets opt into the collage-wall assembler.
    montage_preset: MontagePreset = "classic"
    # Narrated-walkthrough voiceover (0056+). GCS key under voiceover-uploads/.
    # NULL = no voiceover attached; non-null = user has recorded or uploaded one.
    voiceover_gcs_path: str | None = None
    # Landscape-clip fit preference. "fit" (letterbox, default) | "fill" (crop).
    # Only affects clips where width > height; portrait/square always crop.
    landscape_fit: Literal["fit", "fill"] = "fit"
    # Original-audio bed level for narrated. 0 = voice only, 1 = loudest.
    # NULL = Kria's default level. Set via PATCH /{id}/voiceover-bed-level.
    voiceover_bed_level: float | None = None
    # Narrated caption style: "sentence" (sentence-block) or "word" (one big word
    # at a time). NULL = "sentence". PATCH /{id}/voiceover-caption-style.
    voiceover_caption_style: str | None = None
    # BYO-Ideas provenance (M1 T5): the seed whose subject this item honours.
    # NULL = market-bank origin or the item predates T5. Both fields are resolved
    # server-side so the badge is a pure function of the item on the client.
    source_idea_seed_id: str | None = None
    source_idea_seed_text: str | None = None


def plan_item_response(
    item: PlanItem,
    *,
    include_edit_proposal: bool = True,
    instruction_level: str = "full",
    content_mode: str = "create_new",
    seed_text_by_id: dict[str, str] | None = None,
    smart_captions_available: bool | None = None,
    smart_captions_unavailable_reason: str | None = None,
) -> PlanItemResponse:
    # Tolerate missing keys in individual JSONB shots — each shot is constructed
    # via .get() so a hand-corrupted row or a migration-era partial row never raises.
    shots = [
        FilmingShotResponse(
            shot_id=s.get("shot_id"),  # None for pre-0052 rows (backfilled by migration)
            what=s.get("what", ""),
            how=s.get("how", ""),
            duration_s=s.get("duration_s", 1),  # 1 = MIN_SHOT_DURATION_S; 0 renders as "0s" badge
            clip_count=s.get("clip_count", 1),
        )
        for s in (item.filming_guide or [])
        if isinstance(s, dict)
    ]

    # Read-time reconciliation (D15): any assignment whose shot_id is no longer
    # present in the current filming_guide is presented as pool (shot_id=null).
    # This handles the case where the guide was rerolled after clips were attached;
    # the assignment becomes visible extra footage rather than a ghost.
    live_shot_ids = {s.shot_id for s in shots if s.shot_id is not None}
    raw_assignments = item.clip_assignments or []
    reconciled_assignments = [
        ClipAssignmentResponse(
            gcs_path=a.get("gcs_path", ""),
            shot_id=a.get("shot_id") if a.get("shot_id") in live_shot_ids else None,
            user_note=str(a.get("user_note") or ""),
            machine_matched=bool(a.get("machine_matched")),
            media_id=str(a.get("media_id")) if a.get("media_id") else None,
        )
        for a in raw_assignments
        if isinstance(a, dict) and a.get("gcs_path")
    ]

    return PlanItemResponse(
        id=str(item.id),
        day_index=item.day_index,
        theme=item.theme,
        idea=item.idea,
        position=item.position,
        scheduled_date=item.scheduled_date.isoformat() if item.scheduled_date else None,
        notes=item.notes,
        scenes=list(item.scenes) if item.scenes else [],
        filming_suggestion=item.filming_suggestion,
        rationale=item.rationale,
        filming_guide=shots,
        clip_gcs_paths=list(item.clip_gcs_paths or []),
        clip_assignments=reconciled_assignments,
        edit_proposal=_edit_proposal_response(item) if include_edit_proposal else None,
        guided_edit_available=settings.guided_edit_capability_enabled,
        guided_edit_conversation_available=settings.guided_edit_conversation_enabled,
        guided_edit_auto_design=settings.guided_auto_design_enabled,
        status=derive_item_status(item),
        current_job_id=str(item.current_job_id) if item.current_job_id else None,
        finished_at=item.current_job.finished_at if item.current_job is not None else None,
        user_edited=item.user_edited,
        instruction_level=instruction_level,
        conformance=item.conformance,
        content_mode=content_mode
        if content_mode in ("existing_footage", "create_new", "mixed")
        else "create_new",
        edit_format=item.edit_format,
        smart_captions_enabled=getattr(item, "smart_captions_enabled", False) is True,
        smart_sound_design_enabled=(getattr(item, "smart_sound_design_enabled", None) is not False),
        smart_captions_available=smart_captions_available,
        smart_captions_unavailable_reason=smart_captions_unavailable_reason,
        montage_preset=coerce_montage_preset(getattr(item, "montage_preset", None)),
        voiceover_gcs_path=item.voiceover_gcs_path,
        landscape_fit=(
            # Membership check (not just isinstance) guards against arbitrary strings
            # from direct SQL writes or future vocab expansions reaching the Literal model.
            _lf if (_lf := getattr(item, "landscape_fit", None)) in ("fit", "fill") else "fit"
        ),
        voiceover_bed_level=item.voiceover_bed_level,
        voiceover_caption_style=item.voiceover_caption_style,
        source_idea_seed_id=item.source_idea_seed_id,
        source_idea_seed_text=(seed_text_by_id or {}).get(item.source_idea_seed_id)
        if item.source_idea_seed_id
        else None,
    )


async def _get_content_mode(item: PlanItem, db: AsyncSession) -> str:
    """Per-item content_mode override (0058+) → persona JSONB → default create_new.

    Priority: item.content_mode (if set) beats the plan-level persona value.
    This lets montage items toggle the plan-vs-have axis independently of the
    onboarding-fork choice that set the persona default.
    """
    _valid = ("existing_footage", "create_new", "mixed")
    # 1. Per-item override (nullable column, None = "not set yet, inherit persona").
    own = getattr(item, "content_mode", None)
    if own in _valid:
        return own
    # 2. Fall back to persona JSONB (existing pre-0058 behaviour, unchanged).
    _, persona = await _load_item_plan_persona(item, db)
    if not isinstance(persona.persona, dict):
        return "create_new"
    return str(persona.persona.get("content_mode") or "create_new")


async def _get_instruction_level(item: PlanItem, db: AsyncSession) -> str:
    """Read instruction_level from the owning user's personas.style JSONB.

    Invalid optional style data defaults to "full".  A missing, quarantined, or
    cross-tenant persona link is an ownership conflict and never defaults.
    """
    _, persona = await _load_item_plan_persona(item, db)
    style = persona.style or {}
    level = str(style.get("instruction_level", "full") or "full")
    return level if level in ("full", "light", "none") else "full"


async def _get_seed_text_by_id(item: PlanItem, db: AsyncSession) -> dict[str, str]:
    """Build {seed_id: seed_text} map from the owning persona's idea_seeds.

    Used to resolve source_idea_seed_text at read time for the provenance badge.
    An empty/invalid seed list returns {}; an invalid ownership link fails closed.
    """
    _, persona = await _load_item_plan_persona(item, db)
    seeds = persona.idea_seeds if isinstance(persona.idea_seeds, list) else []
    return {
        str(s["id"]): str(s["text"])
        for s in seeds
        if isinstance(s, dict) and s.get("id") and s.get("text")
    }


@router.get("/{item_id}", response_model=PlanItemResponse)
async def get_plan_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    item = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(item, db)
    content_mode = await _get_content_mode(item, db)
    seed_text_by_id = await _get_seed_text_by_id(item, db)
    from app.services.smart_captions import resolve_smart_captions_capability  # noqa: PLC0415

    smart_capability = await resolve_smart_captions_capability(
        user_id=user.id,
        edit_format=item.edit_format,
        db=db,
    )
    return plan_item_response(
        item,
        instruction_level=instruction_level,
        content_mode=content_mode,
        seed_text_by_id=seed_text_by_id,
        smart_captions_available=smart_capability.available,
        smart_captions_unavailable_reason=smart_capability.reason,
    )


class PlanItemEdit(BaseModel):
    theme: str | None = None
    idea: str | None = None
    filming_suggestion: str | None = None
    notes: str | None = None
    scenes: list | None = None
    scheduled_date: str | None = None  # ISO date string (YYYY-MM-DD)
    # User-chosen format (e.g. "montage", "narrated"). Only allowed when the
    # item hasn't started generating (no active job) to avoid mid-flight changes.
    edit_format: str | None = None
    smart_captions_enabled: bool | None = None
    smart_sound_design_enabled: bool | None = None
    # Accept an expand proposal's filming_guide directly.
    filming_guide: list[dict] | None = None
    # Landscape-clip render preference: "fit" (letterbox) | "fill" (crop-to-fill).
    # Ignored for portrait/square clips — they always crop regardless.
    landscape_fit: Literal["fit", "fill"] | None = None
    # Montage visual preset. Only affects montage renders; default classic.
    montage_preset: MontagePreset | None = None
    # Per-item content_mode override (montage plan-vs-have toggle, 0058+).
    # When set, supersedes the persona-level content_mode for this item only.
    # "create_new" = "Planning to film"; "existing_footage" = "I already have footage".
    content_mode: Literal["existing_footage", "create_new", "mixed"] | None = None


def _stamp_missing_filming_shot_ids(shots: list[dict]) -> list[dict]:
    return [{**shot, "shot_id": shot.get("shot_id") or uuid.uuid4().hex} for shot in shots]


@router.patch("/{item_id}", response_model=PlanItemResponse)
async def edit_plan_item(
    item_id: str,
    edit: PlanItemEdit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    from datetime import date as date_type  # noqa: PLC0415

    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    item = await _load_owned_item(item_id, user.id, db, for_update=True)

    updates = edit.model_dump(exclude_none=True)
    smart_capability = None
    if updates.get("smart_captions_enabled") is True:
        from app.services.smart_captions import (  # noqa: PLC0415
            resolve_smart_captions_capability,
        )

        smart_capability = await resolve_smart_captions_capability(
            user_id=user.id,
            edit_format=updates.get("edit_format", item.edit_format),
            db=db,
        )
        if not smart_capability.available:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"smart_captions_unavailable:{smart_capability.reason}",
            )
    if "theme" in updates:
        item.theme = _sanitize_text(updates["theme"]) or item.theme
    if "idea" in updates:
        item.idea = _sanitize_text(updates["idea"]) or item.idea
    if "filming_suggestion" in updates:
        item.filming_suggestion = _sanitize_text(updates["filming_suggestion"]) or None
    if "notes" in updates:
        item.notes = updates["notes"] or None
    if "scenes" in updates:
        item.scenes = list(updates["scenes"])
        flag_modified(item, "scenes")
    if "scheduled_date" in updates:
        raw = updates["scheduled_date"]
        item.scheduled_date = date_type.fromisoformat(raw) if raw else None
    if "edit_format" in updates:
        item.edit_format = updates["edit_format"] or None
        if item.edit_format != "subtitled":
            item.smart_captions_enabled = False
    if "smart_captions_enabled" in updates:
        item.smart_captions_enabled = updates["smart_captions_enabled"] is True
    if "smart_sound_design_enabled" in updates:
        item.smart_sound_design_enabled = updates["smart_sound_design_enabled"] is True
    if "filming_guide" in updates:
        from sqlalchemy.orm.attributes import flag_modified as _flag  # noqa: PLC0415

        item.filming_guide = _stamp_missing_filming_shot_ids(list(updates["filming_guide"]))
        _flag(item, "filming_guide")
    if "landscape_fit" in updates and updates["landscape_fit"] is not None:
        item.landscape_fit = updates["landscape_fit"]  # Pydantic Literal already validates
    if "montage_preset" in updates and updates["montage_preset"] is not None:
        item.montage_preset = updates["montage_preset"]  # Pydantic Literal already validates
    if "content_mode" in updates and updates["content_mode"] is not None:
        item.content_mode = updates["content_mode"]  # Pydantic Literal already validates
    if updates:
        item.user_edited = True
    await db.commit()
    # Reload with current_job eager-loaded (commit expired it) before serializing.
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    content_mode = await _get_content_mode(reloaded, db)
    if smart_capability is None:
        from app.services.smart_captions import (  # noqa: PLC0415
            resolve_smart_captions_capability,
        )

        smart_capability = await resolve_smart_captions_capability(
            user_id=user.id,
            edit_format=reloaded.edit_format,
            db=db,
        )
    return plan_item_response(
        reloaded,
        instruction_level=instruction_level,
        content_mode=content_mode,
        smart_captions_available=smart_capability.available,
        smart_captions_unavailable_reason=smart_capability.reason,
    )


# ── Idea-centric CRUD (0055+) ─────────────────────────────────────────────────


class AddIdeaBody(BaseModel):
    idea: str = Field(..., min_length=1, max_length=500)
    # Optional seed mirror: pass the client-side seed id to link the new item.
    source_idea_seed_id: str | None = None


@router.post("", response_model=PlanItemResponse, status_code=status.HTTP_201_CREATED)
async def add_idea(
    body: AddIdeaBody,
    plan_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Create a bare-minimum PlanItem from a user-supplied idea string.

    Position = max(existing positions) + 1; day_index = None (no calendar slot
    until the item is expanded or explicitly scheduled).
    Also upserts an idea_seed mirror on the persona (status='in_plan') so that
    the idea persists until its linked plan item is deleted.
    """
    try:
        pid = uuid.UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad plan id") from exc

    # Shared mutation lock order: ContentPlan -> Persona -> PlanItem.  Validate
    # and lock both owner rows before adding anything to the unit of work; an
    # autoflush on a later SELECT must not insert a partial item on mismatch.
    plan_stmt = (
        select(ContentPlan)
        .where(ContentPlan.id == pid, ContentPlan.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    plan = (await db.execute(plan_stmt)).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    persona = await _load_plan_persona_or_409(db, plan, for_update=True)
    items_stmt = (
        select(PlanItem)
        .where(PlanItem.content_plan_id == plan.id)
        .order_by(PlanItem.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing_items = (await db.execute(items_stmt)).scalars().all()
    existing_positions = [it.position for it in existing_items]
    next_position = (max(existing_positions) + 1) if existing_positions else 1

    sanitized = _sanitize_text(body.idea) or body.idea
    seed_id = body.source_idea_seed_id or None

    item_id = uuid.uuid4()
    item = PlanItem(
        id=item_id,
        content_plan_id=plan.id,
        idea=sanitized,
        position=next_position,
        day_index=None,  # bare ideas have no calendar slot until explicitly scheduled
        item_status="idea",
        source_idea_seed_id=seed_id,
        user_edited=True,
    )
    # Mirror to idea_seeds on the persona (upsert by id if seed_id supplied).
    raw_seeds: list = list(persona.idea_seeds) if isinstance(persona.idea_seeds, list) else []
    if seed_id:
        # Update existing seed status to in_plan.
        for s in raw_seeds:
            if isinstance(s, dict) and s.get("id") == seed_id:
                s["status"] = "in_plan"
                break
        else:
            raw_seeds.append({"id": seed_id, "text": sanitized, "status": "in_plan"})
    else:
        new_seed_id = uuid.uuid4().hex
        raw_seeds.append({"id": new_seed_id, "text": sanitized, "status": "in_plan"})
        # Backfill on the item so clients can correlate.
        item.source_idea_seed_id = new_seed_id
    persona.idea_seeds = raw_seeds  # type: ignore[assignment]
    flag_modified(persona, "idea_seeds")
    db.add(item)

    await db.commit()
    reloaded = await _load_owned_item(str(item_id), user.id, db)
    return plan_item_response(reloaded)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_idea(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently delete a plan item and its linked persistent idea seed.

    Refuses with 409 if:
    - The item has an active (non-failed/non-done) job attached.
    - The item has clips attached (clips are not automatically cleaned up from GCS).
    """
    # Preserve the existing fast 404/409 behavior before taking write locks.
    item = await _load_owned_item(item_id, user.id, db)

    def ensure_deletable(candidate: PlanItem) -> None:
        derived = derive_item_status(candidate)
        if candidate.current_job_id is not None:
            if derived == "generating":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot delete an item with an active job. Cancel the job first.",
                )

        if candidate.clip_gcs_paths and derived not in {"ready", "failed"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete an item that has clips attached. Remove clips first.",
            )

    ensure_deletable(item)

    # Re-enter under the global ContentPlan -> Persona -> PlanItem lock order,
    # then repeat the safety checks so a job or clip attached between the
    # snapshot and lock returns 409.  No ORM state is mutated before all three
    # ownership/lock checks pass.
    item, _, persona = await _load_owned_item_context(
        item_id,
        user.id,
        db,
        for_update=True,
        known_item=item,
    )
    ensure_deletable(item)
    seed_id = item.source_idea_seed_id

    if seed_id:
        if isinstance(persona.idea_seeds, list):
            retained_seeds = [
                seed
                for seed in persona.idea_seeds
                if not (isinstance(seed, dict) and seed.get("id") == seed_id)
            ]
            if len(retained_seeds) != len(persona.idea_seeds):
                persona.idea_seeds = retained_seeds
                flag_modified(persona, "idea_seeds")

    if item.current_job_id is not None:
        locked_job = await db.get(
            Job,
            item.current_job_id,
            populate_existing=True,
            with_for_update=True,
        )
        if locked_job is not None:
            if locked_job.status == "cancelled":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cancelled video records cannot be deleted.",
                )
            locked_job.content_plan_item_id = None

    await db.delete(item)
    await db.commit()


# ── Themed uploads + per-item generation ──────────────────────────────────────


async def _load_owned_item(
    item_id: str,
    user_id: uuid.UUID,
    db: AsyncSession,
    *,
    for_update: bool = False,
) -> PlanItem:
    item, _, _ = await _load_owned_item_context(
        item_id,
        user_id,
        db,
        for_update=for_update,
    )
    return item


async def _load_owned_item_context(
    item_id: str,
    user_id: uuid.UUID,
    db: AsyncSession,
    *,
    for_update: bool = False,
    known_item: PlanItem | None = None,
) -> tuple[PlanItem, ContentPlan, Persona]:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    # Eager-load current_job: plan_item_response → derive_item_status reads the
    # relationship, and a bare db.get() leaves it lazy → MissingGreenlet 500 on
    # the async session once an item has a linked job (mirrors the list endpoint's
    # selectinload in content_plans.py).
    item = known_item
    if item is None:
        item_stmt = (
            select(PlanItem).where(PlanItem.id == iid).options(selectinload(PlanItem.current_job))
        )
        item = (await db.execute(item_stmt)).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    if item.id != iid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    if for_update:
        plan = await db.get(
            ContentPlan,
            item.content_plan_id,
            populate_existing=True,
            with_for_update=True,
        )
    else:
        plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    persona = await _load_plan_persona_or_409(db, plan, for_update=for_update)

    if for_update:
        # Plan and persona locks are held before the item lock.  Re-read under
        # the lock and pin the original plan id so an inconsistent association
        # cannot cross the validated boundary.
        item_stmt = (
            select(PlanItem)
            .where(PlanItem.id == iid, PlanItem.content_plan_id == plan.id)
            .options(selectinload(PlanItem.current_job))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        item = (await db.execute(item_stmt)).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    _cache_owned_item_context(item, plan, persona)
    return item, plan, persona


class UploadFile(BaseModel):
    filename: str
    content_type: str
    file_size_bytes: int


class UploadUrlsBody(BaseModel):
    files: list[UploadFile]


class UploadUrlItem(BaseModel):
    upload_url: str
    gcs_path: str


class UploadUrlsResponse(BaseModel):
    urls: list[UploadUrlItem]


@router.post("/{item_id}/upload-urls", response_model=UploadUrlsResponse)
async def create_upload_urls(
    item_id: str,
    body: UploadUrlsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UploadUrlsResponse:
    """Signed PUT URLs for themed clips, under the persistent users/ prefix."""
    item = await _load_owned_item(item_id, user.id, db)
    if not body.files or len(body.files) > _MAX_CLIPS_PER_ITEM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provide 1-{_MAX_CLIPS_PER_ITEM} files",
        )
    urls: list[UploadUrlItem] = []
    allowed_types = _allowed_item_upload_content_types(item)
    for f in body.files:
        if f.content_type not in allowed_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Photos require a collage preset"
                    if f.content_type in _IMAGE_CONTENT_TYPES
                    else f"Unsupported content type: {f.content_type}"
                ),
            )
        if f.file_size_bytes <= 0 or f.file_size_bytes > _MAX_BYTES_PER_FILE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad file size")
        # Prefix a uuid so two uploads with the same filename don't collide.
        safe_name = f"{uuid.uuid4().hex}-{f.filename.split('/')[-1]}"
        url, gcs_path = storage.presigned_put_url_for_plan_item(
            user_id=str(user.id),
            plan_item_id=str(item.id),
            filename=safe_name,
            content_type=f.content_type,
        )
        urls.append(UploadUrlItem(upload_url=url, gcs_path=gcs_path))
    return UploadUrlsResponse(urls=urls)


_MAX_NOTE_CHARS = 200


class ClipAssignmentBody(BaseModel):
    """One clip assignment sent from the frontend."""

    gcs_path: str
    shot_id: str | None = None  # null = extra-footage pool
    # Optional creator context ("famous vegan restaurant in Buenos Aires").
    # UNTRUSTED free-text: length-capped here, sanitized + DATA-framed at every
    # prompt boundary that consumes it.
    user_note: str = ""


class AttachClipsBody(BaseModel):
    clip_gcs_paths: list[str]
    # Optional per-shot assignments (shot-slot uploader). When absent the whole
    # batch is treated as pool (legacy / uninstructed callers are unaffected).
    assignments: list[ClipAssignmentBody] | None = None


@router.post("/{item_id}/clips", response_model=PlanItemResponse)
async def attach_clips(
    item_id: str,
    body: AttachClipsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Record uploaded clip paths on the item (validated to the users/ prefix).

    Assignment semantics (shot-slot uploader, D16):
      - body.assignments present → validate shot_ids + derive clip_gcs_paths via set_item_clips
      - body.assignments absent  → treat body.clip_gcs_paths as pool (legacy callers)

    D7: nulls item.conformance before dispatching re-analysis so the panel can
    never describe replaced footage. If re-analysis fails, the panel is absent,
    not stale.
    """
    from app.services.plan_clips import (  # noqa: PLC0415
        ClipAssignment,
        ClipAssignmentError,
        set_item_clips,
    )

    item, plan, _ = await _load_owned_item_context(
        item_id,
        user.id,
        db,
        for_update=True,
    )
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)
    # Accept clips uploaded to THIS item's prefix OR matched in from the plan's
    # footage pool (users/{uid}/plan-pool/{plan_id}/). The frontend re-sends the
    # full assignment set on every attach, so without the pool prefix any
    # remove/add on a pool-matched item would 422 (dogfood: keep/swap broke).
    item_prefix = f"users/{user.id}/plan/{item.id}/"
    pool_prefix = f"users/{user.id}/plan-pool/{item.content_plan_id}/"

    def _allowed(path: str) -> bool:
        return path.startswith(item_prefix) or path.startswith(pool_prefix)

    # Preserve machine_matched ONLY for clips that keep the same gcs_path + slot
    # across this re-attach — a full re-send must not silently "confirm" untouched
    # provisional matches (machine_matched isn't on the wire), but moving/replacing
    # a clip legitimately drops the flag.
    prior_mm: dict[tuple[str, str | None], bool] = {
        (a["gcs_path"], a.get("shot_id")): True
        for a in (item.clip_assignments or [])
        if isinstance(a, dict) and a.get("gcs_path") and a.get("machine_matched")
    }
    prior_media_ids: dict[str, str] = {
        str(a["gcs_path"]): str(a["media_id"])
        for a in (item.clip_assignments or [])
        if isinstance(a, dict) and a.get("gcs_path") and a.get("media_id")
    }

    if body.assignments is not None:
        # Shot-slot uploader path: validate prefix, then validate shot_ids.
        for a in body.assignments:
            if not _allowed(a.gcs_path):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Clip path outside this plan item's upload prefix",
                )

        # Build set of live shot_ids from the item's filming_guide.
        live_shot_ids: set[str] = {
            s["shot_id"]
            for s in (item.filming_guide or [])
            if isinstance(s, dict) and s.get("shot_id")
        }

        for a in body.assignments:
            if a.shot_id is not None and a.shot_id not in live_shot_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown shot_id: {a.shot_id}",
                )

        assignments = [
            ClipAssignment(
                gcs_path=a.gcs_path,
                shot_id=a.shot_id,
                user_note=(a.user_note or "")[:_MAX_NOTE_CHARS],
                machine_matched=prior_mm.get((a.gcs_path, a.shot_id), False),
                media_id=prior_media_ids.get(a.gcs_path),
            )
            for a in body.assignments
        ]
    else:
        # Legacy / uninstructed path: all clips go to pool.
        for p in body.clip_gcs_paths:
            if not _allowed(p):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Clip path outside this plan item's upload prefix",
                )
        assignments = [
            ClipAssignment(
                gcs_path=p,
                shot_id=None,
                machine_matched=prior_mm.get((p, None), False),
                media_id=prior_media_ids.get(p),
            )
            for p in body.clip_gcs_paths
        ]

    # Pool→clip promotion guard (server-side twin of the AssetPool "Use in edit"
    # video-only affordance): a NEWLY attached path under the pool sub-prefix must
    # belong to a video pool asset — an image (or a deleted row) would fail the
    # render confusingly instead of loudly here. Only NEW paths are checked: the
    # frontend re-sends the FULL assignment set on every attach, so a previously
    # promoted clip whose pool row was later deleted must keep re-attaching.
    pool_sub_prefix = f"{item_prefix}pool/"
    already_attached = {
        a.get("gcs_path")
        for a in (item.clip_assignments or [])
        if isinstance(a, dict) and a.get("gcs_path")
    }
    new_pool_paths = [
        a.gcs_path
        for a in assignments
        if a.gcs_path.startswith(pool_sub_prefix) and a.gcs_path not in already_attached
    ]
    if new_pool_paths:
        asset_rows = await db.execute(
            select(PlanItemAsset).where(
                PlanItemAsset.plan_item_id == item.id,
                PlanItemAsset.gcs_path.in_(new_pool_paths),
            )
        )
        kind_by_path = {
            row.gcs_path: row.kind for row in asset_rows.scalars() if row.status == "ready"
        }
        allowed_asset_kinds = {"video", "image"} if _item_uses_collage_preset(item) else {"video"}
        for p in new_pool_paths:
            if kind_by_path.get(p) not in allowed_asset_kinds:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "Photos require a collage preset"
                        if kind_by_path.get(p) == "image"
                        else "Only video visuals from the pool can be used in the edit"
                    ),
                )

    try:
        set_item_clips(item, assignments)
    except ClipAssignmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # D7: null conformance so the panel can never describe replaced footage.
    item.conformance = None

    await db.commit()
    # Fire-and-forget conformance analysis (best-effort, never blocks this response).
    from app.tasks.conformance_build import analyze_item_conformance  # noqa: PLC0415

    analyze_item_conformance.delay(str(item.id), ownership_epoch)
    # Reload with current_job eager-loaded (commit expired it) before serializing.
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


class ShotEditBody(BaseModel):
    what: str | None = None
    how: str | None = None
    duration_s: int | None = None
    clip_count: int | None = None


@router.patch("/{item_id}/shots/{shot_id}", response_model=PlanItemResponse)
async def edit_shot(
    item_id: str,
    shot_id: str,
    body: ShotEditBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Edit the text/metadata of one shot in the item's filming guide.

    Updates filming_guide in-place. shot_id is stable across the edit — attached
    clip_assignments remain bound to the shot. Sets user_edited=True so re-analysis
    knows the guide was touched by the user.
    """
    item = await _load_owned_item(item_id, user.id, db, for_update=True)

    guide = list(item.filming_guide or [])
    matched = False
    for shot in guide:
        if not isinstance(shot, dict):
            continue
        if str(shot.get("shot_id") or "") == shot_id:
            if body.what is not None:
                clean = _sanitize_text(body.what.strip())
                if clean:
                    shot["what"] = clean
            if body.how is not None:
                shot["how"] = _sanitize_text(body.how.strip())
            if body.duration_s is not None:
                shot["duration_s"] = max(1, min(60, int(body.duration_s)))
            if body.clip_count is not None:
                shot["clip_count"] = max(1, min(10, int(body.clip_count)))
            matched = True
            break

    if not matched:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown shot_id: {shot_id}",
        )

    item.filming_guide = guide
    item.user_edited = True
    await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


@router.post("/{item_id}/generate-guide", response_model=PlanItemResponse)
async def generate_guide(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Generate a fresh filming guide for an item with an empty guide.

    Uses the shot_list_writer agent (gemini-2.5-flash). Mints stable shot_ids on
    each new shot. Only allowed when the current guide is empty — existing guides
    are not overwritten (use the PATCH /shots/{shot_id} endpoint to edit them).
    Returns 409 if the item already has a filming guide.
    """
    import uuid as _uuid  # noqa: PLC0415

    from app.agents.shot_list_writer import (  # noqa: PLC0415
        ShotListWriterInput,
        run_shot_list_writer,
    )

    item, plan, _ = await _load_owned_item_context(item_id, user.id, db)
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)

    if item.filming_guide:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This item already has a filming guide. Use PATCH /shots/{shot_id} to edit it.",
        )

    inp = ShotListWriterInput(
        theme=item.theme or "",
        idea=item.idea or "",
        edit_format=str(item.edit_format or "montage"),
    )
    # Release the request transaction before the external model call. The
    # immutable snapshot above is accepted only if the same ownership epoch is
    # still current when we reacquire Plan -> Persona -> PlanItem below.
    await db.rollback()
    try:
        result = await asyncio.to_thread(run_shot_list_writer, inp)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Shot list generation failed. Please try again.",
        )

    if not result.shots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Shot list generation returned no shots. Please try again.",
        )

    # Re-check after Gemini (20s) under the shared mutation lock order. This
    # catches both a concurrent double-submit and an ownership quarantine/repair
    # that landed while the model was running.
    item, live_plan, _ = await _load_owned_item_context(
        item_id,
        user.id,
        db,
        for_update=True,
    )
    if int(getattr(live_plan, "ownership_epoch", 0) or 0) != ownership_epoch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        )
    if item.filming_guide:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This item already has a filming guide. Use PATCH /shots/{shot_id} to edit it.",
        )

    item.filming_guide = [{**s.model_dump(), "shot_id": _uuid.uuid4().hex} for s in result.shots]
    item.user_edited = True
    await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


class ClipNoteBody(BaseModel):
    gcs_path: str
    user_note: str = ""


@router.patch("/{item_id}/clips/note", response_model=PlanItemResponse)
async def set_clip_note(
    item_id: str,
    body: ClipNoteBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set/clear the creator's context note on one attached clip.

    Editing a note counts as the user touching the slot, so it also clears
    machine_matched. The conformance verdict is reset to a carry-over stub
    (contested flag only) and re-analysis is dispatched — the panel shows the
    checking state, never a stale verdict, while the judge re-reads the clip
    with the new context.
    """
    item, plan, _ = await _load_owned_item_context(
        item_id,
        user.id,
        db,
        for_update=True,
    )
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)
    note = (body.user_note or "")[:_MAX_NOTE_CHARS]

    assignments = list(item.clip_assignments or [])
    hit = False
    updated = []
    for a in assignments:
        entry = dict(a) if isinstance(a, dict) else {}
        if entry.get("gcs_path") == body.gcs_path:
            prior_note = str(entry.get("user_note") or "")
            entry["user_note"] = note
            entry["machine_matched"] = False
            if prior_note != note:
                from app.services.edit_proposals import mark_edit_proposal_stale  # noqa: PLC0415

                mark_edit_proposal_stale(item)
            hit = True
        updated.append(entry)
    if not hit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such clip on this item"
        )
    item.clip_assignments = updated

    # Carry only the contested flag through the re-run (suppression memory);
    # the old verdict itself must never render while the judge re-reads.
    prev = item.conformance or {}
    item.conformance = {"contested": True} if prev.get("contested") else None

    await db.commit()
    from app.tasks.conformance_build import analyze_item_conformance  # noqa: PLC0415

    analyze_item_conformance.delay(str(item.id), ownership_epoch)
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


class VoiceoverBody(BaseModel):
    voiceover_gcs_path: str | None = None


@router.patch("/{item_id}/voiceover", response_model=PlanItemResponse)
async def set_item_voiceover(
    item_id: str,
    body: VoiceoverBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Attach or clear the narrated-walkthrough voiceover for a plan item.

    The GCS path must be under the voiceover-uploads/ prefix (validated by the
    narrated archetype at generate time). Passing null clears a prior recording.
    No re-render is triggered — the user still needs to click Generate.
    """
    from app.routes.admin_music import _validate_voiceover_path  # noqa: PLC0415

    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    if body.voiceover_gcs_path is not None:
        try:
            _validate_voiceover_path(body.voiceover_gcs_path)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    item.voiceover_gcs_path = body.voiceover_gcs_path
    await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


class VoiceoverBedLevelBody(BaseModel):
    # 0.0 = voice only, 1.0 = original audio loudest. null → Kria's default level.
    voiceover_bed_level: float | None = None


@router.patch("/{item_id}/voiceover-bed-level", response_model=PlanItemResponse)
async def set_item_voiceover_bed_level(
    item_id: str,
    body: VoiceoverBedLevelBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set how loud the original clip audio plays under the narration.

    0.0 = voice only, 1.0 = loudest; null clears the override (Kria's default).
    Consumed at generate time (the footage bed is side-chain ducked under the
    voice). No re-render is triggered — the user still clicks Generate.
    """
    if body.voiceover_bed_level is not None and not (0.0 <= body.voiceover_bed_level <= 1.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="voiceover_bed_level must be between 0.0 and 1.0",
        )
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    item.voiceover_bed_level = body.voiceover_bed_level
    await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


class VoiceoverCaptionStyleBody(BaseModel):
    # "sentence" = sentence-block captions (default); "word" = one big word at a time
    # (qbuilder word-by-word). null clears the override (→ "sentence" at render time).
    voiceover_caption_style: str | None = None


_VOICEOVER_CAPTION_STYLES = frozenset({"sentence", "word"})


@router.patch("/{item_id}/voiceover-caption-style", response_model=PlanItemResponse)
async def set_item_voiceover_caption_style(
    item_id: str,
    body: VoiceoverCaptionStyleBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Choose how the narrated voiceover captions render.

    "sentence" = sentence-block captions (default); "word" = the qbuilder
    word-by-word look (one big word at a time). null clears the override (Kria's
    default, "sentence"). Consumed at generate time — no re-render is triggered.
    """
    if (
        body.voiceover_caption_style is not None
        and body.voiceover_caption_style not in _VOICEOVER_CAPTION_STYLES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='voiceover_caption_style must be "sentence" or "word"',
        )
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    item.voiceover_caption_style = body.voiceover_caption_style
    await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


@router.post("/{item_id}/conformance/dismiss", response_model=PlanItemResponse)
async def dismiss_conformance(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """'Hide this read' — persist the dismissal so the verdict never re-renders
    for this footage (a fresh attach nulls conformance and starts over)."""
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    if item.conformance:
        item.conformance = {**item.conformance, "dismissed": True}
        await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


@router.post("/{item_id}/conformance/contest", response_model=PlanItemResponse)
async def contest_conformance(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """'Looks wrong? Tell Kria' — mark the verdict contested. From here on,
    only high-confidence (≥0.8) verdicts may render on this footage."""
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    if item.conformance:
        item.conformance = {**item.conformance, "contested": True}
        await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


# ── Ask Kria (per-item filming advisor) ───────────────────────────────────────


class AdvisorTurnBody(BaseModel):
    # Caps bound the per-call Gemini prompt — one authenticated request can't
    # carry tens of thousands of turns into an unbounded prompt (cost abuse).
    # The agent is stateless per turn, so old turns past the window are droppable.
    answer: str = Field(default="", max_length=2000)
    # Full conversation so far: [{role: "agent"|"user", content: str}] — the
    # advisor is stateless per turn (same contract as /personas/agent/turn).
    prior_turns: list[dict] = Field(default_factory=list, max_length=40)


class AdvisorTurnResponse(BaseModel):
    reply: str
    suggestions: list[str] = []
    # Non-empty when the agent proposes re-reading a clip with this distilled
    # creator context; the frontend asks consent then PATCHes the clip note.
    suggested_note: str = ""


@router.post("/{item_id}/agent/turn", response_model=AdvisorTurnResponse)
async def plan_item_advisor_turn(
    item_id: str,
    body: AdvisorTurnBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AdvisorTurnResponse:
    """One "Ask Kria" turn on this item: which clip fits, what to film instead,
    or contesting the brief read. Read-only — advice, never writes."""
    from app.config import settings  # noqa: PLC0415

    if not settings.plan_item_advisor_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="advisor_not_enabled")

    item = await _load_owned_item(item_id, user.id, db)

    # Persona context is optional data behind a mandatory ownership boundary.
    persona_summary = ""
    content_mode = "create_new"
    _, persona_row = await _load_item_plan_persona(item, db)
    if isinstance(persona_row.persona, dict):
        persona_summary = str(persona_row.persona.get("summary") or "")
        content_mode = str(persona_row.persona.get("content_mode") or "create_new")

    # Clips block: filename (uuid prefix stripped), slot label, creator note.
    shot_label_by_id = {
        s.get("shot_id"): f"shot {i + 1}"
        for i, s in enumerate(item.filming_guide or [])
        if isinstance(s, dict) and s.get("shot_id")
    }
    clips = []
    for a in item.clip_assignments or []:
        if not isinstance(a, dict) or not a.get("gcs_path"):
            continue
        raw_name = str(a["gcs_path"]).rsplit("/", 1)[-1]
        filename = raw_name.split("-", 1)[1] if "-" in raw_name else raw_name
        clips.append(
            {
                "filename": filename,
                "shot_label": shot_label_by_id.get(a.get("shot_id"), "extra footage"),
                "user_note": str(a.get("user_note") or ""),
            }
        )

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.interviewer_agent import ConversationTurn  # noqa: PLC0415
    from app.agents.plan_item_advisor import (  # noqa: PLC0415
        PlanItemAdvisorAgent,
        PlanItemAdvisorInput,
    )

    turns = [
        ConversationTurn(role=str(t.get("role", "user")), content=str(t.get("content", "")))
        for t in body.prior_turns
        if isinstance(t, dict) and t.get("content")
    ]
    if body.answer.strip():
        turns.append(ConversationTurn(role="user", content=body.answer.strip()))

    agent_input = PlanItemAdvisorInput(
        turns=turns,
        theme=str(item.theme or ""),
        idea=str(item.idea or ""),
        edit_format=str(getattr(item, "edit_format", "") or "montage"),
        filming_guide=list(item.filming_guide or []),
        clips=clips,
        conformance=item.conformance if isinstance(item.conformance, dict) else None,
        job_phase=derive_item_status(item),
        persona_summary=persona_summary,
        content_mode=content_mode,
    )

    try:
        result = await asyncio.to_thread(PlanItemAdvisorAgent(default_client()).run, agent_input)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_item_advisor.failed", item_id=item_id, error=str(exc)[:300])
        return AdvisorTurnResponse(
            reply=(
                "I couldn't think that through just now — try asking again. "
                "You can always generate with what you have."
            ),
            suggestions=["Which clip fits shot 1?", "What should I film instead?"],
        )

    return AdvisorTurnResponse(
        reply=result.reply,
        suggestions=result.suggestions,
        suggested_note=result.suggested_note,
    )


def _require_guided_edit() -> None:
    if not settings.guided_edit_capability_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plan edit is not available.",
        )


def _require_guided_edit_conversation() -> None:
    _require_guided_edit()
    if not settings.guided_edit_conversation_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversational Plan edit is not available.",
        )


def _edit_conversation_rate_key(request: Request) -> str:
    """Key paid edit-guide calls by the authenticated proxy identity."""

    user_id = request.headers.get("x-user-id", "").strip()
    if user_id:
        return f"user:{user_id[:128]}"
    return f"ip:{get_real_ip(request)}"


class DraftEditProposalBody(ProposalBrief):
    pass


class EditGuideTurnBody(BaseModel):
    expected_proposal_version: int = Field(default=0, ge=0)
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def message_must_have_words(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message cannot be blank")
        return value


class UpdateEditProposalBody(BaseModel):
    expected_proposal_version: int = Field(ge=1)
    snapshot: EditProposalSnapshot


class ApproveEditProposalBody(BaseModel):
    expected_proposal_version: int = Field(ge=1)


async def _edit_guide_media_summary(
    item: PlanItem,
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[dict]:
    """Return bounded, non-identity media context for the conversation agent."""

    summaries: list[dict] = []
    seen_paths: set[str] = set()
    for raw in item.clip_assignments or []:
        if not isinstance(raw, dict) or not raw.get("gcs_path"):
            continue
        path = str(raw["gcs_path"])
        analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
        kind = str(raw.get("kind") or "")
        if kind not in {"image", "video"}:
            image_suffixes = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
            kind = "image" if path.lower().endswith(image_suffixes) else "video"
        summaries.append(
            {
                "kind": kind,
                "source_filename": path.rsplit("/", 1)[-1].split("-", 1)[-1][:160],
                "creator_context": str(raw.get("user_note") or "")[:500],
                "subject": str(analysis.get("subject") or "")[:300],
                "description": str(analysis.get("description") or "")[:500],
            }
        )
        seen_paths.add(path)

    assets = list(
        (
            await db.execute(
                select(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.user_id == user_id,
                    PlanItemAsset.status == "ready",
                )
                .order_by(PlanItemAsset.created_at)
            )
        ).scalars()
    )
    for asset in assets:
        if asset.gcs_path in seen_paths:
            continue
        analysis = asset.analysis if isinstance(asset.analysis, dict) else {}
        summaries.append(
            {
                "kind": "image" if asset.kind == "image" else "video",
                "source_filename": str(asset.source_filename or "")[:160],
                "creator_context": str(asset.user_context or "")[:500],
                "subject": str(analysis.get("subject") or "")[:300],
                "description": str(analysis.get("description") or "")[:500],
            }
        )
    limited = summaries[:60]
    for index, summary in enumerate(limited, start=1):
        summary["media_ref"] = f"media_{index}"
    return limited


def _snapshot_from_edit_guide_revision(current, revision) -> EditProposalSnapshot:  # noqa: ANN001
    """Rejoin AI aliases with server-owned beat and media identities."""

    beat_by_id = {beat.beat_id: beat for beat in current.story_beats}
    media_id_by_ref = {
        f"media_{index}": ref.media_id for index, ref in enumerate(current.media, start=1)
    }
    beats: list[StoryBeat] = []
    for revised in revision.story_beats:
        existing = beat_by_id[revised.beat_id]
        # Creator-authored wording is authoritative. Conversational revisions
        # can reorganize the chapter, but never silently rewrite that wording.
        keep_user_thought = existing.thought_source == "user"
        beats.append(
            StoryBeat(
                beat_id=existing.beat_id,
                topic=revised.topic,
                thought=existing.thought if keep_user_thought else revised.thought,
                thought_source="user" if keep_user_thought else "ai_draft",
                media_ids=[media_id_by_ref[media_ref] for media_ref in revised.media_refs]
                if revised.media_refs
                else existing.media_ids,
                layout=revised.layout,
                duration_s=revised.duration_s,
            )
        )
    return EditProposalSnapshot(
        direction=revision.direction,
        goal=revision.goal,
        pace=revision.pace,
        duration_s=revision.duration_s,
        title=revision.title,
        media=current.media,
        story_beats=beats,
    )


def _proposal_http_conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


_PROPOSAL_GENERATE_MESSAGES = {
    "proposal_required": "Plan this edit before generating.",
    "proposal_draft": "Approve the edit plan before generating.",
    "proposal_stale": "Your media changed. Plan the edit again before generating.",
    "proposal_analyzing": "Kria is still planning this edit.",
    # Was unmapped and fell through to "proposal_draft"'s generic "Approve the
    # edit plan before generating" — misleading for a plan that was never
    # drafted (2026-08 guided-auto-design incident). See
    # proposal_generate_error() in services/edit_proposals.py.
    "proposal_failed": "Kria couldn't finish planning this edit — open the planner to try again.",
}


def _raise_proposal_generate_conflict(code: str) -> NoReturn:
    raise _proposal_http_conflict(code, _PROPOSAL_GENERATE_MESSAGES[code])


async def _maybe_auto_design_generate(
    item_id: str,
    item: PlanItem,
    plan: ContentPlan,
    user: CurrentUser,
    db: AsyncSession,
) -> PlanItemResponse | None:
    """GUIDED_AUTO_DESIGN_ENABLED: reserve+draft instead of 409ing Generate.

    Product decision (2026-08-18): asking the creator for direction stays
    optional — if they never open the planner, Kria designs the edit and
    Generate still works in one click, as long as media exists. Returns None
    when the item has no media at all (nothing to auto-design — the caller
    falls through to the ordinary 409) or when the flag is off. Returns a 200
    PlanItemResponse otherwise: idempotent if an attempt is already in flight,
    or a freshly reserved auto-design attempt (draft_edit_proposal itself
    dispatches the render after it auto-approves — see
    _dispatch_after_auto_design in tasks/edit_proposal_build.py).
    """

    from app.config import settings  # noqa: PLC0415

    if not settings.guided_auto_design_enabled:
        return None
    if not (item.clip_gcs_paths or []):
        ready_assets = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlanItemAsset)
                    .where(
                        PlanItemAsset.plan_item_id == item.id,
                        PlanItemAsset.status == "ready",
                    )
                )
            ).scalar_one()
        )
        if ready_assets == 0:
            return None  # nothing to design from — fall through to the normal 409

    from app.schemas.edit_proposal import ProposalFailure  # noqa: PLC0415
    from app.services.edit_proposals import begin_proposal_attempt  # noqa: PLC0415
    from app.services.plan_clips import ensure_clip_media_ids  # noqa: PLC0415
    from app.tasks.edit_proposal_build import draft_edit_proposal  # noqa: PLC0415

    owner_id = user.id
    locked = await _load_owned_item(item_id, owner_id, db, for_update=True)
    current = parse_edit_proposal(locked.edit_proposal)
    if current and current.status in {"analyzing", "drafting"}:
        # An attempt (auto or manual) is already in flight — idempotent
        # no-op, never mint a duplicate attempt/task.
        await db.rollback()
        instruction_level = await _get_instruction_level(locked, db)
        return plan_item_response(locked, instruction_level=instruction_level)

    ensure_clip_media_ids(locked)
    proposal = begin_proposal_attempt(locked, approval_mode="auto")
    await db.commit()
    try:
        draft_edit_proposal.apply_async(
            args=[
                str(locked.id),
                proposal.generation_attempt_id,
                int(getattr(plan, "ownership_epoch", 0) or 0),
            ],
            kwargs={"auto_finalize": True},
            queue=settings.pool_asset_analysis_queue,
        )
    except Exception as exc:  # noqa: BLE001
        relocked = await _load_owned_item(item_id, owner_id, db, for_update=True)
        relocked_current = parse_edit_proposal(relocked.edit_proposal)
        if (
            relocked_current
            and relocked_current.generation_attempt_id == proposal.generation_attempt_id
        ):
            failed = relocked_current.model_copy(
                update={
                    "proposal_version": relocked_current.proposal_version + 1,
                    "status": "failed",
                    "failure": ProposalFailure(
                        code="proposal_dispatch_failed",
                        message="Kria couldn't start planning this edit. Try again.",
                        retryable=True,
                    ),
                }
            )
            relocked.edit_proposal = failed.model_dump(mode="json")
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "proposal_dispatch_failed",
                "message": "Kria couldn't start planning this edit. Try again.",
            },
        ) from exc
    reloaded = await _load_owned_item(item_id, owner_id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


async def _proposal_media_is_current(
    item: PlanItem,
    snapshot: EditProposalSnapshot,
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> bool:
    """Revalidate ownership + exact object identity before edit or approval."""

    from app.services.edit_proposals import (  # noqa: PLC0415
        asset_ref_matches,
        clip_ref_matches,
    )

    clip_by_id = {
        str(a.get("media_id")): a
        for a in (item.clip_assignments or [])
        if isinstance(a, dict) and a.get("media_id") and a.get("gcs_path")
    }
    asset_refs = [ref for ref in snapshot.media if ref.lane == "asset"]
    asset_by_id: dict[str, PlanItemAsset] = {}
    if asset_refs:
        try:
            asset_uuids = [uuid.UUID(ref.media_id) for ref in asset_refs]
        except ValueError:
            return False
        rows = (
            (
                await db.execute(
                    select(PlanItemAsset).where(
                        PlanItemAsset.id.in_(asset_uuids),
                        PlanItemAsset.plan_item_id == item.id,
                        PlanItemAsset.user_id == user_id,
                        PlanItemAsset.status == "ready",
                    )
                )
            )
            .scalars()
            .all()
        )
        asset_by_id = {str(row.id): row for row in rows}

    for ref in snapshot.media:
        if ref.lane == "clip":
            assignment = clip_by_id.get(ref.media_id)
            if not clip_ref_matches(ref, assignment):
                return False
        else:
            asset = asset_by_id.get(ref.media_id)
            if not asset_ref_matches(ref, asset):
                return False
    if snapshot.media:
        semaphore = asyncio.Semaphore(8)

        async def generation_matches(ref) -> bool:  # noqa: ANN001
            async with semaphore:
                try:
                    metadata = await asyncio.to_thread(storage.object_metadata, ref.gcs_path)
                except Exception:  # noqa: BLE001 - missing/replaced media is stale
                    return False
                return metadata.generation == ref.generation

        if not all(await asyncio.gather(*(generation_matches(ref) for ref in snapshot.media))):
            return False
    return True


@router.post("/{item_id}/edit-proposal/conversation", response_model=PlanItemResponse)
@limiter.limit("12/minute", key_func=_edit_conversation_rate_key)
async def edit_proposal_conversation_turn(
    request: Request,
    item_id: str,
    body: EditGuideTurnBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Turn natural-language direction into a brief or a reviewable draft revision."""

    _ = request
    _require_guided_edit_conversation()
    owner_id = user.id
    item = await _load_owned_item(item_id, owner_id, db, for_update=True)
    current = parse_edit_proposal(item.edit_proposal)
    from app.services.edit_proposals import (  # noqa: PLC0415
        ProposalConflictError,
        release_edit_conversation_attempt,
        require_edit_conversation_attempt,
        reserve_edit_conversation_attempt,
        save_edit_conversation_turn,
    )

    # Mirror draft_item_edit_proposal's media gate: talking to Kria about an
    # edit with nothing uploaded yet burns a model call for advice the item
    # page can't act on. Registered assets still finishing their own analysis
    # count as media (they'll be ready by the time the creator approves). Only
    # queries the pool when clip_assignments is empty — the common case never
    # pays for the extra round trip.
    if not (item.clip_assignments or []):
        ready_assets = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlanItemAsset)
                    .where(
                        PlanItemAsset.plan_item_id == item.id,
                        PlanItemAsset.status.in_({"queued", "analyzing", "ready"}),
                    )
                )
            ).scalar_one()
        )
        if ready_assets == 0:
            raise _proposal_http_conflict(
                "media_required", "Add a photo or video first — then tell Kria what to make."
            )

    try:
        _reserved, conversation_token = reserve_edit_conversation_attempt(
            item,
            expected_version=body.expected_proposal_version,
        )
    except ProposalConflictError as exc:
        raise _proposal_http_conflict("proposal_conflict", str(exc)) from exc

    review_snapshot = (
        current.draft
        if current and current.status in {"draft", "approved"} and current.draft
        else None
    )
    phase = "review" if review_snapshot else "briefing"
    brief = (
        ProposalBrief(
            direction=review_snapshot.direction,
            goal=review_snapshot.goal,
            pace=review_snapshot.pace,
            duration_s=review_snapshot.duration_s,
        )
        if review_snapshot
        else (current.brief if current else ProposalBrief())
    )
    conversation = list(current.conversation) if current else []
    media_summary = await _edit_guide_media_summary(item, db, user_id=owner_id)
    idea = str(item.idea or "")
    theme = str(item.theme or "")
    # Persist the single-flight token, then release the row lock before Gemini
    # starts. Duplicate requests stop here without consuming another model call.
    await db.commit()

    async def release_reservation() -> None:
        locked_item = await _load_owned_item(item_id, owner_id, db, for_update=True)
        if release_edit_conversation_attempt(locked_item, token=conversation_token):
            await db.commit()
        else:
            await db.rollback()

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.edit_guide import (  # noqa: PLC0415
        EditGuideAgent,
        EditGuideBeatInput,
        EditGuideInput,
        EditGuideMediaSummary,
    )
    from app.schemas.edit_proposal import EDIT_CONVERSATION_MAX_TURNS  # noqa: PLC0415

    # Persisted history is user/agent pairs. Keep whole exchanges rather than
    # starting the prompt with an orphaned assistant reply at the window edge.
    turns = conversation[-(EDIT_CONVERSATION_MAX_TURNS - 2) :]
    turns.append({"role": "user", "phase": phase, "content": body.message.strip()})
    try:
        result = await asyncio.to_thread(
            EditGuideAgent(default_client()).run,
            EditGuideInput(
                phase=phase,
                idea=idea,
                theme=theme,
                turns=turns,
                brief=brief,
                media=[EditGuideMediaSummary.model_validate(row) for row in media_summary],
                title=review_snapshot.title if review_snapshot else "",
                beats=(
                    [
                        EditGuideBeatInput(
                            beat_id=beat.beat_id,
                            topic=beat.topic,
                            thought=beat.thought,
                            thought_source=beat.thought_source,
                            layout=beat.layout,
                            duration_s=beat.duration_s,
                            media_count=len(beat.media_ids),
                            media_refs=[
                                f"media_{index}"
                                for index, ref in enumerate(review_snapshot.media, start=1)
                                if ref.media_id in beat.media_ids
                            ],
                        )
                        for beat in review_snapshot.story_beats
                    ]
                    if review_snapshot
                    else []
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - conversational failure is retryable
        log.warning("edit_guide.failed", item_id=item_id, error=str(exc)[:300])
        await release_reservation()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "edit_guide_failed",
                "message": "Kria couldn't think that through. Your words were not lost—try again.",
            },
        ) from exc

    revised_snapshot = None
    # A clarifying review answer cannot mutate the brief independently from
    # the approved/draft render contract. Only a complete revision may do so.
    saved_brief = brief if review_snapshot else result.brief
    if review_snapshot and result.revision:
        revised_snapshot = _snapshot_from_edit_guide_revision(review_snapshot, result.revision)
        from app.pipeline.guided_story import (  # noqa: PLC0415
            validate_proposal_timing,
        )

        try:
            validate_proposal_timing(revised_snapshot)
        except Exception as exc:  # noqa: BLE001 - every invalid revision must release its fence
            log.warning("edit_guide.timing_invalid", item_id=item_id, error=str(exc)[:300])
            await release_reservation()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "edit_guide_failed",
                    "message": "Kria proposed timing that couldn't render safely. Try again.",
                },
            ) from exc
        saved_brief = ProposalBrief(
            direction=revised_snapshot.direction,
            goal=revised_snapshot.goal,
            pace=revised_snapshot.pace,
            duration_s=revised_snapshot.duration_s,
        )

    locked = await _load_owned_item(item_id, owner_id, db, for_update=True)

    try:
        reserved_current = require_edit_conversation_attempt(locked, token=conversation_token)
        save_edit_conversation_turn(
            locked,
            expected_version=reserved_current.proposal_version,
            brief=saved_brief,
            user_message=body.message,
            agent_reply=result.reply,
            suggestions=result.suggestions,
            ready_to_plan=result.ready_to_plan,
            conversation_phase=phase,
            revised_snapshot=revised_snapshot,
        )
    except ProposalConflictError as exc:
        if release_edit_conversation_attempt(locked, token=conversation_token):
            await db.commit()
        else:
            await db.rollback()
        raise _proposal_http_conflict("proposal_conflict", str(exc)) from exc
    await db.commit()
    reloaded = await _load_owned_item(item_id, owner_id, db)
    return plan_item_response(reloaded)


@router.post(
    "/{item_id}/edit-proposal/draft",
    response_model=PlanItemResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("3/minute", key_func=get_real_ip)
async def draft_item_edit_proposal(
    request: Request,
    item_id: str,
    body: DraftEditProposalBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Start one token-fenced analysis + proposal attempt for all item media."""

    _ = request
    _require_guided_edit()
    item, plan, _persona = await _load_owned_item_context(item_id, user.id, db, for_update=True)
    current = parse_edit_proposal(item.edit_proposal)
    if current and current.conversation_attempt is not None:
        from app.services.edit_proposals import (  # noqa: PLC0415
            EDIT_CONVERSATION_ATTEMPT_TTL_S,
            release_edit_conversation_attempt,
        )

        attempt = current.conversation_attempt
        age_s = max(0.0, (datetime.now(UTC) - attempt.started_at).total_seconds())
        if age_s >= EDIT_CONVERSATION_ATTEMPT_TTL_S:
            release_edit_conversation_attempt(item, token=attempt.token)
            await db.commit()
            raise _proposal_http_conflict(
                "edit_guide_retry_required",
                "Kria's reply took too long. Send your direction again before building the plan.",
            )
        raise _proposal_http_conflict(
            "edit_guide_in_progress",
            "Kria is still thinking about your direction.",
        )
    if current and current.status in {"analyzing", "drafting"}:
        # Double-clicks and retries with a lost response converge on the same
        # attempt instead of publishing another expensive analysis task.
        return plan_item_response(item)
    ready_assets = int(
        (
            await db.execute(
                select(func.count())
                .select_from(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.status.in_({"queued", "analyzing", "ready"}),
                )
            )
        ).scalar_one()
    )
    if not (item.clip_assignments or []) and ready_assets == 0:
        raise _proposal_http_conflict("proposal_required", "Upload media before planning an edit.")

    from app.schemas.edit_proposal import ProposalFailure  # noqa: PLC0415
    from app.services.edit_proposals import begin_proposal_attempt  # noqa: PLC0415
    from app.services.plan_clips import ensure_clip_media_ids  # noqa: PLC0415
    from app.tasks.edit_proposal_build import draft_edit_proposal  # noqa: PLC0415

    ensure_clip_media_ids(item)
    proposal = begin_proposal_attempt(item, brief=ProposalBrief.model_validate(body.model_dump()))
    await db.commit()
    try:
        draft_edit_proposal.apply_async(
            args=[
                str(item.id),
                proposal.generation_attempt_id,
                int(getattr(plan, "ownership_epoch", 0) or 0),
            ],
            queue=settings.pool_asset_analysis_queue,
        )
    except Exception as exc:  # noqa: BLE001
        locked = await _load_owned_item(item_id, user.id, db, for_update=True)
        current = parse_edit_proposal(locked.edit_proposal)
        if current and current.generation_attempt_id == proposal.generation_attempt_id:
            failed = current.model_copy(
                update={
                    "proposal_version": current.proposal_version + 1,
                    "status": "failed",
                    "failure": ProposalFailure(
                        code="proposal_dispatch_failed",
                        message="Kria couldn't start planning this edit. Try again.",
                        retryable=True,
                    ),
                }
            )
            locked.edit_proposal = failed.model_dump(mode="json")
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "proposal_dispatch_failed",
                "message": "Kria couldn't start planning this edit. Try again.",
            },
        ) from exc
    reloaded = await _load_owned_item(item_id, user.id, db)
    return plan_item_response(reloaded)


@router.patch("/{item_id}/edit-proposal", response_model=PlanItemResponse)
async def update_item_edit_proposal(
    item_id: str,
    body: UpdateEditProposalBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Save user corrections without allowing media identity substitution."""

    _require_guided_edit()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    from app.schemas.edit_proposal import canonical_media_digest  # noqa: PLC0415
    from app.services.edit_proposals import (  # noqa: PLC0415
        ProposalConflictError,
        mark_edit_proposal_stale,
        save_proposal_draft,
    )

    current = parse_edit_proposal(item.edit_proposal)
    if (
        current is None
        or current.draft is None
        or current.status not in {"draft", "approved"}
        or current.media_digest != canonical_media_digest(body.snapshot.media)
    ):
        raise _proposal_http_conflict(
            "proposal_stale", "The uploaded media no longer matches this edit plan."
        )
    if not await _proposal_media_is_current(item, body.snapshot, db, user_id=user.id):
        mark_edit_proposal_stale(item)
        await db.commit()
        raise _proposal_http_conflict(
            "proposal_stale", "The uploaded media changed. Plan the edit again."
        )
    try:
        save_proposal_draft(
            item,
            expected_version=body.expected_proposal_version,
            # Media metadata is server-owned. The client may edit the story,
            # text, ordering, and layout, but cannot smuggle arbitrary analysis
            # or context into the approved render snapshot.
            snapshot=body.snapshot.model_copy(update={"media": current.draft.media}),
        )
    except ProposalConflictError as exc:
        raise _proposal_http_conflict("proposal_conflict", str(exc)) from exc
    await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    return plan_item_response(reloaded)


@router.post("/{item_id}/edit-proposal/approve", response_model=PlanItemResponse)
async def approve_item_edit_proposal(
    item_id: str,
    body: ApproveEditProposalBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Approve only the current, exact-media draft under the item lock."""

    _require_guided_edit()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    from app.services.edit_proposals import (  # noqa: PLC0415
        ProposalConflictError,
        approve_proposal,
        mark_edit_proposal_stale,
    )

    current = parse_edit_proposal(item.edit_proposal)
    if current is None or current.draft is None:
        raise _proposal_http_conflict("proposal_draft", "Plan the edit before approving it.")
    if current.status == "stale":
        raise _proposal_http_conflict(
            "proposal_stale", "The uploaded media changed. Plan the edit again."
        )
    if current.status != "draft":
        raise _proposal_http_conflict("proposal_draft", "Review the current draft before approval.")
    if not await _proposal_media_is_current(item, current.draft, db, user_id=user.id):
        mark_edit_proposal_stale(item)
        await db.commit()
        raise _proposal_http_conflict(
            "proposal_stale", "The uploaded media changed. Plan the edit again."
        )
    try:
        approve_proposal(item, expected_version=body.expected_proposal_version)
    except ProposalConflictError as exc:
        raise _proposal_http_conflict("proposal_conflict", str(exc)) from exc
    await db.commit()
    reloaded = await _load_owned_item(item_id, user.id, db)
    return plan_item_response(reloaded)


@router.post("/{item_id}/generate", response_model=PlanItemResponse)
async def generate_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Enqueue a render from attached clips or an approved guided story."""
    item, plan, _ = await _load_owned_item_context(item_id, user.id, db)
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)
    # Narrated walkthroughs are spined by narration. With self-narration OFF that
    # means a recorded voiceover — block generation until one is attached (without it
    # the job silently falls back to montage: the "started a narrated render with no
    # audio" dogfood bug). With self-narration ON the footage's own audio may carry
    # the voice, so dispatch proceeds and _resolve_archetype routes by speech; a
    # no-speech clip set falls back to montage WITH a persisted, user-visible reason.
    from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415

    if settings.guided_edit_enforcement_enabled:
        from app.services.edit_proposals import proposal_generate_error  # noqa: PLC0415

        if proposal_error := proposal_generate_error(item):
            auto_response = await _maybe_auto_design_generate(item_id, item, plan, user, db)
            if auto_response is not None:
                return auto_response
            _raise_proposal_generate_conflict(proposal_error)

    approved_guided_media = False
    if settings.guided_edit_capability_enabled or settings.guided_edit_enforcement_enabled:
        proposal = parse_edit_proposal(item.edit_proposal)
        approved_guided_media = bool(
            proposal
            and proposal.status == "approved"
            and proposal.last_approved
            and any(beat.media_ids for beat in proposal.last_approved.snapshot.story_beats)
        )

    if (
        (item.edit_format or "") in NARRATED_EDIT_FORMATS
        and not item.voiceover_gcs_path
        and not settings.narrated_self_narration_enabled
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record or upload your voiceover before generating a Voiceover edit",
        )
    if not (item.clip_gcs_paths or []) and not approved_guided_media:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload at least one clip before generating",
        )
    if not _item_uses_collage_preset(item) and any(
        _is_image_clip_path(p) for p in (item.clip_gcs_paths or [])
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photos require a collage preset",
        )
    from app.tasks.content_plan_build import (  # noqa: PLC0415
        dispatch_item_render_for,
        generate_plan_item_videos,
    )

    if not settings.PLAN_SYNC_DISPATCH_ENABLED:
        # Kill-switch fallback (plans/014) — byte-identical legacy contract:
        # 409 off derived status, Job minted asynchronously by the task, the
        # frontend waits out its registration window.
        if derive_item_status(item) == "generating":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A render is already in progress for this item",
            )
        # Keep the legacy producer fail-closed immediately before publication;
        # the worker repeats this check when it consumes the message.
        await _load_item_plan_persona(item, db)
        generate_plan_item_videos.delay(str(item.id), ownership_epoch)
        reloaded = await _load_owned_item(item_id, user.id, db)
        instruction_level = await _get_instruction_level(reloaded, db)
        return plan_item_response(reloaded, instruction_level=instruction_level)

    # Sync dispatch (plans/014): mint the Job in-request so derive_item_status
    # flips to "generating" before the response — the page's immediate refetch
    # sees a registered render instead of minutes of frozen "Starting…" while
    # a dispatch task waits behind the single-slot render worker.
    # No pre-flight "already generating" 409 here: the helper's FOR-UPDATE
    # re-check owns that race, and an active render maps to an idempotent 200
    # (D5) so a lost-response retry self-heals instead of stranding the user
    # on an error banner while the render actually runs.
    from anyio import to_thread  # noqa: PLC0415

    # Capture BEFORE db.expire_all(): `user` is an ORM row loaded on THIS
    # request session (get_current_user shares the cached get_db dependency),
    # so a post-expire attribute access would lazy-refresh synchronously inside
    # the async handler → MissingGreenlet 500 on every successful generate
    # (review 2026-08-04, performance P1).
    owner_id = user.id
    result = await to_thread.run_sync(
        dispatch_item_render_for,
        str(item.id),
        ownership_epoch,
    )
    if result.outcome == "missing_row":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    if result.outcome == "invalid_persona":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        )
    if result.outcome == "invalid_clips":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Your clips couldn't be validated — re-upload them and try again",
        )
    if result.outcome == "publish_failed":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The render couldn't be queued — give it another go",
        )
    if result.outcome in {
        "proposal_required",
        "proposal_draft",
        "proposal_stale",
        "proposal_analyzing",
        "proposal_failed",
    }:
        _raise_proposal_generate_conflict(result.outcome)
    if result.outcome not in ("dispatched", "already_active"):
        # A future/unknown outcome must never read as success (review CA3/M1) —
        # the silent-no-op class this whole feature exists to kill.
        log.error("plan_item_generate.unexpected_outcome", outcome=result.outcome)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Generation failed unexpectedly — try again",
        )
    # dispatched | already_active → 200 with the item's current state. The
    # helper committed on a SEPARATE sync session; expire this async session's
    # identity map or the reload serves the pre-dispatch row and the response
    # misses the fresh current_job_id (plans/014 A2).
    db.expire_all()
    reloaded = await _load_owned_item(item_id, owner_id, db)
    instruction_level = await _get_instruction_level(reloaded, db)
    return plan_item_response(reloaded, instruction_level=instruction_level)


# ── Per-variant editing (swap song / edit text / change style) ────────────────
# The render job behind a plan item is a generative-mode Job, so each variant can
# be re-rendered exactly like a public generative edit. These endpoints add only
# ownership enforcement (`_load_owned_item`) + job resolution on top of the shared
# validate-and-dispatch helpers in `routes/generative_jobs.py` — the validation
# rules and the `regenerate_generative_variant` dispatch stay single-sourced there.
# Mutation is reachable ONLY here (authenticated, per-user), never on the public
# unauthenticated `/generative-jobs` surface.


async def _owned_item_render_job(item_id: str, user_id: uuid.UUID, db: AsyncSession) -> Job:
    """Return a fresh, user-owned render Job and hide cancelled tombstones."""
    item = await _load_owned_item(item_id, user_id, db)
    job_id = item.current_job_id or (item.current_job.id if item.current_job is not None else None)
    job = await db.get(Job, job_id, populate_existing=True) if job_id is not None else None
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    if job.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancelled videos cannot be edited.",
        )
    return job


async def _locked_owned_item_render_job(item_id: str, user_id: uuid.UUID, db: AsyncSession) -> Job:
    """Lock Plan -> Persona -> PlanItem -> Job, then recheck cancellation."""
    item, _, _ = await _load_owned_item_context(item_id, user_id, db, for_update=True)
    job_id = item.current_job_id or (item.current_job.id if item.current_job is not None else None)
    locked = (
        await db.get(
            Job,
            job_id,
            populate_existing=True,
            with_for_update=True,
        )
        if job_id is not None
        else None
    )
    if locked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    if locked.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancelled videos cannot be edited.",
        )
    return locked


class SpeechCutActionBody(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)


class SpeechCutDispatchResponse(BaseModel):
    status: Literal["rendering"] = "rendering"
    request: dict


@router.post(
    "/{item_id}/variants/{variant_id}/speech-cuts/{candidate_id}/apply",
    response_model=SpeechCutDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_plan_item_speech_cut(
    item_id: str,
    variant_id: str,
    candidate_id: str,
    body: SpeechCutActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SpeechCutDispatchResponse:
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    request, enqueue = dispatch_apply_speech_cut_candidate(
        job,
        variant_id,
        candidate_id=candidate_id,
        expected_revision=body.expected_revision,
    )
    await db.commit()
    try:
        enqueue()
    except Exception as exc:  # noqa: BLE001 — committed dispatch must be reversible
        result = await db.execute(select(Job).where(Job.id == job.id).with_for_update())
        fresh_job = result.scalar_one_or_none()
        if fresh_job is not None and fresh_job.status != "cancelled":
            rollback_speech_cut_dispatch(
                fresh_job,
                str(exc),
                expected_operation_id=str(request.get("operation_id") or ""),
            )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The cut could not be queued. The current video is unchanged.",
        ) from exc
    return SpeechCutDispatchResponse(request=request)


@router.post(
    "/{item_id}/variants/{variant_id}/speech-cuts/restore",
    response_model=SpeechCutDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restore_plan_item_speech_timing(
    item_id: str,
    variant_id: str,
    body: SpeechCutActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SpeechCutDispatchResponse:
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    request, enqueue = dispatch_restore_original_timing(
        job, variant_id, expected_revision=body.expected_revision
    )
    await db.commit()
    try:
        enqueue()
    except Exception as exc:  # noqa: BLE001 — committed dispatch must be reversible
        result = await db.execute(select(Job).where(Job.id == job.id).with_for_update())
        fresh_job = result.scalar_one_or_none()
        if fresh_job is not None and fresh_job.status != "cancelled":
            rollback_speech_cut_dispatch(
                fresh_job,
                str(exc),
                expected_operation_id=str(request.get("operation_id") or ""),
            )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The timing restore could not be queued. The current video is unchanged.",
        ) from exc
    return SpeechCutDispatchResponse(request=request)


@router.post("/{item_id}/variants/{variant_id}/swap-song", response_model=PlanItemResponse)
async def swap_item_song(
    item_id: str,
    variant_id: str,
    req: SwapSongRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants against a different library song."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await dispatch_swap_song(job, variant_id, new_track_id=req.new_track_id, db=db)
    log.info("plan_item_swap_song", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/retext", response_model=PlanItemResponse)
async def retext_item(
    item_id: str,
    variant_id: str,
    req: RetextRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants with new intro text, or remove it."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    pending_publish = dispatch_retext(
        job,
        variant_id,
        text=req.text,
        remove=req.remove,
        publish=False,
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info("plan_item_retext", item_id=item_id, variant_id=variant_id, remove=req.remove)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.patch("/{item_id}/variants/{variant_id}/captions", response_model=PlanItemResponse)
async def edit_item_captions(
    item_id: str,
    variant_id: str,
    req: CaptionsRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Persist hand-edited caption cues for a narrated variant (no re-render).

    The on-video editor calls this as the creator types — the change is instant
    (the player overlays the cues). Apply (`/captions/apply`) reburns them.
    """
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await persist_variant_captions(job.id, variant_id, req.cues, db)
    log.info("plan_item_edit_captions", item_id=item_id, variant_id=variant_id, cues=len(req.cues))
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/captions/apply", response_model=PlanItemResponse)
async def apply_item_captions(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Reburn the variant's edited captions onto its caption-free base (async)."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    # dispatch_apply_captions does its OWN row-locked re-fetch by job.id and commits
    # the rendering gate BEFORE enqueuing (R1-1): the reburn's start write is
    # token-checked, so a worker dequeuing before the commit would read the old
    # generation, discard its start write, and strand the variant in "rendering".
    await dispatch_apply_captions(job.id, variant_id, db=db)
    log.info("plan_item_apply_captions", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.patch("/{item_id}/variants/{variant_id}/caption-font", response_model=PlanItemResponse)
async def set_item_caption_font(
    item_id: str,
    variant_id: str,
    req: CaptionFontRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set the caption font for a narrated variant (no re-render).

    Applies to both sentence and word-by-word captions. The on-video editor previews
    the font locally; Apply (`/captions/apply`) reburns in the chosen font. ``null``
    resets to the default. Unknown fonts are rejected (422).
    """
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await persist_variant_caption_font(job.id, variant_id, req.caption_font, db)
    log.info(
        "plan_item_set_caption_font",
        item_id=item_id,
        variant_id=variant_id,
        font=req.caption_font,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/caption-position", response_model=PlanItemResponse)
async def set_item_caption_position(
    item_id: str,
    variant_id: str,
    req: CaptionPositionRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set caption vertical position and reburn the captioned variant (async)."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    # The dispatcher row-locks, commits the margin + gen mint, then enqueues
    # (R1-1 commit-before-enqueue) — no route-side commit needed.
    margin_v = await dispatch_set_caption_position(job.id, variant_id, y_frac=req.y_frac, db=db)
    log.info(
        "plan_item_set_caption_position",
        item_id=item_id,
        variant_id=variant_id,
        y_frac=req.y_frac,
        margin_v=margin_v,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.patch("/{item_id}/variants/{variant_id}/caption-style", response_model=PlanItemResponse)
async def set_item_caption_style(
    item_id: str,
    variant_id: str,
    req: CaptionStyleRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set sentence/word caption style for a caption variant (no re-render).

    The editor previews the choice; Apply (`/captions/apply`) reburns in the
    chosen style. Moved here from the pre-generation picker — see the plan-item
    redesign (subtitles are now tuned post-gen, not before).
    """
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await persist_variant_caption_style(job.id, variant_id, req.caption_style, db)
    log.info(
        "plan_item_set_caption_style",
        item_id=item_id,
        variant_id=variant_id,
        style=req.caption_style,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.patch("/{item_id}/variants/{variant_id}/captions-enabled", response_model=PlanItemResponse)
async def set_item_captions_enabled(
    item_id: str,
    variant_id: str,
    req: CaptionsEnabledRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Subtitles on/off for a caption variant (no re-render).

    Independent of stored cue count — toggling off never destroys the
    transcript-derived cues, so toggling back on later needs no re-transcription.
    Apply (`/captions/apply`) reburns to reflect the current on/off state.
    """
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await persist_variant_captions_enabled(job.id, variant_id, req.enabled, db)
    log.info(
        "plan_item_set_captions_enabled",
        item_id=item_id,
        variant_id=variant_id,
        enabled=req.enabled,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/bed-level", response_model=PlanItemResponse)
async def set_item_bed_level(
    item_id: str,
    variant_id: str,
    req: BedLevelRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Change a narrated variant's background-sound (voice/bed) level — re-renders (async).

    NOT the generate-time `voiceover_bed_level` PATCH (no-render, item-scoped) — this
    is the post-gen editor's Background Sound slider. Narrated-only: talking-to-camera
    has no separate voice track to duck under, so there is nothing to mix.
    """
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    # dispatch_set_narrated_bed_level does its OWN row-locked re-fetch by job.id and
    # commits internally — unlike the sibling dispatch_* calls above, it must not
    # operate on the unlocked `job` snapshot from _owned_item_render_job (see its
    # docstring for why: the Background Sound slider auto-commits on a debounce
    # right next to the row-locked Captions toggle in the same panel).
    await dispatch_set_narrated_bed_level(job.id, variant_id, bed_level=req.bed_level, db=db)
    log.info(
        "plan_item_set_bed_level",
        item_id=item_id,
        variant_id=variant_id,
        bed_level=req.bed_level,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/caption-language", response_model=PlanItemResponse)
async def set_item_caption_language(
    item_id: str,
    variant_id: str,
    req: CaptionLanguageRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Change a subtitled variant's caption language → re-transcribe + reburn (async).

    The D5 override: re-runs whisper-1 on the cached base's audio with the new language
    hint and rebuilds the cues, REPLACING the current captions + any hand-edits (the
    frontend confirms first). Subtitled-only; unsupported languages are rejected (422).
    """
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    # Same commit-before-enqueue rationale as apply_item_captions above (R1-1) —
    # the dispatcher row-locks, stamps the generation, commits, then enqueues.
    await dispatch_retranscribe_captions(job.id, variant_id, language=req.language, db=db)
    log.info(
        "plan_item_set_caption_language",
        item_id=item_id,
        variant_id=variant_id,
        language=req.language,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/custom-effect", response_model=PlanItemResponse)
async def apply_item_custom_effect(
    item_id: str,
    variant_id: str,
    req: CustomEffectRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Apply Nova's sandboxed effect language to a variant's video (async re-render).

    Dark behind CUSTOM_EFFECTS_ENABLED — 404 when off, matching the SFX-lane
    gating pattern (`sound_effects_enabled`/`media_overlays_enabled` above).
    This is the same endpoint the chat copilot's `apply_custom_effect` op
    PATCHes through (EditorShell, mirroring `intro_layout`); a future direct
    panel control could reuse it too. v1: a single active custom effect —
    each call replaces any previously-applied one, never stacks.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.custom_effects_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Custom effects not available."
        )
    job = await _owned_item_render_job(item_id, user.id, db)  # ownership check only
    # dispatch_apply_custom_effect does its OWN row-locked re-fetch by job.id
    # and commits internally — same discipline as dispatch_retranscribe_captions
    # above (the task's start write is token-checked against the just-minted
    # render_generation_id).
    await dispatch_apply_custom_effect(job.id, variant_id, effect_raw=req.effect, db=db)
    log.info("plan_item_apply_custom_effect", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/change-style", response_model=PlanItemResponse)
async def change_item_style(
    item_id: str,
    variant_id: str,
    req: ChangeStyleRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants with a different curated text style set."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    pending_publish = dispatch_change_style(
        job,
        variant_id,
        style_set_id=req.style_set_id,
        publish=False,
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info("plan_item_change_style", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post(
    "/{item_id}/variants/{variant_id}/copilot/turn",
    response_model=CopilotTurnResponse,
)
@limiter.limit("20/minute", key_func=get_real_ip)
async def plan_item_copilot_turn(
    request: Request,
    item_id: str,
    variant_id: str,
    body: CopilotTurnBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CopilotTurnResponse:
    """Run one stateless editor-copilot turn.

    Zero writes to item/job/variant rows. The client snapshot is untrusted and
    only passed to the agent for parsing; the editor applies returned ops to its
    local draft state and Save still goes through editor-commit validation.
    """
    from app.config import settings  # noqa: PLC0415

    _ = request  # required by the rate-limit decorator
    if not settings.edit_copilot_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="edit_copilot_not_enabled",
        )

    job = await _owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id, allow_guided_text=True)
    # agent_run.job_id FKs jobs.id — pass the render job, never the plan-item id.
    return await run_copilot_turn(body, job_id=job.id)


@router.post(
    "/{item_id}/variants/{variant_id}/director/suggestions",
    response_model=DirectorSuggestionsResponse,
)
@limiter.limit("10/minute", key_func=get_real_ip)
async def plan_item_director_suggestions(
    request: Request,
    item_id: str,
    variant_id: str,
    body: DirectorSuggestionsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DirectorSuggestionsResponse:
    """Return proactive, read-only editorial suggestion bundles."""
    from app.config import settings  # noqa: PLC0415

    _ = request
    if not settings.edit_director_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="edit_director_not_enabled",
        )
    job = await _owned_item_render_job(item_id, user.id, db)
    variant = require_editable_variant(job, variant_id, allow_guided_text=True)
    return await run_director(
        body,
        job_id=job.id,
        authoritative_speech_cut=speech_cut_director_context(job, variant),
    )


@router.post(
    "/{item_id}/variants/{variant_id}/director/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def plan_item_director_feedback(
    item_id: str,
    variant_id: str,
    body: DirectorFeedbackBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Record acceptance/dismissal without mutating the draft or variant."""
    from app.config import settings  # noqa: PLC0415

    if not settings.edit_director_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="edit_director_not_enabled",
        )
    job = await _owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id, allow_guided_text=True)
    record_director_feedback(body, job_id=job.id)


@router.post(
    "/{item_id}/variants/{variant_id}/omni-assets",
    response_model=OmniAssetResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("3/minute", key_func=get_real_ip)
async def start_plan_item_omni_asset(
    request: Request,
    item_id: str,
    variant_id: str,
    body: OmniAssetStartBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OmniAssetResponse:
    """Start an opt-in generated insert without changing the editor draft."""
    _ = request
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id)
    return await start_omni_asset(job, variant_id, body, db)


@router.get(
    "/{item_id}/variants/{variant_id}/omni-assets/{asset_id}",
    response_model=OmniAssetResponse,
)
async def plan_item_omni_asset_status(
    item_id: str,
    variant_id: str,
    asset_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OmniAssetResponse:
    """Poll one generated asset; only ready responses contain an editor op."""
    job = await _owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id)
    return get_omni_asset(job, asset_id)


@router.post(
    "/{item_id}/variants/{variant_id}/omni-assets/{asset_id}/cancel",
    response_model=OmniAssetResponse,
)
async def cancel_plan_item_omni_asset(
    item_id: str,
    variant_id: str,
    asset_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OmniAssetResponse:
    """Request cancellation and leave the current draft untouched."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id)
    return await cancel_omni_asset(job, asset_id, db)


@router.post(
    "/{item_id}/variants/{variant_id}/omni-assets/{asset_id}/claim",
    response_model=OmniAssetResponse,
)
async def claim_plan_item_omni_asset(
    item_id: str,
    variant_id: str,
    asset_id: str,
    body: OmniAssetClaimBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OmniAssetResponse:
    """Claim one completed generated clip after the draft revision is verified."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id)
    return await claim_omni_asset(job, asset_id, body, db)


@router.post("/{item_id}/variants/{variant_id}/edit", response_model=PlanItemResponse)
async def edit_item_variant(
    item_id: str,
    variant_id: str,
    req: EditVariantRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Combined text/style/size/layout edit for one of this item's variants.

    Same contract as the public generative /edit endpoint — notably this is how
    the plan page picks the intro layout (Classic / Editorial word-cluster)
    after a render. One request → one re-render.
    """
    from app.routes.generative_jobs import _UNSET  # noqa: PLC0415

    job = await _locked_owned_item_render_job(item_id, user.id, db)
    # Tri-state (mirrors generative_jobs.edit_variant): absent from the request
    # -> _UNSET (leave unchanged); explicit top-level `null` -> None (remove);
    # an object -> only the fields the client actually set (exclude_unset), so
    # an omitted nested field merges over the persisted value instead of being
    # treated as an explicit null. Without this, dispatch_edit_variant always
    # sees carousel_moment=_UNSET here, so a carousel-only edit (the only kind
    # the CarouselPanel UI sends) 422s with "Provide at least one edit field."
    if "carousel_moment" not in req.model_fields_set:
        carousel_moment_field: object = _UNSET
    elif req.carousel_moment is None:
        carousel_moment_field = None
    else:
        carousel_moment_field = req.carousel_moment.model_dump(exclude_unset=True)
    pending_publish = dispatch_edit_variant(
        job,
        variant_id,
        text=req.text,
        remove_text=req.remove_text,
        style_set_id=req.style_set_id,
        text_size_px=req.text_size_px,
        intro_layout=req.intro_layout,
        font_family=req.font_family,
        effect=req.effect,
        text_color=req.text_color,
        cluster_hero_font=req.cluster_hero_font,
        cluster_body_font=req.cluster_body_font,
        cluster_accent_font=req.cluster_accent_font,
        cluster_hero_size_px=req.cluster_hero_size_px,
        cluster_body_size_px=req.cluster_body_size_px,
        cluster_accent_size_px=req.cluster_accent_size_px,
        text_behind_subject=req.text_behind_subject,
        carousel_moment=carousel_moment_field,
        publish=False,
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "plan_item_edit_variant",
        item_id=item_id,
        variant_id=variant_id,
        has_text=req.text is not None,
        intro_layout=req.intro_layout,
        font_family=req.font_family,
        effect=req.effect,
        text_color=req.text_color,
        cluster_hero_font=req.cluster_hero_font,
        cluster_body_font=req.cluster_body_font,
        cluster_accent_font=req.cluster_accent_font,
        text_behind_subject=req.text_behind_subject,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/intro-size", response_model=PlanItemResponse)
async def set_item_intro_size(
    item_id: str,
    variant_id: str,
    req: SetIntroSizeRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants with a user-pinned AI-intro font size."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    pending_publish = dispatch_set_intro_size(
        job,
        variant_id,
        text_size_px=req.text_size_px,
        publish=False,
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "plan_item_set_intro_size", item_id=item_id, variant_id=variant_id, px=req.text_size_px
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/variants/{variant_id}/intro-timing", response_model=PlanItemResponse)
async def set_item_intro_timing(
    item_id: str,
    variant_id: str,
    req: SetIntroTimingRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants with user-pinned intro overlay timing."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    pending_publish = dispatch_set_intro_timing(
        job,
        variant_id,
        start_s=req.start_s,
        end_s=req.end_s,
        publish=False,
    )
    await db.commit()
    await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "plan_item_set_intro_timing",
        item_id=item_id,
        variant_id=variant_id,
        start_s=req.start_s,
        end_s=req.end_s,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.patch("/{item_id}/variants/{variant_id}/scene-timing", response_model=PlanItemResponse)
async def patch_item_scene_timing(
    item_id: str,
    variant_id: str,
    req: PatchSceneTimingRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Persist user-pinned scene timing overrides for one of this item's variants."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    dispatch_patch_scene_timing(
        job,
        variant_id,
        overrides=[o.model_dump() for o in req.overrides],
    )
    await db.commit()
    log.info(
        "plan_item_patch_scene_timing",
        item_id=item_id,
        variant_id=variant_id,
        override_count=len(req.overrides),
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.get("/{item_id}/variants/{variant_id}/timeline", response_model=TimelineResponse)
async def get_item_timeline(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TimelineResponse:
    """The effective clip timeline of one of this item's variants (+ clip pool)."""
    job = await _owned_item_render_job(item_id, user.id, db)
    return TimelineResponse(**dispatch_get_timeline(job, variant_id))


@router.get("/{item_id}/variants/{variant_id}/lyric-seeds", response_model=LyricSeedsResponse)
async def get_item_lyric_seeds(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LyricSeedsResponse:
    """Instant-materialize seed elements for the editor's Lyrics toggle.

    Lyrics-as-optional-elements (LYRICS_OPTIONAL_ENABLED): read-only, never
    persists anything. 404 when the flag is off; 422 when the variant has no
    matched track / no renderable cached lyrics. Mirrors the generative-jobs
    sibling route (`get_variant_lyric_seeds`) — only the job lookup differs.
    """
    job = await _owned_item_render_job(item_id, user.id, db)
    return LyricSeedsResponse(**await dispatch_get_lyric_seeds(job, variant_id, db))


@router.post("/{item_id}/variants/{variant_id}/timeline", response_model=PlanItemResponse)
async def edit_item_timeline(
    item_id: str,
    variant_id: str,
    req: TimelineEditRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Persist a user-edited clip timeline for one of this item's variants + re-render."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await dispatch_edit_timeline(job, variant_id, req, db=db)
    log.info("plan_item_edit_timeline", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.delete("/{item_id}/variants/{variant_id}/timeline", response_model=PlanItemResponse)
async def reset_item_timeline(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Discard the user timeline on one of this item's variants + re-render from AI."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await dispatch_reset_timeline(job, variant_id, db=db)
    log.info("plan_item_reset_timeline", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


# ── Reroll (swap idea for a single un-started item) ────────────────────────────


@router.post("/{item_id}/reroll", response_model=PlanItemResponse)
async def reroll_plan_item_route(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-generate the idea for a single plan item.

    Only allowed when the item is an un-started idea (item_status == "idea"
    and no current_job_id) — re-rolling a rendered/rendering item would
    orphan work in progress.
    """
    item, plan, _ = await _load_owned_item_context(
        item_id,
        user.id,
        db,
        for_update=True,
    )
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)

    if item.item_status != "idea" or item.current_job_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Can only reroll an un-started idea (item_status='idea', no current_job_id)",
        )

    item.item_status = "rerolling"
    await db.commit()

    from app.tasks.content_plan_build import reroll_plan_item  # noqa: PLC0415

    reroll_plan_item.delay(str(item.id), ownership_epoch)

    log.info("plan_item_reroll.dispatched", item_id=item_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


# ── Idea expansion (propose-only) ─────────────────────────────────────────────


class IdeaExpandResponse(BaseModel):
    """Proposed expansion — never written to DB by this endpoint."""

    theme: str
    filming_suggestion: str
    filming_guide: list[dict]
    rationale: str


class IdeaExpandRequest(BaseModel):
    """Optional creator context for a stronger propose-only expansion."""

    creator_context: str | None = Field(default=None, max_length=800)

    @field_validator("creator_context", mode="before")
    @classmethod
    def _clean_context(cls, value: object) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None


def _expand_video_type(edit_format: str | None) -> str:
    if edit_format in {"narrated", "narrated_planned", "narrated_ready"}:
        return "voiceover"
    if edit_format in {"subtitled", "talking_head"}:
        return "talking_to_camera"
    return "montage"


def _normalize_content_mode(value: object) -> str:
    mode = str(value or "create_new")
    if mode in {"create_new", "existing_footage", "mixed"}:
        return mode
    return "create_new"


@router.post("/{item_id}/expand", response_model=IdeaExpandResponse)
async def expand_idea(
    item_id: str,
    user: CurrentUser,
    body: IdeaExpandRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> IdeaExpandResponse:
    """Propose an AI-expanded plan item (theme, shots, rationale).

    Propose-only: this endpoint NEVER writes to the DB. The caller displays
    the proposal in a card and calls PATCH /{item_id} if the user accepts.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext, TerminalError  # noqa: PLC0415
    from app.agents.idea_expander import IdeaExpanderAgent, IdeaExpanderInput  # noqa: PLC0415

    item = await _load_owned_item(item_id, user.id, db)

    # Gather persona context for richer expansion.
    persona_summary = ""
    content_pillars: list[str] = []
    content_mode = _normalize_content_mode(getattr(item, "content_mode", None))
    _, persona = await _load_item_plan_persona(item, db)
    if isinstance(persona.persona, dict):
        persona_summary = str(persona.persona.get("summary", ""))
        content_pillars = list(persona.persona.get("content_pillars") or [])
        if getattr(item, "content_mode", None) is None:
            content_mode = _normalize_content_mode(persona.persona.get("content_mode"))

    agent = IdeaExpanderAgent(default_client())
    try:
        output = agent.run(
            IdeaExpanderInput(
                idea=item.idea or "",
                persona_summary=persona_summary,
                content_pillars=content_pillars,
                creator_context=body.creator_context if body and body.creator_context else "",
                video_type=_expand_video_type(getattr(item, "edit_format", None)),
                content_mode=content_mode,
            ),
            ctx=RunContext(job_id=None),
        )
    except TerminalError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't plan this idea — try again.",
        ) from exc

    return IdeaExpandResponse(
        theme=output.theme,
        filming_suggestion=output.filming_suggestion,
        filming_guide=_stamp_missing_filming_shot_ids(
            [s.model_dump() for s in output.filming_guide]
        ),
        rationale=output.rationale,
    )


# ── Media-overlay routes ──────────────────────────────────────────────────────


class OverlayUploadFile(BaseModel):
    filename: str
    content_type: str
    file_size_bytes: int


class OverlayUploadUrlsBody(BaseModel):
    files: list[OverlayUploadFile]


class OverlayUploadUrlsResponse(BaseModel):
    urls: list[UploadUrlItem]


class OverlayUploadConfirmFile(BaseModel):
    gcs_path: str
    content_type: str


class OverlayUploadConfirmBody(BaseModel):
    files: list[OverlayUploadConfirmFile]


class OverlayUploadConfirmItem(BaseModel):
    gcs_path: str
    preview_gcs_path: str | None = None
    preview_url: str | None = None


class OverlayUploadConfirmResponse(BaseModel):
    files: list[OverlayUploadConfirmItem]


def _is_heif_overlay(path: str, content_type: str) -> bool:
    return is_heif_overlay(path, content_type)


def _convert_heif_overlay_preview(gcs_path: str) -> tuple[str | None, str | None]:
    # Per-attempt key makes stale-epoch cleanup exact.  Reusing the legacy
    # deterministic `.preview.jpg` key could overwrite and then delete a
    # preview created by an earlier valid request.
    return convert_heif_overlay_preview(
        gcs_path,
        preview_gcs_path=f"{gcs_path}.preview.{uuid.uuid4().hex}.jpg",
    )


@router.post("/{item_id}/overlay-upload-urls", response_model=OverlayUploadUrlsResponse)
async def create_overlay_upload_urls(
    item_id: str,
    body: OverlayUploadUrlsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OverlayUploadUrlsResponse:
    """Signed PUT URLs for media-overlay card assets under the persistent users/ prefix.

    Cards land under `users/{user_id}/plan/{item_id}/overlays/...` which is NOT
    swept by the 24h GCS lifecycle rule — assets survive for the lifetime of the
    plan item and can be re-applied on swap-song/retext re-renders.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.media_overlays_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media overlays not available."
        )

    item = await _load_owned_item(item_id, user.id, db)
    if not body.files or len(body.files) > _MAX_OVERLAY_CARDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provide 1-{_MAX_OVERLAY_CARDS} files",
        )
    urls: list[UploadUrlItem] = []
    for f in body.files:
        if f.content_type not in _OVERLAY_ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported overlay content type: {f.content_type}",
            )
        if f.file_size_bytes <= 0 or f.file_size_bytes > _MAX_OVERLAY_FILE_BYTES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad file size")
        safe_name = f"{uuid.uuid4().hex}-{f.filename.split('/')[-1]}"
        upload_url, gcs_path = storage.presigned_put_url_for_media_overlay(
            user_id=str(user.id),
            plan_item_id=str(item.id),
            filename=safe_name,
            content_type=f.content_type,
        )
        urls.append(UploadUrlItem(upload_url=upload_url, gcs_path=gcs_path))
    return OverlayUploadUrlsResponse(urls=urls)


@router.post(
    "/{item_id}/overlay-upload-confirm",
    response_model=OverlayUploadConfirmResponse,
)
async def confirm_overlay_uploads(
    item_id: str,
    body: OverlayUploadConfirmBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OverlayUploadConfirmResponse:
    """Post-upload hook for browser previews.

    HEIC/HEIF stays as the renderer source, but the browser needs a JPEG preview
    because Chromium cannot decode HEIC. Non-HEIF uploads return no preview path.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.media_overlays_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media overlays not available."
        )

    item, plan, _ = await _load_owned_item_context(item_id, user.id, db)
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)
    stable_item_id = item.id
    await db.rollback()
    prefix = f"users/{user.id}/plan/{stable_item_id}/overlays/"
    confirmed: list[OverlayUploadConfirmItem] = []
    try:
        for f in body.files:
            if not f.gcs_path.startswith(prefix):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Overlay asset path must be under '{prefix}'.",
                )
            preview_gcs_path: str | None = None
            preview_url: str | None = None
            if _is_heif_overlay(f.gcs_path, f.content_type):
                preview_gcs_path, preview_url = _convert_heif_overlay_preview(f.gcs_path)
                preview_gcs_path = nonblank_str(preview_gcs_path)
                preview_url = nonblank_str(preview_url)
            confirmed.append(
                OverlayUploadConfirmItem(
                    gcs_path=f.gcs_path,
                    preview_gcs_path=preview_gcs_path,
                    preview_url=preview_url,
                )
            )
        _, locked_plan, _ = await _load_owned_item_context(
            item_id,
            user.id,
            db,
            for_update=True,
        )
        if int(getattr(locked_plan, "ownership_epoch", 0) or 0) != ownership_epoch:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            )
        await db.rollback()
    except Exception:
        await db.rollback()
        for converted in confirmed:
            if converted.preview_gcs_path:
                deleted = await asyncio.to_thread(
                    storage.delete_object_best_effort,
                    converted.preview_gcs_path,
                )
                if not deleted:
                    log.error(
                        "overlay_preview_stale_cleanup_failed",
                        gcs_path=converted.preview_gcs_path,
                    )
        raise
    return OverlayUploadConfirmResponse(files=confirmed)


class SetMediaOverlaysBody(BaseModel):
    """Full-replace body: the entire new card list. Send [] to clear all cards.

    `render=True` (default): validate cards, persist, and enqueue the FFmpeg
    apply-pass. The variant flips to render_status="rendering".

    `render=False`: validate + persist card metadata only — NO Celery task is
    dispatched and render_status is not changed. Used by the frontend to
    auto-save card positions without triggering a background render; the FFmpeg
    pass runs only when the user explicitly downloads.
    """

    overlays: list[dict] = Field(default_factory=list)
    render: bool = True


async def _require_ready_pool_paths(
    *,
    item_id: str,
    user_id: uuid.UUID,
    payload: object,
    db: AsyncSession,
) -> None:
    from app.services.pool_asset_refs import pool_paths_in_payload  # noqa: PLC0415

    try:
        parsed_item_id = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    prefix = f"users/{user_id}/plan/{parsed_item_id}/pool/"
    paths = pool_paths_in_payload(payload, prefix=prefix)
    if not paths:
        return
    rows = (
        (
            await db.execute(
                select(PlanItemAsset).where(
                    PlanItemAsset.plan_item_id == parsed_item_id,
                    PlanItemAsset.user_id == user_id,
                    PlanItemAsset.gcs_path.in_(paths),
                    PlanItemAsset.status == "ready",
                )
            )
        )
        .scalars()
        .all()
    )
    if {row.gcs_path for row in rows if row.status == "ready"} != paths:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="One of these visual files is no longer available. Refresh and choose again.",
        )


def _persist_overlay_metadata_only(
    job: Job,
    variant_id: str,
    *,
    overlays_raw: list[dict],
    user_id: str,
) -> None:
    """Save overlay card list to assembly_plan without triggering a render pass.

    Validates paths and timing (same rules as dispatch_set_media_overlays) but
    only writes `variants[i]["media_overlays"]`. render_status stays unchanged.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    variants = list((job.assembly_plan or {}).get("variants") or [])
    variant_for_check = next((v for v in variants if v.get("variant_id") == variant_id), {})
    validated = validate_media_overlays_for_user(
        overlays_raw=overlays_raw,
        user_id=user_id,
        variant_context=variant_for_check,
    )
    cascaded_sfx, cascaded_camera_effects = cascade_removed_overlay_effect_groups(
        variant_for_check,
        validated,
    )
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["media_overlays"] = validated or None
            v["media_overlays_render_dirty"] = True
            if cascaded_sfx is not None:
                v["sound_effects"] = cascaded_sfx or None
            if cascaded_camera_effects is not None:
                v["camera_effects"] = cascaded_camera_effects or None
                # render=false updates desired metadata only. Remember that the
                # current base still contains the removed crop/pulse so the next
                # render=true request rebuilds instead of taking the outer-card
                # fast path after the old overlay linkage has disappeared.
                v["overlay_camera_rebuild_pending"] = True
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")


@router.put(
    "/{item_id}/variants/{variant_id}/media-overlays",
    response_model=PlanItemResponse,
)
async def set_item_media_overlays(
    item_id: str,
    variant_id: str,
    body: SetMediaOverlaysBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Apply or clear media-overlay cards on one of this item's variants.

    Full-replace: the `overlays` list becomes the card set for this variant.
    When `render=True` (default), the FFmpeg apply-pass runs async and the
    variant flips to render_status="rendering".
    When `render=False`, only card metadata is persisted — no render is queued.
    The frontend uses render=False for auto-save; render=True only on download.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.media_overlays_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media overlays not available."
        )

    job = await _locked_owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id)
    await _require_ready_pool_paths(
        item_id=item_id,
        user_id=user.id,
        payload=body.overlays,
        db=db,
    )
    enqueue_after_commit = None
    if body.render:
        enqueue_after_commit = dispatch_set_media_overlays(
            job, variant_id, overlays_raw=body.overlays, user_id=str(user.id)
        )
    else:
        _persist_overlay_metadata_only(
            job, variant_id, overlays_raw=body.overlays, user_id=str(user.id)
        )
    await db.commit()
    if enqueue_after_commit is not None:
        # R1-1: caption-reburn branch — the reburn's start write is token-checked
        # against the generation committed above, so the enqueue must come AFTER
        # the commit or a fast worker strands the variant in "rendering".
        enqueue_after_commit()
    log.info(
        "plan_item_set_media_overlays",
        item_id=item_id,
        variant_id=variant_id,
        card_count=len(body.overlays),
        render=body.render,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


# ── TextElement routes ────────────────────────────────────────────────────────


@router.put(
    "/{item_id}/variants/{variant_id}/text-elements",
    response_model=PlanItemResponse,
)
async def set_item_text_elements(
    item_id: str,
    variant_id: str,
    body: TextElementsRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set (or clear) the TextElement list for one of this item's variants.

    Full-replace: `body.elements` becomes the authoritative element list.
    When `render=True` (default), the fast-reburn task runs async and the
    variant flips to render_status="rendering".
    When `render=False`, only the elements are persisted — no render is queued.

    Once text_elements_user_edited is set, the instant-edit surface (PUT /edit)
    returns 409 and directs the caller here instead (A15).
    """
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    pending_publish = dispatch_set_text_elements(
        job,
        variant_id,
        elements=body.elements,
        render=body.render,
        publish=False,
    )
    await db.commit()
    if pending_publish is not None:
        await _publish_committed_variant_render(pending_publish, db)
    log.info(
        "plan_item_set_text_elements",
        item_id=item_id,
        variant_id=variant_id,
        element_count=len(body.elements),
        render=body.render,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.put(
    "/{item_id}/variants/{variant_id}/lyrics",
    response_model=PlanItemResponse,
)
async def set_item_lyrics(
    item_id: str,
    variant_id: str,
    body: LyricsSectionRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Toggle lyrics or replace lyric line overrides for one of this item's variants."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    from app.routes.generative_jobs import _UNSET  # noqa: PLC0415

    enabled = body.enabled if "enabled" in body.model_fields_set else _UNSET
    line_overrides = body.line_overrides if "line_overrides" in body.model_fields_set else _UNSET
    await dispatch_set_lyrics(
        db,
        job,
        variant_id,
        enabled=enabled,
        line_overrides=line_overrides,
    )
    log.info(
        "plan_item_set_lyrics",
        item_id=item_id,
        variant_id=variant_id,
        enabled=body.enabled,
        has_line_overrides="line_overrides" in body.model_fields_set,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.put(
    "/{item_id}/variants/{variant_id}/orientation",
    response_model=PlanItemResponse,
)
async def set_item_orientation(
    item_id: str,
    variant_id: str,
    body: OrientationRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Set portrait/landscape output for one of this item's variants."""
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    await dispatch_set_orientation(
        db,
        job,
        variant_id,
        orientation=body.orientation,
    )
    log.info(
        "plan_item_set_orientation",
        item_id=item_id,
        variant_id=variant_id,
        orientation=body.orientation,
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post(
    "/{item_id}/variants/{variant_id}/editor-commit",
    response_model=EditorCommitResponse,
)
async def editor_commit_item(
    item_id: str,
    variant_id: str,
    body: EditorCommitRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EditorCommitResponse:
    """Transactional editor Save (E2): all sections in ONE commit + ONE render kick.

    Validates every provided section first (nothing persists on ANY failure),
    compares `base_generation` against the variant's current baseline (409
    `baseline_conflict` when another tab/render moved it), then persists all
    job-JSON sections atomically in a single commit, bumps the render
    generation, and enqueues exactly one re-render.

    Deliberately NOT guarded by `require_editable_variant` — saving during an
    in-flight render is the point; the E1 generation guard makes the superseded
    task discard its terminal write (D8 queue/supersede).

    `title` updates the plan item's display title (`PlanItem.theme`). It lives
    on a different table than the variant job-JSON, but both rows are written in
    the SAME database transaction here, so the commit stays all-or-nothing.
    """
    item, _, _ = await _load_owned_item_context(item_id, user.id, db, for_update=True)
    job_id = item.current_job_id or (item.current_job.id if item.current_job is not None else None)
    locked_job = (
        await db.get(
            Job,
            job_id,
            populate_existing=True,
            with_for_update=True,
        )
        if job_id is not None
        else None
    )
    if locked_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    if locked_job.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancelled videos cannot be edited.",
        )
    # Guided stories accept TextElement-only reburns. Run this policy before
    # media/SFX/music/title validation so every unsupported section gets the
    # same stable 422 without performing unrelated lookups or mutations.
    require_guided_story_editor_commit(locked_job, variant_id, body)
    if body.media_overlays is not None:
        await _require_ready_pool_paths(
            item_id=item_id,
            user_id=user.id,
            payload=body.media_overlays,
            db=db,
        )

    # Validate the title section BEFORE the job-JSON staging so a bad title
    # fails the whole commit with nothing persisted (validation-first contract).
    cleaned_title: str | None = None
    if body.title is not None:
        cleaned_title = _sanitize_text(body.title.strip())
        if not cleaned_title:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Title cannot be empty.",
            )

    commit_body = body
    if body.sound_effects is not None:
        resolved_sfx = await _resolve_sound_effect_placements(
            body.sound_effects,
            user_id=str(user.id),
            db=db,
        )
        commit_body = body.model_copy(update={"sound_effects": resolved_sfx})

    selected_music_track = None
    selected_background_music_track = None
    if commit_body.music_track_id is not None:
        selected_music_track = (
            await db.execute(select(MusicTrack).where(MusicTrack.id == commit_body.music_track_id))
        ).scalar_one_or_none()
    elif commit_body.lyrics is not None or commit_body.music_window is not None:
        locked_variant = next(
            (
                v
                for v in (locked_job.assembly_plan or {}).get("variants") or []
                if v.get("variant_id") == variant_id
            ),
            None,
        )
        if locked_variant is not None and locked_variant.get("music_track_id"):
            selected_music_track = (
                await db.execute(
                    select(MusicTrack).where(MusicTrack.id == locked_variant.get("music_track_id"))
                )
            ).scalar_one_or_none()
    if (
        commit_body.background_music is not None
        and not commit_body.background_music.remove
        and commit_body.background_music.track_id
    ):
        selected_background_music_track = (
            await db.execute(
                select(MusicTrack).where(MusicTrack.id == commit_body.background_music.track_id)
            )
        ).scalar_one_or_none()

    selected_background_music_track = None
    if (
        commit_body.background_music is not None
        and commit_body.background_music.track_id is not None
    ):
        selected_background_music_track = (
            await db.execute(
                select(MusicTrack).where(MusicTrack.id == commit_body.background_music.track_id)
            )
        ).scalar_one_or_none()

    visual_assets: dict[str, dict] | None = None
    if commit_body.visual_blocks is not None or commit_body.motion_scenes is not None:
        rows = (
            (await db.execute(select(PlanItemAsset).where(PlanItemAsset.plan_item_id == item.id)))
            .scalars()
            .all()
        )
        visual_assets = {
            str(row.id): {
                "status": row.status,
                "gcs_path": row.gcs_path,
                "kind": row.kind,
                "user_context": getattr(row, "user_context", None),
            }
            for row in rows
        }

    prep = prepare_editor_commit(
        locked_job,
        variant_id,
        commit_body,
        user_id=str(user.id),
        music_track=selected_music_track,
        background_music_track=selected_background_music_track,
        visual_assets=visual_assets,
    )

    if cleaned_title is not None:
        item.theme = cleaned_title
        item.user_edited = True

    await db.commit()
    # Kick AFTER the commit so a worker that grabs the task instantly always
    # observes the committed sections + generation token. If the kick fails the
    # persist stands — the honest partial state ("saved, rendering didn't
    # start") the plan's §9 table describes.
    enqueue_editor_commit_render(str(locked_job.id), variant_id, prep)

    log.info(
        "plan_item_editor_commit",
        item_id=item_id,
        variant_id=variant_id,
        generation=prep["generation"],
        sections={**prep["sections"], "title": cleaned_title is not None},
        visual_block_count=(
            len(commit_body.visual_blocks) if commit_body.visual_blocks is not None else None
        ),
        ai_visual_block_count=(
            sum(1 for block in commit_body.visual_blocks if block.get("origin") == "ai")
            if commit_body.visual_blocks is not None
            else None
        ),
    )
    return EditorCommitResponse(
        ok=True,
        generation=prep["generation"],
        sections=EditorCommitSections(
            text_elements=prep["sections"]["text_elements"],
            caption_cues=prep["sections"]["caption_cues"],
            caption_meta=prep["sections"]["caption_meta"],
            timeline=prep["sections"]["timeline"],
            mix=prep["sections"]["mix"],
            music=prep["sections"]["music"],
            background_music=prep["sections"]["background_music"],
            lyrics=prep["sections"]["lyrics"],
            orientation=prep["sections"]["orientation"],
            sound_effects=prep["sections"]["sound_effects"],
            media_overlays=prep["sections"]["media_overlays"],
            visual_blocks=prep["sections"]["visual_blocks"],
            camera_effects=prep["sections"].get("camera_effects", False),
            carousel_moment=prep["sections"].get("carousel_moment", False),
            title=cleaned_title is not None,
        ),
    )


class RetimeVisualBlockBody(BaseModel):
    visual_block: dict


@router.post("/{item_id}/variants/{variant_id}/visual-blocks/retime")
async def retime_visual_block(
    item_id: str,
    variant_id: str,
    body: RetimeVisualBlockBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return a concretely timed montage without persisting editor state."""
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.visual_blocks_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    item = await _load_owned_item(item_id, user.id, db)
    if item.current_job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    variant = _find_variant_dict(item.current_job, variant_id)
    if variant.get("text_mode") == "lyrics" or not variant.get("base_video_path"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Visual blocks require a non-lyrics variant with a clean base.",
        )

    from app.agents._schemas.visual_block import (  # noqa: PLC0415
        MontageBlock,
        SyncAnchor,
        iter_visual_shots,
        retime_montage,
        validate_visual_blocks,
    )

    raw = body.visual_block
    if raw.get("kind") != "montage":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only montage blocks have automatic shot pacing.",
        )
    try:
        # Retime the raw shot list first so reordered drafts with stale offsets
        # can become valid instead of failing MontageBlock's contiguity guard.
        draft = dict(raw)
        draft_shots = [dict(shot) for shot in draft.get("shots") or []]
        if not draft_shots:
            raise ValueError("A montage needs shots to retime")
        duration_s = float(draft.get("end_s", 0.0)) - float(draft.get("start_s", 0.0))
        per_shot = duration_s / len(draft_shots)
        offset = 0.0
        for index, shot in enumerate(draft_shots):
            shot_duration = duration_s - offset if index == len(draft_shots) - 1 else per_shot
            shot["start_offset_s"] = round(offset, 6)
            shot["duration_s"] = round(shot_duration, 6)
            offset += shot_duration
        draft["shots"] = draft_shots
        draft["timing_mode"] = "auto"
        normalized = validate_visual_blocks(
            [draft],
            duration_s=visual_block_variant_duration(variant),
        )[0]
        anchors: list[SyncAnchor] = []
        for shot in draft_shots:
            if shot.get("sync_anchor"):
                anchors.append(SyncAnchor.model_validate(shot["sync_anchor"]))
        for cue in list(variant.get("caption_cues") or variant.get("transcript_segments") or []):
            if not isinstance(cue, dict):
                continue
            boundary = cue.get("end_s", cue.get("end"))
            if boundary is not None:
                anchors.append(
                    SyncAnchor(
                        type="sentence",
                        time_s=float(boundary),
                        label=str(cue.get("text") or "")[:120] or None,
                    )
                )
        for beat in list(variant.get("beat_grid") or variant.get("beat_timestamps_s") or []):
            value = beat.get("time_s", beat.get("time")) if isinstance(beat, dict) else beat
            if value is not None:
                anchors.append(SyncAnchor(type="beat", time_s=float(value)))
        for effect in list(variant.get("sound_effects") or []):
            if isinstance(effect, dict) and effect.get("start_s") is not None:
                anchors.append(
                    SyncAnchor(
                        type="manual",
                        time_s=float(effect["start_s"]),
                        label="Sound effect",
                    )
                )
        validated = retime_montage(
            MontageBlock.model_validate(normalized), anchors=anchors
        ).model_dump(by_alias=True, exclude_none=True)
        validated = validate_visual_blocks(
            [validated], duration_s=visual_block_variant_duration(variant)
        )[0]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    asset_ids = {str(shot["asset_id"]) for shot in iter_visual_shots([validated])}
    assets = (
        (
            await db.execute(
                select(PlanItemAsset).where(
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.id.in_(asset_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    asset_map = {str(asset.id): asset for asset in assets}
    for shot in iter_visual_shots([validated]):
        asset = asset_map.get(str(shot["asset_id"]))
        if (
            asset is None
            or asset.status != "ready"
            or asset.gcs_path != shot["src_gcs_path"]
            or asset.kind != shot["kind"]
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Visual block assets must be ready assets owned by this plan item.",
            )
    return {"visual_block": validated}


# ── Sound-effects routes ──────────────────────────────────────────────────────


class SfxUploadFile(BaseModel):
    filename: str
    content_type: str
    file_size_bytes: int


class SfxUploadUrlsBody(BaseModel):
    files: list[SfxUploadFile]


class SfxUploadUrlsResponse(BaseModel):
    urls: list[UploadUrlItem]


class SetSoundEffectsBody(BaseModel):
    """Full-replace body: the entire new placement list. Send [] to clear all effects."""

    placements: list[dict] = Field(default_factory=list)


async def _resolve_sound_effect_placements(
    placements: list[dict],
    *,
    user_id: str,
    db: AsyncSession,
) -> list[dict]:
    """Resolve curated SFX IDs server-side, then validate the full placement list."""
    resolved_placements: list[dict] = []
    for raw in placements:
        placement = dict(raw)
        sound_effect_id = placement.get("sound_effect_id")
        if sound_effect_id:
            from sqlalchemy import select as _select  # noqa: PLC0415

            from app.models import SoundEffect  # noqa: PLC0415

            effect_result = await db.execute(
                _select(SoundEffect).where(SoundEffect.id == sound_effect_id)
            )
            effect = effect_result.scalar_one_or_none()
            if effect is None or not effect.audio_gcs_path:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Sound effect {sound_effect_id!r} not found or has no audio.",
                )
            # Always resolve path server-side — never trust a client-supplied sound-effects/ path.
            placement["src_gcs_path"] = effect.audio_gcs_path
            placement["label"] = placement.get("label") or effect.name
            placement["duration_s"] = placement.get("duration_s") or effect.duration_s
        resolved_placements.append(placement)
    return validate_sound_effects_for_user(sfx_raw=resolved_placements, user_id=user_id)


@router.post("/{item_id}/sfx-upload-urls", response_model=SfxUploadUrlsResponse)
async def create_sfx_upload_urls(
    item_id: str,
    body: SfxUploadUrlsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SfxUploadUrlsResponse:
    """Signed PUT URLs for user-uploaded sound-effect assets under the persistent users/ prefix.

    Assets land under `users/{user_id}/plan/{item_id}/sfx/...` which is NOT
    swept by the 24h GCS lifecycle rule.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.sound_effects_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sound effects not available."
        )

    item = await _load_owned_item(item_id, user.id, db)
    if not body.files or len(body.files) > _MAX_SFX_CARDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provide 1-{_MAX_SFX_CARDS} files",
        )
    urls: list[UploadUrlItem] = []
    for f in body.files:
        if f.content_type not in _SFX_ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported SFX content type: {f.content_type}",
            )
        if f.file_size_bytes <= 0 or f.file_size_bytes > _MAX_SFX_FILE_BYTES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad file size")
        safe_name = f"{uuid.uuid4().hex}-{f.filename.split('/')[-1]}"
        upload_url, gcs_path = storage.presigned_put_url_for_sfx(
            user_id=str(user.id),
            plan_item_id=str(item.id),
            filename=safe_name,
            content_type=f.content_type,
        )
        urls.append(UploadUrlItem(upload_url=upload_url, gcs_path=gcs_path))
    return SfxUploadUrlsResponse(urls=urls)


@router.put(
    "/{item_id}/variants/{variant_id}/sound-effects",
    response_model=PlanItemResponse,
)
async def set_item_sound_effects(
    item_id: str,
    variant_id: str,
    body: SetSoundEffectsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Persist sound-effect placements on one of this item's variants (no render).

    Full-replace: the `placements` list becomes the SFX set for this variant.
    Send an empty list (`{"placements": []}`) to clear all effects.

    For placements that reference a glossary effect (sound_effect_id set), the
    server resolves src_gcs_path from the SoundEffect row — client-supplied
    sound-effects/ paths are never trusted.

    Returns the updated plan item immediately. The FFmpeg mix-pass is NOT
    triggered here; call POST /{item_id}/variants/{variant_id}/render-sfx
    (e.g. on Download) to burn the placements in.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.sound_effects_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sound effects not available."
        )

    job = await _locked_owned_item_render_job(item_id, user.id, db)
    require_editable_variant(job, variant_id)

    resolved_placements = await _resolve_sound_effect_placements(
        body.placements,
        user_id=str(user.id),
        db=db,
    )

    # Persist directly — no Celery render dispatched here.
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["sound_effects"] = resolved_placements if resolved_placements else None
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")
    await db.commit()
    log.info(
        "plan_item_set_sound_effects",
        item_id=item_id,
        variant_id=variant_id,
        effect_count=len(body.placements),
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post(
    "/{item_id}/variants/{variant_id}/render-sfx",
    response_model=PlanItemResponse,
)
async def render_item_sound_effects(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Trigger the FFmpeg SFX burn-in pass for a variant that has persisted placements.

    Called by the Download button when sound_effects are set. The variant flips to
    render_status="rendering" and the mix-pass runs async. Returns immediately.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.sound_effects_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sound effects not available."
        )

    job = await _locked_owned_item_render_job(item_id, user.id, db)

    # Read persisted placements from assembly_plan.
    variants = list((job.assembly_plan or {}).get("variants") or [])
    sfx_raw: list[dict] = []
    for v in variants:
        if v.get("variant_id") == variant_id:
            sfx_raw = v.get("sound_effects") or []
            break

    enqueue_after_commit = dispatch_set_sound_effects(
        job,
        variant_id,
        sfx_raw=sfx_raw,
        user_id=str(user.id),
        db_for_glossary=db,
    )
    await db.commit()
    if enqueue_after_commit is not None:
        # R1-1: caption-reburn branch — enqueue only after the gate/gen commit.
        enqueue_after_commit()
    log.info("plan_item_render_sfx", item_id=item_id, variant_id=variant_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.get("/{item_id}/sfx-audio-url")
async def get_sfx_audio_url(
    item_id: str,
    gcs_path: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return a short-lived signed GET URL for a user-uploaded SFX file.

    Only allows paths under users/{user_id}/ — rejects any other prefix.
    """
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.sound_effects_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sound effects not available."
        )

    expected_prefix = f"users/{user.id}/"
    if not gcs_path.startswith(expected_prefix):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    # Verify item ownership (ownership check only — we don't need the job).
    await _load_owned_item(item_id, user.id, db)

    url = storage.signed_url(gcs_path, expiration_minutes=60)
    return {"url": url}


# ── "Get a transcript" voiceover helper (TRANSCRIPT_HELPER_ENABLED) ──────────────
# Optional helper: analyze the footage → ask up to 3 questions → write a script the
# creator reads while recording. Every route 404s when the flag is off and is
# owner-scoped. Agents run when GEMINI_API_KEY is set; otherwise deterministic
# heuristic fallbacks keep the flow working at localhost (no keys). See the plan at
# plans/on-the-narrated-walkthrough-*.md.


def _require_transcript_helper() -> None:
    from app.config import settings  # noqa: PLC0415

    if not settings.transcript_helper_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


class TranscriptAnalyzeResponse(BaseModel):
    analyze_id: str


class TranscriptAnalyzeStatusResponse(BaseModel):
    status: Literal["pending", "ready", "failed"]
    duration_s: float | None = None
    footage_summary: str | None = None


class TranscriptInterviewTurn(BaseModel):
    role: Literal["agent", "user"]
    content: str = Field(default="", max_length=4000)


class TranscriptInterviewBody(BaseModel):
    # Length caps on every untrusted field — bounds prompt size and blocks the
    # amplification path into the heuristic script builder.
    brief: str = Field(default="", max_length=2000)
    footage_summary: str | None = Field(default=None, max_length=4000)
    turns: list[TranscriptInterviewTurn] = Field(default_factory=list, max_length=40)


class TranscriptInterviewResponse(BaseModel):
    question: str
    suggestions: list[str]
    is_final: bool


class TranscriptScriptBody(BaseModel):
    brief: str = Field(default="", max_length=2000)
    footage_summary: str | None = Field(default=None, max_length=4000)
    answers: list[Annotated[str, Field(max_length=2000)]] = Field(
        default_factory=list, max_length=20
    )
    # Bounded: the heuristic fallback sizes a word-fill loop off this, so an
    # unbounded value is a synchronous request-thread DoS (600s ≈ 10 min, well past
    # any real short-form clip).
    duration_s: float = Field(default=30.0, ge=1, le=600)


class TranscriptScriptResponse(BaseModel):
    version: int
    text: str
    read_time_s: int
    lines: list[str]
    source: str


class TranscriptRecordedBody(BaseModel):
    version: int


@router.post("/{item_id}/transcript/analyze", response_model=TranscriptAnalyzeResponse)
async def transcript_analyze(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TranscriptAnalyzeResponse:
    """Kick off the async analyze (probe duration + light footage summary).

    Returns an opaque analyze_id the client polls. Clips must be attached first —
    the script is grounded in the footage.
    """
    _require_transcript_helper()
    item = await _load_owned_item(item_id, user.id, db)
    clip_paths = [p for p in (item.clip_gcs_paths or []) if isinstance(p, str) and p.strip()]
    if not clip_paths:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add clips before getting a transcript.",
        )
    analyze_id = uuid.uuid4().hex
    from app.tasks.transcript_analyze import analyze_transcript_footage  # noqa: PLC0415

    # Default `celery` queue via .delay(), like every other task in Kria — prod
    # workers already drain it, so no fly.toml -Q change is needed. (Registered in
    # app/worker.py `include` so workers know the task.)
    analyze_transcript_footage.delay(analyze_id, clip_paths, item_id)
    return TranscriptAnalyzeResponse(analyze_id=analyze_id)


@router.get(
    "/{item_id}/transcript/analyze/{analyze_id}",
    response_model=TranscriptAnalyzeStatusResponse,
)
async def transcript_analyze_status(
    item_id: str,
    analyze_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TranscriptAnalyzeStatusResponse:
    """Poll the analyze result. 'pending' until the task writes it (or the id is stale)."""
    _require_transcript_helper()
    await _load_owned_item(item_id, user.id, db)  # ownership gate
    from app.services.transcript_store import get_analyze  # noqa: PLC0415

    payload = get_analyze(item_id, analyze_id)
    if payload is None:
        return TranscriptAnalyzeStatusResponse(status="pending")
    raw_status = payload.get("status", "ready")
    outcome: Literal["pending", "ready", "failed"] = (
        raw_status if raw_status in ("pending", "ready", "failed") else "ready"
    )
    return TranscriptAnalyzeStatusResponse(
        status=outcome,
        duration_s=payload.get("duration_s"),
        footage_summary=payload.get("footage_summary"),
    )


@router.post("/{item_id}/transcript/interview", response_model=TranscriptInterviewResponse)
async def transcript_interview(
    item_id: str,
    body: TranscriptInterviewBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TranscriptInterviewResponse:
    """Return the next clarifying question (or a final one). Hard cap 3 questions."""
    _require_transcript_helper()
    await _load_owned_item(item_id, user.id, db)
    from app.config import settings  # noqa: PLC0415

    # Questions already asked = agent turns in the history; this call is the next one.
    asked = sum(1 for t in body.turns if t.role == "agent")

    question: str | None = None
    suggestions: list[str] = []
    is_final = False
    if settings.gemini_api_key:
        try:
            from app.agents._model_client import default_client  # noqa: PLC0415
            from app.agents.voiceover_interviewer import (  # noqa: PLC0415
                VoiceoverInterviewerAgent,
                VoiceoverInterviewerInput,
                VoiceoverTurn,
            )

            agent = VoiceoverInterviewerAgent(default_client())
            inp = VoiceoverInterviewerInput(
                footage_summary=body.footage_summary or "",
                brief=body.brief,
                turns=[VoiceoverTurn(role=t.role, content=t.content) for t in body.turns],
                turn_count=asked + 1,
            )
            out = await asyncio.to_thread(agent.run, inp)
            question, suggestions, is_final = out.question, out.suggestions, out.is_final
        except Exception as exc:  # noqa: BLE001
            log.warning("transcript_interview.agent_failed", error=str(exc)[:200])
            question = None

    if question is None:
        from app.services.transcript_fallbacks import heuristic_question  # noqa: PLC0415

        question, suggestions, is_final = heuristic_question(asked)

    return TranscriptInterviewResponse(
        question=question, suggestions=suggestions, is_final=is_final
    )


@router.post("/{item_id}/transcript/script", response_model=TranscriptScriptResponse)
async def transcript_script(
    item_id: str,
    body: TranscriptScriptBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TranscriptScriptResponse:
    """Generate (or rewrite) the voiceover script, persist it (bump version), return it."""
    _require_transcript_helper()
    item, plan, _ = await _load_owned_item_context(item_id, user.id, db)
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)
    # The model/heuristic needs no live ORM state. Release the read transaction
    # and accept its output only after the fenced owner pair is reacquired.
    await db.rollback()
    from app.agents.voiceover_script_writer import split_script_lines  # noqa: PLC0415
    from app.config import settings  # noqa: PLC0415
    from app.schemas.voiceover_script import VoiceoverScript, estimate_read_time_s  # noqa: PLC0415

    text: str | None = None
    lines: list[str] = []
    if settings.gemini_api_key:
        try:
            from app.agents._model_client import default_client  # noqa: PLC0415
            from app.agents.voiceover_script_writer import (  # noqa: PLC0415
                VoiceoverScriptWriterAgent,
                VoiceoverScriptWriterInput,
            )

            agent = VoiceoverScriptWriterAgent(default_client())
            inp = VoiceoverScriptWriterInput(
                footage_summary=body.footage_summary or "",
                brief=body.brief,
                answers=body.answers,
                target_duration_s=body.duration_s,
            )
            out = await asyncio.to_thread(agent.run, inp)
            text, lines = out.text, out.lines
        except Exception as exc:  # noqa: BLE001
            log.warning("transcript_script.agent_failed", error=str(exc)[:200])
            text = None

    if text is None:
        # The agent path sanitizes its own output; the heuristic weaves the raw
        # brief/answers in, so run the same output sanitizer before it's persisted
        # and read aloud (strips URLs / @handles / role-markers / control chars).
        from app.agents.voiceover_script_writer import _sanitize_script  # noqa: PLC0415
        from app.services.transcript_fallbacks import heuristic_script  # noqa: PLC0415

        text = _sanitize_script(heuristic_script(body.brief, body.answers, body.duration_s))
        lines = split_script_lines(text)

    # Lock in Plan -> Persona -> PlanItem order so two concurrent rewrites cannot
    # lose a version and a quarantine/repair racing the agent cannot accept its
    # stale output.
    item, live_plan, _ = await _load_owned_item_context(
        item_id,
        user.id,
        db,
        for_update=True,
    )
    if int(getattr(live_plan, "ownership_epoch", 0) or 0) != ownership_epoch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        )
    prev = item.voiceover_script if isinstance(item.voiceover_script, dict) else {}
    version = int(prev.get("version", 0)) + 1
    script = VoiceoverScript(
        version=version,
        text=text,
        read_time_s=estimate_read_time_s(text),
        brief=body.brief,
        footage_summary=body.footage_summary,
        interview_turns=[],
        lines=lines,
        source="generated",
    )
    item.voiceover_script = script.model_dump()
    await db.commit()
    return TranscriptScriptResponse(
        version=version,
        text=text,
        read_time_s=script.read_time_s,
        lines=lines,
        source="generated",
    )


class TranscriptScriptEditBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@router.patch("/{item_id}/transcript/script", response_model=TranscriptScriptResponse)
async def transcript_script_edit(
    item_id: str,
    body: TranscriptScriptEditBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TranscriptScriptResponse:
    """Persist an inline-edited script in place: same version, source='edited', and
    the SAME server-side sentence/clause line split the teleprompter reads (the
    client can only newline-split, which collapses prose to one line)."""
    _require_transcript_helper()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    from app.agents.voiceover_script_writer import (  # noqa: PLC0415
        _sanitize_script,
        split_script_lines,
    )
    from app.schemas.voiceover_script import VoiceoverScript, estimate_read_time_s  # noqa: PLC0415

    prev = item.voiceover_script if isinstance(item.voiceover_script, dict) else None
    if not prev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No script to edit yet.")
    text = _sanitize_script(body.text)
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Script can't be empty."
        )
    lines = split_script_lines(text)
    updated = {
        **prev,
        "text": text,
        "read_time_s": estimate_read_time_s(text),
        "lines": lines,
        "source": "edited",
    }
    VoiceoverScript(**updated)  # validate the merged doc before persisting
    item.voiceover_script = updated  # new dict → SQLAlchemy detects the JSONB change
    await db.commit()
    return TranscriptScriptResponse(
        version=int(updated["version"]),
        text=text,
        read_time_s=int(updated["read_time_s"]),
        lines=lines,
        source="edited",
    )


@router.post("/{item_id}/transcript/recorded")
async def transcript_recorded(
    item_id: str,
    body: TranscriptRecordedBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pin the script version the currently-attached take was recorded against."""
    _require_transcript_helper()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    item.voiceover_script_recorded_version = int(body.version)
    await db.commit()
    return {"ok": True}


# ── Asset-pool routes (auto-placement PR0, plans/005) ────────────────────────
#
# The per-item visual asset pool that feeds the overlay auto-placement matcher.
# All routes 404 when OVERLAY_AUTOPLACE_ENABLED is off (dual-flag: the frontend
# twin is NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED — keep Fly + Vercel in sync).
# Objects land under the persistent users/{uid}/plan/{item_id}/pool/ prefix.

_MAX_POOL_ASSETS = 20  # plan 005 finding 9: cap + dedupe keep analysis spend bounded
_MAX_POOL_CONTEXT_CHARS = 500
_POOL_RESERVATION_TTL = timedelta(minutes=15)
_POOL_RESERVATION_CLEANUP_GRACE = timedelta(minutes=15)
_MAX_POOL_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_POOL_VIDEO_BYTES = 512 * 1024 * 1024
_POOL_REUSABLE_STATUSES = {"uploaded", "queued", "analyzing", "ready", "failed"}


def _pool_asset_counts_toward_capacity(now: datetime):
    """Count committed assets and only reservations still inside their grace window."""
    reservation_cutoff = now - _POOL_RESERVATION_CLEANUP_GRACE
    legacy_cutoff = now - (_POOL_RESERVATION_TTL + _POOL_RESERVATION_CLEANUP_GRACE)
    return or_(
        PlanItemAsset.status.notin_({"preparing", "promoting"}),
        and_(
            PlanItemAsset.status.in_({"preparing", "promoting"}),
            or_(
                PlanItemAsset.upload_expires_at >= reservation_cutoff,
                and_(
                    PlanItemAsset.upload_expires_at.is_(None),
                    PlanItemAsset.created_at >= legacy_cutoff,
                ),
            ),
        ),
    )


def _pool_reservation_is_expired(reservation: PlanItemAsset, now: datetime) -> bool:
    expires_at = getattr(reservation, "upload_expires_at", None)
    if isinstance(expires_at, datetime):
        return expires_at < now - _POOL_RESERVATION_CLEANUP_GRACE
    created_at = getattr(reservation, "created_at", None)
    return isinstance(created_at, datetime) and created_at < now - (
        _POOL_RESERVATION_TTL + _POOL_RESERVATION_CLEANUP_GRACE
    )


def _require_autoplace() -> None:
    from app.config import settings as _settings  # noqa: PLC0415

    if not _settings.overlay_autoplace_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Auto-placement not available."
        )


def _require_asset_pool() -> None:
    from app.config import settings as _settings  # noqa: PLC0415

    if not (
        _settings.overlay_autoplace_enabled
        or _settings.visual_blocks_enabled
        or _settings.guided_edit_capability_enabled
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Visual asset pool not available."
        )


def _asset_kind_for_content_type(content_type: str) -> str:
    return "video" if content_type.startswith("video/") else "image"


class PoolUploadFile(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    file_size_bytes: int
    client_upload_id: str | None = Field(default=None, min_length=1, max_length=128)


class PoolUploadUrlsBody(BaseModel):
    files: list[PoolUploadFile] = Field(min_length=1, max_length=_MAX_POOL_ASSETS)


class PoolUploadTarget(BaseModel):
    reservation_id: str
    client_upload_id: str
    upload_url: str
    gcs_path: str
    expires_at: datetime
    upload_headers: dict[str, str]


class PoolUploadUrlsResponse(BaseModel):
    urls: list[PoolUploadTarget]


async def _cleanup_reserved_pool_path(path: str, *, reservation_id: str) -> None:
    """Require idempotent cleanup before forgetting or rotating a reservation path."""
    cleaned = await asyncio.to_thread(storage.delete_object_best_effort, path)
    if cleaned:
        return
    log.warning(
        "pool_asset_reservation_cleanup_failed",
        reservation_id=reservation_id,
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "message": "Kria couldn't refresh this upload yet. Retry in a moment.",
            "code": "upload_cleanup_temporarily_unavailable",
            "retryable": True,
            "stage": "reservation_cleanup",
        },
    )


async def _cleanup_expired_pool_reservation(reservation: PlanItemAsset) -> None:
    """Remove every object an abandoned preparing/promotion claim may own."""
    targets: list[tuple[str, str | None]] = [
        (reservation.gcs_path, getattr(reservation, "gcs_generation", None))
    ]
    promotion = (
        (reservation.analysis or {}).get("_upload_promotion")
        if isinstance(reservation.analysis, dict)
        else None
    )
    if isinstance(promotion, dict):
        source_path = promotion.get("source_path")
        source_generation = promotion.get("source_generation")
        destination_path = promotion.get("destination_path")
        if isinstance(source_path, str) and source_path:
            targets[0] = (
                source_path,
                str(source_generation) if source_generation else None,
            )
        if isinstance(destination_path, str) and destination_path:
            targets.append((destination_path, None))

    for path, generation in dict.fromkeys(targets):
        cleaned = await asyncio.to_thread(
            storage.delete_object_generation_best_effort
            if generation
            else storage.delete_object_best_effort,
            path,
            **({"generation": str(generation)} if generation else {}),
        )
        if not cleaned:
            log.warning(
                "pool_asset_reservation_cleanup_failed",
                reservation_id=str(reservation.id),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "Kria couldn't refresh this upload yet. Retry in a moment.",
                    "code": "upload_cleanup_temporarily_unavailable",
                    "retryable": True,
                    "stage": "reservation_cleanup",
                },
            )


@router.post("/{item_id}/assets/upload-urls", response_model=PoolUploadUrlsResponse)
async def create_pool_upload_urls(
    item_id: str,
    body: PoolUploadUrlsBody,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PoolUploadUrlsResponse:
    """Signed PUT URLs for pool assets (same content-type set as overlay cards)."""
    _require_asset_pool()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    now = datetime.now(UTC)
    correlation_id = request.state.correlation_id
    client_ids = [f.client_upload_id or uuid.uuid4().hex for f in body.files]
    if len(set(client_ids)) != len(client_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Each selected file needs a unique upload ID.",
        )
    # Validate the entire batch before object cleanup or reservation mutation.
    # A bad later entry must not rotate an earlier retry target and then roll
    # its database update back while the old object is already gone.
    for f in body.files:
        if f.content_type not in _OVERLAY_ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported asset content type: {f.content_type}",
            )
        limit = (
            _MAX_POOL_VIDEO_BYTES
            if _asset_kind_for_content_type(f.content_type) == "video"
            else _MAX_POOL_IMAGE_BYTES
        )
        if f.file_size_bytes <= 0 or f.file_size_bytes > limit:
            max_label = "512 MB" if limit == _MAX_POOL_VIDEO_BYTES else "25 MB"
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{_asset_kind_for_content_type(f.content_type).title()} files must be "
                    f"{max_label} or smaller."
                ),
            )

    stale = (
        (
            await db.execute(
                select(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.status.in_({"preparing", "promoting"}),
                    or_(
                        PlanItemAsset.upload_expires_at < now - _POOL_RESERVATION_CLEANUP_GRACE,
                        and_(
                            PlanItemAsset.upload_expires_at.is_(None),
                            PlanItemAsset.created_at
                            < now - (_POOL_RESERVATION_TTL + _POOL_RESERVATION_CLEANUP_GRACE),
                        ),
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in stale:
        await _cleanup_expired_pool_reservation(row)
        await db.delete(row)

    existing_by_client = {
        row.client_upload_id: row
        for row in (
            (
                await db.execute(
                    select(PlanItemAsset)
                    .where(
                        PlanItemAsset.plan_item_id == item.id,
                        PlanItemAsset.client_upload_id.in_(client_ids),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if row.client_upload_id
    }
    occupied = int(
        (
            await db.execute(
                select(func.count())
                .select_from(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == item.id,
                    _pool_asset_counts_toward_capacity(now),
                )
            )
        ).scalar_one()
    )
    new_count = sum(client_id not in existing_by_client for client_id in client_ids)
    if occupied + new_count > _MAX_POOL_ASSETS:
        remaining = max(0, _MAX_POOL_ASSETS - occupied)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Your visuals pool is full. Remove a visual before adding another."
                if remaining == 0
                else f"Your visuals pool has room for {remaining} more. Select up to {remaining}."
            ),
        )
    log.info(
        "pool_asset_upload_urls_requested",
        item_id=str(item.id),
        batch_size=len(body.files),
        request_id=request.state.request_id,
        correlation_id=correlation_id,
    )
    reservations: list[tuple[PlanItemAsset, bool]] = []
    for f, client_upload_id in zip(body.files, client_ids, strict=True):
        reservation = existing_by_client.get(client_upload_id)
        if reservation is not None:
            if reservation.status != "preparing":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This file is already in your visuals pool.",
                )
            if (
                reservation.upload_content_type != f.content_type
                or reservation.upload_size_bytes != f.file_size_bytes
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This upload retry does not match the originally selected file.",
                )
            # Keep one durable key per reservation. Previously issued signed
            # URLs cannot be revoked; rotating the key let a late PUT recreate
            # an untracked object in the persistent pool prefix.
            await _cleanup_reserved_pool_path(
                reservation.gcs_path,
                reservation_id=str(reservation.id),
            )
        else:
            reservation_id = uuid.uuid4()
            safe_name = f"{uuid.uuid4().hex}-{f.filename.split('/')[-1]}"
            # Every signed target stages under the 24h lifecycle prefix. A valid
            # URL can outlive reservation deletion and cannot be revoked; only
            # registration promotes its verified generation into persistent
            # pool storage, so replay never creates an immortal orphan.
            gcs_path = (
                f"dev-user/{user.id}/plan-pool-reservations/{item.id}/{reservation_id}/{safe_name}"
            )
            reservation = PlanItemAsset(
                id=reservation_id,
                plan_item_id=item.id,
                user_id=user.id,
                gcs_path=gcs_path,
                kind=_asset_kind_for_content_type(f.content_type),
                source_filename=f.filename.split("/")[-1],
                client_upload_id=client_upload_id,
                upload_content_type=f.content_type,
                upload_size_bytes=f.file_size_bytes,
                status="preparing",
            )
            db.add(reservation)
        reservation.upload_expires_at = now + _POOL_RESERVATION_TTL
        reservation.correlation_id = correlation_id
        reservations.append((reservation, f.client_upload_id is not None))
    await db.commit()

    urls: list[PoolUploadTarget] = []
    for reservation, strict_headers in reservations:
        await db.refresh(reservation)
        content_type = reservation.upload_content_type or "application/octet-stream"
        if strict_headers:
            upload_url = await asyncio.to_thread(
                storage.signed_put_url,
                reservation.gcs_path,
                content_type,
                int(reservation.upload_size_bytes or 0),
            )
            upload_headers = {"x-goog-if-generation-match": "0"}
        else:
            upload_url = await asyncio.to_thread(
                storage.signed_put_url_legacy,
                reservation.gcs_path,
                content_type,
                int(reservation.upload_size_bytes or 0),
            )
            upload_headers = {}
        urls.append(
            PoolUploadTarget(
                reservation_id=str(reservation.id),
                client_upload_id=reservation.client_upload_id or "",
                upload_url=upload_url,
                gcs_path=reservation.gcs_path,
                expires_at=reservation.upload_expires_at or now,
                upload_headers=upload_headers,
            )
        )
    return PoolUploadUrlsResponse(urls=urls)


class RegisterAssetBody(BaseModel):
    reservation_id: str | None = None
    gcs_path: str
    content_type: str
    content_hash: str | None = None
    source_filename: str | None = None
    user_context: str | None = Field(default=None, max_length=_MAX_POOL_CONTEXT_CHARS)


async def _delete_verified_pool_upload(
    gcs_path: str,
    generation: str,
    *,
    reservation_id: str | None,
) -> None:
    """Delete validated bytes without losing the row needed to retry cleanup."""
    try:
        await asyncio.to_thread(
            storage.delete_object_generation,
            gcs_path,
            generation=generation,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pool_asset_generation_cleanup_failed",
            reservation_id=reservation_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Kria couldn't finish cleaning up this upload. Retry in a moment.",
                "code": "upload_cleanup_temporarily_unavailable",
                "retryable": True,
                "stage": "registration_cleanup",
            },
        ) from exc


class PoolAssetOut(BaseModel):
    id: str
    kind: str
    status: str
    error_code: str | None = None
    error_detail: str | None = None
    retryable: bool = False
    source_filename: str | None
    duration_s: float | None
    aspect: float | None
    # Pixel dims (plan 009 E1) — from the ANALYSIS_VERSION-3 analysis JSONB;
    # None on legacy assets until the backfill re-analyzes them. Feed the FE
    # low-res warning (min(w,h) < 720) — never fake them client-side.
    width: int | None = None
    height: int | None = None
    subject: str | None  # analysis micro-label for the pool tile (2A state table)
    # Creator-authored context is intentionally separate from Nova analysis.
    user_context: str = ""
    nova_description: str | None = None
    nova_on_screen_text: str | None = None
    # Brand/mascot identities from the analysis JSONB (ANALYSIS_VERSION 5,
    # brand-aware matching) — surfaced so detection is verifiable from the pool
    # tile. None on pre-v5 analyses until the backfill re-analyzes them;
    # [] means analyzed with nothing detected.
    brands: list[str] | None = None
    display_url: str | None
    # Signed preview URL (pool asset preview pipeline). Images fold their
    # preview into display_url directly; this is populated for videos, whose
    # display_url stays the signed raw video so playback still works. None
    # when no preview was generated (never attempted or attempted-and-failed).
    preview_url: str | None = None
    deduped: bool = False
    # Object key under users/{uid}/plan/{item_id}/pool/ — inside attach_clips'
    # allowed prefix, so the frontend "Use in edit" promotion can re-register the
    # same object as a clip without a copy or a new endpoint. Required (no default):
    # a constructor that forgets it must fail response validation loudly, never
    # ship an empty path the promotion would 422 on.
    gcs_path: str
    source_type: str | None = None
    source_clip_index: int | None = None
    source_timestamp_s: float | None = None


def _clean_pool_asset_context(value: str | None) -> str | None:
    cleaned = _sanitize_text(str(value or "")).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_POOL_CONTEXT_CHARS]


def _asset_out(asset: PlanItemAsset, *, deduped: bool = False) -> PoolAssetOut:
    from app.config import settings as _settings  # noqa: PLC0415

    analysis = asset.analysis or {}
    if not isinstance(analysis, dict):
        analysis = {}
    # "" is the attempted-and-failed sentinel — treat it the same as never
    # attempted (fall back / omit), never sign it as a path.
    raw_preview_path = getattr(asset, "preview_gcs_path", None)
    preview_path = raw_preview_path or None
    display_url: str | None = None
    preview_url: str | None = None
    try:
        display_url = storage.signed_get_url(
            asset.gcs_path if asset.kind == "video" else (preview_path or asset.gcs_path),
            expiration_minutes=60,
        )
    except Exception:  # noqa: BLE001 — thumbnail signing is best-effort, never 500s the list
        display_url = None
    if asset.kind == "video" and preview_path:
        try:
            preview_url = storage.signed_get_url(preview_path, expiration_minutes=60)
        except Exception:  # noqa: BLE001 — preview signing is best-effort, never 500s the list
            preview_url = None
    # str() coercion: a corrupt JSONB element must degrade, never fail response
    # validation and 500 the whole list.
    raw_brands = analysis.get("brands")
    brands = [str(b) for b in raw_brands if b] if isinstance(raw_brands, list) else None
    raw_user_context = getattr(asset, "user_context", None)
    user_context = raw_user_context.strip()[:500] if isinstance(raw_user_context, str) else ""
    raw_description = analysis.get("description")
    nova_description = (
        str(raw_description).strip()[:400]
        if isinstance(raw_description, str) and raw_description.strip()
        else None
    )

    raw_on_screen_text = analysis.get("on_screen_text")
    nova_on_screen_text = (
        str(raw_on_screen_text).strip()[:400]
        if isinstance(raw_on_screen_text, str) and raw_on_screen_text.strip()
        else None
    )
    raw_error_code = getattr(asset, "error_code", None)
    raw_error_detail = getattr(asset, "error_detail", None)
    raw_retryable = getattr(asset, "error_retryable", False)
    visible_status = asset.status
    if visible_status == "queued" and not _settings.pool_asset_queued_status_enabled:
        visible_status = "uploaded"
    return PoolAssetOut(
        id=str(asset.id),
        kind=asset.kind,
        status=visible_status,
        error_code=raw_error_code if isinstance(raw_error_code, str) else None,
        error_detail=raw_error_detail if isinstance(raw_error_detail, str) else None,
        retryable=raw_retryable if isinstance(raw_retryable, bool) else False,
        source_filename=asset.source_filename,
        duration_s=asset.duration_s,
        aspect=asset.aspect,
        width=analysis.get("width"),
        height=analysis.get("height"),
        subject=analysis.get("subject"),
        user_context=user_context,
        nova_description=nova_description,
        nova_on_screen_text=nova_on_screen_text,
        brands=brands,
        display_url=display_url,
        preview_url=preview_url,
        deduped=deduped,
        gcs_path=asset.gcs_path,
        source_type=str(analysis.get("source") or "") or None,
        source_clip_index=analysis.get("source_clip_index"),
        source_timestamp_s=analysis.get("source_timestamp_s"),
    )


async def _pool_promotion_is_durable(
    asset_id: uuid.UUID,
    *,
    path: str,
    generation: str,
) -> bool | None:
    """Resolve an ambiguous COMMIT without risking deletion of durable bytes.

    A driver can report a connection error after PostgreSQL has committed. Use a
    fresh connection to distinguish that case from a definite rollback. Unknown
    results deliberately preserve the generation for idempotent retry/reconcile.
    """
    from app.database import AsyncSessionLocal  # noqa: PLC0415

    try:
        async with AsyncSessionLocal() as verify_db:
            durable = await verify_db.get(PlanItemAsset, asset_id)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "pool_asset_promotion_commit_unresolved",
            reservation_id=str(asset_id),
            error_type=type(exc).__name__,
        )
        return None
    if durable is None:
        return False
    return (
        durable.gcs_path == path
        and str(durable.gcs_generation or "") == str(generation)
        and durable.status in {"uploaded", "queued", "analyzing", "ready", "failed"}
    )


_ANALYSIS_DISPATCH_ERROR_CODE = "analysis_temporarily_unavailable"
_ANALYSIS_DISPATCH_ERROR_DETAIL = "Kria couldn't start analyzing this file. Try again."


async def _queue_pool_asset_analysis(
    asset: PlanItemAsset,
    db: AsyncSession,
    *,
    reset_attempts: bool = False,
) -> None:
    """Persist a fenced queued attempt, then publish it best-effort.

    The row is committed before broker publication so workers can always claim
    it. A publish failure is made terminal and actionable instead of leaving the
    UI polling an `uploaded` row forever.
    """
    from app.config import settings as _settings  # noqa: PLC0415
    from app.tasks.autoplace import analyze_pool_asset  # noqa: PLC0415

    attempt_token = uuid.uuid4().hex
    prior_attempts = int(getattr(asset, "analysis_attempt_count", 0) or 0)
    asset.status = "queued"
    asset.error_code = None
    asset.error_detail = None
    asset.error_retryable = False
    asset.analysis_attempt_token = attempt_token
    asset.analysis_attempt_count = 1 if reset_attempts else prior_attempts + 1
    asset.analysis_last_dispatched_at = datetime.now(UTC)
    asset.analysis_started_at = None
    await db.commit()
    await db.refresh(asset)

    try:
        task_headers = {"pool_asset_attempt_token": attempt_token}
        if asset.correlation_id:
            task_headers["x-correlation-id"] = asset.correlation_id
        analyze_pool_asset.apply_async(
            args=[str(asset.id), False],
            queue=_settings.pool_asset_analysis_queue,
            headers=task_headers,
        )
        log.info(
            "pool_asset_analysis_queued",
            asset_id=str(asset.id),
            queue=_settings.pool_asset_analysis_queue,
            attempt=asset.analysis_attempt_count,
        )
    except Exception as exc:  # noqa: BLE001
        asset.status = "failed"
        asset.error_code = _ANALYSIS_DISPATCH_ERROR_CODE
        asset.error_detail = _ANALYSIS_DISPATCH_ERROR_DETAIL
        asset.error_retryable = True
        await db.commit()
        await db.refresh(asset)
        log.warning(
            "pool_asset_analysis_dispatch_failed",
            asset_id=str(asset.id),
            attempt=asset.analysis_attempt_count,
            error_type=type(exc).__name__,
        )


@router.post("/{item_id}/assets", response_model=PoolAssetOut)
async def register_pool_asset(
    item_id: str,
    body: RegisterAssetBody,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PoolAssetOut:
    """Register an uploaded pool asset.

    Dedupe: an existing row with the same content_hash on this item is returned
    as-is (`deduped=true`) — identical bytes are never re-analyzed (plan 005
    finding 9). New rows enter `queued`; broker failures become retryable
    terminal failures instead of polling forever.
    """
    _require_asset_pool()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    _pool_prefix = f"users/{user.id}/plan/{item.id}/pool/"
    _staging_prefix = f"dev-user/{user.id}/plan-pool-reservations/{item.id}/"
    if not body.gcs_path.startswith((_pool_prefix, _staging_prefix)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Upload path is not valid for this item.",
        )
    if body.content_type not in _OVERLAY_ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported asset content type: {body.content_type}",
        )
    reservation: PlanItemAsset | None = None
    metadata = None
    if body.reservation_id is not None:
        try:
            reservation_uuid = uuid.UUID(body.reservation_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Upload reservation not found.") from exc
        reservation = (
            await db.execute(
                select(PlanItemAsset)
                .where(
                    PlanItemAsset.id == reservation_uuid,
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.user_id == user.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if reservation is None:
            raise HTTPException(status_code=404, detail="Upload reservation not found.")
        if reservation.status not in {"preparing", "promoting"}:
            if reservation.status == "uploaded":
                retry_staging_prefix = f"{_staging_prefix}{reservation.id}/"
                if body.gcs_path != reservation.gcs_path and not body.gcs_path.startswith(
                    retry_staging_prefix
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Upload target does not match its reservation.",
                    )
                await _queue_pool_asset_analysis(reservation, db)
                if body.gcs_path.startswith(retry_staging_prefix):
                    cleaned = await asyncio.to_thread(
                        storage.delete_object_best_effort,
                        body.gcs_path,
                    )
                    if not cleaned:
                        log.warning(
                            "pool_asset_staging_cleanup_deferred",
                            reservation_id=str(reservation.id),
                        )
                return _asset_out(reservation)
            if reservation.status in {"queued", "analyzing", "ready", "failed"}:
                return _asset_out(reservation)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This upload reservation is no longer available.",
            )
    else:
        # Compatibility for deployed path-based clients, with an ownership
        # fence: a path already referenced by this item is never treated as a
        # disposable new upload. Adopt a preparing reservation or return the
        # finalized asset idempotently; cleanup-only states stay unavailable.
        path_owner = (
            await db.execute(
                select(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.user_id == user.id,
                    PlanItemAsset.gcs_path == body.gcs_path,
                )
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if path_owner is not None:
            if path_owner.status in {"preparing", "promoting"}:
                reservation = path_owner
            elif path_owner.status == "uploaded":
                await _queue_pool_asset_analysis(path_owner, db)
                return _asset_out(path_owner)
            elif path_owner.status in _POOL_REUSABLE_STATUSES:
                return _asset_out(path_owner)
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This upload reservation is no longer available.",
                )
        elif body.gcs_path.startswith(_staging_prefix):
            # After promotion `gcs_path` points at the persistent copy. Recover
            # a lost legacy registration response from the reservation UUID
            # embedded in the original staging key.
            relative = body.gcs_path.removeprefix(_staging_prefix)
            try:
                staged_reservation_id = uuid.UUID(relative.split("/", 1)[0])
            except (ValueError, IndexError) as exc:
                raise HTTPException(
                    status_code=404, detail="Upload reservation not found."
                ) from exc
            promoted = (
                await db.execute(
                    select(PlanItemAsset)
                    .where(
                        PlanItemAsset.id == staged_reservation_id,
                        PlanItemAsset.plan_item_id == item.id,
                        PlanItemAsset.user_id == user.id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if promoted is None or not promoted.gcs_path.startswith(_pool_prefix):
                raise HTTPException(status_code=404, detail="Upload reservation not found.")
            if promoted.status == "uploaded":
                await _queue_pool_asset_analysis(promoted, db)
                await _cleanup_reserved_pool_path(
                    body.gcs_path,
                    reservation_id=str(promoted.id),
                )
                return _asset_out(promoted)
            if promoted.status in {"queued", "analyzing", "ready", "failed"}:
                return _asset_out(promoted)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This upload reservation is no longer available.",
            )
    if reservation is not None and _pool_reservation_is_expired(reservation, datetime.now(UTC)):
        await _cleanup_expired_pool_reservation(reservation)
        await db.delete(reservation)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "This upload link expired. Retry the upload to get a new link.",
                "code": "upload_reservation_expired",
                "retryable": True,
                "stage": "transfer",
            },
        )
    if reservation is not None and reservation.gcs_path != body.gcs_path:
        raise HTTPException(
            status_code=409,
            detail="Upload target does not match its reservation.",
        )

    try:
        metadata = await asyncio.to_thread(storage.object_metadata, body.gcs_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The upload has not finished yet. Retry the upload, then add it again.",
        ) from exc

    expected_type = reservation.upload_content_type if reservation else body.content_type
    expected_size = reservation.upload_size_bytes if reservation else metadata.size
    kind = _asset_kind_for_content_type(expected_type or body.content_type)
    limit = _MAX_POOL_VIDEO_BYTES if kind == "video" else _MAX_POOL_IMAGE_BYTES
    mismatch = (
        metadata.content_type.split(";", 1)[0].strip() != expected_type
        or metadata.size != expected_size
        or metadata.size <= 0
        or metadata.size > limit
    )
    if mismatch:
        await _delete_verified_pool_upload(
            body.gcs_path,
            metadata.generation,
            reservation_id=str(reservation.id) if reservation else None,
        )
        if reservation is not None:
            await db.delete(reservation)
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The uploaded file did not match the selected file. Choose it again and retry.",
        )

    if body.content_hash:
        existing = (
            await db.execute(
                select(PlanItemAsset).where(
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.content_hash == body.content_hash,
                    PlanItemAsset.id != (reservation.id if reservation else uuid.UUID(int=0)),
                    PlanItemAsset.status.in_(_POOL_REUSABLE_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status == "uploaded":
                await _queue_pool_asset_analysis(existing, db)
            cleaned_context = _clean_pool_asset_context(body.user_context)
            if body.user_context is not None and existing.user_context != cleaned_context:
                existing.user_context = cleaned_context
                from app.services.edit_proposals import mark_edit_proposal_stale  # noqa: PLC0415

                mark_edit_proposal_stale(item)
                await db.commit()
                await db.refresh(existing)
            same_retained_object = existing.gcs_path == body.gcs_path and str(
                getattr(existing, "gcs_generation", "")
            ) == str(metadata.generation)
            if not same_retained_object:
                await _delete_verified_pool_upload(
                    body.gcs_path,
                    metadata.generation,
                    reservation_id=str(reservation.id) if reservation else None,
                )
            if reservation is not None:
                await db.delete(reservation)
                await db.commit()
            return _asset_out(existing, deduped=True)
    if reservation is None:
        count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlanItemAsset)
                    .where(
                        PlanItemAsset.plan_item_id == item.id,
                        _pool_asset_counts_toward_capacity(datetime.now(UTC)),
                    )
                )
            ).scalar_one()
        )
        if count >= _MAX_POOL_ASSETS:
            await _delete_verified_pool_upload(
                body.gcs_path,
                metadata.generation,
                reservation_id=None,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Pool is capped at {_MAX_POOL_ASSETS} assets per item.",
            )
    staging_cleanup: tuple[str, str] | None = None
    if reservation is not None and body.gcs_path.startswith(_staging_prefix):
        source_generation = metadata.generation
        promotion_state = (
            (reservation.analysis or {}).get("_upload_promotion")
            if isinstance(reservation.analysis, dict)
            else None
        )
        if reservation.status == "promoting" and isinstance(promotion_state, dict):
            persistent_path = str(promotion_state.get("destination_path") or "")
            if (
                promotion_state.get("source_path") != body.gcs_path
                or str(promotion_state.get("source_generation") or "") != source_generation
                or not persistent_path.startswith(_pool_prefix)
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This upload reservation is no longer available.",
                )
        else:
            persistent_name = (
                f"{reservation.id}-{(reservation.source_filename or 'asset').split('/')[-1]}"
            )
            persistent_path = f"users/{user.id}/plan/{item.id}/pool/{persistent_name}"
            reservation.status = "promoting"
            reservation.upload_expires_at = datetime.now(UTC) + _POOL_RESERVATION_TTL
            reservation.analysis = {
                "_upload_promotion": {
                    "source_path": body.gcs_path,
                    "source_generation": source_generation,
                    "destination_path": persistent_path,
                }
            }
            try:
                # This durable claim precedes the only write into lifecycle-
                # exempt storage, making every later failure reconcilable.
                await db.commit()
                await db.refresh(reservation)
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "message": "The file uploaded, but Kria couldn't add it to your visuals.",
                        "code": "registration_temporarily_unavailable",
                        "retryable": True,
                        "stage": "registration",
                    },
                ) from exc
        try:
            metadata = await asyncio.to_thread(
                storage.copy_object_generation,
                body.gcs_path,
                persistent_path,
                source_generation=source_generation,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "pool_asset_staging_promotion_failed",
                reservation_id=str(reservation.id),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "The file uploaded, but Kria couldn't add it to your visuals.",
                    "code": "registration_temporarily_unavailable",
                    "retryable": True,
                    "stage": "registration",
                },
            ) from exc
        if (
            metadata.size != expected_size
            or metadata.content_type.split(";", 1)[0].strip() != expected_type
        ):
            log.error(
                "pool_asset_staging_promotion_mismatch",
                reservation_id=str(reservation.id),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "The file uploaded, but Kria couldn't add it to your visuals.",
                    "code": "registration_temporarily_unavailable",
                    "retryable": True,
                    "stage": "registration",
                },
            )
        reservation.gcs_path = persistent_path
        staging_cleanup = (body.gcs_path, source_generation)
    asset = reservation or PlanItemAsset(
        plan_item_id=item.id,
        user_id=user.id,
        gcs_path=body.gcs_path,
        kind=kind,
    )
    if reservation is None:
        db.add(asset)
    asset.content_hash = body.content_hash
    asset.source_filename = body.source_filename or asset.source_filename
    asset.user_context = _clean_pool_asset_context(body.user_context)
    asset.gcs_generation = metadata.generation
    asset.upload_content_type = expected_type
    asset.upload_size_bytes = metadata.size
    asset.analysis = None
    asset.correlation_id = request.state.correlation_id or asset.correlation_id
    asset.status = "uploaded" if staging_cleanup is not None else "queued"
    from app.services.edit_proposals import mark_edit_proposal_stale  # noqa: PLC0415

    mark_edit_proposal_stale(item)
    if staging_cleanup is not None:
        promoted_asset_id = asset.id
        promoted_path = asset.gcs_path
        promoted_generation = asset.gcs_generation
        try:
            # Make the persistent generation durable before queue publication.
            # The earlier promoting claim remains the cleanup/retry authority
            # when this commit rolls back or its outcome is ambiguous.
            await db.commit()
            await db.refresh(asset)
        except Exception as exc:  # noqa: BLE001
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001 — fresh-session check is authoritative
                pass
            durable = await _pool_promotion_is_durable(
                promoted_asset_id,
                path=promoted_path,
                generation=str(promoted_generation),
            )
            log.warning(
                "pool_asset_promotion_commit_failed",
                reservation_id=str(promoted_asset_id),
                durable=durable,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "The file uploaded, but Kria couldn't add it to your visuals.",
                    "code": "registration_temporarily_unavailable",
                    "retryable": True,
                    "stage": "registration",
                },
            ) from exc
    await _queue_pool_asset_analysis(asset, db)
    if staging_cleanup is not None:
        staging_path, staging_generation = staging_cleanup
        try:
            await asyncio.to_thread(
                storage.delete_object_generation,
                staging_path,
                generation=staging_generation,
            )
        except Exception as exc:  # noqa: BLE001 — 24h lifecycle is the fallback
            log.warning(
                "pool_asset_staging_cleanup_deferred",
                reservation_id=str(asset.id),
                error_type=type(exc).__name__,
            )
    return _asset_out(asset)


@router.post("/{item_id}/assets/upload", response_model=PoolAssetOut)
async def upload_pool_asset(
    item_id: str,
    request: Request,
    user: CurrentUser,
    file: MultipartFile = File(...),  # noqa: B008
    db: AsyncSession = Depends(get_db),
) -> PoolAssetOut:
    """Compatibility multipart upload through the shared staged registrar."""
    import hashlib  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    _require_asset_pool()
    _, plan, _ = await _load_owned_item_context(item_id, user.id, db)
    ownership_epoch = int(getattr(plan, "ownership_epoch", 0) or 0)
    # Do not retain a transaction or ownership row lock while the client body
    # streams and storage upload run.  The epoch snapshot is revalidated at the
    # only persistence boundary below.
    await db.rollback()

    content_type = (file.content_type or "").split(";")[0].strip()
    if content_type not in _OVERLAY_ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported asset content type: {content_type or 'unknown'}",
        )

    kind = _asset_kind_for_content_type(content_type)
    upload_limit = _MAX_POOL_VIDEO_BYTES if kind == "video" else _MAX_POOL_IMAGE_BYTES
    hasher = hashlib.sha256()
    total = 0
    tmp_path: str | None = None
    try:
        # Hash to disk, never buffering the whole file in process memory.
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > upload_limit:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            "File too large (512 MB max for videos)."
                            if kind == "video"
                            else "File too large (25 MB max for images)."
                        ),
                    )
                hasher.update(chunk)
                tmp.write(chunk)
        if total == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")
        content_hash = hasher.hexdigest()
        source_filename = (file.filename or "asset").split("/")[-1]
        locked_item, locked_plan, _ = await _load_owned_item_context(
            item_id,
            user.id,
            db,
            for_update=True,
        )
        if int(getattr(locked_plan, "ownership_epoch", 0) or 0) != ownership_epoch:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            )

        existing = (
            await db.execute(
                select(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == locked_item.id,
                    PlanItemAsset.content_hash == content_hash,
                    PlanItemAsset.status.in_(_POOL_REUSABLE_STATUSES),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status == "uploaded":
                await _queue_pool_asset_analysis(existing, db)
            await db.rollback()
            return _asset_out(existing, deduped=True)

        count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlanItemAsset)
                    .where(
                        PlanItemAsset.plan_item_id == locked_item.id,
                        _pool_asset_counts_toward_capacity(datetime.now(UTC)),
                    )
                )
            ).scalar_one()
        )
        if count >= _MAX_POOL_ASSETS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Pool is capped at {_MAX_POOL_ASSETS} assets per item.",
            )

        reservation_id = uuid.uuid4()
        safe_name = f"{uuid.uuid4().hex}-{source_filename}"
        gcs_path = (
            f"dev-user/{user.id}/plan-pool-reservations/{locked_item.id}/"
            f"{reservation_id}/{safe_name}"
        )
        reservation = PlanItemAsset(
            id=reservation_id,
            plan_item_id=locked_item.id,
            user_id=user.id,
            gcs_path=gcs_path,
            kind=kind,
            content_hash=content_hash,
            source_filename=source_filename,
            status="preparing",
            upload_content_type=content_type,
            upload_size_bytes=total,
            upload_expires_at=datetime.now(UTC) + _POOL_RESERVATION_TTL,
            correlation_id=request.state.correlation_id,
        )
        db.add(reservation)
        await db.commit()
        await db.refresh(reservation)

        # Staging is lifecycle-covered even if the provider raises after writing.
        await asyncio.to_thread(storage.upload_local_file, tmp_path, gcs_path, content_type)
        return await register_pool_asset(
            item_id,
            RegisterAssetBody(
                reservation_id=str(reservation.id),
                gcs_path=gcs_path,
                content_type=content_type,
                content_hash=content_hash,
                source_filename=source_filename,
            ),
            request,
            user,
            db,
        )
    finally:
        if tmp_path:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass


class PoolReservationCapacity(BaseModel):
    reservation_id: str
    release_at: datetime | None = None


class PoolAssetsResponse(BaseModel):
    assets: list[PoolAssetOut]
    max_assets: int = _MAX_POOL_ASSETS
    occupied_assets: int = 0
    active_reservations: list[PoolReservationCapacity] = Field(default_factory=list)


@router.get("/{item_id}/assets", response_model=PoolAssetsResponse)
async def list_pool_assets(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PoolAssetsResponse:
    _require_asset_pool()
    item = await _load_owned_item(item_id, user.id, db)
    rows = (
        (
            await db.execute(
                select(PlanItemAsset)
                .where(PlanItemAsset.plan_item_id == item.id)
                .order_by(PlanItemAsset.created_at)
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    hidden_statuses = {"preparing", "promoting", "cleanup_pending"}
    capacity_rows = [
        row
        for row in rows
        if row.status not in {"preparing", "promoting"}
        or not _pool_reservation_is_expired(row, now)
    ]
    assets = [row for row in rows if row.status not in hidden_statuses]
    hidden_capacity_rows = [row for row in capacity_rows if row.status in hidden_statuses]
    reservations = []
    for row in hidden_capacity_rows:
        release_at = None
        if row.status == "cleanup_pending":
            # Cleanup owns this reservation until its exact object generation is
            # deleted.  The original upload expiry is not a release guarantee.
            release_at = None
        elif row.upload_expires_at is not None:
            release_at = row.upload_expires_at + _POOL_RESERVATION_CLEANUP_GRACE
        elif row.created_at is not None and row.status in {"preparing", "promoting"}:
            release_at = row.created_at + _POOL_RESERVATION_TTL + _POOL_RESERVATION_CLEANUP_GRACE
        reservations.append(
            PoolReservationCapacity(reservation_id=str(row.id), release_at=release_at)
        )
    return PoolAssetsResponse(
        assets=[_asset_out(a) for a in assets],
        occupied_assets=len(capacity_rows),
        active_reservations=reservations,
    )


@router.post("/{item_id}/assets/{asset_id}/reanalyze", response_model=PoolAssetOut)
async def reanalyze_pool_asset(
    item_id: str,
    asset_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PoolAssetOut:
    """Idempotently retry a failed/legacy pool-asset analysis."""
    _require_asset_pool()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    try:
        aid = uuid.UUID(asset_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset not found",
        ) from exc
    asset = (
        await db.execute(
            select(PlanItemAsset)
            .where(
                PlanItemAsset.id == aid,
                PlanItemAsset.plan_item_id == item.id,
                PlanItemAsset.user_id == user.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if asset.status in {"queued", "analyzing", "ready"}:
        return _asset_out(asset)
    if asset.status not in {"failed", "uploaded"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This upload must finish before analysis can start.",
        )
    await _queue_pool_asset_analysis(asset, db, reset_attempts=True)
    return _asset_out(asset)


@router.delete("/{item_id}/assets/{asset_id}")
async def delete_pool_asset(
    item_id: str,
    asset_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove an asset and durably reclaim unreferenced persistent bytes."""
    from app.services.pool_asset_refs import (  # noqa: PLC0415
        item_references_pool_path,
        job_references_pool_asset,
    )

    _require_asset_pool()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    try:
        aid = uuid.UUID(asset_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    asset = (
        await db.execute(
            select(PlanItemAsset).where(
                PlanItemAsset.id == aid,
                PlanItemAsset.plan_item_id == item.id,
                PlanItemAsset.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    if asset.status in {"preparing", "promoting"} or (
        asset.status == "cleanup_pending" and not asset.gcs_generation
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This upload reservation cannot be removed while its upload link is active.",
        )
    removed = 0
    locked_job = (
        await db.get(
            Job,
            item.current_job_id,
            populate_existing=True,
            with_for_update=True,
        )
        if item.current_job_id is not None
        else None
    )
    if item_references_pool_path(item, asset.gcs_path) or (
        locked_job is not None
        and locked_job.status != "cancelled"
        and job_references_pool_asset(
            locked_job,
            asset_id=str(asset.id),
            gcs_path=asset.gcs_path,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Replace or remove this file from the edit before deleting it.",
        )
    if locked_job is not None and locked_job.status != "cancelled":
        # Decision 11A: deleting an asset eagerly clears dependent PENDING
        # suggestion rows (staged/accepted cards are real placements — untouched).
        removed = _clear_suggestions_for_asset(locked_job, str(asset.id))
    shared_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(PlanItemAsset)
                .where(
                    PlanItemAsset.id != asset.id,
                    PlanItemAsset.gcs_path == asset.gcs_path,
                    PlanItemAsset.gcs_generation == asset.gcs_generation,
                )
            )
        ).scalar_one()
    )
    if shared_count == 0:
        # Keep the row quota-charged until exact-generation cleanup succeeds.
        previous_status = (
            (asset.analysis or {}).get("_pool_cleanup_previous_status")
            if isinstance(asset.analysis, dict)
            else None
        ) or asset.status
        analysis = dict(asset.analysis) if isinstance(asset.analysis, dict) else {}
        analysis["_pool_cleanup_previous_status"] = previous_status
        asset.analysis = analysis
        asset.status = "cleanup_pending"
        await db.commit()

        # The durable claim releases the first transaction. Reacquire item,
        # asset, and job locks; recheck consumers; then hold all locks through
        # storage deletion so a concurrent editor write cannot win the gap.
        locked_item = await _load_owned_item(item_id, user.id, db, for_update=True)
        locked_asset = (
            await db.execute(
                select(PlanItemAsset)
                .where(
                    PlanItemAsset.id == aid,
                    PlanItemAsset.plan_item_id == locked_item.id,
                    PlanItemAsset.user_id == user.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_asset is None:
            return {"ok": True, "removed_suggestions": removed}
        locked_job = (
            await db.get(
                Job,
                locked_item.current_job_id,
                populate_existing=True,
                with_for_update=True,
            )
            if locked_item.current_job_id is not None
            else None
        )
        if item_references_pool_path(locked_item, locked_asset.gcs_path) or (
            locked_job is not None
            and locked_job.status != "cancelled"
            and job_references_pool_asset(
                locked_job,
                asset_id=str(locked_asset.id),
                gcs_path=locked_asset.gcs_path,
            )
        ):
            restored_analysis = (
                dict(locked_asset.analysis) if isinstance(locked_asset.analysis, dict) else {}
            )
            restored_analysis.pop("_pool_cleanup_previous_status", None)
            locked_asset.analysis = restored_analysis or None
            locked_asset.status = str(previous_status)
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Replace or remove this file from the edit before deleting it.",
            )
        fresh_shared_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(PlanItemAsset)
                    .where(
                        PlanItemAsset.id != locked_asset.id,
                        PlanItemAsset.gcs_path == locked_asset.gcs_path,
                        PlanItemAsset.gcs_generation == locked_asset.gcs_generation,
                    )
                )
            ).scalar_one()
        )
        if fresh_shared_count:
            from app.services.edit_proposals import mark_edit_proposal_stale  # noqa: PLC0415

            mark_edit_proposal_stale(locked_item)
            await db.delete(locked_asset)
            await db.commit()
            return {"ok": True, "removed_suggestions": removed}
        if locked_asset.gcs_generation:
            cleaned = await asyncio.to_thread(
                storage.delete_object_generation_best_effort,
                locked_asset.gcs_path,
                generation=str(locked_asset.gcs_generation),
            )
        else:
            cleaned = await asyncio.to_thread(
                storage.delete_object_best_effort,
                locked_asset.gcs_path,
            )
        if not cleaned:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "Kria couldn't remove this file right now. Retry in a moment.",
                    "code": "asset_cleanup_temporarily_unavailable",
                    "retryable": True,
                    "stage": "remove",
                },
            )
        asset = locked_asset
    from app.services.edit_proposals import mark_edit_proposal_stale  # noqa: PLC0415

    mark_edit_proposal_stale(locked_item if shared_count == 0 else item)
    await db.delete(asset)
    await db.commit()
    return {"ok": True, "removed_suggestions": removed}


def _clear_suggestions_for_asset(job: Job, asset_id: str) -> int:
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    removed = 0
    changed = False
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        pending = v.get("overlay_suggestions") or []
        kept = [s for s in pending if str(s.get("asset_id")) != asset_id]
        removed += len(pending) - len(kept)
        if len(kept) != len(pending):
            changed = True
            v["overlay_suggestions"] = kept or None
            if not kept and v.get("overlay_suggest_status") == "ready":
                v["overlay_suggest_status"] = "zero"
        # Any in-flight matcher read the pre-delete ready-asset set. Fence its
        # later persist even when it has not produced suggestion rows yet.
        if v.pop("overlay_suggest_attempt_token", None) is not None:
            changed = True
            if v.get("overlay_suggest_status") == "matching":
                v["overlay_suggest_status"] = None
    if changed:
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        flag_modified(job, "assembly_plan")
    return removed


def _clear_pending_overlay_suggestions(job: Job) -> int:
    """Clear only pending AI suggestions for a job.

    Applied/manual placements live in media_overlays / sound_effects and must
    survive creator-context edits unchanged.
    """
    removed = 0
    changed = False
    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        pending = v.get("overlay_suggestions") or []
        if (
            not pending
            and not v.get("overlay_suggest_status")
            and not v.get("overlay_suggest_hash")
            and not v.get("overlay_suggest_attempt_token")
        ):
            continue
        removed += len(pending) if isinstance(pending, list) else 0
        changed = True
        v["overlay_suggestions"] = None
        v["overlay_suggest_status"] = None
        v["overlay_suggest_hash"] = None
        v["overlay_suggest_wishlist"] = None
        v.pop("overlay_suggest_attempt_token", None)
    if changed:
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        flag_modified(job, "assembly_plan")
    return removed


class UpdateAssetContextBody(BaseModel):
    user_context: str | None = Field(default=None)

    @field_validator("user_context")
    @classmethod
    def _trim_context(cls, value: str | None) -> str | None:
        cleaned = (value or "").strip()
        if not cleaned:
            return None
        if len(cleaned) > 500:
            raise ValueError("user_context must be at most 500 characters")
        return cleaned


@router.patch("/{item_id}/assets/{asset_id}/context", response_model=PoolAssetOut)
async def update_pool_asset_context(
    item_id: str,
    asset_id: str,
    body: UpdateAssetContextBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PoolAssetOut:
    """Set/clear creator-authored context for one pool visual.

    This never re-analyzes assets and never moves applied/manual visuals. It only
    clears pending AI suggestions so the next Re-match uses the new context.
    """
    _require_asset_pool()
    item = await _load_owned_item(item_id, user.id, db, for_update=True)
    try:
        aid = uuid.UUID(asset_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    asset = (
        await db.execute(
            select(PlanItemAsset).where(
                PlanItemAsset.id == aid,
                PlanItemAsset.plan_item_id == item.id,
                PlanItemAsset.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    prior_context = asset.user_context
    asset.user_context = body.user_context
    from app.services.edit_proposals import mark_edit_proposal_stale  # noqa: PLC0415

    if prior_context != asset.user_context:
        mark_edit_proposal_stale(item)
    locked_job = (
        await db.get(
            Job,
            item.current_job_id,
            populate_existing=True,
            with_for_update=True,
        )
        if item.current_job_id is not None
        else None
    )
    if locked_job is not None and locked_job.status != "cancelled":
        _clear_pending_overlay_suggestions(locked_job)
    await db.commit()
    await db.refresh(asset)
    return _asset_out(asset)


# ── Overlay auto-placement suggestion routes (plans/005 PR1b) ─────────────────


_OVERLAY_SUGGEST_ATTEMPT_KEY = "overlay_suggest_attempt_token"


def _find_variant_dict(job: Job, variant_id: str) -> dict:
    for v in (job.assembly_plan or {}).get("variants") or []:
        if v.get("variant_id") == variant_id:
            return v
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")


async def _rollback_overlay_suggest_enqueue_attempt(
    *,
    item_id: str,
    variant_id: str,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    attempt_token: str,
    db: AsyncSession,
) -> bool:
    """Clear ``matching`` only while this failed publish still owns it.

    Broker publication is deliberately outside the database transaction. On
    failure, re-enter through the canonical Plan -> Persona -> PlanItem -> Job
    lock order and compare both the current Job and per-variant attempt token.
    Cancellation, a replacement Job, and a newer re-match therefore win.
    """

    try:
        locked_job = await _locked_owned_item_render_job(item_id, user_id, db)
    except HTTPException:
        await db.rollback()
        return False
    if locked_job.id != job_id:
        await db.rollback()
        return False

    target = next(
        (
            candidate
            for candidate in (locked_job.assembly_plan or {}).get("variants") or []
            if candidate.get("variant_id") == variant_id
        ),
        None,
    )
    if target is None or target.get(_OVERLAY_SUGGEST_ATTEMPT_KEY) != attempt_token:
        await db.rollback()
        return False

    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    target["overlay_suggest_status"] = None
    target.pop(_OVERLAY_SUGGEST_ATTEMPT_KEY, None)
    flag_modified(locked_job, "assembly_plan")
    await db.commit()
    return True


class SuggestOverlaysResponse(BaseModel):
    status: str  # "matching"


@router.post(
    "/{item_id}/variants/{variant_id}/suggest-overlays",
    response_model=SuggestOverlaysResponse,
)
async def suggest_overlays(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SuggestOverlaysResponse:
    """Kick the matcher: draft overlay+SFX placements for this variant.

    Persists overlay_suggest_status="matching" BEFORE enqueuing (persist-first
    pattern) so the frontend Pulse state reflects immediately. The matcher task
    replaces all PENDING suggestions (staged/accepted/manual never touched).
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.config import settings as _settings  # noqa: PLC0415

    _require_autoplace()
    item = await _load_owned_item(item_id, user.id, db)
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    variant = _find_variant_dict(job, variant_id)

    # Music-variant guard (review C20): mirror the zero-click auto path's G2-A
    # rule — Whisper on a song track yields garbage anchors, and lyric variants
    # must never get a fullscreen takeover over the lyrics. The auto path skips
    # these variants entirely; the manual route must too.
    if variant.get("music_track_id") is not None or variant.get("text_mode") == "lyrics":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Auto-placement isn't available on song or lyric variants.",
        )

    # Caption-archetype guard (OV-5, plan 010): manual SFX/overlay lanes are open
    # on narrated/subtitled, but AI suggestions stay off pending a speech-content
    # quality eval. Lockstep with _editor_capabilities' suggestions_reason.
    if variant.get("resolved_archetype") in CAPTION_EDIT_ARCHETYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Auto-placement isn't available on this edit format.",
        )

    ready_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == item.id,
                    PlanItemAsset.status == "ready",
                )
            )
        ).scalar_one()
    )
    if ready_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add at least one visual first — the pool has no analyzed assets.",
        )

    variants = list((job.assembly_plan or {}).get("variants") or [])
    attempt_token = uuid.uuid4().hex
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["overlay_suggest_status"] = "matching"
            v[_OVERLAY_SUGGEST_ATTEMPT_KEY] = attempt_token
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")
    await db.commit()

    from app.tasks.autoplace import match_overlay_suggestions  # noqa: PLC0415

    # Guard the enqueue (review C32): "matching" is already committed, so a
    # broker failure here would strand the UI polling a task that never queued.
    # Revert the status and surface 503 so the rail shows a real error + Retry.
    try:
        match_overlay_suggestions.apply_async(
            args=[str(job.id), variant_id, str(user.id)],
            kwargs={"attempt_token": attempt_token},
            queue=_settings.autoplace_queue,
        )
    except Exception as exc:  # noqa: BLE001
        restored = False
        try:
            restored = await _rollback_overlay_suggest_enqueue_attempt(
                item_id=item_id,
                variant_id=variant_id,
                user_id=user.id,
                job_id=job.id,
                attempt_token=attempt_token,
                db=db,
            )
        except Exception as recovery_exc:  # noqa: BLE001
            await db.rollback()
            log.error(
                "plan_item_suggest_enqueue_recovery_failed",
                item_id=item_id,
                variant_id=variant_id,
                attempt_token=attempt_token,
                publish_error=str(exc)[:200],
                recovery_error=str(recovery_exc)[:200],
            )
        log.warning(
            "plan_item_suggest_enqueue_failed",
            item_id=item_id,
            variant_id=variant_id,
            attempt_token=attempt_token,
            restored=restored,
            error=str(exc)[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Couldn't start matching right now. Please try again.",
        ) from exc
    log.info("plan_item_suggest_overlays", item_id=item_id, variant_id=variant_id)
    return SuggestOverlaysResponse(status="matching")


class OverlaySuggestionsResponse(BaseModel):
    status: str | None  # matching | ready | zero | failed | None (never run)
    suggestions: list[dict]
    wishlist: list[str]
    stale_cleared: bool = False


@router.get(
    "/{item_id}/variants/{variant_id}/overlay-suggestions",
    response_model=OverlaySuggestionsResponse,
)
async def get_overlay_suggestions(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OverlaySuggestionsResponse:
    """Read the suggestion set — with the READ-TIME staleness check (tension 3):
    if the persisted transcript no longer matches the hash the set was matched
    against, pending suggestions are cleared here and the caller shows the
    'Your script changed' notice."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.services.transcript_source import persisted_hash_is_stale  # noqa: PLC0415

    _require_autoplace()
    await _load_owned_item(item_id, user.id, db)
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    variant = _find_variant_dict(job, variant_id)

    stale_cleared = False
    if (variant.get("overlay_suggestions") or None) and persisted_hash_is_stale(variant):
        variants = list((job.assembly_plan or {}).get("variants") or [])
        for v in variants:
            if v.get("variant_id") == variant_id:
                v["overlay_suggestions"] = None
                v["overlay_suggest_status"] = None
                v["overlay_suggest_hash"] = None
                v.pop(_OVERLAY_SUGGEST_ATTEMPT_KEY, None)
                break
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        flag_modified(job, "assembly_plan")
        await db.commit()
        variant = _find_variant_dict(job, variant_id)
        stale_cleared = True

    return OverlaySuggestionsResponse(
        status=variant.get("overlay_suggest_status"),
        suggestions=list(variant.get("overlay_suggestions") or []),
        wishlist=list(variant.get("overlay_suggest_wishlist") or []),
        stale_cleared=stale_cleared,
    )


class ApplySuggestionsBody(BaseModel):
    """The STAGED suggestion envelopes (possibly edited by drag/trim). Accept =
    unwrap + copy through the existing validated dispatch (decision 5A)."""

    suggestions: list[dict]


@router.post(
    "/{item_id}/variants/{variant_id}/overlay-suggestions/apply",
    response_model=PlanItemResponse,
)
async def apply_overlay_suggestions(
    item_id: str,
    variant_id: str,
    body: ApplySuggestionsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """'Apply N to video' — ONE render chain (decisions 4A + 10A).

    Delegates to the SHARED `apply_suggestions_to_variant` helper (plan 007,
    G1-A) — the same unit the zero-click auto-apply task uses, so route and
    automation can never drift. The helper mutates; this route commits.
    """
    from app.services.overlay_apply import apply_suggestions_to_variant  # noqa: PLC0415

    _require_autoplace()
    await _load_owned_item(item_id, user.id, db)
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    _find_variant_dict(job, variant_id)

    await _require_ready_pool_paths(
        item_id=item_id,
        user_id=user.id,
        payload=body.suggestions,
        db=db,
    )

    result = apply_suggestions_to_variant(job, variant_id, body.suggestions, user_id=str(user.id))
    if not result["dispatched"]:
        # No-fault copy (007 finding 13): concurrent updates, not user error.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="These placements were just updated — refresh to see the latest.",
        )
    await db.commit()
    log.info(
        "plan_item_apply_overlay_suggestions",
        item_id=item_id,
        variant_id=variant_id,
        applied=result["applied"],
        dropped=result["dropped"],
        sfx=result["sfx"],
    )
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post(
    "/{item_id}/variants/{variant_id}/overlay-suggestions/dismiss",
)
async def dismiss_overlay_suggestions(
    item_id: str,
    variant_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Clear all PENDING suggestions (staged/accepted/manual cards untouched)."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    _require_autoplace()
    await _load_owned_item(item_id, user.id, db)
    job = await _locked_owned_item_render_job(item_id, user.id, db)
    _find_variant_dict(job, variant_id)

    variants = list((job.assembly_plan or {}).get("variants") or [])
    for v in variants:
        if v.get("variant_id") == variant_id:
            v["overlay_suggestions"] = None
            v["overlay_suggest_status"] = None
            v["overlay_suggest_hash"] = None
            v["overlay_suggest_wishlist"] = None
            v.pop(_OVERLAY_SUGGEST_ATTEMPT_KEY, None)
            break
    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")
    await db.commit()
    return {"ok": True}
