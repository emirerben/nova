"""Generative-edit job endpoints.

POST /generative-jobs                                  — create a generative-mode job
GET  /generative-jobs/style-sets                       — curated text style sets (gen-eligible)
GET  /generative-jobs/{id}/status                      — poll status + variants
POST /generative-jobs/{id}/variants/{vid}/swap-song    — async re-slot against a new song
POST /generative-jobs/{id}/variants/{vid}/retext       — async re-render with new/removed text
POST /generative-jobs/{id}/variants/{vid}/change-style — async re-render with a new style set

A generative job needs no pre-selected song or template — the orchestrator auto-matches
a track, writes its own intro text, and renders three variants. Per-variant state lives
in `Job.assembly_plan["variants"]`, which the status endpoint surfaces directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUserOrSynthetic
from app.database import get_db
from app.models import Job, MusicTrack
from app.routes.admin_music import _validate_clip_path_prefixes

log = structlog.get_logger()
router = APIRouter()

_MAX_CLIPS = 20


# ── Schemas ────────────────────────────────────────────────────────────────────


class CreateGenerativeJobRequest(BaseModel):
    # No `target_duration_s`: output length is DERIVED, never user-set. The edit
    # is sized to the uploaded footage (and the matched song's beat structure) so
    # it can never be longer than the content the user provided. A stale frontend
    # that still posts `target_duration_s` is harmless — Pydantic drops the extra
    # field (default `extra="ignore"`).
    clip_gcs_paths: list[str]
    selected_platforms: list[str] = ["tiktok", "instagram", "youtube"]
    # Closed allowlist: adding a new language requires (a) TR-style prompt branches
    # in intro_writer + overlay_format_matcher, (b) a render-side glyph-presence
    # assertion for any new diacritic ranges. Pydantic rejects unknowns at the edge.
    language: Literal["en", "tr"] = "en"

    @field_validator("clip_gcs_paths")
    @classmethod
    def validate_clips(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("At least 1 clip is required")
        if len(v) > _MAX_CLIPS:
            raise ValueError(f"Maximum {_MAX_CLIPS} clips allowed")
        # Reject arbitrary bucket keys — only upload-endpoint prefixes are allowed.
        return _validate_clip_path_prefixes(v)


class GenerativeJobResponse(BaseModel):
    job_id: str
    status: str


class GenerativeJobStatusResponse(BaseModel):
    job_id: str
    status: str
    variants: list[dict]
    error_detail: str | None
    created_at: datetime
    updated_at: datetime


class SwapSongRequest(BaseModel):
    new_track_id: str


class RetextRequest(BaseModel):
    # text=None + remove=True removes the overlay; text set replaces it.
    text: str | None = None
    remove: bool = False


class ChangeStyleRequest(BaseModel):
    style_set_id: str


class SetIntroSizeRequest(BaseModel):
    # Absolute font size in px for the AI intro overlay; clamped to the intro
    # envelope server-side. The frontend ±stepper sends current_px ± step.
    text_size_px: int = Field(..., gt=0)


class StyleSetSummary(BaseModel):
    id: str
    label: str
    tags: list[str]
    # Display-only typography of the set's representative (hook) role so the picker
    # can render a real-font preview chip BEFORE a re-render. Never reaches the
    # renderer burn dict (see style_sets.style_set_preview — #296 parity invariant).
    font_family: str | None = None
    css_family: str | None = None
    font_file: str | None = None
    font_weight: int | None = None
    text_color: str | None = None
    highlight_color: str | None = None
    effect: str | None = None


class StyleSetListResponse(BaseModel):
    style_sets: list[StyleSetSummary]


# ── Helpers ────────────────────────────────────────────────────────────────────


# content_plan jobs reuse the generative render + per-variant assembly_plan shape,
# so they are READ-able via the status endpoint (the plan item page polls it). The
# mutate endpoints (swap-song / retext / change-style) stay generative-only — those
# are generative-UX affordances that don't apply to a plan item.
_READABLE_MODES = ("generative", "content_plan")


async def _load_generative_job(
    job_id: str, db: AsyncSession, *, allowed_modes: tuple[str, ...] = ("generative",)
) -> Job:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.mode not in allowed_modes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


def _variants_of(job: Job) -> list[dict]:
    return ((job.assembly_plan or {}).get("variants")) or []


def _find_variant(job: Job, variant_id: str) -> dict | None:
    return next((v for v in _variants_of(job) if v.get("variant_id") == variant_id), None)


# ── Shared variant-edit validation + dispatch ───────────────────────────────────
# These are public (no leading underscore) so the content-plan routes
# (`routes/plan_items.py`) can reuse them verbatim across modules — content_plan
# jobs share the generative per-variant assembly_plan shape, so the validation
# rules and the `regenerate_generative_variant` dispatch are identical. The only
# difference between the two surfaces is how the Job is loaded (public job-id vs
# ownership-checked plan item), so that stays in each route; everything below the
# loaded Job is single-sourced here.


def require_editable_variant(job: Job, variant_id: str) -> dict:
    """Return the variant; 404 if unknown, 409 if it's already re-rendering."""
    variant = _find_variant(job, variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")
    if variant.get("render_status") == "rendering":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Variant is already re-rendering."
        )
    return variant


