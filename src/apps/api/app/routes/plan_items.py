"""Plan-item endpoints + shared item serialization (content-plan Phase 4).

PATCH /plan-items/{id} — hand-edit a plan item (theme / idea / filming_suggestion).

Also the home of `derive_item_status` + `plan_item_response`, used here and by
content_plans.py. Live render state is DERIVED from the linked Job.status at read
time (plan T2): `item_status` on the row only ever holds `idea` | `awaiting_clips`;
generating / ready / failed come from the Job so a reaper-killed job can never
leave an item stuck "generating" forever.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import storage
from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, Job, PlanItem
from app.routes.generative_jobs import (
    ChangeStyleRequest,
    RetextRequest,
    SetIntroSizeRequest,
    SwapSongRequest,
    dispatch_change_style,
    dispatch_retext,
    dispatch_set_intro_size,
    dispatch_swap_song,
)

log = structlog.get_logger()
router = APIRouter()

# Themed plan uploads land under the persistent `users/` prefix (NOT swept by the
# 24h GCS delete rule). Allowlisted in admin_music._ALLOWED_CLIP_PREFIXES.
_MAX_CLIPS_PER_ITEM = 20
_MAX_BYTES_PER_FILE = 4 * 1024 * 1024 * 1024  # 4GB
_ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime"}

# Job.status buckets (mode="content_plan" reuses the generative variant states).
_JOB_READY = {"variants_ready", "variants_ready_partial", "done", "clips_ready"}
_JOB_FAILED = {
    "variants_failed",
    "matching_failed",
    "no_labeled_tracks",
    "processing_failed",
    "posting_failed",
    "cancelled",
}


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


class FilmingShotResponse(BaseModel):
    """One shot from the plan item's filming guide.

    All fields default to safe values so a hand-corrupted or legacy JSONB row
    with missing keys never 500s the read path.
    """

    what: str = ""
    how: str = ""
    duration_s: int = 1  # matches MIN_SHOT_DURATION_S; 0 would render as confusing "0s" badge


class PlanItemResponse(BaseModel):
    id: str
    day_index: int
    theme: str
    idea: str
    filming_suggestion: str | None
    # The AI's "why this works", surfaced read-only in the dashboard.
    rationale: str | None
    # Structured shot list (2–4 shots). Always a list; empty for legacy items
    # whose plans predate this field (frontend falls back to filming_suggestion).
    filming_guide: list[FilmingShotResponse]
    clip_gcs_paths: list[str]
    status: str
    current_job_id: str | None
    user_edited: bool


def plan_item_response(item: PlanItem) -> PlanItemResponse:
    # Tolerate missing keys in individual JSONB shots — each shot is constructed
    # via .get() so a hand-corrupted row or a migration-era partial row never raises.
    shots = [
        FilmingShotResponse(
            what=s.get("what", ""),
            how=s.get("how", ""),
            duration_s=s.get("duration_s", 1),  # 1 = MIN_SHOT_DURATION_S; 0 renders as "0s" badge
        )
        for s in (item.filming_guide or [])
        if isinstance(s, dict)
    ]
    return PlanItemResponse(
        id=str(item.id),
        day_index=item.day_index,
        theme=item.theme,
        idea=item.idea,
        filming_suggestion=item.filming_suggestion,
        rationale=item.rationale,
        filming_guide=shots,
        clip_gcs_paths=list(item.clip_gcs_paths or []),
        status=derive_item_status(item),
        current_job_id=str(item.current_job_id) if item.current_job_id else None,
        user_edited=item.user_edited,
    )


@router.get("/{item_id}", response_model=PlanItemResponse)
async def get_plan_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    item = await _load_owned_item(item_id, user.id, db)
    return plan_item_response(item)


class PlanItemEdit(BaseModel):
    theme: str | None = None
    idea: str | None = None
    filming_suggestion: str | None = None


@router.patch("/{item_id}", response_model=PlanItemResponse)
async def edit_plan_item(
    item_id: str,
    edit: PlanItemEdit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    item = await _load_owned_item(item_id, user.id, db)

    updates = edit.model_dump(exclude_none=True)
    if "theme" in updates:
        item.theme = _sanitize_text(updates["theme"]) or item.theme
    if "idea" in updates:
        item.idea = _sanitize_text(updates["idea"]) or item.idea
    if "filming_suggestion" in updates:
        item.filming_suggestion = _sanitize_text(updates["filming_suggestion"]) or None
    if updates:
        item.user_edited = True
    await db.commit()
    # Reload with current_job eager-loaded (commit expired it) before serializing.
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


# ── Themed uploads + per-item generation ──────────────────────────────────────


async def _load_owned_item(item_id: str, user_id: uuid.UUID, db: AsyncSession) -> PlanItem:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    # Eager-load current_job: plan_item_response → derive_item_status reads the
    # relationship, and a bare db.get() leaves it lazy → MissingGreenlet 500 on
    # the async session once an item has a linked job (mirrors the list endpoint's
    # selectinload in content_plans.py).
    item = (
        await db.execute(
            select(PlanItem).where(PlanItem.id == iid).options(selectinload(PlanItem.current_job))
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    return item


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
    for f in body.files:
        if f.content_type not in _ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported content type: {f.content_type}",
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


class AttachClipsBody(BaseModel):
    clip_gcs_paths: list[str]


@router.post("/{item_id}/clips", response_model=PlanItemResponse)
async def attach_clips(
    item_id: str,
    body: AttachClipsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Record uploaded clip paths on the item (validated to the users/ prefix)."""
    item = await _load_owned_item(item_id, user.id, db)
    expected = f"users/{user.id}/plan/{item.id}/"
    for p in body.clip_gcs_paths:
        if not p.startswith(expected):
            # Reject any path that isn't this user's own plan-item prefix.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Clip path outside this plan item's upload prefix",
            )
    item.clip_gcs_paths = list(body.clip_gcs_paths)
    await db.commit()
    # Reload with current_job eager-loaded (commit expired it) before serializing.
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


