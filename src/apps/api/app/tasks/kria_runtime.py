"""Durable runtime-v2 planning, editing, approval, and observation tasks.

Every consequential render remains separated from draft creation by a pinned,
persisted approval. Workers report only durable receipts and observed Job truth.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.contracts import KriaObservedTurnResponse, KriaToolReceipt, KriaTurnPlan
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.language import is_paraphrase_only
from app.kria.planner import PlannedKriaTurn, plan_live_turn
from app.kria.registry import KRIA_TOOLS
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    MusicTrack,
    PlanItem,
)
from app.routes.generative_jobs import (
    EditorCommitRequest,
    dispatch_apply_speech_cut_candidate,
    enqueue_editor_commit_render,
    prepare_editor_commit,
)
from app.services.kria_editor_ops import (
    compile_editor_ops,
    merge_editor_draft,
    project_editor_draft,
)
from app.worker import celery_app

log = structlog.get_logger()
_REPUBLISH_BACKOFF = timedelta(minutes=1)
_APPROVAL_TTL = timedelta(minutes=30)
_LEASE_HEARTBEAT_SECONDS = 5
_DRAFT_BODY_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class _Completion:
    committed: bool
    successor_turn_id: str | None = None
    requeue_turn_id: str | None = None


@dataclass(frozen=True)
class _ApprovalDispatchClaim:
    approval_id: uuid.UUID
    execution_id: uuid.UUID
    thread_id: uuid.UUID
    turn_id: uuid.UUID
    session_id: uuid.UUID
    item_id: uuid.UUID
    ownership_epoch: int
    draft_kind: str
    strategy: dict[str, Any] | None
    editor_prep: dict[str, Any] | None
    target_job_id: uuid.UUID | None
    target_variant_id: str | None
    target_generation_id: str | None
    creator_request: str
    preflight_analysis_id: uuid.UUID | None = None


def _snapshot(thread: CreationThread) -> dict[str, Any]:
    state = dict(thread.state or {})
    media = state.get("media") or []
    labels = [
        str(item.get("filename") or item.get("media_id") or "footage")
        for item in media
        if isinstance(item, dict)
    ]
    return {
        "thread_id": str(getattr(thread, "id", "")),
        "creator_id": str(getattr(thread, "creator_id", "")),
        "item_id": (
            str(getattr(thread, "active_plan_item_id"))
            if getattr(thread, "active_plan_item_id", None)
            else None
        ),
        "media_labels": labels,
        "edit_format": state.get("edit_format") or state.get("format") or "montage",
        "strongest_moment": state.get("strongest_moment"),
        "editorial_decision": state.get("editorial_decision"),
    }


def _complete_response_turn(
    turn_id: uuid.UUID,
    *,
    lease_owner: str,
    lease_epoch: int,
    claimed_thread_revision: int,
    plan: KriaTurnPlan,
) -> _Completion:
    with sync_session() as db:
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).with_for_update()
        ).scalar_one_or_none()
        database_now = db.execute(select(func.now())).scalar_one()
        if turn is None or not _owns_turn_lease(
            turn,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            database_now=database_now,
        ):
            return _Completion(committed=False)
        thread = db.execute(
            select(CreationThread).where(CreationThread.id == turn.thread_id).with_for_update()
        ).scalar_one()
        if int(thread.revision) != claimed_thread_revision:
            turn.status = "pending"
            turn.lease_owner = None
            turn.lease_expires_at = None
            db.commit()
            return _Completion(committed=False, requeue_turn_id=str(turn.id))
        event = _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content=plan.response,
            payload={
                "turn_id": str(turn.id),
                "turn_value": plan.turn_value,
                "receipt_ids": [],
                "next_actions": [],
                "schema_version": plan.schema_version,
            },
        )
        turn.plan_json = plan.model_dump(mode="json")
        turn.observed_event_id = event.id
        turn.status = "completed"
        turn.completed_at = datetime.now(UTC)
        turn.lease_owner = None
        turn.lease_expires_at = None
        db.commit()
    return _Completion(
        committed=True,
        successor_turn_id=_promote_queued_successor_sync(turn.thread_id),
    )


def _strategy_changes(arguments: Any) -> list[str]:
    strategy = arguments.strategy
    values = [
        arguments.summary,
        f"{strategy.pacing.replace('_', ' ').title()} pacing",
        f"{strategy.edit_format.replace('_', ' ').title()} format",
    ]
    return list(dict.fromkeys(value for value in values if value))[:3]


def _validate_draft_plan(plan: KriaTurnPlan) -> None:
    """Allow one atomic draft, optionally followed by its exact render approval."""
    if plan.mode != "act" or len(plan.intents) not in {1, 2}:
        raise RuntimeError("Kria produced an unsupported tool group")
    apply_intent = plan.intents[0]
    if apply_intent.tool_name not in {"draft.apply_strategy", "draft.apply_editor_ops"}:
        raise RuntimeError("Kria produced an unsupported draft tool")
    if apply_intent.depends_on:
        raise RuntimeError("The draft tool cannot depend on an unexecuted intent")
    apply_tool = KRIA_TOOLS.get(apply_intent.tool_name, apply_intent.tool_version)
    if apply_tool.definition.risk != "reversible_draft":
        raise RuntimeError("Kria draft tool risk changed")
    if len(plan.intents) == 1:
        return
    render_intent = plan.intents[1]
    if render_intent.tool_name != "render.request" or render_intent.depends_on != [
        apply_intent.intent_id
    ]:
        raise RuntimeError("Render approval must depend on the exact draft bundle")
    render_tool = KRIA_TOOLS.get(render_intent.tool_name, render_intent.tool_version)
    if render_tool.definition.risk != "approval_required":
        raise RuntimeError("Kria render tool risk changed")


def _useful_plan(planned: PlannedKriaTurn, *, user_message: str) -> PlannedKriaTurn:
    """Fail closed on live acknowledgement-shaped copy before it reaches the ledger."""

    plan = planned.plan
    if plan.mode == "respond":
        if not is_paraphrase_only(user_message=user_message, assistant_message=plan.response or ""):
            return planned
        replacement = plan.model_copy(
            update={
                "turn_value": "question",
                "response": (
                    "I need one concrete creative choice before I can make a useful edit decision. "
                    "Which moment should viewers remember?"
                ),
            }
        )
        return PlannedKriaTurn(replacement, planned.manifest_hash, planned.context_hash)

    intents = []
    changed = False
    for intent in plan.intents:
        arguments = dict(intent.arguments)
        summary = arguments.get("summary")
        if isinstance(summary, str) and is_paraphrase_only(
            user_message=user_message,
            assistant_message=summary,
        ):
            if intent.tool_name == "draft.apply_editor_ops":
                count = len(arguments.get("operations") or [])
                suffix = "s" if count != 1 else ""
                arguments["summary"] = f"I prepared {count} reversible editor change{suffix}."
            else:
                strategy = arguments.get("strategy") or {}
                pacing = str(strategy.get("pacing") or "focused").replace("_", " ")
                edit_format = str(strategy.get("edit_format") or "video").replace("_", " ")
                arguments["summary"] = (
                    f"I prepared a {pacing} {edit_format} draft around the available footage."
                )
            changed = True
        intents.append(intent.model_copy(update={"arguments": arguments}))
    if not changed:
        return planned
    return PlannedKriaTurn(
        plan.model_copy(update={"intents": intents}),
        planned.manifest_hash,
        planned.context_hash,
    )


def _complete_draft_turn(
    turn_id: uuid.UUID,
    *,
    lease_owner: str,
    lease_epoch: int,
    claimed_thread_revision: int,
    planned: PlannedKriaTurn,
) -> _Completion:
    plan = planned.plan
    _validate_draft_plan(plan)
    apply_intent = plan.intents[0]
    render_intent = plan.intents[1] if len(plan.intents) == 2 else None
    apply_tool = KRIA_TOOLS.get(apply_intent.tool_name, apply_intent.tool_version)
    arguments = apply_tool.arguments_model.model_validate(apply_intent.arguments)
    if render_intent is not None:
        render_tool = KRIA_TOOLS.get(render_intent.tool_name, render_intent.tool_version)
        render_tool.arguments_model.model_validate(render_intent.arguments)
    document: KriaDraftDocument | None = None
    changes: list[str] = []
    snapshot: dict[str, Any] = {}
    snapshot_hash = ""
    if apply_intent.tool_name == "draft.apply_strategy":
        changes = _strategy_changes(arguments)
        document = KriaDraftDocument(
            kind="strategy",
            intent=arguments.summary,
            edit_format=arguments.strategy.edit_format,
            strategy=arguments.strategy.model_dump(mode="json", exclude_none=True),
            changes=changes,
        )
        snapshot, snapshot_hash = canonical_snapshot(document)

    with sync_session() as db:
        turn_ref = db.get(CreatorAgentTurn, turn_id)
        if turn_ref is None or turn_ref.session_id is None:
            return _Completion(committed=False)
        thread_ref = db.get(CreationThread, turn_ref.thread_id)
        if thread_ref is None or thread_ref.active_plan_item_id is None:
            return _Completion(committed=False)

        # Global write order: Plan -> PlanItem -> Job -> Session -> Turn ->
        # Draft -> Approval -> Thread.  No lock was held during the model call.
        plan_row = db.execute(
            select(ContentPlan)
            .join(PlanItem, PlanItem.content_plan_id == ContentPlan.id)
            .where(
                PlanItem.id == thread_ref.active_plan_item_id,
                ContentPlan.user_id == thread_ref.creator_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        item = db.execute(
            select(PlanItem).where(PlanItem.id == thread_ref.active_plan_item_id).with_for_update()
        ).scalar_one_or_none()
        if plan_row is None or item is None:
            return _Completion(committed=False)
        job = (
            db.execute(
                select(Job).where(Job.id == item.current_job_id).with_for_update()
            ).scalar_one_or_none()
            if item.current_job_id is not None
            else None
        )
        session = db.execute(
            select(CreatorAgentSession)
            .where(CreatorAgentSession.id == turn_ref.session_id)
            .with_for_update()
        ).scalar_one_or_none()
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).with_for_update()
        ).scalar_one_or_none()
        database_now = db.execute(select(func.now())).scalar_one()
        if (
            session is None
            or turn is None
            or not _owns_turn_lease(
                turn,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                database_now=database_now,
            )
        ):
            return _Completion(committed=False)
        thread = db.execute(
            select(CreationThread).where(CreationThread.id == turn.thread_id).with_for_update()
        ).scalar_one()
        if int(thread.revision) != claimed_thread_revision:
            turn.status = "pending"
            turn.lease_owner = None
            turn.lease_expires_at = None
            db.commit()
            return _Completion(committed=False, requeue_turn_id=str(turn.id))

        variant_key = str(session.target_variant_id or "initial")
        generation_id = str(session.target_generation_id or "") or None
        head = db.execute(
            select(CreatorEditDraft)
            .where(
                CreatorEditDraft.item_id == item.id,
                CreatorEditDraft.variant_key == variant_key,
                CreatorEditDraft.is_head.is_(True),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if apply_intent.tool_name == "draft.apply_editor_ops":
            variant = (
                next(
                    (
                        row
                        for row in (job.assembly_plan or {}).get("variants") or []
                        if isinstance(row, dict) and str(row.get("variant_id") or "") == variant_key
                    ),
                    None,
                )
                if job is not None
                else None
            )
            if variant is None:
                raise RuntimeError("The exact editor target is no longer available")
            prior_payload = (
                (head.snapshot_json or {}).get("editor_payload") or {}
                if head is not None
                and head.base_job_id == job.id
                and head.base_generation_id == generation_id
                and (head.snapshot_json or {}).get("kind") == "editor"
                else {}
            )
            if prior_payload and any(
                op.get("op") == "apply_speech_cut_candidate" for op in arguments.operations
            ):
                raise RuntimeError("Save the current draft before applying speech processing")
            compiled = compile_editor_ops(
                job, project_editor_draft(variant, prior_payload), arguments.operations
            )
            changes = compiled.changes
            document = KriaDraftDocument(
                kind="editor",
                intent=arguments.summary,
                edit_format=str(item.edit_format or "montage"),
                editor_payload=(
                    merge_editor_draft(
                        prior_payload, compiled.payload.model_dump(mode="json", exclude_none=True)
                    )
                    if isinstance(compiled.payload, EditorCommitRequest)
                    else compiled.payload
                ),
                changes=changes,
            )
            snapshot, snapshot_hash = canonical_snapshot(document)
        if document is None:
            raise RuntimeError("Kria produced an unsupported draft tool")
        next_revision = (
            int(
                db.execute(
                    select(func.coalesce(func.max(CreatorEditDraft.draft_revision), -1)).where(
                        CreatorEditDraft.item_id == item.id,
                        CreatorEditDraft.variant_key == variant_key,
                    )
                ).scalar_one()
            )
            + 1
        )
        draft_execution = CreatorAgentExecution(
            session_id=session.id,
            turn_id=turn.id,
            idempotency_key=f"kria:{turn.id}:{apply_intent.intent_id}",
            request_digest=turn.request_digest,
            expected_revision=int(session.revision),
            expected_manifest_hash=planned.manifest_hash,
            tool_name=apply_intent.tool_name,
            tool_version=apply_intent.tool_version,
            risk="reversible_draft",
            dependency_group=0,
            group_order=0,
            target_thread_id=thread.id,
            status="completed",
            result={"snapshot_hash": snapshot_hash, "changes": changes},
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        db.add(draft_execution)
        db.flush()
        if head is not None:
            head.is_head = False
        draft = CreatorEditDraft(
            creator_id=thread.creator_id,
            thread_id=thread.id,
            item_id=item.id,
            variant_key=variant_key,
            base_job_id=job.id if job is not None else None,
            base_generation_id=generation_id,
            draft_revision=next_revision,
            parent_draft_id=head.id if head is not None else None,
            snapshot_json=snapshot,
            snapshot_hash=snapshot_hash,
            source_execution_id=draft_execution.id,
            is_head=True,
        )
        db.add(draft)
        db.flush()
        draft_execution.target_draft_id = draft.id
        draft_execution.target_draft_revision = draft.draft_revision
        draft_execution.result = {
            **(draft_execution.result or {}),
            "draft_id": str(draft.id),
            "draft_revision": draft.draft_revision,
        }

        if render_intent is None:
            event = _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="draft_applied",
                content=arguments.summary,
                payload={
                    "turn_id": str(turn.id),
                    "draft_id": str(draft.id),
                    "draft_revision": draft.draft_revision,
                    "snapshot_hash": draft.snapshot_hash,
                    "changes": changes,
                    "can_undo": head is not None,
                    "receipt_ids": [str(draft_execution.id)],
                    "render_requested": False,
                },
            )
            turn.plan_json = plan.model_dump(mode="json")
            turn.observed_event_id = event.id
            turn.status = "completed"
            turn.completed_at = datetime.now(UTC)
            turn.lease_owner = None
            turn.lease_expires_at = None
            thread_id = thread.id
            db.commit()
            return _Completion(
                committed=True, successor_turn_id=_promote_queued_successor_sync(thread_id)
            )

        render_execution = CreatorAgentExecution(
            session_id=session.id,
            turn_id=turn.id,
            idempotency_key=f"kria:{turn.id}:{render_intent.intent_id}",
            request_digest=turn.request_digest,
            expected_revision=int(session.revision),
            expected_manifest_hash=planned.manifest_hash,
            tool_name=render_intent.tool_name,
            tool_version=render_intent.tool_version,
            risk="approval_required",
            dependency_group=1,
            group_order=1,
            target_thread_id=thread.id,
            target_draft_id=draft.id,
            target_draft_revision=draft.draft_revision,
            target_job_id=job.id if job is not None else None,
            target_variant_id=session.target_variant_id,
            target_generation_id=generation_id,
            target_manifest_hash=planned.manifest_hash,
            target_ownership_epoch=int(session.ownership_epoch),
            status="awaiting_approval",
            result={"consequence": "Start one render from this exact draft."},
            started_at=datetime.now(UTC),
            awaiting_approval_at=datetime.now(UTC),
        )
        db.add(render_execution)
        db.flush()
        approval = CreatorAgentApproval(
            creator_id=thread.creator_id,
            thread_id=thread.id,
            session_id=session.id,
            turn_id=turn.id,
            draft_id=draft.id,
            draft_revision=draft.draft_revision,
            target_job_id=job.id if job is not None else None,
            target_variant_id=session.target_variant_id,
            target_generation_id=generation_id,
            target_manifest_hash=planned.manifest_hash,
            target_ownership_epoch=int(session.ownership_epoch),
            execution_ids=[str(render_execution.id)],
            consequence_summary=f"Render this draft: {arguments.summary}",
            cost_summary="One render",
            status="pending",
            expires_at=datetime.now(UTC) + _APPROVAL_TTL,
        )
        db.add(approval)
        db.flush()
        render_execution.result = {
            **(render_execution.result or {}),
            "approval_id": str(approval.id),
        }
        session.manifest_hash = planned.manifest_hash
        session.active_plan = {
            "runtime_version": 2,
            "draft_kind": document.kind,
            "strategy": document.strategy,
            "editor_payload": document.editor_payload,
            "summary": arguments.summary,
            "context_hash": planned.context_hash,
        }
        draft_event = _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="draft_applied",
            content=arguments.summary,
            payload={
                "turn_id": str(turn.id),
                "draft_id": str(draft.id),
                "draft_revision": draft.draft_revision,
                "snapshot_hash": draft.snapshot_hash,
                "changes": changes,
                "can_undo": head is not None,
                "receipt_ids": [str(draft_execution.id)],
            },
        )
        _append_sync_event(
            db,
            thread,
            role="system",
            event_type="approval_requested",
            content=None,
            payload={
                "turn_id": str(turn.id),
                "approval_id": str(approval.id),
                "draft_id": str(draft.id),
                "draft_revision": draft.draft_revision,
                "consequence_summary": approval.consequence_summary,
                "cost_summary": approval.cost_summary,
                "expires_at": approval.expires_at.isoformat(),
                "artifact_key": f"approval:{approval.id}",
            },
        )
        turn.plan_json = plan.model_dump(mode="json")
        turn.observed_event_id = draft_event.id
        turn.status = "awaiting_approval"
        turn.lease_owner = None
        turn.lease_expires_at = None
        db.commit()
        return _Completion(committed=True)


def _renew_turn_lease(turn_id: uuid.UUID, *, lease_owner: str, lease_epoch: int) -> bool:
    """Renew only the exact live epoch using database time as the authority."""

    with sync_session() as db:
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).with_for_update()
        ).scalar_one_or_none()
        database_now = db.execute(select(func.now())).scalar_one()
        if turn is None or not _owns_turn_lease(
            turn,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            database_now=database_now,
        ):
            db.rollback()
            return False
        turn.lease_expires_at = database_now + timedelta(seconds=settings.kria_turn_lease_seconds)
        db.commit()
        return True


async def _plan_with_live_agent(
    snapshot: dict[str, Any],
    user_message: str,
    *,
    turn_id: uuid.UUID,
    lease_owner: str,
    lease_epoch: int,
) -> PlannedKriaTurn:
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=_LEASE_HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                alive = await asyncio.to_thread(
                    _renew_turn_lease,
                    turn_id,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                )
                if not alive:
                    return

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        async with AsyncSessionLocal() as db:
            return await plan_live_turn(
                db,
                thread_id=uuid.UUID(str(snapshot["thread_id"])),
                item_id=uuid.UUID(str(snapshot["item_id"])),
                creator_id=uuid.UUID(str(snapshot["creator_id"])),
                user_message=user_message,
            )
    finally:
        stop.set()
        await heartbeat


def _append_sync_event(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    thread: CreationThread,
    *,
    role: str,
    event_type: str,
    content: str | None,
    payload: dict[str, Any],
) -> CreationThreadEvent:
    sequence = (
        int(
            db.execute(
                select(func.coalesce(func.max(CreationThreadEvent.sequence), -1)).where(
                    CreationThreadEvent.thread_id == thread.id
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
        role=role,
        event_type=event_type,
        content=content,
        payload=payload,
    )
    db.add(event)
    db.flush()
    return event


def _owns_turn_lease(
    turn: CreatorAgentTurn,
    *,
    lease_owner: str,
    lease_epoch: int,
    database_now: datetime,
) -> bool:
    return (
        turn.status == "planning"
        and turn.lease_owner == lease_owner
        and int(turn.lease_epoch) == lease_epoch
        and turn.lease_expires_at is not None
        and turn.lease_expires_at > database_now
    )


def _claim(turn_id: uuid.UUID, lease_owner: str) -> tuple[dict[str, Any], str, int, int] | None:
    """Claim briefly; no lock survives tool/model/storage/broker work."""

    if not settings.kria_runtime_v2_enabled:
        return None
    with sync_session() as db:
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).with_for_update()
        ).scalar_one_or_none()
        database_now = db.execute(select(func.now())).scalar_one()
        expired_read_lease = (
            turn is not None
            and turn.status == "planning"
            and turn.lease_expires_at is not None
            and turn.lease_expires_at <= database_now
        )
        if turn is None or (turn.status != "pending" and not expired_read_lease):
            return None
        if turn.cancel_requested_at is not None:
            turn.status = "cancelled"
            turn.completed_at = datetime.now(UTC)
            db.commit()
            return None
        turn.status = "planning"
        turn.lease_owner = lease_owner
        turn.lease_epoch = int(turn.lease_epoch or 0) + 1
        turn.lease_expires_at = database_now + timedelta(seconds=settings.kria_turn_lease_seconds)
        db.commit()
        thread = db.get(CreationThread, turn.thread_id)
        source = db.get(CreationThreadEvent, turn.source_event_id)
        if thread is None or source is None:
            return None
        return (
            _snapshot(thread),
            str(source.content or ""),
            int(turn.lease_epoch),
            int(thread.revision),
        )


def _complete_read_turn(
    turn_id: uuid.UUID,
    *,
    lease_owner: str,
    lease_epoch: int,
    claimed_thread_revision: int,
    plan: KriaTurnPlan,
    receipt: KriaToolReceipt,
    response: KriaObservedTurnResponse,
) -> _Completion:
    with sync_session() as db:
        # Turn precedes Thread in the v2 global lock order. The external tool
        # work already completed, so this is a short projection transaction.
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).with_for_update()
        ).scalar_one_or_none()
        database_now = db.execute(select(func.now())).scalar_one()
        if turn is None or not _owns_turn_lease(
            turn,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            database_now=database_now,
        ):
            return _Completion(committed=False)
        if turn.cancel_requested_at is not None:
            turn.status = "cancelled"
            turn.completed_at = datetime.now(UTC)
            db.commit()
            return _Completion(committed=False)

        if turn.session_id is None:
            raise RuntimeError("runtime-v2 turn is missing its durable receipt session")
        session = db.get(CreatorAgentSession, turn.session_id)
        if session is None:
            raise RuntimeError("runtime-v2 receipt session is unavailable")
        key = f"kria:{turn.id}:{receipt.intent_id}"
        execution = db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.session_id == session.id,
                CreatorAgentExecution.idempotency_key == key,
            )
        ).scalar_one_or_none()
        created_execution = execution is None
        if created_execution:
            execution = CreatorAgentExecution(
                session_id=session.id,
                turn_id=turn.id,
                idempotency_key=key,
                request_digest=turn.request_digest,
                expected_revision=int(session.revision),
                expected_manifest_hash=session.manifest_hash,
                status="completed",
                tool_name=receipt.tool_name,
                tool_version=receipt.tool_version,
                risk="read",
                dependency_group=0,
                group_order=0,
                target_thread_id=turn.thread_id,
                result=receipt.result,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            db.add(execution)
            db.flush()
        receipt_ids = [str(execution.id)]

        # Execution precedes Thread in the global lock order. Only after the
        # receipt is settled do we lock the transcript projection.
        thread = db.execute(
            select(CreationThread).where(CreationThread.id == turn.thread_id).with_for_update()
        ).scalar_one()
        if int(thread.revision) != claimed_thread_revision:
            # The receipt describes a superseded snapshot. Remove its
            # uncommitted row, release the lease, and republish this durable
            # turn so the next claim observes the new project revision.
            if created_execution:
                db.delete(execution)
            turn.status = "pending"
            turn.lease_owner = None
            turn.lease_expires_at = None
            db.commit()
            return _Completion(committed=False, requeue_turn_id=str(turn.id))

        observed = response.model_copy(update={"receipt_ids": receipt_ids})
        event = _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content=observed.message,
            payload={
                "turn_id": str(turn.id),
                "turn_value": observed.turn_value,
                "receipt_ids": observed.receipt_ids,
                "next_actions": observed.next_actions,
                "schema_version": observed.schema_version,
            },
        )
        turn.plan_json = plan.model_dump(mode="json")
        turn.observed_event_id = event.id
        turn.status = "completed"
        turn.completed_at = datetime.now(UTC)
        turn.lease_owner = None
        turn.lease_expires_at = None
        db.commit()

    # Never lock queued Turn B while holding Thread: cancellation locks Turn B
    # before Thread. Promotion is an independent, turn-only transaction.
    successor_turn_id = _promote_queued_successor_sync(turn.thread_id)
    return _Completion(committed=True, successor_turn_id=successor_turn_id)


def _promote_queued_successor_sync(thread_id: uuid.UUID) -> str | None:
    with sync_session() as db:
        successor = (
            db.execute(
                select(CreatorAgentTurn)
                .where(
                    CreatorAgentTurn.thread_id == thread_id,
                    CreatorAgentTurn.status == "queued",
                )
                .order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
                .with_for_update()
            )
            .scalars()
            .first()
        )
        if successor is not None:
            # Turn -> Thread serializes this slot promotion with turn submit.
            db.execute(
                select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
            )
            successor.status = "pending"
        db.commit()
        return str(successor.id) if successor is not None else None


def _fail_turn(
    turn_id: uuid.UUID,
    *,
    code: str,
    lease_owner: str,
    lease_epoch: int,
) -> str | None:
    try:
        with sync_session() as db:
            turn = db.execute(
                select(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).with_for_update()
            ).scalar_one_or_none()
            database_now = db.execute(select(func.now())).scalar_one()
            if turn is None or not _owns_turn_lease(
                turn,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                database_now=database_now,
            ):
                return None
            thread_id = turn.thread_id
            thread = db.execute(
                select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
            ).scalar_one_or_none()
            if thread is None:
                return None
            turn.status = "failed"
            turn.error = {"code": code, "retryable": True, "recovery": "retry"}
            turn.completed_at = datetime.now(UTC)
            turn.lease_owner = None
            turn.lease_expires_at = None
            event = _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_error",
                content=(
                    "I couldn't finish that step, but your project and saved draft are safe. "
                    "Try the request again."
                ),
                payload={
                    "turn_id": str(turn.id),
                    "code": code,
                    "retryable": True,
                    "recovery": "retry",
                    # Planning failed before a tool execution receipt existed.
                    # Keep this empty rather than inventing a receipt identity.
                    "receipt_ids": [],
                },
            )
            turn.observed_event_id = event.id
            db.commit()
        return _promote_queued_successor_sync(thread_id)
    except Exception:  # noqa: BLE001 - preserve the original task exception
        log.exception("kria_turn_failure_projection_failed", turn_id=str(turn_id))
        return None


@celery_app.task(
    bind=True,
    name="tasks.run_kria_turn",
    soft_time_limit=90,
    time_limit=120,
    max_retries=0,
)
def run_kria_turn(self, turn_id: str) -> dict[str, str]:  # noqa: ANN001
    """Plan and execute one durable turn without holding locks across inference."""

    identifier = uuid.UUID(turn_id)
    lease_owner = str(self.request.id or identifier)
    claimed = _claim(identifier, lease_owner)
    if claimed is None:
        return {"turn_id": turn_id, "status": "ignored"}
    snapshot, user_message, lease_epoch, claimed_thread_revision = claimed
    try:
        if settings.main_creator_agent_enabled and snapshot.get("item_id"):
            planned = asyncio.run(
                _plan_with_live_agent(
                    snapshot,
                    user_message,
                    turn_id=identifier,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                )
            )
            planned = _useful_plan(planned, user_message=user_message)
            if planned.plan.mode == "respond":
                completion = _complete_response_turn(
                    identifier,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                    claimed_thread_revision=claimed_thread_revision,
                    plan=planned.plan,
                )
            else:
                completion = _complete_draft_turn(
                    identifier,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                    claimed_thread_revision=claimed_thread_revision,
                    planned=planned,
                )
            if not completion.committed:
                if completion.requeue_turn_id is not None:
                    run_kria_turn.apply_async(
                        args=[completion.requeue_turn_id],
                        task_id=completion.requeue_turn_id,
                        queue="agent-control",
                    )
                    return {"turn_id": turn_id, "status": "requeued"}
                return {"turn_id": turn_id, "status": "ignored"}
            if completion.successor_turn_id is not None:
                run_kria_turn.apply_async(
                    args=[completion.successor_turn_id],
                    task_id=completion.successor_turn_id,
                    queue="agent-control",
                )
            return {
                "turn_id": turn_id,
                "status": (
                    "awaiting_approval"
                    if any(intent.tool_name == "render.request" for intent in planned.plan.intents)
                    else "completed"
                ),
            }

        planned_turn_value = "question" if not snapshot.get("media_labels") else "decision"
        plan = KriaTurnPlan.model_validate(
            {
                "schema_version": 2,
                "mode": "act",
                "turn_value": planned_turn_value,
                "evidence_ids": ["trusted-project-snapshot"],
                "intents": [
                    {
                        "intent_id": "inspect-project",
                        "tool_name": "project.inspect",
                        "tool_version": 1,
                    }
                ],
            }
        )
        intent = plan.intents[0]
        tool = KRIA_TOOLS.get(intent.tool_name, intent.tool_version)
        arguments = tool.arguments_model.model_validate(intent.arguments)
        result = tool.result_model.model_validate(tool.handler(arguments, snapshot))
        result_json = result.model_dump(mode="json")
        receipt = KriaToolReceipt(
            intent_id=intent.intent_id,
            tool_name=intent.tool_name,
            tool_version=intent.tool_version,
            status="completed",
            result=result_json,
        )
        message = str(result_json["editorial_decision"])
        if is_paraphrase_only(user_message=user_message, assistant_message=message):
            message = "I inspected the project, but I need footage evidence before editing."
        response = KriaObservedTurnResponse(
            turn_value="question" if result_json["next_action"] == "attach_media" else "decision",
            message=message,
            receipt_ids=[intent.intent_id],
            next_actions=[str(result_json["next_action"])],
        )
        completion = _complete_read_turn(
            identifier,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            claimed_thread_revision=claimed_thread_revision,
            plan=plan,
            receipt=receipt,
            response=response,
        )
        if not completion.committed:
            if completion.requeue_turn_id is not None:
                run_kria_turn.apply_async(
                    args=[completion.requeue_turn_id],
                    task_id=completion.requeue_turn_id,
                    queue="agent-control",
                )
                return {"turn_id": turn_id, "status": "requeued"}
            return {"turn_id": turn_id, "status": "ignored"}
        if completion.successor_turn_id is not None:
            try:
                run_kria_turn.apply_async(
                    args=[completion.successor_turn_id],
                    task_id=completion.successor_turn_id,
                    queue="agent-control",
                )
            except Exception as exc:  # noqa: BLE001 - reconciler republishes pending row
                log.error(
                    "kria_successor_publish_failed",
                    turn_id=completion.successor_turn_id,
                    error_class=type(exc).__name__,
                )
        return {"turn_id": turn_id, "status": "completed"}
    except Exception:  # noqa: BLE001 - failure is projected before Celery records it
        successor_turn_id = _fail_turn(
            identifier,
            code="runtime_turn_failed",
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
        )
        if successor_turn_id is not None:
            run_kria_turn.apply_async(
                args=[successor_turn_id],
                task_id=successor_turn_id,
                queue="agent-control",
            )
        raise


def _claim_approval_dispatch(approval_id: uuid.UUID) -> _ApprovalDispatchClaim | None:
    """Consume exact consent into a durable accepted execution.

    This transaction performs no broker or renderer work. A crash after commit
    leaves ``accepted`` as the recovery ledger for the minute reconciler.
    """

    with sync_session() as db:
        approval_ref = db.get(CreatorAgentApproval, approval_id)
        if approval_ref is None or not approval_ref.execution_ids:
            return None
        try:
            execution_id = uuid.UUID(str(approval_ref.execution_ids[0]))
        except (TypeError, ValueError):
            return None
        turn_ref = db.get(CreatorAgentTurn, approval_ref.turn_id)
        session_ref = db.get(CreatorAgentSession, approval_ref.session_id)
        draft_ref = db.get(CreatorEditDraft, approval_ref.draft_id)
        thread_ref = db.get(CreationThread, approval_ref.thread_id)
        if any(row is None for row in (turn_ref, session_ref, draft_ref, thread_ref)):
            return None
        item_ref = db.get(PlanItem, session_ref.plan_item_id)
        if item_ref is None:
            return None

        # Canonical lock order -- app/db_locks.CANONICAL_LOCK_ORDER is the single
        # source of truth and tests/routes/test_lock_order.py enforces it:
        # Plan -> PlanItem -> Job -> Session -> Turn -> Draft -> Approval ->
        # Execution -> Thread. No external work is done while these are held.
        plan = db.execute(
            select(ContentPlan)
            .where(
                ContentPlan.id == item_ref.content_plan_id,
                ContentPlan.user_id == approval_ref.creator_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        item = db.execute(
            select(PlanItem).where(PlanItem.id == item_ref.id).with_for_update()
        ).scalar_one_or_none()
        current_job = (
            db.execute(
                select(Job).where(Job.id == item.current_job_id).with_for_update()
            ).scalar_one_or_none()
            if item is not None and item.current_job_id is not None
            else None
        )
        session = db.execute(
            select(CreatorAgentSession)
            .where(CreatorAgentSession.id == approval_ref.session_id)
            .with_for_update()
        ).scalar_one_or_none()
        turn = db.execute(
            select(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == approval_ref.turn_id)
            .with_for_update()
        ).scalar_one_or_none()
        draft = db.execute(
            select(CreatorEditDraft)
            .where(CreatorEditDraft.id == approval_ref.draft_id)
            .with_for_update()
        ).scalar_one_or_none()
        approval = db.execute(
            select(CreatorAgentApproval)
            .where(CreatorAgentApproval.id == approval_id)
            .with_for_update()
        ).scalar_one_or_none()
        execution = db.execute(
            select(CreatorAgentExecution)
            .where(CreatorAgentExecution.id == execution_id)
            .with_for_update()
        ).scalar_one_or_none()
        thread = db.execute(
            select(CreationThread)
            .where(CreationThread.id == approval_ref.thread_id)
            .with_for_update()
        ).scalar_one_or_none()
        rows = (plan, item, session, turn, draft, approval, execution, thread)
        if any(row is None for row in rows):
            return None
        if execution.status in {"dispatched", "completed", "succeeded"}:
            return None
        if approval.status not in {"approved", "consumed"} or execution.status not in {
            "awaiting_approval",
            "accepted",
        }:
            return None

        current_job_id = current_job.id if current_job is not None else None
        target_valid = (
            int(thread.runtime_version) == 2
            and thread.active_creator_agent_session_id == session.id
            and session.plan_item_id == item.id
            and int(plan.ownership_epoch or 0) == int(approval.target_ownership_epoch)
            and int(session.ownership_epoch) == int(approval.target_ownership_epoch)
            and approval.draft_id == draft.id
            and approval.draft_revision == draft.draft_revision
            and draft.is_head
            and draft.snapshot_json is not None
            and execution.target_draft_id == draft.id
            and execution.target_draft_revision == draft.draft_revision
            and approval.target_job_id == current_job_id
            and approval.target_job_id == session.target_job_id
            and approval.target_variant_id == session.target_variant_id
            and approval.target_variant_id == execution.target_variant_id
            and approval.target_manifest_hash == session.manifest_hash
        )
        document: KriaDraftDocument | None = None
        if target_valid:
            try:
                document = KriaDraftDocument.model_validate(draft.snapshot_json)
            except ValueError:
                target_valid = False
        if document is None or (
            (document.kind == "strategy" and document.strategy is None)
            or (document.kind == "editor" and document.editor_payload is None)
            or document.kind not in {"strategy", "editor"}
        ):
            target_valid = False

        if (
            target_valid
            and execution.status == "accepted"
            and document is not None
            and document.kind == "editor"
            and current_job is not None
        ):
            current_variant = next(
                (
                    row
                    for row in (current_job.assembly_plan or {}).get("variants") or []
                    if isinstance(row, dict)
                    and row.get("variant_id") == execution.target_variant_id
                ),
                None,
            )
            persisted_prep = (execution.result or {}).get("editor_prep")
            if (
                isinstance(current_variant, dict)
                and current_variant.get("render_generation_id") == execution.target_generation_id
                and isinstance(persisted_prep, dict)
            ):
                return _ApprovalDispatchClaim(
                    approval_id=approval.id,
                    execution_id=execution.id,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    session_id=session.id,
                    item_id=item.id,
                    ownership_epoch=int(session.ownership_epoch),
                    draft_kind="editor",
                    strategy=None,
                    editor_prep=persisted_prep,
                    target_job_id=current_job.id,
                    target_variant_id=execution.target_variant_id,
                    target_generation_id=execution.target_generation_id,
                    creator_request=document.intent,
                )
            target_valid = False

        if target_valid and execution.status == "awaiting_approval":
            target_valid = (
                approval.target_generation_id == session.target_generation_id
                and approval.target_generation_id == execution.target_generation_id
            )

        now = datetime.now(UTC)
        if not target_valid:
            approval.status = "cancelled"
            execution.status = "stale"
            execution.error = {
                "code": "approval_target_stale",
                "retryable": False,
                "recovery": "refresh_replan",
            }
            execution.completed_at = now
            turn.status = "failed"
            turn.completed_at = now
            turn.error = execution.error
            session.status = "awaiting_feedback"
            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_error",
                content=(
                    "The project changed before I could start that render. "
                    "I kept your draft; ask me to prepare it again."
                ),
                payload={
                    "turn_id": str(turn.id),
                    "approval_id": str(approval.id),
                    "code": "approval_target_stale",
                    "recovery": "refresh_replan",
                },
            )
            db.commit()
            return None

        strategy_payload: dict[str, Any] | None = None
        editor_prep: dict[str, Any] | None = None
        preflight_analysis_id: uuid.UUID | None = None
        target_variant_id = approval.target_variant_id
        target_generation_id = approval.target_generation_id
        if document.kind == "strategy":
            from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415

            strategy = CreativeStrategy.model_validate(document.strategy)
            strategy_payload = strategy.model_dump(mode="json", exclude_none=True)
            # Strategy dispatch mints a new Job whose winning output identity is
            # unknown until observation. Prior session variant/generation pins
            # authorize the input state, not an output sibling in the new Job.
            target_variant_id = None
            target_generation_id = None
            execution.target_variant_id = None
            if strategy.audio_strategy == "original_audio":
                next_audio_mode = "original"
            elif strategy.audio_strategy == "licensed_music":
                next_audio_mode = "kria"
            elif item.voiceover_gcs_path:
                next_audio_mode = "voiceover"
            else:
                approval.status = "cancelled"
                execution.status = "failed"
                execution.error = {
                    "code": "voiceover_required",
                    "retryable": False,
                    "recovery": "ask_user",
                }
                execution.completed_at = now
                turn.status = "failed"
                turn.completed_at = now
                turn.error = execution.error
                session.status = "awaiting_feedback"
                _append_sync_event(
                    db,
                    thread,
                    role="assistant",
                    event_type="assistant_question",
                    content="Record a voiceover first, then I can render this direction.",
                    payload={
                        "turn_id": str(turn.id),
                        "approval_id": str(approval.id),
                        "code": "voiceover_required",
                        "recovery": "ask_user",
                    },
                )
                db.commit()
                return None
            from app.services.plan_item_media import (  # noqa: PLC0415
                current_detector_policy,
                mutate_plan_item_media,
            )
            from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
                mutation_current_analysis_sync,
                schedule_item_preflight_sync,
            )

            current_cleanup = mutation_current_analysis_sync(db, item.id, for_update=True)
            mutate_plan_item_media(
                item,
                detector_policy=current_detector_policy(),
                edit_format=strategy.edit_format,
                audio_mode=next_audio_mode,
                current_analysis=current_cleanup,
            )
            preflight_analysis_id = schedule_item_preflight_sync(db, item)
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
        else:
            if current_job is None or not approval.target_variant_id:
                return None
            if document.editor_payload.get("operation") == "speech_cut":
                request, _enqueue = dispatch_apply_speech_cut_candidate(
                    current_job,
                    approval.target_variant_id,
                    candidate_id=str(document.editor_payload.get("candidate_id") or ""),
                    expected_revision=str(document.editor_payload.get("expected_revision") or ""),
                )
                control = (current_job.assembly_plan or {}).get("speech_cut_control") or {}
                target_generation_id = str(control.get("render_generation_id") or "")
                editor_prep = {
                    "speech_cut": True,
                    "request": request,
                    "generation": target_generation_id,
                }
            else:
                editor_payload = EditorCommitRequest.model_validate(document.editor_payload)
                music_track = (
                    db.get(MusicTrack, uuid.UUID(editor_payload.music_track_id))
                    if editor_payload.music_track_id
                    else None
                )
                editor_prep = prepare_editor_commit(
                    current_job,
                    approval.target_variant_id,
                    editor_payload,
                    user_id=str(thread.creator_id),
                    music_track=music_track,
                    plan_item_id=str(item.id),
                )
                target_generation_id = str(editor_prep["generation"])

        approval.status = "consumed"
        approval.consumed_at = approval.consumed_at or now
        execution.status = "accepted"
        execution.accepted_at = execution.accepted_at or now
        execution.external_task_id = f"kria-approval:{approval.id}"
        execution.target_generation_id = target_generation_id
        if editor_prep is not None:
            execution.result = {**(execution.result or {}), "editor_prep": editor_prep}
        turn.status = "executing"
        session.status = "executing"
        db.commit()
        return _ApprovalDispatchClaim(
            approval_id=approval.id,
            execution_id=execution.id,
            thread_id=thread.id,
            turn_id=turn.id,
            session_id=session.id,
            item_id=item.id,
            ownership_epoch=int(session.ownership_epoch),
            draft_kind=document.kind,
            strategy=strategy_payload,
            editor_prep=editor_prep,
            target_job_id=current_job.id if current_job is not None else None,
            target_variant_id=target_variant_id,
            target_generation_id=target_generation_id,
            creator_request=document.intent,
            preflight_analysis_id=preflight_analysis_id,
        )


def _finish_approval_dispatch(
    claim: _ApprovalDispatchClaim,
    *,
    outcome: str,
    job_id: str | None,
) -> tuple[str, str | None]:
    successful = outcome in {"dispatched", "already_active"} and job_id is not None
    successor_id: str | None = None
    with sync_session() as db:
        session = db.execute(
            select(CreatorAgentSession)
            .where(CreatorAgentSession.id == claim.session_id)
            .with_for_update()
        ).scalar_one_or_none()
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == claim.turn_id).with_for_update()
        ).scalar_one_or_none()
        approval = db.execute(
            select(CreatorAgentApproval)
            .where(CreatorAgentApproval.id == claim.approval_id)
            .with_for_update()
        ).scalar_one_or_none()
        execution = db.execute(
            select(CreatorAgentExecution)
            .where(CreatorAgentExecution.id == claim.execution_id)
            .with_for_update()
        ).scalar_one_or_none()
        thread = db.execute(
            select(CreationThread).where(CreationThread.id == claim.thread_id).with_for_update()
        ).scalar_one_or_none()
        if any(row is None for row in (session, turn, approval, execution, thread)):
            return "ignored", None
        if execution.status == "dispatched":
            return "dispatched", None
        if execution.status != "accepted":
            return "ignored", None

        now = datetime.now(UTC)
        if successful:
            identifier = uuid.UUID(str(job_id))
            execution.status = "dispatched"
            execution.target_job_id = identifier
            execution.external_task_id = str(job_id)
            execution.result = {
                **(execution.result or {}),
                "approval_id": str(approval.id),
                "dispatch_outcome": outcome,
                "job_id": str(job_id),
            }
            execution.dispatched_at = now
            turn.status = "observing"
            session.status = "rendering"
            session.target_job_id = identifier
            session.target_variant_id = claim.target_variant_id
            session.target_generation_id = claim.target_generation_id
            session.render_attempts = int(session.render_attempts) + 1
            _append_sync_event(
                db,
                thread,
                role="system",
                event_type="render_queued",
                content=None,
                payload={
                    "turn_id": str(turn.id),
                    "approval_id": str(approval.id),
                    "execution_id": str(execution.id),
                    "job_id": str(job_id),
                    "variant_id": claim.target_variant_id,
                    "generation_id": claim.target_generation_id,
                    "status": "queued",
                    "artifact_key": f"job:{job_id}",
                    "receipt_ids": [str(execution.id)],
                },
            )
            db.commit()
            return "dispatched", None

        if outcome == "outcome_unknown":
            execution.status = "outcome_unknown"
            execution.error = {
                "code": "render_dispatch_outcome_unknown",
                "retryable": False,
                "recovery": "manual",
            }
            session.status = "awaiting_feedback"
            session.last_error = execution.error
            turn.status = "failed"
            turn.completed_at = now
            turn.error = execution.error
            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_render_failed",
                content=(
                    "The render queue did not confirm whether it received this edit. "
                    "Your approved draft is saved; I won't send it twice until it is reconciled."
                ),
                payload={
                    "turn_id": str(turn.id),
                    "approval_id": str(approval.id),
                    "execution_id": str(execution.id),
                    "job_id": job_id,
                    "variant_id": claim.target_variant_id,
                    "generation_id": claim.target_generation_id,
                    "status": "outcome_unknown",
                    "code": "render_dispatch_outcome_unknown",
                    "recovery": "manual",
                },
            )
            db.commit()
            return "outcome_unknown", _promote_queued_successor_sync(thread.id)

        execution.status = "failed"
        execution.error = {
            "code": "render_dispatch_failed",
            "outcome": outcome,
            "retryable": outcome == "publish_failed",
            "recovery": "retry",
        }
        execution.completed_at = now
        turn.status = "failed"
        turn.completed_at = now
        turn.error = execution.error
        session.status = "awaiting_feedback"
        session.last_error = execution.error
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_render_failed",
            content=(
                "I couldn't start the render. Your draft is still saved, "
                "so you can retry without repeating the edit."
            ),
            payload={
                "turn_id": str(turn.id),
                "approval_id": str(approval.id),
                "execution_id": str(execution.id),
                "job_id": job_id,
                "status": "failed",
                "code": "render_dispatch_failed",
                "dispatch_outcome": outcome,
                "recovery": "retry",
            },
        )
        db.commit()
        successor_id = _promote_queued_successor_sync(thread.id)
    return "failed", successor_id


@celery_app.task(
    name="tasks.execute_kria_approval",
    soft_time_limit=45,
    time_limit=60,
    max_retries=0,
)
def execute_kria_approval(approval_id: str) -> dict[str, str | None]:
    """Consume one pinned approval and dispatch through the canonical Job path."""

    if not settings.kria_runtime_v2_enabled:
        return {"approval_id": approval_id, "status": "ignored", "job_id": None}
    identifier = uuid.UUID(approval_id)
    claim = _claim_approval_dispatch(identifier)
    if claim is None:
        return {"approval_id": approval_id, "status": "ignored", "job_id": None}
    preflight_analysis_id = getattr(claim, "preflight_analysis_id", None)
    if preflight_analysis_id is not None:
        from app.services.plan_item_media import publish_preflight_after_commit  # noqa: PLC0415

        publish_preflight_after_commit(preflight_analysis_id)

    if getattr(claim, "draft_kind", "strategy") == "editor":
        if (
            claim.target_job_id is None
            or claim.target_variant_id is None
            or claim.editor_prep is None
        ):
            return {"approval_id": approval_id, "status": "ignored", "job_id": None}
        try:
            if claim.editor_prep.get("speech_cut") is True:
                from app.tasks.generative_build import rerender_speech_timing  # noqa: PLC0415

                operation_id = str((claim.editor_prep.get("request") or {}).get("operation_id"))
                rerender_speech_timing.apply_async(
                    args=[str(claim.target_job_id), operation_id],
                    queue="plan-jobs",
                )
            else:
                enqueue_editor_commit_render(
                    str(claim.target_job_id),
                    claim.target_variant_id,
                    claim.editor_prep,
                )
            outcome = "dispatched"
        except Exception:  # noqa: BLE001 - committed generation must be reconciled, never duplicated
            log.exception(
                "kria_editor_render_publish_unknown",
                approval_id=approval_id,
                job_id=str(claim.target_job_id),
                variant_id=claim.target_variant_id,
                generation_id=claim.target_generation_id,
            )
            outcome = "outcome_unknown"
        result_job_id = str(claim.target_job_id)
    else:
        from app.tasks.content_plan_build import dispatch_item_render_for  # noqa: PLC0415

        result = dispatch_item_render_for(
            str(claim.item_id),
            claim.ownership_epoch,
            bypass_guided_edit_gate=True,
            creator_strategy=claim.strategy,
            creator_request=claim.creator_request,
        )
        outcome = result.outcome
        result_job_id = result.job_id
    status, successor_id = _finish_approval_dispatch(
        claim,
        outcome=outcome,
        job_id=result_job_id,
    )
    if successor_id is not None:
        run_kria_turn.apply_async(
            args=[successor_id],
            task_id=successor_id,
            queue="agent-control",
        )
    return {"approval_id": approval_id, "status": status, "job_id": result_job_id}


def _ready_variant(
    job: Job,
    *,
    variant_id: str | None = None,
    generation_id: str | None = None,
) -> dict[str, Any] | None:
    variants = (job.assembly_plan or {}).get("variants") or []
    return next(
        (
            variant
            for variant in variants
            if isinstance(variant, dict)
            and variant.get("render_status") == "ready"
            and variant.get("variant_id")
            and (variant_id is None or str(variant.get("variant_id")) == variant_id)
            and (
                generation_id is None
                or str(variant.get("render_generation_id") or "") == generation_id
            )
        ),
        None,
    )


def _observe_dispatched_execution(execution_id: uuid.UUID) -> tuple[str, str | None]:
    """Settle one dispatched receipt from durable Job truth."""

    from app.services.creator_sessions import (  # noqa: PLC0415
        PLAN_ITEM_JOB_FAILED,
        PLAN_ITEM_JOB_READY,
    )

    with sync_session() as db:
        execution_ref = db.get(CreatorAgentExecution, execution_id)
        if (
            execution_ref is None
            or execution_ref.status != "dispatched"
            or execution_ref.target_job_id is None
            or execution_ref.turn_id is None
            or execution_ref.target_thread_id is None
        ):
            return "ignored", None
        session_ref = db.get(CreatorAgentSession, execution_ref.session_id)
        if session_ref is None:
            return "ignored", None
        item_ref = db.get(PlanItem, session_ref.plan_item_id)
        if item_ref is None:
            return "ignored", None

        plan = db.execute(
            select(ContentPlan).where(ContentPlan.id == item_ref.content_plan_id).with_for_update()
        ).scalar_one_or_none()
        item = db.execute(
            select(PlanItem).where(PlanItem.id == item_ref.id).with_for_update()
        ).scalar_one_or_none()
        job = db.execute(
            select(Job).where(Job.id == execution_ref.target_job_id).with_for_update()
        ).scalar_one_or_none()
        session = db.execute(
            select(CreatorAgentSession)
            .where(CreatorAgentSession.id == execution_ref.session_id)
            .with_for_update()
        ).scalar_one_or_none()
        turn = db.execute(
            select(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == execution_ref.turn_id)
            .with_for_update()
        ).scalar_one_or_none()
        execution = db.execute(
            select(CreatorAgentExecution)
            .where(CreatorAgentExecution.id == execution_id)
            .with_for_update()
        ).scalar_one_or_none()
        thread = db.execute(
            select(CreationThread)
            .where(CreationThread.id == execution_ref.target_thread_id)
            .with_for_update()
        ).scalar_one_or_none()
        if any(row is None for row in (plan, item, job, session, turn, execution, thread)):
            return "ignored", None
        if execution.status != "dispatched" or turn.status != "observing":
            return "ignored", None

        exact_target = (
            int(thread.runtime_version) == 2
            and thread.active_creator_agent_session_id == session.id
            and thread.active_plan_item_id == item.id
            and plan.user_id == thread.creator_id == session.creator_id == job.user_id
            and item.content_plan_id == plan.id
            and item.current_job_id == job.id
            and job.content_plan_item_id == item.id
            and session.plan_item_id == item.id
            and session.target_job_id == job.id
            and int(plan.ownership_epoch or 0) == int(session.ownership_epoch)
            and int(job.content_plan_ownership_epoch or 0) == int(session.ownership_epoch)
        )
        terminal = job.status in PLAN_ITEM_JOB_READY or job.status in PLAN_ITEM_JOB_FAILED
        if not terminal:
            return "pending", None

        now = datetime.now(UTC)
        variant_failure_code: str | None = None
        if exact_target and job.status in PLAN_ITEM_JOB_READY:
            pinned_variant_id = str(execution.target_variant_id or "") or None
            pinned_generation_id = str(execution.target_generation_id or "") or None
            target_variant = next(
                (
                    row
                    for row in (job.assembly_plan or {}).get("variants") or []
                    if isinstance(row, dict)
                    and pinned_variant_id is not None
                    and str(row.get("variant_id") or "") == pinned_variant_id
                ),
                None,
            )
            if pinned_variant_id is not None and target_variant is not None:
                current_generation = str(target_variant.get("render_generation_id") or "") or None
                if current_generation == pinned_generation_id and target_variant.get(
                    "render_status"
                ) in {"pending", "rendering"}:
                    return "pending", None
                if (
                    current_generation == pinned_generation_id
                    and target_variant.get("render_status") == "failed"
                ):
                    variant_failure_code = "variant_render_failed"
            variant = _ready_variant(
                job,
                variant_id=pinned_variant_id,
                generation_id=pinned_generation_id,
            )
            if variant is not None:
                variant_id = str(variant["variant_id"])
                generation_id = str(variant.get("render_generation_id") or "") or None
                execution.status = "completed"
                execution.completed_at = now
                execution.observed_at = now
                if execution.target_variant_id is None:
                    execution.target_variant_id = variant_id
                if execution.target_generation_id is None:
                    execution.target_generation_id = generation_id
                execution.result = {
                    **(execution.result or {}),
                    "outcome": "ready",
                    "job_id": str(job.id),
                    "variant_id": variant_id,
                    "render_generation_id": generation_id,
                }
                turn.status = "completed"
                turn.completed_at = now
                session.status = "awaiting_feedback"
                session.target_variant_id = variant_id
                session.target_generation_id = generation_id
                session.last_good = {
                    "job_id": str(job.id),
                    "variant_id": variant_id,
                    "render_generation_id": generation_id,
                    "draft_id": (
                        str(execution.target_draft_id) if execution.target_draft_id else None
                    ),
                }
                thread.active_job_id = job.id
                event = _append_sync_event(
                    db,
                    thread,
                    role="assistant",
                    event_type="generation_ready",
                    content=None,
                    payload={
                        "turn_id": str(turn.id),
                        "execution_id": str(execution.id),
                        "job_id": str(job.id),
                        "variant_id": variant_id,
                        "render_generation_id": generation_id,
                        "status": "ready",
                        "artifact_key": f"job:{job.id}:variant:{variant_id}",
                        "receipt_ids": [str(execution.id)],
                    },
                )
                execution.observed_event_id = event.id
                review = _append_sync_event(
                    db,
                    thread,
                    role="assistant",
                    event_type="assistant_review",
                    content=(
                        f"The {variant_id.replace('_', ' ')} cut is ready. "
                        "The approved render finished; review the opening, pacing, and text, "
                        "then tell me what you want changed."
                    ),
                    payload={
                        "turn_id": str(turn.id),
                        "turn_value": "review",
                        "execution_id": str(execution.id),
                        "job_id": str(job.id),
                        "variant_id": variant_id,
                        "render_generation_id": generation_id,
                        "receipt_ids": [str(execution.id)],
                        "next_actions": ["review_cut", "request_revision"],
                        "schema_version": 2,
                    },
                )
                turn.observed_event_id = review.id
                db.commit()
                return "completed", _promote_queued_successor_sync(thread.id)

        failure_code = variant_failure_code or (
            job.failure_reason
            if job.status in PLAN_ITEM_JOB_FAILED and job.failure_reason
            else "render_identity_mismatch"
        )
        execution.status = "failed"
        execution.completed_at = now
        execution.observed_at = now
        execution.error = {
            "code": failure_code,
            "retryable": True,
            "recovery": "retry",
        }
        turn.status = "failed"
        turn.completed_at = now
        turn.error = execution.error
        session.status = "awaiting_feedback"
        session.last_error = execution.error
        event = _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_render_failed",
            content=(
                "That render didn't finish. Your approved draft is still saved, "
                "so you can retry without rebuilding the edit."
            ),
            payload={
                "turn_id": str(turn.id),
                "execution_id": str(execution.id),
                "job_id": str(job.id),
                "status": "failed",
                "code": failure_code,
                "recovery": "retry",
                "receipt_ids": [str(execution.id)],
            },
        )
        execution.observed_event_id = event.id
        turn.observed_event_id = event.id
        db.commit()
        return "failed", _promote_queued_successor_sync(thread.id)


@celery_app.task(
    name="tasks.reconcile_kria_turns",
    soft_time_limit=30,
    time_limit=45,
    max_retries=0,
)
def reconcile_kria_turns() -> dict[str, int]:
    """Republish bounded pending turns and accepted approval executions.

    ``CreatorAgentTurn`` is the durable dispatch ledger. A publish can fail
    after the accepting transaction commits, so this sweep closes that gap
    without inventing a second outbox. The task ID and lease claim make
    duplicate broker deliveries inert.
    """

    if not settings.kria_runtime_v2_enabled:
        return {"published": 0, "settled": 0}

    with sync_session() as db:
        database_now = db.execute(select(func.now())).scalar_one()
        turns = list(
            db.execute(
                select(CreatorAgentTurn)
                .where(
                    (
                        (CreatorAgentTurn.status == "pending")
                        & (
                            (CreatorAgentTurn.lease_expires_at.is_(None))
                            | (CreatorAgentTurn.lease_expires_at <= func.now())
                        )
                    )
                    | (
                        (CreatorAgentTurn.status == "planning")
                        & (CreatorAgentTurn.lease_expires_at < func.now())
                    )
                )
                .order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        # Persist a not-before marker before broker I/O. Concurrent sweepers
        # skip these locked rows, and a worker may still claim the pending row
        # immediately because broker delivery is the intended fast path.
        for turn in turns:
            turn.status = "pending"
            turn.lease_owner = None
            turn.lease_expires_at = database_now + _REPUBLISH_BACKOFF
        executions = list(
            db.execute(
                select(CreatorAgentExecution)
                .where(CreatorAgentExecution.status == "accepted")
                .order_by(CreatorAgentExecution.accepted_at, CreatorAgentExecution.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        approved_approval_ids = list(
            db.execute(
                select(CreatorAgentApproval.id)
                .where(CreatorAgentApproval.status == "approved")
                .order_by(CreatorAgentApproval.created_at, CreatorAgentApproval.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        db.commit()
        identifiers = [turn.id for turn in turns]
        approval_identifiers = [
            str((execution.result or {}).get("approval_id"))
            for execution in executions
            if (execution.result or {}).get("approval_id")
        ]
        approval_identifiers = list(
            dict.fromkeys([*(str(value) for value in approved_approval_ids), *approval_identifiers])
        )
        dispatched_execution_ids = list(
            db.execute(
                select(CreatorAgentExecution.id)
                .where(
                    CreatorAgentExecution.status == "dispatched",
                    CreatorAgentExecution.target_job_id.is_not(None),
                )
                .order_by(CreatorAgentExecution.dispatched_at, CreatorAgentExecution.id)
                .limit(50)
            ).scalars()
        )
    for identifier in identifiers:
        turn_id = str(identifier)
        run_kria_turn.apply_async(args=[turn_id], task_id=turn_id, queue="agent-control")
    for approval_id in approval_identifiers:
        execute_kria_approval.apply_async(
            args=[approval_id],
            task_id=f"kria-approval:{approval_id}",
            queue="agent-control",
        )
    settled = 0
    successors: list[str] = []
    for execution_id in dispatched_execution_ids:
        outcome, successor_id = _observe_dispatched_execution(execution_id)
        if outcome in {"completed", "failed"}:
            settled += 1
        if successor_id is not None:
            successors.append(successor_id)
    for successor_id in successors:
        run_kria_turn.apply_async(
            args=[successor_id],
            task_id=successor_id,
            queue="agent-control",
        )
    return {
        "published": len(identifiers) + len(approval_identifiers),
        "settled": settled,
    }


@celery_app.task(
    name="tasks.prune_kria_drafts",
    soft_time_limit=30,
    time_limit=45,
    max_retries=0,
)
def prune_kria_drafts() -> dict[str, int]:
    """Prune old superseded bodies while preserving identity and approved work."""

    with sync_session() as db:
        cutoff = db.execute(select(func.now())).scalar_one() - _DRAFT_BODY_RETENTION
        candidates = list(
            db.execute(
                select(CreatorEditDraft)
                .where(
                    CreatorEditDraft.is_head.is_(False),
                    CreatorEditDraft.snapshot_json.is_not(None),
                    CreatorEditDraft.created_at < cutoff,
                )
                .order_by(CreatorEditDraft.created_at, CreatorEditDraft.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        if not candidates:
            db.commit()
            return {"pruned": 0}
        protected = set(
            db.execute(
                select(CreatorAgentApproval.draft_id).where(
                    CreatorAgentApproval.draft_id.in_([row.id for row in candidates]),
                    CreatorAgentApproval.status.in_({"pending", "approved", "consumed"}),
                )
            ).scalars()
        )
        pruned = 0
        for draft in candidates:
            if draft.id in protected:
                continue
            draft.snapshot_json = None
            pruned += 1
        db.commit()
        return {"pruned": pruned}