async def dispatch_swap_song(
    job: Job, variant_id: str, *, new_track_id: str, db: AsyncSession
) -> None:
    """Validate + enqueue a song swap for one variant (async re-slot)."""
    variant = require_editable_variant(job, variant_id)
    # Swapping a song only makes sense on a song variant. The original-audio variant
    # has no track; converting it to a song variant would silently change its identity.
    if variant.get("variant_id") == "original_text" or variant.get("music_track_id") is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This is the original-audio edit — it has no song to swap.",
        )
    # The new track must exist and be ready (published not required — swap is a
    # deliberate user pick from the gallery, mirroring admin test-job semantics).
    track = (
        await db.execute(select(MusicTrack).where(MusicTrack.id == new_track_id))
    ).scalar_one_or_none()
    if track is None or track.analysis_status != "ready" or not track.audio_gcs_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Requested song is not available for rendering.",
        )

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, new_track_id=new_track_id)


def dispatch_retext(job: Job, variant_id: str, *, text: str | None, remove: bool) -> None:
    """Validate + enqueue an intro-text edit/removal for one variant."""
    require_editable_variant(job, variant_id)
    if not remove and not (text and text.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide `text` to update, or set `remove=true` to clear the overlay.",
        )

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(
        str(job.id),
        variant_id,
        override_text=(text.strip() if (text and not remove) else None),
        remove_text=bool(remove),
    )


def dispatch_change_style(job: Job, variant_id: str, *, style_set_id: str) -> None:
    """Validate + enqueue a text-style-set change for one variant."""
    from app.pipeline.style_sets import style_set_ids  # noqa: PLC0415

    require_editable_variant(job, variant_id)
    if style_set_id not in set(style_set_ids(applies_to="generative")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown or non-generative style set.",
        )

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, style_set_id=style_set_id)


