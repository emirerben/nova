"""Admin CRUD for Smart Captions creator-style assignments.

The review of v0.11.0.0 shipped `creator_style_assignments` with no write
surface — rows had to be written with raw SQL. This router is the sanctioned
path: upsert by email, list with emails resolved, delete to restore
default-preset eligibility.

Semantics the resolver relies on (services/smart_captions.py):
- row with ``enabled=true``  → that preset wins (overrides any default)
- row with ``enabled=false`` → explicit opt-out, the default does NOT apply
- no row                     → the configured default preset applies (if any)

Auth: X-Admin-Token header (same gate as the rest of admin.py).
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import CreatorStyleAssignment, User
from app.routes.admin import _require_admin
from app.services.generative_jobs import _SMART_PRESET_TOKEN_RE

log = structlog.get_logger()

router = APIRouter()


class AssignmentUpsertRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    preset_id: str = Field(min_length=1, max_length=64)
    preset_version: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    shadow_preset_id: str | None = Field(default=None, max_length=64)
    shadow_preset_version: str | None = Field(default=None, max_length=64)
    assigned_by: str = Field(default="admin", max_length=80)


class AssignmentResponse(BaseModel):
    user_id: str
    email: str
    preset_id: str
    preset_version: str
    enabled: bool
    shadow_preset_id: str | None
    shadow_preset_version: str | None
    assigned_by: str
    updated_at: datetime


class AssignmentListResponse(BaseModel):
    assignments: list[AssignmentResponse]
    # The fleet-wide fallback applied to users WITHOUT a row (empty = off).
    default_preset_id: str
    default_preset_version: str


def _validate_preset_pair(preset_id: str, preset_version: str) -> None:
    if not (
        _SMART_PRESET_TOKEN_RE.fullmatch(preset_id)
        and _SMART_PRESET_TOKEN_RE.fullmatch(preset_version)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Preset id/version contain unsupported characters.",
        )
    try:
        from app.smart_edit.presets import load_preset  # noqa: PLC0415

        load_preset(preset_id, preset_version)
    except Exception as exc:  # noqa: BLE001 — surface unknown presets as 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown Smart preset: {preset_id}/{preset_version}",
        ) from exc


def _response(assignment: CreatorStyleAssignment, email: str) -> AssignmentResponse:
    return AssignmentResponse(
        user_id=str(assignment.user_id),
        email=email,
        preset_id=assignment.preset_id,
        preset_version=assignment.preset_version,
        enabled=assignment.enabled,
        shadow_preset_id=assignment.shadow_preset_id,
        shadow_preset_version=assignment.shadow_preset_version,
        assigned_by=assignment.assigned_by,
        updated_at=assignment.updated_at,
    )


@router.get("", response_model=AssignmentListResponse)
async def list_assignments(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> AssignmentListResponse:
    rows = (
        (
            await db.execute(
                select(CreatorStyleAssignment, User.email)
                .join(User, User.id == CreatorStyleAssignment.user_id)
                .order_by(CreatorStyleAssignment.updated_at.desc())
            )
        )
        .tuples()
        .all()
    )
    return AssignmentListResponse(
        assignments=[_response(assignment, email) for assignment, email in rows],
        default_preset_id=settings.smart_captions_default_preset_id,
        default_preset_version=settings.smart_captions_default_preset_version,
    )


@router.post("", response_model=AssignmentResponse)
async def upsert_assignment(
    req: AssignmentUpsertRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> AssignmentResponse:
    _validate_preset_pair(req.preset_id, req.preset_version)
    shadow_id = (req.shadow_preset_id or "").strip()
    shadow_version = (req.shadow_preset_version or "").strip()
    if bool(shadow_id) != bool(shadow_version):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="shadow_preset_id and shadow_preset_version must be set together.",
        )
    if shadow_id:
        _validate_preset_pair(shadow_id, shadow_version)

    email = req.email.strip().lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user with email {email!r}.",
        )

    assignment = await db.get(CreatorStyleAssignment, user.id)
    if assignment is None:
        assignment = CreatorStyleAssignment(user_id=user.id)
        db.add(assignment)
    assignment.preset_id = req.preset_id
    assignment.preset_version = req.preset_version
    assignment.enabled = req.enabled
    assignment.shadow_preset_id = shadow_id or None
    assignment.shadow_preset_version = shadow_version or None
    assignment.assigned_by = req.assigned_by
    await db.commit()
    await db.refresh(assignment)
    log.info(
        "creator_style_assignment_upserted",
        user_id=str(user.id),
        preset=f"{req.preset_id}/{req.preset_version}",
        enabled=req.enabled,
        assigned_by=req.assigned_by,
    )
    return _response(assignment, email)


@router.delete("/{email}")
async def delete_assignment(
    email: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> dict[str, bool]:
    normalized = email.strip().lower()
    user = (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user with email {normalized!r}.",
        )
    assignment = await db.get(CreatorStyleAssignment, user.id)
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No assignment for this user.",
        )
    await db.delete(assignment)
    await db.commit()
    log.info("creator_style_assignment_deleted", user_id=str(user.id))
    return {"deleted": True}
