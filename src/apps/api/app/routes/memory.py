# ruff: noqa: E501
"""Owner-scoped creator memory and project direction contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import (
    CreationThread,
    CreatorMemoryItem,
    CreatorMemoryOperation,
    Persona,
)
from app.services.creator_direction import (
    CreatorDirectionResolver,
    CreatorDirectionService,
    DirectionConflict,
    DirectionError,
    IdempotencyMismatch,
    LimitReached,
    MemoryItemNotFound,
    ProjectDirectionNotFound,
    StaleRevision,
    UndoExpired,
    UndoNotApplicable,
)
from app.services.creator_direction_capabilities import capability_status
from app.services.creator_direction_receipts import project_direction_receipt, stamp_private_receipt
from app.services.creator_direction_snapshot import serialize_private_snapshot

router = APIRouter()
direction_router = APIRouter()
service = CreatorDirectionService()
resolver = CreatorDirectionResolver()

_COMPATIBILITY_NORMALIZED_KEYS = {
    key: f"creator_profile_{key}"
    for key in ("summary", "goal", "audience", "pillars", "cadence", "tone")
}


class MemoryItemBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=80)
    enforcement: Literal["constraint", "default", "advisory"]
    compatibility_key: (
        Literal["summary", "goal", "audience", "pillars", "cadence", "tone"] | None
    ) = None
    normalized_key: str | None = Field(default=None, max_length=120)
    structured_value: dict[str, Any] | None = None
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MemoryToggleBody(BaseModel):
    enabled: bool
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MemoryActionBody(BaseModel):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


class OverrideBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=500)
    normalized_key: str = Field(min_length=1, max_length=120)
    structured_value: dict[str, Any] | None = None
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


def _error(exc: DirectionError) -> HTTPException:
    detail = {
        "code": exc.code,
        "message": str(exc),
        "retryable": isinstance(exc, StaleRevision),
        "recoverable": isinstance(
            exc,
            (DirectionConflict, IdempotencyMismatch, StaleRevision, UndoNotApplicable),
        ),
        "recovery_action": (
            "Reload personalization and review the current value."
            if isinstance(exc, (DirectionConflict, StaleRevision, UndoNotApplicable))
            else "Correct the instruction and try again."
        ),
    }
    if isinstance(exc, (MemoryItemNotFound, ProjectDirectionNotFound)):
        return HTTPException(status_code=404, detail=detail)
    if isinstance(exc, UndoExpired):
        return HTTPException(status_code=410, detail=detail)
    if isinstance(exc, LimitReached):
        return HTTPException(status_code=429, detail=detail)
    if isinstance(exc, StaleRevision):
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, (DirectionConflict, IdempotencyMismatch, UndoNotApplicable)):
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=422, detail=detail)


def _mutation_gate() -> None:
    if not settings.creator_memory_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "creator_memory_disabled",
                "message": "Creator memory is temporarily unavailable.",
                "retryable": True,
                "recoverable": True,
                "recovery_action": "Try again after personalization is available.",
            },
        )


def _item(row: CreatorMemoryItem) -> dict[str, Any]:
    source_deleted = row.source_kind == "creation_thread" and row.source_thread_id is None
    return {
        "id": str(row.id),
        "category": row.category,
        "normalized_key": row.normalized_key,
        "instruction": row.instruction,
        "enforcement": row.enforcement,
        "structured_value": row.structured_value,
        "source_kind": row.source_kind,
        "source_thread_id": str(row.source_thread_id) if row.source_thread_id else None,
        "source_deleted": source_deleted,
        "state": row.state,
        "user_locked": row.user_locked,
        "confidence": row.confidence,
        "updated_at": row.updated_at,
        "enforcement_status": capability_status(
            row.normalized_key,
            enforcement=row.enforcement,
        ),
        "scope_label": "Applies to all future videos",
        "source_label": "Learned from a project"
        if row.source_kind == "creation_thread"
        else "Added in Personalization",
    }


def _compatibility_projection(
    persona: Persona | None, *, replaced_keys: set[str] | None = None
) -> dict[str, Any] | None:
    """Expose the legacy creator profile in the unified Personalization document.

    The profile remains compatibility context (memory rules still have their own
    precedence), but returning it here prevents the UI from splitting a creator's
    direction across two settings surfaces.
    """
    value = persona.persona if persona is not None else None
    if not isinstance(value, dict):
        return None
    replaced = replaced_keys or set()
    text_fields = {
        key: value.get(key)
        for key in ("summary", "tone", "audience", "posting_cadence", "goal")
        if ("cadence" if key == "posting_cadence" else key) not in replaced
        and isinstance(value.get(key), str)
        and value.get(key).strip()
    }
    pillars = value.get("content_pillars")
    if "pillars" not in replaced and isinstance(pillars, list):
        text_fields["content_pillars"] = [
            item.strip() for item in pillars if isinstance(item, str) and item.strip()
        ]
    return text_fields or None


def _op(row: CreatorMemoryOperation) -> dict[str, Any]:
    return {
        "operation_id": str(row.id),
        "item_id": str(row.item_id) if row.item_id else None,
        "revision": row.resulting_revision,
        "undo_expires_at": row.undo_expires_at,
    }


async def _refresh_project_direction(
    db: AsyncSession, *, user_id: uuid.UUID, thread_id: uuid.UUID
) -> dict[str, Any]:
    thread = (
        await db.execute(
            select(CreationThread)
            .where(CreationThread.id == thread_id, CreationThread.creator_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if thread is None:
        raise ProjectDirectionNotFound("creation thread not found")
    snapshot = await resolver.snapshot(db, user_id, thread_id=thread_id)
    private_snapshot = stamp_private_receipt(
        serialize_private_snapshot(snapshot, source="project_override"), snapshot
    )
    thread.creator_direction_snapshot = private_snapshot
    await db.flush()
    return project_direction_receipt(private_snapshot)


@router.get("/memory")
async def get_memory(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    snapshot = await resolver.snapshot(db, user.id)
    rows = (
        (
            await db.execute(
                select(CreatorMemoryItem)
                .where(
                    CreatorMemoryItem.user_id == user.id,
                    CreatorMemoryItem.state.in_(("active", "suggested")),
                )
                .order_by(CreatorMemoryItem.updated_at.desc())
                .limit(120)
            )
        )
        .scalars()
        .all()
    )
    active = [_item(row) for row in rows if row.state == "active"]
    replaced_compatibility_keys = {
        key
        for key, normalized_key in _COMPATIBILITY_NORMALIZED_KEYS.items()
        if any(row.state == "active" and row.normalized_key == normalized_key for row in rows)
    }
    persona = (
        await db.execute(select(Persona).where(Persona.user_id == user.id))
    ).scalar_one_or_none()
    # Compatibility projection is deliberately read-only and lower precedence;
    # it is never mislabeled as creator-authored ledger memory.
    if persona and isinstance(persona.style, dict) and persona.style.get("style_set_id"):
        active.append(
            {
                "id": "compatibility-style",
                "category": "video_style",
                "instruction": f"Existing style: {persona.style['style_set_id']}",
                "enforcement": "advisory",
                "enforcement_status": "advisory",
                "source_kind": "compatibility",
                "state": "active",
                "user_locked": False,
                "scope_label": "",
            }
        )
    sections = {key: [] for key in ("content", "video_style", "stories_pacing", "avoid", "other")}
    for row in active:
        category = row["category"] if row["category"] in sections else "other"
        sections[category].append(row)
    active_keys = {
        row.normalized_key for row in rows if row.state == "active" and row.normalized_key
    }
    suggestions = []
    for row in rows:
        if row.state != "suggested":
            continue
        projected = _item(row)
        if row.normalized_key and row.normalized_key in active_keys:
            projected["enforcement_status"] = "conflicted"
            projected["conflict"] = {
                "message": "This conflicts with a preference already in use.",
                "choices": ["Keep current", "Use this instead"],
            }
        if len(suggestions) < 20:
            suggestions.append(projected)
    recent_undo = (
        await db.execute(
            select(CreatorMemoryOperation)
            .where(
                CreatorMemoryOperation.user_id == user.id,
                CreatorMemoryOperation.actor_kind == "system",
                CreatorMemoryOperation.operation_kind == "create_item",
                CreatorMemoryOperation.resulting_revision == snapshot.revision,
                CreatorMemoryOperation.undone_at.is_(None),
                CreatorMemoryOperation.undo_expires_at > datetime.now(UTC),
            )
            .order_by(CreatorMemoryOperation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "feature_available": bool(settings.creator_memory_enabled),
        "enabled": snapshot.enabled if settings.creator_memory_enabled else False,
        "revision": snapshot.revision,
        "personalization_sections": sections,
        "active": active,
        "suggestions": suggestions,
        "recent_undo": _op(recent_undo) if recent_undo else None,
        "compatibility": _compatibility_projection(
            persona, replaced_keys=replaced_compatibility_keys
        ),
    }


@router.patch("/memory")
async def toggle_memory(
    body: MemoryToggleBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    _mutation_gate()
    try:
        op = await service.set_enabled(
            db, user.id, body.enabled, body.expected_revision, body.idempotency_key
        )
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.post("/memory/items", status_code=status.HTTP_201_CREATED)
async def create_memory(
    body: MemoryItemBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    _mutation_gate()
    try:
        body_values = body.model_dump(exclude={"compatibility_key"})
        if body.compatibility_key is not None:
            body_values["normalized_key"] = _COMPATIBILITY_NORMALIZED_KEYS[body.compatibility_key]
        op = await service.create_item(db, user.id, **body_values)
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.post("/memory/clear")
async def clear_memory(
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _mutation_gate()
    try:
        op = await service.clear_preferences(
            db,
            user.id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.patch("/memory/items/{item_id}")
async def update_memory(
    item_id: uuid.UUID, body: MemoryItemBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    _mutation_gate()
    try:
        body_values = body.model_dump(exclude={"compatibility_key"})
        op = await service.update_item(db, user.id, item_id, **body_values)
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.delete("/memory/items/{item_id}")
async def forget_memory(
    item_id: uuid.UUID,
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _mutation_gate()
    try:
        op = await service.forget(
            db, user.id, item_id, body.expected_revision, body.idempotency_key
        )
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.post("/memory/items/{item_id}/restore")
async def restore_memory(
    item_id: uuid.UUID,
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _mutation_gate()
    try:
        op = await service.set_item_state(
            db,
            user.id,
            item_id,
            state="active",
            required_state="forgotten",
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.post("/memory/items/{item_id}/accept")
async def accept_memory(
    item_id: uuid.UUID,
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _mutation_gate()
    try:
        op = await service.set_item_state(
            db,
            user.id,
            item_id,
            state="active",
            required_state="suggested",
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.delete("/memory/items/{item_id}/suggestion")
async def dismiss_memory(
    item_id: uuid.UUID,
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _mutation_gate()
    try:
        op = await service.set_item_state(
            db,
            user.id,
            item_id,
            state="dismissed",
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.post("/memory/operations/{operation_id}/undo")
async def undo_memory(
    operation_id: uuid.UUID,
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _mutation_gate()
    try:
        op = await service.undo(
            db, user.id, operation_id, body.expected_revision, body.idempotency_key
        )
        await db.commit()
        return _op(op)
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.put("/memory/threads/{thread_id}/override")
async def put_override(
    thread_id: uuid.UUID, body: OverrideBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    _mutation_gate()
    try:
        row = await service.set_override(db, user.id, thread_id, **body.model_dump())
        operation = (
            await db.execute(
                select(CreatorMemoryOperation).where(
                    CreatorMemoryOperation.user_id == user.id,
                    CreatorMemoryOperation.idempotency_key == body.idempotency_key,
                )
            )
        ).scalar_one()
        receipt = (operation.prior_state or {}).get("_receipt")
        if not isinstance(receipt, dict):
            receipt = await _refresh_project_direction(db, user_id=user.id, thread_id=thread_id)
            operation.prior_state = {**(operation.prior_state or {}), "_receipt": receipt}
        await db.commit()
        return {
            "id": str(row.id),
            "thread_id": str(row.thread_id),
            "normalized_key": row.normalized_key,
            "instruction": row.instruction,
            "structured_value": row.structured_value,
            "revision": row.revision,
            "direction_receipt": receipt,
        }
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@router.get("/memory/threads/{thread_id}/override")
async def get_overrides(
    thread_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    snapshot = await resolver.snapshot(db, user.id, thread_id=thread_id)
    return list(snapshot.overrides)


@router.delete("/memory/threads/{thread_id}/override/{normalized_key}")
async def delete_override(
    thread_id: uuid.UUID,
    normalized_key: str,
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _mutation_gate()
    try:
        operation = await service.delete_override(
            db,
            user.id,
            thread_id,
            normalized_key,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )
        receipt = (operation.prior_state or {}).get("_receipt")
        if not isinstance(receipt, dict):
            receipt = await _refresh_project_direction(db, user_id=user.id, thread_id=thread_id)
            operation.prior_state = {**(operation.prior_state or {}), "_receipt": receipt}
        await db.commit()
        return {**_op(operation), "direction_receipt": receipt}
    except DirectionError as exc:
        await db.rollback()
        raise _error(exc) from exc


@direction_router.post("/creation-threads/{thread_id}/direction-overrides")
async def create_direction_override(
    thread_id: uuid.UUID, body: OverrideBody, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return await put_override(thread_id, body, user, db)


@direction_router.delete("/creation-threads/{thread_id}/direction-overrides/{normalized_key}")
async def remove_direction_override(
    thread_id: uuid.UUID,
    normalized_key: str,
    body: MemoryActionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await delete_override(thread_id, normalized_key, body, user, db)
