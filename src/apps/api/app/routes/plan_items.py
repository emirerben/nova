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
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, PlanItem

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


class PlanItemResponse(BaseModel):
    id: str
    day_index: int
    theme: str
    idea: str
    filming_suggestion: str | None
    clip_gcs_paths: list[str]
    status: str
    current_job_id: str | None
    user_edited: bool


def plan_item_response(item: PlanItem) -> PlanItemResponse:
    return PlanItemResponse(
        id=str(item.id),
        day_index=item.day_index,
        theme=item.theme,
        idea=item.idea,
        filming_suggestion=item.filming_suggestion,
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
    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    item = await db.get(PlanItem, iid)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    # Ownership: the item's plan must belong to the caller.
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

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
    await db.refresh(item)
    return plan_item_response(item)


# ── Themed uploads + per-item generation ──────────────────────────────────────


async def _load_owned_item(item_id: str, user_id: uuid.UUID, db: AsyncSession) -> PlanItem:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    item = await db.get(PlanItem, iid)
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
    await db.refresh(item)
    return plan_item_response(item)


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
    await db.refresh(item)
    return plan_item_response(item)
