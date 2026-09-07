"""Durable runtime-v2 turn, delta, cancellation, and approval boundaries.

This service owns only the control plane. It never renders and never calls an
HTTP route internally. Consequential approval records explicit consent; the
later editor-commit service must consume that exact record and use the existing
Job dispatcher.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kria.api_schemas import (
    ApprovalDecisionBody,
    ApprovalDecisionOut,
    ApprovalSnapshotOut,
    DeltaEvent,
    SubmitTurnBody,
    ThreadDeltaOut,
    TurnAccepted,
    TurnCancelled,
)
from app.kria.contracts import KriaTurnPlan
from app.kria.language import is_help_question, is_status_question
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
)


@dataclass
class RuntimeFailure(Exception):
    status_code: int
    code: str
    message: str
    phase: Literal["accept", "plan", "policy", "tool", "approval", "dispatch", "observe"] = "accept"
    recovery: Literal["retry", "refresh_replan", "ask_user", "manual", "none"] = "none"
    retryable: bool = False
    current_revision: int | None = None


def request_digest(body: SubmitTurnBody) -> str:
    encoded = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def approval_fingerprint(approval: CreatorAgentApproval) -> str:
    """Hash immutable server-authored pins; this is a stale fence, not auth."""

    pins = {
        "approval_id": str(approval.id),
        "turn_id": str(approval.turn_id),
        "draft_id": str(approval.draft_id) if approval.draft_id else None,
        "draft_revision": approval.draft_revision,
        "target_job_id": str(approval.target_job_id) if approval.target_job_id else None,
        "target_variant_id": approval.target_variant_id,
        "target_generation_id": approval.target_generation_id,
        "target_manifest_hash": approval.target_manifest_hash,
        "target_ownership_epoch": approval.target_ownership_epoch,
        "expires_at": approval.expires_at.isoformat(),
    }
    canonical = json.dumps(pins, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _owned_thread(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    lock: bool,
    require_active: bool = True,
) -> CreationThread:
    statement = select(CreationThread).where(
        CreationThread.id == thread_id,
        CreationThread.creator_id == creator_id,
    )
    if lock:
        statement = statement.with_for_update()
    thread = (await db.execute(statement)).scalar_one_or_none()
    if thread is None:
        raise RuntimeFailure(404, "thread_not_found", "Creation thread not found")
    if int(thread.runtime_version) != 2:
        raise RuntimeFailure(
            409,
            "runtime_version_required",
            "This project uses the original creation experience.",
            recovery="manual",
            current_revision=int(thread.revision),
        )
    if require_active and thread.status != "active":
        raise RuntimeFailure(
            409,
            "thread_inactive",
            "This creation thread is not active.",
            current_revision=int(thread.revision),
        )
    return thread


async def _append_event(
    db: AsyncSession,
    thread: CreationThread,
    *,
    role: str,
    event_type: str,
    content: str | None,
    payload: dict[str, Any] | None = None,
    client_event_id: str | None = None,
) -> CreationThreadEvent:
    sequence = (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(CreationThreadEvent.sequence), -1)).where(
                        CreationThreadEvent.thread_id == thread.id
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    thread.revision = int(thread.revision) + 1
    event = CreationThreadEvent(
        thread_id=thread.id,
        sequence=sequence,
        revision=thread.revision,
        client_event_id=client_event_id,
        role=role,
        event_type=event_type,
        content=content,
        payload=payload,
    )
    db.add(event)
    await db.flush()
    return event


async def submit_turn(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    body: SubmitTurnBody,
) -> tuple[TurnAccepted, bool]:
    """Commit a user event and pending turn together, before broker I/O."""

    thread = await _owned_thread(db, thread_id=thread_id, creator_id=creator_id, lock=True)
    digest = request_digest(body)
    existing = (
        await db.execute(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.thread_id == thread.id,
                CreatorAgentTurn.client_event_id == body.client_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_digest != digest:
            raise RuntimeFailure(
                409,
                "idempotency_key_reused",
                "That message identity was already used for different content.",
                recovery="refresh_replan",
                current_revision=int(thread.revision),
            )
        # The caller may publish a durable pending turn after this returns.
        # Release the thread lock before touching the broker.
        replay_turn_id = str(existing.id)
        replay_revision = int(thread.revision)
        replay_status = existing.status
        should_publish = replay_status == "pending"
        await db.rollback()
        return (
            TurnAccepted(
                turn_id=replay_turn_id,
                thread_revision=replay_revision,
                status=replay_status,
                replayed=True,
            ),
            should_publish,
        )
    if int(thread.revision) != body.expected_thread_revision:
        raise RuntimeFailure(
            409,
            "thread_revision_stale",
            "The project changed before this message was accepted.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )

    active = (
        (
            await db.execute(
                select(CreatorAgentTurn)
                .where(
                    CreatorAgentTurn.thread_id == thread.id,
                    CreatorAgentTurn.status.in_(
                        {"pending", "planning", "executing", "awaiting_approval", "observing"}
                    ),
                )
                .order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
            )
        )
        .scalars()
        .first()
    )
    inert_response: tuple[Literal["progress", "question"], str] | None = None
    if is_status_question(body.message):
        status = getattr(active, "status", None)
        if status in {"pending", "planning"}:
            message = "I’m working on the edit plan. Your project is saved."
        elif status == "awaiting_approval":
            message = "Your draft is ready. Review the pinned approval before I start the render."
        elif status in {"executing", "observing"}:
            message = "Your approved render is in progress. Your draft is saved."
        else:
            message = "There’s no edit running right now. Your latest project state is saved."
        inert_response = ("progress", message)
    elif is_help_question(body.message):
        inert_response = (
            "question",
            "I can inspect your footage, prepare reversible draft edits, explain what changed, "
            "and render only after you approve the exact draft.",
        )

    if inert_response is not None:
        turn_value, message = inert_response
        turn_id = uuid.uuid4()
        source = await _append_event(
            db,
            thread,
            role="user",
            event_type="user_message",
            content=body.message,
            payload={
                "request_digest": digest,
                "runtime_version": 2,
                "turn_id": str(turn_id),
                "turn_status": "completed",
                "inert": True,
            },
            client_event_id=body.client_event_id,
        )
        observed = await _append_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content=message,
            payload={
                "turn_id": str(turn_id),
                "turn_value": turn_value,
                "receipt_ids": [],
                "next_actions": [],
                "schema_version": 2,
            },
        )
        plan = KriaTurnPlan(mode="respond", turn_value=turn_value, response=message)
        db.add(
            CreatorAgentTurn(
                id=turn_id,
                thread_id=thread.id,
                session_id=thread.active_creator_agent_session_id,
                source_event_id=source.id,
                client_event_id=body.client_event_id,
                request_digest=digest,
                status="completed",
                plan_json=plan.model_dump(mode="json"),
                observed_event_id=observed.id,
                completed_at=datetime.now(UTC),
            )
        )
        await db.flush()
        await db.commit()
        return (
            TurnAccepted(
                turn_id=str(turn_id),
                thread_revision=int(thread.revision),
                status="completed",
            ),
            False,
        )

    # Always check the successor slot, including the tiny window after the
    # active turn commits and before its queued successor is promoted. The
    # promotion transaction also locks Thread, so this Thread lock serializes
    # the two paths and prevents two pending turns.
    queued = (
        await db.execute(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.thread_id == thread.id,
                CreatorAgentTurn.status == "queued",
            )
        )
    ).scalar_one_or_none()
    if queued is not None:
        raise RuntimeFailure(
            409,
            "queued_successor_exists",
            "A follow-up is already waiting for the current turn.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    turn_status = "pending"
    queued_replaces_turn_id = None
    if active is not None:
        turn_status = "queued"
        queued_replaces_turn_id = active.id

    turn_id = uuid.uuid4()
    event = await _append_event(
        db,
        thread,
        role="user",
        event_type="user_message",
        content=body.message,
        payload={
            "request_digest": digest,
            "runtime_version": 2,
            "turn_id": str(turn_id),
            "turn_status": turn_status,
        },
        client_event_id=body.client_event_id,
    )
    turn = CreatorAgentTurn(
        id=turn_id,
        thread_id=thread.id,
        session_id=thread.active_creator_agent_session_id,
        source_event_id=event.id,
        client_event_id=body.client_event_id,
        request_digest=digest,
        status=turn_status,
        queued_replaces_turn_id=queued_replaces_turn_id,
    )
    db.add(turn)
    await db.flush()
    await db.commit()
    return (
        TurnAccepted(
            turn_id=str(turn.id),
            thread_revision=int(thread.revision),
            status=turn.status,
        ),
        turn_status == "pending",
    )


async def read_delta(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    after_sequence: int,
    limit: int,
    before_sequence: int | None = None,
) -> ThreadDeltaOut:
    """Read the newest bootstrap page or events after an explicit cursor.

    ``after_sequence == -1`` is the route's omitted-cursor sentinel. Bootstrap
    reads newest-first for a bounded query, then restores chronological response
    order. ``before_sequence`` retrieves older pages; nonnegative after cursors
    remain forward-only deltas.
    """

    thread = await _owned_thread(
        db,
        thread_id=thread_id,
        creator_id=creator_id,
        lock=False,
        require_active=False,
    )
    if before_sequence is not None and after_sequence != -1:
        raise RuntimeFailure(
            422,
            "cursor_invalid",
            "Use either after_sequence or before_sequence, not both.",
            recovery="manual",
        )
    statement = select(CreationThreadEvent).where(CreationThreadEvent.thread_id == thread.id)
    if before_sequence is not None:
        statement = statement.where(CreationThreadEvent.sequence < before_sequence).order_by(
            CreationThreadEvent.sequence.desc()
        )
    elif after_sequence == -1:
        statement = statement.order_by(CreationThreadEvent.sequence.desc())
    else:
        statement = statement.where(CreationThreadEvent.sequence > after_sequence).order_by(
            CreationThreadEvent.sequence
        )
    rows = list((await db.execute(statement.limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    if after_sequence == -1:
        rows.reverse()
    next_sequence = rows[-1].sequence if rows else after_sequence
    previous_sequence = rows[0].sequence if after_sequence == -1 and rows and has_more else None
    return ThreadDeltaOut(
        thread_id=str(thread.id),
        runtime_version=2,
        status=thread.status,
        thread_revision=int(thread.revision),
        events=[
            DeltaEvent(
                id=str(row.id),
                sequence=row.sequence,
                revision=row.revision,
                role=row.role,
                event_type=row.event_type,
                content=row.content,
                payload=row.payload,
                created_at=row.created_at,
            )
            for row in rows
        ],
        after_sequence=after_sequence,
        next_after_sequence=next_sequence,
        before_sequence=before_sequence,
        previous_before_sequence=previous_sequence,
        has_more=has_more,
    )


async def cancel_turn(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
    creator_id: uuid.UUID,
    expected_thread_revision: int,
) -> tuple[TurnCancelled, str | None]:
    # Lock Turn before CreationThread per the global runtime lock order. This
    # transaction does not touch PlanItem/Job and cannot cancel a render.
    turn = (
        await db.execute(
            select(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == turn_id, CreatorAgentTurn.thread_id == thread_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if turn is None:
        raise RuntimeFailure(404, "turn_not_found", "Kria turn not found")
    thread = await _owned_thread(db, thread_id=thread_id, creator_id=creator_id, lock=True)
    if int(thread.revision) != expected_thread_revision:
        raise RuntimeFailure(
            409,
            "thread_revision_stale",
            "The project changed before cancellation.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    if turn.status in {"completed", "failed", "cancelled", "superseded"}:
        if turn.status != "cancelled":
            raise RuntimeFailure(
                409,
                "turn_not_cancellable",
                "This turn has already finished.",
                current_revision=int(thread.revision),
            )
        return (
            TurnCancelled(
                turn_id=str(turn.id), thread_revision=int(thread.revision), status="cancelled"
            ),
            None,
        )
    if turn.status in {"executing", "observing"}:
        raise RuntimeFailure(
            409,
            "turn_not_cancellable",
            "Kria has already committed work for this turn.",
            recovery="manual",
            current_revision=int(thread.revision),
        )

    now = datetime.now(UTC)
    approval_ids: list[str] = []
    if turn.status == "awaiting_approval":
        approvals = list(
            (
                await db.execute(
                    select(CreatorAgentApproval)
                    .where(
                        CreatorAgentApproval.turn_id == turn.id,
                        CreatorAgentApproval.status == "pending",
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for approval in approvals:
            approval.status = "cancelled"
            approval_ids.append(str(approval.id))
    turn.cancel_requested_at = now
    turn.status = "cancelled"
    turn.completed_at = now
    await _append_event(
        db,
        thread,
        role="system",
        event_type="turn_cancelled",
        content=None,
        payload={"turn_id": str(turn.id), "approval_ids": approval_ids},
    )
    await db.commit()
    # Promotion is deliberately a second transaction. Locking queued Turn B
    # while this transaction holds Thread would deadlock with cancellation of
    # B, whose valid order is Turn B -> Thread.
    successor_id = await _promote_queued_successor(db, thread_id=thread.id)
    return (
        TurnCancelled(
            turn_id=str(turn.id),
            thread_revision=int(thread.revision),
            status=turn.status,
            approval_ids=approval_ids,
        ),
        successor_id,
    )


async def _promote_queued_successor(db: AsyncSession, *, thread_id: uuid.UUID) -> str | None:
    successor = (
        (
            await db.execute(
                select(CreatorAgentTurn)
                .where(
                    CreatorAgentTurn.thread_id == thread_id,
                    CreatorAgentTurn.status == "queued",
                )
                .order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if successor is None:
        await db.rollback()
        return None
    # Turn -> Thread is the global order. Holding Thread here serializes the
    # promotion with submit_turn, which locks Thread before checking slots.
    await db.execute(select(CreationThread).where(CreationThread.id == thread_id).with_for_update())
    successor.status = "pending"
    await db.commit()
    return str(successor.id)


async def decide_approval(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    approval_id: uuid.UUID,
    creator_id: uuid.UUID,
    decision: Literal["approve", "deny"],
    body: ApprovalDecisionBody,
) -> tuple[ApprovalDecisionOut, str | None]:
    # Session -> Turn -> Draft -> Approval -> Thread follows the global lock
    # order. The initial approval read only discovers immutable FK identities.
    approval_ref = (
        await db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.id == approval_id,
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.creator_id == creator_id,
            )
        )
    ).scalar_one_or_none()
    if approval_ref is None:
        raise RuntimeFailure(404, "approval_not_found", "Approval not found", phase="approval")
    if approval_ref.draft_id is None or approval_ref.draft_revision is None:
        raise RuntimeFailure(
            409,
            "approval_target_missing",
            "This approval does not contain a renderable draft.",
            phase="approval",
            recovery="refresh_replan",
        )
    session = (
        await db.execute(
            select(CreatorAgentSession)
            .where(
                CreatorAgentSession.id == approval_ref.session_id,
                CreatorAgentSession.creator_id == creator_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise RuntimeFailure(
            409,
            "approval_target_missing",
            "The edit session for this approval is unavailable.",
            phase="approval",
            recovery="refresh_replan",
        )
    turn = (
        await db.execute(
            select(CreatorAgentTurn)
            .where(
                CreatorAgentTurn.id == approval_ref.turn_id,
                CreatorAgentTurn.thread_id == thread_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if turn is None:
        raise RuntimeFailure(
            409,
            "approval_target_missing",
            "The turn for this approval is unavailable.",
            phase="approval",
            recovery="refresh_replan",
        )
    draft = (
        await db.execute(
            select(CreatorEditDraft)
            .where(
                CreatorEditDraft.id == approval_ref.draft_id,
                CreatorEditDraft.creator_id == creator_id,
                CreatorEditDraft.thread_id == thread_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    approval = (
        await db.execute(
            select(CreatorAgentApproval)
            .where(
                CreatorAgentApproval.id == approval_id,
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.creator_id == creator_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    thread = await _owned_thread(db, thread_id=thread_id, creator_id=creator_id, lock=True)
    if approval is None or draft is None:
        raise RuntimeFailure(
            409,
            "approval_target_missing",
            "The approved draft is unavailable.",
            phase="approval",
        )
    session_pins_match = (
        int(session.ownership_epoch) == approval.target_ownership_epoch
        and (
            approval.target_manifest_hash is None
            or session.manifest_hash == approval.target_manifest_hash
        )
        and (approval.target_job_id is None or session.target_job_id == approval.target_job_id)
        and (
            approval.target_variant_id is None
            or session.target_variant_id == approval.target_variant_id
        )
        and (
            approval.target_generation_id is None
            or session.target_generation_id == approval.target_generation_id
        )
    )
    if not session_pins_match or thread.active_creator_agent_session_id != session.id:
        raise RuntimeFailure(
            409,
            "approval_target_stale",
            "The edit target changed after this approval was prepared.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    if int(thread.revision) != body.expected_thread_revision:
        raise RuntimeFailure(
            409,
            "thread_revision_stale",
            "The project changed before this decision.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    if approval_fingerprint(approval) != body.expected_approval_fingerprint:
        raise RuntimeFailure(
            409,
            "approval_stale",
            "This approval no longer describes the current action.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    now = datetime.now(UTC)
    if approval.status != "pending":
        raise RuntimeFailure(
            409,
            "approval_not_pending",
            "This approval has already been decided.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    if approval.expires_at <= now:
        approval.status = "expired"
        await db.commit()
        raise RuntimeFailure(
            409,
            "approval_expired",
            "This approval expired. Ask Kria to prepare it again.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    if approval.draft_revision != body.expected_draft_revision:
        raise RuntimeFailure(
            409,
            "approval_draft_stale",
            "This approval targets a different draft revision.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    if draft.draft_revision != body.expected_draft_revision or not draft.is_head:
        raise RuntimeFailure(
            409,
            "draft_stale",
            "The draft changed after this approval was prepared.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )

    approval.status = "approved" if decision == "approve" else "denied"
    if decision == "deny":
        turn.status = "completed"
        turn.completed_at = now
    event = await _append_event(
        db,
        thread,
        role="system",
        event_type=f"approval_{approval.status}",
        content=None,
        payload={
            "approval_id": str(approval.id),
            "turn_id": str(approval.turn_id),
            "draft_id": str(draft.id),
            "draft_revision": draft.draft_revision,
            "render_dispatched": False,
        },
    )
    del event
    await db.commit()
    successor_id = (
        await _promote_queued_successor(db, thread_id=thread.id) if decision == "deny" else None
    )
    return (
        ApprovalDecisionOut(
            approval_id=str(approval.id),
            turn_id=str(approval.turn_id),
            status=approval.status,
            thread_revision=int(thread.revision),
            render_dispatched=False,
        ),
        successor_id,
    )


async def read_approval(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    approval_id: uuid.UUID,
    creator_id: uuid.UUID,
) -> ApprovalSnapshotOut:
    """Return the server-authored pins a client must echo when deciding."""

    thread = await _owned_thread(
        db,
        thread_id=thread_id,
        creator_id=creator_id,
        lock=False,
        require_active=False,
    )
    approval = (
        await db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.id == approval_id,
                CreatorAgentApproval.thread_id == thread.id,
                CreatorAgentApproval.creator_id == creator_id,
            )
        )
    ).scalar_one_or_none()
    if approval is None:
        raise RuntimeFailure(404, "approval_not_found", "Approval not found", phase="approval")
    return ApprovalSnapshotOut(
        approval_id=str(approval.id),
        turn_id=str(approval.turn_id),
        draft_id=str(approval.draft_id) if approval.draft_id else None,
        draft_revision=approval.draft_revision,
        status=approval.status,
        consequence_summary=approval.consequence_summary,
        cost_summary=approval.cost_summary,
        expires_at=approval.expires_at,
        approval_fingerprint=approval_fingerprint(approval),
    )
