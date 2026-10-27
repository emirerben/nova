"""Opaque request links on existing committed user events, without another event stream."""

from __future__ import annotations

import os
import re
import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.models import CreationThreadEvent, CreatorAgentTurn
from app.services.client_protocol import current_client_protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.kria.api_schemas import RequestOutcomeContext


class RequestOutcomeLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    request_turn_id: uuid.UUID
    intent_revision_turn_id: uuid.UUID
    parent_turn_id: uuid.UUID | None = None
    relation: Literal["original", "continuation", "retry", "revision", "recovery", "unclassified"]
    original_known: bool
    link_status: Literal["known", "legacy_parent", "parent_unavailable", "unclassified"]
    client_protocol: int | None = Field(default=None, ge=0, le=9999)
    client_build: str | None = Field(default=None, pattern=r"^[0-9]{1,10}$")
    client_version: str | None = Field(
        default=None, max_length=32, pattern=r"^[0-9]+(?:\.[0-9]+){0,3}$"
    )
    server_revision: str = Field(default="unknown", pattern=r"^(?:[a-fA-F0-9]{40}|unknown)$")


async def request_outcome_payload(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
    context: RequestOutcomeContext | None,
) -> dict:
    if context is None:
        return {}
    request_id, revision_id = turn_id, turn_id
    parent_id = None
    known = context.relation == "original"
    status = "known" if known else "unclassified"
    if context.parent_turn_id is not None:
        parent = (
            await db.execute(
                select(CreatorAgentTurn.id, CreationThreadEvent.payload)
                .join(
                    CreationThreadEvent, CreationThreadEvent.id == CreatorAgentTurn.source_event_id
                )
                .where(
                    CreatorAgentTurn.id == context.parent_turn_id,
                    CreatorAgentTurn.thread_id == thread_id,
                    CreationThreadEvent.thread_id == thread_id,
                )
            )
        ).one_or_none()
        if parent is None:
            status = "parent_unavailable"
        else:
            parent_id = parent.id
            try:
                prior = RequestOutcomeLink.model_validate(
                    (parent.payload or {}).get("request_outcome_link")
                )
            except (ValidationError, TypeError):
                prior = None
            request_id = prior.request_turn_id if prior else parent.id
            revision_id = (
                turn_id
                if context.relation in {"revision", "recovery"}
                else prior.intent_revision_turn_id
                if prior
                else parent.id
            )
            known = prior.original_known if prior else False
            status = prior.link_status if prior else "legacy_parent"
    revision = os.environ.get("CREATOR_DURATION_CHECK_REVISION", "")
    link = RequestOutcomeLink(
        request_turn_id=request_id,
        intent_revision_turn_id=revision_id,
        parent_turn_id=parent_id,
        relation=context.relation,
        original_known=known,
        link_status=status,
        client_protocol=current_client_protocol(),
        client_build=context.client_build,
        client_version=context.client_version,
        server_revision=revision if re.fullmatch(r"[0-9a-f]{40}", revision) else "unknown",
    )
    return {"request_outcome_link": link.model_dump(mode="json", exclude_none=True)}
