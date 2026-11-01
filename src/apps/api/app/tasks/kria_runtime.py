"""Durable runtime-v2 planning, editing, approval, and observation tasks.

Every consequential render remains separated from draft creation by a pinned,
persisted approval. Workers report only durable receipts and observed Job truth.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased
from sqlalchemy.pool import NullPool

from app.agents._runtime import ProviderOutcomeUnknownError
from app.config import settings
from app.database import sync_session
from app.db_locks import CONTENT_PLAN_LOCK
from app.kria.brief import (
    BriefRequirement,
    BriefUpdate,
    CreativeBrief,
    apply_updates,
    load_latest_brief_sync,
    persist_brief_version_sync,
    removed_target_ids,
    render_brief_request,
)
from app.kria.brief_binding import BriefBindingRequestTooLongError
from app.kria.brief_binding import snapshot_media as _draft_media
from app.kria.brief_checks import (
    NARRATED_ALIGNMENT_FIELD,
    _is_spoken_text_cue,
    _wants_bed_muted,
    _wants_source_sound_kept,
    build_receipts,
    clip_speed_notice,
    defers_caption_words_to_phone_render,
    defers_text_to_phone,
    defers_to_narrated_render,
    defers_to_unified_montage,
    describe_text_look,
    is_judged,
    judged_at_render,
    keeps_model_summary,
    needs_creator_choice,
    plan_facts_from_editor_payload,
    plan_facts_from_phone_variant,
    plan_facts_from_rendered_montage,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
    speech_cleanup_review_note,
    wants_light_change,
    with_planned_cuts,
)
from app.kria.contracts import KriaObservedTurnResponse, KriaToolReceipt, KriaTurnPlan
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.duplicate_send import is_duplicate_answer
from app.kria.editor_receipts import (
    bind_editor_receipts,
    receipt_lead,
    reply_from_diff,
    reply_from_editor_receipts,
)
from app.kria.language import is_paraphrase_only
from app.kria.order_replacement_receipts import compiled_order_preflight_sync
from app.kria.planner import (
    PlannedKriaTurn,
    answering_through_sequence,
    continuation_superseded_reply,
    extract_deferred_brief,
    localized_editor_state_reply,
    plan_live_turn,
    start_degraded_retry,
    turn_deadline,
)
from app.kria.registry import KRIA_TOOLS
from app.kria.reply_language import (
    bind_reply_language,
    current_reply_language,
    detect_chat_language,
    release_reply_language,
    reply_language_for,
    say,
    thread_reply_language,
)
from app.kria.route_disclosure import (
    UNIFIED_MONTAGE,
    card_disclosures,
    card_text,
    decoupled_from_readiness,
    draft_route,
    planned_card_length,
    requirements_with_route_declines,
    with_route_declines,
)
from app.kria.select_asks import exclusion_facts, only_video_ask
from app.kria.voiceover_script import event_content as voiceover_script_content
from app.models import (
    CREATOR_AGENT_RUNNING_TURN_STATUSES,
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
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
    CONFLICT_SPEECH_OCCURRENCE,
    CONFLICT_TITLE_TEXT,
    CONFLICT_WHICH_VOICE,
    KEEP_OPEN_REASON,
    MAX_ASKS_PER_QUESTION,
    SIMPLER_OMITTED_IDS_KEY,
    SIMPLER_VERSION_REASON,
    ChoiceCapability,
    answered_brief,
    build_choice_question,
    choice_question_text,
    count_asks,
    match_open_choice,
    open_conflicts,
    simpler_version_ask,
    simpler_version_question,
    tag_event,
)
from app.services.client_protocol import (
    APPROVAL_CLIENT_PROTOCOL_FIELD,
    bind_sending_build,
    unbind_sending_build,
)
from app.services.creation_thread_titles import conversation_revision_matches_sync
from app.services.creative_copy_gate import CREATIVE_COPY_PENDING, creative_copy_problem
from app.services.creator_label_reply import LabelTextReply, label_text_reply
from app.services.device_render import DEVICE_RENDER_FIELD, device_status
from app.services.kria_editor_ops import (
    EditorStateReplyError,
    EditorStateSpeechCutError,
    KriaEditorOpError,
    TextTimingConflict,
    _clip_label_links,
    build_editor_snapshot,
    compile_editor_ops,
    editor_payload_dump,
    editor_state_has_lanes,
    failed_editor_draft_basis,
    merge_editor_draft,
    parse_editor_state,
    resolve_editor_base,
    unknown_editor_render_query,
)
from app.services.kria_editor_ops_diff import describe_diff
from app.services.length_policy import creator_duration
from app.services.phone_rollout import stamped_client_protocol
from app.services.phone_speaker_background import (
    BACKGROUND_REFUSALS,
    pin_background_for_commit_sync,
)
from app.services.pin_withdrawal import pin_block_coverage, unrepeated_reply
from app.services.song_order import model_text
from app.services.thought_summaries import (
    ThoughtSummaryPublisher,
    bind_thought_publisher,
    publisher_for_creation,
)
from app.text_fold import loose_words
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
# KRI-709: a continued turn is re-published after this delay (its cached vision answers
# and clip analysis are already saved, so there is nothing to wait for but the worker).
_CONTINUE_COUNTDOWN_S = 5
# The continued turn's `lease_expires_at` is `countdown + slack` ahead, so the 60 s
# reconciler sweep neither republishes it early nor, if the publish was lost, misses it.
_CONTINUE_LEASE_SLACK_S = 30


@dataclass(frozen=True)
class _Completion:
    committed: bool
    successor_turn_id: str | None = None
    requeue_turn_id: str | None = None
    response_only: bool = False


class ApprovedCreatorRequestTooLongError(ValueError):
    """The approval fence cannot retain every creator instruction safely."""


def _text_timing_conflict_plan(exc: TextTimingConflict) -> KriaTurnPlan:
    if exc.constraint == "duration":
        closest = round(exc.required_duration_s, 3)
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=say(
                en=(
                    f"With these clips, the closest supported length is {closest:g}s. "
                    "Use that length, or choose different clips? Nothing was changed."
                ),
                tr=(
                    f"Bu kliplerle desteklenen en yakın süre {closest:g} saniye. "
                    "Bu süreyi mi kullanayım, yoksa farklı klipler mi seçelim? "
                    "Hiçbir şey değişmedi."
                ),
            ),
            reply_options=[
                say(en=f"Retime text to {closest:g}s", tr=f"Yazıları {closest:g} saniyeye yay"),
                say(en="Choose different clips", tr="Farklı klipler seç"),
            ],
        )
    if exc.constraint == "readability":
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=say(
                en=(
                    f"These words need at least {exc.minimum_duration_s:g}s of reading time. "
                    "May I use a longer video, or would you like to shorten the text? "
                    "Nothing was changed."
                ),
                tr=(
                    f"Bu yazılar en az {exc.minimum_duration_s:g} saniye "
                    "okuma süresi gerektiriyor. "
                    "Daha uzun bir video kullanayım mı, yoksa metni kısaltmak ister misin? "
                    "Hiçbir şey değişmedi."
                ),
            ),
        )
    return KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response=say(
            en=(
                f"Your text ends at {exc.required_duration_s:g}s, "
                f"beyond the requested {exc.requested_duration_s:g}s. "
                f"Keep the video at least {exc.minimum_duration_s:g}s, "
                "or allow me to adjust the text timing? Nothing was changed."
            ),
            tr=(
                f"Yazıların {exc.required_duration_s:g}. saniyede bitiyor. "
                f"Videoyu en az {exc.minimum_duration_s:g} saniye mi tutayım, "
                "yoksa yazıların zamanlamasını değiştirebilir miyim? Hiçbir şey değişmedi."
            ),
        ),
        reply_options=[
            say(
                en=f"Keep the video at {exc.minimum_duration_s:g}s",
                tr=f"Videoyu {exc.minimum_duration_s:g} saniye tut",
            ),
            say(
                en=f"Retime text to {exc.requested_duration_s:g}s",
                tr=f"Yazıları {exc.requested_duration_s:g} saniyeye yay",
            ),
        ],
    )


def _full_creator_request_sync(db, *, thread_id: uuid.UUID, through_sequence: int) -> str | None:  # noqa: ANN001
    """Pin every creator instruction visible to the approved source turn."""
    messages = [
        str(content).strip()
        for content, payload in db.execute(
            select(CreationThreadEvent.content, CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "user",
                CreationThreadEvent.sequence <= through_sequence,
                CreationThreadEvent.content.is_not(None),
            )
            .order_by(CreationThreadEvent.sequence)
        ).all()
        # KRI-552: a re-sent copy of a message adds no instruction to the pinned request.
        if str(content).strip() and not is_duplicate_answer(payload)
    ]
    request = "\n".join(messages)
    return request if len(request) <= 12_000 else None


def _marks_route_sync(  # noqa: ANN001
    db,
    *,
    thread: CreationThread,
    turn: CreatorAgentTurn,
    item,
    document,
) -> bool:
    """KRI-901: this act turn came from an app that shows the mark card AND its draft renders
    on the phone Talking writer (`protected_targets.marks_route`). The gate and approval
    readiness both read this one answer, so neither asks for nor promises a mark elsewhere."""
    from app.kria.protected_targets import marks_route  # noqa: PLC0415

    source = db.get(CreationThreadEvent, turn.source_event_id)
    payload = getattr(source, "payload", None)
    return marks_route(
        client_marks=isinstance(payload, dict) and payload.get("marks_protected_regions") is True,
        creator_id=thread.creator_id,
        strategy=document.strategy,
        edit_format=getattr(item, "edit_format", None) or document.edit_format,
        clip_paths=getattr(item, "clip_gcs_paths", None) or (),
        voiceover_path=getattr(item, "voiceover_gcs_path", None),
    )


def _protected_region_gate_sync(  # noqa: ANN001
    db,
    *,
    thread: CreationThread,
    turn: CreatorAgentTurn,
    item: PlanItem,
    brief: Any,
    marks_route: bool,
    render_requested: bool,
) -> tuple[KriaTurnPlan, dict[str, Any]] | None:
    """KRI-901: the "Mark the plate" question, when this act turn must ask it.

    Asked only when every part holds: the turn would request render approval, the brief is
    bound at approval (the mark rides the binding), the turn is on the mark route (an app
    that shows the card, and a draft the phone Talking writer renders: the only one that
    keeps text off a marked object; `_marks_route_sync`), and a live ``keep_text_off``
    object has no current mark. One object per question, brief order.
    """
    from app.kria.protected_targets import (  # noqa: PLC0415
        build_protected_region_question,
        clip_generations,
        mark_request_text,
        missing_object_marks,
        rendered_speaker_media_id,
        speaker_candidates,
    )

    if not render_requested or not marks_route:
        return None
    if not settings.brief_binding_for(thread.creator_id):
        return None
    source = db.get(CreationThreadEvent, turn.source_event_id)
    if source is None:
        return None
    if brief is None:
        brief = load_latest_brief_sync(db, thread.id)
    clip_assignments = getattr(item, "clip_assignments", None) or []
    generations = clip_generations(clip_assignments)
    # The mark counts only on a clip the writer may put the text on; once a render of this
    # item spoke from a clip, that clip is one of them, so a mark on another clip is asked
    # for again instead of being promised and read back as ``other_source``.
    job = getattr(item, "current_job", None)
    variants = ((getattr(job, "assembly_plan", None) or {}).get("variants")) or []
    missing = missing_object_marks(
        brief,
        _protected_regions_sync(db, thread_id=thread.id, through_sequence=int(source.sequence)),
        generations,
        render_clips=speaker_candidates(
            clip_assignments, rendered_speaker=rendered_speaker_media_id(variants)
        ),
    )
    if not missing:
        return None
    target, clips = missing[0]
    question = build_protected_region_question(
        target, question_id=f"mark-{turn.id.hex[:16]}", media_ids=clips
    )
    plan = KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response=mark_request_text(target.label),
        protected_region_question=question,
    )
    return plan, question


def _protected_regions_sync(db, *, thread_id: uuid.UUID, through_sequence: int) -> list[dict]:  # noqa: ANN001
    """KRI-827: the creator's marks of what to keep text off, from their own turns only."""
    from app.kria.protected_targets import fold_protected_regions  # noqa: PLC0415

    return fold_protected_regions(
        db.execute(
            select(CreationThreadEvent.role, CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "user",
                CreationThreadEvent.sequence <= through_sequence,
            )
            .order_by(CreationThreadEvent.sequence)
        ).all()
    )


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
    creator_request: str = ""
    brief_binding: dict[str, Any] | None = None
    preflight_analysis_id: uuid.UUID | None = None
    speech_cleanup_analysis_id: uuid.UUID | None = None
    speech_cleanup_choice: str | None = None
    # KRI-306: the creator's explicit output-shape choice (`{"output_orientation",
    # "landscape_fit"}`), stashed by `decide_approval`; None = they never chose.
    render_shape: dict[str, str] | None = None
    # KRI-898: the app build that approved (stashed by `decide_approval`); the Job carries it
    # as `creator_client_protocol` so the render plans a freeze only for a build that plays
    # it. None = unknown (web approval, a legacy approval), so no freeze is planned.
    client_protocol: int | None = None
    # What the plan item pointed at BEFORE a strategy dispatch mints its new Job, so a
    # failure after the pointer moves can put it back (never leave an orphan target).
    prior_item_status: str | None = None
    creator_guided_attempt_id: str | None = None
    # KRI-722: {id, digest, schema_version, attempt_id, approval_id, draft_id,
    # draft_revision, binding_digest} for a stamped approval; None = legacy.
    prepared_execution: dict[str, Any] | None = None


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
    brief_delegated: bool = False,
    brief_delegation_read_ids: tuple[str, ...] = (),
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
        if not conversation_revision_matches_sync(db, thread, claimed_thread_revision):
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
        if brief_updates or brief_delegated:
            # KRI-188: a question/recovery turn still records what the creator
            # stated. Written under the thread lock, after the revision fence,
            # so a requeued turn never persists a version. KRI-814: a delegating
            # request answered with a question, or held for another reason, is
            # still recorded as read, so the same chat can reach approval later.
            persisted_brief = persist_brief_version_sync(
                db,
                thread_id=thread.id,
                turn_id=turn.id,
                updates=brief_updates,
                record_empty=brief_delegated,
                delegation_read_event_ids=brief_delegation_read_ids,
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
            # KRI-815: a drafted script is also readable as prose by clients without the card.
            content=voiceover_script_content(plan),
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
                # KRI-723: persisted so the answer can be validated + folded later.
                **(
                    {"moment_order_question": plan.moment_order_question}
                    if plan.moment_order_question
                    else {}
                ),
                # KRI-588: persisted so the answer can be validated + folded later.
                **(
                    {"song_range_question": plan.song_range_question.model_dump(mode="json")}
                    if plan.song_range_question is not None
                    else {}
                ),
                **({"choice_question": plan.choice_question} if plan.choice_question else {}),
                # KRI-901: the "Mark the plate" card.
                **(
                    {"protected_region_question": plan.protected_region_question}
                    if plan.protected_region_question
                    else {}
                ),
                # KRI-815: the script card + its record/import continuation (spoken words only).
                **({"voiceover_script": plan.voiceover_script} if plan.voiceover_script else {}),
                # KRI-738: tappable lengths; the prose above still lists them for old clients.
                **({"options": plan.reply_options} if plan.reply_options else {}),
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


def _continue_turn(turn_id: uuid.UUID, *, lease_owner: str, lease_epoch: int) -> str:
    """KRI-709: release a turn whose planner stopped on a time budget so it runs again.

    Returns ``"continued"`` (turn is ``pending`` again, caller publishes it),
    ``"exhausted"`` (this was already run ``kria_turn_auto_continue_max_runs``, or the
    switch is off: answer with the old reply), ``"successor"`` (the creator already sent
    a newer message: answer and let it run) or ``"lost"`` (this run no longer owns the
    turn, e.g. it was cancelled: write nothing).

    Runs are counted by `lease_epoch` (+1 per claim), not by turn age: a message that sat
    queued behind another turn is old before it ever plans, and every run can pay for a
    Main Creator call, so the run count is also the spend bound.

    Same lock discipline as `_complete_response_turn`: the turn row under FOR UPDATE and
    the lease re-checked, so a cancel during planning stays cancelled. No event is
    written and no brief version persisted: the next run does both.
    """
    if lease_epoch >= int(settings.kria_turn_auto_continue_max_runs):
        return "exhausted"
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
            return "lost"
        if _lock_queued_successor(db, turn.thread_id) is not None:
            db.rollback()
            return "successor"
        turn.status = "pending"
        turn.lease_owner = None
        turn.lease_expires_at = database_now + timedelta(
            seconds=_CONTINUE_COUNTDOWN_S + _CONTINUE_LEASE_SLACK_S
        )
        db.commit()
    log.info("kria_turn_auto_continued", turn_id=str(turn_id), lease_epoch=lease_epoch)
    return "continued"


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


def _receipt_lead_fields(
    lead: str | None, also_changed: Sequence[str], receipts: Sequence[Any]
) -> dict[str, Any]:
    """KRI-739: the lead line + unclaimed changes shown above the receipt rows. Only an editor turn
    that bound receipts has them; every other event stays byte-identical."""
    if not lead or not receipts:
        return {}
    return {"reply_lead": lead, **({"also_changed": list(also_changed)} if also_changed else {})}


def _unresolved_choice_plan(
    strategy: Mapping[str, Any] | None,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    *,
    contract_brief: Any = None,
    events: Any = (),
    has_draft: bool = False,
    creator_id: Any = None,
    client_protocol: int | None = None,
    allow_choices: bool = True,
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
    from app.kria.unclear_word_ask import clarification_question  # noqa: PLC0415

    clarification = clarification_question(
        brief, media_snapshot, strategy=strategy, creator_id=creator_id
    )
    if clarification is not None:
        return (
            KriaTurnPlan(
                mode="respond", turn_value="question", response=f"{clarification}{unchanged}"
            ),
            "unresolved_requirement",
        )
    if not allow_choices:
        return None
    capability = ChoiceCapability(
        creator_id=creator_id,
        voice_route=settings.voice_behind_footage_enabled,
        speech_occurrence=settings.speech_occurrence_question_enabled,
        client_protocol=client_protocol,  # KRI-898: no freeze question for an older build
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
        if conflict.kind in (
            CONFLICT_ORDER_BASIS,
            CONFLICT_TITLE_TEXT,
            CONFLICT_WHICH_VOICE,
            CONFLICT_SPEECH_OCCURRENCE,  # KRI-724: only the creator knows who says it
        ):
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
        # KRI-667: the contract writes English; a Turkish chat gets the Turkish twin or a
        # Turkish line (`_worker_copy`), never the English sentence.
        reason = _worker_copy(str(exc)) or say(
            en=str(exc),
            tr="Onayladığın isteklerle bu düzenlemeyi güvenle oluşturamıyorum.",
        )
        return (
            KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=f"{reason}{unchanged}",
            ),
            "unresolved_requirement",
        )
    if contract is not None and contract.unresolved:
        label_reply = _label_text_unresolved(contract.unresolved[0], contract_brief)
        if label_reply is not None:
            # KRI-804: a dictated text a label can't print is a question about its words,
            # with tappable ways forward, never a recovery with nothing to tap.
            return (
                KriaTurnPlan(
                    mode="respond",
                    turn_value="question",
                    response=f"{label_reply.question}{unchanged}",
                    reply_options=list(label_reply.options) or None,
                ),
                "label_text_unusable",
            )
        quoted = _quoted_unresolved(contract.unresolved[0])
        if quoted is not None:
            # KRI-1025: a clip the contract names ("which clip "X" means") comes with the
            # way forward, never as a reply with nothing to do next.
            reason, step = quoted
            question = f"{reason} {say(en=step[0], tr=step[1])}"
        else:
            question = _worker_copy(contract.unresolved[0]) or say(
                en=contract.unresolved[0],
                tr="Onayladığın isteklerden birini bu düzenlemede doğrulayamıyorum.",
            )
        return (
            KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=f"{question}{unchanged}",
            ),
            "unresolved_requirement",
        )
    return None


def _unstuck_recovery(
    db: Any,
    thread_id: uuid.UUID,
    plan: KriaTurnPlan,
    brief: Any,
    notices: tuple[str, ...],
) -> KriaTurnPlan:
    """KRI-1068: the backstop's reply, never word for word the last message Kria showed
    (a reply, a draft or a render note), and saying first what this turn's "skip that"
    dropped (``notices``)."""
    previous = db.execute(
        select(CreationThreadEvent.content)
        .where(
            CreationThreadEvent.thread_id == thread_id,
            CreationThreadEvent.role == "assistant",
            CreationThreadEvent.content.is_not(None),
        )
        .order_by(CreationThreadEvent.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    response = " ".join([*notices, unrepeated_reply(plan.response or "", previous, brief)])
    return plan if response == plan.response else plan.model_copy(update={"response": response})


def _label_text_unresolved(unresolved: str, brief: Any) -> LabelTextReply | None:
    """The label-text question behind the contract's ``unresolved`` line, if it is one.

    ``build_render_contract`` writes ``label_text_reply`` for a dictated shot text a label
    can't print (KRI-804), in the language this turn is bound to; the same brief text
    rebuilds it here with its tappable options.
    """
    for requirement in brief.live() if brief is not None else ():
        if not requirement.is_shot_text:
            continue
        reply = label_text_reply([requirement.literal], keep=False)
        if reply is not None and reply.question == unresolved:
            return reply
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


SimplerConsent = Literal["named", "unnamed"]
# `KriaTurnPlan.response` max_length: the held-draft reply must fit it whole.
_MAX_TURN_RESPONSE_CHARS = 1200
# A "leaves out" sentence longer than this is not shown (and not recorded): the offer then
# consents to nothing by name, and the draft says "in the simpler version", not "as agreed".
_MAX_LEAVES_OUT_CHARS = 600


def _simpler_version_offer(
    brief: CreativeBrief | None, requirement_ids: Sequence[str]
) -> tuple[str, dict[str, Any]]:
    """KRI-968: the sentence naming what the simpler version leaves out, and the question
    that records exactly those ids (brief order, the order the sentence names them in) and
    the brief revision it named them at. ``("", plain question)`` when nothing can be named.
    """
    question = simpler_version_question()
    if brief is None:
        return "", question
    wanted = set(requirement_ids)
    named = [req for req in brief.live() if req.id in wanted]
    if not named:
        return "", question
    listed = "; ".join(req.text() for req in named)
    sentence = say(
        en=f"The simpler version leaves out: {listed}.",
        tr=f"Daha basit sürümde çıkarılacak: {listed}.",
    )
    if len(sentence) > _MAX_LEAVES_OUT_CHARS:
        return "", question
    named_ids = [req.id for req in named]
    for option in question["options"]:
        if option["key"] == "simpler_version":
            option["description"] = say(en=f"Leaves out: {listed}", tr=f"Çıkarılacak: {listed}")
    # An unmet id with no live row has nothing to drop; it is still part of the set the
    # creator answers, so a changed set is offered again.
    question[SIMPLER_OMITTED_IDS_KEY] = [
        *named_ids,
        *(req_id for req_id in dict.fromkeys(requirement_ids) if req_id not in named_ids),
    ]
    question["brief_version"] = brief.version
    return sentence, question


def _fit_reply(body: str, tail: str) -> str:
    """``body``, then ``tail`` on its own line, within the turn plan's response limit. The
    tail (what is held back, what the simpler version leaves out, the question) stays whole;
    the receipt lines above it are shortened first."""
    room = _MAX_TURN_RESPONSE_CHARS - len(tail) - 1
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return f"{body}\n{tail}"


def _left_out_line(req: BriefRequirement, *, agreed: bool) -> str:
    """One requirement the simpler version left out. "As agreed" only when the answered
    offer named it (KRI-968); an older offer that named nothing says what happened."""
    if agreed:
        return say(
            en=f"- Left out as agreed: {req.text()}",
            tr=f"- Kararlaştırdığımız gibi çıkarıldı: {req.text()}",
        )
    return say(
        en=f"- Left out in the simpler version: {req.text()}",
        tr=f"- Daha basit sürümde çıkarıldı: {req.text()}",
    )


def _with_left_out(text: str, lines: str) -> str:
    """``lines`` go with the Done/Partly lines, above "Have a look at these in the video:"
    (the `reply_from_receipts` header), so a drop never reads as something to check."""
    if not lines:
        return text
    if not text:
        return lines
    header = say(en="Have a look at these in the video:", tr="Bunlara videoda bir göz at:")
    if text.startswith(header):
        return f"{lines}\n{text}"
    head, separator, tail = text.partition(f"\n{header}")
    if separator:
        return f"{head}\n{lines}{separator}{tail}"
    return f"{text}\n{lines}"


def _left_out_lines(brief: CreativeBrief, coverage: Mapping[str, Any]) -> list[str]:
    """The approved draft's simpler-version omissions, for the after-render review (KRI-968).

    Only ids the approved brief really superseded: an id live again (restored later) or
    unknown to the brief says nothing.
    """
    agreed = {str(req_id) for req_id in coverage.get("agreed_omission_ids") or ()}
    by_id = {req.id: req for req in brief.requirements}
    lines = []
    for req_id in coverage.get("accepted_omission_ids") or ():
        req = by_id.get(str(req_id))
        if req is not None and not req.live:
            lines.append(_left_out_line(req, agreed=req.id in agreed))
    return list(dict.fromkeys(lines))


def _approved_draft_coverage(db: Any, execution: Any) -> Mapping[str, Any]:
    """The ``brief_coverage`` of the draft a render execution approved ({} when unknown)."""
    draft_id = getattr(execution, "target_draft_id", None)
    if db is None or draft_id is None:
        return {}
    draft = db.get(CreatorEditDraft, draft_id)
    coverage = (draft.snapshot_json or {}).get("brief_coverage") if draft is not None else None
    return coverage if isinstance(coverage, Mapping) else {}


def _offered_brief_revision(
    offered: object,
    brief: CreativeBrief,
    named: Sequence[object],
    source_event: CreationThreadEvent,
) -> bool:
    """``brief`` is the revision a named simpler-version offer was made at, or (KRI-1113) the
    next one, written by the answering turn itself restating only the asks it names.

    Prod thread 83f2dcaa (2026-10-31 13:51): the Main Creator restated r4 when the creator
    tapped "Make it without these", the turn persisted brief v8 and the v7 offer stopped
    counting, so the same offer came back. A restatement of what is being left out is not a new
    instruction (the runtime still refuses one beside it).
    """
    if offered == brief.version:
        return True
    turn_id = str((source_event.payload or {}).get("turn_id") or "")
    written = {req.id for req in brief.requirements if turn_id and req.source_turn_id == turn_id}
    return (
        isinstance(offered, int)
        and brief.version == offered + 1
        and bool(written)
        and written <= {str(req_id) for req_id in named}
    )


def _simpler_version_consent(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    thread_id: uuid.UUID,
    requirement_ids: list[str],
    *,
    brief: CreativeBrief | None,
    source_event: CreationThreadEvent | None,
) -> SimplerConsent | None:
    """Explicit acceptance of these exact omissions at the offered brief revision.

    ``"named"``: the offer's reply named exactly these requirements as left out (KRI-968).
    ``"unnamed"``: a legacy offer (no list) that blocked exactly these; it is still honoured,
    but nothing may say the creator agreed to a list they were never shown.

    Legacy offers have receipt versions but no choice card. They use the same exact
    label/alias matcher, preserving KRI-610's text fallback without treating silence,
    refusal, another question, or an old stable requirement ID as consent (KRI-698).
    """
    if not requirement_ids or brief is None or source_event is None:
        return None
    rows = db.execute(
        select(CreationThreadEvent.event_type, CreationThreadEvent.payload)
        .where(
            CreationThreadEvent.thread_id == thread_id,
            CreationThreadEvent.event_type.in_(("assistant_response", "draft_applied")),
        )
        .order_by(CreationThreadEvent.sequence.desc())
        .limit(50)
    ).all()
    for event_type, payload in rows:
        if event_type == "draft_applied":
            return None
        payload = payload if isinstance(payload, Mapping) else {}
        coverage = payload.get("brief_coverage")
        if not isinstance(coverage, Mapping) or coverage.get("reason") not in {
            SIMPLER_VERSION_REASON,
            "approval_requirements_not_ready",
        }:
            continue
        question = payload.get("choice_question") or simpler_version_question()
        named = question.get(SIMPLER_OMITTED_IDS_KEY)
        if isinstance(named, list):
            # KRI-968: consent covers exactly what the reply named, at that revision. A
            # changed set (a new miss, a different draft) is offered again, never dropped.
            if set(requirement_ids) != {
                str(req_id) for req_id in named
            } or not _offered_brief_revision(
                question.get("brief_version"), brief, named, source_event
            ):
                return None
            consent: SimplerConsent = "named"
        elif not _legacy_offer_matches(payload, coverage, requirement_ids, brief):
            return None
        else:
            consent = "unnamed"
        selection = (source_event.payload or {}).get("choice_selection")
        if isinstance(selection, Mapping):
            return (
                consent
                if selection.get("question_id") == question.get("question_id")
                and selection.get("option_key") == "simpler_version"
                else None
            )
        # A bare number may answer an intervening clip question. Only the live
        # card's validated selection can use its ordinal; a text fallback must
        # name the simpler option itself.
        named_option = {
            "options": [o for o in question.get("options", []) if o.get("key") == "simpler_version"]
        }
        return (
            consent
            if match_open_choice(named_option, source_event.content) == "simpler_version"
            else None
        )
    return None


def _legacy_offer_matches(
    payload: Mapping[str, Any],
    coverage: Mapping[str, Any],
    requirement_ids: list[str],
    brief: CreativeBrief,
) -> bool:
    """An offer stored before KRI-968 named nothing: it blocked these exact ids, at this
    brief revision, judging by its own receipts / readiness rows."""
    from app.kria.approval_readiness import APPROVABLE  # noqa: PLC0415

    readiness = coverage.get("approval_readiness")
    blocked_ids = (
        {
            row.get("requirement_id")
            for row in readiness.get("requirements", [])
            if row.get("disposition") not in APPROVABLE
        }
        if isinstance(readiness, Mapping)
        else None
    )
    receipts = [
        receipt
        for receipt in payload.get("requirement_receipts") or []
        if isinstance(receipt, Mapping)
        and (
            receipt.get("requirement_id") in blocked_ids
            if blocked_ids is not None
            else needs_creator_choice(receipt)
        )
    ]
    offered = {str(receipt.get("requirement_id")) for receipt in receipts}
    return set(requirement_ids) == offered and {r.get("brief_version") for r in receipts} == {
        brief.version
    }


def _policy_text_omission_reason(
    req: BriefRequirement, omitted_texts: Mapping[str, Sequence[str]]
) -> str | None:
    """Match exact brief copy to server-proven compiler omissions, never a model claim."""
    if req.kind != "text" or not req.literal or _is_spoken_text_cue(req):
        return None
    if _wants_source_sound_kept(req) or _wants_bed_muted(req) or "audio_intent" in req.facts:
        return None
    fields = (
        ("opening_title",)
        if req.scope == "title"
        else ("shot_labels", "pinned_texts")
        if req.scope == "per_clip" or req.scope.startswith("clip:")
        else ("opening_title", "closing_title", "pinned_texts")
        if req.scope in {"global", "closing", "ending"}
        else ()
    )
    literal = " ".join(req.literal.split())
    if any(
        literal == " ".join(text.split())
        for field in fields
        for text in omitted_texts.get(field, ())
    ):
        return say(
            en="This kind of edit can't show this text yet.",
            tr="Bu tür düzenleme bu yazıyı henüz gösteremiyor.",
        )
    return None


def _policy_text_reasons(
    brief: CreativeBrief | None, omitted_texts: Mapping[str, Sequence[str]]
) -> dict[str, str]:
    """KRI-1009: the creator's quoted copy the compiler proved it can't draw, by requirement,
    with that reason. The question before the draft says why, never "isn't in this draft"
    (nothing the creator does to the draft brings it back)."""
    if brief is None or not omitted_texts:
        return {}
    return {
        req.id: reason
        for req in brief.live()
        if (reason := _policy_text_omission_reason(req, omitted_texts)) is not None
    }


def _automatic_omission_reason(req: BriefRequirement, strategy: Mapping[str, Any]) -> str | None:
    """Prove a supported fallback from a known capability, not a missing draft field."""
    if (
        req.kind not in {"text", "style"}
        or _wants_source_sound_kept(req)
        or _wants_bed_muted(req)
        or "audio_intent" in req.facts
    ):
        return None
    if wants_light_change(req):
        # A brightness fact can share a row with independently requested copy.
        # Only the known, already-kept title has a safe partial fallback here;
        # absent/other copy still needs repair or exact compiler-omission proof.
        if req.literal and (
            req.kind != "text"
            or req.scope != "title"
            or not isinstance(strategy.get("opening_title"), str)
            or " ".join(strategy["opening_title"].split()) != " ".join(req.literal.split())
        ):
            return None
        return say(
            en="This edit cannot change the footage's brightness yet.",
            tr="Bu düzenleme henüz çekimin parlaklığını değiştiremiyor.",
        )
    # Exact shot labels belong to the guided renderer (creator_capabilities).
    # A guided draft that forgot them is repairable, not an unsupported request.
    # Spoken cues retain their own timing/trigger-word questions.
    if (
        req.kind == "text"
        and (req.scope == "per_clip" or req.scope.startswith("clip:"))
        and not _is_spoken_text_cue(req)
        and (
            strategy.get("edit_format") == "subtitled" or strategy.get("render_program") == "native"
        )
    ):
        return say(
            en="This edit cannot show separate text on each clip yet.",
            tr="Bu düzenleme henüz her klipte ayrı yazı gösteremiyor.",
        )
    return None


def _automatic_omission_ids(
    brief: CreativeBrief | None,
    requirement_receipts: Sequence[Mapping[str, Any]],
    readiness_report: Mapping[str, Any] | None,
    *,
    strategy: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return only deterministic text/style requirements safe to omit automatically.

    A missing choice, selection, order, audio or timing requirement changes the edit's
    meaning, so it must still stop for the creator.  This allowlist is deliberately
    narrower than the general ``unmet_ids`` set used by the explicit simpler-version
    flow.  Unchecked and evidence-missing rows are also excluded: they do not prove the
    renderer cannot perform the request.

    KRI-1009: the creator's own quoted words are never dropped without asking, even when
    the compiler proved it can't draw them (`_policy_text_omission_reason`). The draft is
    held back with the "simpler version?" question that names them; a tap drops them as
    agreed. A brightness facet beside a kept title stays automatic: no words are lost.
    """
    if brief is None:
        return ()
    allowed: set[str] = set()
    by_id = {
        req.id: req
        for req in brief.live()
        if not (req.kind == "text" and req.literal and not wants_light_change(req))
        and _automatic_omission_reason(req, strategy or {}) is not None
    }
    if readiness_report is not None:
        if readiness_report.get("policy_problem"):
            return ()
        for row in readiness_report.get("requirements") or ():
            if not isinstance(row, Mapping) or row.get("disposition") != "unsupported":
                continue
            req = by_id.get(str(row.get("requirement_id")))
            if req is not None:
                allowed.add(req.id)
    else:
        for row in requirement_receipts:
            if row.get("verification") != "checked" or row.get("status") != "not_possible":
                continue
            req = by_id.get(str(row.get("requirement_id")))
            if req is not None:
                allowed.add(req.id)
    return tuple(req.id for req in brief.live() if req.id in allowed)


def _append_automatic_omissions_sync(
    db: Any,
    *,
    thread_id: uuid.UUID,
    brief: CreativeBrief,
    updates: Sequence[BriefUpdate],
) -> CreativeBrief:
    """Append the automatic omission as a second ledger version in this turn.

    The creator's additions are persisted first for normal per-turn idempotency.  A
    second append is therefore required when the same turn discovers a deterministic
    unsupported text/style requirement after compiling the draft.  ``source_turn_id``
    stays NULL because the first version already owns the turn's source updates.
    """
    merged = apply_updates(
        brief, updates, source_turn_id=None, server_removed_ids=removed_target_ids(updates)
    )
    next_version = (
        int(
            db.execute(
                select(func.coalesce(func.max(CreativeBriefVersion.version), 0)).where(
                    CreativeBriefVersion.thread_id == thread_id
                )
            ).scalar_one()
        )
        + 1
    )
    merged = CreativeBrief(version=next_version, requirements=merged.requirements)
    db.add(
        CreativeBriefVersion(
            thread_id=thread_id,
            version=next_version,
            requirements=[req.model_dump(mode="json") for req in merged.requirements],
            source_turn_id=None,
        )
    )
    db.flush()
    return merged


def _complete_draft_turn(
    turn_id: uuid.UUID,
    *,
    lease_owner: str,
    lease_epoch: int,
    claimed_thread_revision: int,
    planned: PlannedKriaTurn,
    client_protocol: int | None = None,
) -> _Completion:
    # KRI-898: ``client_protocol`` is the app build that sent this message (the claim
    # snapshot's stamp); a freeze is planned for this draft only if that build plays one.
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
    editor_diff_trace: list[dict[str, Any]] = []
    state_id: str | None = None
    your_edits_snapshot: dict[str, Any] | None = None
    your_edits_hash = ""
    restore_record: dict[str, Any] | None = None  # KRI-442: pre-update lanes for Undo
    scope_note = ""  # KRI-441: "I left X alone." for what layer 3 took back
    if apply_intent.tool_name == "draft.apply_strategy":
        changes = _strategy_changes(arguments)
        document = KriaDraftDocument(
            kind="strategy",
            intent=arguments.summary,
            edit_format=arguments.strategy.edit_format,
            strategy=arguments.strategy.model_dump(mode="json", exclude_none=True),
            changes=changes,
            prepared_collection_story=getattr(planned, "prepared_collection_story", None),
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
        if not conversation_revision_matches_sync(db, thread, claimed_thread_revision):
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
                brief_delegated=planned.brief_delegated,
                brief_delegation_read_ids=planned.brief_delegation_read_ids,
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
            if (plan.diagnostics or {}).get("editor_recovery_basis") is not None or variant.get(
                "render_status"
            ) in {"failed", "needs_attention"}:
                basis = failed_editor_draft_basis(
                    head,
                    job,
                    variant,
                    thread_id=thread.id,
                    creator_id=thread.creator_id,
                    item_id=item.id,
                )
                if (
                    basis is None
                    or (plan.diagnostics or {}).get("editor_recovery_basis") != basis._asdict()
                ):
                    raise KriaEditorOpError(
                        "The saved draft changed. Your correction was not applied; "
                        "review the current draft first."
                    )
                if (
                    db.execute(
                        unknown_editor_render_query(session, job, variant)
                    ).scalar_one_or_none()
                    is not None
                ):
                    raise KriaEditorOpError(
                        "The previous render's outcome is still unknown. "
                        "Check its status before starting another render."
                    )
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
            from app.kria.editor_sequence_timing import (
                sequence_timing_plan_from_operations,
                timing_only_operations,
            )
            from app.services.kria_editor_ops_text import validate_timing_only_text

            source_event = db.get(CreationThreadEvent, turn.source_event_id)
            timing_snapshot = build_editor_snapshot(job, editor_base.projected)
            preserve_text = any(
                update.facts.get("preserve_editor_text") is True for update in planned.brief_updates
            ) or any(op.get("preserve_text") is True for op in arguments.operations)
            timing_plan = sequence_timing_plan_from_operations(
                arguments.operations, timing_snapshot, preserve_text=preserve_text
            )
            compiled = compile_editor_ops(
                job, editor_base.projected, arguments.operations, preserve_text=preserve_text
            )
            if timing_only_operations(arguments.operations) or any(
                update.facts.get("editor_timing_only") is True for update in planned.brief_updates
            ):
                validate_timing_only_text(
                    editor_base.projected.get("text_elements") or [],
                    compiled.payload.text_elements
                    if isinstance(compiled.payload, EditorCommitRequest)
                    and compiled.payload.text_elements is not None
                    else editor_base.projected.get("text_elements") or [],
                )
            scoped_fields = _scoped_turn_fields(source_event)
            if scoped_fields is not None:
                # KRI-441 layer 3: whatever the model or a manual edit slipped outside the
                # flagged sections is stripped from the compiled draft, never failed.
                from app.kria import plan_review  # noqa: PLC0415

                clip_bar_ids = frozenset(_clip_label_links(job, editor_base.projected))
                compiled, repair = plan_review.repair_compiled(
                    compiled, scoped_fields["scope"], clip_bar_ids=clip_bar_ids
                )
                dropped = (plan.diagnostics or {}).get("scope_dropped") or []
                scope_note = plan_review.repair_note(
                    repair, [str(d.get("section")) for d in dropped]
                )
                if repair or dropped:
                    log.info(
                        "plan_review_scope_repaired",
                        thread_id=str(thread.id),
                        turn_id=str(turn.id),
                        dropped_ops=[d.get("op") for d in dropped],
                        reverted_fields=[*repair.reverted_fields, *repair.reverted_bars],
                    )
                if isinstance(compiled.payload, EditorCommitRequest):
                    repaired_data = editor_payload_dump(compiled.payload)
                    if not plan_review.lanes_in(repaired_data):
                        raise KriaEditorOpError(
                            say(
                                en="that would only change sections you did not flag",
                                tr="bu yalnızca işaretlemediğin bölümleri değiştirirdi",
                            )
                        )
                    restore_record = plan_review.build_restore_record(
                        repaired_data,
                        compiled.before,
                        scoped_fields["scope"],
                        turn_id=str(turn.id),
                        base_job_id=str(job.id),
                        clip_bar_ids=clip_bar_ids,
                    )
                if head is None and not (
                    editor_base.source == "client_state" and editor_state_has_lanes(client_state)
                ):
                    # A baseline revision under the update, so "Undo all" has a parent.
                    from app.kria.drafts import _editor_snapshot  # noqa: PLC0415

                    baseline = KriaDraftDocument(
                        kind="editor",
                        intent="Before update",
                        edit_format=str(item.edit_format or "montage"),
                        editor_payload=_editor_snapshot(
                            dict(editor_base.projected), canonical_generation_id
                        ),
                        changes=[],
                    )
                    your_edits_snapshot, your_edits_hash = canonical_snapshot(baseline)
            changes = compiled.changes
            editor_diff_trace = compiled.diff.to_json()
            state_id = (
                client_state.client_state_id if editor_base.source == "client_state" else None
            )
            document = KriaDraftDocument(
                kind="editor",
                intent=arguments.summary,
                edit_format=str(item.edit_format or "montage"),
                editor_payload=(
                    merge_editor_draft(prior_payload, editor_payload_dump(compiled.payload))
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
        if scope_note:
            reply_text = f"{reply_text} {scope_note}".strip()
        requirement_receipts: list[dict[str, Any]] = []
        # KRI-739: an editor turn's one-line lead and its unclaimed changes. The chat shows them
        # above the receipt rows; `reply_text` keeps the full prose for older clients and the web.
        reply_lead: str | None = None
        also_changed: list[str] = []
        effective_brief_updates = planned.brief_updates
        readiness_required = render_intent is not None and settings.approval_readiness_for(
            thread.creator_id
        )
        readiness_report = None
        # KRI-901: one answer for the mark gate and readiness (an app with the mark card and a
        # draft the phone Talking writer renders); off it neither asks nor promises.
        _marks_route = render_intent is not None and _marks_route_sync(
            db, thread=thread, turn=turn, item=item, document=document
        )
        brief = None
        speed_notice: str | None = None  # KRI-689
        route_name: str | None = None  # the render route this strategy draft lands on
        route_disclosures: list = []  # route_disclosure: what that route does not read
        phone_blockers: list = []  # KRI-775
        draft_research = None  # KRI-881
        if planned.brief_route is not None:
            # KRI-188: persist this turn's requirements (idempotent per turn),
            # then check each one deterministically against what was drafted.
            current_brief = load_latest_brief_sync(db, thread.id)
            if planned.brief_expected_version is not None:
                if planned.brief_expected_version != (
                    current_brief.version if current_brief else 0
                ):
                    raise RuntimeError("The request changed during planning; please retry")
            if apply_intent.tool_name == "draft.apply_editor_ops" and timing_plan is not None:
                from app.kria.editor_sequence_timing import merge_sequence_brief_updates

                canonical_plan = timing_plan
                if (
                    timing_plan.intent is not None
                    and timing_plan.intent.mode == "fill"
                    and timing_plan.span_s is None
                    and timing_plan.intent.target_s is None
                    and compiled.canonical_duration_s is not None
                ):
                    canonical_plan = replace(
                        timing_plan,
                        intent=replace(timing_plan.intent, target_s=compiled.canonical_duration_s),
                    )
                effective_brief_updates = merge_sequence_brief_updates(
                    current_brief,
                    planned.brief_updates,
                    canonical_plan,
                    timing_snapshot,
                    source_event.content or "" if source_event else "",
                )
            brief = persist_brief_version_sync(
                db,
                thread_id=thread.id,
                turn_id=turn.id,
                updates=effective_brief_updates,
                # KRI-814: a typed delegation is recorded, so readiness reads it as a
                # delegated request instead of a missing extraction.
                record_empty=planned.brief_delegated,
                delegation_read_event_ids=planned.brief_delegation_read_ids,
            )
            if brief is not None and effective_brief_updates != planned.brief_updates:
                current_ids = [req.id for req in brief.live()]
                planned = replace(
                    planned,
                    brief_updates=effective_brief_updates,
                    brief_coverage={
                        **(planned.brief_coverage or {}),
                        "applicable_ids": current_ids,
                        "retrieved_ids": current_ids,
                        "enforced_ids": [],
                        "unresolved_ids": current_ids,
                    },
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
                    # KRI-705: a phone Talking draft answers every ask plainly, so the
                    # approval text cannot promise a cut or a mix change no receipt backs.
                    # KRI-793: caption language/spellings remain a render-time plan.
                    talking_writer = defers_caption_words_to_phone_render(
                        creator_id=thread.creator_id,
                        edit_format=document.strategy.get("edit_format") or item.edit_format,
                        audio_strategy=document.strategy.get("audio_strategy"),
                        clip_paths=item.clip_gcs_paths or (),
                    )
                    phone_talking = talking_writer and (
                        document.strategy.get("edit_format") or item.edit_format
                    ) in {"subtitled", "talking_head"}
                    facts = replace(
                        facts,
                        phone_talking=phone_talking,
                        caption_words_at_render=talking_writer,
                        # KRI-1024: any phone writer draws text with the phone's fonts, so a
                        # literal none of them draws is a "Couldn't" before approval.
                        phone_text=defers_text_to_phone(
                            creator_id=thread.creator_id, clip_paths=item.clip_gcs_paths or ()
                        ),
                        # KRI-842: one clip's own sound is judged on the draft that the phone
                        # Voiceover compiler will render, against the footage it will play.
                        phone_narrated=defers_to_narrated_render(
                            creator_id=thread.creator_id,
                            edit_format=document.strategy.get("edit_format") or item.edit_format,
                            audio_strategy=document.strategy.get("audio_strategy"),
                            clip_paths=item.clip_gcs_paths or (),
                        ),
                        # KRI-942: clip sound on/off is judged on a draft the phone montage
                        # writer will render.
                        phone_montage=defers_to_unified_montage(
                            creator_id=thread.creator_id,
                            edit_format=document.strategy.get("edit_format") or item.edit_format,
                            audio_strategy=document.strategy.get("audio_strategy"),
                            clip_paths=item.clip_gcs_paths or (),
                        ),
                        media_snapshot=planned.media_snapshot
                        if planned.media_snapshot is not None
                        else _draft_media(item),
                        # KRI-790: quoted boundaries and length use the same kept window.
                        take_window=(
                            _draft_take_window(
                                brief, document.strategy, planned.media_snapshot, item
                            )
                            if phone_talking
                            else None
                        ),
                        # KRI-849: what the draft does about "ask me instead of guessing".
                        unclear_words=(
                            _draft_unclear_words(
                                brief, document.strategy, planned.media_snapshot, item
                            )
                            if phone_talking
                            else None
                        ),
                    )
                    # KRI-776: a moment ask is judged against the windows the approved length
                    # plan will cut, so the card never promises moments the plan doesn't hold.
                    facts = with_planned_cuts(facts, planned.media_snapshot)
                    from app.kria.approval_readiness import contract_drawn_texts  # noqa: PLC0415
                    from app.services.creator_render_contract import (  # noqa: PLC0415
                        build_render_contract,
                    )

                    contract_media = planned.media_snapshot or _draft_media(item)
                    facts = replace(
                        facts,
                        contract_drawn_texts=contract_drawn_texts(
                            (
                                build_render_contract(
                                    document.strategy,
                                    generation_id="draft-receipt",
                                    brief=brief,
                                    media_snapshot=contract_media,
                                )
                                if settings.brief_binding_for(thread.creator_id)
                                else None
                            ),
                            document.strategy,
                            media=contract_media,
                            manifest=planned.brief_manifest,
                            creator_id=thread.creator_id,
                        ),
                    )
                    # The pinned length plan the card reads (its length, a story's holds).
                    _card_media = (
                        planned.media_snapshot
                        if planned.media_snapshot is not None
                        else _draft_media(item)
                    )
                    if facts.phone_montage and settings.kria_route_disclosure_enabled:
                        # The length on the draft is the length the plan renders.
                        # Research prepared for the planner's strategy is pinned to its digest
                        # (`research_preview.basis_digest`), so that strategy stays as planned.
                        # A length the creator stated (message, brief, answer) is theirs.
                        _card_source = db.get(CreationThreadEvent, turn.source_event_id)
                        _stated = creator_duration(
                            creator_texts=[_card_source.content or ""] if _card_source else [],
                            brief=brief,
                            authoritative_answers=(document.strategy or {}).get("choice_answers")
                            or (),
                        )
                        _card_length = (
                            None
                            if planned.prepared_research is not None or _stated is not None
                            else planned_card_length(
                                document.strategy or {}, _card_media, route=UNIFIED_MONTAGE
                            )
                        )
                        if _card_length is not None:
                            document = document.model_copy(
                                update={
                                    "strategy": {
                                        **(document.strategy or {}),
                                        "target_duration_s": _card_length,
                                    }
                                }
                            )
                            snapshot, snapshot_hash = canonical_snapshot(document)
                    # The route this draft renders through, so the card reads that route's
                    # table (`route_capabilities`); registry off: the unified montage only.
                    route_name = draft_route(
                        creator_id=thread.creator_id,
                        edit_format=(document.strategy or {}).get("edit_format")
                        or item.edit_format,
                        strategy=document.strategy,
                        clip_paths=item.clip_gcs_paths or (),
                        phone_montage=facts.phone_montage,
                    )
                    checked = brief.live()
                    # KRI-190: the unified montage planner writes the per-clip text,
                    # order and title at render time and reports on them then. Judging
                    # them against this text-free draft would only mislead.
                    checked = (
                        checked
                        if readiness_required
                        else requirements_to_check_at_draft(
                            checked,
                            creator_id=thread.creator_id,
                            strategy=document.strategy,
                            item_edit_format=item.edit_format,
                            clip_paths=item.clip_gcs_paths or (),
                            client_protocol=client_protocol,
                        )
                    )
                    from app.kria import research_preview  # noqa: PLC0415
                    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
                    from app.services.phone_montage_preflight import (  # noqa: PLC0415
                        _ready_visuals,
                    )

                    # KRI-881: the research done before approval, for the brief this turn
                    # persisted. KRI-945: readiness drafts are judged on it too.
                    draft_strategy = document.strategy or {}
                    draft_research = research_preview.for_draft(
                        planned.prepared_research,
                        brief=brief,
                        strategy=document.strategy,
                        media_snapshot=planned.media_snapshot
                        if planned.media_snapshot is not None
                        else snapshot_media(item),
                        terms=planned.prepared_research_terms,
                        # The basis inputs research read beyond the draft: this item's
                        # ready Visuals now and the slot windows they plan with.
                        creator_id=thread.creator_id,
                        edit_format=draft_strategy.get("edit_format") or item.edit_format,
                        visuals=lambda: _ready_visuals(
                            db,
                            item=item,
                            user_id=thread.creator_id,
                            strategy=document.strategy,
                        ),
                        client_protocol=client_protocol,
                    )
                    if not readiness_required:
                        # KRI-775: the phone montage worker refuses a draft whose order or
                        # length falls short. Plan it now (deterministic, fail-open) so its
                        # receipts hold the draft back here instead of after approval.
                        from app.services.phone_montage_preflight import (  # noqa: PLC0415
                            phone_montage_blockers,
                            with_blocked_requirements,
                        )

                        phone_blockers = phone_montage_blockers(
                            db,
                            creator_id=thread.creator_id,
                            item=item,
                            brief=brief,
                            strategy=document.strategy,
                            media_snapshot=planned.media_snapshot
                            if planned.media_snapshot is not None
                            else snapshot_media(item),
                            client_protocol=client_protocol,
                        )
                        # KRI-881: research that failed before approval is a precise limit
                        # the creator rules on here, never a ready card.
                        if draft_research is not None and not draft_research.ready:
                            phone_blockers = [
                                *phone_blockers,
                                research_preview.blocker_receipt(
                                    draft_research.prepared, draft_research.requirement
                                ),
                            ]
                        checked = with_blocked_requirements(checked, brief, phone_blockers)
                    # KRI-689: where the phone montage plan cannot change clip speed (no
                    # verified renderer, a song montage), say so before approval; the
                    # render-ready review reports it as "Couldn't". Where it can, the plan's
                    # own receipts (the preflight's, then the render's) say which clips play
                    # at which rate, so no blanket sentence is added.
                    speed_notice = clip_speed_notice(
                        brief.live(),
                        creator_id=thread.creator_id,
                        strategy=document.strategy,
                        item_edit_format=item.edit_format,
                        clip_paths=item.clip_gcs_paths or (),
                        judged_ids={receipt.requirement_id for receipt in phone_blockers},
                    )
                else:
                    # Editor operations verify only the requirements stated in
                    # this very turn, against what the ops changed (KRI-558).
                    facts = plan_facts_from_editor_payload(
                        document.editor_payload,
                        document.editor_text_diff,
                        changes,
                        canonical_duration_s=compiled.canonical_duration_s,
                    )
                    facts = replace(facts, anaphora_rows=compiled.diff.changed_text_ids())
                    checked = [req for req in brief.live() if req.source_turn_id == str(turn.id)]
                editor_turn = apply_intent.tool_name == "draft.apply_editor_ops"
                # An ask the route cannot do is declined on this card even where the draft
                # check leaves it to the render (registry on: every account).
                answered = (
                    requirements_with_route_declines(
                        checked, brief.live(), document.strategy, route=route_name
                    )
                    if route_name is not None and not readiness_required and not editor_turn
                    else checked
                )
                if answered:
                    bound_writer = settings.brief_binding_for(thread.creator_id)
                    receipts = (
                        build_receipts(
                            checked,
                            facts,
                            include_unchecked=editor_turn or bound_writer or facts.phone_talking,
                            # Listing unchecked asks must not switch on the strict order
                            # verdicts of a creator whose writer is not brief-bound.
                            strict_order=bound_writer,
                        )
                        if checked
                        else []
                    )
                    if phone_blockers:
                        from app.services.phone_montage_preflight import (  # noqa: PLC0415
                            with_blocker_receipts,
                        )

                        # KRI-775: a requirement the phone plan falls short on is judged by it.
                        receipts = with_blocker_receipts(receipts, phone_blockers)
                    policy_reasons = _policy_text_reasons(brief, planned.policy_omitted_texts)
                    receipts = [
                        receipt.model_copy(
                            update={"reason": policy_reasons[receipt.requirement_id]}
                        )
                        if receipt.requirement_id in policy_reasons
                        else receipt
                        for receipt in receipts
                    ]
                    if (
                        route_name is not None
                        and (readiness_required or decoupled_from_readiness())
                        and not editor_turn
                    ):
                        # An ask the render route does not read is answered plainly now.
                        receipts = with_route_declines(
                            receipts, answered, document.strategy, route=route_name
                        )
                    bound = False
                    if editor_turn and not compiled.diff.unavailable:
                        try:
                            bound_receipts, unbound = bind_editor_receipts(
                                checked,
                                receipts,
                                compiled.diff,
                                facts.text_styles or (),
                                unmet=[item.model_dump() for item in arguments.unmet_requests],
                            )
                            also_changed = describe_diff(
                                unbound, live_text_count=compiled.diff.live_text_count, limit=2
                            )
                            reply_text = reply_from_editor_receipts(
                                checked,
                                bound_receipts,
                                unbound=also_changed,
                                notices=planned.policy_notices,
                                notes=arguments.notes,
                            )
                            reply_lead = receipt_lead(
                                bound_receipts,
                                notices=planned.policy_notices,
                                notes=arguments.notes,
                            )
                            receipts, bound = bound_receipts, True
                        except Exception:  # noqa: BLE001 - receipts must never fail a good edit
                            log.exception("kria_editor_receipts_failed", turn_id=str(turn.id))
                    if not bound:
                        # A first draft has no finished video yet: a request only the render
                        # can show is said nothing about now (the render-ready review judges
                        # it), never "couldn't verify" (KRI-558). (An editor turn whose receipts
                        # could not be bound keeps its open items, listed calmly.)
                        # KRI-793: a phone Talking draft lists every ask (KRI-705); one no
                        # checker can judge yet goes under "have a look", never silence.
                        if not editor_turn and not readiness_required and not facts.phone_talking:
                            receipts = [r for r in receipts if r.verification != "unchecked"]
                        reply_text = reply_from_receipts(
                            CreativeBrief(version=brief.version, requirements=answered),
                            receipts,
                            summary=arguments.summary,
                            notices=planned.policy_notices,
                            # Outside readiness a route decline follows the planner's
                            # summary instead of replacing it (registry on).
                            declines_after_summary=route_name is not None
                            and not readiness_required
                            and not editor_turn
                            and decoupled_from_readiness(),
                        )
                    requirement_receipts = [r.model_dump(mode="json") for r in receipts]
                elif editor_turn:
                    phrases = describe_diff(
                        compiled.diff.entries, live_text_count=compiled.diff.live_text_count
                    )
                    if phrases:
                        reply_text = reply_from_diff(
                            phrases, notices=planned.policy_notices, notes=arguments.notes
                        )
        if speed_notice:
            reply_text = f"{reply_text}\n{speed_notice}"
        if draft_research is not None and draft_research.ready:
            from app.kria import research_preview  # noqa: PLC0415

            reply_text = f"{reply_text}\n{research_preview.preview_lines(draft_research.prepared)}"
        if (
            route_name is not None
            and brief is not None
            and (readiness_required or decoupled_from_readiness())
        ):
            route_disclosures = card_disclosures(
                brief.live(),
                document.strategy,
                route=route_name,
                research_previewed=draft_research is not None and draft_research.ready,
                # The pinned length plan: whether a story direction's holds fit the length.
                media=_card_media,
            )
        route_notice = card_text(route_disclosures) if route_disclosures else None
        if route_notice:
            reply_text = f"{reply_text}\n{route_notice}"
        if brief is not None:
            # A repeated offer can have no new updates to persist in the response
            # transaction. Its receipts still name the revision it checked.
            requirement_receipts = [
                {**receipt, "brief_version": brief.version} for receipt in requirement_receipts
            ]
        if readiness_required:
            from app.kria.approval_readiness import (  # noqa: PLC0415
                APPROVABLE,
                assess_readiness,
                readiness_reply,
            )
            from app.kria.brief_binding import snapshot_media as _readiness_media  # noqa: PLC0415
            from app.services.phone_montage_preflight import (  # noqa: PLC0415
                readiness_plan_blockers,
            )

            if brief is None:
                brief = load_latest_brief_sync(db, thread.id)
            # KRI-774: the same text the approval binding pins, so a later recheck agrees.
            _readiness_source = db.get(CreationThreadEvent, turn.source_event_id)
            _readiness_request = (
                _full_creator_request_sync(
                    db, thread_id=thread.id, through_sequence=int(_readiness_source.sequence)
                )
                if _readiness_source is not None
                else None
            )
            # KRI-901: the marks this approval would pin, so a marked object is executable.
            _readiness_marks = (
                _protected_regions_sync(
                    db, thread_id=thread.id, through_sequence=int(_readiness_source.sequence)
                )
                if _readiness_source is not None
                else []
            )
            # KRI-945: what the approval would pin (`BriefBinding.prepared_research`), or the
            # research limit the creator is told about.
            _readiness_research = (
                draft_research.prepared.model_dump(mode="json")
                if draft_research is not None
                else None
            )
            # Leverkusen M2 run 3: a speed ask the phone montage plan can't place is said
            # before the card here too (readiness skipped the KRI-775 plan above).
            _readiness_blockers = (
                readiness_plan_blockers(
                    db,
                    creator_id=thread.creator_id,
                    item=item,
                    brief=brief,
                    strategy=document.strategy,
                    media_snapshot=planned.media_snapshot
                    if planned.media_snapshot is not None
                    else _readiness_media(item),
                    client_protocol=client_protocol,
                )
                if document.editor_payload is None
                else []
            )
            readiness_report = assess_readiness(
                brief,
                document.strategy or {},
                media=planned.media_snapshot
                if planned.media_snapshot is not None
                else _readiness_media(item),
                manifest=planned.readiness_manifest,
                editor_payload=document.editor_payload,
                editor_text_diff=document.editor_text_diff,
                creator_request=_readiness_request,
                protected_regions=_readiness_marks,
                protected_marks=_marks_route,
                prepared_collection_story=document.prepared_collection_story,
                creator_id=thread.creator_id,
                prepared_research=_readiness_research,
                order_execution=compiled_order_preflight_sync(
                    db,
                    item=item,
                    creator_id=thread.creator_id,
                    brief=brief,
                    strategy=document.strategy or {},
                    media_snapshot=planned.media_snapshot
                    if planned.media_snapshot is not None
                    else _readiness_media(item),
                    client_protocol=client_protocol,
                ),
                order_source_context=_order_source_context_sync(
                    db, brief=brief, item_id=item.id, creator_id=thread.creator_id
                ),
                plan_blockers=_readiness_blockers,
            )
            policy_reasons = _policy_text_reasons(brief, planned.policy_omitted_texts)
            readiness_report["requirements"] = [
                {**row, "reason": policy_reasons[row["requirement_id"]]}
                if row["requirement_id"] in policy_reasons and row["disposition"] not in APPROVABLE
                else row
                for row in readiness_report["requirements"]
            ]
            if not readiness_report["ready"]:
                reply_text = readiness_reply(brief, readiness_report)
            elif document.prepared_collection_story is not None:
                # The frozen execution checker owns this selected requirement;
                # persist its checked verdict in the receipt as well as approval.
                story_proofs = {
                    row["requirement_id"]: row
                    for row in readiness_report["requirements"]
                    if row["evidence_owner"] == "prepared_collection_story"
                    and row["disposition"] == "executable"
                }
                requirement_receipts = [
                    {
                        **receipt,
                        "status": "met",
                        "verification": "checked",
                        "reason": story_proofs[receipt["requirement_id"]]["reason"],
                    }
                    if receipt["requirement_id"] in story_proofs
                    else receipt
                    for receipt in requirement_receipts
                ]
                if story_proofs and len(story_proofs) == len(readiness_report["requirements"]):
                    reply_text = arguments.summary
        unmet_ids = [
            str(receipt.get("requirement_id"))
            for receipt in requirement_receipts
            if needs_creator_choice(receipt)
        ]
        if readiness_report is not None:
            unmet_ids = [
                row["requirement_id"]
                for row in readiness_report["requirements"]
                if row["disposition"] not in APPROVABLE
            ]
        automatic_omission_ids = _automatic_omission_ids(
            brief,
            requirement_receipts,
            readiness_report,
            strategy=document.strategy,
        )
        # A removed title can have an unchecked/missing receipt precisely because
        # the compiler removed it. Its exact server proof supersedes that absence,
        # while every unrelated unknown requirement still blocks approval.
        unmet_ids = list(
            dict.fromkeys(
                [
                    *unmet_ids,
                    *(
                        req.id
                        for req in (brief.live() if brief is not None else ())
                        if _policy_text_omission_reason(req, planned.policy_omitted_texts)
                    ),
                ]
            )
        )
        automatic_omission_ids = tuple(
            req_id for req_id in automatic_omission_ids if req_id in unmet_ids
        )
        # KRI-983: under approval readiness a version without the unmet asks must still make
        # something they asked for. Elsewhere KRI-610 keeps "simpler version" able to drop a
        # sole unmet ask the creator gave up on.
        simpler_keeps_an_ask = (
            not readiness_required
            or brief is None
            or any(req.id not in unmet_ids for req in brief.live())
        )
        automatic_omission = (
            bool(automatic_omission_ids)
            and set(automatic_omission_ids) == set(unmet_ids)
            and apply_intent.tool_name == "draft.apply_strategy"
            and settings.brief_binding_for(thread.creator_id)
        )
        retained_automatic_ids = {
            req.id
            for req in (brief.live() if brief is not None else ())
            if automatic_omission
            and req.id in automatic_omission_ids
            and req.literal
            and wants_light_change(req)
            and _policy_text_omission_reason(req, planned.policy_omitted_texts) is None
        }
        simpler_consent = (
            _simpler_version_consent(
                db,
                thread.id,
                unmet_ids,
                brief=brief,
                source_event=db.get(CreationThreadEvent, turn.source_event_id),
            )
            if (
                bool(unmet_ids)
                and simpler_keeps_an_ask
                and apply_intent.tool_name == "draft.apply_strategy"
                and settings.brief_binding_for(thread.creator_id)
                # A concurrent/new instruction needs its own offer, even if its stable
                # requirement IDs happen to be the same. Never waive model-authored updates,
                # except a restatement of exactly what is being left out (KRI-1113).
                and all(
                    update.operation == "change" and update.target_requirement_id in unmet_ids
                    for update in planned.brief_updates
                )
                and not (readiness_report or {}).get("policy_problem")
            )
            else None
        )
        simpler_version_chosen = simpler_consent is not None
        # KRI-968: only an offer that named these requirements lets a reply say "as agreed".
        agreed_omission_ids = list(unmet_ids) if simpler_consent == "named" else []
        if simpler_version_chosen or automatic_omission:
            omitted = [req for req in brief.live() if req.id in unmet_ids]
            omission_reasons = {
                str(row["requirement_id"]): str(row.get("reason") or "")
                for row in (
                    readiness_report["requirements"] if readiness_report else requirement_receipts
                )
            }
            if automatic_omission:
                omission_reasons.update(
                    {
                        req.id: _policy_text_omission_reason(req, planned.policy_omitted_texts)
                        or _automatic_omission_reason(req, document.strategy or {})
                        for req in omitted
                    }
                )
            effective_brief_updates = tuple(
                BriefUpdate(
                    operation="change",
                    target_requirement_id=req.id,
                    expected_version=brief.version,
                    kind=req.kind,
                    scope=req.scope,
                    literal=req.literal,
                    description=req.description,
                    facts={
                        key: value for key, value in req.facts.items() if key != "footage_light"
                    },
                )
                if req.id in retained_automatic_ids
                else BriefUpdate(
                    operation="remove",
                    target_requirement_id=req.id,
                    expected_version=brief.version,
                )
                for req in omitted
            )
            if automatic_omission or planned.brief_updates:
                # The turn's creator additions (KRI-1113: or the model's restatement of what
                # the creator just left out) were already persisted above.  Append the
                # server-owned omission as its own immutable brief revision.
                brief = _append_automatic_omissions_sync(
                    db,
                    thread_id=thread.id,
                    brief=brief,
                    updates=effective_brief_updates,
                )
            else:
                brief = persist_brief_version_sync(
                    db,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    updates=effective_brief_updates,
                    server_removed_ids=removed_target_ids(effective_brief_updates),
                )
            if settings.kria_choice_questions_enabled:
                brief = answered_brief(brief, document.strategy)
            # These are now disclosed omissions, not failed obligations in the new
            # revision. Keep the history; pin only the effective requirements.
            requirement_receipts = [
                r for r in requirement_receipts if r["requirement_id"] not in unmet_ids
            ]
            live_by_id = {req.id: req for req in brief.live()}
            checked = [
                live_by_id.get(req.id, req)
                for req in checked
                if req.id not in unmet_ids or req.id in retained_automatic_ids
            ]
            if retained_automatic_ids:
                # Recheck the retained title under its effective requirement,
                # rather than retaining its old brightness failure or dropping it.
                requirement_receipts.extend(
                    receipt.model_dump(mode="json")
                    for receipt in build_receipts(
                        [req for req in checked if req.id in retained_automatic_ids],
                        facts,
                        include_unchecked=True,
                        strict_order=True,
                    )
                )
            if readiness_required:
                readiness_report = assess_readiness(
                    brief,
                    document.strategy or {},
                    media=planned.media_snapshot
                    if planned.media_snapshot is not None
                    else _readiness_media(item),
                    manifest=planned.readiness_manifest,
                    creator_request=_readiness_request,
                    protected_regions=_readiness_marks,
                    protected_marks=_marks_route,
                    prepared_collection_story=document.prepared_collection_story,
                    creator_id=thread.creator_id,
                    prepared_research=_readiness_research,
                    order_execution=compiled_order_preflight_sync(
                        db,
                        item=item,
                        creator_id=thread.creator_id,
                        brief=brief,
                        strategy=document.strategy or {},
                        media_snapshot=planned.media_snapshot
                        if planned.media_snapshot is not None
                        else _readiness_media(item),
                        client_protocol=client_protocol,
                    ),
                    order_source_context=_order_source_context_sync(
                        db, brief=brief, item_id=item.id, creator_id=thread.creator_id
                    ),
                    plan_blockers=_readiness_blockers,
                )
            requirement_receipts = [
                {**receipt, "brief_version": brief.version} for receipt in requirement_receipts
            ]
            log.info(
                "kria_simpler_version_drafted",
                thread_id=str(thread.id),
                turn_id=str(turn.id),
                requirement_ids=unmet_ids,
            )
            lead = say(en="Here's the simpler version.", tr="İşte daha basit sürüm.")
            omission_labels = {
                req.id: say(en="the footage brightness change", tr="çekimin parlaklık değişikliği")
                if req.id in retained_automatic_ids
                else req.text()
                for req in omitted
            }
            omissions = "\n".join(
                say(
                    en=(
                        f"- Left out: {omission_labels[req.id]} "
                        f"({omission_reasons.get(req.id, '')})"
                    ),
                    tr=(
                        f"- Çıkarıldı: {omission_labels[req.id]} "
                        f"({omission_reasons.get(req.id, '')})"
                    ),
                )
                if automatic_omission
                else _left_out_line(req, agreed=req.id in agreed_omission_ids)
                for req in omitted
            )
            kept = (
                reply_from_receipts(
                    brief,
                    build_receipts(checked, facts, include_unchecked=True, strict_order=True),
                    notices=planned.policy_notices,
                )
                if checked
                else ""
            )
            reply_text = "\n".join(
                part
                for part in (lead, _with_left_out(kept, omissions), speed_notice, route_notice)
                if part
            )
        mark_gate = _protected_region_gate_sync(
            db,
            thread=thread,
            turn=turn,
            item=item,
            brief=brief,
            marks_route=_marks_route,
            render_requested=render_intent is not None,
        )
        if mark_gate is not None:
            # KRI-901: "No text on the plate" with no mark on the plate: the render could only
            # report `no_selection` afterwards, so ask for the mark before approval. Same
            # rollback-then-respond pattern as the choice gate below (the old draft stays the
            # head). Re-fires on each act turn until a mark exists or the ask is dropped.
            mark_plan, mark_question = mark_gate
            db.rollback()
            completed = _complete_response_turn(
                turn_id,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                claimed_thread_revision=claimed_thread_revision,
                plan=mark_plan,
                brief_updates=planned.brief_updates
                if automatic_omission
                else effective_brief_updates,
                brief_delegated=planned.brief_delegated,
                brief_delegation_read_ids=planned.brief_delegation_read_ids,
                requirement_receipts=requirement_receipts,
                brief_coverage={
                    **(planned.brief_coverage or {}),
                    "stage": "draft",
                    "reason": "protected_region_mark_needed",
                    "protected_region_requirement_ids": mark_question["requirement_ids"],
                },
                brief_expected_version=planned.brief_expected_version,
            )
            return replace(completed, response_only=True)
        if (
            settings.brief_binding_for(thread.creator_id)
            and (
                (unmet_ids and not (simpler_version_chosen or automatic_omission))
                or (readiness_report is not None and not readiness_report["ready"])
            )
            # KRI-558: an editor draft that really changed something is kept, and its
            # Partly / Couldn't lines say what is missing. Only an edit that changed
            # nothing falls back to the "different approach?" question.
            and (
                readiness_required
                or apply_intent.tool_name == "draft.apply_strategy"
                or compiled.diff.empty()
            )
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
                # KRI-613: the receipt lines above describe "this draft", so the state
                # line says it was held back rather than denying it exists.
                else say(
                    en="I held this first draft back.", tr="Bu ilk taslağı şimdilik beklettim."
                )
            )
            policy_problem = (readiness_report or {}).get("policy_problem")
            can_offer_simpler = simpler_keeps_an_ask and (
                not readiness_required or bool(unmet_ids) and not policy_problem
            )
            # KRI-968: name what the simpler version leaves out before the creator picks it,
            # and record those ids on the question so a tap consents to exactly them.
            leaves_out, simpler_question = (
                _simpler_version_offer(brief, unmet_ids) if can_offer_simpler else ("", None)
            )
            ask = (
                simpler_version_ask()
                if can_offer_simpler
                else say(
                    en="Please clarify the requested changes so I can prepare the edit again.",
                    tr=(
                        "Düzenlemeyi yeniden hazırlayabilmem için "
                        "istediğin değişiklikleri netleştir."
                    ),
                )
                if policy_problem
                else say(
                    en="Tell me what to change and I'll prepare it again.",
                    tr="Neyi değiştirmemi istediğini söyle, yeniden hazırlayayım.",
                )
            )
            recovery = KriaTurnPlan(
                mode="respond",
                turn_value="question",
                choice_question=simpler_question,
                response=_fit_reply(
                    reply_text, " ".join(part for part in (draft_state, leaves_out, ask) if part)
                ),
            )
            completed = _complete_response_turn(
                turn_id,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
                claimed_thread_revision=claimed_thread_revision,
                plan=recovery,
                brief_updates=planned.brief_updates,
                brief_delegated=planned.brief_delegated,
                brief_delegation_read_ids=planned.brief_delegation_read_ids,
                requirement_receipts=requirement_receipts,
                brief_coverage={
                    **(planned.brief_coverage or {}),
                    "stage": "draft",
                    "reason": "approval_requirements_not_ready"
                    if readiness_required
                    else SIMPLER_VERSION_REASON,
                    **({"approval_readiness": readiness_report} if readiness_required else {}),
                    **(
                        {"automatic_omission_ids": list(automatic_omission_ids)}
                        if automatic_omission
                        else {}
                    ),
                },
                brief_expected_version=planned.brief_expected_version,
            )
            return replace(completed, response_only=True)
        approval_summary = arguments.summary
        if (
            apply_intent.tool_name == "draft.apply_strategy"
            and brief is not None
            and (
                simpler_version_chosen
                or automatic_omission
                or (requirement_receipts and not keeps_model_summary(brief, requirement_receipts))
            )
        ):
            # KRI-700: the reply above already replaced the model's summary with what was
            # checked ("Couldn't: brighten the dark river clips"). The draft's change list and
            # the approval card follow it, so neither promises what the plan doesn't contain.
            changes = [change for change in changes if change != arguments.summary]
            approval_summary = reply_text
            document = document.model_copy(
                update={
                    "changes": changes,
                    **(
                        {
                            "intent": say(
                                en=(
                                    "Simpler video with the supported edits."
                                    if automatic_omission
                                    else "Simpler version with the agreed omissions."
                                ),
                                tr=(
                                    "Desteklenen düzenlemelerle daha basit video."
                                    if automatic_omission
                                    else "Kararlaştırılan eksiklerle daha basit sürüm."
                                ),
                            )
                        }
                        if simpler_version_chosen or automatic_omission
                        else {}
                    ),
                }
            )
            snapshot, snapshot_hash = canonical_snapshot(document)
        choice_gate = (
            (
                settings.kria_choice_questions_enabled
                or settings.brief_binding_for(thread.creator_id)
            )
            and apply_intent.tool_name == "draft.apply_strategy"
            and bool(document.strategy)
        )
        if choice_gate:
            from app.kria.brief_binding import snapshot_media as _snapshot_media  # noqa: PLC0415

            binding_on = settings.brief_binding_for(thread.creator_id)
            gate_brief = brief
            if gate_brief is None and binding_on:
                gate_brief = load_latest_brief_sync(db, thread.id)
                if settings.kria_choice_questions_enabled:
                    gate_brief = answered_brief(gate_brief, document.strategy)
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
                client_protocol=client_protocol,
                allow_choices=settings.kria_choice_questions_enabled,
            )
            if blocked is not None:
                blocked_plan, blocked_reason = blocked
                # KRI-1068: record the clip a reply is stuck on, so "skip that" next turn
                # drops it; never send the same stuck reply twice in a row.
                stuck_on = pin_block_coverage(gate_brief, blocked_plan.response)
                blocked_plan = _unstuck_recovery(
                    db, thread.id, blocked_plan, gate_brief, planned.pin_notices
                )
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
                    # Auto omissions were speculative and rolled back too. Preserve
                    # the original request; recompute omissions after the real choice.
                    brief_updates=planned.brief_updates
                    if automatic_omission
                    else effective_brief_updates,
                    brief_delegated=planned.brief_delegated,
                    brief_delegation_read_ids=planned.brief_delegation_read_ids,
                    requirement_receipts=requirement_receipts,
                    brief_coverage={
                        **(planned.brief_coverage or {}),
                        "stage": "draft",
                        "reason": blocked_reason,
                        **stuck_on,
                        **(
                            {
                                "accepted_omission_ids": unmet_ids,
                                "agreed_omission_ids": agreed_omission_ids,
                            }
                            if simpler_version_chosen
                            else {}
                        ),
                    },
                    brief_expected_version=planned.brief_expected_version,
                )
                return replace(completed, response_only=True)
        if settings.brief_binding_for(thread.creator_id):
            from app.kria.brief_binding import BriefBinding, snapshot_media  # noqa: PLC0415

            if brief is None:
                brief = load_latest_brief_sync(db, thread.id)
                if settings.kria_choice_questions_enabled:
                    brief = answered_brief(brief, document.strategy)
            source_event = db.get(CreationThreadEvent, turn.source_event_id)
            full_request = (
                _full_creator_request_sync(
                    db,
                    thread_id=thread.id,
                    through_sequence=int(source_event.sequence),
                )
                if source_event is not None
                else None
            )
            if source_event is not None and full_request is None:
                raise ApprovedCreatorRequestTooLongError
            coverage = dict(planned.brief_coverage or {})
            if readiness_required:
                if readiness_report is None or not readiness_report["ready"]:
                    raise RuntimeError("Render approval requires executable requirement evidence")
                coverage["approval_readiness"] = readiness_report
            if simpler_version_chosen:
                coverage["accepted_omission_ids"] = unmet_ids
                coverage["agreed_omission_ids"] = agreed_omission_ids
                coverage["applicable_ids"] = [req.id for req in brief.live()]
                coverage["retrieved_ids"] = [
                    req_id
                    for req_id in coverage.get("retrieved_ids", [])
                    if req_id not in unmet_ids
                ]
            if automatic_omission:
                fully_omitted_ids = set(automatic_omission_ids) - retained_automatic_ids
                coverage["automatic_omission_ids"] = [
                    req_id for req_id in automatic_omission_ids if req_id in fully_omitted_ids
                ]
                if retained_automatic_ids:
                    coverage["automatic_omission_facets"] = {
                        req_id: ["footage_light"] for req_id in sorted(retained_automatic_ids)
                    }
                coverage["applicable_ids"] = [req.id for req in brief.live()]
                coverage["retrieved_ids"] = [
                    req_id
                    for req_id in coverage.get("retrieved_ids", [])
                    if req_id not in fully_omitted_ids
                ]
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
                        full_creator_request=full_request,
                        media_snapshot=planned.media_snapshot
                        if planned.media_snapshot is not None
                        else snapshot_media(item),
                        choice_answers=(
                            list(document.strategy.get("choice_answers") or [])
                            if choice_gate
                            else None
                        ),
                        protected_regions=(
                            _protected_regions_sync(
                                db,
                                thread_id=thread.id,
                                through_sequence=int(source_event.sequence),
                            )
                            if source_event is not None
                            else None
                        ),
                        # KRI-881: the sourced wording the reply showed, replayed at render.
                        prepared_research=(
                            draft_research.prepared.model_dump(mode="json")
                            if draft_research is not None and draft_research.ready
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
            result={
                "snapshot_hash": snapshot_hash,
                "changes": changes,
                **({"editor_diff": editor_diff_trace} if editor_diff_trace else {}),
                **state_trace,
                **({"plan_review_before": restore_record} if restore_record else {}),
            },
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
                    **_receipt_lead_fields(reply_lead, also_changed, requirement_receipts),
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
                **(
                    {"approval_readiness_digest": readiness_report["digest"]}
                    if readiness_required
                    else {}
                ),
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
            # KRI-689: the speed heads-up rides on the card too, unless the KRI-700 reply
            # (which already ends with it) became the summary.
            consequence_summary=" ".join(
                part
                for part in (
                    f"Render this draft: {approval_summary}",
                    speed_notice if speed_notice and speed_notice not in approval_summary else None,
                    route_notice if route_notice and route_notice not in approval_summary else None,
                )
                if part
            ),
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
                **(
                    {"not_applied": [item.as_record() for item in route_disclosures]}
                    if route_disclosures
                    else {}
                ),
                **_receipt_lead_fields(reply_lead, also_changed, requirement_receipts),
                **(
                    {"research_preview": _research_preview_payload(draft_research)}
                    if draft_research is not None and draft_research.ready
                    else {}
                ),
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


def _research_preview_payload(draft_research: Any) -> dict[str, Any]:
    from app.kria import research_preview  # noqa: PLC0415

    return research_preview.preview_payload(draft_research.prepared)


@contextmanager
def _lease_heartbeat_thread(
    turn_id: uuid.UUID, *, lease_owner: str, lease_epoch: int
) -> Iterator[None]:
    """Renew the turn lease from a thread while blocking work runs outside planning.

    Same contract as `_plan_with_live_agent`'s heartbeat: a failed renewal is retried on
    the next beat, and a lost lease ends the beat (the commit then writes nothing).
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(_LEASE_HEARTBEAT_SECONDS):
            try:
                alive = _renew_turn_lease(turn_id, lease_owner=lease_owner, lease_epoch=lease_epoch)
            except Exception:  # noqa: BLE001 - the lease outlasts a missed renewal
                log.warning("kria_turn_lease_renewal_failed", turn_id=str(turn_id), exc_info=True)
                continue
            if not alive:
                return

    beat = threading.Thread(target=_beat, name=f"kria-lease-{turn_id}", daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=_LEASE_HEARTBEAT_SECONDS + 5)


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
    thought_publisher: ThoughtSummaryPublisher | None = None,
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
    # KRI-1037: "Try again" after a provider deadline starts the Main Creator on its lighter
    # rung (the claim stamped the snapshot from the thread's last finished turn).
    degraded_token = start_degraded_retry.set(bool(snapshot.get("start_degraded")))
    sender = bind_sending_build(stamped_client_protocol(snapshot.get("client_protocol")))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            parsed_state = parse_editor_state(editor_state)
            live_scope = snapshot.get("live_scope")
            scoped: dict[str, Any] = {}
            if isinstance(live_scope, dict):
                from app.kria.plan_contract import ManualEdit  # noqa: PLC0415

                scoped = {
                    "scope": list(live_scope["scope"]),
                    "manual_edits": [
                        ManualEdit.model_validate(e) for e in live_scope["manual_edits"]
                    ],
                }
                log.info(
                    "plan_review_scope_applied",
                    thread_id=str(snapshot["thread_id"]),
                    turn_id=str(turn_id),
                    scope=scoped["scope"],
                    manual_edit_count=len(scoped["manual_edits"]),
                )
            if snapshot.get("camera_roll_story"):
                from app.services.media_collection_preparation import plan_selected_story

                return await plan_selected_story(
                    db,
                    reference=snapshot["camera_roll_story"],
                    thread_id=uuid.UUID(str(snapshot["thread_id"])),
                    item_id=uuid.UUID(str(snapshot["item_id"])),
                    creator_id=uuid.UUID(str(snapshot["creator_id"])),
                )
            with bind_thought_publisher(thought_publisher):
                planned = await plan_live_turn(
                    db,
                    thread_id=uuid.UUID(str(snapshot["thread_id"])),
                    item_id=uuid.UUID(str(snapshot["item_id"])),
                    creator_id=uuid.UUID(str(snapshot["creator_id"])),
                    user_message=user_message,
                    # Only passed when present so the no-state call is byte-identical.
                    **({"editor_state": parsed_state} if parsed_state is not None else {}),
                    # KRI-282: a clip-picker answer must re-plan, never take the copilot path.
                    **({"answers_clip_question": True} if answers_clip_question else {}),
                    # KRI-802: this app answers a title card and a clip picker on one turn.
                    **({"batch_questions": True} if snapshot.get("batch_questions") else {}),
                    # KRI-723: this app answers a moment-order card.
                    **({"moment_questions": True} if snapshot.get("moment_questions") else {}),
                    # KRI-898: the sending app build, for the choice gate's freeze question.
                    **(
                        {"client_protocol": snapshot["client_protocol"]}
                        if snapshot.get("client_protocol") is not None
                        else {}
                    ),
                    **scoped,
                )
                if (
                    settings.approval_readiness_for(snapshot["creator_id"])
                    and planned.plan.mode == "act"
                ):
                    strategy_intent = next(
                        (
                            intent
                            for intent in planned.plan.intents
                            if intent.tool_name == "draft.apply_strategy"
                        ),
                        None,
                    )
                    if any(intent.tool_name == "render.request" for intent in planned.plan.intents):
                        from app.kria.approval_readiness import (
                            resolve_readiness_manifest,  # noqa: PLC0415
                        )

                        item = await db.get(
                            PlanItem, uuid.UUID(str(snapshot["item_id"])), populate_existing=True
                        )
                        manifest = await resolve_readiness_manifest(
                            db,
                            item,
                            strategy_intent.arguments["strategy"] if strategy_intent else None,
                        )
                        if strategy_intent is not None:
                            from app.agents._schemas.creator_agent import (
                                CreativeStrategy,  # noqa: PLC0415
                            )
                            from app.kria.strategy_policy import (  # noqa: PLC0415
                                RefusedStrategy,
                                check_strategy_for_runtime_v2,
                            )

                            # Resolve defaults against the PROPOSED renderer before
                            # offering approval, then save that exact canonical strategy.
                            # The assessor and approval reader still reject later repairs.
                            checked = check_strategy_for_runtime_v2(
                                manifest,
                                CreativeStrategy.model_validate(
                                    strategy_intent.arguments["strategy"]
                                ),
                                ask_before_simplifying=True,
                                ask_about_stated_settings=True,
                            )
                            if not isinstance(checked, RefusedStrategy):
                                canonical = strategy_intent.model_copy(
                                    update={
                                        "arguments": {
                                            **strategy_intent.arguments,
                                            "strategy": checked.strategy.model_dump(
                                                mode="json", exclude_none=True
                                            ),
                                            "summary": " ".join(
                                                [
                                                    str(
                                                        strategy_intent.arguments.get("summary")
                                                        or ""
                                                    ),
                                                    *checked.notices,
                                                ]
                                            ).strip()[:1000],
                                        },
                                    }
                                )
                                planned = replace(
                                    planned,
                                    plan=planned.plan.model_copy(
                                        update={
                                            "intents": [
                                                canonical if i is strategy_intent else i
                                                for i in planned.plan.intents
                                            ],
                                        }
                                    ),
                                )
                        planned = replace(planned, readiness_manifest=manifest)
                return planned
    finally:
        turn_deadline.reset(deadline)
        start_degraded_retry.reset(degraded_token)
        unbind_sending_build(sender)
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


def _model_message(source: Any, *, language: str | None = None) -> str:
    """The creator's words as the planner and brief extractor should read them (KRI-575).

    Callers that run before the turn binds the chat's language pass it (KRI-694), so a tapped
    answer's neutral sentence is in the chat's language, like the history rows read later.
    """
    return model_text(
        getattr(source, "payload", None), getattr(source, "content", None), language=language
    )


def _answers_clip_question(source: Any) -> bool:
    payload = getattr(source, "payload", None)
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("clip_selection"), dict):
        return bool(settings.kria_clip_selection_questions_enabled)
    if isinstance(payload.get("protected_region"), dict):
        # KRI-901: a mark answers the mark card: re-plan (the gate re-reads the marks), never
        # an editor follow-up that reads "I marked the plate" as a text edit.
        return True
    if isinstance(payload.get("moment_order"), dict):
        # KRI-723: the creator pointed at their moments; the re-plan folds the answer.
        return True
    # A tapped choice_question option ANSWERS the open question: it must re-plan (the
    # planner folds the stored answer), never be read as a follow-up edit request.
    return isinstance(payload.get("choice_selection"), dict) and bool(
        settings.kria_choice_questions_enabled
    )


def _answers_batched_questions(source: Any) -> bool:
    """KRI-802: the app that sent this message answers a reply's choice card and clip picker
    on one turn (stamped at submit), and both card kinds are on."""
    payload = getattr(source, "payload", None)
    return (
        isinstance(payload, dict)
        and payload.get("answers_batched_questions") is True
        and bool(settings.kria_choice_questions_enabled)
        and bool(settings.kria_clip_selection_questions_enabled)
    )


def _scoped_turn_fields(source: Any) -> dict[str, Any] | None:
    """``{"scope": [...], "manual_edits": [...]}`` stored on a scoped turn's user_message."""
    payload = getattr(source, "payload", None)
    # Deliberately NOT gated on LIVE_PLAN_REVIEW_ENABLED: the flag was checked at submit. If it
    # flipped off while the turn was queued, honouring the stored scope is safe; ignoring it
    # would plan the creator's scoped ask as an unscoped re-plan.
    if not isinstance(payload, dict):
        return None
    scope = payload.get("scope")
    if not isinstance(scope, list) or not scope:
        return None
    edits = payload.get("manual_edits")
    return {
        "scope": [str(s) for s in scope],
        "manual_edits": [e for e in edits if isinstance(e, dict)]
        if isinstance(edits, list)
        else [],
    }


def _stored_editor_state(turn: Any) -> dict[str, Any] | None:
    state = getattr(turn, "editor_state", None)
    return state if isinstance(state, dict) else None


# How long a provider-deadline failure keeps the creator's next turn on the lighter rung. A
# later turn that completes, or fails differently, ends it sooner.
_DEGRADED_START_WINDOW = timedelta(minutes=10)


# A whole message that only asks to send the failed request again, after `_request_fingerprint`.
# Exact match on the whole message (no word guessing): a miss only means the retry opens at full
# strength, the behavior before KRI-1037; the turn is never refused or changed otherwise.
_BARE_RETRY_MESSAGES = frozenset(
    {
        "try again",
        "try again please",
        "please try again",
        "retry",
        "retry please",
        "please retry",
        "again",
        "once more",
        "one more time",
        "do it again",
        "send it again",
        "tekrar dene",
        "tekrar deneyin",
        "tekrar dene lutfen",
        "lutfen tekrar dene",
        "yeniden dene",
        "yeniden dene lutfen",
        "lutfen yeniden dene",
        "tekrar",
        "yeniden",
    }
)


def _request_fingerprint(text: str | None) -> str:
    """The message's words, case, accents and punctuation folded away."""
    return " ".join(loose_words(text or ""))


def _is_retry_of(failed_text: str | None, new_text: str | None) -> bool:
    """Whether ``new_text`` asks for the failed request again (KRI-1037).

    True for the same words, or a bare "try again". A different request is not a retry: it
    gets the full configuration, since the deadline says nothing about it.
    """
    new = _request_fingerprint(new_text)
    if not new:
        return False
    return new == _request_fingerprint(failed_text) or new in _BARE_RETRY_MESSAGES


def _previous_turn_hit_provider_deadline(
    db,  # noqa: ANN001
    turn: CreatorAgentTurn,
    new_message: str | None = None,
) -> bool:
    """Whether ``turn`` retries the newest finished turn before it, which hit a deadline."""
    try:
        previous = db.execute(
            select(CreatorAgentTurn)
            .where(
                CreatorAgentTurn.thread_id == turn.thread_id,
                CreatorAgentTurn.id != turn.id,
                CreatorAgentTurn.status.in_(("completed", "failed")),
            )
            .order_by(CreatorAgentTurn.completed_at.desc().nulls_last(), CreatorAgentTurn.id)
            .limit(1)
        ).scalar_one_or_none()
        if (
            previous is None
            or previous.status != "failed"
            or not isinstance(previous.error, dict)
            or previous.error.get("code") != "runtime_provider_outcome_unknown"
            # Transport and SDK outcome-unknown errors are not a slow-call signal.
            or previous.error.get("provider_deadline") is not True
        ):
            return False
        finished = previous.completed_at
        if finished is not None and datetime.now(UTC) - finished > _DEGRADED_START_WINDOW:
            return False
        failed_source = db.get(CreationThreadEvent, previous.source_event_id)
        return _is_retry_of(
            _model_message(failed_source) if failed_source is not None else None, new_message
        )
    except Exception:  # noqa: BLE001 - a lookup miss only costs the lighter start
        log.warning("kria_degraded_start_lookup_failed", exc_info=True)
        return False


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
        claimed_snapshot = {**_snapshot(thread), "client_request_id": turn.client_event_id}
        source_payload = getattr(source, "payload", None)
        if isinstance(source_payload, dict) and source_payload.get("camera_roll_story"):
            claimed_snapshot["camera_roll_story"] = source_payload["camera_roll_story"]
        claimed_snapshot["source_sequence"] = int(source.sequence)
        live_scope = _scoped_turn_fields(source)
        if live_scope is not None:
            # KRI-441: rides the snapshot dict so the claim tuple keeps its shape.
            claimed_snapshot["live_scope"] = live_scope
        if _answers_batched_questions(source):
            # KRI-802: the same way; only when true, so every other snapshot is unchanged.
            claimed_snapshot["batch_questions"] = True
        if isinstance(source_payload, dict) and source_payload.get("answers_moment_questions"):
            # KRI-723: the app answers a moment-order card (stamped at submit from its protocol).
            claimed_snapshot["moment_questions"] = True
        sent_from = stamped_client_protocol(
            source_payload.get("client_protocol") if isinstance(source_payload, dict) else None
        )
        if sent_from is not None:
            # KRI-898: the app build that sent the message; only when stamped.
            claimed_snapshot["client_protocol"] = sent_from
        if _previous_turn_hit_provider_deadline(db, turn, _model_message(source)):
            # KRI-1037: only when true, so every other snapshot is unchanged.
            claimed_snapshot["start_degraded"] = True
        return (
            claimed_snapshot,
            # KRI-694: the task binds the chat's language only after this claim.
            _model_message(source, language=thread_reply_language(thread)),
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
        if not conversation_revision_matches_sync(db, thread, claimed_thread_revision):
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


def _turn_running_sync(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    thread_id: uuid.UUID,
) -> bool:
    return (
        db.execute(
            select(CreatorAgentTurn.id)
            .where(
                CreatorAgentTurn.thread_id == thread_id,
                CreatorAgentTurn.status.in_(CREATOR_AGENT_RUNNING_TURN_STATUSES),
            )
            .limit(1)
        ).first()
        is not None
    )


def _promote_queued_successor_sync(thread_id: uuid.UUID) -> str | None:
    with sync_session() as db:
        successor = _lock_queued_successor(db, thread_id)
        if successor is not None:
            # Turn -> Thread serializes this slot promotion with turn submit.
            db.execute(
                select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
            )
            # A turn the creator sent after this finished may already run (a delayed
            # promotion, or the reconciler's): the follow-up stays queued behind it.
            if _turn_running_sync(db, thread_id):
                successor = None
            else:
                successor.status = "pending"
        db.commit()
        return str(successor.id) if successor is not None else None


_NOT_RETRYABLE_NOW_TURN_CODES = frozenset({"ai_quota_exhausted"})
_TURN_FAILURE_COPY: dict[str, tuple[str, str]] = {
    # KRI-1037: not "try again in a moment" (the same heavy call repeats): say what happened
    # and what makes the next send lighter.
    "runtime_provider_outcome_unknown": (
        "The AI service ran out of time on that one. Your request, footage and saved draft "
        "are safe. Send it again, or ask for one change at a time so each step is lighter.",
        "Yapay zekâ hizmeti bunu zamanında bitiremedi. İsteğin, çekimlerin ve kayıtlı "
        "taslağın güvende. Yeniden gönder ya da her adım hafiflesin diye her seferinde tek "
        "bir değişiklik iste.",
    ),
    "runtime_output_truncated": (
        "My answer was cut off before it was complete. Your request, footage and saved "
        "draft are safe. Ask for a smaller step, such as one change at a time, and I'll "
        "take it from there.",
        "Yanıtım tamamlanmadan kesildi. İsteğin, çekimlerin ve kayıtlı taslağın güvende. "
        "Daha küçük bir adım iste, örneğin her seferinde tek bir değişiklik; oradan "
        "devam ederim.",
    ),
    # A1: the provider's spend cap or the AI budget. Not "try again": it won't work yet.
    "ai_quota_exhausted": (
        "Kria's AI service has reached its usage limit for now, so I couldn't work on this "
        "request. Your request, project and saved draft are safe. Trying again right away "
        "won't help; send it again later.",
        "Kria'nın yapay zekâ hizmeti şimdilik kullanım sınırına ulaştı, bu yüzden bu istek "
        "üzerinde çalışamadım. İsteğin, projen ve kayıtlı taslağın güvende. Hemen tekrar "
        "denemek işe yaramaz; daha sonra yeniden gönder.",
    ),
    # A3: the worker running this request died (out of memory, a deploy) on every claim
    # and was already re-run in the background; say that instead of a bare "try again".
    "runtime_turn_claims_exhausted": (
        "This request stopped partway through several times in a row, so I stopped "
        "retrying it on my own. Your project and saved draft are safe. Send it again, or "
        "ask for one change at a time if it keeps happening.",
        "Bu istek arka arkaya birkaç kez yarıda kaldı, bu yüzden kendi kendime yeniden "
        "denemeyi bıraktım. Projen ve kayıtlı taslağın güvende. Yeniden gönder; tekrar "
        "olursa her seferinde tek bir değişiklik iste.",
    ),
}
_TURN_FAILURE_DEFAULT_COPY = (
    "I couldn't finish that step, but your project and saved draft are safe. "
    "Try the request again.",
    "Bu adımı tamamlayamadım ama projen ve kayıtlı taslağın güvende. İsteği tekrar dene.",
)


def _turn_failure_code(exc: BaseException) -> str:
    """The turn failure code for ``exc``, looking through wrappers to the provider error.

    The Main Creator and the copilot wrap a provider ``TerminalError`` (``RuntimeError``
    / HTTP 502 ``from exc``), so the spend cap is found on the cause chain.
    """
    from app.agents._runtime import (  # noqa: PLC0415
        AiBudgetExceededError,
        ProviderQuotaExceededError,
        TerminalOutputTruncatedError,
    )

    seen: list[BaseException] = []
    current: BaseException | None = exc
    code = "runtime_turn_failed"
    while current is not None and all(current is not link for link in seen):
        if isinstance(current, ProviderQuotaExceededError | AiBudgetExceededError):
            return "ai_quota_exhausted"
        # KRI-1037: found anywhere on the chain (the planner wraps a TerminalError), but a
        # spend cap further down still wins, so keep walking.
        if isinstance(current, ProviderOutcomeUnknownError):
            code = "runtime_provider_outcome_unknown"
        elif isinstance(current, TerminalOutputTruncatedError) and code == "runtime_turn_failed":
            code = "runtime_output_truncated"
        seen.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return code


def _hit_provider_deadline(exc: BaseException) -> bool:
    """Whether our own provider deadline fired anywhere on ``exc``'s cause chain."""
    from app.agents._runtime import ProviderDeadlineExceededError  # noqa: PLC0415

    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not link for link in seen):
        if isinstance(current, ProviderDeadlineExceededError):
            return True
        seen.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return False


def _project_retryable_failure(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    turn: CreatorAgentTurn,
    thread: CreationThread,
    *,
    code: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Fail a locked turn and tell the creator what to do next; the caller commits.

    ``detail`` (``error_class`` + a truncated ``error_message``) makes an
    otherwise opaque ``runtime_turn_failed`` diagnosable (KRI-203: the
    2026-09-25 failure carried only the code). The full detail lives on
    ``turn.error``, which only admin routes read. The ``assistant_error`` event
    payload is returned unfiltered to the creator's app, so it gets the class
    name only (see ``_event_failure_detail``). Creator-facing copy is unchanged.
    """

    # A spend cap answers the same on every retry until someone raises it: say so and
    # offer no retry (A1). Everything else here is worth retrying.
    retryable = code not in _NOT_RETRYABLE_NOW_TURN_CODES
    recovery = "retry" if retryable else "ask_user"
    turn.status = "failed"
    turn.error = {"code": code, "retryable": retryable, "recovery": recovery, **(detail or {})}
    turn.completed_at = datetime.now(UTC)
    turn.lease_owner = None
    turn.lease_expires_at = None
    # KRI-520: also reached from `_claim` (claims exhausted) before any turn binding, so
    # the copy follows the chat's language from the thread row itself.
    with reply_language_for(thread_reply_language(thread)):
        en, tr = _TURN_FAILURE_COPY.get(code, _TURN_FAILURE_DEFAULT_COPY)
        failure_copy = say(en=en, tr=tr)
    event = _append_sync_event(
        db,
        thread,
        role="assistant",
        event_type="assistant_error",
        content=failure_copy,
        payload={
            "turn_id": str(turn.id),
            "code": code,
            "retryable": retryable,
            "recovery": recovery,
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


def _event_failure_detail(detail: dict[str, Any] | None) -> dict[str, str]:
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


def _draft_take_window(
    brief: Any, strategy: Mapping[str, Any] | None, media_snapshot: Any, item: Any
) -> Any:
    """KRI-790: the take window a phone Talking draft's quoted lines keep (None = no ask, or
    an estimate that failed: the receipts then say nothing about where the lines land)."""
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
    from app.kria.opening_cut_ask import estimate_take_window  # noqa: PLC0415

    try:
        return estimate_take_window(
            brief.live(),
            media_snapshot if media_snapshot is not None else snapshot_media(item),
            strategy=strategy,
        )
    except Exception as exc:  # noqa: BLE001 - an estimate never fails a turn
        log.warning("kria_draft_take_window_failed", error=str(exc)[:200])
        return None


def _draft_unclear_words(
    brief: Any, strategy: Mapping[str, Any] | None, media_snapshot: Any, item: Any
) -> dict[str, Any] | None:
    """KRI-849: what a phone Talking draft does about the word the creator asked to be asked
    about (None = no such ask, or a comparison that failed: the receipts then say nothing)."""
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
    from app.kria.brief_checks import brief_caption_spellings  # noqa: PLC0415
    from app.kria.unclear_word_ask import draft_state  # noqa: PLC0415

    try:
        return draft_state(
            brief.live(),
            media_snapshot if media_snapshot is not None else snapshot_media(item),
            strategy=strategy,
            spellings=brief_caption_spellings(brief),
        )
    except Exception as exc:  # noqa: BLE001 - a receipt line never fails a turn
        log.warning("kria_draft_unclear_words_failed", error=str(exc)[:200])
        return None


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
    detail: dict[str, Any] | None = None,
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
    turn_started = time.monotonic()
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
    thought_publisher = (
        publisher_for_creation(
            creator_id=uuid.UUID(str(snapshot["creator_id"])),
            thread_id=uuid.UUID(str(snapshot["thread_id"])),
            client_request_id=str(snapshot["client_request_id"]),
        )
        if snapshot.get("client_request_id")
        else None
    )
    # A follow-up sent while this turn plans is queued behind it: plan without it.
    history_token = answering_through_sequence.set(snapshot.get("source_sequence"))
    try:
        if (
            settings.main_creator_agent_enabled or snapshot.get("camera_roll_story")
        ) and snapshot.get("item_id"):
            planned = asyncio.run(
                _plan_with_live_agent(
                    snapshot,
                    user_message,
                    turn_id=identifier,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                    thought_publisher=thought_publisher,
                    **({"editor_state": editor_state} if editor_state else {}),
                    **({"answers_clip_question": True} if answers_clip_question else {}),
                )
            )
            planned = _useful_plan(planned, user_message=user_message)
            # KRI-881: research a typed research ask before the approval card, outside
            # every lock; "checking sources" continues the turn when it needs more time.
            from app.kria import research_preview  # noqa: PLC0415

            # Planning's own heartbeat stopped with it; research takes 20-90 s and the
            # lease is 15 s, so keep renewing it or the draft commit finds the turn lost.
            with _lease_heartbeat_thread(
                identifier, lease_owner=lease_owner, lease_epoch=lease_epoch
            ):
                planned = research_preview.with_research(
                    planned,
                    snapshot,
                    turn_id=identifier,
                    deadline=turn_started + float(run_kria_turn.soft_time_limit or 200),
                )
            if planned.plan.mode == "respond" and planned.continuable:
                # KRI-709: the planner stopped on a time budget with its answers saved.
                # Run the same turn again instead of asking the creator to say "go
                # ahead"; the chat keeps showing "Thinking" (no assistant event yet).
                outcome = _continue_turn(
                    identifier, lease_owner=lease_owner, lease_epoch=lease_epoch
                )
                if outcome == "lost":
                    return {"turn_id": turn_id, "status": "ignored"}
                if outcome == "continued":
                    try:
                        run_kria_turn.apply_async(
                            args=[turn_id],
                            # Unique per run: the finished run's own id is still on record.
                            task_id=f"{turn_id}-c{lease_epoch}",
                            queue="agent-control",
                            countdown=_CONTINUE_COUNTDOWN_S,
                        )
                    except Exception:  # noqa: BLE001 - the reconciler republishes the pending row
                        log.warning("kria_turn_continue_publish_failed", turn_id=turn_id)
                    return {"turn_id": turn_id, "status": "continued"}
                if outcome == "successor":
                    planned = replace(
                        planned,
                        plan=planned.plan.model_copy(
                            update={"response": continuation_superseded_reply()}
                        ),
                    )
            if planned.plan.mode == "respond":
                completion = _complete_response_turn(
                    identifier,
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                    claimed_thread_revision=claimed_thread_revision,
                    plan=planned.plan,
                    brief_updates=planned.brief_updates,
                    creative_copy_resolution=planned.creative_copy_resolution,
                    brief_delegated=planned.brief_delegated,
                    brief_delegation_read_ids=planned.brief_delegation_read_ids,
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
                        client_protocol=stamped_client_protocol(snapshot.get("client_protocol")),
                    )
                except TextTimingConflict as exc:
                    planned = replace(planned, plan=_text_timing_conflict_plan(exc))
                    completion = _complete_response_turn(
                        identifier,
                        lease_owner=lease_owner,
                        lease_epoch=lease_epoch,
                        claimed_thread_revision=claimed_thread_revision,
                        plan=planned.plan,
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
                except (ApprovedCreatorRequestTooLongError, BriefBindingRequestTooLongError):
                    planned = replace(
                        planned,
                        plan=KriaTurnPlan(
                            mode="respond",
                            turn_value="recovery",
                            response=say(
                                en=(
                                    "Your full edit request is too long to preserve safely. "
                                    "Nothing was changed; please start a new request with "
                                    "the key directions."
                                ),
                                tr=(
                                    "Tam düzenleme isteğin güvenle korumak için çok uzun. "
                                    "Hiçbir şey değişmedi; önemli yönergelerle yeni bir "
                                    "istek başlat."
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
            if thought_publisher is not None:
                thought_publisher.complete()
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
            code=_turn_failure_code(exc),
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            detail={
                **_failure_detail(exc),
                # KRI-1037: marks a deadline (not a transport error) for `_claim`'s lookup.
                **({"provider_deadline": True} if _hit_provider_deadline(exc) else {}),
            },
        )
        if successor_turn_id is not None:
            run_kria_turn.apply_async(
                args=[successor_turn_id],
                task_id=successor_turn_id,
                queue="agent-control",
            )
        raise
    finally:
        if thought_publisher is not None:
            thought_publisher.fail()
        release_reply_language(language_token)
        answering_through_sequence.reset(history_token)


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
    name="tasks.prepare_kria_research",
    soft_time_limit=170,
    time_limit=200,
    max_retries=0,
)
def prepare_kria_research(payload: dict[str, Any]) -> dict[str, str]:
    """KRI-881: research one strategy draft before approval with a full budget.

    Enqueued when a turn had too little time left; caches the result by the basis it was
    researched for, so the continued turn finds it. Writes no thread, draft or approval.
    """
    from app.kria import research_preview  # noqa: PLC0415

    return {"status": research_preview.run_task_body(payload)}


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
        snapshot = _snapshot(thread)
        message = _model_message(source, language=snapshot.get("reply_language"))
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


def _verify_prepared_execution(
    db: Any,
    *,
    approval: CreatorAgentApproval,
    draft: CreatorEditDraft,
    document: KriaDraftDocument,
    item: PlanItem,
    execution: CreatorAgentExecution,
) -> tuple[dict[str, Any] | None, Any]:
    """KRI-722: (attempt record, None) when the approved snapshot still holds, else
    (None, PreparedExecutionError). The first refusal is written to the row for audit.

    A fresh claim re-derives every source identity and mints a new attempt id. An
    accepted redelivery (lost acknowledgement, outcome_unknown) of the same snapshot
    keeps its attempt and is not re-fenced: its inputs passed at the original claim and
    its Job may already be rendering, so a later deploy, flag flip or source change
    must not cancel work that was already dispatched (as the legacy media fence does).
    """

    from app.kria import prepared_execution as prepared  # noqa: PLC0415
    from app.models import CreatorPreparedExecution  # noqa: PLC0415

    row = db.get(CreatorPreparedExecution, approval.prepared_execution_id)
    accepted = (execution.result or {}).get(prepared.RESULT_FIELD)
    try:
        if row is None or row.creator_id != approval.creator_id:
            raise prepared.PreparedExecutionError(prepared.STALE, "The prepared edit is missing.")
        if (
            execution.status == "accepted"
            and isinstance(accepted, dict)
            and accepted.get("id") == str(row.id)
        ):
            return dict(accepted), None
        snapshot = prepared.parse(row)
        if row.invalidated_at is not None:
            raise prepared.PreparedExecutionError(
                row.invalidation_code or prepared.STALE, "The prepared edit was invalidated."
            )
        prepared.verify(
            snapshot,
            draft=draft,
            document=document,
            thread_id=approval.thread_id,
            sources=prepared.load_source_identities_sync(
                db,
                item,
                approval.creator_id,
                slides=prepared.SLIDE_POST_REF in snapshot.compiled_refs,
            ),
            item=item,
        )
    except prepared.PreparedExecutionError as exc:
        if row is not None and row.invalidated_at is None:
            row.invalidated_at = datetime.now(UTC)
            row.invalidation_code = exc.code
        log.info(
            "kria_prepared_execution_refused",
            approval_id=str(approval.id),
            code=exc.code,
            changed=exc.changed,
        )
        return None, exc
    return (
        prepared.attempt_record(
            row,
            snapshot.digest,
            prepared.new_attempt_id(),
            approval_id=str(approval.id),
            draft_id=str(draft.id),
            draft_revision=int(draft.draft_revision),
            binding_digest=snapshot.binding_digest,
        ),
        None,
    )


def _claim_approval_dispatch(approval_id: uuid.UUID) -> _ApprovalDispatchClaim | None:
    """Consume exact consent into a durable accepted execution.

    This transaction performs no broker or renderer work. A crash after commit
    leaves ``accepted`` as the recovery ledger for the minute reconciler.
    """

    from app.kria.render_retry import claim_render_retry  # noqa: PLC0415

    handled, retry_claim = claim_render_retry(approval_id)
    if handled:
        return retry_claim
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

        prepared_record: dict[str, Any] | None = None
        prepared_failure: Any = None
        if (
            target_valid
            and document is not None
            and getattr(approval, "prepared_execution_id", None) is not None
        ):
            prepared_record, prepared_failure = _verify_prepared_execution(
                db,
                approval=approval,
                draft=draft,
                document=document,
                item=item,
                execution=execution,
            )
            target_valid = prepared_failure is None

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

        if (
            target_valid
            and document is not None
            and getattr(document, "prepared_collection_story", None) is not None
        ):
            from app.services.media_collection_preparation import install_approved_story

            try:
                install_approved_story(
                    db,
                    item=item,
                    owner=thread.creator_id,
                    raw=document.prepared_collection_story,
                    strategy=document.strategy,
                    dry_run=True,
                )
            except ValueError:
                target_valid = False
        unknown_outcome = False
        if (
            target_valid
            and document is not None
            and document.kind == "editor"
            and current_job is not None
            and execution.status == "awaiting_approval"
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
            if current_variant and current_variant.get("render_status") in {
                "failed",
                "needs_attention",
            }:
                unknown_outcome = (
                    db.execute(
                        unknown_editor_render_query(session, current_job, current_variant)
                    ).scalar_one_or_none()
                    is not None
                )
                if unknown_outcome:
                    target_valid = False
        now = datetime.now(UTC)
        if not target_valid:
            stale_code = (
                "render_outcome_unknown"
                if unknown_outcome
                else CREATIVE_COPY_PENDING
                if copy_problem
                else prepared_failure.code
                if prepared_failure is not None
                else "approval_target_stale"
            )
            approval.status = "cancelled"
            execution.status = "stale"
            execution.error = {
                "code": stale_code,
                "retryable": False,
                "recovery": "refresh_replan",
                **({"changed": prepared_failure.changed} if prepared_failure is not None else {}),
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
                    say(
                        en=(
                            "The previous render's outcome is still unknown. "
                            "I kept your corrected draft and did not start another render. "
                            "Refresh the project to check its status."
                        ),
                        tr=(
                            "Önceki videonun sonucu hâlâ bilinmiyor. "
                            "Düzeltilmiş taslağını sakladım ve başka bir video başlatmadım. "
                            "Sonucu kontrol etmek için projeyi yenile."
                        ),
                    )
                    if unknown_outcome
                    else copy_problem
                )
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
                    "code": stale_code,
                    "recovery": "refresh_replan",
                },
            )
            db.commit()
            return None

        if prepared_record is not None and document.kind == "editor" and current_job is not None:
            # An editor approval re-pins its target variant inside this claim, so the
            # Job carries the stamp before the recipe is pinned (the pin copies it).
            prepared_record = {**prepared_record, "variant_id": approval.target_variant_id}
            prepared_job_before = (current_job.all_candidates, current_job.assembly_plan)
            from app.kria.prepared_execution import stamp_job  # noqa: PLC0415

            stamp_job(current_job, prepared_record)

        creator_guided_attempt_id = None
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
            if getattr(document, "prepared_collection_story", None) is not None:
                from app.services.media_collection_preparation import install_approved_story

                creator_guided_attempt_id = install_approved_story(
                    db,
                    item=item,
                    owner=thread.creator_id,
                    raw=document.prepared_collection_story,
                    strategy=strategy_payload,
                )
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
                plan_before_background_pin = current_job.assembly_plan
                try:
                    # Inside the try: deriving the lanes re-validates the pinned
                    # recipe, and a device recipe that fails is a refusal too.
                    phone_sfx_catalog_paths = phone_subtitled_sfx_paths_sync(
                        db,
                        current_job,
                        _find_variant(current_job, approval.target_variant_id) or {},
                    )
                    # KRI-1117: a chat edit that sets a Talking background pins its pool
                    # visual first, like the HTTP Save route (never any other Save).
                    pin_background_for_commit_sync(
                        db,
                        current_job,
                        approval.target_variant_id,
                        editor_payload,
                        plan_item_id=item.id,
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
                    from app.services.finished_style_learning import (
                        explicitly_temporary,
                        stage_prepared_style_edit_sync,
                    )

                    stage_prepared_style_edit_sync(
                        db,
                        thread.creator_id,
                        current_job,
                        approval.target_variant_id,
                        editor_prep,
                        temporary=explicitly_temporary(document.intent),
                    )
                except (HTTPException, ValueError, KeyError) as exc:
                    # A refused Save leaves no pinned receipt behind (the pin replaces the
                    # plan dict, never edits it in place).
                    current_job.assembly_plan = plan_before_background_pin
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
                    if prepared_record is not None:
                        # Nothing was pinned: the Job keeps the attempt it had.
                        current_job.all_candidates, current_job.assembly_plan = prepared_job_before
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
        if prepared_record is not None:
            from app.kria.prepared_execution import RESULT_FIELD  # noqa: PLC0415

            execution.result = {**(execution.result or {}), RESULT_FIELD: prepared_record}
        turn.status = "executing"
        session.status = "executing"
        db.commit()
        return _ApprovalDispatchClaim(
            prepared_execution=prepared_record,
            approval_id=approval.id,
            execution_id=execution.id,
            thread_id=thread.id,
            turn_id=turn.id,
            session_id=session.id,
            item_id=item.id,
            ownership_epoch=int(session.ownership_epoch),
            draft_kind=document.kind,
            creator_guided_attempt_id=creator_guided_attempt_id,
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
            client_protocol=stamped_client_protocol(
                (execution.result or {}).get(APPROVAL_CLIENT_PROTOCOL_FIELD)
            ),
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
# KRI-1117: a background the Save or the pin refused, with the reason (S18).
_DEVICE_EDIT_REFUSALS.update(
    {
        code: f"{en}, so I left the video as it was."
        for code, (en, _tr) in BACKGROUND_REFUSALS.items()
    }
)
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
_DEVICE_EDIT_REFUSALS_TR.update(
    {
        code: f"{tr}, o yüzden videoyu olduğu gibi bıraktım."
        for code, (_en, tr) in BACKGROUND_REFUSALS.items()
    }
)
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
            # Output checks are advisory: a published export is a finished video,
            # whatever its saved verification report says.
            states.append("ready")
        elif status.phase == "needs_attention":
            states.append("failed")
        else:
            states.append("pending")
    if "pending" in states:
        return "pending"
    return "failed" if "failed" in states else "ready"


_DETERMINISTIC_JOB_FAILURE_CODES = {
    "phone_plan_unsupported",
    "user_song_plan_declined",
    "silent_audio_declined",
}

# Not a property of the edit, but a retry now answers the same: the AI provider's spend cap
# or our AI budget is exhausted until someone raises it. No Retry button, no ask-for-a-change
# copy; the table sentence says to wait.
_NOT_RETRYABLE_NOW_JOB_FAILURE_CODES = {"ai_quota_exhausted"}


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
    for key in ("field_path", "alternative", "missing_text"):
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
            for key in ("field_path", "alternative", "message", "missing_text"):
                if isinstance(raw.get(key), str) and raw[key].strip():
                    decline[key] = raw[key].strip()[:500]
            return decline
    return None


# KRI-667: what the render workers and the pin-time verifier write for a refused edit
# (`error_detail`, a decline's `alternative`, the device record's `message`). They run with
# no chat language bound, so their text is always English; the chat re-words it here, by
# exact text (the `brief_checks._loc` precedent). A sentence with no Turkish twin is left
# out of a Turkish reply (`_worker_copy`), never shown in English. A test pins that every
# key still matches what the workers write.
_WORKER_TEXT_TR: dict[str, str] = {
    # services/creator_render_contract.py
    "Tell me which option you want and I'll continue.": (
        "Hangi seçeneği istediğini söyle, devam edeyim."
    ),
    "I can rebuild the edit so it shows this, or you can relax the requirement.": (
        "Düzenlemeyi bunu gösterecek şekilde yeniden kurabilirim ya da bu isteği esnetebilirsin."
    ),
    (
        "Ask for it to be rendered on your iPhone, where I can check it, "
        "or tell me to drop that requirement."
    ): ("Kontrol edebilmem için iPhone'unda oluşturulmasını iste ya da bu isteği bırakmamı söyle."),
    "I can rebuild the edit with that soundtrack playing once, or you can pick a different one.": (
        "Düzenlemeyi o ses bir kez çalacak şekilde yeniden kurabilirim ya da başka bir ses "
        "seçebilirsin."
    ),
    "Choose either your recorded voice or the camera audio for this edit.": (
        "Bu düzenleme için ya kaydettiğin sesi ya da kamera sesini seç."
    ),
    "I can rebuild the edit without that soundtrack, or you can ask for music.": (
        "Düzenlemeyi o ses olmadan yeniden kurabilirim ya da müzik isteyebilirsin."
    ),
    "Your confirmed edit lengths conflict, so I can't render it safely.": (
        "Onayladığın uzunluklar birbiriyle çelişiyor, o yüzden videoyu güvenle oluşturamıyorum."
    ),
    "Tell me which length you want.": "Hangi uzunluğu istediğini söyle.",
    "This edit belongs to a different approved revision.": (
        "Bu düzenleme onaylanmış başka bir sürüme ait."
    ),
    "Ask me to make the edit again from your latest request.": (
        "Düzenlemeyi son isteğine göre yeniden yapmamı iste."
    ),
    "This edit needs your confirmed recorded voice.": (
        "Bu düzenleme için onayladığın ses kaydın gerekiyor."
    ),
    "Record or upload your voice, or tell me to use music instead.": (
        "Sesini kaydet ya da yükle; istersen bunun yerine müzik kullanmamı söyle."
    ),
    "This renderer can't combine the confirmed soundtracks.": (
        "Bu video hazırlama yolu onayladığın sesleri birleştiremiyor."
    ),
    "Choose one soundtrack: the camera audio, your voice, or your song.": (
        "Tek bir ses seç: kamera sesi, kendi sesin ya da şarkın."
    ),
    "I couldn't build the confirmed camera-audio edit.": (
        "Onayladığın kamera sesli düzenlemeyi kuramadım."
    ),
    "Pick a different clip to carry the speech, or ask for a plain montage.": (
        "Konuşmayı taşıyacak başka bir klip seç ya da düz bir montaj iste."
    ),
    "I can't verify this ordering rule from the approved media.": (
        "Bu sıralama kuralını onaylanan çekimlerden doğrulayamıyorum."
    ),
    "I need to know which clips are in the edit to verify their order.": (
        "Sıralarını doğrulamak için düzenlemede hangi kliplerin olduğunu bilmem gerekiyor."
    ),
    "I need capture times for every selected clip to verify chronological order.": (
        "Çekim sırasını doğrulamak için seçilen her klibin çekim zamanını bilmem gerekiyor."
    ),
    "I couldn't tell which clips you want first or last, so I can't confirm their order.": (
        "Hangi klipleri başa ya da sona istediğini anlayamadım, o yüzden sıralarını "
        "doğrulayamıyorum."
    ),
    "This edit is missing confirmed on-screen text.": (
        "Bu düzenlemede onayladığın ekran yazısı eksik."
    ),
    "This edit couldn't keep confirmed text on screen long enough.": (
        "Bu düzenleme onayladığın yazıyı ekranda yeterince uzun tutamadı."
    ),
    "This edit put confirmed text in the wrong place.": (
        "Bu düzenleme onayladığın yazıyı yanlış yere koydu."
    ),
    "This edit keeps confirmed text on screen after the picture has ended.": (
        "Bu düzenleme onayladığın yazıyı görüntü bittikten sonra da ekranda tutuyor."
    ),
    # services/cloud_render_contract.py
    "I can render it again, or you can relax the requirement.": (
        "Videoyu yeniden oluşturabilirim ya da bu isteği esnetebilirsin."
    ),
    (
        "Ask for a spoken-excerpt montage on your iPhone, which can keep only that camera audio, "
        "or tell me to drop that requirement."
    ): (
        "iPhone'unda yalnızca o kamera sesini koruyabilen, konuşma kesitli bir montaj iste ya da "
        "bu isteği bırakmamı söyle."
    ),
    (
        "Ask for it to be rendered on your iPhone, which renders clips in the order you confirmed, "
        "or tell me to drop that requirement."
    ): (
        "Klipleri onayladığın sırayla oluşturan iPhone'unda oluşturulmasını iste ya da bu isteği "
        "bırakmamı söyle."
    ),
    (
        "Ask for a spoken-excerpt montage on your iPhone, which keeps or mutes camera audio as you "
        "confirmed, or tell me to drop that requirement."
    ): (
        "iPhone'unda kamera sesini onayladığın gibi koruyan ya da kısan, konuşma kesitli bir "
        "montaj iste ya da bu isteği bırakmamı söyle."
    ),
    # services/render_route.py
    "Ask for a different format.": "Farklı bir format iste.",
    "Ask for a montage with your voice, or a different format.": (
        "Sesinle bir montaj ya da farklı bir format iste."
    ),
    "Record your voice for this edit, or ask for a different format.": (
        "Bu düzenleme için sesini kaydet ya da farklı bir format iste."
    ),
    "Ask for a montage, a subtitled clip, or a narrated edit.": (
        "Bir montaj, altyazılı bir klip ya da seslendirmeli bir düzenleme iste."
    ),
    (
        "This plan has a guided story but the edit format is audio-led. "
        "Pick one: the guided story, or the spoken/narrated edit."
    ): (
        "Bu planda rehberli bir hikâye var ama düzenleme formatı sese dayalı. Birini seç: "
        "rehberli hikâye ya da konuşmalı/seslendirmeli düzenleme."
    ),
    # pipeline/phone_speech_montage_plan.py
    "Pick a clip where you talk to use as the voice, or ask for a plain montage.": (
        "Ses olarak kullanmak için konuştuğun bir klip seç ya da düz bir montaj iste."
    ),
    # tasks/generative_build.py
    "I can rebuild the edit from your confirmed plan, or you can ask for a different edit.": (
        "Düzenlemeyi onayladığın plana göre yeniden kurabilirim ya da farklı bir düzenleme "
        "isteyebilirsin."
    ),
    "I couldn't find clear speech in your clips to build this edit.": (
        "Bu düzenlemeyi kurmak için kliplerinde net bir konuşma bulamadım."
    ),
    "Pick a clip where you talk, record a voiceover, or ask for a montage.": (
        "Konuştuğun bir klip seç, bir seslendirme kaydet ya da bir montaj iste."
    ),
    "The clip with your speech is too short to cut other footage into.": (
        "Konuştuğun klip, araya başka çekimler koymak için çok kısa."
    ),
    "Use a longer clip of you talking, or ask for a montage.": (
        "Konuştuğun daha uzun bir klip kullan ya da bir montaj iste."
    ),
    "I couldn't read the speech in your clip.": "Klibindeki konuşmayı okuyamadım.",
    "Try a different clip of you talking, or ask for a montage.": (
        "Konuştuğun başka bir klibi dene ya da bir montaj iste."
    ),
    "Your video has no sound, so there's nothing to caption or cut.": (
        "Videonda hiç ses yok, o yüzden altyazı eklenecek ya da kesilecek bir şey yok."
    ),
    "Record it again and check that nothing is covering the microphone.": (
        "Yeniden kaydet ve mikrofonun üstünü hiçbir şeyin kapatmadığından emin ol."
    ),
    "Ask me to make the change as a new edit instead.": (
        "Bunun yerine değişikliği yeni bir düzenleme olarak yapmamı iste."
    ),
    "Choose another song or render with footage audio.": (
        "Başka bir şarkı seç ya da videoyu çekimlerin kendi sesiyle oluştur."
    ),
}


def _worker_copy(text: str | None) -> str | None:
    """A worker-written refusal sentence in the chat's language, or None to leave it out.

    English chats get the stored text unchanged. A Turkish chat gets its Turkish twin; a
    sentence with no twin is dropped (the caller keeps its own Turkish line) unless it
    already reads as Turkish.
    """
    if not text or not text.strip():
        return None
    if current_reply_language() != "tr":
        return text
    stored = text.strip()
    turkish = _WORKER_TEXT_TR.get(stored)
    if turkish is not None:
        return turkish
    return stored if detect_chat_language(stored) == "tr" else None


_LAST_GOOD_STAYS = "Your last good version is still available."
_NOTHING_PUBLISHED = "Nothing was published."
# What a creator can really do next: send a new message. The approved request (the brief)
# stays in force across turns, so a plain "redo it" builds a new version from it; the
# runtime has no automatic re-run of a refused phone render and no device-job chat retry.
_REDO_FROM_APPROVED = (
    "Tell me to redo it and I'll make a new version from what you already approved."
)


def _device_refusal_copy(
    decline: dict[str, str], *, last_good: bool = True, question: dict[str, Any] | None = None
) -> str:
    """What the creator hears when the contract refused an edit of a phone render.

    Says what could not be confirmed, that the edit was NOT applied, that the last good
    version is kept, and a next step that exists. Never promises an automatic rebuild and
    never asks the creator to restate a clear instruction. ``question`` is the re-asked
    choice a `needs_choice` refusal shows (KRI-795).
    """
    # The message and alternative come from the server's contract checks, in English;
    # a Turkish chat gets their Turkish twin or, when there is none, the line below.
    reason = decline["decline_reason"]
    quoted = _quoted_missing_text(decline) if reason == "evidence_missing" else None
    message = (
        (
            say(
                en=f"The text {quoted} isn't in that edit.",
                tr=f"{quoted} yazısı bu düzenlemede yok.",
            )
            if quoted
            else None
        )
        or _worker_copy(decline.get("message"))
        or say(
            en="That edit didn't keep something you asked for.",
            tr="Bu düzenleme istediğin bir şeyi korumadı.",
        )
    )
    kept = (
        say(en=_LAST_GOOD_STAYS, tr="Son iyi sürümün hâlâ duruyor.")
        if last_good
        else say(en=_NOTHING_PUBLISHED, tr="Yeni bir video oluşturulmadı.")
    )
    not_applied = say(en="That edit was not applied.", tr="Bu düzenleme uygulanmadı.")
    if reason == "capability_unavailable":
        return f"{_capability_refusal_copy(decline)} {not_applied} {kept}"
    if reason in _CHOICE_DECLINES:
        unresolved, way = _choice_refusal_parts(decline, question)
        return " ".join(part for part in (unresolved, not_applied, kept, way) if part)
    redo = say(
        en=_REDO_FROM_APPROVED,
        tr="Baştan yapmamı söylersen onayladığın isteğe göre yeni bir sürüm hazırlarım.",
    )
    return f"{message} {not_applied} {kept} {redo}"


_MISSING_TEXT_QUOTE_CHARS = 120


def _quoted_missing_text(decline: dict[str, str]) -> str | None:
    """The confirmed on-screen text a refused edit lacks, quoted for a chat line."""
    text = " ".join(str(decline.get("missing_text") or "").split())
    if not text:
        return None
    if len(text) > _MISSING_TEXT_QUOTE_CHARS:
        text = text[: _MISSING_TEXT_QUOTE_CHARS - 1].rstrip() + "…"
    return f"“{text}”"


def _phone_evidence_refusal_copy(decline: dict[str, str]) -> str:
    """A phone plan the contract refused for missing evidence (KRI-701).

    Nothing here is an iPhone limit, so it never says "your iPhone can't render": a missing
    text is named with one question, anything else says what the edit lacked (the
    alternative, when there is one, is added by the caller).
    """
    quoted = _quoted_missing_text(decline)
    if quoted:
        return say(
            en=(
                f"Your video wasn't made: the text {quoted} isn't in the edit. "
                "Want me to add it, or leave it out?"
            ),
            tr=(
                f"Videon oluşturulmadı: {quoted} yazısı düzenlemede yok. "
                "Ekleyeyim mi, yoksa bu yazı olmadan mı yapayım?"
            ),
        )
    return _worker_copy(decline.get("message")) or say(
        en="Your video wasn't made: the edit didn't keep something you asked for.",
        tr="Videon oluşturulmadı: düzenleme istediğin bir şeyi korumadı.",
    )


# KRI-795: a refusal that needs the creator's decision (`needs_choice`,
# `requirement_conflict`). The phone never saw the edit, so it is never "something your iPhone
# can't render". The contract's unresolved sentences, keyed by the exact English the render
# worker stores: (plain English, Turkish, what to do next in English and Turkish, the thread
# question that answers it). That question is asked again on the failure card, so a tap (or
# its label typed) is the answer the next turn's gate replays. A test pins every key to
# `creator_render_contract`.
_CHOICE_DECLINES = frozenset({"needs_choice", "requirement_conflict"})
_ORDER_NEXT_STEP = (
    "Tell me how you want the clips ordered, and I'll make a new draft.",
    "Klipleri hangi sırayla istediğini söyle, yeni bir taslak hazırlayayım.",
)
_UNRESOLVED_PLAIN: dict[str, tuple[str, str, tuple[str, str], str | None]] = {
    "I can't verify this ordering rule from the approved media.": (
        "I couldn't keep the clip order you asked for.",
        "İstediğin klip sırasını koruyamadım.",
        _ORDER_NEXT_STEP,
        "order_basis",
    ),
    # Written only after the creator chose upload order and the clip rows did not add up,
    # so the order question's recommended answer would hit the same refusal: no picker.
    "I need to know which clips are in the edit to verify their order.": (
        "I couldn't tell which clips are in this edit, so I couldn't keep their order.",
        "Bu düzenlemede hangi kliplerin olduğunu anlayamadım, o yüzden sıralarını koruyamadım.",
        _ORDER_NEXT_STEP,
        None,
    ),
    "I need capture times for every selected clip to verify chronological order.": (
        "Some of your clips have no filming time, so I couldn't put them in the order you "
        "filmed them.",
        "Bazı kliplerinde çekim saati yok, o yüzden onları çektiğin sıraya koyamadım.",
        _ORDER_NEXT_STEP,
        "order_basis",
    ),
    # KRI-762: named first / last clips are the clip resolver's, never the order question.
    "I couldn't tell which clips you want first or last, so I can't confirm their order.": (
        "I couldn't tell which clips you want first or last.",
        "Hangi klipleri başa ya da sona istediğini anlayamadım.",
        (
            "Tell me which clip goes first or last, and I'll make a new draft.",
            "Hangi klibin başta ya da sonda olacağını söyle, yeni bir taslak hazırlayayım.",
        ),
        None,
    ),
}
_CLIP_NEXT_STEP = (
    "Tell me which clip you mean, and I'll make a new draft.",
    "Hangi klibi kastettiğini söyle, yeni bir taslak hazırlayayım.",
)


def _quoted_unresolved(stored: str) -> tuple[str, tuple[str, str]] | None:
    """A contract sentence that quotes the creator's words, in the chat's language.

    The worker wrote it with one of the contract's own sentence builders, in English or (a
    dispatch bound to the chat's language, KRI-765) in Turkish; the builder that produced it
    is found by its fixed words around the quote and called again with the same words under
    the chat's language, so both languages stay the contract's.
    """
    from app.services.clip_order_sequence import unclear_sequence_words  # noqa: PLC0415
    from app.services.creator_render_contract import (  # noqa: PLC0415
        _pin_not_found,
        text_clip_not_found,
    )

    for build, step in (
        (_pin_not_found, _CLIP_NEXT_STEP),
        (unclear_sequence_words, _CLIP_NEXT_STEP),
        (
            text_clip_not_found,
            (
                "Tell me which clip that text goes on, and I'll make a new draft.",
                "O yazının hangi klibe gideceğini söyle, yeni bir taslak hazırlayayım.",
            ),
        ),
    ):
        for written_in in ("en", "tr"):
            with reply_language_for(written_in):
                head, _, tail = build("\x00").partition("\x00")
            if (
                len(stored) > len(head) + len(tail)
                and stored.startswith(head)
                and stored.endswith(tail)
            ):
                return build(stored[len(head) : len(stored) - len(tail)]), step
    return None


def _choice_refusal_parts(
    decline: dict[str, str], question: dict[str, Any] | None
) -> tuple[str, str | None]:
    """``(what is unresolved, the way forward)`` for a refusal that needs a decision.

    The way forward is the re-asked question with its options listed, else the worker's
    own alternative, else what to tell me; never "which option" with no options shown.
    ``None`` when the reason is itself the question.
    """
    from app.services.choice_questions import reasked_question_text  # noqa: PLC0415
    from app.services.creator_render_contract import _ASK_FOR_CHOICE  # noqa: PLC0415

    stored = " ".join(str(decline.get("message") or "").split())
    plain = _UNRESOLVED_PLAIN.get(stored)
    quoted = None if plain else _quoted_unresolved(stored)
    if plain:
        reason, step = say(en=plain[0], tr=plain[1]), plain[2]
    elif quoted:
        reason, step = quoted
    else:
        step = None
        reason = _worker_copy(stored) or say(
            en="I couldn't keep something you asked for in this edit.",
            tr="İstediğin bir şeyi bu düzenlemede koruyamadım.",
        )
    if question is not None:
        return reason, reasked_question_text(question)
    alternative = " ".join(str(decline.get("alternative") or "").split())
    if alternative and alternative != _ASK_FOR_CHOICE:
        return reason, _worker_copy(alternative) or _try_different_approach()
    if step is not None:
        return reason, say(en=step[0], tr=step[1])
    return reason, None if reason.endswith("?") else _try_different_approach()


def _choice_refusal_copy(decline: dict[str, str], question: dict[str, Any] | None) -> str:
    reason, way = _choice_refusal_parts(decline, question)
    return f"{reason} {way}" if way else reason


def _reasked_choice_question(
    db,  # noqa: ANN001 - SQLAlchemy sync Session
    thread_id: uuid.UUID,
    decline: dict[str, str] | None,
    job: Job,
    *,
    source_event_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """The thread's latest question for this refusal's choice, asked again (KRI-795).

    Only a refusal the thread's own question answers (`_UNRESOLVED_PLAIN`), only a question
    the gate really asked in this thread (its digest is what the next turn replays an answer
    against), and only while choice questions are on. Anything else gets no picker: a tap
    nothing replays is worse than plain words. The answer the refused render carried is not
    recommended again.
    """
    from app.services.choice_questions import (  # noqa: PLC0415
        fold_scoped_answers,
        reask_choice_question,
    )

    if (
        decline is None
        or decline.get("decline_reason") != "needs_choice"
        or not settings.kria_choice_questions_enabled
    ):
        return None
    plain = _UNRESOLVED_PLAIN.get(" ".join(str(decline.get("message") or "").split()))
    conflict = plain[3] if plain else None
    if conflict is None:
        return None
    asked = db.execute(
        select(CreationThreadEvent.payload)
        .where(
            CreationThreadEvent.thread_id == thread_id,
            CreationThreadEvent.role == "assistant",
            CreationThreadEvent.payload["choice_question"]["conflict"].astext == conflict,
        )
        .order_by(CreationThreadEvent.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    question = asked.get("choice_question") if isinstance(asked, dict) else None
    if not isinstance(question, dict):
        return None
    rows = db.execute(
        select(
            CreationThreadEvent.role,
            CreationThreadEvent.payload,
            CreationThreadEvent.sequence,
        )
        .where(CreationThreadEvent.thread_id == thread_id)
        .order_by(CreationThreadEvent.sequence)
    ).all()
    history = [(role, payload) for role, payload, _sequence in rows]
    if count_asks(history, conflict, question.get("input_digest")) >= MAX_ASKS_PER_QUESTION:
        return None
    # An old render can finish after a new server-derived answer is queued. Its
    # failure must not reopen a choice the creator has already changed/confirmed.
    source = db.get(CreationThreadEvent, source_event_id) if source_event_id else None
    refused = _pinned_answer(job, conflict)
    if source is not None:
        approved = fold_scoped_answers(
            (role, payload) for role, payload, sequence in rows if sequence <= source.sequence
        ).get(conflict)
        current = fold_scoped_answers(history).get(conflict)
        if current is not None and (approved is None or current.index > approved.index):
            return None
        if refused is None and approved is not None:
            refused = approved.option_key
    return reask_choice_question(question, refused=refused)


def _pinned_answer(job: Job, conflict: str) -> str | None:
    """The option this job's approval pinned for ``conflict``: on the brief binding, or on
    the approved strategy (the job factory may have stripped it there, KRI-788)."""
    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    binding = plan.get("creator_brief_binding")
    candidates = getattr(job, "all_candidates", None)
    strategy = candidates.get("creator_strategy") if isinstance(candidates, dict) else None
    for source in (binding, strategy):
        rows = source.get("choice_answers") if isinstance(source, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("conflict") == conflict and row.get("option"):
                return str(row["option"])
    return None


def _capability_refusal_copy(decline: dict[str, str]) -> str:
    """A refusal that names the limit and the supported way forward."""
    limit = _worker_copy(decline.get("message")) or say(
        en="This render path can't keep that requirement.",
        tr="Bu video hazırlama yolu bu isteği koruyamıyor.",
    )
    alternative = _worker_copy(decline.get("alternative")) or _try_different_approach()
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
    turkish = _PHONE_GATE_MESSAGES_TR.get(reason)
    if reason == "subtitled_clip_count_unsupported":
        from app.services.phone_rollout import (  # noqa: PLC0415
            TALKING_MULTI_CLIP_MAX_CLIPS,
            phone_talking_multi_clip_supported,
        )

        if phone_talking_multi_clip_supported():
            # KRI-719: the reason now means "more than the cap", not "not one".
            turkish = (
                "Kameraya konuşma videoları iPhone'unda en fazla "
                f"{TALKING_MULTI_CLIP_MAX_CLIPS} klipten oluşturulur. Fazla klipleri kaldır "
                "ve tekrar dene. Yedek bir düzenleme oluşturulmadı."
            )
    message = say(en=english or "", tr=turkish or english or "")
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
                # KRI-443: every section starts `waiting` (a scoped update copies the
                # untouched ones from the previous job), in the SAME transaction (and
                # under the same thread lock) as `render_queued`.
                from app.kria.plan_blocks import (  # noqa: PLC0415
                    dispatch_payload,
                    plan_block_payload,
                    waiting_blocks,
                )

                # The Celery task is already queued, so a feed problem must never fail
                # this transaction: build the scoped payload inside a savepoint and fall
                # back to the plain `waiting` event if anything goes wrong.
                try:
                    with db.begin_nested():
                        plan_payload = dispatch_payload(
                            db,
                            thread,
                            turn_id=str(turn.id),
                            job_id=str(job_id),
                            source_event_id=getattr(turn, "source_event_id", None),
                        )
                except Exception as exc:  # noqa: BLE001 - the feed must never fail a dispatch
                    log.warning(
                        "plan_dispatch_payload_failed",
                        job_id=str(job_id),
                        error_class=type(exc).__name__,
                        error=str(exc)[:200],
                    )
                    plan_payload = plan_block_payload(
                        turn_id=str(turn.id), job_id=str(job_id), blocks=waiting_blocks()
                    )
                _append_sync_event(
                    db,
                    thread,
                    role="system",
                    event_type="plan_block",
                    content=None,
                    payload=plan_payload,
                )
                scoped_fields = (
                    _scoped_turn_fields(db.get(CreationThreadEvent, turn.source_event_id))
                    if getattr(claim, "draft_kind", "strategy") == "editor"
                    else None
                )
                if scoped_fields is not None:
                    # KRI-441/442: a scoped editor update (or a section undo) re-renders the
                    # SAME Job, which the previous-job bookkeeping cannot see, so the
                    # changed sections are written here from the freshly committed variant.
                    try:
                        with db.begin_nested():
                            from app.kria import plan_review  # noqa: PLC0415

                            job_row = db.get(Job, uuid.UUID(str(job_id)))
                            variant_row = (
                                _find_variant(job_row, claim.target_variant_id)
                                if job_row is not None
                                else None
                            )
                            if variant_row is not None:
                                plan_review.emit_scoped_update_feed(
                                    db,
                                    thread,
                                    turn_id=str(turn.id),
                                    job=job_row,
                                    variant=variant_row,
                                    scope=scoped_fields["scope"],
                                )
                    except Exception as exc:  # noqa: BLE001 - the feed must never fail a dispatch
                        log.warning(
                            "plan_scoped_update_feed_failed",
                            job_id=str(job_id),
                            error_class=type(exc).__name__,
                            error=str(exc)[:200],
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
    if getattr(claim, "draft_kind", "strategy") == "retry":
        # Claim committed the same recipe under a new device identity. There is
        # no planner, media analysis or broker render task to invoke.
        outcome = "dispatched"
        result_job_id = str(claim.target_job_id)
    elif getattr(claim, "draft_kind", "strategy") == "editor":
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
            bypass_guided_edit_gate=not bool(getattr(claim, "creator_guided_attempt_id", None)),
            **(
                {"creator_guided_attempt_id": claim.creator_guided_attempt_id}
                if getattr(claim, "creator_guided_attempt_id", None)
                else {}
            ),
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
            # KRI-898: the approving app build (absent = byte-identical call).
            **(
                {"creator_client_protocol": claim.client_protocol}
                if getattr(claim, "client_protocol", None) is not None
                else {}
            ),
            # KRI-722: the verified snapshot + attempt (absent = legacy call).
            **(
                {"prepared_execution": claim.prepared_execution}
                if getattr(claim, "prepared_execution", None)
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
    if status == "dispatched" and result_job_id is not None:
        # The render may have finished before this receipt read `dispatched` (a fast phone-plan
        # refusal); its own end-of-task nudge then found nothing to settle.
        nudge_kria_observer(result_job_id)
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
    from app.kria.prepared_execution import pinned_brief_for_job  # noqa: PLC0415

    # KRI-722: an accepted attempt reports against the brief it was approved with;
    # later chat cannot change it. Unstamped (legacy) Jobs keep the latest-brief read.
    stamped, brief = pinned_brief_for_job(db, job, thread.id)
    if not stamped:
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


def _plan_record_generations(job: Job, variant: dict) -> set[str | None]:
    """The generations whose plan records describe this variant's finished output.

    A cloud render (and a pending phone variant) carries the approved generation itself.
    A published phone export carries the phone's upload attempt id instead (KRI-546), so
    the approved generation is read off that variant's device record: the generation its
    recipe was checked against when this very attempt was published. An editor Save pins
    its own generation there, so the original plan's records are never read for it.
    """
    generation = str(variant.get("render_generation_id") or "") or None
    accepted: set[str | None] = {generation}
    records = (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD)
    record = (
        records.get(str(variant.get("variant_id") or "")) if isinstance(records, dict) else None
    )
    if (
        generation is not None
        and variant.get("render_destination") == "device"
        and isinstance(record, dict)
        and str(record.get("published_attempt") or "") == generation
    ):
        pinned = record.get("requirement_generation")
        if isinstance(pinned, str) and pinned:
            accepted.add(pinned)
    return accepted


def _source_fingerprints(job: Job) -> dict[str, str]:
    """Each added clip's media id -> the sha256 of its original upload (KRI-546).

    Read from the job's phone source bindings and Visuals bindings (the fingerprints a
    phone export is checked against), then the approved media snapshot's upload receipts.
    A media id with two different fingerprints is left out, so it is never judged.
    """
    from app.services.phone_sources import (  # noqa: PLC0415
        PHONE_SOURCES_FIELD,
        PHONE_VISUALS_FIELD,
    )

    plan = job.assembly_plan or {}
    found: dict[str, str] = {}
    conflicted: set[str] = set()

    def take(media_id: object, sha: object) -> None:
        if not isinstance(media_id, str) or not media_id or not isinstance(sha, str) or not sha:
            return
        if found.setdefault(media_id, sha) != sha:
            conflicted.add(media_id)

    for row in plan.get(PHONE_SOURCES_FIELD) or []:
        if isinstance(row, dict) and isinstance(row.get("original"), dict):
            take(row.get("media_id"), row["original"].get("sha256"))
    for row in plan.get(PHONE_VISUALS_FIELD) or []:
        if isinstance(row, dict):
            take(row.get("media_id"), row.get("sha256"))
    binding = plan.get("creator_brief_binding")
    snapshot = binding.get("media_snapshot") if isinstance(binding, dict) else None
    for row in (snapshot.get("clip_assignments") if isinstance(snapshot, dict) else None) or []:
        contract = row.get("upload_contract") if isinstance(row, dict) else None
        proxy = contract.get("proxy") if isinstance(contract, dict) else None
        original = proxy.get("original") if isinstance(proxy, dict) else None
        if isinstance(original, dict):
            take(row.get("media_id"), original.get("sha256"))
    return {media_id: sha for media_id, sha in found.items() if media_id not in conflicted}


def _approved_generation_review(
    db: Any,
    thread: CreationThread,
    job: Job,
    variant: dict,
    execution: CreatorAgentExecution,
    default_text: str,
) -> tuple[str, list[dict[str, Any]]]:
    """The finished render's reply, plus what its speech cleanup cut and left (KRI-551).

    The cleanup note is read from the variant's own cut record, so it survives a lost brief
    binding (the requirement receipts then cannot say a retake was not cut).
    """
    text, receipts = _approved_generation_requirements_review(
        db, thread, job, variant, execution, default_text
    )
    contract = (job.assembly_plan or {}).get("speech_cleanup_contract")
    note = speech_cleanup_review_note(
        variant,
        text=text,
        # Consented cleanup never runs the retake detector; the older auto path does when
        # its lever is on, so only then must the reply not deny a restart cut.
        retake_cut_possible=contract != "required_v1" and bool(settings.retake_cut_enabled),
    )
    if not note:
        return text, receipts
    # A requirement list is multi-line: keep the note off its last bullet.
    separator = "\n\n" if "\n" in text else " "
    return f"{text}{separator}{note}", receipts


def _with_clip_sound_toggles(job: Job, variant: dict, facts: Any, *, brief_binding: Any) -> Any:
    """``facts`` with the on/off asks re-judged against the recipe pinned for ``variant``.

    An editor Save re-pins the recipe, so the receipt follows what the phone renders now;
    without a pinned recipe the worker's own record (already on the facts) stands.
    """
    from app.services.device_render import device_status  # noqa: PLC0415
    from app.services.phone_clip_sound_toggle import rendered_toggle_receipt  # noqa: PLC0415

    variant_id = variant.get("variant_id")
    try:
        recipe = device_status(job, str(variant_id)).request.recipe if variant_id else None
    except (AttributeError, KeyError, ValueError, TypeError):
        return facts
    receipt = rendered_toggle_receipt(
        job.assembly_plan or {}, variant, recipe, brief_binding=brief_binding
    )
    return dataclasses.replace(facts, clip_sound_toggles=receipt) if receipt is not None else facts


def _order_source_context_sync(db, *, brief, item_id, creator_id):
    from app.kria.order_replacement_sources import load_brief_order_source_context_sync

    return load_brief_order_source_context_sync(
        db, brief=brief, item_id=item_id, creator_id=creator_id
    )


def _approved_generation_requirements_review(
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
    if brief is None:
        return default_text, []
    # KRI-968: what the approved simpler version left out is said again on the finished
    # video, so the review never reads as if everything else was done.
    left_out = "\n".join(_left_out_lines(brief, _approved_draft_coverage(db, execution)))
    if not brief.live():
        return _with_left_out(default_text, left_out), []
    generation = str(variant.get("render_generation_id") or "") or None
    # An editor turn changed the cut those records describe: it keeps the exact id match.
    accepted = (
        {generation}
        if (execution.result or {}).get("editor_prep") is not None
        else _plan_record_generations(job, variant)
    )
    receipts = []
    montage: dict | None = None
    for record_key in ("unified_montage", NARRATED_ALIGNMENT_FIELD):
        record = (job.assembly_plan or {}).get(record_key) or {}
        record_generation = record.get("generation_id")
        if record_generation not in accepted or record.get("brief_version") != brief.version:
            continue
        if record_key == "unified_montage":
            montage = record
        for row in record.get("requirement_receipts") or []:
            try:
                receipt = RequirementReceipt.model_validate(row)
            except ValueError:
                continue
            if (
                receipt.brief_version == brief.version
                and receipt.generation_id == record_generation
            ):
                receipts.append(receipt)
    live = {req.id: req for req in brief.live()}
    if montage is not None:
        # KRI-546: a finished phone montage. Its plan record judged order, text and length
        # when it was laid out; what the record left unjudged, and the asks only the
        # finished edit can show (a held closing line, a repeated file), are judged here.
        facts = plan_facts_from_rendered_montage(
            variant, montage, fingerprints=_source_fingerprints(job)
        )
        # KRI-994: the creator's tapped skips and a "video clips only" ask are judged against
        # the finished timeline, whatever the plan record said when it was laid out.
        facts = dataclasses.replace(
            facts,
            exclusions=exclusion_facts((job.all_candidates or {}).get("creator_strategy"), brief),
        )
        finished = {f.requirement_id for f in facts.exclusions} | {
            req.id for req in live.values() if only_video_ask(req)
        }
        receipts = [
            receipt
            for receipt in receipts
            if is_judged(live.get(receipt.requirement_id), receipt)
            and not judged_at_render(live[receipt.requirement_id])
            and receipt.requirement_id not in finished
        ]
    else:
        # KRI-537: the variant's phone_beat_receipt and voiceover_bed_level are the render's
        # evidence for pop-in and mix asks; the text-lane facts alone cannot see them.
        facts = plan_facts_from_phone_variant(variant)
        # KRI-821: a mix receipt names only a control this variant (and app build) has.
        from app.routes.generative_jobs import original_audio_recovery  # noqa: PLC0415

        facts = dataclasses.replace(
            facts, original_audio_control=original_audio_recovery(job, variant)
        )
    # KRI-942: clip sound on/off asks are judged on the recipe the phone renders now.
    facts = _with_clip_sound_toggles(job, variant, facts, brief_binding=raw)
    if any(req.order_replacement is not None for req in live.values()):
        from app.kria.order_replacement_receipts import with_verified_order_execution

        facts = with_verified_order_execution(
            facts,
            brief=brief,
            strategy=(job.all_candidates or {}).get("creator_strategy"),
            execution=variant,
            source_context=_order_source_context_sync(
                db,
                brief=brief,
                item_id=job.content_plan_item_id,
                creator_id=thread.creator_id,
            ),
            creator_id=thread.creator_id,
        )
        receipts = [
            receipt
            for receipt in receipts
            if not (
                live.get(receipt.requirement_id)
                and live[receipt.requirement_id].order_replacement is not None
            )
        ]
    known = {receipt.requirement_id for receipt in receipts}
    receipts.extend(
        build_receipts(
            [req for req in brief.live() if req.id not in known], facts, include_unchecked=True
        )
    )
    order = {req_id: index for index, req_id in enumerate(live)}
    receipts.sort(key=lambda receipt: order.get(receipt.requirement_id, len(order)))
    # KRI-558: an ask no check could decide says what the finished video holds, never
    # "I can't verify this one automatically".
    receipts = [
        receipt.model_copy(
            update={"reason": describe_text_look(live[receipt.requirement_id], facts)}
        )
        if receipt.verification == "unchecked" and receipt.requirement_id in live
        else receipt
        for receipt in receipts
    ]
    receipts = [
        receipt.model_copy(
            update={
                "brief_version": brief.version,
                "generation_id": generation,
            }
        )
        for receipt in receipts
    ]
    return _with_left_out(reply_from_receipts(brief, receipts, summary=default_text), left_out), [
        receipt.model_dump(mode="json") for receipt in receipts
    ]


# KRI-520: Turkish copy for the Job failure sentences a render-failed event shows. The
# English table lives in `content_plan_build.JOB_FAILURE_MESSAGES`; a test pins that every
# code there has a Turkish line here, so the two cannot drift apart.
_JOB_FAILURE_MESSAGES_TR: dict[str, str] = {
    "creation_text_failed": (
        "Onayladığın yazıyı koruyarak metni hazırlayamadım. "
        "Taslağın ve çekimlerin kayıtlı. Bu videoyu yeniden oluşturmayı dene."
    ),
    "user_song_plan_declined": (
        "Çekimlerini şarkınla hizalayamadım. Şarkı yüksek sesle çalarken çekim yap ya da "
        "sözleri net söyleyerek eşlik et, sonra tekrar dene."
    ),
    "silent_audio_declined": (
        "Videonda hiç ses yok, bu yüzden altyazı ekleyecek ya da kesecek bir şey bulamadım. "
        "Tekrar çek ve mikrofonun kapalı olmadığından emin ol."
    ),
    "phone_plan_unsupported": (
        "Bu düzenleme iPhone'unun henüz oluşturamadığı bir şey içeriyor. Farklı bir klip ya da "
        "formatla yeni bir düzenleme başlat."
    ),
    "phone_capability_unavailable": (
        "Bu düzenleme için gereken bir iPhone oluşturma özelliği şu anda kapalı. Düzenlemen ve "
        "çekimlerin kayıtlı. Daha sonra tekrar dene ya da farklı bir stil iste."
    ),
    "ai_quota_exhausted": (
        "Kria'nın yapay zekâ hizmeti şimdilik kullanım sınırına ulaştı, bu yüzden videon "
        "oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Hemen tekrar denemek işe yaramaz; "
        "daha sonra tekrar dene."
    ),
    "creation_text_timeout": (
        "Bu kadar klip için ekrandaki yazıları hazırlamak yapay zekânın izin verdiği süreden "
        "uzun sürdü, bu yüzden videon oluşturulmadı. Düzenlemen ve çekimlerin kayıtlı. Tekrar "
        "dene; yine olursa daha az klip kullan ya da daha az yazı iste."
    ),
    "plan_ai_timeout": (
        "Yapay zekâ bu düzenlemeyi planlarken çok uzun sürdü, bu yüzden videon oluşturulmadı. "
        "Düzenlemen ve çekimlerin kayıtlı. Birazdan tekrar dene."
    ),
    "phone_plan_failed": (
        "Bu düzenleme planlanırken bir şey bozuldu, bu yüzden videon oluşturulmadı. "
        "Düzenlemen ve çekimlerin kayıtlı. Tekrar dene ya da bir değişiklik iste."
    ),
    "prepared_execution_stale": (
        "Onayladığın bir şey bu düzenleme oluşturulmadan önce değişti, bu yüzden videoyu "
        "yapmadım. Kria'dan düzenlemeyi yeniden hazırlamasını iste."
    ),
    "prepared_execution_unsupported": (
        "Bu düzenleme, onu oluşturan sürümden daha yeni bir Kria sürümüyle hazırlandı. "
        "Kria'dan yeniden hazırlamasını iste."
    ),
    "originals_not_uploaded": (
        "Kliplerinin yalnızca önizleme kopyaları yüklü (orijinaller iPhone'unda duruyor), bu "
        "yüzden bu düzenleme iPhone'unda yapılmalı. Düzenlemen kayıtlı. Projeyi klipler olan "
        "iPhone'da aç ve Kria'dan videoyu yeniden yapmasını iste."
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
        "Bu proje yalnızca sunucularımızda oluşturulabiliyor ve sunucuda oluşturma şu anda "
        "kapalı, bu yüzden bu sürüm yapılmadı. Bu kliplerle iPhone'unda yeni bir düzenleme "
        "başlatarak videoyu orada yap."
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


# KRI-673: the failed card's headline, sent with the event so the client never has to
# invent one ("This cut needs another pass" said nothing about what happened).
_FAILURE_TITLES: dict[str, tuple[str, str]] = {
    "creation_text_timeout": ("Planning took too long", "Planlama çok uzun sürdü"),
    "plan_ai_timeout": ("Planning took too long", "Planlama çok uzun sürdü"),
    "creation_text_failed": ("The text couldn't be added", "Yazılar eklenemedi"),
    "ai_quota_exhausted": ("Kria's AI is at its limit", "Kria'nın yapay zekâsı sınırda"),
}
_FAILURE_TITLE_DEVICE = (
    "Your iPhone couldn't finish the video",
    "iPhone'un videoyu tamamlayamadı",
)
_FAILURE_TITLE_DEFAULT = ("Your video wasn't made", "Videon oluşturulmadı")


def _failure_title(failure_code: str | None, *, device_failed: bool = False) -> str:
    en, tr = (
        _FAILURE_TITLE_DEVICE
        if device_failed
        else _FAILURE_TITLES.get(failure_code or "", _FAILURE_TITLE_DEFAULT)
    )
    return say(en=en, tr=tr)


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
    ``render_block_recovery`` of these receipts, or the phone Voiceover worker's own
    alignment question (KRI-592, with or without receipts), is rebuilt; anything else
    shows as stored.
    """
    if current_reply_language() != "tr":
        return message
    from app.kria.brief_checks import (  # noqa: PLC0415
        NARRATED_ALIGNMENT_RECOVERY,
        narrated_alignment_recovery,
        needs_creator_choice,
        render_block_recovery,
    )

    if message.strip() == NARRATED_ALIGNMENT_RECOVERY:
        return narrated_alignment_recovery()
    if not receipts:
        return message

    # The worker's own filter: a format limit (KRI-689) is never in the message, so keeping
    # it here would rebuild a different English text and leave the card English (KRI-778).
    failures = [row for row in receipts if needs_creator_choice(row)]
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
            if variant is not None and device_state != "failed":
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
                # KRI-756: the render settled ready: word the follow-up suggestion chips for it
                # (flag-checked, enqueue-only, never raises).
                from app.services.suggestion_serve import enqueue_for_thread  # noqa: PLC0415

                enqueue_for_thread(thread.id, trigger="render_ready", creator_id=thread.creator_id)
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
        deterministic = not device_failed and (
            failure_code in _DETERMINISTIC_JOB_FAILURE_CODES
            or failure_code in _NOT_RETRYABLE_NOW_JOB_FAILURE_CODES
        )
        # Typed creator-contract decline (additive; the failure code is unchanged):
        # missing evidence is a repair/retry; an unavailable capability is a refusal
        # that asks for a different request; conflicts and choices keep the existing
        # behaviour until the clarification gate wires real questions.
        device_decline = (
            _device_contract_decline(job, str(execution.target_variant_id or "") or None)
            if device_failed
            else None
        )
        export_not_intact = bool(
            device_decline and device_decline.get("field_path") == "export_verification"
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
        from app.kria.render_retry import retry_program_available  # noqa: PLC0415

        exact_retry_available = retry_program_available(job, execution)
        native_without_program = bool((job.assembly_plan or {}).get("native_device_only")) and (
            not device_failed and not deterministic and not exact_retry_available
        )
        if device_failed and exact_retry_available:
            recovery, retryable = "retry", True
        elif native_without_program:
            recovery, retryable = "ask_user", False
        if device_decline is not None and not export_not_intact:
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
        from app.tasks.content_plan_build import (  # noqa: PLC0415
            CREATOR_FACING_DETAIL_CODES,
            JOB_FAILURE_MESSAGES,
        )

        # KRI-673/KRI-765: every code with a sentence of its own says why the render stopped.
        cause_codes = {
            "phone_capability_unavailable",
            "creation_text_failed",
            *CREATOR_FACING_DETAIL_CODES,
            *JOB_FAILURE_MESSAGES,
        }
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
        # KRI-795: the question a refusal for an unresolved choice asks again, if any.
        reasked: dict[str, Any] | None = None
        # The refusal copy below already says the way forward (never added twice).
        says_way_forward = False
        if device_decline is not None:
            from app.services.device_render import has_accepted_artifact  # noqa: PLC0415

            last_good = has_accepted_artifact(job, str(execution.target_variant_id or "") or None)
            if export_not_intact:
                failure_content = (
                    say(
                        en=(
                            "The exported video didn't arrive intact, so it wasn't published. "
                            "Your approved edit is saved. Retry the same edit."
                        ),
                        tr=(
                            "Dışa aktarılan video eksiksiz ulaşmadığı için yayınlanmadı. "
                            "Onayladığın düzenleme kayıtlı. Aynı düzenlemeyi yeniden dene."
                        ),
                    )
                    + " "
                    + (
                        say(en=_LAST_GOOD_STAYS, tr="Son iyi sürümün hâlâ duruyor.")
                        if last_good
                        else say(en=_NOTHING_PUBLISHED, tr="Yeni bir video oluşturulmadı.")
                    )
                )
            else:
                reasked = _reasked_choice_question(
                    db,
                    thread.id,
                    device_decline,
                    job,
                    source_event_id=getattr(turn, "source_event_id", None),
                )
                failure_content = _device_refusal_copy(
                    device_decline, last_good=last_good, question=reasked
                )
        elif device_failed and exact_retry_available:
            failure_content = say(
                en=(
                    "Your iPhone couldn't finish the render. Your approved edit is still "
                    "saved. Retry the same edit on your iPhone after reviewing its approval."
                ),
                tr=(
                    "iPhone'un videoyu tamamlayamadı. Onayladığın düzenleme hâlâ kayıtlı: "
                    "iPhone'unda aynı düzenlemeyi yeniden denemeden önce onayını gözden geçir."
                ),
            )
        elif device_failed:
            failure_content = say(
                en="Your iPhone stopped this export. Your approved edit is saved. "
                "Check the device error before trying again.",
                tr="iPhone'un bu dışa aktarımı durdurdu. Onayladığın düzenleme kayıtlı. "
                "Yeniden denemeden önce cihazdaki hatayı kontrol et.",
            )
        elif native_without_program and failure_code in cause_codes:
            # No exact retry exists for this render, but the cause is known: name it, then
            # the real way forward (KRI-765: "creation_text_failed" read as a generic stop).
            failure_content = (
                f"{_job_failure_copy(failure_code, getattr(job, 'error_detail', None))} "
                + say(
                    en="To try again, ask for a new version and review its approval.",
                    tr="Tekrar denemek için yeni bir sürüm isteyip onayını gözden geçir.",
                )
            )
        elif native_without_program:
            failure_content = say(
                en=(
                    "The render stopped before a complete render plan was saved. "
                    "Your approved direction and footage are safe. "
                    "Ask for a new version and review its approval."
                ),
                tr=(
                    "Video, eksiksiz oluşturma planı kaydedilmeden durdu. "
                    "Onayladığın yön ve görüntüler güvende. "
                    "Yeni bir sürüm isteyip onayını gözden geçir."
                ),
            )
        elif recovery_message is not None:
            failure_content = _localized_block_recovery(recovery_message, recovery_receipts)
        elif (
            typed_decline is not None
            and typed_decline["decline_reason"] in _CHOICE_DECLINES
            and (
                failure_code == "phone_plan_unsupported"
                or _cloud_decline_needs_the_creator(failure_code, typed_decline["decline_reason"])
            )
        ):
            # KRI-795: a decision only the creator can make. The phone never saw this edit,
            # so the "can't render yet" table line blamed the wrong thing, and "tell me which
            # option" pointed at options nobody showed: name what is unresolved and ask the
            # thread's own question again when there is one.
            reasked = _reasked_choice_question(
                db,
                thread.id,
                typed_decline,
                job,
                source_event_id=getattr(turn, "source_event_id", None),
            )
            failure_content = _choice_refusal_copy(typed_decline, reasked)
            says_way_forward = True
        elif typed_decline is not None and (
            typed_decline["decline_reason"] == "capability_unavailable"
            or _cloud_decline_needs_the_creator(failure_code, typed_decline["decline_reason"])
        ):
            # The decline's own authored message plus the way forward. Only a TYPED decline
            # reaches here, so no raw exception text is ever shown.
            failure_content = _capability_refusal_copy(typed_decline)
        elif (
            typed_decline is not None
            and failure_code == "phone_plan_unsupported"
            and typed_decline["decline_reason"] == "evidence_missing"
        ):
            # KRI-701: the plan lacked something the creator confirmed; the iPhone can render
            # it, so the "can't render yet" table line would blame the wrong thing.
            failure_content = _phone_evidence_refusal_copy(typed_decline)
        elif deterministic or failure_code in cause_codes:
            # Retryable, but the generic "didn't finish" copy would hide WHY (KRI-286).
            # Creator-facing codes (KRI-466) show the job's own actionable detail.
            # KRI-673: every code with a table sentence says why; the generic line is
            # only for codes nothing has words for.
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
        worker_alternative = _worker_copy(
            typed_decline.get("alternative") if typed_decline is not None else None
        )
        if (
            typed_decline is not None
            and worker_alternative
            and failure_code in _DETERMINISTIC_JOB_FAILURE_CODES
            and typed_decline["decline_reason"] != "capability_unavailable"
            and recovery_message is None
            and not device_failed
            and not says_way_forward
            # A named missing text already asks its one question (KRI-701).
            and not _quoted_missing_text(typed_decline)
        ):
            # A deterministic phone decline keeps its copy and adds the way forward.
            failure_content = f"{failure_content} {worker_alternative}"
        failure_question = _blocked_title_question(recovery_receipts) or reasked
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
                "title": _failure_title(failure_code, device_failed=device_failed),
                **({"choice_question": failure_question} if failure_question else {}),
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
            successor = _expire_overdue_approval(approval_id)
        except Exception:  # noqa: BLE001 - one bad approval must not stop the sweep
            log.warning("kria_approval_expiry_failed", approval_id=str(approval_id), exc_info=True)
            continue
        if successor is not None:
            successors.append(successor)
    return successors


def _expire_overdue_approval(approval_id: uuid.UUID) -> str | None:
    """Expire one approval the sweep found overdue; return the successor turn to publish.

    None when it is gone, no longer pending, or carries a strategy media stash (see
    `_expire_pending_approvals`). It does not re-check `expires_at`: the sweep's query did.
    """
    with sync_session() as db, ExitStack() as language:
        ref = db.get(CreatorAgentApproval, approval_id)
        if ref is None:
            return None
        # Canonical order (subset): Session -> Turn -> Approval -> Execution -> Thread.
        session = db.execute(
            select(CreatorAgentSession)
            .where(CreatorAgentSession.id == ref.session_id)
            .with_for_update()
        ).scalar_one_or_none()
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == ref.turn_id).with_for_update()
        ).scalar_one_or_none()
        # populate_existing: `ref` was read before the lock, and an expiry or
        # decision committed in between (the lazy expiry in `submit_turn`, a deny)
        # must not look pending here, or it is expired and announced twice.
        approval = db.execute(
            select(CreatorAgentApproval)
            .where(CreatorAgentApproval.id == approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if approval is None or approval.status != "pending":
            # Let go before waiting on Thread: the `submit_turn` that just closed
            # it holds Thread and then needs Session FOR KEY SHARE for its new
            # turn's foreign key, so waiting here deadlocks that request.
            return None
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
            select(CreationThread).where(CreationThread.id == ref.thread_id).with_for_update()
        ).scalar_one_or_none()
        if (
            session is None
            or turn is None
            or approval is None
            or thread is None
            or approval.status != "pending"
        ):
            return None
        if isinstance(getattr(execution, "result", None), dict) and (
            "strategy_media_before" in execution.result
        ):
            return None
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
        return _promote_queued_successor_sync(thread.id)


def _release_stranded_successors() -> int:
    """Promote queued follow-ups left behind a turn that already finished.

    Completion, cancel, failure, deny and approval expiry each commit the finished turn and
    promote its queued follow-up in a second transaction. A process that dies between the
    two leaves the follow-up `queued` with nothing running, and every new message is then
    refused with `queued_successor_exists`. Found by TLC: `formal/kria_turns`.
    """
    running = aliased(CreatorAgentTurn)
    no_running_turn = ~exists().where(
        running.thread_id == CreatorAgentTurn.thread_id,
        running.status.in_(CREATOR_AGENT_RUNNING_TURN_STATUSES),
    )
    with sync_session() as db:
        thread_ids = list(
            db.execute(
                select(CreatorAgentTurn.thread_id)
                .where(CreatorAgentTurn.status == "queued", no_running_turn)
                .limit(50)
            ).scalars()
        )
    released = 0
    for thread_id in thread_ids:
        with sync_session() as db:
            successor = db.execute(
                select(CreatorAgentTurn)
                .where(CreatorAgentTurn.thread_id == thread_id, CreatorAgentTurn.status == "queued")
                .order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if successor is None:
                continue
            # Turn -> Thread, as every promotion locks. Under the Thread lock no submit or
            # promotion can start a turn, so the check below stays true until commit.
            db.execute(
                select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
            )
            if _turn_running_sync(db, thread_id):
                continue
            successor.status = "pending"
            db.commit()
            released += 1
            log.warning(
                "kria_stranded_successor_released",
                turn_id=str(successor.id),
                thread_id=str(thread_id),
            )
    return released


def _reconcile_kria_turns_body() -> dict[str, int]:
    # Before the pending sweep below, which then publishes the released turns.
    _release_stranded_successors()
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
    return {
        "published": len(identifiers) + len(approval_identifiers),
        "settled": _settle_dispatched_executions(dispatched_execution_ids),
    }


def _settle_dispatched_executions(execution_ids: list[uuid.UUID]) -> int:
    """Observe each dispatched receipt; publish the queued turns the settled ones release."""
    settled = 0
    successors: list[str] = []
    for execution_id in execution_ids:
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
    return settled


# A nudge still queued after this long is moot: the minute sweep has settled the Job by then.
OBSERVE_NUDGE_TTL_S = 120


@celery_app.task(
    name="tasks.observe_kria_job",
    soft_time_limit=30,
    time_limit=45,
    max_retries=0,
)
def observe_kria_job(job_id: str) -> dict[str, int]:
    """Settle the chat receipts waiting on one Job, right after that Job changed.

    The minute sweep used to be the only thing that posted a render's ready/failed card, so
    the creator waited up to a minute for it (café M2 job 50f67a06 failed at 13:05:12; its
    card came at 13:05:53). The render tasks, the phone's publish/failure reports and the
    approval dispatch nudge this task instead. It runs the sweep's own observer, which
    row-locks and re-checks each receipt, so a nudge racing the sweep posts one card; the
    sweep stays the fallback for a nudge that never arrives.
    """

    if not settings.kria_runtime_v2_enabled:
        return {"settled": 0}
    with sync_session() as db:
        execution_ids = list(
            db.execute(
                select(CreatorAgentExecution.id)
                .where(
                    CreatorAgentExecution.target_job_id == uuid.UUID(job_id),
                    CreatorAgentExecution.status == "dispatched",
                )
                .order_by(CreatorAgentExecution.dispatched_at, CreatorAgentExecution.id)
                .limit(50)
            ).scalars()
        )
    return {"settled": _settle_dispatched_executions(execution_ids)}


def nudge_kria_observer(job_id: object) -> None:
    """Ask `observe_kria_job` to settle this Job's chat receipts now. Never raises.

    Call it after the Job write commits. A lost nudge (broker down, worker killed) only
    means the minute sweep posts the card instead.
    """

    try:
        if not settings.kria_runtime_v2_enabled:
            return
        observe_kria_job.apply_async(
            args=[str(job_id)], queue="agent-control", expires=OBSERVE_NUDGE_TTL_S
        )
    except Exception:  # noqa: BLE001 - the sweep settles whatever a lost nudge misses
        log.warning("kria_observe_nudge_failed", job_id=str(job_id), exc_info=True)


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
        from app.models import CreatorPreparedExecution  # noqa: PLC0415

        candidate_draft_ids = [row.id for row in candidates]
        protected = set(
            db.execute(
                select(CreatorAgentApproval.draft_id)
                .where(
                    CreatorAgentApproval.draft_id.in_(candidate_draft_ids),
                    CreatorAgentApproval.status.in_({"pending", "approved", "consumed"}),
                )
                # KRI-722: a draft a live prepared execution was approved from (and that
                # its retries re-verify against) stays restorable.
                .union(
                    select(CreatorPreparedExecution.draft_id).where(
                        CreatorPreparedExecution.draft_id.in_(candidate_draft_ids),
                        CreatorPreparedExecution.invalidated_at.is_(None),
                    )
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
