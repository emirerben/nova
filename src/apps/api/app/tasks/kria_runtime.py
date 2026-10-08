"""Durable runtime-v2 planning, editing, approval, and observation tasks.

Every consequential render remains separated from draft creation by a pinned,
persisted approval. Workers report only durable receipts and observed Job truth.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from contextlib import ExitStack
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
    NARRATED_ALIGNMENT_FIELD,
    build_receipts,
    is_judged,
    needs_creator_choice,
    plan_facts_from_editor_payload,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.contracts import KriaObservedTurnResponse, KriaToolReceipt, KriaTurnPlan
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.language import is_paraphrase_only
from app.kria.planner import (
    PlannedKriaTurn,
    extract_deferred_brief,
    localized_editor_state_reply,
    plan_live_turn,
    turn_deadline,
)
from app.kria.registry import KRIA_TOOLS
from app.kria.reply_language import (
    bind_reply_language,
    current_reply_language,
    release_reply_language,
    reply_language_for,
    say,
    thread_reply_language,
)
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
from app.services.choice_questions import (
    CONFLICT_ORDER_BASIS,
    CONFLICT_TITLE_TEXT,
    CONFLICT_WHICH_VOICE,
    KEEP_OPEN_REASON,
    MAX_ASKS_PER_QUESTION,
    ChoiceCapability,
    answered_brief,
    build_choice_question,
    choice_question_text,
    count_asks,
    open_conflicts,
    tag_event,
)
from app.services.creative_copy_gate import CREATIVE_COPY_PENDING, creative_copy_problem
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
    response_only: bool = False


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
    brief_binding: dict[str, Any] | None = None
    preflight_analysis_id: uuid.UUID | None = None
    speech_cleanup_analysis_id: uuid.UUID | None = None
    speech_cleanup_choice: str | None = None
    # KRI-306: the creator's explicit output-shape choice (`{"output_orientation",
    # "landscape_fit"}`), stashed by `decide_approval`; None = they never chose.
    render_shape: dict[str, str] | None = None
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
        # KRI-520: the chat's language; the turn binds it for its server copy.
        # Absent until a message told, so older snapshots are unchanged.
        **({"reply_language": language} if (language := thread_reply_language(thread)) else {}),
    }


def _complete_response_turn(
    turn_id: uuid.UUID,
    *,
    lease_owner: str,
    lease_epoch: int,
    claimed_thread_revision: int,
    plan: KriaTurnPlan,
    brief_updates: tuple[BriefUpdate, ...] = (),
    brief_coverage: dict | None = None,
    brief_expected_version: int | None = None,
    requirement_receipts: list[dict[str, Any]] | None = None,
    creative_copy_resolution: dict | None = None,
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
        if brief_expected_version is not None:
            current_brief = load_latest_brief_sync(db, thread.id)
            if brief_expected_version != (current_brief.version if current_brief else 0):
                turn.status = "pending"
                turn.lease_owner = None
                turn.lease_expires_at = None
                db.commit()
                return _Completion(committed=False, requeue_turn_id=str(turn.id))
        persisted_brief = None
        if brief_updates:
            # KRI-188: a question/recovery turn still records what the creator
            # stated. Written under the thread lock, after the revision fence,
            # so a requeued turn never persists a version.
            persisted_brief = persist_brief_version_sync(
                db, thread_id=thread.id, turn_id=turn.id, updates=brief_updates
            )
        if requirement_receipts and persisted_brief is not None:
            # A response/recovery has no produced output generation.  Pin its
            # diagnostics to the version it just persisted so a later edit of
            # the same stable requirement cannot inherit this outcome.
            requirement_receipts = [
                {**receipt, "brief_version": persisted_brief.version, "generation_id": None}
                for receipt in requirement_receipts
            ]
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
                **({"requirement_receipts": requirement_receipts} if requirement_receipts else {}),
                **({"brief_coverage": brief_coverage} if brief_coverage is not None else {}),
                **({"clip_question": plan.clip_question} if plan.clip_question else {}),
                # KRI-374: persisted so the answer can be validated + folded later.
                **(
                    {"song_order_question": plan.song_order_question.model_dump(mode="json")}
                    if plan.song_order_question is not None
                    else {}
                ),
                **({"choice_question": plan.choice_question} if plan.choice_question else {}),
                **(
                    {"creative_copy_resolution": creative_copy_resolution}
                    if creative_copy_resolution
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


def _bind_thread_language(stack: ExitStack, thread: CreationThread) -> None:
    """KRI-520: write the rest of this transaction's creator-visible copy in the chat's language.

    ``stack`` rides in the function's own ``with`` header, so the language is released
    on every exit path without re-indenting the long, lock-ordered body. The thread row
    is already loaded: this adds no query.
    """
    stack.enter_context(reply_language_for(thread_reply_language(thread)))


_PACING_TR = {"relaxed": "Sakin", "balanced": "Dengeli", "fast": "Hızlı"}
_EDIT_FORMAT_TR = {
    "montage": "Montaj",
    "talking_head": "Kameraya konuşma",
    "day_vlog": "Günlük vlog",
    "single_hero": "Tek kahraman klip",
    "subtitled": "Altyazılı",
    "narrated": "Seslendirmeli",
    "narrated_planned": "Seslendirmeli",
    "narrated_ready": "Seslendirmeli",
    "slides": "Slayt",
}


def _strategy_changes(arguments: Any) -> list[str]:
    strategy = arguments.strategy
    pacing = strategy.pacing.replace("_", " ").title()
    edit_format = strategy.edit_format.replace("_", " ").title()
    values = [
        arguments.summary,
        say(en=f"{pacing} pacing", tr=f"Tempo: {_PACING_TR.get(strategy.pacing, pacing)}"),
        say(
            en=f"{edit_format} format",
            tr=f"Format: {_EDIT_FORMAT_TR.get(strategy.edit_format, edit_format)}",
        ),
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
                "response": say(
                    en=(
                        "I need one concrete creative choice before I can make a useful edit "
                        "decision. Which moment should viewers remember?"
                    ),
                    tr=(
                        "Anlamlı bir düzenleme kararı verebilmem için net bir yaratıcı seçime "
                        "ihtiyacım var. İzleyenler hangi anı hatırlamalı?"
                    ),
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
                arguments["summary"] = say(
                    en=f"I prepared {count} reversible editor change{suffix}.",
                    tr=f"Editörde {count} geri alınabilir değişiklik hazırladım.",
                )
            else:
                strategy = arguments.get("strategy") or {}
                pacing = str(strategy.get("pacing") or "focused").replace("_", " ")
                edit_format = str(strategy.get("edit_format") or "video").replace("_", " ")
                arguments["summary"] = say(
                    en=f"I prepared a {pacing} {edit_format} draft around the available footage.",
                    # The pace and format words are machine values (English), so the
                    # Turkish line leaves them out rather than mixing languages.
                    tr="Elindeki çekimlere göre bir taslak hazırladım.",
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


def _unresolved_choice_plan(
    strategy: Mapping[str, Any] | None,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    *,
    contract_brief: Any = None,
    events: Any = (),
    has_draft: bool = False,
    creator_id: Any = None,
) -> tuple[KriaTurnPlan, str] | None:
    """KRI-476 backstop: the reply that replaces a draft an unresolved choice blocks.

    A draft must never be approvable while the requirement contract it will pin still
    has an open question (askable -> the same tappable question the planner gate asks)
    or an unresolved item (not askable -> a plain refusal). Both used to surface only
    when the render was dispatched. ``None`` = clear; otherwise ``(plan, reason)``.

    A question already asked ``MAX_ASKS_PER_QUESTION`` times is never asked again and
    never answered for the creator: a length question lets the plan through (the receipts
    state what is unmet), an order question becomes ONE plain message that says what
    cannot be checked and quotes the supported ways forward, which a typed reply still
    answers (``unresolved_choice`` keeps the question open).

    ``contract_brief`` is the brief the dispatch-time contract will actually read: only a
    creator with a brief binding has one, so the brief-derived ``unresolved`` items are
    evaluated for that cohort alone.
    """

    from app.config import settings  # noqa: PLC0415
    from app.services.creator_render_contract import (  # noqa: PLC0415
        CreatorRenderContractError,
        build_render_contract,
        commitments_from_strategy,
    )

    if not strategy:
        return None
    unchanged = (
        say(en=" Your current draft is unchanged.", tr=" Mevcut taslağın değişmedi.")
        if has_draft
        else ""
    )
    history = list(events)
    capability = ChoiceCapability(
        creator_id=creator_id, voice_route=settings.voice_behind_footage_enabled
    )
    for conflict in open_conflicts(strategy, brief, media_snapshot, capability):
        if count_asks(history, conflict.conflict_id, conflict.input_digest) < (
            MAX_ASKS_PER_QUESTION
        ):
            candidate = conflict.candidate()
            return (
                KriaTurnPlan(
                    mode="respond",
                    turn_value="question",
                    response=choice_question_text(candidate),
                    choice_question=build_choice_question(candidate),
                ),
                KEEP_OPEN_REASON,
            )
        if conflict.kind in (CONFLICT_ORDER_BASIS, CONFLICT_TITLE_TEXT, CONFLICT_WHICH_VOICE):
            # The option labels follow the chat's language (the conflict is built under
            # the turn's binding) and the matcher accepts them, so they are quoted as is.
            quoted = [f'"{o.label}"' for o in conflict.options]
            ways = " or ".join(quoted)
            ways_tr = f"{' veya '.join(quoted)} yaz"
            if conflict.kind == CONFLICT_TITLE_TEXT:
                ways = f"{ways}, or type the words you want"
                ways_tr = f"{ways_tr} ya da istediğin kelimeleri yaz"
            return (
                KriaTurnPlan(
                    mode="respond",
                    turn_value="recovery",
                    response=say(
                        en=(
                            f"{conflict.intro} {conflict.reason} I won't guess, so I haven't "
                            f"made an edit yet. To go ahead, reply {ways}.{unchanged}"
                        ),
                        tr=(
                            f"{conflict.intro} {conflict.reason} Tahmin yürütmeyeceğim, o "
                            f"yüzden henüz bir düzenleme yapmadım. Devam etmek için {ways_tr}."
                            f"{unchanged}"
                        ),
                    ),
                ),
                KEEP_OPEN_REASON,
            )
    try:
        contract = build_render_contract(
            strategy,
            generation_id="preflight",
            brief=contract_brief,
            media_snapshot=media_snapshot,
            # KRI-479: the dry run resolves the order the dispatch-time contract will.
            composition=(
                commitments_from_strategy(strategy)
                if settings.kria_plan_authority_enabled
                else None
            ),
        )
    except CreatorRenderContractError as exc:
        return (
            KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=f"{exc}{unchanged}",
            ),
            "unresolved_requirement",
        )
    if contract is not None and contract.unresolved:
        return (
            KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=f"{contract.unresolved[0]}{unchanged}",
            ),
            "unresolved_requirement",
        )
    return None


def _creative_copy_problem_sync(db, thread_id, media, *, strategy=None, resolution=None):  # noqa: ANN001, ANN202
    rows = db.execute(
        select(
            CreationThreadEvent.role,
            CreationThreadEvent.payload,
            CreationThreadEvent.event_type,
            CreationThreadEvent.content,
        )
        .where(CreationThreadEvent.thread_id == thread_id)
        .order_by(CreationThreadEvent.sequence)
    ).all()
    return creative_copy_problem(
        [
            tag_event(role, payload, event_type, content)
            for role, payload, event_type, content in rows
        ]
        + [("assistant", {"creative_copy_resolution": resolution})],
        media,
        strategy=strategy,
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

        if planned.media_snapshot is not None:
            from app.kria.brief_binding import media_identity, snapshot_media  # noqa: PLC0415

            if media_identity(planned.media_snapshot) != media_identity(snapshot_media(item)):
                raise RuntimeError("Your clips changed after planning; please plan again")
        # KRI-506: consent is checked under the revision fence for BOTH draft kinds.
        # A planner/editor shortcut or an exhausted question budget cannot bypass it.
        from app.kria.brief_binding import snapshot_media as _copy_media  # noqa: PLC0415

        copy_problem = _creative_copy_problem_sync(
            db,
            thread.id,
            _copy_media(item),
            strategy=document.strategy if document is not None else None,
            resolution=planned.creative_copy_resolution,
        )
        if copy_problem:
            log.info("kria_creative_copy_blocked", phase="draft", thread_id=str(thread.id))
            db.rollback()
            completed = _complete_response_turn(
                turn_id,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                claimed_thread_revision=claimed_thread_revision,
                plan=KriaTurnPlan(mode="respond", turn_value="question", response=copy_problem),
                brief_coverage={"stage": "draft", "reason": KEEP_OPEN_REASON},
                brief_expected_version=planned.brief_expected_version,
            )
            return replace(completed, response_only=True)

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
        brief = None
        if planned.brief_route is not None:
            # KRI-188: persist this turn's requirements (idempotent per turn),
            # then check each one deterministically against what was drafted.
            if planned.brief_expected_version is not None:
                current_brief = load_latest_brief_sync(db, thread.id)
                if planned.brief_expected_version != (
                    current_brief.version if current_brief else 0
                ):
                    raise RuntimeError("The request changed during planning; please retry")
            brief = persist_brief_version_sync(
                db,
                thread_id=thread.id,
                turn_id=turn.id,
                updates=planned.brief_updates,
            )
            if settings.kria_choice_questions_enabled and apply_intent.tool_name == (
                "draft.apply_strategy"
            ):
                # KRI-476: the creator's answers supersede the requirement they
                # resolved, so receipts judge (and approval pins) what they chose.
                brief = answered_brief(brief, document.strategy)
            if brief is not None:
                if apply_intent.tool_name == "draft.apply_strategy":
                    facts = plan_facts_from_strategy(
                        document.strategy,
                        clip_ids=planned.brief_clip_ids,
                        manifest=planned.brief_manifest,
                        speech_cleanup_enabled=bool(getattr(item, "speech_cleanup_enabled", False)),
                        speech_cleanup_offered=_speech_cleanup_offered(item, document.strategy),
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
                    receipts = build_receipts(
                        checked,
                        facts,
                        include_unchecked=settings.brief_binding_for(thread.creator_id),
                    )
                    requirement_receipts = [r.model_dump(mode="json") for r in receipts]
                    reply_text = reply_from_receipts(
                        CreativeBrief(version=brief.version, requirements=checked),
                        receipts,
                        summary=arguments.summary,
                        notices=planned.policy_notices,
                    )
        # KRI-529: list the untouched requirements only when a NEW cut is drafted (the
        # approval moment). An editor turn reports on what it was asked, instead of a
        # fresh "still needs an output check" chip for every earlier requirement.
        if (
            settings.brief_binding_for(thread.creator_id)
            and brief is not None
            and apply_intent.tool_name == "draft.apply_strategy"
        ):
            from app.kria.contracts import RequirementReceipt  # noqa: PLC0415

            checked_ids = {receipt["requirement_id"] for receipt in requirement_receipts}
            requirement_receipts.extend(
                RequirementReceipt(
                    requirement_id=req.id,
                    status="partial",
                    verification="unchecked",
                    stage="understood",
                    reason=say(
                        en="This requirement still needs an output check.",
                        tr="Bu isteğin videoda hâlâ kontrol edilmesi gerekiyor.",
                    ),
                    target_media_ids=[req.scope.split(":", 1)[1]]
                    if req.scope.startswith("clip:")
                    else [],
                ).model_dump(mode="json")
                for req in brief.live()
                if req.id not in checked_ids
            )
        if settings.brief_binding_for(thread.creator_id) and any(
            needs_creator_choice(receipt) for receipt in requirement_receipts
        ):
            # Do not replace a creator's current draft with a known partial edit.
            # Roll back the speculative draft/brief work, then persist the request
            # and the specific choice in the normal response transaction. A limit
            # of the chosen format (a Talking edit's length, speech cleanup chosen
            # at approval) is reported in the receipt instead and never asks.
            db.rollback()
            draft_state = (
                say(en="Your current draft is unchanged.", tr="Mevcut taslağın değişmedi.")
                if head is not None
                else say(en="I haven't started a draft yet.", tr="Henüz bir taslağa başlamadım.")
            )
            recovery = KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response=(
                    f"{reply_text}\n{draft_state} "
                    + say(
                        en="Should I try a different approach, or make this simpler version?",
                        tr=(
                            "Farklı bir yol mu deneyeyim, yoksa bunun daha basit bir "
                            "sürümünü mü hazırlayayım?"
                        ),
                    )
                ),
            )
            completed = _complete_response_turn(
                turn_id,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                claimed_thread_revision=claimed_thread_revision,
                plan=recovery,
                brief_updates=planned.brief_updates,
                requirement_receipts=requirement_receipts,
                brief_coverage={
                    **(planned.brief_coverage or {}),
                    "stage": "draft",
                    "reason": "simplification_requires_choice",
                },
                brief_expected_version=planned.brief_expected_version,
            )
            return replace(completed, response_only=True)
        choice_gate = (
            settings.kria_choice_questions_enabled
            and apply_intent.tool_name == "draft.apply_strategy"
            and bool(document.strategy)
        )
        if choice_gate:
            from app.kria.brief_binding import snapshot_media as _snapshot_media  # noqa: PLC0415

            binding_on = settings.brief_binding_for(thread.creator_id)
            gate_brief = brief
            if gate_brief is None and binding_on:
                gate_brief = answered_brief(
                    load_latest_brief_sync(db, thread.id), document.strategy
                )
            blocked = _unresolved_choice_plan(
                document.strategy,
                gate_brief if binding_on else None,
                planned.media_snapshot
                if planned.media_snapshot is not None
                else _snapshot_media(item),
                contract_brief=gate_brief if binding_on else None,
                events=[
                    tag_event(role, payload, event_type, content)
                    for role, payload, event_type, content in db.execute(
                        select(
                            CreationThreadEvent.role,
                            CreationThreadEvent.payload,
                            CreationThreadEvent.event_type,
                            CreationThreadEvent.content,
                        )
                        .where(
                            CreationThreadEvent.thread_id == thread.id,
                            CreationThreadEvent.role.in_({"user", "assistant"}),
                        )
                        .order_by(CreationThreadEvent.sequence)
                    ).all()
                ],
                has_draft=head is not None,
                creator_id=thread.creator_id if binding_on else None,
            )
            if blocked is not None:
                blocked_plan, blocked_reason = blocked
                # Same rollback-then-respond pattern as the receipts gate above: the
                # speculative draft/brief work is undone and the reply is persisted in
                # the normal response transaction (the old draft stays the head).
                db.rollback()
                completed = _complete_response_turn(
                    turn_id,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                    claimed_thread_revision=claimed_thread_revision,
                    plan=blocked_plan,
                    brief_updates=planned.brief_updates,
                    requirement_receipts=requirement_receipts,
                    brief_coverage={
                        **(planned.brief_coverage or {}),
                        "stage": "draft",
                        "reason": blocked_reason,
                    },
                    brief_expected_version=planned.brief_expected_version,
                )
                return replace(completed, response_only=True)
        if settings.brief_binding_for(thread.creator_id):
            from app.kria.brief_binding import BriefBinding, snapshot_media  # noqa: PLC0415

            if brief is None:
                brief = load_latest_brief_sync(db, thread.id)
                if choice_gate:
                    brief = answered_brief(brief, document.strategy)
            source_event = db.get(CreationThreadEvent, turn.source_event_id)
            coverage = dict(planned.brief_coverage or {})
            coverage["enforced_ids"] = [
                r["requirement_id"]
                for r in requirement_receipts
                if r.get("verification") == "checked" and r["status"] == "met"
            ]
            coverage["unresolved_ids"] = (
                [req.id for req in brief.live() if req.id not in coverage["enforced_ids"]]
                if brief
                else []
            )
            document = document.model_copy(
                update={
                    "brief_binding": BriefBinding.create(
                        thread.id,
                        brief,
                        latest_message=str(source_event.content or "")
                        if source_event
                        else document.intent,
                        media_snapshot=planned.media_snapshot
                        if planned.media_snapshot is not None
                        else snapshot_media(item),
                        choice_answers=(
                            list(document.strategy.get("choice_answers") or [])
                            if choice_gate
                            else None
                        ),
                    ),
                    "brief_coverage": coverage,
                }
            )
            snapshot, snapshot_hash = canonical_snapshot(document)
        if brief is not None:
            requirement_receipts = [
                {**receipt, "brief_version": brief.version, "generation_id": generation_id}
                for receipt in requirement_receipts
            ]
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
                    **(
                        {"creative_copy_resolution": planned.creative_copy_resolution}
                        if planned.creative_copy_resolution
                        else {}
                    ),
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
            result={
                "consequence": "Start one render from this exact draft.",
                "creator_request": render_brief_request(brief)
                if brief is not None
                else document.intent,
            },
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
            cost_summary=say(en="One render", tr="Tek bir video"),
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
                **(
                    {"creative_copy_resolution": planned.creative_copy_resolution}
                    if planned.creative_copy_resolution
                    else {}
                ),
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
    answers_clip_question: bool = False,
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
    # Bounded waits inside planning (clip understanding) must leave the turn time to
    # finish before Celery's soft limit turns it into "I couldn't finish that step".
    deadline = turn_deadline.set(time.monotonic() + float(run_kria_turn.soft_time_limit or 90))
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
                # KRI-282: a clip-picker answer must re-plan, never take the copilot path.
                **({"answers_clip_question": True} if answers_clip_question else {}),
            )
    finally:
        turn_deadline.reset(deadline)
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


def _answers_clip_question(source: Any) -> bool:
    payload = getattr(source, "payload", None)
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("clip_selection"), dict):
        return bool(settings.kria_clip_selection_questions_enabled)
    # A tapped choice_question option ANSWERS the open question: it must re-plan (the
    # planner folds the stored answer), never be read as a follow-up edit request.
    return isinstance(payload.get("choice_selection"), dict) and bool(
        settings.kria_choice_questions_enabled
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
            # Appended only when true so every other claim keeps its exact shape.
            *((True,) if _answers_clip_question(source) else ()),
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
    # KRI-520: also reached from `_claim` (claims exhausted) before any turn binding, so
    # the copy follows the chat's language from the thread row itself.
    with reply_language_for(thread_reply_language(thread)):
        failure_copy = say(
            en=(
                "I couldn't finish that step, but your project and saved draft are safe. "
                "Try the request again."
            ),
            tr=(
                "Bu adımı tamamlayamadım ama projen ve kayıtlı taslağın güvende. "
                "İsteği tekrar dene."
            ),
        )
    event = _append_sync_event(
        db,
        thread,
        role="assistant",
        event_type="assistant_error",
        content=failure_copy,
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


class _DraftItemView:
    """``item`` as approval will shape it: the draft strategy's edit format and audio
    mode instead of the item's current ones (`_apply_strategy_approval_media`)."""

    def __init__(self, item: Any, *, edit_format: str, audio_mode: str) -> None:
        self._item = item
        self.edit_format = edit_format
        self.audio_mode = audio_mode

    def __getattr__(self, name: str) -> Any:
        return getattr(self._item, name)