@router.post("/{item_id}/generate", response_model=PlanItemResponse)
async def generate_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Enqueue a generative render for this item's themed clips (≥1 required)."""
    item = await _load_owned_item(item_id, user.id, db)
    if not (item.clip_gcs_paths or []):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload at least one clip before generating",
        )
    from app.tasks.content_plan_build import generate_plan_item_videos  # noqa: PLC0415

    generate_plan_item_videos.delay(str(item.id))
    # current_job_id is set by the task, not synchronously here; reload with the
    # relationship eager-loaded so serialization never lazy-loads on the session.
    return plan_item_response(await _load_owned_item(item_id, user.id, db))


# ── Per-variant editing (swap song / edit text / change style) ────────────────
# The render job behind a plan item is a generative-mode Job, so each variant can
# be re-rendered exactly like a public generative edit. These endpoints add only
# ownership enforcement (`_load_owned_item`) + job resolution on top of the shared
# validate-and-dispatch helpers in `routes/generative_jobs.py` — the validation
# rules and the `regenerate_generative_variant` dispatch stay single-sourced there.
# Mutation is reachable ONLY here (authenticated, per-user), never on the public
# unauthenticated `/generative-jobs` surface.


async def _owned_item_render_job(item_id: str, user_id: uuid.UUID, db: AsyncSession) -> Job:
    """Load the user-owned item and return its current render Job (404 if none yet)."""
    item = await _load_owned_item(item_id, user_id, db)
    job = item.current_job
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No render to edit yet")
    return job


@router.post("/{item_id}/variants/{variant_id}/swap-song", response_model=PlanItemResponse)
async def swap_item_song(
    item_id: str,
    variant_id: str,
    req: SwapSongRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    """Re-render one of this item's variants against a different library song."""
    job = await _owned_item_render_job(item_id, user.id, db)
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
    job = await _owned_item_render_job(item_id, user.id, db)
    dispatch_retext(job, variant_id, text=req.text, remove=req.remove)
    log.info("plan_item_retext", item_id=item_id, variant_id=variant_id, remove=req.remove)
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
    job = await _owned_item_render_job(item_id, user.id, db)
    dispatch_change_style(job, variant_id, style_set_id=req.style_set_id)
    log.info("plan_item_change_style", item_id=item_id, variant_id=variant_id)
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
    job = await _owned_item_render_job(item_id, user.id, db)
    dispatch_set_intro_size(job, variant_id, text_size_px=req.text_size_px)
    log.info(
        "plan_item_set_intro_size", item_id=item_id, variant_id=variant_id, px=req.text_size_px
    )
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
    item = await _load_owned_item(item_id, user.id, db)

    if item.item_status != "idea" or item.current_job_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Can only reroll an un-started idea (item_status='idea', no current_job_id)",
        )

    item.item_status = "rerolling"
    await db.commit()

    from app.tasks.content_plan_build import reroll_plan_item  # noqa: PLC0415

    reroll_plan_item.delay(str(item.id))

    log.info("plan_item_reroll.dispatched", item_id=item_id)
    return plan_item_response(await _load_owned_item(item_id, user.id, db))
