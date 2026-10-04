"""Durable runtime-v2 turn, delta, cancellation, and approval boundaries.

This service owns only the control plane. It never renders and never calls an
HTTP route internally. Consequential approval records explicit consent; the
later editor-commit service must consume that exact record and use the existing
Job dispatcher.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db_locks import CONTENT_PLAN_LOCK
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
from app.kria.brief import apply_receipt_statuses, load_latest_brief
from app.kria.brief_checks import is_judged
from app.kria.contracts import (
    CreativeBriefOut,
    CreativeBriefRequirementOut,
    KriaTurnPlan,
    RequirementReceipt,
)
from app.kria.language import is_help_question, is_status_question
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    PlanItem,
)
from app.services.choice_questions import ChoiceSelectionIn, latest_open_choice_question
from app.services.clip_selection import ClipSelectionIn, latest_open_clip_question
from app.services.creation_thread_titles import (
    matches_conversation_revision,
    prepare_message_title,
)
from app.services.speech_cleanup_decision import (
    SPEECH_CLEANUP_CONFLICT_COPY,
    SpeechCleanupDecisionConflict,
    evaluate_enforce_mode_decision,
    legacy_default_decision,
    resolve_next_audio_mode,
    reuse_recorded_cleanup_decision,
)

log = structlog.get_logger()


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
    # `editor_state` is deliberately outside the digest: a retry of the same
    # client_event_id carries a fresher snapshot of the editor, and the FIRST stored
    # state wins -- it must never turn a replay into idempotency_key_reused.
    # `clip_selection` stays in the digest (a different tap set under one id is a
    # different request) but is excluded when absent so pre-existing digests hold.
    excluded = (
        {"editor_state"}
        | ({"clip_selection"} if body.clip_selection is None else set())
        | ({"choice_selection"} if body.choice_selection is None else set())
    )
    encoded = json.dumps(
        body.model_dump(mode="json", exclude=excluded),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def approval_fingerprint(approval: CreatorAgentApproval) -> str:
    """Hash immutable server-authored pins; this is a stale fence, not auth.

    ``expires_at`` is normalized to UTC before formatting: asyncpg always
    returns a UTC-aware ``timestamptz`` regardless of the session's
    ``TimeZone`` GUC, while psycopg2 (the sync engine Celery tasks use to
    mint an approval) renders it in whatever timezone that GUC is set to.
    Both represent the identical instant, but ``.isoformat()`` on the two
    would otherwise disagree whenever the server's default timezone isn't
    UTC, hashing the SAME approval into two different fingerprints purely
    because of which driver read it -- not a real staleness.
    """

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
        "expires_at": approval.expires_at.astimezone(UTC).isoformat(),
    }
    canonical = json.dumps(pins, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _validate_clip_selection(
    db: AsyncSession, thread: CreationThread, selection: ClipSelectionIn
) -> None:
    """The answer must target the thread's latest UNANSWERED clip question, and every
    tapped clip must be one the server offered for that category (KRI-282)."""

    rows = (
        await db.execute(
            select(CreationThreadEvent.role, CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread.id,
                CreationThreadEvent.role.in_({"user", "assistant"}),
            )
            .order_by(CreationThreadEvent.sequence)
        )
    ).all()
    question = latest_open_clip_question((role, payload) for role, payload in rows)
    if question is None or question.get("question_id") != selection.question_id:
        raise RuntimeFailure(
            422,
            "clip_selection_stale",
            "That clip question is no longer open. Refresh and answer the latest one.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    categories = {c["key"]: c for c in question.get("categories", [])}
    unknown_keys = [a.key for a in selection.answers if a.key not in categories] + [
        k for k in selection.none_keys if k not in categories
    ]
    if unknown_keys:
        raise RuntimeFailure(
            422,
            "clip_selection_invalid",
            "That answer names a category the question did not ask about.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    for answer in selection.answers:
        offered = set(categories[answer.key].get("candidate_media_ids") or [])
        if not set(answer.media_ids) <= offered:
            raise RuntimeFailure(
                422,
                "clip_selection_invalid",
                "That answer includes a clip the question did not offer.",
                recovery="refresh_replan",
                current_revision=int(thread.revision),
            )


async def _validate_choice_selection(
    db: AsyncSession, thread: CreationThread, selection: ChoiceSelectionIn
) -> None:
    """The answer must target the thread's latest UNANSWERED choice question and name
    one of the options the server offered (KRI-282)."""

    rows = (
        await db.execute(
            select(CreationThreadEvent.role, CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread.id,
                CreationThreadEvent.role.in_({"user", "assistant"}),
            )
            .order_by(CreationThreadEvent.sequence)
        )
    ).all()
    question = latest_open_choice_question((role, payload) for role, payload in rows)
    if question is None or question.get("question_id") != selection.question_id:
        raise RuntimeFailure(
            422,
            "choice_selection_stale",
            "That question is no longer open. Refresh and answer the latest one.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    offered = {str(o.get("key")) for o in question.get("options") or [] if isinstance(o, dict)}
    if selection.option_key not in offered:
        raise RuntimeFailure(
            422,
            "choice_selection_invalid",
            "That answer is not one of the options the question offered.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )


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
    if role == "user" and event_type == "user_message" and content:
        await prepare_message_title(db, thread, content)
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
    _expected_revision_override: int | None = None,
) -> tuple[TurnAccepted, bool]:
    """Commit a user event and pending turn together, before broker I/O.

    ``_expected_revision_override`` is internal: set only on the single re-entry
    after an overdue approval was lazily expired (that expiry bumps the thread
    revision, which the client could not have known).
    """

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
    if not matches_conversation_revision(
        thread,
        body.expected_thread_revision
        if _expected_revision_override is None
        else _expected_revision_override,
    ):
        raise RuntimeFailure(
            409,
            "thread_revision_stale",
            "The project changed before this message was accepted.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )

    if body.clip_selection is not None and settings.kria_clip_selection_questions_enabled:
        await _validate_clip_selection(db, thread, body.clip_selection)
    if body.choice_selection is not None and settings.kria_choice_questions_enabled:
        await _validate_choice_selection(db, thread, body.choice_selection)

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
    if (
        active is not None
        and active.status == "awaiting_approval"
        and _expected_revision_override is None
    ):
        # KRI-295: an overdue pending approval must never block the thread (the
        # creator can no longer decide it -- the UI hides expired cards -- and a
        # queued follow-up would otherwise 409 every new message forever).
        overdue_approval_id = (
            (
                await db.execute(
                    select(CreatorAgentApproval.id).where(
                        CreatorAgentApproval.turn_id == active.id,
                        CreatorAgentApproval.status == "pending",
                        CreatorAgentApproval.expires_at <= datetime.now(UTC),
                    )
                )
            )
            .scalars()
            .first()
        )
        if overdue_approval_id is not None:
            # Release the Thread lock taken above: expiry re-locks in canonical
            # order (Session -> Turn -> Approval -> Execution -> Thread).
            await db.rollback()
            revision_after_expiry = await _expire_blocking_approval(
                db,
                thread_id=thread_id,
                creator_id=creator_id,
                approval_id=overdue_approval_id,
            )
            return await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=creator_id,
                body=body,
                _expected_revision_override=(
                    revision_after_expiry
                    if revision_after_expiry is not None
                    else body.expected_thread_revision
                ),
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
            (
                "A follow-up is already waiting. Approve or dismiss the pending render first."
                if getattr(active, "status", None) == "awaiting_approval"
                else "A follow-up is already waiting for the current turn."
            ),
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
            # Flag off: dropped silently, nothing stored (byte-identical to old clients).
            **(
                {"clip_selection": body.clip_selection.model_dump(mode="json")}
                if body.clip_selection is not None
                and settings.kria_clip_selection_questions_enabled
                else {}
            ),
            **(
                {"choice_selection": body.choice_selection.model_dump(mode="json")}
                if body.choice_selection is not None and settings.kria_choice_questions_enabled
                else {}
            ),
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
        # Flag off: dropped silently, nothing stored (byte-identical to old clients).
        **(
            {
                "editor_state": body.editor_state.model_dump(
                    mode="json", exclude_unset=True, exclude_none=True
                )
            }
            if body.editor_state is not None and settings.kria_editor_state_turns_enabled
            else {}
        ),
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
                client_event_id=getattr(row, "client_event_id", None),
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
    if not matches_conversation_revision(thread, expected_thread_revision):
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


def _parse_draft_document(snapshot_json: dict[str, Any] | None) -> Any | None:
    """Best-effort parse; a malformed/legacy row is simply not a strategy draft."""

    if not isinstance(snapshot_json, dict):
        return None
    # Lazy: `app.kria.drafts` imports from this module, so a top-level import
    # here would be circular.
    from app.kria.drafts import KriaDraftDocument  # noqa: PLC0415

    try:
        return KriaDraftDocument.model_validate(snapshot_json)
    except ValueError:
        return None


async def _lock_strategy_plan_item(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    creator_id: uuid.UUID,
) -> PlanItem | None:
    """Lock ContentPlan -> PlanItem ahead of Session, mirroring the claim task.

    Two unlocked reads discover the identity to lock (a session's
    ``plan_item_id`` and an item's ``content_plan_id`` are both immutable
    foreign keys), then both rows are re-read ``FOR UPDATE`` in canonical
    order. Returns ``None`` for any missing/foreign row -- the caller treats
    that exactly like "not a strategy approval" and lets the existing
    validation chain surface whatever is actually wrong.
    """

    session_peek = (
        await db.execute(select(CreatorAgentSession).where(CreatorAgentSession.id == session_id))
    ).scalar_one_or_none()
    if session_peek is None or session_peek.plan_item_id is None:
        return None
    item_peek = (
        await db.execute(select(PlanItem).where(PlanItem.id == session_peek.plan_item_id))
    ).scalar_one_or_none()
    if item_peek is None:
        return None
    plan_row = (
        await db.execute(
            select(ContentPlan)
            .where(
                ContentPlan.id == item_peek.content_plan_id,
                ContentPlan.user_id == creator_id,
            )
            .with_for_update(**CONTENT_PLAN_LOCK)
        )
    ).scalar_one_or_none()
    if plan_row is None:
        return None
    return (
        await db.execute(select(PlanItem).where(PlanItem.id == item_peek.id).with_for_update())
    ).scalar_one_or_none()


_STRATEGY_MEDIA_SNAPSHOT_FIELDS = (
    "edit_format",
    "audio_mode",
    "voiceover_caption_style",
    "user_edited",
)


def _snapshot_plan_item_media_fields(item: PlanItem) -> dict[str, Any]:
    """The pre-mutation values `_apply_strategy_approval_media` is about to change.

    Captured once per approval (never overwritten by a retry) so a later deny
    or expiry can restore the creator's actual media state -- otherwise a
    committed-but-never-approved mutation (e.g. a speech-cleanup 409 that left
    the approval pending) would strand the item on a direction the creator
    then rejected.
    """

    return {field: getattr(item, field) for field in _STRATEGY_MEDIA_SNAPSHOT_FIELDS}


def _clear_strategy_media_snapshot(execution: CreatorAgentExecution | None) -> None:
    """Drop a consumed/superseded snapshot. Safe to call when none exists."""

    if execution is None or not isinstance(execution.result, dict):
        return
    if "strategy_media_before" not in execution.result:
        return
    execution.result = {
        key: value for key, value in execution.result.items() if key != "strategy_media_before"
    }


async def _restore_strategy_approval_media(
    db: AsyncSession,
    *,
    item: PlanItem,
    snapshot: dict[str, Any],
) -> uuid.UUID | None:
    """Undo `_apply_strategy_approval_media`'s mutation for a denied/expired approval.

    Restores exactly the fields that call changed, then re-runs the same
    supersede/reschedule an ordinary mutation runs: the creator's actual media
    identity may resolve a different (or no) active narration source once
    restored, and that source's analysis is a completely separate concern
    from whatever the rejected strategy needed checked.
    """

    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        mutation_current_analysis_async,
        schedule_item_preflight_async,
    )

    edit_format = snapshot.get("edit_format")
    audio_mode = snapshot.get("audio_mode")
    if not isinstance(edit_format, str) or not isinstance(audio_mode, str):
        # A malformed/legacy snapshot must never crash a deny -- worst case,
        # the item keeps the rejected format and a human notices in review.
        return None
    current_cleanup_for_mutation = await mutation_current_analysis_async(
        db, item.id, for_update=True
    )
    mutate_plan_item_media(
        item,
        detector_policy=current_detector_policy(),
        edit_format=edit_format,
        audio_mode=audio_mode,
        current_analysis=current_cleanup_for_mutation,
    )
    item.voiceover_caption_style = snapshot.get("voiceover_caption_style")
    item.user_edited = bool(snapshot.get("user_edited", False))
    return await schedule_item_preflight_async(db, item)


async def _restore_strategy_media_snapshot_if_present(
    db: AsyncSession,
    *,
    item: PlanItem | None,
    execution: CreatorAgentExecution | None,
) -> uuid.UUID | None:
    """Reverse a strategy draft's committed media mutation, if one is pending reversal.

    Used by both the deny path and the lazy expiry check -- an approval that
    will never be approved must not leave the PlanItem on a direction the
    creator never confirmed.
    """

    if item is None or execution is None or not isinstance(execution.result, dict):
        return None
    snapshot = execution.result.get("strategy_media_before")
    if not isinstance(snapshot, dict):
        return None
    preflight_analysis_id = await _restore_strategy_approval_media(db, item=item, snapshot=snapshot)
    _clear_strategy_media_snapshot(execution)
    return preflight_analysis_id


@dataclass(frozen=True)
class _StrategyApprovalMedia:
    """What `_apply_strategy_approval_media` learned, for the caller to persist.

    ``preflight_analysis_id`` is set whenever this call scheduled a genuinely
    NEW current analysis (not a reused/historical row) -- the caller must
    publish it (enqueue the actual analysis work) once its own transaction,
    which contains this row's INSERT, has committed. A conflict path commits
    and publishes for itself instead (see `_commit_and_publish` below) and
    raises rather than returning, so it never reaches here.
    """

    speech_cleanup_stash: dict[str, Any] | None
    preflight_analysis_id: uuid.UUID | None


async def _apply_strategy_approval_media(
    db: AsyncSession,
    *,
    thread: CreationThread,
    item: PlanItem,
    strategy_payload: dict[str, Any],
    body: ApprovalDecisionBody,
    execution: CreatorAgentExecution | None,
) -> _StrategyApprovalMedia:
    """Apply a strategy draft's media targets, then gate on speech cleanup.

    Runs the exact media mutation `_claim_approval_dispatch` runs today, just
    earlier -- at approval time, under the PlanItem lock `decide_approval`
    already took for a strategy draft -- so the claim's later, identical call
    becomes a no-op (unchanged source -> unchanged fingerprint -> nothing to
    supersede or reschedule). Returns the stash to persist on the render
    execution's ``result["speech_cleanup"]`` for the claim to read back; both
    fields are ``None`` when there is nothing to gate (voiceover still
    required -- left to the claim exactly like before this change -- mode
    isn't "enforce", or this source isn't in the enforce cohort).

    May raise `RuntimeFailure` (phase="approval", recovery="ask_user") after
    committing the media mutation and any newly scheduled preflight analysis,
    so the check this refusal is waiting on has actually been scheduled.
    """

    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415

    strategy = CreativeStrategy.model_validate(strategy_payload)
    next_audio_mode = resolve_next_audio_mode(strategy, item)
    if next_audio_mode is None:
        # voiceover_required: leave the failure and its copy to the claim,
        # which still runs this same derivation against the (unmutated) item.
        return _StrategyApprovalMedia(None, None)

    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
        publish_preflight_after_commit,
        resolve_item_narration,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        current_analysis_async,
        mutation_current_analysis_async,
        preflight_enabled_for_source,
        schedule_item_preflight_async,
    )

    # Snapshot exactly once per approval (a retry after a 409 must not
    # overwrite it with the already-mutated, rejected-strategy values) so a
    # later deny or expiry can restore the creator's actual media state.
    if execution is not None and not (
        isinstance(execution.result, dict) and "strategy_media_before" in execution.result
    ):
        execution.result = {
            **(execution.result or {}),
            "strategy_media_before": _snapshot_plan_item_media_fields(item),
        }

    current_cleanup_for_mutation = await mutation_current_analysis_async(
        db, item.id, for_update=True
    )
    mutate_plan_item_media(
        item,
        detector_policy=current_detector_policy(),
        edit_format=strategy.edit_format,
        audio_mode=next_audio_mode,
        current_analysis=current_cleanup_for_mutation,
    )
    preflight_analysis_id = await schedule_item_preflight_async(db, item)
    caption_style = {
        "clean": "sentence",
        "editorial": "sentence",
        "kinetic": "word",
        "karaoke": "word",
    }.get(strategy.caption_style)
    if caption_style:
        item.voiceover_caption_style = caption_style
    elif strategy.caption_style == "none":
        item.voiceover_caption_style = None
    item.user_edited = True

    if settings.speech_cleanup_preflight_mode != "enforce":
        _clear_strategy_media_snapshot(execution)
        return _StrategyApprovalMedia(None, preflight_analysis_id)
    resolution = resolve_item_narration(item, detector_policy=current_detector_policy())
    enforced_for_source = bool(
        resolution.source
        and preflight_enabled_for_source(
            resolution.source.source_policy_fingerprint,
            mode=settings.speech_cleanup_preflight_mode,
            rollout_percent=settings.speech_cleanup_preflight_rollout_percent,
        )
    )
    if not enforced_for_source:
        _clear_strategy_media_snapshot(execution)
        return _StrategyApprovalMedia(None, preflight_analysis_id)

    async def _commit_and_publish(extra_analysis_id: uuid.UUID | None) -> None:
        await db.commit()
        if preflight_analysis_id is not None:
            await asyncio.to_thread(publish_preflight_after_commit, preflight_analysis_id)
        if extra_analysis_id is not None:
            await asyncio.to_thread(publish_preflight_after_commit, extra_analysis_id)

    if not body.speech_cleanup_aware:
        current_cleanup = await current_analysis_async(db, item.id)
        legacy = legacy_default_decision(current_cleanup)
        log.info(
            "kria_speech_cleanup_legacy_default",
            item_id=str(item.id),
            analysis_id=str(legacy.analysis_id) if legacy.analysis_id else None,
            choice=legacy.choice,
        )
        _clear_strategy_media_snapshot(execution)
        return _StrategyApprovalMedia(
            {
                "analysis_id": str(legacy.analysis_id) if legacy.analysis_id else None,
                "choice": legacy.choice,
            },
            preflight_analysis_id,
        )

    if body.speech_cleanup_choice == "create_without_cleanup":
        # The unchecked bypass: same shape as v1's dedicated `action ==
        # "create_without_cleanup"` route branch, folded into this one call
        # since runtime-v2 has no separate action verb.
        current_cleanup = await current_analysis_async(db, item.id, for_update=True)
        if (
            current_cleanup is None
            or body.speech_cleanup_analysis_id is None
            or current_cleanup.id != body.speech_cleanup_analysis_id
            or current_cleanup.status not in {"queued", "running", "failed"}
        ):
            await _commit_and_publish(None)
            raise RuntimeFailure(
                409,
                "speech_cleanup_analysis_changed",
                SPEECH_CLEANUP_CONFLICT_COPY["speech_cleanup_analysis_changed"],
                phase="approval",
                recovery="ask_user",
                current_revision=int(thread.revision),
            )
        _clear_strategy_media_snapshot(execution)
        return _StrategyApprovalMedia(
            {"analysis_id": str(current_cleanup.id), "choice": "create_without_cleanup"},
            preflight_analysis_id,
        )

    submitted_analysis_id = body.speech_cleanup_analysis_id
    submitted_choice = body.speech_cleanup_choice
    if submitted_analysis_id is None or submitted_choice is None:
        # Follow-up approval after the creator already chose: the projection
        # stopped asking, so the client sends neither. Reuse the recorded one.
        reusable = reuse_recorded_cleanup_decision(
            await current_analysis_async(db, item.id, for_update=True),
            source_policy_fingerprint=(
                resolution.source.source_policy_fingerprint if resolution.source else None
            ),
            submitted_analysis_id=submitted_analysis_id,
            submitted_choice=submitted_choice,
        )
        if reusable is not None:
            log.info(
                "kria_speech_cleanup_recorded_decision_reused",
                item_id=str(item.id),
                analysis_id=str(reusable.analysis_id),
                choice=reusable.choice,
            )
            submitted_analysis_id = reusable.analysis_id
            submitted_choice = reusable.choice

    result = await evaluate_enforce_mode_decision(
        db,
        item,
        cleanup_analysis_id=submitted_analysis_id,
        cleanup_choice=submitted_choice,
        resolution=resolution,
    )
    if isinstance(result, SpeechCleanupDecisionConflict):
        await _commit_and_publish(result.refreshed_analysis_id)
        raise RuntimeFailure(
            409,
            result.code,
            SPEECH_CLEANUP_CONFLICT_COPY[result.code],
            phase="approval",
            recovery="ask_user",
            current_revision=int(thread.revision),
        )
    _clear_strategy_media_snapshot(execution)
    return _StrategyApprovalMedia(
        {
            "analysis_id": str(result.analysis_id) if result.analysis_id else None,
            "choice": result.choice,
        },
        preflight_analysis_id,
    )


async def _close_pending_approval(
    db: AsyncSession,
    *,
    thread: CreationThread,
    session: CreatorAgentSession,
    turn: CreatorAgentTurn,
    approval: CreatorAgentApproval,
    execution: CreatorAgentExecution | None,
    plan_item: Any,
    code: str,
    message: str,
    approval_status: str = "cancelled",
) -> int:
    """Close a pending approval that can no longer be decided; return the new revision.

    Commits first (so the closure persists), restores any strategy media stash, and
    promotes the queued follow-up (the reconcile sweep publishes it). Callers must
    already hold the locks in canonical order (Session -> Turn -> Approval ->
    Execution -> Thread, PlanItem ahead of Session when a stash exists).
    """
    now = datetime.now(UTC)
    approval.status = approval_status
    if execution is not None and execution.status == "awaiting_approval":
        execution.status = "stale"
        execution.error = {"code": code, "retryable": False, "recovery": "refresh_replan"}
        execution.completed_at = now
    if turn.status == "awaiting_approval":
        turn.status = "failed"
        turn.completed_at = now
        turn.error = {"code": code, "retryable": False, "recovery": "refresh_replan"}
    if session.status != "rendering":
        session.status = "awaiting_feedback"
    preflight_analysis_id = await _restore_strategy_media_snapshot_if_present(
        db, item=plan_item, execution=execution
    )
    await _append_event(
        db,
        thread,
        role="assistant",
        event_type="assistant_error",
        content=message,
        payload={
            "turn_id": str(turn.id),
            "approval_id": str(approval.id),
            "code": code,
            "recovery": "refresh_replan",
        },
    )
    await db.commit()
    revision = int(thread.revision)
    if preflight_analysis_id is not None:
        from app.services.plan_item_media import publish_preflight_after_commit  # noqa: PLC0415

        await asyncio.to_thread(publish_preflight_after_commit, preflight_analysis_id)
    await _promote_queued_successor(db, thread_id=thread.id)
    return revision


async def _cancel_pending_approval(
    db: AsyncSession,
    *,
    thread: CreationThread,
    session: CreatorAgentSession,
    turn: CreatorAgentTurn,
    approval: CreatorAgentApproval,
    execution: CreatorAgentExecution | None,
    plan_item: Any,
    code: str,
    message: str,
    approval_status: str = "cancelled",
) -> None:
    """Close a pending approval (see `_close_pending_approval`), then raise its 409."""
    revision = await _close_pending_approval(
        db,
        thread=thread,
        session=session,
        turn=turn,
        approval=approval,
        execution=execution,
        plan_item=plan_item,
        code=code,
        message=message,
        approval_status=approval_status,
    )
    raise RuntimeFailure(
        409,
        code,
        message,
        phase="approval",
        recovery="refresh_replan",
        current_revision=revision,
    )


_APPROVAL_EXPIRED_COPY = "This approval expired. Ask Kria to prepare it again."


async def _expire_blocking_approval(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    approval_id: uuid.UUID,
) -> int | None:
    """Lazily expire an overdue pending approval that blocks `submit_turn` (KRI-295).

    Returns the thread revision after expiry, or None when nothing was expired
    (decided/extended meanwhile). The caller must hold NO locks: this re-acquires
    them in canonical order (PlanItem -> Session -> Turn -> Approval -> Execution
    -> Thread), exactly like `decide_approval`. `submit_turn` therefore rolls back
    (releasing its Thread lock) before calling this, rather than locking Thread
    first and then Session/Turn, which would invert the order and can deadlock
    with a concurrent decide/cancel/sweeper.
    """
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
        return None
    try:
        execution_id = uuid.UUID(str((getattr(approval_ref, "execution_ids", None) or [None])[0]))
    except (TypeError, ValueError, IndexError):
        execution_id = None
    execution_peek = None
    if execution_id is not None:
        execution_peek = (
            await db.execute(
                select(CreatorAgentExecution).where(CreatorAgentExecution.id == execution_id)
            )
        ).scalar_one_or_none()
    has_stash = bool(
        execution_peek is not None
        and isinstance(execution_peek.result, dict)
        and "strategy_media_before" in execution_peek.result
    )
    plan_item = (
        await _lock_strategy_plan_item(
            db, session_id=approval_ref.session_id, creator_id=creator_id
        )
        if has_stash
        else None
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
    approval = (
        await db.execute(
            select(CreatorAgentApproval)
            .where(CreatorAgentApproval.id == approval_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    execution = None
    if execution_id is not None:
        execution = (
            await db.execute(
                select(CreatorAgentExecution)
                .where(CreatorAgentExecution.id == execution_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    thread = await _owned_thread(db, thread_id=thread_id, creator_id=creator_id, lock=True)
    if (
        session is None
        or turn is None
        or approval is None
        or approval.status != "pending"
        or approval.expires_at > datetime.now(UTC)
    ):
        await db.rollback()
        return None
    return await _close_pending_approval(
        db,
        thread=thread,
        session=session,
        turn=turn,
        approval=approval,
        execution=execution,
        plan_item=plan_item,
        code="approval_expired",
        message=(
            "That approval expired before it was decided, so nothing was rendered. "
            "Tell me what you want and I'll prepare it again."
        ),
        approval_status="expired",
    )


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
    # KRI-205: a strategy draft's approval is also the first (and, on a phone
    # narration source, only) chance to apply its media targets and gate on
    # the enforce-mode speech-cleanup question -- `_claim_approval_dispatch`
    # otherwise runs too late (dispatch has already 409'd the whole render).
    # That needs PlanItem/ContentPlan locked ahead of Session in canonical
    # order (Plan -> PlanItem -> Job -> Session -> Turn -> Draft -> Approval ->
    # Execution -> Thread), so peek the draft's `kind` here, unlocked -- a
    # draft's kind never changes after mint (a new direction is always a new
    # draft row), so this cannot race the authoritative re-read below.
    document_peek = (
        _parse_draft_document(
            getattr(
                (
                    await db.execute(
                        select(CreatorEditDraft).where(CreatorEditDraft.id == approval_ref.draft_id)
                    )
                ).scalar_one_or_none(),
                "snapshot_json",
                None,
            )
        )
        if decision == "approve"
        else None
    )
    is_strategy_approval = bool(
        document_peek is not None and document_peek.kind == "strategy" and document_peek.strategy
    )
    # A deny (or, further below, a lazy expiry) must reverse an already
    # -committed strategy media mutation, if this approval's render execution
    # is carrying one -- discovered the same unlocked-peek way, from the same
    # immutable `execution_ids[0]` FK. `is_strategy_approval` alone would miss
    # this: it only reflects the CURRENT decision's draft kind, not whether a
    # PRIOR attempt on this exact approval already mutated the item.
    deny_restore_pending = False
    if decision == "deny":
        try:
            execution_id_peek = uuid.UUID(
                str((getattr(approval_ref, "execution_ids", None) or [None])[0])
            )
        except (TypeError, ValueError, IndexError):
            execution_id_peek = None
        if execution_id_peek is not None:
            execution_peek = (
                await db.execute(
                    select(CreatorAgentExecution).where(
                        CreatorAgentExecution.id == execution_id_peek
                    )
                )
            ).scalar_one_or_none()
            deny_restore_pending = bool(
                execution_peek is not None
                and isinstance(execution_peek.result, dict)
                and "strategy_media_before" in execution_peek.result
            )
    needs_plan_item_lock = is_strategy_approval or deny_restore_pending
    plan_item = (
        await _lock_strategy_plan_item(
            db, session_id=approval_ref.session_id, creator_id=creator_id
        )
        if needs_plan_item_lock
        else None
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
    execution: CreatorAgentExecution | None = None
    if needs_plan_item_lock:
        try:
            execution_id = uuid.UUID(
                str((getattr(approval_ref, "execution_ids", None) or [None])[0])
            )
        except (TypeError, ValueError, IndexError):
            execution_id = None
        if execution_id is not None:
            execution = (
                await db.execute(
                    select(CreatorAgentExecution)
                    .where(CreatorAgentExecution.id == execution_id)
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
    stale_target = not session_pins_match or thread.active_creator_agent_session_id != session.id
    if stale_target and decision == "approve" and approval.status == "pending":
        # A stale approval can never render, so approving it must not leave it pending
        # (it would block every later message): cancel it cleanly, release the queued
        # follow-up, and tell the creator. Deny is unaffected (it always works).
        await _cancel_pending_approval(
            db,
            thread=thread,
            session=session,
            turn=turn,
            approval=approval,
            execution=execution,
            plan_item=plan_item,
            code="approval_target_stale",
            message=(
                "That approval was prepared for an earlier version of the video, so I "
                "cancelled it. Nothing was rendered; tell me what you want and I'll "
                "prepare it again."
            ),
        )
    if decision == "approve" and not matches_conversation_revision(
        thread, body.expected_thread_revision
    ):
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
    if decision == "approve" and approval.expires_at <= now:
        # An expired approval can never be approved: close it like a stale one
        # (restores any strategy media stash, fails the awaiting turn, promotes
        # the queued follow-up) so it cannot keep blocking the thread (KRI-295).
        await _cancel_pending_approval(
            db,
            thread=thread,
            session=session,
            turn=turn,
            approval=approval,
            execution=execution,
            plan_item=plan_item,
            code="approval_expired",
            message=_APPROVAL_EXPIRED_COPY,
            approval_status="expired",
        )
    if decision == "approve" and approval.draft_revision != body.expected_draft_revision:
        raise RuntimeFailure(
            409,
            "approval_draft_stale",
            "This approval targets a different draft revision.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )
    if decision == "approve" and (
        draft.draft_revision != body.expected_draft_revision or not draft.is_head
    ):
        raise RuntimeFailure(
            409,
            "draft_stale",
            "The draft changed after this approval was prepared.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )

    strategy_media: _StrategyApprovalMedia | None = None
    preflight_analysis_id_to_publish: uuid.UUID | None = None
    if is_strategy_approval and decision == "approve" and plan_item is not None:
        strategy_media = await _apply_strategy_approval_media(
            db,
            thread=thread,
            item=plan_item,
            strategy_payload=document_peek.strategy,
            body=body,
            execution=execution,
        )
        if strategy_media.speech_cleanup_stash is not None and execution is not None:
            execution.result = {
                **(execution.result or {}),
                "speech_cleanup": strategy_media.speech_cleanup_stash,
            }
        preflight_analysis_id_to_publish = strategy_media.preflight_analysis_id
    elif decision == "deny":
        # This deny is final for this approval (it can never be decided
        # again), so any strategy media mutation a prior approve attempt
        # already committed must be reversed now, before the item is left on
        # a direction the creator just rejected.
        preflight_analysis_id_to_publish = await _restore_strategy_media_snapshot_if_present(
            db, item=plan_item, execution=execution
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
    if preflight_analysis_id_to_publish is not None:
        from app.services.plan_item_media import publish_preflight_after_commit  # noqa: PLC0415

        await asyncio.to_thread(publish_preflight_after_commit, preflight_analysis_id_to_publish)
    # Capture before `_promote_queued_successor`: on the common "nothing
    # queued" path it calls `db.rollback()`, which expires every ORM object
    # in this session regardless of `expire_on_commit` (that flag only
    # governs post-*commit* behavior) -- reading these attributes afterward
    # would silently need a new (unawaited) round trip and crash with
    # `MissingGreenlet` in this async session.
    response = ApprovalDecisionOut(
        approval_id=str(approval.id),
        turn_id=str(approval.turn_id),
        status=approval.status,
        thread_revision=int(thread.revision),
        render_dispatched=False,
    )
    successor_id = (
        await _promote_queued_successor(db, thread_id=thread.id) if decision == "deny" else None
    )
    return response, successor_id


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


async def read_creative_brief(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
) -> CreativeBriefOut:
    """Current Creative Brief plus the newest receipt per requirement (KRI-188).

    Read-only. Empty (version 0) when the brief is off for this creator or the
    thread has none yet, so a client can call it unconditionally.
    """

    thread = await _owned_thread(
        db,
        thread_id=thread_id,
        creator_id=creator_id,
        lock=False,
        require_active=False,
    )
    empty = CreativeBriefOut(thread_id=str(thread.id), version=0)
    if not settings.creative_brief_for(creator_id):
        return empty
    brief = await load_latest_brief(db, thread.id)
    if brief is None:
        return empty
    events = (
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread.id,
                    CreationThreadEvent.role == "assistant",
                    CreationThreadEvent.payload.is_not(None),
                )
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    live = {req.id: req for req in brief.live()}
    newest: dict[str, RequirementReceipt] = {}
    for event in events:
        for raw in (event.payload or {}).get("requirement_receipts") or []:
            try:
                receipt = RequirementReceipt.model_validate(raw)
            except ValueError:
                continue
            # Older events can hold a "can't verify" receipt that judged nothing;
            # it is never a requirement's outcome, so the requirement stays open.
            rid = receipt.requirement_id
            if rid not in newest and is_judged(live.get(rid), receipt):
                newest[rid] = receipt
    receipts = list(newest.values())
    shown = apply_receipt_statuses(brief, [r.model_dump(mode="json") for r in receipts])
    return CreativeBriefOut(
        thread_id=str(thread.id),
        version=shown.version,
        requirements=[
            CreativeBriefRequirementOut(
                id=req.id,
                kind=req.kind,
                scope=req.scope,
                literal=req.literal,
                description=req.description,
                status=req.status,
            )
            for req in shown.live()
        ],
        requirement_receipts=receipts,
    )
