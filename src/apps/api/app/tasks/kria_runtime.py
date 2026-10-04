"""Durable runtime-v2 planning, editing, approval, and observation tasks.

Every consequential render remains separated from draft creation by a pinned,
persisted approval. Workers report only durable receipts and observed Job truth.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import sync_session
from app.db_locks import CONTENT_PLAN_LOCK
from app.kria.brief import (
    BriefUpdate,
    CreativeBrief,
    load_latest_brief_sync,
    persist_brief_version_sync,
    render_brief_request,
)
from app.kria.brief_checks import (
    build_receipts,
    is_judged,
    plan_facts_from_editor_payload,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.contracts import KriaObservedTurnResponse, KriaToolReceipt, KriaTurnPlan
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.language import is_paraphrase_only
from app.kria.planner import PlannedKriaTurn, extract_deferred_brief, plan_live_turn
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
    _find_variant,
    dispatch_apply_speech_cut_candidate,
    enqueue_editor_commit_render,
    phone_subtitled_sfx_paths_sync,
    prepare_editor_commit,
    variant_render_baseline,
)
from app.services.device_render import DEVICE_RENDER_FIELD, device_status
from app.services.kria_editor_ops import (
    EditorStateReplyError,
    EditorStateSpeechCutError,
    KriaEditorOpError,
    compile_editor_ops,
    editor_state_has_lanes,
    merge_editor_draft,
    parse_editor_state,
    resolve_editor_base,
)
from app.worker import celery_app

log = structlog.get_logger()
_REPUBLISH_BACKOFF = timedelta(minutes=1)
_APPROVAL_TTL = timedelta(minutes=30)
_LEASE_HEARTBEAT_SECONDS = 5
# A turn whose runs ended without a result this many times (killed at the task's
# time limit, a lost worker) is failed instead of re-planned: every run can pay
# for a Main Creator call, and the reconciler would republish it forever.
_MAX_ABANDONED_CLAIMS = 3
_DRAFT_BODY_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class _Completion:
    committed: bool
    successor_turn_id: str | None = None
    requeue_turn_id: str | None = None


@dataclass(frozen=True)
class _ClaimsExhausted:
    successor_turn_id: str | None = None


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
    speech_cleanup_analysis_id: uuid.UUID | None = None
    speech_cleanup_choice: str | None = None
    # What the plan item pointed at BEFORE a strategy dispatch mints its new Job, so a
    # failure after the pointer moves can put it back (never leave an orphan target).
    prior_item_status: str | None = None


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
    brief_updates: tuple[BriefUpdate, ...] = (),
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
        if brief_updates:
            # KRI-188: a question/recovery turn still records what the creator
            # stated. Written under the thread lock, after the revision fence,
            # so a requeued turn never persists a version.
            persist_brief_version_sync(
                db, thread_id=thread.id, turn_id=turn.id, updates=brief_updates
            )
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
                **({"clip_question": plan.clip_question} if plan.clip_question else {}),
                # KRI-374: persisted so the answer can be validated + folded later.
                **(
                    {"song_order_question": plan.song_order_question.model_dump(mode="json")}
                    if plan.song_order_question is not None
                    else {}
                ),
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


def _validate_draft_plan(plan: KriaTurnPlan, *, required_route: str | None = None) -> None:
    """Allow one atomic draft, optionally followed by its exact render approval.

    ``required_route`` is the Creative Brief router's verdict (KRI-188). When it
    says ``replan``, an editor-ops-only plan is rejected: the new requirements
    need a fresh strategy, and a lossy editor op would silently drop them.
    """
    if plan.mode != "act" or len(plan.intents) not in {1, 2}:
        raise RuntimeError("Kria produced an unsupported tool group")
    apply_intent = plan.intents[0]
    if apply_intent.tool_name not in {"draft.apply_strategy", "draft.apply_editor_ops"}:
        raise RuntimeError("Kria produced an unsupported draft tool")
    if required_route == "replan" and apply_intent.tool_name != "draft.apply_strategy":
        raise RuntimeError("The brief needs a new plan, not editor operations")
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
        return replace(planned, plan=replacement)

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
    return replace(planned, plan=plan.model_copy(update={"intents": intents}))


def _state_event_fields(state_id: str | None, trace: dict[str, Any]) -> dict[str, Any]:
    """`draft_applied` additions; empty (byte-identical event) when no state rode the turn."""
    return {
        **({"based_on_client_state_id": state_id} if state_id else {}),
        **trace,
    }


def _complete_draft_turn(
    turn_id: uuid.UUID,
    *,
    lease_owner: str,
    lease_epoch: int,
    claimed_thread_revision: int,
    planned: PlannedKriaTurn,
) -> _Completion:
    plan = planned.plan
    _validate_draft_plan(plan, required_route=planned.brief_route)
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
    # Editor-state provenance (set only when a client state rode this turn).
    state_trace: dict[str, Any] = {}
    state_id: str | None = None
    your_edits_snapshot: dict[str, Any] | None = None
    your_edits_hash = ""
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
            # `of=`: the joined PlanItem row is locked FOR UPDATE just below.
            .with_for_update(of=ContentPlan, **CONTENT_PLAN_LOCK)
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
        draft_generation_id = generation_id
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
            if (
                thread.active_creator_agent_session_id != session.id
                or session.plan_item_id != item.id
                or session.target_job_id != job.id
                or item.current_job_id != job.id
                or job.content_plan_item_id != item.id
                or job.user_id != thread.creator_id
                or session.creator_id != thread.creator_id
                or variant.get("variant_id") != session.target_variant_id
            ):
                raise RuntimeError("The exact editor target is no longer available")
            # The SAME resolver the planner used for the snapshot the model saw, re-run
            # here under the row locks so a state that went stale during the model
            # call is refused instead of silently rebased.
            client_state = parse_editor_state(turn.editor_state)
            editor_base = resolve_editor_base(
                job,
                variant,
                head if head is not None and head.base_job_id == job.id else None,
                client_state,
            )
            prior_payload = editor_base.prior_payload
            # The session pointer goes stale after an editor Save; stamp the draft
            # with the variant's real generation so it never self-perpetuates.
            canonical_generation_id = variant_render_baseline(variant) or generation_id
            generation_id = canonical_generation_id
            draft_generation_id = canonical_generation_id
            # The editor Save is an external Job mutation. Keep this exact
            # session target aligned with the locked variant before minting a
            # later approval, while the existing Job -> Session lock order is
            # held. A failed transaction rolls this pointer back with the Job.
            session.target_generation_id = canonical_generation_id
            wants_speech_cut = any(
                op.get("op") == "apply_speech_cut_candidate" for op in arguments.operations
            )
            if wants_speech_cut and editor_base.source == "client_state":
                if editor_state_has_lanes(client_state):
                    raise EditorStateSpeechCutError(EditorStateSpeechCutError.reply)
            elif wants_speech_cut and prior_payload:
                raise RuntimeError("Save the current draft before applying speech processing")
            compiled = compile_editor_ops(job, editor_base.projected, arguments.operations)
            changes = compiled.changes
            state_id = (
                client_state.client_state_id if editor_base.source == "client_state" else None
            )
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
                editor_text_diff=compiled.text_diff or None,
                changes=changes,
                client_state_id=state_id,
            )
            snapshot, snapshot_hash = canonical_snapshot(document)
            if client_state is not None:
                state_trace = {
                    "editor_state_source": editor_base.source,
                    **(
                        {"editor_state_fallback": editor_base.fallback_reason}
                        if editor_base.fallback_reason
                        else {}
                    ),
                }
            if editor_base.source == "client_state" and editor_state_has_lanes(client_state):
                your_edits = KriaDraftDocument(
                    kind="editor",
                    intent="Your edits",
                    edit_format=str(item.edit_format or "montage"),
                    editor_payload=prior_payload,
                    changes=[],
                    client_state_id=state_id,
                )
                your_edits_snapshot, your_edits_hash = canonical_snapshot(your_edits)
        if document is None:
            raise RuntimeError("Kria produced an unsupported draft tool")
        reply_text = arguments.summary
        requirement_receipts: list[dict[str, Any]] = []
        if planned.brief_route is not None:
            # KRI-188: persist this turn's requirements (idempotent per turn),
            # then check each one deterministically against what was drafted.
            brief = persist_brief_version_sync(
                db,
                thread_id=thread.id,
                turn_id=turn.id,
                updates=planned.brief_updates,
            )
            if brief is not None:
                if apply_intent.tool_name == "draft.apply_strategy":
                    facts = plan_facts_from_strategy(
                        document.strategy,
                        clip_ids=planned.brief_clip_ids,
                        manifest=planned.brief_manifest,
                        speech_cleanup_enabled=bool(getattr(item, "speech_cleanup_enabled", False)),
                    )
                    checked = brief.live()
                    # KRI-190: the unified montage planner writes the per-clip text,
                    # order and title at render time and reports on them then. Judging
                    # them against this text-free draft would only mislead.
                    checked = requirements_to_check_at_draft(
                        checked,
                        creator_id=thread.creator_id,
                        strategy=document.strategy,
                        item_edit_format=item.edit_format,
                        clip_paths=item.clip_gcs_paths or (),
                    )
                else:
                    # Editor operations verify only the requirements stated in
                    # this very turn, against literal text in the editor payload.
                    facts = plan_facts_from_editor_payload(
                        document.editor_payload, document.editor_text_diff, changes
                    )
                    checked = [req for req in brief.live() if req.source_turn_id == str(turn.id)]
                if checked:
                    receipts = build_receipts(checked, facts)
                    requirement_receipts = [r.model_dump(mode="json") for r in receipts]
                    reply_text = reply_from_receipts(
                        CreativeBrief(version=brief.version, requirements=checked),
                        receipts,
                        summary=arguments.summary,
                        notices=planned.policy_notices,
                    )
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
            result={"snapshot_hash": snapshot_hash, "changes": changes, **state_trace},
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        db.add(draft_execution)
        db.flush()
        if head is not None:
            head.is_head = False
        parent_draft = head
        if your_edits_snapshot is not None:
            # The creator's unsaved editor state becomes its own revision first (the
            # chat draft's parent, so Undo returns to exactly what they had).
            db.flush()
            parent_draft = CreatorEditDraft(
                creator_id=thread.creator_id,
                thread_id=thread.id,
                item_id=item.id,
                variant_key=variant_key,
                base_job_id=job.id if job is not None else None,
                base_generation_id=draft_generation_id,
                draft_revision=next_revision,
                parent_draft_id=head.id if head is not None else None,
                snapshot_json=your_edits_snapshot,
                snapshot_hash=your_edits_hash,
                is_head=True,
            )
            db.add(parent_draft)
            db.flush()
            parent_draft.is_head = False
            db.flush()
            next_revision += 1
        draft = CreatorEditDraft(
            creator_id=thread.creator_id,
            thread_id=thread.id,
            item_id=item.id,
            variant_key=variant_key,
            base_job_id=job.id if job is not None else None,
            base_generation_id=draft_generation_id,
            draft_revision=next_revision,
            parent_draft_id=parent_draft.id if parent_draft is not None else None,
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
                content=reply_text,
                payload={
                    "turn_id": str(turn.id),
                    "draft_id": str(draft.id),
                    "draft_revision": draft.draft_revision,
                    "snapshot_hash": draft.snapshot_hash,
                    "changes": changes,
                    "can_undo": parent_draft is not None,
                    "receipt_ids": [str(draft_execution.id)],
                    "render_requested": False,
                    **_state_event_fields(state_id, state_trace),
                    **(
                        {"requirement_receipts": requirement_receipts}
                        if requirement_receipts
                        else {}
                    ),
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
            content=reply_text,
            payload={
                "turn_id": str(turn.id),
                "draft_id": str(draft.id),
                "draft_revision": draft.draft_revision,
                "snapshot_hash": draft.snapshot_hash,
                "changes": changes,
                "can_undo": parent_draft is not None,
                "receipt_ids": [str(draft_execution.id)],
                **_state_event_fields(state_id, state_trace),
                **({"requirement_receipts": requirement_receipts} if requirement_receipts else {}),
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
    editor_state: dict[str, Any] | None = None,
) -> PlannedKriaTurn:
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=_LEASE_HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                try:
                    alive = await asyncio.to_thread(
                        _renew_turn_lease,
                        turn_id,
                        lease_owner=lease_owner,
                        lease_epoch=lease_epoch,
                    )
                except Exception:  # noqa: BLE001 - the lease outlasts a missed renewal
                    # Ending the heartbeat here would let the lease lapse mid-plan
                    # and, re-raised in `finally`, replace a finished plan.
                    log.warning(
                        "kria_turn_lease_renewal_failed", turn_id=str(turn_id), exc_info=True
                    )
                    continue
                if not alive:
                    return

    # Each task run plans inside a fresh `asyncio.run` loop, and asyncpg
    # connections cannot be shared across loops: a connection pooled by the
    # previous turn in this Celery child fails its pre-ping with "attached to a
    # different loop". Plan on an unpooled engine that lives for this loop only.
    # Unpooled means the planner's rollbacks before model calls close the
    # connection (a reconnect costs tens of ms against multi-second model calls).
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    heartbeat = asyncio.create_task(_heartbeat())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            parsed_state = parse_editor_state(editor_state)
            return await plan_live_turn(
                db,
                thread_id=uuid.UUID(str(snapshot["thread_id"])),
                item_id=uuid.UUID(str(snapshot["item_id"])),
                creator_id=uuid.UUID(str(snapshot["creator_id"])),
                user_message=user_message,
                # Only passed when present so the no-state call is byte-identical.
                **({"editor_state": parsed_state} if parsed_state is not None else {}),
            )
    finally:
        stop.set()
        try:
            await heartbeat
        finally:
            await engine.dispose()


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


def _stored_editor_state(turn: Any) -> dict[str, Any] | None:
    state = getattr(turn, "editor_state", None)
    return state if isinstance(state, dict) else None


def _claim(
    turn_id: uuid.UUID, lease_owner: str
) -> tuple[dict[str, Any], str, int, int, dict[str, Any] | None] | _ClaimsExhausted | None:
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
        if expired_read_lease:
            # A redelivered task reached the lapsed lease before the reconciler
            # reset it; the run that held it ended without a result.
            turn.abandoned_claims = int(turn.abandoned_claims or 0) + 1
        if int(turn.abandoned_claims or 0) >= _MAX_ABANDONED_CLAIMS:
            return _fail_exhausted_turn(db, turn)
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
            _stored_editor_state(turn),
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


def _lock_queued_successor(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    thread_id: uuid.UUID,
) -> CreatorAgentTurn | None:
    return (
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


def _promote_queued_successor_sync(thread_id: uuid.UUID) -> str | None:
    with sync_session() as db:
        successor = _lock_queued_successor(db, thread_id)
        if successor is not None:
            # Turn -> Thread serializes this slot promotion with turn submit.
            db.execute(
                select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
            )
            successor.status = "pending"
        db.commit()
        return str(successor.id) if successor is not None else None


def _project_retryable_failure(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    turn: CreatorAgentTurn,
    thread: CreationThread,
    *,
    code: str,
    detail: dict[str, str] | None = None,
) -> None:
    """Fail a locked turn and tell the creator to retry; the caller commits.

    ``detail`` (``error_class`` + a truncated ``error_message``) makes an
    otherwise opaque ``runtime_turn_failed`` diagnosable (KRI-203: the
    2026-09-25 failure carried only the code). The full detail lives on
    ``turn.error``, which only admin routes read. The ``assistant_error`` event
    payload is returned unfiltered to the creator's app, so it gets the class
    name only (see ``_event_failure_detail``). Creator-facing copy is unchanged.
    """

    turn.status = "failed"
    turn.error = {"code": code, "retryable": True, "recovery": "retry", **(detail or {})}
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
            **_event_failure_detail(detail),
        },
    )
    turn.observed_event_id = event.id


def _fail_exhausted_turn(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    turn: CreatorAgentTurn,
) -> _ClaimsExhausted | None:
    """Fail a locked turn that is out of claims and promote its successor.

    One transaction, Turn -> successor Turn -> Thread (`CANONICAL_LOCK_ORDER`):
    promoting separately could fail after the commit and strand the successor
    `queued` behind a finished turn, where no sweep promotes it and every new
    message is refused with `queued_successor_exists`.
    """

    successor = _lock_queued_successor(db, turn.thread_id)
    thread = db.execute(
        select(CreationThread).where(CreationThread.id == turn.thread_id).with_for_update()
    ).scalar_one_or_none()
    if thread is None:
        return None
    _project_retryable_failure(db, turn, thread, code="runtime_turn_claims_exhausted")
    if successor is not None:
        successor.status = "pending"
    db.commit()
    log.warning(
        "kria_turn_claims_exhausted",
        turn_id=str(turn.id),
        thread_id=str(thread.id),
        abandoned_claims=int(turn.abandoned_claims),
        lease_epoch=int(turn.lease_epoch),
        successor_turn_id=str(successor.id) if successor is not None else None,
    )
    return _ClaimsExhausted(str(successor.id) if successor is not None else None)


_FAILURE_MESSAGE_CHARS = 200


# Only these exceptions carry a message written for humans (no SQL, paths, URLs or
# provider bodies), so only their message may reach the creator-visible event.
_EVENT_SAFE_MESSAGE_CLASSES = frozenset({"KriaEditorOpError"})


def _event_failure_detail(detail: dict[str, str] | None) -> dict[str, str]:
    """The slice of a failure detail that may go on a creator-visible event."""
    if not detail:
        return {}
    out = {"error_class": detail["error_class"]} if detail.get("error_class") else {}
    if detail.get("error_class") in _EVENT_SAFE_MESSAGE_CLASSES and detail.get("error_message"):
        out["error_message"] = detail["error_message"]
    return out


def _failure_detail(exc: BaseException) -> dict[str, str]:
    """Bounded, single-line error summary safe to persist on a turn/event."""
    message = " ".join(str(exc).split())[:_FAILURE_MESSAGE_CHARS]
    return {"error_class": type(exc).__name__, "error_message": message}


def _fail_turn(
    turn_id: uuid.UUID,
    *,
    code: str,
    lease_owner: str,
    lease_epoch: int,
    detail: dict[str, str] | None = None,
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
            _project_retryable_failure(db, turn, thread, code=code, detail=detail)
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
    if isinstance(claimed, _ClaimsExhausted):
        if claimed.successor_turn_id is not None:
            run_kria_turn.apply_async(
                args=[claimed.successor_turn_id],
                task_id=claimed.successor_turn_id,
                queue="agent-control",
            )
        return {"turn_id": turn_id, "status": "failed"}
    snapshot, user_message, lease_epoch, claimed_thread_revision, *claimed_rest = claimed
    editor_state = claimed_rest[0] if claimed_rest else None
    try:
        if settings.main_creator_agent_enabled and snapshot.get("item_id"):
            planned = asyncio.run(
                _plan_with_live_agent(
                    snapshot,
                    user_message,
                    turn_id=identifier,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                    **({"editor_state": editor_state} if editor_state else {}),
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
                    brief_updates=planned.brief_updates,
                )
            else:
                try:
                    completion = _complete_draft_turn(
                        identifier,
                        lease_owner=lease_owner,
                        lease_epoch=lease_epoch,
                        claimed_thread_revision=claimed_thread_revision,
                        planned=planned,
                    )
                except EditorStateReplyError as exc:
                    # The creator's unsaved state is stale (video changed) or the op
                    # cannot honour it (speech cut): an honest reply, nothing changed.
                    log.info("kria_editor_state_refused", turn_id=turn_id, reply=exc.reply)
                    planned = replace(
                        planned,
                        plan=KriaTurnPlan(
                            mode="respond", turn_value="recovery", response=exc.reply
                        ),
                    )
                    completion = _complete_response_turn(
                        identifier,
                        lease_owner=lease_owner,
                        lease_epoch=lease_epoch,
                        claimed_thread_revision=claimed_thread_revision,
                        plan=planned.plan,
                    )
                except KriaEditorOpError as exc:
                    # KRI-219: an op the recipe cannot represent (e.g. speed on a
                    # device recipe) is a limit to explain, not a runtime crash.
                    # The draft transaction rolled back; reply with the reason.
                    log.info("kria_editor_op_unsupported", turn_id=turn_id, error=str(exc)[:200])
                    planned = replace(
                        planned,
                        plan=KriaTurnPlan(
                            mode="respond",
                            turn_value="recovery",
                            response=(
                                f"I can't do that on this edit: {str(exc).strip().rstrip('.')}. "
                                "Nothing was changed."
                            ),
                        ),
                    )
                    completion = _complete_response_turn(
                        identifier,
                        lease_owner=lease_owner,
                        lease_epoch=lease_epoch,
                        claimed_thread_revision=claimed_thread_revision,
                        plan=planned.plan,
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
            if planned.defer_brief:
                try:
                    extract_kria_brief.apply_async(
                        args=[turn_id], task_id=f"brief-{turn_id}", queue="agent-control"
                    )
                except Exception:  # noqa: BLE001 - the brief is best-effort context
                    log.warning(
                        "kria_deferred_brief_enqueue_failed", turn_id=turn_id, exc_info=True
                    )
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
    except Exception as exc:  # noqa: BLE001 - failure is projected before Celery records it
        # The failure was invisible for weeks: no log line, no error detail
        # (KRI-203). Record the class + a truncated message on the turn/event
        # and log the traceback before re-raising.
        log.exception(
            "kria_turn_failed",
            turn_id=turn_id,
            error_class=type(exc).__name__,
        )
        successor_turn_id = _fail_turn(
            identifier,
            code="runtime_turn_failed",
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            detail=_failure_detail(exc),
        )
        if successor_turn_id is not None:
            run_kria_turn.apply_async(
                args=[successor_turn_id],
                task_id=successor_turn_id,
                queue="agent-control",
            )
        raise


async def _extract_brief_async(snapshot: dict[str, Any], user_message: str):
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            return await extract_deferred_brief(
                db,
                thread_id=uuid.UUID(str(snapshot["thread_id"])),
                item_id=uuid.UUID(str(snapshot["item_id"])),
                creator_id=uuid.UUID(str(snapshot["creator_id"])),
                user_message=user_message,
            )
    finally:
        await engine.dispose()


_DEFERRED_REPLAN_NOTE = (
    "I made that change in the editor. Part of your request needs a fresh edit, not an "
    "in-place tweak: tell me to redo it and I'll re-plan around your full request."
)


@celery_app.task(
    bind=True,
    name="tasks.extract_kria_brief",
    soft_time_limit=90,
    time_limit=120,
    max_retries=0,
)
def extract_kria_brief(self, turn_id: str) -> dict[str, str]:  # noqa: ANN001
    """KRI-219: record the requirements of an in-place edit the copilot already applied.

    The fast path answered the creator first; this runs the slow extraction after
    the fact, persists the brief version for that turn (idempotent per turn), and
    says so when the router would have re-planned (the edit covered only part of
    the ask). Best-effort: it never touches the draft.
    """
    identifier = uuid.UUID(turn_id)
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, identifier)
        if turn is None or turn.status != "completed":
            return {"turn_id": turn_id, "status": "ignored"}
        thread = db.get(CreationThread, turn.thread_id)
        source = db.get(CreationThreadEvent, turn.source_event_id)
        if thread is None or source is None:
            return {"turn_id": turn_id, "status": "ignored"}
        snapshot, message = _snapshot(thread), str(source.content or "")
    if not snapshot.get("item_id"):
        return {"turn_id": turn_id, "status": "ignored"}
    try:
        updates, route = asyncio.run(_extract_brief_async(snapshot, message))
    except Exception:  # noqa: BLE001 - best-effort context
        log.warning("kria_deferred_brief_failed", turn_id=turn_id, exc_info=True)
        return {"turn_id": turn_id, "status": "failed"}
    with sync_session() as db:
        thread = db.execute(
            select(CreationThread)
            .where(CreationThread.id == uuid.UUID(str(snapshot["thread_id"])))
            .with_for_update()
        ).scalar_one()
        if updates:
            persist_brief_version_sync(db, thread_id=thread.id, turn_id=identifier, updates=updates)
        if route == "replan":
            log.info("kria_fast_path_route_mismatch", turn_id=turn_id)
            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_response",
                content=_DEFERRED_REPLAN_NOTE,
                payload={
                    "turn_id": turn_id,
                    "turn_value": "recovery",
                    "receipt_ids": [],
                    "next_actions": [],
                    "schema_version": 2,
                },
            )
        db.commit()
    return {"turn_id": turn_id, "status": "done", "route": str(route)}


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
            .with_for_update(**CONTENT_PLAN_LOCK)
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
            if document is not None and document.kind == "editor" and current_job is not None:
                current_variant = next(
                    (
                        row
                        for row in (current_job.assembly_plan or {}).get("variants") or []
                        if isinstance(row, dict)
                        and row.get("variant_id") == execution.target_variant_id
                    ),
                    None,
                )
                current_generation = (
                    variant_render_baseline(current_variant)
                    if isinstance(current_variant, dict)
                    else ""
                )
                target_valid = bool(current_generation) and (
                    approval.target_generation_id == current_generation
                    and execution.target_generation_id == current_generation
                )
            else:
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
        speech_cleanup_analysis_id: uuid.UUID | None = None
        speech_cleanup_choice: str | None = None
        target_variant_id = approval.target_variant_id
        target_generation_id = approval.target_generation_id
        if document.kind == "strategy":
            from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
            from app.services.speech_cleanup_decision import (  # noqa: PLC0415
                resolve_next_audio_mode,
            )

            strategy = CreativeStrategy.model_validate(document.strategy)
            strategy_payload = strategy.model_dump(mode="json", exclude_none=True)
            # Strategy dispatch mints a new Job whose winning output identity is
            # unknown until observation. Prior session variant/generation pins
            # authorize the input state, not an output sibling in the new Job.
            target_variant_id = None
            target_generation_id = None
            execution.target_variant_id = None
            next_audio_mode = resolve_next_audio_mode(strategy, item)
            if next_audio_mode is None:
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
            # KRI-205: `decide_approval` stashed the speech-cleanup decision it
            # (or its legacy default) already validated at approval time --
            # read it back rather than asking the creator again here. Absent
            # for a thread that predates the stash, an editor-kind draft, or a
            # cohort/mode this item was never in; `dispatch_item_render_for`
            # treats both `None`s exactly like today's no-decision call.
            stash = (execution.result or {}).get("speech_cleanup")
            if isinstance(stash, dict):
                try:
                    speech_cleanup_analysis_id = (
                        uuid.UUID(str(stash["analysis_id"])) if stash.get("analysis_id") else None
                    )
                except (TypeError, ValueError):
                    speech_cleanup_analysis_id = None
                speech_cleanup_choice = stash.get("choice")
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
                device_variant = _is_device_variant(current_job, approval.target_variant_id)
                try:
                    # Inside the try: deriving the lanes re-validates the pinned
                    # recipe, and a device recipe that fails is a refusal too.
                    phone_sfx_catalog_paths = phone_subtitled_sfx_paths_sync(
                        db,
                        current_job,
                        _find_variant(current_job, approval.target_variant_id) or {},
                    )
                    editor_prep = prepare_editor_commit(
                        current_job,
                        approval.target_variant_id,
                        editor_payload,
                        user_id=str(thread.creator_id),
                        music_track=music_track,
                        plan_item_id=str(item.id),
                        phone_sfx_catalog_paths=phone_sfx_catalog_paths,
                    )
                except (HTTPException, ValueError, KeyError) as exc:
                    if not device_variant:
                        raise
                    # A device variant re-compiles its phone recipe here
                    # (`prepare_phone_editor_commit`), and refusing an edit the
                    # phone cannot draw is an expected outcome, not a crash.
                    # Uncaught, this task would fail before any state moved and
                    # the reconciler would republish the same approval forever.
                    # Nothing was staged (validation runs on a copy).
                    code = _device_refusal_code(exc)
                    log.warning(
                        "kria_device_edit_refused",
                        approval_id=str(approval.id),
                        code=code,
                        error_type=type(exc).__name__,
                        error=str(getattr(exc, "detail", None) or exc),
                    )
                    approval.status = "cancelled"
                    execution.status = "failed"
                    execution.error = {"code": code, "retryable": False, "recovery": "revise"}
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
                        content=_DEVICE_EDIT_REFUSALS.get(code, _DEVICE_EDIT_REFUSAL_FALLBACK),
                        payload={
                            "turn_id": str(turn.id),
                            "approval_id": str(approval.id),
                            "code": code,
                            "recovery": "revise",
                        },
                    )
                    db.commit()
                    return None
                target_generation_id = str(editor_prep["generation"])
                if device_variant and editor_prep.get("render_destination") == "device":
                    # The phone publishes under its own upload-attempt id, so the
                    # observer matches this Save by the recipe revision it pinned.
                    try:
                        revision = device_status(
                            current_job, approval.target_variant_id
                        ).request.identity.recipe_revision
                    except (KeyError, ValueError, TypeError):
                        revision = None
                    if revision is not None and editor_prep.get("has_render_section"):
                        editor_prep = {**editor_prep, "device_recipe_revision": int(revision)}

        dispatch_request = document.intent
        if document.kind == "strategy" and settings.creative_brief_for(thread.creator_id):
            # KRI-188: the strategy's creator request is rendered from the
            # Creative Brief, never from the draft summary or chip text.
            brief = load_latest_brief_sync(db, thread.id)
            if brief is not None and brief.live():
                dispatch_request = render_brief_request(brief)

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
            creator_request=dispatch_request,
            preflight_analysis_id=preflight_analysis_id,
            speech_cleanup_analysis_id=speech_cleanup_analysis_id,
            speech_cleanup_choice=speech_cleanup_choice,
            prior_item_status=(
                str(getattr(item, "item_status", None))
                if getattr(item, "item_status", None) is not None
                else None
            ),
        )


_DEVICE_EDIT_REFUSALS = {
    "baseline_conflict": (
        "The video changed on your iPhone after I drafted that edit, so I left it as it was. "
        "Ask me again and I'll redo the change on the current version."
    ),
    "unsupported_phone_edit": (
        "That change can't be rendered on your iPhone yet, so I left the video as it was."
    ),
    "phone_editor_media_unavailable": (
        "Adding that media isn't available for on-device edits yet, so I left the video as it was."
    ),
    "phone_rendering_unavailable": (
        "On-device rendering isn't available for this account right now, "
        "so I left the video as it was."
    ),
}
_DEVICE_EDIT_REFUSAL_FALLBACK = (
    "I couldn't apply that change on your iPhone, so I left the video as it was."
)


def _is_device_variant(job: Job | None, variant_id: str | None) -> bool:
    """Whether ``variant_id`` on ``job`` renders on the creator's iPhone (KRI-187)."""

    if job is None or not variant_id:
        return False
    return any(
        isinstance(row, dict)
        and row.get("variant_id") == variant_id
        and row.get("render_destination") == "device"
        for row in (job.assembly_plan or {}).get("variants") or []
    )