def _speech_cleanup_offered(item: Any, strategy: Mapping[str, Any] | None) -> bool | None:
    """Whether approving this draft of ``item`` offers "Clean up speech" (KRI-205):
    the preflight check is enforced and the draft's speech source is in its
    cohort. None when that cannot be told, so a receipt promises nothing."""
    try:
        if settings.speech_cleanup_preflight_mode != "enforce":
            return False
        from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
        from app.services.plan_item_media import (  # noqa: PLC0415
            current_detector_policy,
            resolve_item_narration,
        )
        from app.services.speech_cleanup_decision import resolve_next_audio_mode  # noqa: PLC0415
        from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
            preflight_enabled_for_source,
        )

        edit_format = str(getattr(item, "edit_format", "") or "")
        audio_mode = str(getattr(item, "audio_mode", "") or "")
        if strategy:
            parsed = CreativeStrategy.model_validate(strategy)
            edit_format = str(parsed.edit_format or edit_format)
            audio_mode = resolve_next_audio_mode(parsed, item) or audio_mode
        view = _DraftItemView(item, edit_format=edit_format, audio_mode=audio_mode)
        resolution = resolve_item_narration(view, detector_policy=current_detector_policy())
        if resolution.source is None:
            return None
        return bool(
            preflight_enabled_for_source(
                resolution.source.source_policy_fingerprint,
                mode=settings.speech_cleanup_preflight_mode,
                rollout_percent=settings.speech_cleanup_preflight_rollout_percent,
            )
        )
    except Exception:  # noqa: BLE001 - a receipt nicety never fails a turn
        return None


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