def dispatch_set_intro_size(job: Job, variant_id: str, *, text_size_px: int) -> None:
    """Validate + enqueue a user intro font-size override for one variant."""
    from app.pipeline.overlay_sizing import clamp_intro_px  # noqa: PLC0415

    variant = require_editable_variant(job, variant_id)
    # Only the AI-intro text variants carry a resizable hero overlay. The lyrics
    # variant's typography is governed by its style set and a text-removed variant
    # has no overlay, so resizing either is a no-op — reject rather than spin up a
    # render that changes nothing.
    if variant.get("text_mode") != "agent_text":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This edit has no resizable intro text.",
        )
    px = clamp_intro_px(text_size_px)

    from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

    regenerate_generative_variant.delay(str(job.id), variant_id, size_override_px=px)


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("", response_model=GenerativeJobResponse, status_code=status.HTTP_201_CREATED)
async def create_generative_job(
    req: CreateGenerativeJobRequest,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Create a generative edit job (auto song + AI text, three variants)."""
    # Single source of truth for Job shape + clip validation, shared with the
    # content-plan per-item task. Prefixes were already validated by the request
    # schema; build_generative_job re-validates (cheap defense-in-depth).
    from app.services.generative_jobs import build_generative_job  # noqa: PLC0415

    job = build_generative_job(
        user_id=current_user.id,
        clip_paths=req.clip_gcs_paths,
        language=req.language,
        selected_platforms=req.selected_platforms,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.services.job_dispatch import enqueue_orchestrator  # noqa: PLC0415
    from app.tasks.generative_build import orchestrate_generative_job  # noqa: PLC0415

    await enqueue_orchestrator(orchestrate_generative_job, job.id, db)

    log.info(
        "generative_job_created",
        job_id=str(job.id),
        clips=len(req.clip_gcs_paths),
        language=req.language,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="queued")


@router.get("/style-sets", response_model=StyleSetListResponse)
async def list_generative_style_sets() -> StyleSetListResponse:
    """The curated text style sets a user/admin can pick from for a generative edit.

    Generative-eligible only (no music-only lyric sets). Mirrors `GET /music-tracks`
    — the gallery the swap-song picker reads. Declared BEFORE `/{job_id}/status` so
    the literal path isn't captured as a job id.
    """
    from app.pipeline.style_sets import list_style_sets, style_set_preview  # noqa: PLC0415

    return StyleSetListResponse(
        style_sets=[
            StyleSetSummary(**s, **style_set_preview(s["id"]))
            for s in list_style_sets(applies_to="generative")
        ]
    )


@router.get("/{job_id}/status", response_model=GenerativeJobStatusResponse)
async def get_generative_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobStatusResponse:
    """Poll generative job status. `variants` carries the per-variant render state.

    Also serves content_plan jobs (the plan item page polls this for variants).
    """
    job = await _load_generative_job(job_id, db, allowed_modes=_READABLE_MODES)
    return GenerativeJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        variants=_variants_of(job),
        error_detail=job.error_detail,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.post("/{job_id}/variants/{variant_id}/swap-song", response_model=GenerativeJobResponse)
async def swap_song(
    job_id: str,
    variant_id: str,
    req: SwapSongRequest,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant against a different library song (async re-slot)."""
    job = await _load_generative_job(job_id, db)
    await dispatch_swap_song(job, variant_id, new_track_id=req.new_track_id, db=db)
    log.info(
        "generative_swap_song", job_id=str(job.id), variant_id=variant_id, track_id=req.new_track_id
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post("/{job_id}/variants/{variant_id}/retext", response_model=GenerativeJobResponse)
async def retext(
    job_id: str,
    variant_id: str,
    req: RetextRequest,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant with user-supplied intro text, or remove the text."""
    job = await _load_generative_job(job_id, db)
    dispatch_retext(job, variant_id, text=req.text, remove=req.remove)
    log.info("generative_retext", job_id=str(job.id), variant_id=variant_id, remove=req.remove)
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post("/{job_id}/variants/{variant_id}/change-style", response_model=GenerativeJobResponse)
async def change_style(
    job_id: str,
    variant_id: str,
    req: ChangeStyleRequest,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant with a different curated text style set (async).

    Unlike swap-song this applies to ALL variants — the style set governs the AI
    intro on the text variants and the lyric typography on the lyrics variant.
    """
    job = await _load_generative_job(job_id, db)
    dispatch_change_style(job, variant_id, style_set_id=req.style_set_id)
    log.info(
        "generative_change_style",
        job_id=str(job.id),
        variant_id=variant_id,
        style_set_id=req.style_set_id,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")


@router.post("/{job_id}/variants/{variant_id}/intro-size", response_model=GenerativeJobResponse)
async def set_intro_size(
    job_id: str,
    variant_id: str,
    req: SetIntroSizeRequest,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant with a user-pinned AI-intro font size (the ±size nudge)."""
    job = await _load_generative_job(job_id, db)
    dispatch_set_intro_size(job, variant_id, text_size_px=req.text_size_px)
    log.info(
        "generative_set_intro_size",
        job_id=str(job.id),
        variant_id=variant_id,
        px=req.text_size_px,
    )
    return GenerativeJobResponse(job_id=str(job.id), status="rendering")