def _device_refusal_code(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        return detail["code"]
    return "unsupported_phone_edit" if isinstance(exc, ValueError) else "device_edit_rejected"


def _device_render_state(job: Job, execution: CreatorAgentExecution) -> str | None:
    """Settle an execution whose Job renders on the iPhone: pending/ready/failed.

    ``None`` means "not a device job" and leaves the cloud observer untouched.
    A device job sits in ``awaiting_device`` (a non-terminal Job status) until
    the phone publishes or the client/reaper marks the record
    ``needs_attention`` -- which does NOT move ``Job.status``, so failure has
    to be read off the pinned device record. The published variant carries the
    phone's upload attempt id as its ``render_generation_id`` (never the id the
    editor Save minted), so an editor execution is matched by recipe revision:
    a record older than the one this approval pinned is still pending.
    """

    records = (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD)
    if not isinstance(records, dict) or not records:
        return None
    variant_id = str(execution.target_variant_id or "") or None
    prep = (execution.result or {}).get("editor_prep")
    if isinstance(prep, dict) and "device_recipe_revision" not in prep:
        # An editor edit with no render section pinned no recipe revision: it
        # changed nothing the phone draws, so the device record (possibly an
        # older failure or publish) says nothing about it. Let the cloud
        # observer settle it as before.
        return None
    minimum = prep.get("device_recipe_revision") if isinstance(prep, dict) else None
    states: list[str] = []
    for record_variant in [variant_id] if variant_id else list(records):
        try:
            status = device_status(job, record_variant)
        except (KeyError, ValueError, TypeError):
            states.append("pending")
            continue
        if isinstance(minimum, int) and status.request.identity.recipe_revision < minimum:
            states.append("pending")
        elif status.phase == "published":
            states.append("ready")
        elif status.phase == "needs_attention":
            states.append("failed")
        else:
            states.append("pending")
    if "pending" in states:
        return "pending"
    return "failed" if "failed" in states else "ready"


_DETERMINISTIC_JOB_FAILURE_CODES = {"phone_plan_unsupported"}


def _phone_gate_refusal_copy(reason: str) -> str:
    from app.tasks.content_plan_build import PHONE_GATE_MESSAGES  # noqa: PLC0415

    message = PHONE_GATE_MESSAGES.get(reason, (None, None))[1]
    lead = message or "That kind of edit isn't available for iPhone renders yet."
    return f"{lead} Tell me what you'd like to change and I'll try a different approach."


# KRI-205: every `DispatchResult("speech_cleanup_*")` outcome
# `dispatch_item_render_for` can return (grep `content_plan_build.py` for
# `DispatchResult("speech_cleanup`) -- the decision `decide_approval` stashed
# (or the claim's own re-derived mutation) no longer matches what dispatch
# re-validates under its own fresh lock. This is never the generic "retry"
# dead loop this whole feature exists to close: the creator must approve
# again to answer the question, not resend the exact same request.
_SPEECH_CLEANUP_DISPATCH_REFUSALS: dict[str, str] = {
    # An approval is single-use, so each copy points at the failed card's
    # "Refresh project" button: it asks Kria for a fresh draft, whose new
    # approval runs the speech-cleanup gate against the current analysis.
    "speech_cleanup_analysis_conflict": (
        "The speech check changed before I could start. Tap Refresh project and "
        "I'll set it up again so you can choose how to handle the pauses."
    ),
    "speech_cleanup_recovery_conflict": (
        "The speech check changed before I could start. Tap Refresh project and "
        "I'll set it up again so you can choose how to handle the pauses."
    ),
    "speech_cleanup_unavailable": (
        "The speech check isn't available for this video yet. Tap Refresh project "
        "and I'll render it without cleanup."
    ),
    "speech_cleanup_unavailable_on_phone": (
        "Cleaning up speech isn't available for this iPhone edit yet. Tap Refresh "
        "project and choose to keep the original speech."
    ),
}

# KRI-217: the project's Visuals block this render. Resending the same approval
# refuses the same way, so these are never the generic "retry" copy either.
_VISUALS_DISPATCH_REFUSALS: dict[str, str] = {
    # The montage lane this edit renders on cannot place Visuals (a cloud
    # runtime-v2 montage, or a Visual kind the phone cannot draw yet).
    "guided_edit_bypass_unsafe": (
        "I can't put your Visuals into this montage yet, so I didn't start the "
        "render. Remove them from Visuals, then tap Refresh project and I'll make "
        "it from your videos."
    ),
    "visuals_processing": (
        "A photo or video you added to Visuals is still being prepared. Give it a "
        "moment, then tap Refresh project and I'll start the render."
    ),
}


_FINISH_DEADLOCK_ATTEMPTS = 3


def _is_deadlock(exc: BaseException) -> bool:
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

    if not isinstance(exc, DBAPIError):
        return False
    return (getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)) in {
        "40P01",
        "40001",
    }


