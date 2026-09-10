"""Admin read-only integrity report for one creation thread.

Diagnoses a "my creation disappeared" report by naming exactly which
render-graph edge (PlanItem / ContentPlan / CreatorAgentSession / Job) is
incoherent, reusing the same predicate that ``GET /creation-threads/{id}``
degrades on -- so the operator's view and the user-facing behavior can never
drift apart. Never returns a signed URL or event content.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadDeletion,
    CreationThreadEvent,
    CreatorAgentSession,
    Job,
    PlanItem,
)
from app.routes.admin import _require_admin
from app.routes.creation_threads import _load_authorized_projection_rows

router = APIRouter(dependencies=[Depends(_require_admin)])


class ThreadIntegrityOut(BaseModel):
    id: str
    creator_id: str
    runtime_version: int
    status: str
    revision: int
    created_at: str
    updated_at: str


class TombstoneIntegrityOut(BaseModel):
    exists: bool
    creator_id: str | None = None
    created_at: str | None = None


class EventsIntegrityOut(BaseModel):
    count: int
    min_sequence: int | None = None
    max_sequence: int | None = None


class EdgeIntegrityOut(BaseModel):
    id: str | None = None
    exists: bool
    coherent: bool
    reason: str | None = None


class VariantIntegrityOut(BaseModel):
    variant_id: str | None = None
    render_status: str | None = None
    has_video_path: bool = False


class VideoIntegrityOut(BaseModel):
    job_id: str | None = None
    job_status: str | None = None
    variant_count: int = 0
    variants: list[VariantIntegrityOut] = []


class CreationThreadIntegrityOut(BaseModel):
    thread: ThreadIntegrityOut | None = None
    tombstone: TombstoneIntegrityOut
    events: EventsIntegrityOut | None = None
    edges: dict[str, EdgeIntegrityOut] = {}
    video: VideoIntegrityOut | None = None


async def _edge_out(db: AsyncSession, model: type, row_id: uuid.UUID | None) -> EdgeIntegrityOut:
    if row_id is None:
        return EdgeIntegrityOut(id=None, exists=False, coherent=True)
    row = await db.get(model, row_id)
    return EdgeIntegrityOut(id=str(row_id), exists=row is not None, coherent=row is not None)


@router.get("/{thread_id}/integrity", response_model=CreationThreadIntegrityOut)
async def creation_thread_integrity(
    thread_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreationThreadIntegrityOut:
    try:
        identifier = uuid.UUID(thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_thread_id") from exc

    thread = (
        await db.execute(select(CreationThread).where(CreationThread.id == identifier))
    ).scalar_one_or_none()

    tombstone = (
        await db.execute(
            select(CreationThreadDeletion).where(CreationThreadDeletion.thread_id == identifier)
        )
    ).scalar_one_or_none()
    tombstone_out = (
        TombstoneIntegrityOut(
            exists=True,
            creator_id=str(tombstone.creator_id),
            created_at=tombstone.created_at.isoformat(),
        )
        if tombstone is not None
        else TombstoneIntegrityOut(exists=False)
    )

    if thread is None:
        return CreationThreadIntegrityOut(thread=None, tombstone=tombstone_out)

    thread_out = ThreadIntegrityOut(
        id=str(thread.id),
        creator_id=str(thread.creator_id),
        runtime_version=int(thread.runtime_version),
        status=thread.status,
        revision=thread.revision,
        created_at=thread.created_at.isoformat(),
        updated_at=thread.updated_at.isoformat(),
    )

    event_row = (
        await db.execute(
            select(
                func.count(CreationThreadEvent.id),
                func.min(CreationThreadEvent.sequence),
                func.max(CreationThreadEvent.sequence),
            ).where(CreationThreadEvent.thread_id == thread.id)
        )
    ).one()
    events_out = EventsIntegrityOut(
        count=int(event_row[0] or 0), min_sequence=event_row[1], max_sequence=event_row[2]
    )

    # Reuse the exact predicate the user-facing read degrades on, so this
    # report can never say "coherent" for an edge the API would still hide.
    item, session, job, integrity = await _load_authorized_projection_rows(db, thread, degrade=True)
    reason_by_edge = dict(zip(integrity.detached, integrity.codes, strict=False))
    edges = {
        "plan_item": (await _edge_out(db, PlanItem, thread.active_plan_item_id)).model_copy(
            update={"reason": reason_by_edge.get("plan_item")}
        ),
        "content_plan": await _edge_out(db, ContentPlan, thread.content_plan_id),
        "creator_agent_session": (
            await _edge_out(db, CreatorAgentSession, thread.active_creator_agent_session_id)
        ).model_copy(update={"reason": reason_by_edge.get("creator_agent_session")}),
        "job": (await _edge_out(db, Job, thread.active_job_id)).model_copy(
            update={"reason": reason_by_edge.get("job")}
        ),
    }
    for edge_name, edge in edges.items():
        if reason_by_edge.get(edge_name):
            edge.coherent = False

    video_out = None
    raw_job = job or (await db.get(Job, thread.active_job_id) if thread.active_job_id else None)
    if raw_job is not None:
        variants_raw = (
            raw_job.assembly_plan.get("variants")
            if isinstance(raw_job.assembly_plan, dict)
            else None
        )
        variants = [
            VariantIntegrityOut(
                variant_id=row.get("variant_id"),
                render_status=row.get("render_status"),
                has_video_path=bool(row.get("video_path")),
            )
            for row in (variants_raw if isinstance(variants_raw, list) else [])
            if isinstance(row, dict)
        ]
        video_out = VideoIntegrityOut(
            job_id=str(raw_job.id),
            job_status=raw_job.status,
            variant_count=len(variants),
            variants=variants,
        )

    return CreationThreadIntegrityOut(
        thread=thread_out,
        tombstone=tombstone_out,
        events=events_out,
        edges=edges,
        video=video_out,
    )
