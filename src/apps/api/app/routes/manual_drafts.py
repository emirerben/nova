"""Hidden manual-editor draft lifecycle.

Manual drafts deliberately reuse the content-plan ownership boundary, PlanItem,
Job, generative variant JSON, and EditorShell.  The creation hub keeps the entry
point feature-flagged until the browser/media acceptance matrix is green.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, Job, PlanItem
from app.services.content_plan_persona import (
    PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
    PlanPersonaOwnershipError,
    load_owned_plan_persona,
)

router = APIRouter()

_MANUAL_MODE = "manual_draft"
_MANUAL_VARIANT_ID = "original_text"
_IMAGE_EXTENSIONS = {".avif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"}
_PHOTO_DRAFT_DETAIL = (
    "Photo timelines are not available in the manual editor yet. "
    "Use Make a video with Kria for photos, or choose videos only."
)


class ManualDraftCreateBody(BaseModel):
    title: str | None = Field(default=None, max_length=500)


class ManualDraftResponse(BaseModel):
    plan_item_id: str
    job_id: str
    variant_id: str | None = None
    status: Literal["draft"] = "draft"


class ManualDraftMedia(BaseModel):
    gcs_path: str
    duration_s: float = Field(ge=0.1, le=60.0)
    kind: Literal["video", "image"]


class ManualDraftInitializeBody(BaseModel):
    media: list[ManualDraftMedia] = Field(default_factory=list, max_length=20)


def _clean_title(value: str | None) -> str:
    cleaned = _sanitize_text((value or "").strip())
    return (cleaned or "Untitled video")[:500]


def _is_image_path(path: str) -> bool:
    return Path(path.split("?", 1)[0]).suffix.lower() in _IMAGE_EXTENSIONS


def _response(item: PlanItem, job: Job) -> ManualDraftResponse:
    variants = list((job.assembly_plan or {}).get("variants") or [])
    variant_id = next(
        (
            str(variant.get("variant_id"))
            for variant in variants
            if isinstance(variant, dict) and variant.get("variant_id")
        ),
        None,
    )
    return ManualDraftResponse(
        plan_item_id=str(item.id),
        job_id=str(job.id),
        variant_id=variant_id,
    )


@router.post("/manual-drafts", response_model=ManualDraftResponse, status_code=201)
async def create_manual_draft(
    body: ManualDraftCreateBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ManualDraftResponse:
    """Create, or resume, the caller's latest unexported manual draft."""

    plan = (
        await db.execute(
            select(ContentPlan)
            .where(ContentPlan.user_id == user.id)
            .order_by(ContentPlan.created_at.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No content plan found")
    try:
        await load_owned_plan_persona(db, plan, for_update=True)
    except PlanPersonaOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        ) from exc

    latest_item = (
        await db.execute(
            select(PlanItem)
            .join(Job, Job.id == PlanItem.current_job_id)
            .where(
                PlanItem.content_plan_id == plan.id,
                Job.user_id == user.id,
                Job.mode == _MANUAL_MODE,
            )
            .order_by(PlanItem.position.desc(), PlanItem.id.desc())
            .limit(1)
            .with_for_update(of=PlanItem)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if latest_item is not None:
        job = (
            await db.execute(
                select(Job)
                .where(Job.id == latest_item.current_job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if job is None or job.user_id != user.id or job.mode != _MANUAL_MODE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            )
        if job.content_plan_item_id != latest_item.id or int(
            job.content_plan_ownership_epoch or 0
        ) != int(getattr(plan, "ownership_epoch", 0) or 0):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            )
        return _response(latest_item, job)

    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    title = _clean_title(body.title)
    max_position = (
        await db.execute(
            select(func.coalesce(func.max(PlanItem.position), 0)).where(
                PlanItem.content_plan_id == plan.id
            )
        )
    ).scalar_one()
    next_position = int(max_position or 0) + 1
    item = PlanItem(
        id=item_id,
        content_plan_id=plan.id,
        day_index=None,
        position=next_position,
        theme=title,
        idea=title,
        item_status="awaiting_clips",
        content_mode="existing_footage",
        edit_format="montage",
        montage_preset="classic",
        clip_gcs_paths=[],
        clip_assignments=[],
        current_job_id=None,
        user_edited=True,
    )
    db.add(item)
    await db.flush()
    job = Job(
        id=job_id,
        user_id=user.id,
        status="draft",
        job_type="default",
        mode=_MANUAL_MODE,
        raw_storage_path="",
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=int(getattr(plan, "ownership_epoch", 0) or 0),
        all_candidates={
            "clip_paths": [],
            "edit_format": "montage",
            "montage_preset": "classic",
            "landscape_fit": "fit",
            "manual_draft": True,
        },
        assembly_plan={"manual_draft": True, "variants": []},
    )
    db.add(job)
    await db.flush()
    # Insert the two sides in FK order, then close the circular link.  This is
    # safe on databases that do not defer the PlanItem.current_job_id FK.
    item.current_job_id = job.id
    await db.commit()
    return _response(item, job)


@router.post(
    "/{item_id}/manual-draft/initialize",
    response_model=ManualDraftResponse,
)
async def initialize_manual_draft(
    item_id: str,
    body: ManualDraftInitializeBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ManualDraftResponse:
    """Seed the one manual variant from attached media in authored order."""

    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    visible_item = (
        await db.execute(
            select(PlanItem)
            .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
            .where(PlanItem.id == iid, ContentPlan.user_id == user.id)
        )
    ).scalar_one_or_none()
    if visible_item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    plan = (
        await db.execute(
            select(ContentPlan)
            .where(ContentPlan.id == visible_item.content_plan_id, ContentPlan.user_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    try:
        await load_owned_plan_persona(db, plan, for_update=True)
    except PlanPersonaOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        ) from exc
    item = (
        await db.execute(
            select(PlanItem)
            .where(PlanItem.id == iid, PlanItem.content_plan_id == plan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if item.current_job_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Manual draft job missing")
    job = (
        await db.execute(
            select(Job)
            .where(Job.id == item.current_job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None or job.user_id != user.id or job.mode != _MANUAL_MODE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manual draft not found")
    if job.content_plan_item_id != item.id or int(job.content_plan_ownership_epoch or 0) != int(
        getattr(plan, "ownership_epoch", 0) or 0
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        )
    paths = [str(path) for path in (item.clip_gcs_paths or []) if path]
    if not paths:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Add footage before opening the editor.",
        )
    media_by_path = {entry.gcs_path: entry for entry in body.media}
    if body.media and [entry.gcs_path for entry in body.media] != paths:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Media metadata must match the attached footage order.",
        )
    existing_variants = list((job.assembly_plan or {}).get("variants") or [])
    if existing_variants:
        existing_slots = (
            (existing_variants[0].get("ai_timeline") or {}).get("slots") or []
            if isinstance(existing_variants[0], dict)
            else []
        )
        existing_paths = [
            str(slot.get("source_gcs_path"))
            for slot in existing_slots
            if isinstance(slot, dict) and slot.get("source_gcs_path")
        ]
        if existing_paths != paths:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Draft footage changed. Start a new manual draft to rebuild the timeline.",
            )
        return _response(item, job)

    slots: list[dict] = []
    total_duration_s = 0.0
    for index, path in enumerate(paths):
        supplied = media_by_path.get(path)
        path_is_image = _is_image_path(path)
        if supplied is not None and (supplied.kind == "image") != path_is_image:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Media kind must match the attached file.",
            )
        is_image = supplied.kind == "image" if supplied else path_is_image
        source_duration_s = float(supplied.duration_s) if supplied else (3.0 if is_image else 5.0)
        duration_s = min(source_duration_s, 3.0 if is_image else 5.0)
        total_duration_s += duration_s
        slots.append(
            {
                "slot_id": uuid.uuid4().hex,
                "clip_index": index,
                "source_gcs_path": path,
                "source_duration_s": round(source_duration_s, 3),
                "in_s": 0.0,
                "duration_s": round(duration_s, 3),
                "duration_beats": None,
                "order": index,
                "moment_energy": None,
                "moment_description": "Manual draft",
                "removed": False,
                "transition_after": "cut",
                "transition_duration_s": None,
            }
        )
    if any(_is_image_path(path) for path in paths):
        # The canonical virtual preview currently uses HTMLVideoElement decks.
        # Fail before seeding a draft that cannot preview or safely first-export;
        # the creation hub keeps this workflow hidden until image acceptance lands.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_PHOTO_DRAFT_DETAIL,
        )
    if total_duration_s > 60.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Manual drafts must be 60 seconds or shorter.",
        )

    variant = {
        "variant_id": _MANUAL_VARIANT_ID,
        "rank": 1,
        "text_mode": "none",
        "render_status": "draft",
        "ok": False,
        "manual_draft": True,
        "output_url": None,
        "video_path": None,
        "base_video_url": None,
        "base_video_path": None,
        "music_track_id": None,
        "track_title": None,
        "style_set_id": None,
        "intro_text": None,
        "intro_highlight_word": None,
        "intro_text_size_px": None,
        "intro_size_source": None,
        "resolved_archetype": "montage",
        "orientation": "portrait",
        "duration_s": round(total_duration_s, 3),
        "ai_timeline": {"beat_grid": [], "slots": slots},
    }
    job.raw_storage_path = paths[0]
    job.all_candidates = {
        **(job.all_candidates or {}),
        "clip_paths": paths,
        "manual_draft": True,
    }
    job.assembly_plan = {"manual_draft": True, "variants": [variant]}
    await db.commit()
    return _response(item, job)
