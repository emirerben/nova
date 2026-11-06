"""Strict recovery endpoint. Legacy paid endpoints never receive a recovery key."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import SlidePostAIRequest
from app.routes import plan_items
from app.services.slide_post_ai_requests import (
    AIReceiptConflict,
    SQLAIRequestStore,
    receipt_output,
)

router = APIRouter()


class AIRequestBody(BaseModel):
    model_config = {"extra": "forbid"}
    client_request_id: uuid.UUID
    project_id: uuid.UUID
    kind: Literal["propose", "chat_edit"]
    input: dict
    delivery_version: Literal[1] | None = None


class AIRequestReceipt(BaseModel):
    client_request_id: uuid.UUID
    item_id: uuid.UUID
    project_id: uuid.UUID
    kind: Literal["propose", "chat_edit"]
    input_digest: str
    input: dict
    status: Literal["pending", "running", "completed", "unknown", "rejected"]
    result: dict | None = None
    delivery_version: int = 0


async def owned_receipt(item_id, request_id, user, db):
    item = await plan_items._load_owned_item(item_id, user.id, db)
    thread = await plan_items._slide_post_thread_for_item(item, user.id, db)
    row = await db.get(SlidePostAIRequest, request_id, populate_existing=True)
    if (
        row is None
        or row.owner_id != user.id
        or row.item_id != item.id
        or thread is None
        or row.project_id != thread.id
    ):
        raise HTTPException(404, "AI request not found")
    return row


@router.get("/{item_id}/slide-post/ai-requests/{request_id}", response_model=AIRequestReceipt)
async def get_ai_request(
    item_id: str, request_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    # Existing receipts remain observable even when new admission is disabled.
    return receipt_output(await owned_receipt(item_id, request_id, user, db))


@router.post("/{item_id}/slide-post/ai-requests", response_model=AIRequestReceipt)
@plan_items.limiter.limit("20/minute", key_func=plan_items.get_real_ip)
@plan_items.limiter.limit("30/hour", key_func=plan_items._edit_conversation_rate_key)
async def request_ai(
    request: Request,
    item_id: str,
    body: AIRequestBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    client_protocol: plan_items.KriaClientProtocol = None,
):
    item = await plan_items._load_owned_item(item_id, user.id, db)
    thread = await plan_items._slide_post_thread_for_item(item, user.id, db)
    if thread is None or thread.id != body.project_id:
        raise HTTPException(409, "This post belongs to a different project")
    input_type = (
        plan_items.SlidePostProposeBody
        if body.kind == "propose"
        else plan_items.SlidePostChatEditBody
    )
    # Inner request IDs are not an independent authority; reject conflicting IDs.
    inner_id = body.input.get("client_request_id")
    try:
        if inner_id is not None and uuid.UUID(str(inner_id)) != body.client_request_id:
            raise ValueError("different intent")
    except ValueError as exc:
        raise HTTPException(409, "AI request identity mismatch") from exc
    if type(body.input.get("expected_version", 0)) is not int:
        raise HTTPException(422, "expected_version must be an integer")
    try:
        parsed = input_type.model_validate(
            {**body.input, "client_request_id": str(body.client_request_id)}
        )
    except ValueError as exc:
        raise HTTPException(422, "Invalid AI request input") from exc
    store = SQLAIRequestStore(db)
    existing = await db.get(SlidePostAIRequest, body.client_request_id)
    if existing is None and not settings.slide_post_ai_recovery_enabled:
        raise HTTPException(404, "Recoverable AI editing is not available yet")
    from app.services.slide_post_ai_authority import authority_snapshot
    from app.services.slide_post_ai_delivery import deliver, publish_due
    from app.services.slide_post_ai_prepared import PrepareAIExecution, PreparedInput, envelope

    prepared = authority = None
    if existing is None:
        # Canonical item locks serialize Save/source replacement. Thread is read
        # optimistically here and locked NOWAIT with an exact pin check at claim.
        item, plan, _ = await plan_items._load_owned_item_context(
            item_id, user.id, db, for_update=True
        )
        await db.refresh(thread)
        if (
            thread.creator_id != user.id
            or thread.active_plan_item_id != item.id
            or thread.status != "active"
        ):
            raise HTTPException(409, "This project changed before admission")
        authority = await authority_snapshot(db, item, plan, thread)
        gate = PrepareAIExecution(body.client_request_id)
        try:
            if body.kind == "propose":
                result = await plan_items._propose_slide_post_impl(
                    request, item_id, parsed, user, db, paid_execution=gate
                )
            else:
                result = await plan_items._chat_edit_slide_post_impl(
                    request, item_id, parsed, user, db, client_protocol, paid_execution=gate
                )
            prepared = envelope(body.kind, result=result.model_dump(mode="json"))
        except PreparedInput as captured:
            prepared = envelope(body.kind, snapshot=captured.snapshot)
        except HTTPException as exc:
            if not 400 <= exc.status_code < 500:
                raise
            prepared = {
                "version": 1,
                "rejection": "This request cannot use the current draft. Send a new instruction.",
            }
        await db.refresh(thread)
        if authority != await authority_snapshot(db, item, plan, thread):
            raise HTTPException(409, "The original source changed during admission")
    try:
        row = await store.reserve(
            request_id=body.client_request_id,
            owner_id=user.id,
            item_id=item.id,
            project_id=thread.id,
            kind=body.kind,
            body=body.input,
            prepared_input=prepared,
            authority_snapshot=authority,
        )
    except AIReceiptConflict as exc:
        raise HTTPException(409, "AI request identity mismatch") from exc
    if row.status == "pending":
        # The store returns the INSERT winner. Never execute caller-local preparation.
        await deliver(db, row.id)
        # Inline completion is the low-latency path. A busy claim or cancelled
        # HTTP process leaves a durable outbox row for the independently scheduled sweep.
        await publish_due(db, request_id=body.client_request_id)
    return receipt_output(await owned_receipt(item_id, body.client_request_id, user, db))


@router.post(
    "/{item_id}/slide-post/ai-requests/{request_id}/cancel", response_model=AIRequestReceipt
)
async def cancel_ai_request(
    item_id: str, request_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    row = await owned_receipt(item_id, request_id, user, db)
    if row.status == "pending":
        await SQLAIRequestStore(db).cancel(row.id)
        row = await owned_receipt(item_id, request_id, user, db)
    if row.status in {"running", "unknown"}:
        raise HTTPException(
            409, "This original request was already claimed; its outcome remains observable."
        )
    return receipt_output(row)
