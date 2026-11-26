"""Keep or drop the sourced facts of a research draft before approving it (CA-5, KRI-1644).

A research draft's preview names each shown fact (``fact_id``) and the research it belongs to
(``selection_id``, the research ``basis_digest``). A turn carrying ``research_selection`` saves
the same draft again with only the kept facts pinned in its approval binding, cancels the
pending approval and asks for a new one, inside this request: no planner, no search, no model.

The binding digest covers the pinned research, so a narrowed draft is a new revision and a new
approval, never a rewrite of the one the creator was shown. Selections only narrow: a fact the
current draft does not show is ignored, so the same selection posted again writes nothing.
Keeping none pins a waived result, so the render shows no sourced text and never researches.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db_locks import acquire_key_share_rows
from app.kria import research_preview
from app.kria.api_schemas import SubmitTurnBody, TurnAccepted
from app.kria.brief import load_latest_brief
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import canonical_snapshot, staged_tap_footprint
from app.kria.reply_language import reply_language_for, say, thread_reply_language
from app.kria.runtime import (
    RuntimeFailure,
    _append_event,
    _lock_strategy_plan_item,
    _owned_thread,
    _parse_draft_document,
)
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.schemas.research import PreparedResearch
from app.services.client_protocol import current_client_protocol

STALE = "research_selection_stale"
# On the selection's user event: the event whose turn researched the facts, for the next one.
SOURCE_KEY = "research_source_event_id"


def _stale(message: str) -> RuntimeFailure:
    return RuntimeFailure(409, STALE, message, recovery="refresh_replan")


async def _research_source(db: AsyncSession, turn: CreatorAgentTurn) -> CreationThreadEvent | None:
    source = await db.get(CreationThreadEvent, turn.source_event_id)
    origin = (source.payload or {}).get(SOURCE_KEY) if source is not None else None
    return await db.get(CreationThreadEvent, uuid.UUID(origin)) if origin else source


def _reply(kept: PreparedResearch, total: int) -> str:
    if kept.waived:
        return say(
            en="Continuing without the sourced facts.",
            tr="Kaynaklı bilgiler olmadan devam ediyorum.",
        )
    head = say(
        en="Kept {kept} of {total} sourced facts.",
        tr="{total} kaynaklı bilginin {kept} tanesini tuttum.",
        kept=len(research_preview.fact_ids(kept)),
        total=total,
    )
    return f"{head}\n{research_preview.preview_lines(kept)}"


def _summary(old: str, pinned: PreparedResearch, kept: PreparedResearch) -> str:
    """The card's text with the shown research swapped for the kept facts, so a dropped fact is
    never on the new card."""
    shown = research_preview.preview_lines(pinned)
    if shown in old:
        return old.replace(shown, "" if kept.waived else research_preview.preview_lines(kept))
    dropped = {row.text for row in pinned.texts} - {row.text for row in kept.texts}
    return "\n".join(
        line for line in old.split("\n") if not any(f"“{text}”" in line for text in dropped)
    )


async def submit_research_selection(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    body: SubmitTurnBody,
    digest: str,
) -> tuple[TurnAccepted, bool]:
    selection = body.research_selection
    assert selection is not None
    protocol = current_client_protocol()
    if protocol is None or protocol < settings.research_selection_min_client_protocol:
        raise RuntimeFailure(
            426, "client_update_required", "Update Kria to choose which facts to keep."
        )
    replay = (
        await db.execute(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.thread_id == thread_id,
                CreatorAgentTurn.client_event_id == body.client_event_id,
            )
        )
    ).scalar_one_or_none()
    thread_peek = await _owned_thread(db, thread_id=thread_id, creator_id=creator_id, lock=False)
    if replay is not None:
        if replay.request_digest != digest:
            raise RuntimeFailure(
                409,
                "idempotency_key_reused",
                "That message identity was already used for different content.",
                recovery="refresh_replan",
            )
        return _accepted(replay, thread_peek, replayed=True), False

    approval_ref = (
        await db.execute(
            select(CreatorAgentApproval)
            .where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.creator_id == creator_id,
                CreatorAgentApproval.status == "pending",
            )
            .order_by(CreatorAgentApproval.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    draft_ref = (
        await db.get(CreatorEditDraft, approval_ref.draft_id)
        if approval_ref is not None and approval_ref.draft_id is not None
        else None
    )
    document = _parse_draft_document(getattr(draft_ref, "snapshot_json", None))
    binding = getattr(document, "brief_binding", None)
    pinned = (
        PreparedResearch.model_validate(binding.prepared_research)
        if binding is not None and binding.prepared_research is not None
        else None
    )
    if (
        draft_ref is None
        or not draft_ref.is_head
        or pinned is None
        or pinned.basis_digest != selection.selection_id
    ):
        raise _stale("These facts belong to research this draft no longer uses.")
    old_turn = await db.get(CreatorAgentTurn, approval_ref.turn_id)
    if pinned.waived and not selection.kept_fact_ids:
        return _accepted(old_turn, thread_peek, replayed=True), False
    if not research_preview.selectable(pinned):
        raise _stale("These facts belong to research this draft no longer uses.")
    item_peek = await db.get(PlanItem, draft_ref.item_id)
    source = await _research_source(db, old_turn)
    language = thread_reply_language(thread_peek)
    current = await db.run_sync(
        lambda sync_db: research_preview.selection_basis_sync(
            sync_db,
            document=document,
            item=item_peek,
            creator_id=creator_id,
            source_event=source,
            language=language,
        )
    )
    if current != selection.selection_id:
        raise _stale("The footage changed after these facts were checked.")
    kept = research_preview.keep(pinned, selection.kept_fact_ids)
    if selection.kept_fact_ids and kept.waived:
        # Only an empty list waives; ids from an older card that match nothing are stale.
        raise _stale("None of these facts are in this draft any more.")
    if kept.texts == pinned.texts:
        # Nothing narrows: the approval the creator is looking at already pins these facts.
        return _accepted(old_turn, thread_peek, replayed=True), False

    # Canonical lock order: Plan -> PlanItem -> Job -> Session -> Turn -> Draft -> Approval ->
    # Execution -> Thread, as `decide_approval` takes it; then re-check what discovery read.
    item = await _lock_strategy_plan_item(
        db, session_id=approval_ref.session_id, creator_id=creator_id
    )
    if item is not None:
        await db.refresh(item)
    await acquire_key_share_rows(db, {Job: thread_peek.active_job_id})
    session = (
        await db.execute(
            select(CreatorAgentSession)
            .where(CreatorAgentSession.id == approval_ref.session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    turn = (
        await db.execute(
            select(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == approval_ref.turn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    draft = (
        await db.execute(
            select(CreatorEditDraft)
            .where(CreatorEditDraft.id == draft_ref.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    approval = (
        await db.execute(
            select(CreatorAgentApproval)
            .where(CreatorAgentApproval.id == approval_ref.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    render = (
        await db.execute(
            select(CreatorAgentExecution)
            .where(CreatorAgentExecution.id == uuid.UUID(str(approval.execution_ids[0])))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    thread = await _owned_thread(db, thread_id=thread_id, creator_id=creator_id, lock=True)
    now = datetime.now(UTC)
    if (
        approval.status != "pending"
        or approval.expires_at <= now
        or not draft.is_head
        or draft.snapshot_hash != draft_ref.snapshot_hash
        or render.status != "awaiting_approval"
        or item is None
    ):
        raise _stale("This draft changed before your choice arrived.")
    if "strategy_media_before" in (render.result or {}) or await staged_tap_footprint(db, approval):
        raise _stale("This draft changed before your choice arrived.")
    if (
        await db.execute(
            select(CreatorAgentTurn.id).where(
                CreatorAgentTurn.thread_id == thread.id, CreatorAgentTurn.status == "queued"
            )
        )
    ).first() is not None:
        raise RuntimeFailure(
            409,
            "queued_successor_exists",
            "A follow-up is already waiting. Approve or dismiss the pending render first.",
            recovery="refresh_replan",
            current_revision=int(thread.revision),
        )

    with reply_language_for(thread_reply_language(thread)):
        return await _mint(
            db,
            thread=thread,
            session=session,
            old_turn=turn,
            draft=draft,
            approval=approval,
            render=render,
            item=item,
            document=document,
            binding=binding,
            pinned=pinned,
            kept=kept,
            source=source,
            body=body,
            digest=digest,
            protocol=protocol,
            now=now,
        )


def _accepted(turn: CreatorAgentTurn, thread: CreationThread, *, replayed: bool) -> TurnAccepted:
    return TurnAccepted(
        turn_id=str(turn.id),
        thread_revision=int(thread.revision),
        status=turn.status,
        replayed=replayed,
    )


async def _readiness(
    db: AsyncSession, *, thread: Any, item: Any, document: Any, binding: Any, research: dict
) -> dict | None:
    """The saved readiness re-assessed on the kept research, or None when none was saved.

    The approval re-runs this exact assessment; only the research may move, so the brief,
    media and capability digests must equal the saved ones."""
    from app.kria.approval_readiness import (  # noqa: PLC0415
        READINESS_KEY,
        assess_readiness,
        resolve_readiness_manifest,
        saved_protected_marks,
    )
    from app.services.choice_questions import answered_brief  # noqa: PLC0415

    saved = (document.brief_coverage or {}).get(READINESS_KEY)
    if saved is None:
        return None
    fresh = assess_readiness(
        answered_brief(await load_latest_brief(db, thread.id), document.strategy),
        document.strategy,
        media=binding.media_snapshot,
        manifest=await resolve_readiness_manifest(db, item, document.strategy),
        editor_payload=document.editor_payload,
        editor_text_diff=document.editor_text_diff,
        creator_request=binding.creator_request,
        protected_regions=list(binding.protected_regions or []),
        protected_marks=saved_protected_marks(
            document, creator_id=thread.creator_id, item=item, saved=saved
        ),
        prepared_collection_story=document.prepared_collection_story,
        creator_id=thread.creator_id,
        prepared_research=research,
    )
    if not fresh["ready"] or any(
        fresh.get(key) != saved.get(key)
        for key in ("brief_version", "brief_digest", "media_digest", "capabilities_digest")
    ):
        raise _stale("This draft can no longer be approved with these facts.")
    return fresh


async def _mint(
    db: AsyncSession,
    *,
    thread: CreationThread,
    session: CreatorAgentSession,
    old_turn: CreatorAgentTurn,
    draft: CreatorEditDraft,
    approval: CreatorAgentApproval,
    render: CreatorAgentExecution,
    item: Any,
    document: Any,
    binding: BriefBinding,
    pinned: PreparedResearch,
    kept: PreparedResearch,
    source: CreationThreadEvent | None,
    body: SubmitTurnBody,
    digest: str,
    protocol: int,
    now: datetime,
) -> tuple[TurnAccepted, bool]:
    """Cancel the pending approval and save the narrowed draft with its own approval, in one
    transaction. Every lock is held by the caller, down to the Thread."""
    from app.kria.approval_readiness import READINESS_KEY  # noqa: PLC0415
    from app.tasks.kria_runtime import _APPROVAL_TTL  # noqa: PLC0415

    research = kept.model_dump(mode="json")
    rebound = binding.model_copy(
        update={
            "prepared_research": research,
            "digest": BriefBinding._digest(
                binding.thread_id,
                binding.state,
                binding.brief,
                binding.creator_request,
                binding.media_snapshot,
                binding.choice_answers,
                binding.protected_regions,
                research,
            ),
        }
    )
    readiness = await _readiness(
        db, thread=thread, item=item, document=document, binding=binding, research=research
    )
    coverage = dict(document.brief_coverage or {})
    if readiness is not None:
        coverage[READINESS_KEY] = readiness
    snapshot, snapshot_hash = canonical_snapshot(
        document.model_copy(update={"brief_binding": rebound, "brief_coverage": coverage or None})
    )
    kept_ids = research_preview.fact_ids(kept)
    receipt = {
        "selection_id": pinned.basis_digest,
        "kept_fact_ids": kept_ids,
        "waived": kept.waived,
    }

    approval.status = "cancelled"
    render.status = "stale"
    render.error = {"code": STALE, "retryable": False, "recovery": "refresh_replan"}
    render.completed_at = now
    old_turn.status = "superseded"
    old_turn.completed_at = now
    draft.is_head = False
    await db.flush()

    turn_id = uuid.uuid4()
    user_event = await _append_event(
        db,
        thread,
        role="user",
        event_type="user_message",
        content=None,
        payload={
            "request_digest": digest,
            "runtime_version": 2,
            "turn_id": str(turn_id),
            "turn_status": "awaiting_approval",
            "inert": True,
            "client_protocol": protocol,
            "research_selection": receipt,
            **({SOURCE_KEY: str(source.id)} if source is not None else {}),
        },
        client_event_id=body.client_event_id,
    )
    turn = CreatorAgentTurn(
        id=turn_id,
        thread_id=thread.id,
        session_id=session.id,
        source_event_id=user_event.id,
        client_event_id=body.client_event_id,
        request_digest=digest,
        status="awaiting_approval",
        plan_json=old_turn.plan_json,
    )
    db.add(turn)
    await db.flush()
    apply_intent, render_intent = (old_turn.plan_json or {}).get("intents", [{}, {}])[:2]
    revision = (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(CreatorEditDraft.draft_revision), -1)).where(
                        CreatorEditDraft.item_id == draft.item_id,
                        CreatorEditDraft.variant_key == draft.variant_key,
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    applied = CreatorAgentExecution(
        session_id=session.id,
        turn_id=turn.id,
        idempotency_key=f"kria:{turn.id}:{apply_intent.get('intent_id')}",
        request_digest=digest,
        expected_revision=int(session.revision),
        expected_manifest_hash=render.expected_manifest_hash,
        tool_name=apply_intent.get("tool_name"),
        tool_version=apply_intent.get("tool_version"),
        risk="reversible_draft",
        dependency_group=0,
        group_order=0,
        target_thread_id=thread.id,
        status="completed",
        result={"snapshot_hash": snapshot_hash, "research_selection": receipt},
        started_at=now,
        completed_at=now,
    )
    db.add(applied)
    await db.flush()
    row = CreatorEditDraft(
        creator_id=thread.creator_id,
        thread_id=thread.id,
        item_id=draft.item_id,
        variant_key=draft.variant_key,
        base_job_id=draft.base_job_id,
        base_generation_id=draft.base_generation_id,
        draft_revision=revision,
        parent_draft_id=draft.id,
        snapshot_json=snapshot,
        snapshot_hash=snapshot_hash,
        source_execution_id=applied.id,
        is_head=True,
    )
    db.add(row)
    await db.flush()
    applied.target_draft_id = row.id
    applied.target_draft_revision = row.draft_revision
    applied.result = {**applied.result, "draft_id": str(row.id), "draft_revision": revision}
    rendering = CreatorAgentExecution(
        session_id=session.id,
        turn_id=turn.id,
        idempotency_key=f"kria:{turn.id}:{render_intent.get('intent_id')}",
        request_digest=digest,
        expected_revision=int(session.revision),
        expected_manifest_hash=render.expected_manifest_hash,
        tool_name=render.tool_name,
        tool_version=render.tool_version,
        risk="approval_required",
        dependency_group=1,
        group_order=1,
        target_thread_id=thread.id,
        target_draft_id=row.id,
        target_draft_revision=revision,
        target_job_id=render.target_job_id,
        target_variant_id=render.target_variant_id,
        target_generation_id=render.target_generation_id,
        target_manifest_hash=render.target_manifest_hash,
        target_ownership_epoch=render.target_ownership_epoch,
        status="awaiting_approval",
        result={
            **{key: value for key, value in (render.result or {}).items() if key != "approval_id"},
            **({"approval_readiness_digest": readiness["digest"]} if readiness else {}),
        },
        started_at=now,
        awaiting_approval_at=now,
    )
    db.add(rendering)
    await db.flush()
    renewed = CreatorAgentApproval(
        creator_id=thread.creator_id,
        thread_id=thread.id,
        session_id=session.id,
        turn_id=turn.id,
        draft_id=row.id,
        draft_revision=revision,
        target_job_id=approval.target_job_id,
        target_variant_id=approval.target_variant_id,
        target_generation_id=approval.target_generation_id,
        target_manifest_hash=approval.target_manifest_hash,
        target_ownership_epoch=approval.target_ownership_epoch,
        execution_ids=[str(rendering.id)],
        consequence_summary=_summary(approval.consequence_summary or "", pinned, kept),
        cost_summary=approval.cost_summary,
        status="pending",
        expires_at=now + _APPROVAL_TTL,
    )
    db.add(renewed)
    await db.flush()
    rendering.result = {**rendering.result, "approval_id": str(renewed.id)}

    # Both clients close a card only on an approval_* event naming it.
    await _append_event(
        db,
        thread,
        role="system",
        event_type="approval_cancelled",
        content=None,
        payload={"approval_id": str(approval.id), "turn_id": str(old_turn.id)},
    )
    reply = await _append_event(
        db,
        thread,
        role="assistant",
        event_type="draft_applied",
        content=_reply(kept, len(research_preview.fact_ids(pinned))),
        payload={
            "turn_id": str(turn.id),
            "draft_id": str(row.id),
            "draft_revision": revision,
            "snapshot_hash": snapshot_hash,
            "changes": list(document.changes or []),
            "can_undo": True,
            "receipt_ids": [str(applied.id)],
            "research_selection": receipt,
            **(
                {"research_preview": research_preview.preview_payload(kept, with_ids=True)}
                if not kept.waived
                else {}
            ),
        },
    )
    await _append_event(
        db,
        thread,
        role="system",
        event_type="approval_requested",
        content=None,
        payload={
            "turn_id": str(turn.id),
            "approval_id": str(renewed.id),
            "draft_id": str(row.id),
            "draft_revision": revision,
            "consequence_summary": renewed.consequence_summary,
            "cost_summary": renewed.cost_summary,
            "expires_at": renewed.expires_at.isoformat(),
            "artifact_key": f"approval:{renewed.id}",
        },
    )
    turn.observed_event_id = reply.id
    await db.commit()
    return _accepted(turn, thread, replayed=False), False
