"""Admin read-only integrity report for one creation thread.

Diagnoses a "my creation disappeared" report by naming exactly which
render-graph edge (PlanItem / ContentPlan / CreatorAgentSession / Job) is
incoherent, reusing the same predicate that ``GET /creation-threads/{id}``
degrades on -- so the operator's view and the user-facing behavior can never
drift apart. ``/integrity`` never returns a signed URL or event content.

``/events`` and ``/turns`` are the operator's transcript view: they DO return
the creator's message text, payloads, ``KriaTurnPlan`` and tool receipts (that
is their purpose, and the router is admin-only), but any signed storage URL
found inside is replaced with a placeholder. Both are strictly read-only.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadDeletion,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
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


# ── Transcript + turn visibility (read-only) ─────────────────────────────────

EVENTS_DEFAULT_LIMIT = 100
EVENTS_MAX_LIMIT = 500
TURNS_DEFAULT_LIMIT = 50
TURNS_MAX_LIMIT = 200

_SIGNED_URL_MARKERS = (
    "X-Goog-Signature",
    "X-Amz-Signature",
    "X-Goog-Credential",
    "X-Amz-Credential",
    "Signature=",
)
_MAX_SEQUENCE_CURSOR = 2**31 - 1  # sequence is a 32-bit Integer column
_REDACTED_URL = "[redacted-signed-url]"


def _scrub(value: Any) -> Any:
    """Replace signed storage URLs anywhere inside a JSON-ish value."""
    if isinstance(value, str):
        return _REDACTED_URL if any(m in value for m in _SIGNED_URL_MARKERS) else value
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_thread_id(thread_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid_thread_id") from exc


async def _require_thread(db: AsyncSession, thread_id: uuid.UUID) -> CreationThread:
    thread = (
        await db.execute(select(CreationThread).where(CreationThread.id == thread_id))
    ).scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="thread_not_found")
    return thread


class ThreadEventOut(BaseModel):
    id: str
    sequence: int
    kind: str
    actor: str
    revision: int
    created_at: str | None
    client_event_id: str | None = None
    content: str | None = None
    payload: Any = None
    # Set on the event that started a runtime-v2 turn.
    turn_id: str | None = None
    # Brief ledger version that turn produced (creative_brief_versions).
    brief_version: int | None = None


class ThreadEventsOut(BaseModel):
    thread_id: str
    runtime_version: int
    events: list[ThreadEventOut]
    next_cursor: str | None = None


@router.get("/{thread_id}/events", response_model=ThreadEventsOut)
async def creation_thread_events(
    thread_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=EVENTS_DEFAULT_LIMIT, ge=1, le=EVENTS_MAX_LIMIT),
    cursor: str | None = Query(default=None, max_length=32),
) -> ThreadEventsOut:
    """Transcript events in ``sequence`` order; ``cursor`` is the last sequence seen."""
    identifier = _parse_thread_id(thread_id)
    after_sequence = -1
    if cursor:
        if not (cursor.isascii() and cursor.isdigit()):
            raise HTTPException(status_code=422, detail="invalid_cursor")
        after_sequence = int(cursor)
        if after_sequence > _MAX_SEQUENCE_CURSOR:
            raise HTTPException(status_code=422, detail="invalid_cursor")
    thread = await _require_thread(db, identifier)

    fetched = list(
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread.id,
                    CreationThreadEvent.sequence > after_sequence,
                )
                .order_by(CreationThreadEvent.sequence)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = fetched[:limit]
    has_more = len(fetched) > limit

    turn_by_event: dict[uuid.UUID, uuid.UUID] = {}
    brief_by_turn: dict[uuid.UUID, int] = {}
    if page:
        turn_rows = (
            await db.execute(
                select(CreatorAgentTurn.id, CreatorAgentTurn.source_event_id).where(
                    CreatorAgentTurn.thread_id == thread.id,
                    CreatorAgentTurn.source_event_id.in_([e.id for e in page]),
                )
            )
        ).all()
        turn_by_event = {row[1]: row[0] for row in turn_rows}
        if turn_by_event:
            brief_rows = (
                await db.execute(
                    select(CreativeBriefVersion.source_turn_id, CreativeBriefVersion.version).where(
                        CreativeBriefVersion.thread_id == thread.id,
                        CreativeBriefVersion.source_turn_id.in_(list(turn_by_event.values())),
                    )
                )
            ).all()
            brief_by_turn = {row[0]: int(row[1]) for row in brief_rows}

    events = []
    for e in page:
        turn_id = turn_by_event.get(e.id)
        events.append(
            ThreadEventOut(
                id=str(e.id),
                sequence=e.sequence,
                kind=e.event_type,
                actor=e.role,
                revision=e.revision,
                created_at=_iso(e.created_at),
                client_event_id=e.client_event_id,
                content=e.content,
                payload=_scrub(e.payload),
                turn_id=str(turn_id) if turn_id else None,
                brief_version=brief_by_turn.get(turn_id) if turn_id else None,
            )
        )
    return ThreadEventsOut(
        thread_id=str(thread.id),
        runtime_version=int(thread.runtime_version),
        events=events,
        next_cursor=str(page[-1].sequence) if has_more and page else None,
    )


class TurnExecutionOut(BaseModel):
    id: str
    tool_name: str | None = None
    tool_version: int | None = None
    risk: str | None = None
    status: str
    dependency_group: int | None = None
    group_order: int | None = None
    target_job_id: str | None = None
    target_variant_id: str | None = None
    target_draft_id: str | None = None
    external_task_id: str | None = None
    # The tool receipt.
    result: Any = None
    error: Any = None
    created_at: str | None = None
    completed_at: str | None = None


class ThreadTurnOut(BaseModel):
    id: str
    status: str
    client_event_id: str
    source_event_id: str
    observed_event_id: str | None = None
    session_id: str | None = None
    created_at: str | None = None
    completed_at: str | None = None
    cancel_requested_at: str | None = None
    # The persisted KriaTurnPlan.
    plan: Any = None
    error: Any = None
    brief_version: int | None = None
    executions: list[TurnExecutionOut] = []
    job_ids: list[str] = []


class ThreadTurnsOut(BaseModel):
    thread_id: str
    runtime_version: int
    turns: list[ThreadTurnOut]
    next_cursor: str | None = None


def _encode_turn_cursor(created_at: datetime, turn_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{turn_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_turn_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_raw, id_raw = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
        return datetime.fromisoformat(created_raw), uuid.UUID(id_raw)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid_cursor") from exc


def _execution_out(ex: CreatorAgentExecution) -> TurnExecutionOut:
    return TurnExecutionOut(
        id=str(ex.id),
        tool_name=ex.tool_name,
        tool_version=ex.tool_version,
        risk=ex.risk,
        status=ex.status,
        dependency_group=ex.dependency_group,
        group_order=ex.group_order,
        target_job_id=str(ex.target_job_id) if ex.target_job_id else None,
        target_variant_id=ex.target_variant_id,
        target_draft_id=str(ex.target_draft_id) if ex.target_draft_id else None,
        external_task_id=ex.external_task_id,
        result=_scrub(ex.result),
        error=_scrub(ex.error),
        created_at=_iso(ex.created_at),
        completed_at=_iso(ex.completed_at),
    )


@router.get("/{thread_id}/turns", response_model=ThreadTurnsOut)
async def creation_thread_turns(
    thread_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=TURNS_DEFAULT_LIMIT, ge=1, le=TURNS_MAX_LIMIT),
    cursor: str | None = Query(default=None, max_length=256),
) -> ThreadTurnsOut:
    """Runtime-v2 turns oldest first: plan, tool receipts, linked executions/jobs.

    A runtime-v1 thread has no turn rows and returns an empty list.
    """
    identifier = _parse_thread_id(thread_id)
    cursor_key = _decode_turn_cursor(cursor) if cursor else None
    thread = await _require_thread(db, identifier)

    query = select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread.id)
    if cursor_key is not None:
        query = query.where(tuple_(CreatorAgentTurn.created_at, CreatorAgentTurn.id) > cursor_key)
    fetched = list(
        (
            await db.execute(
                query.order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    page = fetched[:limit]
    has_more = len(fetched) > limit

    executions_by_turn: dict[uuid.UUID, list[CreatorAgentExecution]] = {}
    brief_by_turn: dict[uuid.UUID, int] = {}
    if page:
        turn_ids = [t.id for t in page]
        execution_rows = (
            (
                await db.execute(
                    select(CreatorAgentExecution)
                    .where(CreatorAgentExecution.turn_id.in_(turn_ids))
                    .order_by(
                        CreatorAgentExecution.created_at,
                        CreatorAgentExecution.dependency_group,
                        CreatorAgentExecution.group_order,
                    )
                )
            )
            .scalars()
            .all()
        )
        for ex in execution_rows:
            executions_by_turn.setdefault(ex.turn_id, []).append(ex)
        brief_rows = (
            await db.execute(
                select(CreativeBriefVersion.source_turn_id, CreativeBriefVersion.version).where(
                    CreativeBriefVersion.thread_id == thread.id,
                    CreativeBriefVersion.source_turn_id.in_(turn_ids),
                )
            )
        ).all()
        brief_by_turn = {row[0]: int(row[1]) for row in brief_rows}

    turns_out: list[ThreadTurnOut] = []
    for turn in page:
        executions = executions_by_turn.get(turn.id, [])
        job_ids: list[str] = []
        for ex in executions:
            if ex.target_job_id is not None and str(ex.target_job_id) not in job_ids:
                job_ids.append(str(ex.target_job_id))
        turns_out.append(
            ThreadTurnOut(
                id=str(turn.id),
                status=turn.status,
                client_event_id=turn.client_event_id,
                source_event_id=str(turn.source_event_id),
                observed_event_id=str(turn.observed_event_id) if turn.observed_event_id else None,
                session_id=str(turn.session_id) if turn.session_id else None,
                created_at=_iso(turn.created_at),
                completed_at=_iso(turn.completed_at),
                cancel_requested_at=_iso(turn.cancel_requested_at),
                plan=_scrub(turn.plan_json),
                error=_scrub(turn.error),
                brief_version=brief_by_turn.get(turn.id),
                executions=[_execution_out(ex) for ex in executions],
                job_ids=job_ids,
            )
        )
    return ThreadTurnsOut(
        thread_id=str(thread.id),
        runtime_version=int(thread.runtime_version),
        turns=turns_out,
        next_cursor=(
            _encode_turn_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
        ),
    )