# 200 s soft limit: a clip-intent turn measured ~70 s of model + vision work, and may
# also wait (bounded by this limit, see `turn_deadline`) for clip analysis in flight.
# KRI-542: the Main Creator's own deadline is 130 s (thinking "high" on a 16k
# budget), and a truncated call is retried once at "low" (~35-45 s), so a degraded
# turn needs ~175 s before the resolver runs. Both limits stay far under the
# broker's 1900 s visibility_timeout (tests/tasks/test_task_time_limits.py).
@celery_app.task(
    bind=True,
    name="tasks.run_kria_turn",
    soft_time_limit=200,
    time_limit=230,
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
    # `_claim` appends True only when the source event answers a question AND that
    # question kind's flag is on.
    answers_clip_question = bool(len(claimed_rest) > 1 and claimed_rest[1] is True)
    # KRI-520: every reply this turn writes (model prompts, server copy, failures)
    # follows the chat's language; released in the `finally` below.
    language_token = bind_reply_language(snapshot.get("reply_language"))
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
                    **({"answers_clip_question": True} if answers_clip_question else {}),
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
                    creative_copy_resolution=planned.creative_copy_resolution,
                    brief_coverage=planned.brief_coverage,
                    brief_expected_version=planned.brief_expected_version,
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
                            mode="respond",
                            turn_value="recovery",
                            # KRI-520: the two constants are English; the planner maps them.
                            response=localized_editor_state_reply(exc.reply),
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
                            response=say(
                                en=(
                                    f"I can't do that on this edit: "
                                    f"{str(exc).strip().rstrip('.')}. Nothing was changed."
                                ),
                                tr=(
                                    f"Bu düzenlemede bunu yapamıyorum: "
                                    f"{str(exc).strip().rstrip('.')}. Hiçbir şey değişmedi."
                                ),
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
                    if not completion.response_only
                    and any(intent.tool_name == "render.request" for intent in planned.plan.intents)
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
            message = say(
                en="I inspected the project, but I need footage evidence before editing.",
                tr="Projeyi inceledim ama düzenlemeden önce çekimlerini görmem gerekiyor.",
            )
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
    finally:
        release_reply_language(language_token)


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


def _deferred_replan_note() -> str:
    return say(
        en=_DEFERRED_REPLAN_NOTE,
        tr=(
            "Bu değişikliği editörde yaptım. İsteğinin bir kısmı yerinde bir ayar değil, "
            "yeni bir düzenleme gerektiriyor: baştan yapmamı söylersen tüm isteğine göre "
            "planı yeniden kurarım."
        ),
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
        # KRI-520: the extraction runs under the chat's language, like the turn it follows.
        with reply_language_for(snapshot.get("reply_language")):
            updates, route = asyncio.run(_extract_brief_async(snapshot, message))
    except Exception:  # noqa: BLE001 - best-effort context
        log.warning("kria_deferred_brief_failed", turn_id=turn_id, exc_info=True)
        return {"turn_id": turn_id, "status": "failed"}
    with sync_session() as db, ExitStack() as language:
        thread = db.execute(
            select(CreationThread)
            .where(CreationThread.id == uuid.UUID(str(snapshot["thread_id"])))
            .with_for_update()
        ).scalar_one()
        _bind_thread_language(language, thread)
        if updates:
            persist_brief_version_sync(db, thread_id=thread.id, turn_id=identifier, updates=updates)
        if route == "replan":
            log.info("kria_fast_path_route_mismatch", turn_id=turn_id)
            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_response",
                content=_deferred_replan_note(),
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

    with sync_session() as db, ExitStack() as language:
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
        # KRI-520: every refusal this claim writes (and the creative-copy gate's wording)
        # follows the chat's language; released when the transaction scope exits.
        _bind_thread_language(language, thread_ref)

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

        if target_valid and document is not None:
            from app.kria.brief_binding import media_identity, snapshot_media  # noqa: PLC0415

            if document.brief_binding is not None:
                try:
                    document.brief_binding.resolve(thread.id)
                    if execution.status != "accepted" and document.brief_binding.media_snapshot:
                        target_valid = media_identity(
                            document.brief_binding.media_snapshot
                        ) == media_identity(snapshot_media(item))
                except ValueError:
                    target_valid = False
            elif settings.brief_binding_for(thread.creator_id):
                # Legacy accepted work may retry only its own saved inputs.
                target_valid = execution.status == "accepted" and isinstance(
                    (execution.result or {}).get("creator_request"), str
                )

        copy_problem = None
        if target_valid and document is not None:
            # The draft's approved media is authoritative after strategy selection
            # changes the working item's subset. The identity fence above checks drift.
            from app.kria.brief_binding import snapshot_media as _copy_media  # noqa: PLC0415

            copy_media = (
                document.brief_binding.media_snapshot
                if document.brief_binding is not None and document.brief_binding.media_snapshot
                else _copy_media(item)
            )
            copy_problem = _creative_copy_problem_sync(
                db, thread.id, copy_media, strategy=document.strategy
            )
            if copy_problem:
                target_valid = False
                log.info("kria_creative_copy_blocked", phase="dispatch", thread_id=str(thread.id))

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
                "code": CREATIVE_COPY_PENDING if copy_problem else "approval_target_stale",
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
                content=copy_problem
                or say(
                    en=(
                        "The project changed before I could start that render. "
                        "I kept your draft; ask me to prepare it again."
                    ),
                    tr=(
                        "Videoyu başlatamadan önce proje değişti. Taslağını sakladım; "
                        "tekrar hazırlamamı isteyebilirsin."
                    ),
                ),
                payload={
                    "turn_id": str(turn.id),
                    "approval_id": str(approval.id),
                    "code": CREATIVE_COPY_PENDING if copy_problem else "approval_target_stale",
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
        render_shape_choice: dict[str, str] | None = None
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
                    content=say(
                        en="Record a voiceover first, then I can render this direction.",
                        tr="Önce bir seslendirme kaydet, sonra bu yönde videoyu oluşturabilirim.",
                    ),
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
            # KRI-306: apply the creator's output-shape choice HERE (not at
            # approval time) so a deny, which never reaches the claim, has
            # nothing to undo. Landscape never has bars, so it leaves the
            # item's remembered bars/crop preference alone.
            from app.services.render_shape import shape_from_all_candidates  # noqa: PLC0415

            render_shape_choice = shape_from_all_candidates(
                {"creator_render_shape": (execution.result or {}).get("render_shape")}
            )
            if render_shape_choice is not None and render_shape_choice["output_orientation"] == (
                "portrait"
            ):
                item.landscape_fit = render_shape_choice["landscape_fit"]
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
                        # The validated approval binding is staged with the
                        # native editor save before its phone recipe is pinned.
                        # Never substitute current thread state here.
                        creator_brief_binding=(
                            document.brief_binding.model_dump(mode="json")
                            if document.brief_binding is not None
                            else None
                        ),
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
                        content=_device_edit_refusal_copy(code),
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

        dispatch_request = str((execution.result or {}).get("creator_request", document.intent))
        if document.brief_binding is not None:
            document.brief_binding.resolve(thread.id)
            dispatch_request = document.brief_binding.creator_request
        execution.result = {
            **(execution.result or {}),
            "creator_request": dispatch_request,
            "brief_binding": document.brief_binding.model_dump(mode="json")
            if document.brief_binding
            else None,
        }
        if document.kind == "editor" and document.brief_binding is not None:
            assembly = dict(current_job.assembly_plan or {})
            bindings = dict(assembly.get("creator_brief_bindings") or {})
            bindings[target_generation_id] = document.brief_binding.model_dump(mode="json")
            current_job.assembly_plan = {**assembly, "creator_brief_bindings": bindings}

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
            brief_binding=document.brief_binding.model_dump(mode="json")
            if document.brief_binding
            else None,
            preflight_analysis_id=preflight_analysis_id,
            speech_cleanup_analysis_id=speech_cleanup_analysis_id,
            speech_cleanup_choice=speech_cleanup_choice,
            render_shape=render_shape_choice,
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
# KRI-520: Turkish copy for the same refusal codes (a test pins that no code is missing).
_DEVICE_EDIT_REFUSALS_TR = {
    "baseline_conflict": (
        "Taslağı hazırladığımdan beri iPhone'undaki video değişti, o yüzden olduğu gibi "
        "bıraktım. Tekrar iste, değişikliği güncel sürümde yapayım."
    ),
    "unsupported_phone_edit": (
        "Bu değişiklik iPhone'unda henüz yapılamıyor, o yüzden videoyu olduğu gibi bıraktım."
    ),
    "phone_editor_media_unavailable": (
        "Bu medyayı eklemek cihaz üzerindeki düzenlemelerde henüz kullanılamıyor, o yüzden "
        "videoyu olduğu gibi bıraktım."
    ),
    "phone_rendering_unavailable": (
        "Cihaz üzerinde video oluşturma bu hesapta şu anda kullanılamıyor, o yüzden videoyu "
        "olduğu gibi bıraktım."
    ),
}
_DEVICE_EDIT_REFUSAL_FALLBACK_TR = (
    "Bu değişikliği iPhone'unda uygulayamadım, o yüzden videoyu olduğu gibi bıraktım."
)


def _device_edit_refusal_copy(code: str) -> str:
    return say(
        en=_DEVICE_EDIT_REFUSALS.get(code, _DEVICE_EDIT_REFUSAL_FALLBACK),
        tr=_DEVICE_EDIT_REFUSALS_TR.get(code, _DEVICE_EDIT_REFUSAL_FALLBACK_TR),
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


_DETERMINISTIC_JOB_FAILURE_CODES = {"phone_plan_unsupported", "user_song_plan_declined"}


_VARIANT_DECLINE_FAILURE_CODES = {"variant_render_failed", "creator_render_contract_unverified"}

# Cloud contract declines (KRI-470 PR-A/E/F). ``unsupported`` is a JOB-level decline raised
# before anything renders (adapter preflight, plan-authority route declines): the same inputs
# decline identically on every retry, and a retry re-runs ingest + clip analysis first, so no
# reason is retryable. ``unverified`` is a publication-time decline of a rendered VARIANT: only
# missing evidence can appear on a re-run; a conflict or a choice needs the creator.
_CLOUD_PREFLIGHT_DECLINE_CODE = "creator_render_contract_unsupported"
_CLOUD_PUBLICATION_DECLINE_CODE = "creator_render_contract_unverified"


def _cloud_decline_needs_the_creator(failure_code: str, reason: str) -> bool:
    """Whether a typed cloud decline is a question/refusal rather than a repair retry."""
    if failure_code == _CLOUD_PREFLIGHT_DECLINE_CODE:
        return True
    return failure_code == _CLOUD_PUBLICATION_DECLINE_CODE and reason in {
        "needs_choice",
        "requirement_conflict",
        "capability_unavailable",
    }


def _typed_creator_decline(
    job: Job, variant_id: str | None, failure_code: str | None = None
) -> dict[str, str] | None:
    """The typed creator-contract decline a failed job persisted, if any.

    Phone/cloud-preflight declines live in ``assembly_plan["creator_decline"]``;
    a cloud publication decline lives on the failed variant beside its
    ``error_class``.  The failure code itself is never changed by this (the typed
    reason rides beside it), so untyped failures keep their exact recovery.
    """
    from app.services.creator_render_contract import (  # noqa: PLC0415
        CREATOR_DECLINE_FIELD,
        DECLINE_REASONS,
    )

    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    raw = plan.get(CREATOR_DECLINE_FIELD)
    message = getattr(job, "error_detail", None)
    # A job-level decline belongs to the failure code it was stamped with. A job
    # that failed once with a refusal and later fails for another reason (retry,
    # re-dispatch) must not inherit the earlier refusal.
    if not (
        isinstance(raw, dict)
        and raw.get("decline_reason") in DECLINE_REASONS
        and failure_code is not None
        and raw.get("failure_reason") == failure_code
    ):
        raw = None
        if failure_code not in _VARIANT_DECLINE_FAILURE_CODES:
            return None
        variants = [row for row in plan.get("variants") or [] if isinstance(row, dict)]
        # The target variant only; scan every variant only when none is named.
        candidates = (
            [row for row in variants if row.get("variant_id") == variant_id]
            if variant_id
            else variants
        )
        for row in candidates:
            if (
                row.get("render_status") == "failed"
                and row.get("decline_reason") in DECLINE_REASONS
            ):
                raw = row
                message = row.get("error")
                break
    if raw is None:
        return None
    decline = {"decline_reason": str(raw["decline_reason"])}
    for key in ("field_path", "alternative"):
        if isinstance(raw.get(key), str) and raw[key]:
            decline[key] = raw[key]
    if isinstance(message, str) and message.strip():
        decline["message"] = message.strip()[:500]
    return decline


def _device_contract_decline(job: Job, variant_id: str | None) -> dict[str, str] | None:
    """The typed refusal recorded on a failed device render's record (KRI-470 PR-G).

    Written beside the record's intact state when the contract refused an edit at
    publication or retry. ``None`` for a phone-side failure (thermal, storage, cancel):
    those keep the "tap Retry on your iPhone" recovery.
    """
    from app.services.creator_render_contract import DECLINE_REASONS  # noqa: PLC0415
    from app.services.device_render import contract_decline  # noqa: PLC0415

    records = (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD)
    if not isinstance(records, dict):
        return None
    for record_variant in [variant_id] if variant_id else list(records):
        raw = contract_decline(job, str(record_variant))
        if isinstance(raw, dict) and raw.get("decline_reason") in DECLINE_REASONS:
            decline = {"decline_reason": str(raw["decline_reason"])}
            for key in ("field_path", "alternative", "message"):
                if isinstance(raw.get(key), str) and raw[key].strip():
                    decline[key] = raw[key].strip()[:500]
            return decline
    return None


_LAST_GOOD_STAYS = "Your last good version is still available."
_NOTHING_PUBLISHED = "Nothing was published."
# What a creator can really do next: send a new message. The approved request (the brief)
# stays in force across turns, so a plain "redo it" builds a new version from it; the
# runtime has no automatic re-run of a refused phone render and no device-job chat retry.
_REDO_FROM_APPROVED = (
    "Tell me to redo it and I'll make a new version from what you already approved."
)


def _device_refusal_copy(decline: dict[str, str], *, last_good: bool = True) -> str:
    """What the creator hears when the contract refused an edit of a phone render.

    Says what could not be confirmed, that the edit was NOT applied, that the last good
    version is kept, and a next step that exists. Never promises an automatic rebuild and
    never asks the creator to restate a clear instruction.
    """
    message = decline.get("message") or say(
        en="That edit didn't keep something you asked for.",
        tr="Bu düzenleme istediğin bir şeyi korumadı.",
    )
    reason = decline["decline_reason"]
    kept = (
        say(en=_LAST_GOOD_STAYS, tr="Son iyi sürümün hâlâ duruyor.")
        if last_good
        else say(en=_NOTHING_PUBLISHED, tr="Yeni bir video oluşturulmadı.")
    )
    not_applied = say(en="That edit was not applied.", tr="Bu düzenleme uygulanmadı.")
    if reason == "capability_unavailable":
        return f"{_capability_refusal_copy(decline)} {not_applied} {kept}"
    if reason in {"needs_choice", "requirement_conflict"}:
        alternative = decline.get("alternative") or say(
            en="Tell me which way you want to go.",
            tr="Hangi yoldan gitmek istediğini söyle.",
        )
        return f"{message} {not_applied} {alternative} {kept}"
    redo = say(
        en=_REDO_FROM_APPROVED,
        tr="Baştan yapmamı söylersen onayladığın isteğe göre yeni bir sürüm hazırlarım.",
    )
    return f"{message} {not_applied} {kept} {redo}"


def _capability_refusal_copy(decline: dict[str, str]) -> str:
    """A refusal that names the limit and the supported way forward."""
    limit = decline.get("message") or say(
        en="This render path can't keep that requirement.",
        tr="Bu video hazırlama yolu bu isteği koruyamıyor.",
    )
    alternative = decline.get("alternative") or _try_different_approach()
    return f"{limit} {alternative}"


def _try_different_approach() -> str:
    return say(
        en="Tell me what you'd like to change and I'll try a different approach.",
        tr="Neyi değiştirmek istediğini söyle, farklı bir yol deneyeyim.",
    )


# KRI-520: Turkish copy for every `content_plan_build.PHONE_GATE_MESSAGES` reason (a test
# pins that no reason is missing, so a new gate cannot silently stay English).
_VOICEOVER_UNAVAILABLE_TR = (
    "iPhone'unda henüz seslendirmeli video oluşturulamıyor ve bu projenin videoları bu "
    "iPhone'da oluşturuluyor. Bu düzenlemeyi seslendirme olmadan iste. Yedek bir düzenleme "
    "oluşturulmadı."
)
_PHONE_GATE_MESSAGES_TR = {
    "device_render_unsupported": (
        "Bu proje tamamen bu iPhone'da oluşturulamıyor. Mevcut videon değişmedi."
    ),
    "not_enrolled": (
        "Bu projenin çekimleri iPhone'unda duruyor ve şu anda bu hesaptan videoya dönüştürülemiyor."
    ),
    "unapproved_guided": (
        "Bu düzenleme planının iPhone'unda videoya dönüşebilmesi için yeniden onaylanması "
        "gerekiyor."
    ),
    "unsupported_format": (
        "Bu tür bir video şu anda iPhone'unda oluşturulamıyor. iPhone'da oluşturulabilen bir "
        "format seç (Montaj, ya da uygun olduğunda Kameraya konuşma / Seslendirmeli). Yedek "
        "bir düzenleme oluşturulmadı."
    ),
    "voiceover_unavailable": _VOICEOVER_UNAVAILABLE_TR,
    "guided_voiceover_unavailable": _VOICEOVER_UNAVAILABLE_TR,
    "narrated_voiceover_unavailable": _VOICEOVER_UNAVAILABLE_TR,
    "subtitled_clip_count_unsupported": (
        "Kameraya konuşma videoları iPhone'unda tam olarak tek klipten oluşturulur. Fazla "
        "klipleri kaldır (ya da bir klip ekle) ve tekrar dene. Yedek bir düzenleme "
        "oluşturulmadı."
    ),
    "subtitled_clip_too_long": (
        "Bu klip, iPhone'unda kameraya konuşma videosu için çok uzun. 5 dakikadan kısa bir "
        "klip kullan ve tekrar dene. Yedek bir düzenleme oluşturulmadı."
    ),
    "self_narration_multi_clip": (
        "Birden fazla klip üzerinde anlatım iPhone'da henüz yok. Bir seslendirme kaydet ya da "
        "kameraya konuşma için tek klip kullan."
    ),
    "user_song_unavailable": (
        "Kendi şarkın şu anda yalnızca iPhone'unda hazırlanan videolarda kullanılabiliyor. "
        "Bu düzenlemeyi şarkın olmadan iste. Yedek bir düzenleme oluşturulmadı."
    ),
}


def _phone_gate_refusal_copy(reason: str) -> str:
    from app.tasks.content_plan_build import PHONE_GATE_MESSAGES  # noqa: PLC0415

    english = PHONE_GATE_MESSAGES.get(reason, (None, None))[1]
    message = say(en=english or "", tr=_PHONE_GATE_MESSAGES_TR.get(reason) or english or "")
    lead = message or say(
        en="That kind of edit isn't available for iPhone renders yet.",
        tr="Bu tür bir düzenleme iPhone'da henüz kullanılamıyor.",
    )
    return f"{lead} {_try_different_approach()}"


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
    "request_binding_stale": (
        "Your source clips changed after this draft was approved. Your draft is saved; "
        "please make a fresh plan before rendering."
    ),
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

# KRI-520: Turkish copy for every outcome in the two tables above (a test pins that none is
# missing). The card buttons are not translated in the app, so "Refresh project" and
# "Visuals" are quoted as the creator sees them.
_DISPATCH_REFUSALS_TR: dict[str, str] = {
    "speech_cleanup_analysis_conflict": (
        'Başlamadan önce konuşma kontrolü değişti. "Refresh project" düğmesine dokun, '
        "duraklamaları nasıl ele alacağını seçebilmen için yeniden hazırlayayım."
    ),
    "speech_cleanup_recovery_conflict": (
        'Başlamadan önce konuşma kontrolü değişti. "Refresh project" düğmesine dokun, '
        "duraklamaları nasıl ele alacağını seçebilmen için yeniden hazırlayayım."
    ),
    "speech_cleanup_unavailable": (
        'Bu video için konuşma kontrolü henüz kullanılamıyor. "Refresh project" düğmesine '
        "dokun, videoyu temizleme olmadan hazırlayayım."
    ),
    "speech_cleanup_unavailable_on_phone": (
        "Konuşma temizleme bu iPhone düzenlemesi için henüz kullanılamıyor. "
        '"Refresh project" düğmesine dokun ve özgün konuşmayı korumayı seç.'
    ),
    "request_binding_stale": (
        "Bu taslak onaylandıktan sonra kaynak kliplerin değişti. Taslağın kayıtlı; videoyu "
        "oluşturmadan önce lütfen yeni bir plan hazırlat."
    ),
    "guided_edit_bypass_unsafe": (
        "Visuals bölümündeki görsellerini henüz bu montaja ekleyemiyorum, o yüzden videoyu "
        'başlatmadım. Onları Visuals bölümünden çıkar, sonra "Refresh project" düğmesine '
        "dokun; videoyu senin videolarından hazırlayayım."
    ),
    "visuals_processing": (
        "Visuals bölümüne eklediğin bir fotoğraf ya da video hâlâ hazırlanıyor. Biraz bekle, "
        'sonra "Refresh project" düğmesine dokun, videoyu başlatayım.'
    ),
}


def _dispatch_refusal_copy(outcome: str) -> str | None:
    """The fixed refusal sentence for a dispatch outcome that needs a fresh approval."""
    english = _SPEECH_CLEANUP_DISPATCH_REFUSALS.get(outcome) or _VISUALS_DISPATCH_REFUSALS.get(
        outcome
    )
    if english is None:
        return None
    return say(en=english, tr=_DISPATCH_REFUSALS_TR.get(outcome, english))


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
    with sync_session() as db, ExitStack() as language:
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
        # KRI-520: the refusal copy below follows the chat's language.
        _bind_thread_language(language, thread)

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
            if settings.live_plan_review_enabled:
                # KRI-443: all seven sections start `waiting`, in the SAME transaction
                # (and under the same thread lock) as `render_queued`.
                from app.kria.plan_blocks import (  # noqa: PLC0415
                    plan_block_payload,
                    waiting_blocks,
                )

                _append_sync_event(
                    db,
                    thread,
                    role="system",
                    event_type="plan_block",
                    content=None,
                    payload=plan_block_payload(
                        turn_id=str(turn.id), job_id=str(job_id), blocks=waiting_blocks()
                    ),
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
                content=say(
                    en=(
                        "The render queue did not confirm whether it received this edit. "
                        "Your approved draft is saved; I won't send it twice until it is "
                        "reconciled."
                    ),
                    tr=(
                        "Video sırası bu düzenlemeyi alıp almadığını doğrulamadı. Onayladığın "
                        "taslak kayıtlı; durum netleşene kadar aynı işi ikinci kez "
                        "göndermeyeceğim."
                    ),
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

        refusal_copy = _dispatch_refusal_copy(outcome)
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
                or say(
                    en=(
                        "I couldn't start the render. Your draft is still saved, "
                        "so you can retry without repeating the edit."
                    ),
                    tr=(
                        "Videoyu başlatamadım. Taslağın hâlâ kayıtlı, düzenlemeyi baştan "
                        "anlatmadan tekrar deneyebilirsin."
                    ),
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
            **(
                {"creator_brief_binding": claim.brief_binding}
                if getattr(claim, "brief_binding", None)
                else {}
            ),
            speech_cleanup_analysis_id=(
                str(claim.speech_cleanup_analysis_id)
                if claim.speech_cleanup_analysis_id is not None
                else None
            ),
            speech_cleanup_choice=claim.speech_cleanup_choice,
            # KRI-306: only an explicit choice rides the dispatch (absent =
            # byte-identical call).
            **(
                {"creator_render_shape": claim.render_shape}
                if getattr(claim, "render_shape", None)
                else {}
            ),
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
    if not isinstance(record, dict):
        # KRI-533: a phone Voiceover edit keeps its receipts on its own record.
        record = (job.assembly_plan or {}).get(NARRATED_ALIGNMENT_FIELD)
    if not isinstance(record, dict):
        return default_text, []
    # KRI-282: what each requested group / sport name / chapter text did in the plan.
    # It is read from the finished plan, not the brief ledger, so it is listed even
    # when the ledger judged none of those asks.
    outcomes = [row for row in record.get("intent_outcomes") or [] if isinstance(row, dict)]
    if not record.get("requirement_receipts") and not outcomes:
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
    for raw in record.get("requirement_receipts") or []:
        try:
            receipts.append(RequirementReceipt.model_validate(raw))
        except ValueError:
            continue
    live = {req.id: req for req in brief.live()}
    # A record planned before unjudged receipts were dropped can still carry some.
    receipts = [r for r in receipts if is_judged(live.get(r.requirement_id), r)]
    if not receipts and not outcomes:
        return default_text, []
    checked = CreativeBrief(
        version=brief.version,
        requirements=[live[receipt.requirement_id] for receipt in receipts],
    )
    return (
        reply_from_receipts(checked, receipts, summary=default_text, outcomes=outcomes),
        [receipt.model_dump(mode="json") for receipt in receipts],
    )


def _user_song_note(job: Job) -> str:
    """Plain-language account of how the creator's song was used (KRI-466).

    Reads the plan's ids-only song receipt (`unified_montage.user_song`); empty when
    nothing noteworthy happened. One sentence per applicable case.
    """
    record = (job.assembly_plan or {}).get("unified_montage")
    receipt = record.get("user_song") if isinstance(record, dict) else None
    if not isinstance(receipt, dict):
        return ""
    notes: list[str] = []
    if receipt.get("fallback_reason"):
        notes.append(
            say(
                en=(
                    "I couldn't find where your takes sit in the song, so I used it as "
                    "background music cut to the beat. To lip-sync, play the song out loud "
                    "while filming, or sing along clearly so I can match your words "
                    "(earbuds work, but the sync is a bit looser)."
                ),
                tr=(
                    "Çekimlerinin şarkının neresine denk geldiğini bulamadım, o yüzden şarkıyı "
                    "ritme göre kesilmiş fon müziği olarak kullandım. Dudak senkronu için "
                    "çekim yaparken şarkıyı yüksek sesle çal ya da sözleri net bir şekilde "
                    "söyleyerek eşlik et, böylece sözlerini eşleştirebilirim (kulaklıkla da "
                    "olur ama senkron biraz daha gevşek olur)."
                ),
            )
        )
    broll = receipt.get("kept_broll_ids")
    if isinstance(broll, list) and broll:
        count = len(broll)
        notes.append(
            say(
                en=(
                    f"{count} take{' has' if count == 1 else 's have'} no usable singing or "
                    f"words, so {'it is' if count == 1 else 'they are'} in as short muted "
                    f"clip{'' if count == 1 else 's'}. Trim or remove "
                    f"{'it' if count == 1 else 'them'} in the editor."
                ),
                tr=(
                    f"{count} çekimde kullanılabilir şarkı ya da söz yok, bu yüzden kısa ve "
                    "sessiz klip olarak eklendi. Editörde kısaltabilir veya "
                    "kaldırabilirsin."
                ),
            )
        )
    low = receipt.get("low_confidence_ids")
    if isinstance(low, list) and low:
        count = len(low)
        notes.append(
            say(
                en=(
                    f"{count} take{' is' if count == 1 else 's are'} placed by my best guess "
                    f"and may be slightly off; check {'it' if count == 1 else 'them'} in the "
                    "editor."
                ),
                tr=(
                    f"{count} çekimi tahminime göre yerleştirdim, biraz kayık olabilir; "
                    "editörde kontrol et."
                ),
            )
        )
    outside = receipt.get("placed_outside_ids")
    if isinstance(outside, list) and outside:
        count = len(outside)
        notes.append(
            say(
                en=(
                    f"{count} take{' sits' if count == 1 else 's sit'} later in the song than "
                    "a 2-minute video can hold."
                ),
                tr=(
                    f"{count} çekim şarkıda, 2 dakikalık bir videonun sığabileceğinden daha "
                    "geride kalıyor."
                ),
            )
        )
    placed = receipt.get("placed")
    by_lyrics = (
        sum(1 for row in placed if isinstance(row, dict) and row.get("method") == "lyrics")
        if isinstance(placed, list)
        else 0
    )
    if by_lyrics:
        notes.append(
            say(
                en=f"I matched {by_lyrics} take{'' if by_lyrics == 1 else 's'} by your singing.",
                tr=f"{by_lyrics} çekimi söylediğin sözlere göre eşleştirdim.",
            )
        )
    return " ".join(notes)


def _voice_behind_footage_note(job: Job) -> str:
    """What the continuous-voice composer changed, in the creator's words (KRI-479).

    Reads the composer's own receipt (`assembly_plan["speech_montage"]["adjustments"]`, set only
    for `route == "voice_behind_footage"`): a trimmed voice, a length extended so every clip is
    shown, a silent stretch, clips sharing a capture time. Empty when nothing changed.
    """
    record = (job.assembly_plan or {}).get("speech_montage")
    if not isinstance(record, dict) or record.get("route") != "voice_behind_footage":
        return ""
    sentences: list[str] = []
    for raw in record.get("adjustments") or []:
        text = " ".join(str(raw).split())
        if not text:
            continue
        text = text[0].upper() + text[1:]
        sentences.append(text if text.endswith((".", "!", "?")) else f"{text}.")
    return " ".join(sentences)


def _approved_generation_review(
    db: Any,
    thread: CreationThread,
    job: Job,
    variant: dict,
    execution: CreatorAgentExecution,
    default_text: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Read the accepted request, never a newer chat brief, when describing output."""
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415
    from app.kria.contracts import RequirementReceipt  # noqa: PLC0415

    raw = (execution.result or {}).get("brief_binding")
    if raw is None:
        raw = (job.assembly_plan or {}).get("creator_brief_binding")
    if raw is None:
        return _unified_montage_review(db, thread, job, default_text)
    binding = BriefBinding.model_validate(raw)
    brief = binding.resolve(thread.id)
    if brief is None or not brief.live():
        return default_text, []
    generation = str(variant.get("render_generation_id") or "") or None
    receipts = []
    for record_key in ("unified_montage", NARRATED_ALIGNMENT_FIELD):
        record = (job.assembly_plan or {}).get(record_key) or {}
        if (
            record.get("generation_id") != generation
            or record.get("brief_version") != brief.version
        ):
            continue
        for row in record.get("requirement_receipts") or []:
            try:
                receipt = RequirementReceipt.model_validate(row)
            except ValueError:
                continue
            if receipt.brief_version == brief.version and receipt.generation_id == generation:
                receipts.append(receipt)
    known = {receipt.requirement_id for receipt in receipts}
    facts = plan_facts_from_editor_payload(variant)
    receipts.extend(
        build_receipts(
            [req for req in brief.live() if req.id not in known], facts, include_unchecked=True
        )
    )
    receipts = [
        receipt.model_copy(
            update={
                "brief_version": brief.version,
                "generation_id": generation,
            }
        )
        for receipt in receipts
    ]
    return reply_from_receipts(brief, receipts, summary=default_text), [
        receipt.model_dump(mode="json") for receipt in receipts
    ]


# KRI-520: Turkish copy for the Job failure sentences a render-failed event shows. The
# English table lives in `content_plan_build.JOB_FAILURE_MESSAGES`; a test pins that every
# code there has a Turkish line here, so the two cannot drift apart.
_JOB_FAILURE_MESSAGES_TR: dict[str, str] = {
    "user_song_plan_declined": (
        "Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya da "
        "sözleri net söyleyerek eşlik et, sonra tekrar dene."
    ),
    "phone_plan_unsupported": (
        "Bu düzenleme iPhone'unun henüz oluşturamadığı bir şey içeriyor. Farklı bir klip ya da "
        "formatla yeni bir düzenleme başlat."
    ),
    "phone_capability_unavailable": (
        "Bu düzenleme için gereken bir iPhone özelliği henüz kullanılamıyor. Daha sonra tekrar "
        "dene ya da farklı bir stil iste."
    ),
    "phone_plan_failed": (
        "Bu düzenleme iPhone'unda oluşturulamadı. Tekrar dene ya da yönde bir değişiklik iste."
    ),
    "originals_not_uploaded": (
        "Orijinal çekimlerin yüklenmedi, bu yüzden iPhone'unda oluşturulamıyor. Kliplerini "
        "yeniden ekle ve tekrar dene."
    ),
    "processing_timeout": (
        "İşlem zaman aşımına uğradı; kliplerin çok ağır (muhtemelen 4K/HDR). Daha az ya da "
        "daha kısa klip dene."
    ),
    "speech_cleanup_failed": (
        "Sesi temizleme tamamlanmadı. Tekrar dene ya da konuşma temizlemeyi kapat."
    ),
    "no_labeled_tracks": (
        "Bu düzenleme için uygun müzik bulunamadı. Tekrar dene ya da müziği kendin seç."
    ),
    "matching_failed": "Çekimlerini müzikle eşleştirme tamamlanmadı. Tekrar dene.",
    "auto_music_disabled": "Otomatik müzik eşleştirme şu anda kapalı. Daha sonra tekrar dene.",
    "cloud_render_disabled": (
        "Bu proje iPhone ile oluşturmayı gerektiriyor. Kria'yı güncelle, sonra projeyi açıp "
        "tekrar dene."
    ),
    "dispatch_publish_failed": "Video sıraya iletilemedi. Bir kez daha dene.",
    "drive_import_failed": "Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
    "drive_import_dispatch_failed": "Drive'dan içe aktarma tamamlanmadı. Tekrar dene.",
    "upload_promotion_failed": "Yüklemen kaydedilemedi. Tekrar dene.",
    "skia_disabled": (
        "Bu düzenleme şu anda geçici olarak kapalı bir oluşturucuya ihtiyaç duyuyor. Daha "
        "sonra tekrar dene."
    ),
    "cancelled_by_admin": "Bu video iptal edildi.",
    "render_oom": (
        "Video hazırlanırken bellek yetmedi; kliplerin çok ağır. Daha az ya da daha kısa klip dene."
    ),
    "ffmpeg_failed": "Videon birleştirilirken bir şeyler ters gitti. Tekrar dene.",
    "gemini_analysis_failed": "Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
    "analysis_failed": "Çekimlerinin analizi tamamlanmadı. Tekrar dene.",
    "copy_generation_failed": "Videonun paylaşım metnini yazma tamamlanmadı. Tekrar dene.",
    "output_upload_failed": "Hazır videon kaydedilemedi. Tekrar dene.",
    "user_clip_download_failed": "Kliplerinden biri indirilemedi. Yeniden ekle ve tekrar dene.",
    "user_clip_unusable": (
        "Kliplerinden biri kullanılamadı (bozuk ya da desteklenmiyor olabilir). Farklı bir "
        "klip dene."
    ),
    "template_misconfigured": "Bu şablonda bir yapılandırma sorunu var. Farklı bir tane dene.",
    "template_assets_missing": (
        "Bu şablonun ihtiyaç duyduğu dosyalar eksik. Farklı bir tane dene."
    ),
    "artifact_eligibility_revoked": "Bu dışa aktarma artık kullanılamıyor.",
    "eligibility_changed_during_export": "Dışa aktarma sırasında bir şey değişti. Tekrar dene.",
}
_JOB_FAILURE_DEFAULT_TR = (
    "Bir şeyler ters gitti ve bu video tamamlanmadı. Yönün ve çekimlerin hâlâ kayıtlı; tekrar dene."
)


def _job_failure_copy(failure_code: str | None, detail: str | None) -> str | None:
    """`content_plan_build.job_failure_message` in the chat's language.

    A Turkish chat gets the Turkish line for the code. The job's own ``error_detail`` for
    the creator-facing codes is English-only text, so Turkish prefers the code's line.
    """
    from app.tasks.content_plan_build import job_failure_message  # noqa: PLC0415

    english = job_failure_message(failure_code, detail)
    if english is None:
        return None
    return say(
        en=english,
        tr=_JOB_FAILURE_MESSAGES_TR.get(failure_code or "", _JOB_FAILURE_DEFAULT_TR),
    )


# The three variant ids every generative job renders; any other id is left out of the
# Turkish sentence rather than mixing an English machine name into it.
_VARIANT_NAMES_TR = {
    "song_lyrics": "Şarkı sözlü",
    "song_text": "Şarkılı yazılı",
    "original_text": "Orijinal sesli",
}


def _render_ready_review_copy(variant_id: str) -> str:
    """The default review sentence for a finished render (`{variant}` cut is ready)."""
    name = _VARIANT_NAMES_TR.get(variant_id)
    return say(
        en=(
            f"The {variant_id.replace('_', ' ')} cut is ready. "
            "The approved render finished; review the opening, pacing, and text, "
            "then tell me what you want changed."
        ),
        tr=(
            f"{name + ' versiyon' if name else 'Videon'} hazır. "
            "Onayladığın video tamamlandı; açılışı, temposunu ve yazıları incele, "
            "sonra neyi değiştirmek istediğini söyle."
        ),
    )


def _blocked_title_question(receipts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The tappable `title_text` question for a render blocked on a wordless title.

    Same conflict and digest as the draft-time question (`title_text_choice`), so the
    existing free-text / tap machinery answers it and the gate replays the answer; the
    copy already tells the creator both ways forward. Only for receipts that carry the
    wordless-title reason, and only while choice questions are on.
    """
    from app.kria.brief_checks import is_no_title_reason  # noqa: PLC0415
    from app.services.choice_questions import title_text_choice  # noqa: PLC0415

    if not settings.kria_choice_questions_enabled:
        return None
    ids = [
        str(r["requirement_id"])
        for r in receipts
        # KRI-520: the reason is English or Turkish, depending on the chat.
        if is_no_title_reason(r.get("reason")) and r.get("requirement_id")
    ]
    if not ids:
        return None
    return build_choice_question(title_text_choice(ids).candidate())


def _localized_block_recovery(message: str, receipts: list[dict[str, Any]]) -> str:
    """KRI-520: a render-block recovery the render worker wrote in English, re-worded for
    a Turkish chat.

    The worker has no chat language bound. Only a message that is exactly the English
    ``render_block_recovery`` of these receipts is rebuilt; anything else shows as stored.
    """
    if current_reply_language() != "tr" or not receipts:
        return message
    from app.kria.brief_checks import render_block_recovery  # noqa: PLC0415

    failures = [
        row
        for row in receipts
        if row.get("verification") == "checked" and row.get("status") != "met"
    ]
    if not failures:
        return message
    with reply_language_for("en"):
        english = render_block_recovery(failures).message
    if english.strip() != message.strip():
        return message
    return render_block_recovery(failures).message


def _observe_dispatched_execution(execution_id: uuid.UUID) -> tuple[str, str | None]:
    """Settle one dispatched receipt from durable Job truth."""

    from app.services.creator_sessions import (  # noqa: PLC0415
        PLAN_ITEM_JOB_FAILED,
        PLAN_ITEM_JOB_READY,
    )

    with sync_session() as db, ExitStack() as language:
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
        # KRI-520: the review and failure copy below follow the chat's language.
        _bind_thread_language(language, thread)

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
                review_text, review_receipts = _approved_generation_review(
                    db,
                    thread,
                    job,
                    variant,
                    execution,
                    _render_ready_review_copy(variant_id),
                )
                song_note = _user_song_note(job)
                if song_note:
                    review_text = f"{review_text} {song_note}"
                voice_note = _voice_behind_footage_note(job)
                if voice_note:
                    review_text = f"{review_text} {voice_note}"
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
        # Typed creator-contract decline (additive; the failure code is unchanged):
        # missing evidence is a repair/retry; an unavailable capability is a refusal
        # that asks for a different request; conflicts and choices keep the existing
        # behaviour until the clarification gate wires real questions.
        device_decline = (
            _device_contract_decline(job, str(execution.target_variant_id or "") or None)
            if device_failed
            else None
        )
        typed_decline = (
            device_decline
            if device_failed
            else _typed_creator_decline(
                job, str(execution.target_variant_id or "") or None, failure_code
            )
        )
        if typed_decline is not None and not device_failed:
            if _cloud_decline_needs_the_creator(failure_code, typed_decline["decline_reason"]):
                # KRI-470 PR-F: a cloud decline that no retry can fix is never a Retry button.
                deterministic = True
            elif (
                typed_decline["decline_reason"] == "evidence_missing"
                and failure_code not in _DETERMINISTIC_JOB_FAILURE_CODES
            ):
                # Retry only where the evidence can appear on a re-run (cloud
                # publication / preflight). A phone plan decline is deterministic.
                deterministic = False
            elif typed_decline["decline_reason"] == "capability_unavailable":
                deterministic = True
        recovery = "manual" if device_failed else ("ask_user" if deterministic else "retry")
        retryable = not device_failed and not deterministic
        if device_decline is not None:
            # The contract refused this edit, so the phone's own Retry (which re-pins the
            # same refused recipe) is a dead end, and nothing re-runs it automatically.
            # The creator's real next step is a new message, never a retry button.
            recovery = "ask_user"
            retryable = False
        execution.error = {
            "code": failure_code,
            "retryable": retryable,
            "recovery": recovery,
            **(
                {
                    key: typed_decline[key]
                    for key in ("decline_reason", "field_path")
                    if key in typed_decline
                }
                if typed_decline
                else {}
            ),
        }
        turn.status = "failed"
        turn.completed_at = now
        turn.error = execution.error
        session.status = "awaiting_feedback"
        session.last_error = execution.error
        from app.tasks.content_plan_build import CREATOR_FACING_DETAIL_CODES  # noqa: PLC0415

        recovery_receipts: list[dict[str, Any]] = []
        recovery_message: str | None = None
        raw_recovery = (job.assembly_plan or {}).get("request_recovery")
        expected_generation = (
            str((job.assembly_plan or {}).get("creator_generation_id") or "") or None
        )
        raw_binding = (job.assembly_plan or {}).get("creator_brief_binding")
        try:
            from app.kria.brief_binding import BriefBinding  # noqa: PLC0415
            from app.kria.contracts import RequirementReceipt  # noqa: PLC0415

            binding = BriefBinding.model_validate(raw_binding)
            approved_brief = binding.resolve(thread.id)
        except ValueError:
            approved_brief = None
            binding = None
        recovery_matches_binding = (
            isinstance(raw_recovery, dict)
            and isinstance(raw_recovery.get("message"), str)
            and raw_recovery["message"].strip()
            and binding is not None
            and raw_recovery.get("binding_digest") == binding.digest
            and raw_recovery.get("generation_id") == expected_generation
            and isinstance(raw_recovery.get("requirement_receipts"), list)
        )
        if (
            recovery_matches_binding
            and approved_brief is None
            and raw_recovery.get("brief_version") is None
            and not raw_recovery["requirement_receipts"]
        ):
            # A bound request can be exact even before extraction found a
            # structured requirement. Keep the specific recovery question;
            # there is no receipt to publish or judge.
            recovery_message = raw_recovery["message"].strip()
        elif (
            recovery_matches_binding
            and approved_brief is not None
            and raw_recovery.get("brief_version") == approved_brief.version
        ):
            try:
                receipts = [
                    RequirementReceipt.model_validate(row)
                    for row in raw_recovery["requirement_receipts"]
                ]
            except (TypeError, ValueError):
                receipts = []
            if len(receipts) == len(raw_recovery["requirement_receipts"]) and all(
                receipt.brief_version == approved_brief.version
                and receipt.generation_id == expected_generation
                for receipt in receipts
            ):
                recovery_receipts = [receipt.model_dump(mode="json") for receipt in receipts]
                recovery_message = raw_recovery["message"].strip()
        if device_decline is not None:
            from app.services.device_render import has_accepted_artifact  # noqa: PLC0415

            failure_content = _device_refusal_copy(
                device_decline,
                last_good=has_accepted_artifact(
                    job, str(execution.target_variant_id or "") or None
                ),
            )
        elif device_failed:
            failure_content = say(
                en=(
                    "Your iPhone couldn't finish the render. Your approved edit is still "
                    "saved: open the project on your iPhone and tap Retry."
                ),
                tr=(
                    "iPhone'un videoyu tamamlayamadı. Onayladığın düzenleme hâlâ kayıtlı: "
                    'projeyi iPhone\'unda açıp "Retry" düğmesine dokun.'
                ),
            )
        elif recovery_message is not None:
            failure_content = _localized_block_recovery(recovery_message, recovery_receipts)
        elif typed_decline is not None and (
            typed_decline["decline_reason"] == "capability_unavailable"
            or _cloud_decline_needs_the_creator(failure_code, typed_decline["decline_reason"])
        ):
            # The decline's own authored message plus the way forward. Only a TYPED decline
            # reaches here, so no raw exception text is ever shown.
            failure_content = _capability_refusal_copy(typed_decline)
        elif deterministic or failure_code in {
            "phone_capability_unavailable",
            *CREATOR_FACING_DETAIL_CODES,
        }:
            # Retryable, but the generic "didn't finish" copy would hide WHY (KRI-286).
            # Creator-facing codes (KRI-466) show the job's own actionable detail.
            failure_content = _job_failure_copy(failure_code, getattr(job, "error_detail", None))
        else:
            failure_content = say(
                en=(
                    "That render didn't finish. Your approved draft is still saved, "
                    "so you can retry without rebuilding the edit."
                ),
                tr=(
                    "Bu video tamamlanamadı. Onayladığın taslak hâlâ kayıtlı, düzenlemeyi "
                    "baştan kurmadan tekrar deneyebilirsin."
                ),
            )
        if (
            typed_decline is not None
            and typed_decline.get("alternative")
            and failure_code in _DETERMINISTIC_JOB_FAILURE_CODES
            and typed_decline["decline_reason"] != "capability_unavailable"
            and recovery_message is None
            and not device_failed
        ):
            # A deterministic phone decline keeps its copy and adds the way forward.
            failure_content = f"{failure_content} {typed_decline['alternative']}"
        title_question = _blocked_title_question(recovery_receipts)
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
                **({"choice_question": title_question} if title_question else {}),
                "receipt_ids": [str(execution.id)],
                **(
                    {
                        key: typed_decline[key]
                        for key in ("decline_reason", "field_path")
                        if key in typed_decline
                    }
                    if typed_decline
                    else {}
                ),
                **({"requirement_receipts": recovery_receipts} if recovery_receipts else {}),
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
            with sync_session() as db, ExitStack() as language:
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
                # KRI-520: this sweep has no turn; the notice follows the chat's language.
                _bind_thread_language(language, thread)
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
                    content=say(
                        en=(
                            "That approval expired before it was decided, so nothing was "
                            "rendered. Tell me what you want and I'll prepare it again."
                        ),
                        tr=(
                            "Bu onay karara bağlanmadan süresi doldu, o yüzden hiçbir video "
                            "oluşturulmadı. Ne istediğini söyle, yeniden hazırlayayım."
                        ),
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