def _finish_with_deadlock_retry(
    claim: _ApprovalDispatchClaim, **kwargs: Any
) -> tuple[str, str | None]:
    """`_finish_approval_dispatch` is idempotent (a dispatched execution returns early),
    so an aborted deadlock victim may simply be retried a bounded number of times."""
    import time  # noqa: PLC0415

    for attempt in range(_FINISH_DEADLOCK_ATTEMPTS):
        try:
            return _finish_approval_dispatch(claim, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not _is_deadlock(exc) or attempt == _FINISH_DEADLOCK_ATTEMPTS - 1:
                raise
            log.warning("kria_approval_finish_deadlock_retry", attempt=attempt + 1)
            time.sleep(0.15 * 2**attempt)
    raise AssertionError("unreachable")


def _compensate_failed_dispatch(claim: _ApprovalDispatchClaim, new_job_id: str) -> None:
    """Undo `dispatch_item_render_for`'s pointer move when the approval could not be settled.

    Only while the plan item still points at the Job this dispatch minted (anything newer
    wins). Restores the previous Job and item status and fails the orphan Job. Lock order:
    PlanItem -> Job (canonical).
    """
    new_id = uuid.UUID(str(new_job_id))
    with sync_session() as db:
        item = db.execute(
            select(PlanItem).where(PlanItem.id == claim.item_id).with_for_update()
        ).scalar_one_or_none()
        job = db.execute(select(Job).where(Job.id == new_id).with_for_update()).scalar_one_or_none()
        if item is None or job is None or item.current_job_id != new_id:
            return
        if claim.target_job_id is not None:
            item.current_job_id = claim.target_job_id
            if claim.prior_item_status is not None:
                item.item_status = claim.prior_item_status
        job.status = "failed"
        job.failure_reason = "kria_dispatch_aborted"
        db.commit()
        log.warning(
            "kria_dispatch_compensated",
            item_id=str(claim.item_id),
            orphan_job_id=str(new_id),
            restored_job_id=str(claim.target_job_id),
        )


def _finish_approval_dispatch(
    claim: _ApprovalDispatchClaim,
    *,
    outcome: str,
    job_id: str | None,
    reason: str | None = None,
) -> tuple[str, str | None]:
    successful = outcome in {"dispatched", "already_active"} and job_id is not None
    successor_id: str | None = None
    with sync_session() as db:
        if successful:
            # Canonical order (db_locks): PlanItem -> Job -> Session. Pointing the session and
            # the execution at the NEW Job takes a FOR KEY SHARE on that Job row through the
            # foreign key, and a status poll holds that Job FOR UPDATE while it waits for the
            # Session (`_lock_reconciliation_graph`): taking the Session first here made the
            # two a deadlock (2026-10-01, approval 3ac08f80). Lock the same rows in the same
            # order so the later FK check is a no-op.
            db.execute(select(PlanItem).where(PlanItem.id == claim.item_id).with_for_update())
            db.execute(select(Job).where(Job.id == uuid.UUID(str(job_id))).with_for_update())
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

        refusal_copy = _SPEECH_CLEANUP_DISPATCH_REFUSALS.get(
            outcome
        ) or _VISUALS_DISPATCH_REFUSALS.get(outcome)
        never_retry = refusal_copy is not None or bool(reason)
        execution.status = "failed"
        execution.error = {
            "code": "render_dispatch_failed",
            "outcome": outcome,
            "retryable": outcome == "publish_failed" and not never_retry,
            # A phone-gate refusal (`reason`), a speech-cleanup conflict or a
            # Visuals refusal refuses identically every time, so none may send
            # the creator into a retry loop -- each needs a fresh approval.
            "recovery": "ask_user" if never_retry else "retry",
            **({"reason": reason} if reason else {}),
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
                refusal_copy
                or (_phone_gate_refusal_copy(reason) if reason else None)
                or (
                    "I couldn't start the render. Your draft is still saved, "
                    "so you can retry without repeating the edit."
                )
            ),
            payload={
                "turn_id": str(turn.id),
                "approval_id": str(approval.id),
                "execution_id": str(execution.id),
                "job_id": job_id,
                "status": "failed",
                "code": "render_dispatch_failed",
                "dispatch_outcome": outcome,
                "recovery": "ask_user" if never_retry else "retry",
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

    dispatch_reason: str | None = None
    if getattr(claim, "draft_kind", "strategy") == "editor":
        if (
            claim.target_job_id is None
            or claim.target_variant_id is None
            or claim.editor_prep is None
        ):
            return {"approval_id": approval_id, "status": "ignored", "job_id": None}
        try:
            if claim.editor_prep.get("render_destination") == "device":
                # KRI-187: `prepare_phone_editor_commit` (inside the claim's
                # `prepare_editor_commit`) already pinned revision N+1 and set
                # the variant `awaiting_device`. There is no cloud task to
                # enqueue: the phone picks the recipe up by polling, and the
                # observer settles this execution from the device record.
                pass
            elif claim.editor_prep.get("speech_cut") is True:
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
            # KRI-187: a phone account has no approved guided proposal on this
            # path; the flag inside dispatch decides whether it may proceed to
            # the device montage compiler. Non-phone accounts ignore it.
            allow_phone_unapproved_montage=True,
            # Runtime-v2 has no creator choice surface for the speech-cleanup
            # card v1's chat route offers, so an undecided phone (analysis
            # proxy) narration source dispatches without cleanup instead of
            # refusing under the enforce guard (see the docstring on
            # `_dispatch_item_render` in content_plan_build.py).
            phone_speech_cleanup_unattended=True,
            creator_strategy=claim.strategy,
            creator_request=claim.creator_request,
            speech_cleanup_analysis_id=(
                str(claim.speech_cleanup_analysis_id)
                if claim.speech_cleanup_analysis_id is not None
                else None
            ),
            speech_cleanup_choice=claim.speech_cleanup_choice,
        )
        outcome = result.outcome
        result_job_id = result.job_id
        dispatch_reason = getattr(result, "reason", None)
    try:
        status, successor_id = _finish_with_deadlock_retry(
            claim,
            outcome=outcome,
            job_id=result_job_id,
            **({"reason": dispatch_reason} if dispatch_reason else {}),
        )
    except Exception:
        # The dispatch already minted the Job and moved the plan item to it. Leaving that
        # half-switched strands the project on an unrendered orphan (the app shows the old
        # video "gone"), and the reconcile sweep would then find the approval stale. Put the
        # pointer back, fail the new Job, and settle the approval as a retryable failure.
        log.exception("kria_approval_finish_failed", approval_id=approval_id, job_id=result_job_id)
        if outcome == "dispatched" and result_job_id is not None:
            _compensate_failed_dispatch(claim, result_job_id)
            status, successor_id = _finish_approval_dispatch(
                claim, outcome="publish_failed", job_id=None
            )
        else:
            raise
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


def _unified_montage_review(
    db: Any, thread: CreationThread, job: Job, default_text: str
) -> tuple[str, list[dict[str, Any]]]:
    """Review text and receipts for a unified montage (KRI-190), or the default.

    The receipts were computed by the render worker from what it actually put in
    the plan (`plan_facts_from_unified_montage`), so the reply can only say what
    was checked: what was met, what was partial and what could not be done. With
    the Creative Brief off, no unified record, or nothing to report, this is the
    unchanged default review.
    """

    record = (job.assembly_plan or {}).get("unified_montage")
    if not isinstance(record, dict) or not record.get("requirement_receipts"):
        return default_text, []
    if not settings.creative_brief_for(thread.creator_id):
        return default_text, []
    brief = load_latest_brief_sync(db, thread.id)
    if brief is None:
        return default_text, []
    if record.get("brief_version") != brief.version:
        # The brief changed after the plan was made: its receipts describe the
        # old wording, so say nothing about them.
        return default_text, []
    from app.kria.contracts import RequirementReceipt  # noqa: PLC0415

    receipts = []
    for raw in record["requirement_receipts"]:
        try:
            receipts.append(RequirementReceipt.model_validate(raw))
        except ValueError:
            continue
    live = {req.id: req for req in brief.live()}
    # A record planned before unjudged receipts were dropped can still carry some.
    receipts = [r for r in receipts if is_judged(live.get(r.requirement_id), r)]
    if not receipts:
        return default_text, []
    checked = CreativeBrief(
        version=brief.version,
        requirements=[live[receipt.requirement_id] for receipt in receipts],
    )
    return (
        reply_from_receipts(checked, receipts, summary=default_text),
        [receipt.model_dump(mode="json") for receipt in receipts],
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
            select(ContentPlan)
            .where(ContentPlan.id == item_ref.content_plan_id)
            .with_for_update(**CONTENT_PLAN_LOCK)
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
        # A device render is `awaiting_device` (never terminal) until the phone
        # publishes; its failure lives on the device record, not on Job.status.
        device_state = _device_render_state(job, execution)
        if device_state == "pending":
            return "pending", None
        if device_state == "failed":
            terminal = True
        if not terminal:
            return "pending", None

        now = datetime.now(UTC)
        variant_failure_code: str | None = (
            "device_render_failed" if device_state == "failed" else None
        )
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
                # The phone publishes under its own upload-attempt id, so a
                # published device render is matched by revision (above).
                generation_id=None if device_state == "ready" else pinned_generation_id,
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
                review_text, review_receipts = _unified_montage_review(
                    db,
                    thread,
                    job,
                    f"The {variant_id.replace('_', ' ')} cut is ready. "
                    "The approved render finished; review the opening, pacing, and text, "
                    "then tell me what you want changed.",
                )
                review = _append_sync_event(
                    db,
                    thread,
                    role="assistant",
                    event_type="assistant_review",
                    content=review_text,
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
                        **({"requirement_receipts": review_receipts} if review_receipts else {}),
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
        device_failed = device_state == "failed"
        # Chat retry cannot recover a device render (the Job is still
        # `awaiting_device`); the creator retries from the phone's render panel.
        # Deterministic compiler rejects fail identically on every retry: ask
        # the creator for a change instead of offering a dead retry loop.
        deterministic = not device_failed and failure_code in _DETERMINISTIC_JOB_FAILURE_CODES
        recovery = "manual" if device_failed else ("ask_user" if deterministic else "retry")
        execution.error = {
            "code": failure_code,
            "retryable": not device_failed and not deterministic,
            "recovery": recovery,
        }
        turn.status = "failed"
        turn.completed_at = now
        turn.error = execution.error
        session.status = "awaiting_feedback"
        session.last_error = execution.error
        from app.tasks.content_plan_build import humanize_job_failure_reason  # noqa: PLC0415

        if device_failed:
            failure_content = (
                "Your iPhone couldn't finish the render. Your approved edit is still saved: "
                "open the project on your iPhone and tap Retry."
            )
        elif deterministic:
            failure_content = humanize_job_failure_reason(failure_code)
        else:
            failure_content = (
                "That render didn't finish. Your approved draft is still saved, "
                "so you can retry without rebuilding the edit."
            )
        event = _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_render_failed",
            content=failure_content,
            payload={
                "turn_id": str(turn.id),
                "execution_id": str(execution.id),
                "job_id": str(job.id),
                "status": "failed",
                "code": failure_code,
                "recovery": recovery,
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
    # One sweep at a time: beat can fire again (or a local helper loop can run) while the
    # previous sweep is still observing renders. A second sweep would only republish the
    # same approvals and fight the first over the same rows, so skip it.
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import sync_engine  # noqa: PLC0415

    with sync_engine.connect() as lock_conn:
        got = lock_conn.execute(
            text("select pg_try_advisory_lock(:key)"), {"key": _RECONCILE_ADVISORY_KEY}
        ).scalar()
        if not got:
            log.info("kria_reconcile_skipped_overlap")
            return {"published": 0, "settled": 0, "skipped": 1}
        try:
            result = _reconcile_kria_turns_body()
            try:
                for successor_id in _expire_pending_approvals():
                    run_kria_turn.apply_async(
                        args=[successor_id], task_id=successor_id, queue="agent-control"
                    )
            except Exception:  # noqa: BLE001 - expiry is best-effort; the next sweep retries
                log.warning("kria_approval_expiry_sweep_failed", exc_info=True)
            return result
        finally:
            lock_conn.execute(
                text("select pg_advisory_unlock(:key)"), {"key": _RECONCILE_ADVISORY_KEY}
            )
            lock_conn.commit()


# Arbitrary, stable advisory-lock key for the reconcile sweep ("kria rec" in hex).
_RECONCILE_ADVISORY_KEY = 0x4B52494152454300


def _expire_pending_approvals() -> list[str]:
    """Cancel approvals past `expires_at` that nobody decided (they would block the thread
    forever), release the queued follow-up, and say so. Returns successor turn ids to publish.

    Only approvals with nothing to reverse are swept here: a strategy approval whose
    execution carries a committed media mutation (`strategy_media_before`) keeps its
    lazy path: the next approve/deny OR the next `submit_turn` (KRI-295,
    `runtime._expire_blocking_approval`) restores the item and expires the approval, so such an
    approval can no longer deadlock the thread. The sweep stays sync-only on purpose (the
    restore helpers are async).
    """
    successors: list[str] = []
    with sync_session() as db:
        pending = list(
            db.execute(
                select(CreatorAgentApproval.id)
                .where(
                    CreatorAgentApproval.status == "pending",
                    CreatorAgentApproval.expires_at < func.now(),
                )
                .order_by(CreatorAgentApproval.expires_at)
                .limit(25)
            ).scalars()
        )
    for approval_id in pending:
        try:
            with sync_session() as db:
                ref = db.get(CreatorAgentApproval, approval_id)
                if ref is None:
                    continue
                # Canonical order (subset): Session -> Turn -> Approval -> Execution -> Thread.
                session = db.execute(
                    select(CreatorAgentSession)
                    .where(CreatorAgentSession.id == ref.session_id)
                    .with_for_update()
                ).scalar_one_or_none()
                turn = db.execute(
                    select(CreatorAgentTurn)
                    .where(CreatorAgentTurn.id == ref.turn_id)
                    .with_for_update()
                ).scalar_one_or_none()
                approval = db.execute(
                    select(CreatorAgentApproval)
                    .where(CreatorAgentApproval.id == approval_id)
                    .with_for_update()
                ).scalar_one_or_none()
                execution = None
                try:
                    execution_id = uuid.UUID(str((ref.execution_ids or [None])[0]))
                    execution = db.execute(
                        select(CreatorAgentExecution)
                        .where(CreatorAgentExecution.id == execution_id)
                        .with_for_update()
                    ).scalar_one_or_none()
                except (TypeError, ValueError, IndexError):
                    execution = None
                thread = db.execute(
                    select(CreationThread)
                    .where(CreationThread.id == ref.thread_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if (
                    session is None
                    or turn is None
                    or approval is None
                    or thread is None
                    or approval.status != "pending"
                ):
                    continue
                if isinstance(getattr(execution, "result", None), dict) and (
                    "strategy_media_before" in execution.result
                ):
                    continue
                now = datetime.now(UTC)
                error = {
                    "code": "approval_expired",
                    "retryable": False,
                    "recovery": "refresh_replan",
                }
                approval.status = "expired"
                if execution is not None and execution.status == "awaiting_approval":
                    execution.status = "stale"
                    execution.error = error
                    execution.completed_at = now
                if turn.status == "awaiting_approval":
                    turn.status = "failed"
                    turn.completed_at = now
                    turn.error = error
                if session.status != "rendering":
                    session.status = "awaiting_feedback"
                _append_sync_event(
                    db,
                    thread,
                    role="assistant",
                    event_type="assistant_error",
                    content=(
                        "That approval expired before it was decided, so nothing was rendered. "
                        "Tell me what you want and I'll prepare it again."
                    ),
                    payload={
                        "turn_id": str(turn.id),
                        "approval_id": str(approval.id),
                        "code": "approval_expired",
                        "recovery": "refresh_replan",
                    },
                )
                db.commit()
                successor = _promote_queued_successor_sync(thread.id)
                if successor is not None:
                    successors.append(successor)
        except Exception:  # noqa: BLE001 - one bad approval must not stop the sweep
            log.warning("kria_approval_expiry_failed", approval_id=str(approval_id), exc_info=True)
    return successors


def _reconcile_kria_turns_body() -> dict[str, int]:
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
            if turn.status == "planning":
                # Its lease lapsed: the run was killed at the task's time limit
                # or lost its worker. A pending row only waited for delivery.
                turn.abandoned_claims = int(turn.abandoned_claims or 0) + 1
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
        # Empty editor drafts are pinned by a Job variant rather than an
        # approval. A subsequent ordinary draft can make that row non-head;
        # pruning its snapshot would leave the empty editor with no canonical
        # restore source. Inspect only candidate ids and keep those references.
        candidate_ids = {str(row.id) for row in candidates}
        candidate_job_ids = {row.base_job_id for row in candidates if row.base_job_id is not None}
        assemblies = db.execute(
            select(Job.assembly_plan).where(Job.id.in_(candidate_job_ids))
        ).scalars()
        for assembly in assemblies:
            for variant in (assembly or {}).get("variants") or []:
                reference = variant.get("editor_draft") if isinstance(variant, dict) else None
                draft_id = reference.get("draft_id") if isinstance(reference, dict) else None
                if str(draft_id) in candidate_ids:
                    try:
                        protected.add(uuid.UUID(str(draft_id)))
                    except (TypeError, ValueError):
                        continue
        pruned = 0
        for draft in candidates:
            if draft.id in protected:
                continue
            draft.snapshot_json = None
            pruned += 1
        db.commit()
        return {"pruned": pruned}
