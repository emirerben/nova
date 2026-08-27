"""Authenticated durable Main Creator Agent v1 routes.

The model can only propose an inert strategy. This route owns revision fences,
explicit confirmation, manifest revalidation, typed PlanItem mutations, and
dispatch through the existing render entry point.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents._schemas.creator_agent import (
    ApplySpeechCutCommand,
    AskUser,
    CreativeStrategy,
    CreatorCraftBundle,
    CreatorEditPlan,
    ProposeStrategy,
    ReviewDecision,
    SetLicensedSfxCommand,
    canonical_context_hash,
)
from app.agents._schemas.creator_policy import (
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
    MixedMediaTimingUnavailableError,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.limiter import limiter
from app.models import (
    ContentPlan,
    CreatorAgentEvent,
    CreatorAgentExecution,
    CreatorAgentSession,
    Job,
    Persona,
    PlanItem,
    PlanItemAsset,
    SoundEffect,
)
from app.routes.generative_jobs import (
    enqueue_editor_commit_render,
    prepare_editor_commit,
    validate_sound_effects_for_user,
    visual_block_variant_duration,
)
from app.schemas.edit_proposal import recognize_mixed_media_timing
from app.services.content_plan_persona import load_owned_plan_persona
from app.services.creator_autonomy import (
    build_auto_bundle,
    evaluate_auto_iteration,
    recover_auto_bundle,
)
from app.services.creator_craft import (
    CreatorCraftValidationError,
    build_core_craft_editor_commit,
    build_media_overlay_craft_editor_commit,
    craft_preview,
)
from app.services.creator_sessions import (
    ACTIVE_CREATOR_PHASES,
    append_event,
    compile_active_plan,
    creator_context,
    reconcile_render_state,
    resolve_item_creator_context,
    rollout_eligible,
    serialize_session,
)
from app.services.job_phases import mark_reattempt
from app.services.job_status import PLAN_ITEM_JOB_READY, PLAN_ITEM_JOB_TERMINAL

log = structlog.get_logger()
router = APIRouter()

# Planning turns can invoke a paid model call.  Keep this bounded independently
# of the read/poll and explicit-confirmation routes; the latter are cheap and
# must remain usable while a client is recovering from a retry storm.
CREATOR_AGENT_MUTATION_RATE_LIMIT = "12/minute"
_MANAGE_CRAFT_SESSION_STATE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "manage_creator_craft_session_state", default=True
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartBody(_StrictBody):
    message: str = Field(min_length=1, max_length=2000)
    client_event_id: str = Field(min_length=1, max_length=128)

    @field_validator("message")
    @classmethod
    def _nonblank_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped


class TurnBody(StartBody):
    session_id: uuid.UUID
    expected_revision: int = Field(ge=0)


class ConfirmBody(_StrictBody):
    session_id: uuid.UUID
    expected_revision: int = Field(ge=0)
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(min_length=64, max_length=64)
    client_event_id: str = Field(min_length=1, max_length=128)


class CancelBody(_StrictBody):
    session_id: uuid.UUID
    expected_revision: int = Field(ge=0)


class AutoIterationBody(_StrictBody):
    session_id: uuid.UUID
    expected_revision: int = Field(ge=0)
    opt_in: bool = True
    client_event_id: str = Field(min_length=1, max_length=128)


class CreatorCraftResponse(BaseModel):
    status: str
    receipt_id: str
    generation: str | None = None
    preview: dict[str, Any] = Field(default_factory=dict)


class CreatorSessionResponse(BaseModel):
    id: str
    status: str
    revision: int
    render_attempts: int
    max_render_attempts: int
    can_render: bool
    pending_plan: dict | None
    current_job_id: str | None
    last_review: dict | None
    events: list[dict]
    auto_iteration: dict | None = None
    created_at: str
    updated_at: str


def _require_feature(user_id: uuid.UUID, *, execution: bool = False) -> None:
    if not rollout_eligible(user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Creator agent unavailable"
        )
    if execution and not settings.main_creator_agent_execution_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Creator agent rendering is not enabled yet",
        )


async def _owned_context(
    db: AsyncSession,
    item_id: str,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> tuple[PlanItem, ContentPlan, Persona]:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad id") from exc
    item_ref = await db.get(PlanItem, iid)
    if item_ref is None:
        raise HTTPException(status_code=404, detail="Plan item not found")
    plan = await db.get(
        ContentPlan,
        item_ref.content_plan_id,
        populate_existing=for_update,
        with_for_update=for_update,
    )
    if plan is None or plan.user_id != user_id:
        raise HTTPException(status_code=404, detail="Plan item not found")
    persona = await load_owned_plan_persona(db, plan, for_update=for_update)
    item = await db.get(
        PlanItem,
        iid,
        populate_existing=for_update,
        with_for_update=for_update,
    )
    if item is None or item.content_plan_id != plan.id:
        raise HTTPException(status_code=404, detail="Plan item not found")
    return item, plan, persona


def _session_stmt(session_id: uuid.UUID, user_id: uuid.UUID, item_id: uuid.UUID):
    return (
        select(CreatorAgentSession)
        .where(
            CreatorAgentSession.id == session_id,
            CreatorAgentSession.creator_id == user_id,
            CreatorAgentSession.plan_item_id == item_id,
        )
        .options(selectinload(CreatorAgentSession.events))
    )


async def _load_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> CreatorAgentSession:
    stmt = _session_stmt(session_id, user_id, item_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Creator session not found")
    return session


async def _latest_session(
    db: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID, *, active_only: bool = False
) -> CreatorAgentSession | None:
    stmt = (
        select(CreatorAgentSession)
        .where(
            CreatorAgentSession.creator_id == user_id,
            CreatorAgentSession.plan_item_id == item_id,
        )
        .order_by(CreatorAgentSession.updated_at.desc(), CreatorAgentSession.created_at.desc())
        .limit(1)
    )
    if active_only:
        stmt = stmt.where(CreatorAgentSession.status.in_(ACTIVE_CREATOR_PHASES))
    return (await db.execute(stmt)).scalar_one_or_none()


async def _session_for_start_event(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    client_event_id: str,
) -> CreatorAgentSession | None:
    """Find a prior start receipt, including sessions that are now terminal.

    The event id is only meaningful inside the creator/item scope.  Keep the
    query bounded and filter on the indexed event identity before loading the
    session's events; the message comparison remains in Python because the
    payload is JSONB and the persisted event is the authoritative receipt.
    """

    stmt = (
        select(CreatorAgentSession)
        .join(CreatorAgentEvent, CreatorAgentEvent.session_id == CreatorAgentSession.id)
        .where(
            CreatorAgentSession.creator_id == user_id,
            CreatorAgentSession.plan_item_id == item_id,
            CreatorAgentEvent.client_event_id == client_event_id,
            CreatorAgentEvent.event_type == "user_message",
            CreatorAgentEvent.role == "user",
            # A start receipt is the first user event in a session.  Turn
            # events use the same event type, so this fence prevents a turn's
            # id from being accepted as a new-session idempotency key.
            CreatorAgentEvent.sequence == 0,
        )
        .options(selectinload(CreatorAgentSession.events))
        .order_by(CreatorAgentEvent.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _response(db: AsyncSession, session: CreatorAgentSession) -> CreatorSessionResponse:
    await db.commit()
    loaded = await _load_session(db, session.id, session.creator_id, session.plan_item_id)
    return CreatorSessionResponse.model_validate(serialize_session(loaded))


def _conversation(events: list[CreatorAgentEvent]) -> list[dict[str, str]]:
    return [
        {"role": event.role, "content": str((event.payload or {}).get("message") or "")[:1000]}
        for event in sorted(events, key=lambda value: value.sequence)[-20:]
        if (event.payload or {}).get("message")
    ]


def _confirmed_creator_request(events: list[CreatorAgentEvent], current_message: str) -> str:
    """Preserve bounded creator-authored intent across clarification turns."""

    messages = [
        str((event.payload or {}).get("message") or "").strip()
        for event in sorted(events, key=lambda value: value.sequence)
        if event.role == "user" and str((event.payload or {}).get("message") or "").strip()
    ]
    if current_message.strip() and (not messages or messages[-1] != current_message.strip()):
        messages.append(current_message.strip())
    return "\n".join(messages)[:1000]


def _fallback_strategy(manifest: Any, *, user_message: str = "") -> CreativeStrategy:
    current_format = manifest.edit_format
    current_available = manifest.capabilities.get(f"edit_format:{current_format}")
    safe_format = current_format if current_available and current_available.available else "montage"
    return CreativeStrategy(
        direction="fast_montage" if manifest.render_program == "guided" else "native",
        edit_format=safe_format,
        audio_strategy="licensed_music",
        pacing="balanced",
        render_program=manifest.render_program,
        mixed_media_timing=recognize_mixed_media_timing(user_message),
        selected_media_ids=(
            []
            if manifest.render_program == "guided"
            else [
                media.media_id
                for media in manifest.media
                if not media.media_id.startswith("asset-")
            ][:MAX_MAIN_CREATOR_SELECTED_MEDIA]
        ),
        rationale=(
            "Build a clear opening, keep only the strongest moments, "
            "and preserve a natural short-form rhythm."
        ),
    )


def _strict_creator_format(edit_format: str) -> bool:
    """Formats that must fail visibly instead of falling back to montage."""

    return edit_format in {"day_vlog", "single_hero"}


def _auto_iteration_already_finalized(session: CreatorAgentSession) -> bool:
    marker = session.last_review if isinstance(session.last_review, dict) else {}
    auto_marker = (
        marker.get("auto_iteration") if isinstance(marker.get("auto_iteration"), dict) else {}
    )
    return int(session.automatic_revision_count or 0) >= 1 or auto_marker.get("status") in {
        "queued",
        "complete",
    }


def _reset_render_target(session: CreatorAgentSession) -> None:
    """Fence a new creative plan from every prior render identity."""

    session.active_plan = None
    session.target_job_id = None
    session.target_variant_id = None
    session.target_generation_id = None
    session.last_review = None


def _job_matches_guided_attempt(job: Job | None, attempt_id: str | None) -> bool:
    if job is None or not attempt_id:
        return False
    assembly = job.assembly_plan or {}
    for key in ("guided_edit", "creator_guided_fallback"):
        snapshot = assembly.get(key)
        if isinstance(snapshot, dict) and snapshot.get("generation_attempt_id") == attempt_id:
            return True
    return False


async def _run_planning_turn(
    db: AsyncSession,
    *,
    item_id: str,
    user: Any,
    session_id: uuid.UUID,
    expected_revision: int,
    user_message: str,
) -> CreatorSessionResponse:
    item, _plan, persona = await _owned_context(db, item_id, user.id)
    session = await _load_session(db, session_id, user.id, item.id)
    manifest, media_context = await resolve_item_creator_context(db, item, persona=persona)
    if not manifest.capabilities["dispatch_render"].available:
        locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
        if locked.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Creator session changed")
        locked.status = "briefing"
        await append_event(
            db,
            locked,
            event_type="assistant_question",
            role="assistant",
            payload={
                "message": "Add at least one clip first, then I can design the edit around it."
            },
        )
        return await _response(db, locked)

    creator_summary, item_summary = creator_context(persona, item)
    creator_request = _confirmed_creator_request(session.events, user_message)
    agent_input = MainCreatorInput(
        user_message=user_message,
        creator_context=creator_summary,
        item_context=item_summary,
        media_context=media_context,
        conversation=_conversation(session.events),
        capability_manifest=manifest,
    )
    action: AskUser | ProposeStrategy | ReviewDecision
    try:
        output = await asyncio.to_thread(
            MainCreatorAgent(default_client()).run,
            agent_input,
            ctx=RunContext(
                creator_agent_session_id=str(session.id),
                request_id=str(expected_revision),
            ),
        )
        action = output.action
    except TerminalError as exc:
        log.warning(
            "main_creator.planning_fallback", session_id=str(session.id), error=str(exc)[:300]
        )
        action = ProposeStrategy(
            kind="propose_strategy",
            strategy=_fallback_strategy(manifest, user_message=creator_request),
            summary="A focused, fast-moving edit built from your strongest footage.",
        )

    locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
    if locked.revision != expected_revision or locked.status not in {"planning", "revising"}:
        raise HTTPException(status_code=409, detail="Creator session changed while planning")
    locked.agent_call_count += 1
    if locked.agent_call_count > locked.agent_call_budget:
        locked.status = "failed"
        locked.last_error = {"code": "agent_budget_exhausted"}
        await append_event(
            db,
            locked,
            event_type="assistant_error",
            payload={"message": "This edit needs a fresh creator session."},
        )
        return await _response(db, locked)

    if isinstance(action, AskUser) and locked.question_count < locked.question_budget:
        locked.status = "briefing"
        locked.question_count += 1
        await append_event(
            db,
            locked,
            event_type="assistant_question",
            payload={
                "message": action.question,
                "reason_code": action.reason_code,
                "options": action.options,
            },
        )
    elif isinstance(action, ReviewDecision):
        if action.decision == "approve":
            locked.status = "completed"
        else:
            locked.status = "briefing"
        await append_event(
            db,
            locked,
            event_type="assistant_review",
            payload={
                "message": action.summary,
                "decision": action.decision,
                "issues": action.issues,
            },
        )
    else:
        strategy = (
            action.strategy
            if isinstance(action, ProposeStrategy)
            else _fallback_strategy(manifest, user_message=creator_request)
        )
        summary = (
            action.summary
            if isinstance(action, ProposeStrategy)
            else "A focused edit from your strongest footage."
        )
        try:
            locked.active_plan = compile_active_plan(
                locked,
                manifest=manifest,
                strategy=strategy,
                summary=summary,
                creator_request=creator_request,
            )
        except ValueError as exc:
            log.warning(
                "main_creator.unsafe_strategy_dropped",
                session_id=str(locked.id),
                error=str(exc)[:300],
            )
            if isinstance(exc, MixedMediaTimingUnavailableError):
                locked.status = "failed"
                locked.last_error = {
                    "code": "mixed_media_timing_unavailable",
                    "message": str(exc)[:300],
                }
                await append_event(
                    db,
                    locked,
                    event_type="assistant_error",
                    payload={
                        "message": (
                            "Mixed photo and video timing is temporarily unavailable. "
                            "No fallback edit was rendered."
                        ),
                        "code": "mixed_media_timing_unavailable",
                    },
                )
                return await _response(db, locked)
            if _strict_creator_format(strategy.edit_format):
                locked.status = "failed"
                locked.last_error = {
                    "code": "edit_format_unavailable",
                    "edit_format": strategy.edit_format,
                    "message": str(exc)[:300],
                }
                await append_event(
                    db,
                    locked,
                    event_type="assistant_error",
                    payload={
                        "message": (
                            f"{strategy.edit_format} is not available for this Creator rollout. "
                            "No fallback edit was rendered."
                        ),
                        "code": "edit_format_unavailable",
                        "edit_format": strategy.edit_format,
                    },
                )
                return await _response(db, locked)
            strategy = _fallback_strategy(manifest, user_message=creator_request)
            locked.active_plan = compile_active_plan(
                locked,
                manifest=manifest,
                strategy=strategy,
                summary="A safe, focused edit from your strongest footage.",
                creator_request=creator_request,
            )
        locked.manifest_hash = manifest.manifest_hash
        locked.status = "awaiting_confirmation"
        await append_event(
            db,
            locked,
            event_type="assistant_strategy",
            payload={
                "message": locked.active_plan["summary"],
                "plan_hash": locked.active_plan["plan_hash"],
            },
        )
    return await _response(db, locked)


@router.get("/{item_id}/creator-agent/session", response_model=CreatorSessionResponse | None)
async def get_creator_session(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse | None:
    _require_feature(user.id)
    # Reconciliation may expire an exact guided proposal, so acquire the
    # canonical Plan -> Persona -> PlanItem lock order before the session row.
    item, _plan, _persona = await _owned_context(db, item_id, user.id, for_update=True)
    session = await _latest_session(db, user.id, item.id)
    if session is None:
        return None
    # Reconciliation mutates the state machine and appends an event. Serialize
    # concurrent pollers on the session row so MAX(sequence)+1 remains unique.
    session = await _load_session(db, session.id, user.id, item.id, for_update=True)
    if await reconcile_render_state(db, session):
        return await _response(db, session)
    return CreatorSessionResponse.model_validate(serialize_session(session))


@router.post("/{item_id}/creator-agent/session", response_model=CreatorSessionResponse)
@limiter.limit(CREATOR_AGENT_MUTATION_RATE_LIMIT)
async def start_creator_session(
    request: Request,
    item_id: str,
    body: StartBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse:
    _ = request
    _require_feature(user.id)
    item, plan, _persona = await _owned_context(db, item_id, user.id)

    # Idempotency is a receipt of the original start event, not a property of
    # the currently-active state machine row.  Check terminal sessions before
    # selecting/creating an active row so a retry after completion cannot
    # accidentally start a second model conversation.
    prior = await _session_for_start_event(
        db,
        user_id=user.id,
        item_id=item.id,
        client_event_id=body.client_event_id,
    )
    if prior is not None:
        session = await _load_session(db, prior.id, user.id, item.id, for_update=True)
        duplicate_message = str(
            next(
                (
                    (event.payload or {}).get("message")
                    for event in session.events
                    if event.client_event_id == body.client_event_id
                ),
                "",
            )
        )
        if duplicate_message != body.message.strip():
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        return await _response(db, session)

    session = await _latest_session(db, user.id, item.id, active_only=True)
    if session is None:
        session = CreatorAgentSession(
            creator_id=user.id,
            plan_item_id=item.id,
            status="briefing",
            ownership_epoch=int(plan.ownership_epoch or 0),
            max_render_attempts=2,
            iteration_budget=2,
            events=[],
        )
        db.add(session)
        try:
            await db.flush()
        except IntegrityError:
            # The partial unique index is the final authority. A simultaneous
            # start may win after our read; reload that durable active session.
            await db.rollback()
            item, _plan, _persona = await _owned_context(db, item_id, user.id)
            session = await _latest_session(db, user.id, item.id, active_only=True)
            if session is None:
                raise
    # Existing sessions must be locked before the duplicate check and event
    # append. Otherwise parallel starts can both mint MAX(sequence)+1.
    session = await _load_session(db, session.id, user.id, item.id, for_update=True)
    duplicate = (
        await db.execute(
            select(CreatorAgentEvent).where(
                CreatorAgentEvent.session_id == session.id,
                CreatorAgentEvent.client_event_id == body.client_event_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        if str((duplicate.payload or {}).get("message") or "") != body.message.strip():
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        return await _response(db, session)
    if session.status not in {"briefing", "awaiting_confirmation", "awaiting_feedback"}:
        raise HTTPException(status_code=409, detail="Creator session is busy")
    session.status = "planning" if session.status != "awaiting_feedback" else "revising"
    _reset_render_target(session)
    await append_event(
        db,
        session,
        event_type="user_message",
        role="user",
        payload={"message": body.message.strip()},
        client_event_id=body.client_event_id,
    )
    expected_revision = session.revision
    await db.commit()
    return await _run_planning_turn(
        db,
        item_id=item_id,
        user=user,
        session_id=session.id,
        expected_revision=expected_revision,
        user_message=body.message.strip(),
    )


@router.post("/{item_id}/creator-agent/turn", response_model=CreatorSessionResponse)
@limiter.limit(CREATOR_AGENT_MUTATION_RATE_LIMIT)
async def creator_session_turn(
    request: Request,
    item_id: str,
    body: TurnBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse:
    _ = request
    _require_feature(user.id)
    item, _plan, _persona = await _owned_context(db, item_id, user.id)
    session = await _load_session(db, body.session_id, user.id, item.id, for_update=True)
    duplicate = (
        await db.execute(
            select(CreatorAgentEvent).where(
                CreatorAgentEvent.session_id == session.id,
                CreatorAgentEvent.client_event_id == body.client_event_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        if str((duplicate.payload or {}).get("message") or "") != body.message.strip():
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        return await _response(db, session)
    if session.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creator session changed")
    if session.status not in {"briefing", "awaiting_confirmation", "awaiting_feedback"}:
        raise HTTPException(status_code=409, detail="Creator session is not accepting feedback")
    session.status = "revising" if session.render_attempts else "planning"
    _reset_render_target(session)
    await append_event(
        db,
        session,
        event_type="user_message",
        role="user",
        payload={"message": body.message.strip()},
        client_event_id=body.client_event_id,
    )
    expected_revision = session.revision
    await db.commit()
    return await _run_planning_turn(
        db,
        item_id=item_id,
        user=user,
        session_id=session.id,
        expected_revision=expected_revision,
        user_message=body.message.strip(),
    )


def _apply_plan_intent(item: PlanItem, plan: CreatorEditPlan) -> None:
    for command in plan.commands:
        if command.command == "set_item_intent":
            item.edit_format = command.edit_format
    strategy = plan.strategy
    audio = getattr(strategy, "audio_strategy", None)
    if audio == "original_audio":
        item.audio_mode = "original"
    elif audio == "voiceover":
        if not item.voiceover_gcs_path:
            raise HTTPException(
                status_code=409, detail="Record a voiceover before confirming this plan"
            )
        item.audio_mode = "voiceover"
    elif audio == "licensed_music":
        item.audio_mode = "kria"
    caption = getattr(strategy, "caption_style", None)
    # Translate the creative vocabulary into the renderer's existing typed
    # sentence/word contract. "auto" preserves the creator's current choice;
    # "none" clears a stale caption preference but does not bypass an
    # archetype whose renderer requires captions.
    caption_style = {
        "clean": "sentence",
        "editorial": "sentence",
        "kinetic": "word",
        "karaoke": "word",
    }.get(caption)
    if caption_style:
        item.voiceover_caption_style = caption_style
    elif caption == "none":
        item.voiceover_caption_style = None
    item.user_edited = True


def _seed_guided_specialist_brief(
    item: PlanItem,
    plan: CreatorEditPlan,
    *,
    summary: str,
    creator_request: str = "",
) -> None:
    """Delegate the confirmed strategy through the existing guided planner.

    The Main Creator never authors story-beat storage itself. It converts its
    high-level decision into the guided planner's typed brief; that specialist
    then analyzes exact owned media and produces the approved renderer input.
    """

    from app.schemas.edit_proposal import ProposalBrief, parse_edit_proposal  # noqa: PLC0415
    from app.services.edit_proposals import (  # noqa: PLC0415
        mark_edit_proposal_stale,
        save_edit_conversation_turn,
    )

    current = parse_edit_proposal(item.edit_proposal)
    if current is not None:
        mark_edit_proposal_stale(item)
        current = parse_edit_proposal(item.edit_proposal)
    expected_version = current.proposal_version if current else 0
    direction = plan.strategy.direction
    if direction not in {"guided_story", "fast_montage", "text_explainer"}:
        direction = "guided_story"
    story_goal = "; ".join(plan.strategy.story_structure)
    goal = (story_goal or plan.strategy.rationale or summary)[:500]
    save_edit_conversation_turn(
        item,
        expected_version=expected_version,
        brief=ProposalBrief(
            direction=direction,
            goal=goal,
            pace=plan.strategy.pacing,
            duration_s=24,
            creator_request=creator_request,
            mixed_media_timing=plan.strategy.mixed_media_timing,
            output_orientation=(
                "portrait" if plan.strategy.mixed_media_timing is not None else None
            ),
        ),
        user_message=creator_request or "Use the confirmed Main Creator direction.",
        agent_reply=summary or plan.strategy.rationale or "Build this direction.",
        suggestions=[],
        ready_to_plan=True,
    )


def _confirmed_edit_plan(active: dict[str, Any]) -> CreatorEditPlan:
    raw_edit_plan = active.get("edit_plan")
    if not isinstance(raw_edit_plan, dict):
        raise HTTPException(status_code=409, detail="Creator plan changed")
    try:
        return CreatorEditPlan.model_validate(raw_edit_plan)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Creator plan changed") from exc


async def _concurrent_render_response(
    db: AsyncSession,
    *,
    session: CreatorAgentSession,
    item: PlanItem,
    user_id: uuid.UUID,
    receipt: CreatorAgentExecution,
) -> CreatorSessionResponse:
    conflicted = await _load_session(db, session.id, user_id, item.id, for_update=True)
    conflicted.status = "briefing"
    conflicted.render_attempts = max(0, conflicted.render_attempts - 1)
    conflicted.iteration_count = conflicted.render_attempts
    stale_receipt = await db.get(CreatorAgentExecution, receipt.id, with_for_update=True)
    if stale_receipt:
        stale_receipt.status = "stale"
        stale_receipt.error = {"code": "concurrent_render"}
        stale_receipt.completed_at = datetime.now(UTC)
    await append_event(
        db,
        conflicted,
        event_type="assistant_error",
        payload={
            "message": (
                "Another render started first. Your direction is saved; "
                "ask me to recheck it when that render finishes."
            )
        },
    )
    return await _response(db, conflicted)


@router.post("/{item_id}/creator-agent/confirm", response_model=CreatorSessionResponse)
async def confirm_creator_plan(
    item_id: str,
    body: ConfirmBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse:
    _require_feature(user.id, execution=True)
    item, plan_row, persona = await _owned_context(db, item_id, user.id, for_update=True)
    session = await _load_session(db, body.session_id, user.id, item.id, for_update=True)
    # Keep identity/ownership scalars local.  The guided auto-design helper
    # commits its own proposal work, and the rollback below expires every ORM
    # instance in this AsyncSession; reading those instances afterward would
    # trigger an implicit async refresh (MissingGreenlet).
    user_id = user.id
    plan_item_id = item.id
    creator_session_id = session.id
    creator_ownership_epoch = getattr(session, "ownership_epoch", None)
    request_digest = canonical_context_hash(body.model_dump(mode="json"))
    receipt = (
        await db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.session_id == session.id,
                CreatorAgentExecution.idempotency_key == body.client_event_id,
            )
        )
    ).scalar_one_or_none()
    active = session.active_plan or {}
    resuming = receipt is not None
    if receipt is not None:
        if receipt.request_digest != request_digest:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        if receipt.status != "running":
            return await _response(db, session)
        if session.status not in {"executing", "rendering"}:
            raise HTTPException(status_code=409, detail="Creator execution is not resumable")
        edit_plan = _confirmed_edit_plan(active)
    else:
        if session.revision != body.expected_revision or session.status != "awaiting_confirmation":
            raise HTTPException(status_code=409, detail="Creator plan changed")
        if active.get("version") != body.plan_version or active.get("plan_hash") != body.plan_hash:
            raise HTTPException(status_code=409, detail="Creator plan changed")
        edit_plan = _confirmed_edit_plan(active)
        if session.ownership_epoch != int(plan_row.ownership_epoch or 0):
            raise HTTPException(status_code=409, detail="Creator ownership changed")
        if session.render_attempts >= session.max_render_attempts:
            raise HTTPException(status_code=409, detail="This session has used its render attempts")
        if item.current_job_id:
            current_job = await db.get(Job, item.current_job_id, with_for_update=True)
            if current_job is not None and current_job.status not in PLAN_ITEM_JOB_TERMINAL:
                raise HTTPException(
                    status_code=409, detail="Wait for the current render before confirming"
                )
        manifest, _media_context = await resolve_item_creator_context(db, item, persona=persona)
        if manifest.manifest_hash != session.manifest_hash:
            raise HTTPException(
                status_code=409, detail="Footage or capabilities changed; review the plan again"
            )
        receipt = CreatorAgentExecution(
            session_id=session.id,
            idempotency_key=body.client_event_id,
            request_digest=request_digest,
            expected_revision=body.expected_revision,
            expected_manifest_hash=manifest.manifest_hash,
            status="running",
        )
        db.add(receipt)
        from app.services.speech_cleanup import (  # noqa: PLC0415
            cleanup_inputs,
            reconcile_item_policy_change,
        )

        previous_speech_inputs = cleanup_inputs(item)
        _apply_plan_intent(item, edit_plan)
        reconcile_item_policy_change(item, previous_speech_inputs)
        if edit_plan.strategy.render_program == "guided":
            _seed_guided_specialist_brief(
                item,
                edit_plan,
                summary=str(active.get("summary") or ""),
                creator_request=str(active.get("creator_request") or "")[:1000],
            )
            # Mint the immutable guided execution identity before publishing
            # any background work. A process crash after enqueue can then
            # resume against the exact proposal attempt instead of a mutable
            # proposal_version that advances during draft + approval.
            session.active_plan = {
                **active,
                "guided_generation_attempt_id": str(uuid.uuid4()),
            }
        session.status = "executing"
        session.render_attempts += 1
        session.iteration_count = session.render_attempts
        await append_event(
            db,
            session,
            event_type="user_confirmation",
            role="user",
            payload={"message": "Render this direction", "plan_hash": body.plan_hash},
            client_event_id=body.client_event_id,
        )
        await db.commit()

    assert receipt is not None  # narrowed after new-or-resume handling
    receipt_id = receipt.id

    # Reuse the product's only PlanItem→Job dispatch boundary. Guided plans use
    # the existing auto-design path so the strict guided renderer still receives
    # an approved, media-pinned proposal; native plans dispatch directly.
    guided = edit_plan.strategy.render_program == "guided"
    job_id: uuid.UUID | None = None
    guided_proposal_version: int | None = None
    raw_guided_attempt = (session.active_plan or {}).get("guided_generation_attempt_id")
    guided_generation_attempt_id = (
        str(raw_guided_attempt)
        if guided and isinstance(raw_guided_attempt, str) and raw_guided_attempt
        else None
    )
    if resuming and item.current_job_id:
        candidate = await db.get(Job, item.current_job_id)
        expected_strategy = edit_plan.strategy.model_dump(mode="json", exclude_none=True)
        candidate_strategy = (
            (candidate.all_candidates or {}).get("creator_strategy") if candidate else None
        )
        created_after_receipt = bool(
            candidate
            and candidate.created_at
            and receipt.created_at
            and candidate.created_at >= receipt.created_at
        )
        exact_owner = bool(
            candidate
            and candidate.user_id == user_id
            and candidate.content_plan_item_id == plan_item_id
            and candidate.content_plan_ownership_epoch == creator_ownership_epoch
        )
        expected_guided_attempt = (session.active_plan or {}).get("guided_generation_attempt_id")
        candidate_guided = (candidate.assembly_plan or {}).get("guided_edit") if candidate else None
        guided_matches = guided and _job_matches_guided_attempt(candidate, expected_guided_attempt)
        if (
            created_after_receipt
            and exact_owner
            and (guided_matches or (not guided and candidate_strategy == expected_strategy))
        ):
            job_id = candidate.id
            if guided:
                guided_generation_attempt_id = str(expected_guided_attempt)
                guided_proposal_version = (
                    int(candidate_guided.get("proposal_version") or 0) or None
                    if isinstance(candidate_guided, dict)
                    else None
                )
    try:
        if job_id is not None:
            pass
        elif guided:
            if not settings.guided_auto_design_enabled:
                raise RuntimeError("guided creator execution requires guided auto design")
            from app.routes.plan_items import _maybe_auto_design_generate  # noqa: PLC0415

            live_item, live_plan, _ = await _owned_context(db, item_id, user_id)
            if live_item.current_job_id:
                live_job = await db.get(Job, live_item.current_job_id)
                if live_job is not None and live_job.status not in PLAN_ITEM_JOB_TERMINAL:
                    return await _concurrent_render_response(
                        db,
                        session=session,
                        item=item,
                        user_id=user_id,
                        receipt=receipt,
                    )
            if guided_generation_attempt_id is None:
                raise RuntimeError("guided creator execution has no generation identity")
            result = await _maybe_auto_design_generate(
                item_id,
                live_item,
                live_plan,
                user,
                db,
                generation_attempt_id=guided_generation_attempt_id,
            )
            if result is None:
                raise RuntimeError("guided auto design was not applicable")
            await db.rollback()
            # Rehydrate all rows touched after the rollback.  In particular,
            # session/receipt are needed for idempotent job matching and the
            # success/error state transition below.
            refreshed_item, _plan, _persona = await _owned_context(db, item_id, user_id)
            session = await _load_session(
                db, creator_session_id, user_id, plan_item_id, for_update=True
            )
            refreshed_receipt = await db.get(
                CreatorAgentExecution, receipt_id, with_for_update=True
            )
            if refreshed_receipt is None:
                raise RuntimeError("creator execution receipt disappeared during execution")
            receipt = refreshed_receipt
            proposal_state = (
                refreshed_item.edit_proposal
                if isinstance(refreshed_item.edit_proposal, dict)
                else {}
            )
            raw_attempt_id = proposal_state.get("generation_attempt_id")
            if raw_attempt_id != guided_generation_attempt_id:
                raise RuntimeError("guided proposal identity changed during execution")
            guided_proposal_version = int(proposal_state.get("proposal_version") or 0) or None
            if guided_proposal_version is None:
                raise RuntimeError("guided proposal reservation was not persisted")
            if refreshed_item.current_job_id:
                candidate = await db.get(Job, refreshed_item.current_job_id)
                exact_guided_job = bool(
                    candidate
                    and candidate.created_at
                    and receipt.created_at
                    and candidate.created_at >= receipt.created_at
                    and candidate.user_id == user_id
                    and candidate.content_plan_item_id == plan_item_id
                    and candidate.content_plan_ownership_epoch == creator_ownership_epoch
                    and _job_matches_guided_attempt(candidate, guided_generation_attempt_id)
                )
                if exact_guided_job:
                    job_id = candidate.id
                elif candidate and candidate.status not in PLAN_ITEM_JOB_TERMINAL:
                    return await _concurrent_render_response(
                        db,
                        session=session,
                        item=refreshed_item,
                        user_id=user_id,
                        receipt=receipt,
                    )
        else:
            from app.tasks.content_plan_build import dispatch_item_render_for  # noqa: PLC0415

            outcome = await asyncio.to_thread(
                dispatch_item_render_for,
                item_id,
                int(plan_row.ownership_epoch or 0),
                creator_strategy=edit_plan.strategy.model_dump(mode="json", exclude_none=True),
            )
            if outcome.outcome == "already_active":
                return await _concurrent_render_response(
                    db,
                    session=session,
                    item=item,
                    user_id=user_id,
                    receipt=receipt,
                )
            if outcome.outcome != "dispatched":
                raise RuntimeError(f"render dispatch failed: {outcome.outcome}")
            job_id = uuid.UUID(outcome.job_id) if outcome.job_id else None
    except Exception as exc:  # noqa: BLE001
        # A failed helper/dispatch may have left the transaction aborted or
        # expired its ORM state.  Start the failure transition from a clean
        # transaction and reload by the preserved scalar identities.
        await db.rollback()
        log.warning(
            "main_creator.execution_failed",
            session_id=str(creator_session_id),
            error=str(exc)[:300],
        )
        failed = await _load_session(db, creator_session_id, user_id, plan_item_id, for_update=True)
        failed.status = "failed"
        failed.last_error = {"code": "execution_failed", "message": str(exc)[:300]}
        failed_receipt = await db.get(CreatorAgentExecution, receipt_id, with_for_update=True)
        if failed_receipt:
            failed_receipt.status = "failed"
            failed_receipt.error = failed.last_error
            failed_receipt.completed_at = datetime.now(UTC)
        await append_event(
            db,
            failed,
            event_type="assistant_error",
            payload={"message": "I couldn't start that render. Your creative plan is still saved."},
        )
        return await _response(db, failed)

    completed = await _load_session(db, creator_session_id, user_id, plan_item_id, for_update=True)
    if guided_generation_attempt_id is not None:
        completed.active_plan = {
            **(completed.active_plan or {}),
            "guided_generation_attempt_id": guided_generation_attempt_id,
            "guided_proposal_version": guided_proposal_version,
        }
    completed.target_job_id = job_id
    completed.status = "rendering" if job_id else "executing"
    completed_receipt = await db.get(CreatorAgentExecution, receipt_id, with_for_update=True)
    if completed_receipt:
        completed_receipt.status = "succeeded"
        completed_receipt.result = {"job_id": str(job_id) if job_id else None}
        completed_receipt.completed_at = datetime.now(UTC)
    await append_event(
        db,
        completed,
        event_type="assistant_execution",
        payload={
            "message": (
                "I started the confirmed edit. I'll review the exact rendered "
                "version when it's ready."
            )
        },
    )
    return await _response(db, completed)


def _craft_response(receipt: CreatorAgentExecution) -> CreatorCraftResponse:
    result = receipt.result if isinstance(receipt.result, dict) else {}
    preview = result.get("preview") if isinstance(result.get("preview"), dict) else {}
    return CreatorCraftResponse(
        status=str(receipt.status),
        receipt_id=str(receipt.id),
        generation=(str(result.get("generation")) if result.get("generation") else None),
        preview=preview,
    )


def _stable_manifest_fingerprint(manifest: Any) -> str:
    """Hash live policy/media context while excluding the expected render identity."""

    payload = manifest.model_dump(mode="json")
    payload["current_edit"] = None
    payload.pop("context_hash", None)
    payload.pop("manifest_hash", None)
    return canonical_context_hash(payload)


async def _rollback_craft_commit(
    db: AsyncSession,
    *,
    receipt_id: uuid.UUID,
    session_id: uuid.UUID,
    job_id: uuid.UUID,
    previous_assembly_plan: dict | None,
    variant_id: str,
    generation: str,
    error: Exception,
    previous_job_state: dict[str, Any] | None = None,
    previous_session_state: dict[str, Any] | None = None,
) -> None:
    """Undo only this craft generation when broker publication fails.

    The target generation is the compare-and-swap guard.  Restore the exact
    target variant and speech-cut fields owned by this craft, while retaining
    sibling variants and unrelated assembly-plan keys committed during broker
    publication.
    """

    creator_owned_keys = (
        "silence_cut_disabled",
        "speech_cut_control",
        "speech_cut_previous_variant",
        "speech_cut_previous_variants",
        "speech_cut_last_error",
    )

    await db.rollback()
    # Match the route-wide lock order: CreatorAgentSession -> Job -> receipt.
    # Reversing the first two creates a PostgreSQL deadlock when a fresh craft
    # request overlaps broker-failure rollback for the same session and Job.
    locked_session = None
    if previous_session_state:
        locked_session = await db.get(
            CreatorAgentSession,
            session_id,
            populate_existing=True,
            with_for_update=True,
        )
    locked_job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
    generation_still_owned = False
    if locked_job is not None:
        variants = list((locked_job.assembly_plan or {}).get("variants") or [])
        current_index = next(
            (
                index
                for index, value in enumerate(variants)
                if value.get("variant_id") == variant_id
                and value.get("render_generation_id") == generation
            ),
            None,
        )
        if current_index is not None:
            generation_still_owned = True
            previous_variants = list((previous_assembly_plan or {}).get("variants") or [])
            previous_variant = next(
                (
                    value
                    for value in previous_variants
                    if isinstance(value, dict) and value.get("variant_id") == variant_id
                ),
                None,
            )
            if previous_variant is None:
                variants.pop(current_index)
            else:
                variants[current_index] = copy.deepcopy(previous_variant)
            # Restore only this target variant. Sibling variants and unrelated
            # top-level assembly state may have been committed while the
            # broker call was in flight and must survive the rollback.
            current_assembly = copy.deepcopy(locked_job.assembly_plan or {})
            current_assembly["variants"] = variants
            previous_owned = (previous_assembly_plan or {}).get("_creator_craft_owned")
            if not isinstance(previous_owned, dict):
                previous_owned = {
                    key: (previous_assembly_plan or {})[key]
                    for key in creator_owned_keys
                    if key in (previous_assembly_plan or {})
                }
            for key in creator_owned_keys:
                if key in previous_owned:
                    current_assembly[key] = copy.deepcopy(previous_owned[key])
                else:
                    current_assembly.pop(key, None)
            locked_job.assembly_plan = current_assembly
            if previous_job_state:
                locked_job.status = previous_job_state.get("status")
                started_at = previous_job_state.get("started_at")
                locked_job.started_at = (
                    datetime.fromisoformat(started_at) if isinstance(started_at, str) else None
                )
    if previous_session_state:
        if (
            locked_session is not None
            and generation_still_owned
            and str(locked_session.target_generation_id or "") == generation
        ):
            locked_session.status = previous_session_state.get("status")
            previous_target_job_id = previous_session_state.get("target_job_id")
            locked_session.target_job_id = (
                uuid.UUID(previous_target_job_id) if previous_target_job_id else None
            )
            for field in (
                "target_variant_id",
                "target_generation_id",
                "render_attempts",
                "iteration_count",
                "revision",
            ):
                setattr(locked_session, field, previous_session_state.get(field))
    failed_receipt = await db.get(CreatorAgentExecution, receipt_id, with_for_update=True)
    if failed_receipt is not None:
        failed_receipt.status = "failed"
        failed_receipt.error = {
            "code": "craft_enqueue_failed",
            "message": str(error)[:300],
            "job_id": str(job_id),
            "generation": generation,
            "rolled_back": True,
        }
        failed_receipt.completed_at = datetime.now(UTC)
    await db.commit()


async def _resolve_creator_overlay_asset(
    db: AsyncSession,
    *,
    item: PlanItem,
    user_id: uuid.UUID,
    asset_id: str,
) -> dict[str, Any]:
    """Resolve an opaque upload/catalog identity to a locked asset snapshot.

    The agent sees ``asset-{uuid}`` identities in its manifest.  Accept the
    equivalent ``visual-{uuid}`` catalog spelling and the bare UUID for
    version-skewed clients, but never accept a path or URL from the request.
    """

    raw_id = asset_id
    for prefix in ("asset-", "visual-"):
        if raw_id.startswith(prefix):
            raw_id = raw_id[len(prefix) :]
            break
    try:
        asset_uuid = uuid.UUID(raw_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Unknown overlay asset") from exc
    asset = (
        await db.execute(
            select(PlanItemAsset)
            .where(
                PlanItemAsset.id == asset_uuid,
                PlanItemAsset.plan_item_id == item.id,
                PlanItemAsset.user_id == user_id,
                PlanItemAsset.status == "ready",
                PlanItemAsset.deduplicated_to_asset_id.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=422, detail="Unknown overlay asset")
    return {
        "id": str(asset.id),
        "kind": asset.kind,
        "gcs_path": asset.gcs_path,
        "preview_gcs_path": getattr(asset, "preview_gcs_path", None),
        "duration_s": asset.duration_s,
    }


async def _resolve_creator_licensed_sfx(
    db: AsyncSession,
    *,
    command: SetLicensedSfxCommand,
    user_id: uuid.UUID,
    plan_item_id: uuid.UUID,
    variant: dict[str, Any],
) -> dict[str, Any]:
    """Resolve one opaque catalog id into the existing validated SFX shape.

    Creator craft intentionally has no asset/path or end-time input.  The
    catalog row owns the audio path and duration; the shared placement validator
    still enforces the persistent path namespace before the editor commit sees
    it.
    """

    effect = (
        await db.execute(select(SoundEffect).where(SoundEffect.id == command.sound_effect_id))
    ).scalar_one_or_none()
    if (
        effect is None
        or effect.status != "ready"
        or effect.published_at is None
        or effect.archived_at is not None
        or not effect.audio_gcs_path
    ):
        raise HTTPException(status_code=404, detail="Licensed sound effect is unavailable")
    at_s = float(command.at_s)
    duration = float(visual_block_variant_duration(variant) or 0.0)
    if duration <= 0.0 or at_s > duration + 1e-6:
        raise HTTPException(status_code=422, detail="SFX placement is outside the variant")
    raw = {
        "id": uuid.uuid4().hex,
        "sound_effect_id": str(effect.id),
        "src_gcs_path": str(effect.audio_gcs_path),
        "at_s": at_s,
        "duration_s": effect.duration_s,
        "label": effect.name,
        "source": "creator_agent",
    }
    validated = validate_sound_effects_for_user(
        sfx_raw=[raw],
        user_id=str(user_id),
        plan_item_id=str(plan_item_id),
    )
    if len(validated) != 1:
        raise HTTPException(status_code=422, detail="Licensed sound effect is invalid")
    return validated[0]


def _creator_speech_cut_source_enabled(source: str) -> bool:
    """Resolve the independent detector switch for an approved candidate."""

    if source == "retake_review":
        return settings.retake_cut_enabled
    if source in {"silence_review", "filler_review"}:
        return settings.silence_cut_enabled
    return False


def _stage_creator_speech_cut(
    job: Job,
    *,
    variant_id: str,
    command: ApplySpeechCutCommand,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Stage an existing candidate state transition without publishing it."""

    from sqlalchemy.orm.attributes import flag_modified

    from app.pipeline.speech_cut_state import accept_candidate

    variant = next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants") or []
            if v.get("variant_id") == variant_id
        ),
        None,
    )
    if variant is None:
        raise HTTPException(status_code=404, detail="Creator variant changed")
    candidate = next(
        (
            value
            for value in variant.get("speech_cut_candidates") or []
            if isinstance(value, dict) and value.get("candidate_id") == command.candidate_id
        ),
        None,
    )
    if candidate is None or candidate.get("status") != "pending":
        raise HTTPException(status_code=404, detail="speech_cut_candidate_not_found")
    candidate_source = str(candidate.get("source") or "")
    if not _creator_speech_cut_source_enabled(candidate_source):
        raise HTTPException(status_code=404, detail="Automatic speech cuts are unavailable")
    if variant.get("resolved_archetype") not in {"subtitled", "talking_head"}:
        raise HTTPException(status_code=422, detail="Automatic speech cuts are unavailable")
    if not variant.get("base_video_path"):
        raise HTTPException(status_code=422, detail="Automatic speech cuts are unavailable")
    if variant.get("render_status") == "rendering" or variant.get("speech_cut_in_flight"):
        raise HTTPException(status_code=409, detail="Creator render is busy")
    if not command.expected_cut_revision:
        raise HTTPException(status_code=409, detail="Speech-cut revision is required")
    try:
        updated, request = accept_candidate(
            variant,
            candidate_id_value=command.candidate_id,
            expected_revision=command.expected_cut_revision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # ApplySpeechCutCommand's cut revision is separate from the creator session
    # integer revision envelope and is checked by the candidate state machine.
    operation_id = uuid.uuid4().hex
    previous_variants = list((job.assembly_plan or {}).get("variants") or [])
    updated.update({"ok": False, "render_status": "rendering", "speech_cut_last_error": None})
    variants = [updated if v.get("variant_id") == variant_id else v for v in previous_variants]
    control = {
        "variant_id": variant_id,
        "forced_removals": updated["speech_cut_in_flight"]["desired_forced_removals"],
        "desired_disabled": False,
        "prior_disabled": (job.assembly_plan or {}).get("silence_cut_disabled") is True,
        "operation": request,
        "operation_id": operation_id,
        "finalizer_claim": None,
        "revision": request["revision"],
        "in_flight": updated["speech_cut_in_flight"],
    }
    job.assembly_plan = {
        **(job.assembly_plan or {}),
        "silence_cut_disabled": False,
        "speech_cut_control": control,
        "speech_cut_previous_variant": variant,
        "speech_cut_previous_variants": previous_variants,
        "speech_cut_last_error": None,
        "variants": variants,
    }
    job.status = "processing"
    flag_modified(job, "assembly_plan")
    mark_reattempt(job)
    return request, operation_id, variant


async def _execute_creator_craft(
    item_id: str,
    body: CreatorCraftBundle,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    *,
    manage_session_state: bool,
) -> CreatorCraftResponse:
    """Execute one exact-generation craft bundle atomically.

    Caption style, transitions, looks, owner-scoped overlays, and catalog-backed
    SFX compile into the existing editor gateway. Speech cuts stage through the
    validated candidate state machine. The route owns creator/session/job
    fences, the idempotency receipt, and queue publication; it never accepts a
    storage path or constructs FFmpeg from model output.
    """

    _require_feature(user.id, execution=True)
    item, plan_row, persona = await _owned_context(db, item_id, user.id, for_update=True)
    try:
        session_id = uuid.UUID(body.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Creator session changed") from exc
    session = await _load_session(db, session_id, user.id, item.id, for_update=True)
    prior_target_job_id = getattr(session, "target_job_id", None)
    previous_session_state = {
        "status": session.status,
        "target_job_id": str(prior_target_job_id) if prior_target_job_id else None,
        "target_variant_id": getattr(session, "target_variant_id", None),
        "target_generation_id": getattr(session, "target_generation_id", None),
        "render_attempts": getattr(session, "render_attempts", 0),
        "iteration_count": getattr(session, "iteration_count", 0),
        "revision": session.revision,
    }
    request_digest = canonical_context_hash(body.model_dump(mode="json"))
    receipt = (
        await db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.session_id == session.id,
                CreatorAgentExecution.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if receipt is not None:
        if receipt.request_digest != request_digest:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        if receipt.status not in {"running", "succeeded"}:
            raise HTTPException(status_code=409, detail="Creator craft execution is not resumable")
    elif session.status not in {"reviewing", "awaiting_feedback", "revising", "completed"}:
        raise HTTPException(status_code=409, detail="Creator render is not ready for craft")
    existing_result = receipt.result if receipt is not None else None
    recovering_prepared = bool(
        receipt is not None
        and receipt.status in {"running", "succeeded"}
        and isinstance(existing_result, dict)
        and existing_result.get("prepared")
    )

    # A direct craft commit advances the controller revision before broker
    # publication.  Only the exact idempotency receipt (same digest/session)
    # may replay across that intentional revision bump; every fresh stale
    # request still fails closed.
    if session.revision != body.expected_revision and not recovering_prepared:
        raise HTTPException(status_code=409, detail="Creator session changed")
    if session.ownership_epoch != int(plan_row.ownership_epoch or 0):
        raise HTTPException(status_code=409, detail="Creator ownership changed")
    if body.expected_ownership_epoch != int(plan_row.ownership_epoch or 0):
        raise HTTPException(status_code=409, detail="Creator ownership changed")

    manifest, _media_context = await resolve_item_creator_context(db, item, persona=persona)
    full_manifest_match = (
        body.expected_manifest_hash == manifest.manifest_hash
        and body.expected_context_hash == manifest.context_hash
    )
    stable_manifest_match = bool(
        not full_manifest_match
        and recovering_prepared
        and existing_result.get("stable_manifest_fingerprint")
        == _stable_manifest_fingerprint(manifest)
    )
    if not full_manifest_match and not stable_manifest_match:
        raise HTTPException(status_code=409, detail="Creator capability manifest changed")

    try:
        expected_job_id = uuid.UUID(body.expected_job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Creator render target changed") from exc
    job = await db.get(Job, expected_job_id, populate_existing=True, with_for_update=True)
    if (
        job is None
        or job.user_id != user.id
        or job.content_plan_item_id != item.id
        or int(job.content_plan_ownership_epoch or 0) != body.expected_ownership_epoch
        or item.current_job_id != job.id
        or (
            job.status not in PLAN_ITEM_JOB_READY
            and not (recovering_prepared and job.status == "processing")
        )
    ):
        raise HTTPException(status_code=409, detail="Creator render target changed")
    previous_job_state = {
        "status": job.status,
        "started_at": (job.started_at.isoformat() if getattr(job, "started_at", None) else None),
    }
    variants = list((job.assembly_plan or {}).get("variants") or [])
    variant = next(
        (value for value in variants if value.get("variant_id") == body.expected_variant_id),
        None,
    )
    if variant is None:
        raise HTTPException(status_code=409, detail="Creator variant changed")
    current_generation = str(variant.get("render_generation_id") or "")

    if receipt is not None and receipt.status == "succeeded":
        recorded_result = receipt.result if isinstance(receipt.result, dict) else {}
        if current_generation != str(recorded_result.get("generation") or ""):
            raise HTTPException(status_code=409, detail="Creator craft execution is stale")
        return _craft_response(receipt)

    # A running receipt may have committed the new generation immediately
    # before a worker/process crash.  Reuse its exact prepared editor commit;
    # never compile against mutable post-crash state.
    prepared = existing_result.get("prepared") if isinstance(existing_result, dict) else None
    if prepared is not None:
        generation = str(existing_result.get("generation") or "")
        if not generation or current_generation != generation:
            raise HTTPException(status_code=409, detail="Creator craft execution is stale")
        recorded_pins = existing_result.get("pins")
        if isinstance(recorded_pins, dict):
            expected_pins = {
                "manifest_hash": body.expected_manifest_hash,
                "context_hash": body.expected_context_hash,
                "job_id": body.expected_job_id,
                "variant_id": body.expected_variant_id,
                "generation_id": body.expected_generation_id,
                "revision": body.expected_revision,
                "ownership_epoch": body.expected_ownership_epoch,
            }
            if recorded_pins != expected_pins:
                raise HTTPException(status_code=409, detail="Creator craft execution is stale")
        preview = existing_result.get("preview") or {}
        previous_assembly_plan = existing_result.get("previous_assembly_plan")
        if isinstance(existing_result.get("previous_job_state"), dict):
            previous_job_state = existing_result["previous_job_state"]
        if isinstance(existing_result.get("previous_session_state"), dict):
            previous_session_state = existing_result["previous_session_state"]
        speech_operation_id = existing_result.get("speech_cut_operation_id")
    else:
        if manage_session_state:
            attempts = int(getattr(session, "render_attempts", 0) or 0)
            max_attempts = int(getattr(session, "max_render_attempts", 2) or 0)
            if attempts >= max_attempts:
                raise HTTPException(status_code=409, detail="Creator render budget exhausted")
        if current_generation != body.expected_generation_id:
            raise HTTPException(status_code=409, detail="Creator render generation changed")
        if variant.get("render_status") not in (None, "ready"):
            raise HTTPException(status_code=409, detail="Creator render is busy")
        overlay_assets: dict[str, dict[str, Any]] = {}
        for command in body.commands:
            capability_name = {
                "set_caption_style": "caption_style",
                "set_transition": "transitions",
                "set_look_preset": "wide_looks",
                "set_media_overlay": "media_overlays",
                "set_licensed_sfx": "sound_effects",
                "apply_speech_cut": "automatic_cut",
            }.get(command.command)
            if command.command == "remove_optional_treatment":
                capability_name = (
                    "media_overlays" if command.treatment == "media_overlay" else "sound_effects"
                )
            if capability_name is None:
                raise HTTPException(
                    status_code=404,
                    detail="Creator treatment is unavailable",
                )
            capability = manifest.capabilities.get(capability_name)
            if capability is None or not capability.available:
                raise HTTPException(
                    status_code=404,
                    detail=f"Creator treatment is unavailable: {capability_name}",
                )
            if command.command == "set_media_overlay":
                overlay_assets[command.asset_id] = await _resolve_creator_overlay_asset(
                    db,
                    item=item,
                    user_id=user.id,
                    asset_id=command.asset_id,
                )
        current_assembly_plan = job.assembly_plan or {}
        previous_assembly_plan = {
            # Keep the receipt's rollback material bounded to the one target
            # variant.  Sibling variants and unrelated top-level state may be
            # changed by another request while the broker call is in flight.
            "variants": [copy.deepcopy(variant)],
            "_creator_craft_owned": {
                key: copy.deepcopy(current_assembly_plan[key])
                for key in (
                    "silence_cut_disabled",
                    "speech_cut_control",
                    "speech_cut_previous_variant",
                    "speech_cut_previous_variants",
                    "speech_cut_last_error",
                )
                if key in current_assembly_plan
            },
        }
        speech_operation_id: str | None = None
        resolved_sfx: dict[str, Any] | None = None
        try:
            speech_commands = [
                command for command in body.commands if isinstance(command, ApplySpeechCutCommand)
            ]
            if len(speech_commands) > 1:
                raise CreatorCraftValidationError("Only one speech-cut command is allowed")
            if speech_commands:
                _request, speech_operation_id, _prior_variant = _stage_creator_speech_cut(
                    job,
                    variant_id=body.expected_variant_id,
                    command=speech_commands[0],
                )
            sfx_commands = [
                command for command in body.commands if isinstance(command, SetLicensedSfxCommand)
            ]
            if len(sfx_commands) > 1:
                raise CreatorCraftValidationError("Only one licensed SFX command is allowed")
            if sfx_commands:
                resolved_sfx = await _resolve_creator_licensed_sfx(
                    db,
                    command=sfx_commands[0],
                    user_id=user.id,
                    plan_item_id=item.id,
                    variant=variant,
                )
            if overlay_assets:
                editor_commit = build_media_overlay_craft_editor_commit(
                    body,
                    variant=variant,
                    assets=overlay_assets,
                )
            else:
                editor_commit = build_core_craft_editor_commit(
                    body,
                    variant=variant,
                    licensed_sfx=resolved_sfx,
                )
            has_editor_sections = any(
                value is not None
                for value in (
                    editor_commit.caption_meta,
                    editor_commit.timeline_slots,
                    editor_commit.sound_effects,
                    editor_commit.media_overlays,
                )
            )
            if has_editor_sections:
                prepared = prepare_editor_commit(
                    job,
                    body.expected_variant_id,
                    editor_commit,
                    user_id=str(user.id),
                    plan_item_id=str(item.id),
                )
                if speech_operation_id:
                    # The speech rerender projects creator-authored lanes from
                    # this snapshot onto its freshly rebuilt source. Include
                    # the same-bundle SFX/caption/timeline changes; the separate
                    # previous_variants snapshot remains the rollback source.
                    staged_variant = next(
                        (
                            value
                            for value in (job.assembly_plan or {}).get("variants") or []
                            if value.get("variant_id") == body.expected_variant_id
                        ),
                        None,
                    )
                    if staged_variant is not None:
                        job.assembly_plan = {
                            **(job.assembly_plan or {}),
                            "speech_cut_previous_variant": copy.deepcopy(staged_variant),
                        }
            elif speech_operation_id:
                # Speech-only bundles do not have an editor section. Mint the
                # same token the existing speech rerender uses while retaining
                # the candidate state staged above in this transaction.
                generation = uuid.uuid4().hex
                staged_variants = list((job.assembly_plan or {}).get("variants") or [])
                for staged in staged_variants:
                    if staged.get("variant_id") == body.expected_variant_id:
                        staged["render_generation_id"] = generation
                        staged["render_status"] = "rendering"
                job.assembly_plan = {**(job.assembly_plan or {}), "variants": staged_variants}
                prepared = {
                    "generation": generation,
                    "has_render_section": True,
                    "sections": {"speech_cut": True},
                    "speech_cut_operation_id": speech_operation_id,
                }
            else:
                raise CreatorCraftValidationError("Provide at least one craft command")
        except CreatorCraftValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        generation = str(prepared["generation"])
        if speech_operation_id:
            prepared["speech_cut_operation_id"] = speech_operation_id
        preview = craft_preview(body, generation=generation, sections=prepared["sections"])
        if receipt is None:
            receipt = CreatorAgentExecution(
                session_id=session.id,
                idempotency_key=body.idempotency_key,
                request_digest=request_digest,
                expected_revision=body.expected_revision,
                expected_manifest_hash=manifest.manifest_hash,
                status="running",
            )
            db.add(receipt)
        receipt.result = {
            "kind": "creator_core_craft",
            "generation": generation,
            "prepared": prepared,
            "preview": preview,
            "previous_assembly_plan": previous_assembly_plan,
            "previous_job_state": previous_job_state,
            "previous_session_state": previous_session_state,
            "speech_cut_operation_id": speech_operation_id,
            "stable_manifest_fingerprint": _stable_manifest_fingerprint(manifest),
            "pins": {
                "manifest_hash": body.expected_manifest_hash,
                "context_hash": body.expected_context_hash,
                "job_id": body.expected_job_id,
                "variant_id": body.expected_variant_id,
                "generation_id": body.expected_generation_id,
                "revision": body.expected_revision,
                "ownership_epoch": body.expected_ownership_epoch,
            },
        }
        if manage_session_state:
            # The craft route is a second render path, not merely an editor
            # receipt.  Advance the controller while the same session/job
            # locks are held so reconciliation cannot observe a new Job
            # generation paired with the old target or render budget.
            session.status = "rendering"
            session.target_job_id = job.id
            session.target_variant_id = body.expected_variant_id
            session.target_generation_id = generation
            session.render_attempts = int(getattr(session, "render_attempts", 0) or 0) + 1
            session.iteration_count = session.render_attempts
            session.revision = int(getattr(session, "revision", 0) or 0) + 1
        await db.flush()
        receipt_id = receipt.id
        await db.commit()

    assert receipt is not None
    if receipt.id is None:
        raise HTTPException(status_code=409, detail="Creator craft receipt is unavailable")
    receipt_id = receipt.id
    try:
        craft_task_id = f"creator-craft-{receipt_id}-{generation}"
        if speech_operation_id:
            from app.tasks.generative_build import rerender_speech_timing

            rerender_speech_timing.apply_async(
                args=[str(expected_job_id), str(speech_operation_id)],
                queue="plan-jobs",
                task_id=craft_task_id,
            )
        else:
            enqueue_editor_commit_render(
                str(expected_job_id),
                body.expected_variant_id,
                prepared,
                task_id=craft_task_id,
            )
    except Exception as exc:  # noqa: BLE001 — committed state must be rolled back
        if previous_assembly_plan is not None:
            await _rollback_craft_commit(
                db,
                receipt_id=receipt_id,
                session_id=session.id,
                job_id=expected_job_id,
                previous_assembly_plan=previous_assembly_plan,
                variant_id=body.expected_variant_id,
                generation=generation,
                error=exc,
                previous_job_state=previous_job_state,
                previous_session_state=(previous_session_state if manage_session_state else None),
            )
        else:
            await db.rollback()
            failed_receipt = await db.get(CreatorAgentExecution, receipt_id, with_for_update=True)
            if failed_receipt is not None:
                failed_receipt.status = "failed"
                failed_receipt.error = {
                    "code": "craft_enqueue_failed",
                    "message": str(exc)[:300],
                }
                failed_receipt.completed_at = datetime.now(UTC)
                await db.commit()
        raise HTTPException(
            status_code=503,
            detail="The creator treatment could not be queued; the current video is unchanged.",
        ) from exc

    receipt.status = "succeeded"
    receipt.completed_at = datetime.now(UTC)
    response = _craft_response(receipt)
    await db.commit()
    return response


@router.post(
    "/{item_id}/creator-agent/craft",
    response_model=CreatorCraftResponse,
)
async def execute_creator_craft(
    item_id: str,
    body: CreatorCraftBundle,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorCraftResponse:
    return await _execute_creator_craft(
        item_id,
        body,
        user,
        db,
        manage_session_state=_MANAGE_CRAFT_SESSION_STATE.get(),
    )


@router.post("/{item_id}/creator-agent/auto-iteration", response_model=CreatorSessionResponse)
async def request_creator_auto_iteration(
    item_id: str,
    body: AutoIterationBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse:
    """Opt into at most one objective, allowlisted revision for this session."""

    _require_feature(user.id, execution=True)
    if not (
        settings.main_creator_agent_review_enabled
        and settings.main_creator_agent_quality_review_enabled
        and settings.main_creator_agent_auto_iteration_enabled
    ):
        raise HTTPException(status_code=409, detail="Automatic creator iteration is unavailable")
    item, plan_row, persona = await _owned_context(db, item_id, user.id, for_update=True)
    session = await _load_session(db, body.session_id, user.id, item.id, for_update=True)
    if not body.opt_in:
        raise HTTPException(
            status_code=422, detail="Explicit automatic-iteration opt-in is required"
        )

    marker = session.last_review if isinstance(session.last_review, dict) else {}
    auto_marker = (
        marker.get("auto_iteration") if isinstance(marker.get("auto_iteration"), dict) else {}
    )
    duplicate = (
        await db.execute(
            select(CreatorAgentEvent).where(
                CreatorAgentEvent.session_id == session.id,
                CreatorAgentEvent.client_event_id == body.client_event_id,
            )
        )
    ).scalar_one_or_none()
    duplicate_running = duplicate is not None and auto_marker.get("status") == "running"
    if duplicate and not duplicate_running:
        return await _response(db, session)
    request_expected_revision = auto_marker.get("request_expected_revision")
    exact_duplicate_revision = (
        duplicate_running
        and isinstance(request_expected_revision, int)
        and body.expected_revision == request_expected_revision
    )
    if session.revision != body.expected_revision and not exact_duplicate_revision:
        raise HTTPException(status_code=409, detail="Creator session changed")
    session.auto_iteration_opt_in = True
    budget = max(0, int(session.max_render_attempts or 0) - int(session.render_attempts or 0))
    decision = evaluate_auto_iteration(
        marker,
        opted_in=session.auto_iteration_opt_in,
        render_budget_remaining=budget,
        automatic_revision_count=int(session.automatic_revision_count or 0),
    )
    if decision.decision != "eligible":
        await append_event(
            db,
            session,
            event_type="system_auto_iteration_skipped",
            payload={
                "message": "No automatic revision was applied.",
                "reason_code": decision.reason_code,
            },
            client_event_id=body.client_event_id,
        )
        await db.commit()
        return await _response(db, session)
    if session.status not in {"awaiting_feedback", "reviewing", "revising", "completed"}:
        raise HTTPException(
            status_code=409, detail="Creator render is not ready for automatic revision"
        )
    if not (session.target_job_id and session.target_variant_id and session.target_generation_id):
        raise HTTPException(status_code=409, detail="Creator render target is incomplete")
    auto_idempotency_key = str(
        auto_marker.get("idempotency_key")
        or f"creator-auto:{session.id}:{session.target_generation_id}"
    )
    auto_receipt = (
        await db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.session_id == session.id,
                CreatorAgentExecution.idempotency_key == auto_idempotency_key,
            )
        )
    ).scalar_one_or_none()
    prepared_recovery = bool(
        auto_receipt
        and auto_receipt.status in {"running", "succeeded"}
        and isinstance(auto_receipt.result, dict)
        and auto_receipt.result.get("prepared")
    )
    job = await db.get(Job, session.target_job_id, with_for_update=True)
    variants = list((job.assembly_plan or {}).get("variants") or []) if job else []
    variant = next(
        (
            value
            for value in variants
            if isinstance(value, dict) and value.get("variant_id") == session.target_variant_id
        ),
        None,
    )
    if (
        job is None
        or variant is None
        or (
            str(variant.get("render_generation_id") or "") != str(session.target_generation_id)
            and not (
                prepared_recovery
                and str(variant.get("render_generation_id") or "")
                == str((auto_receipt.result or {}).get("generation") or "")
            )
        )
    ):
        raise HTTPException(status_code=409, detail="Creator render generation changed")
    manifest, _media_context = await resolve_item_creator_context(db, item, persona=persona)
    raw_bundle = auto_marker.get("bundle")
    if prepared_recovery or duplicate_running:
        try:
            bundle = recover_auto_bundle(
                raw_bundle,
                session_id=str(session.id),
                idempotency_key=auto_idempotency_key,
                job_id=str(job.id),
                variant_id=str(session.target_variant_id),
                generation_id=str(session.target_generation_id),
                ownership_epoch=int(plan_row.ownership_epoch or 0),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409, detail="Creator automatic revision is stale"
            ) from exc
    else:
        if not duplicate:
            await append_event(
                db,
                session,
                event_type="user_auto_iteration_opt_in",
                role="user",
                payload={"message": "Allow one automatic objective revision."},
                client_event_id=body.client_event_id,
            )
        pin = {
            "expected_manifest_hash": manifest.manifest_hash,
            "expected_context_hash": manifest.context_hash,
            "expected_job_id": str(job.id),
            "expected_variant_id": str(session.target_variant_id),
            "expected_generation_id": str(session.target_generation_id),
            "expected_revision": session.revision,
            "expected_ownership_epoch": int(plan_row.ownership_epoch or 0),
        }
        try:
            bundle = build_auto_bundle(
                session_id=str(session.id),
                idempotency_key=auto_idempotency_key,
                pin=pin,
                action=str(marker.get("allowlist_action")),
                review=marker,
                variant=variant,
            )
        except ValueError as exc:
            await append_event(
                db,
                session,
                event_type="system_auto_iteration_skipped",
                payload={
                    "message": "No safe automatic revision was available.",
                    "reason_code": str(exc),
                },
            )
            await db.commit()
            return await _response(db, session)
        session.last_review = {
            **marker,
            "auto_iteration": {
                "status": "running",
                "action": str(marker.get("allowlist_action")),
                "session_id": str(session.id),
                "job_id": str(job.id),
                "variant_id": str(session.target_variant_id),
                "previous_generation_id": str(session.target_generation_id),
                "request_expected_revision": request_expected_revision
                if isinstance(request_expected_revision, int)
                else body.expected_revision,
                "expected_revision": session.revision,
                "ownership_epoch": int(plan_row.ownership_epoch or 0),
                "idempotency_key": auto_idempotency_key,
                "bundle": bundle.model_dump(mode="json"),
            },
        }
        await db.commit()
    try:
        craft_state_token = _MANAGE_CRAFT_SESSION_STATE.set(False)
        try:
            craft_response = await execute_creator_craft(item_id, bundle, user, db)
        finally:
            _MANAGE_CRAFT_SESSION_STATE.reset(craft_state_token)
    except HTTPException as exc:
        refreshed = await _load_session(db, session.id, user.id, item.id, for_update=True)
        refreshed.status = "awaiting_feedback"
        refreshed.last_review = {
            **(refreshed.last_review if isinstance(refreshed.last_review, dict) else marker),
            "auto_iteration": {
                **auto_marker,
                "status": "unavailable",
                "reason_code": "craft_failed",
            },
        }
        await append_event(
            db,
            refreshed,
            event_type="system_auto_iteration_unavailable",
            payload={
                "message": "Automatic revision was unavailable; review the current video manually."
            },
        )
        await db.commit()
        log.warning("creator_auto_iteration_failed_open", status=exc.status_code)
        return await _response(db, refreshed)

    refreshed = await _load_session(db, session.id, user.id, item.id, for_update=True)
    # The craft receipt is idempotent, but two requests can both observe the
    # pre-craft `running` marker while the first is across the broker boundary.
    # The session row lock serializes finalization; once either request records
    # the one allowed cycle, every follower returns without burning counters.
    if _auto_iteration_already_finalized(refreshed):
        return await _response(db, refreshed)
    refreshed.status = "rendering"
    refreshed.target_generation_id = craft_response.generation
    refreshed.render_attempts += 1
    refreshed.iteration_count = refreshed.render_attempts
    refreshed.automatic_revision_count += 1
    refreshed.last_good = {
        "job_id": str(job.id),
        "variant_id": str(session.target_variant_id),
        "generation_id": str(session.target_generation_id),
        "rollback_receipt_id": craft_response.receipt_id,
    }
    refreshed.last_review = {
        **(refreshed.last_review if isinstance(refreshed.last_review, dict) else marker),
        "automatic_revision_count": refreshed.automatic_revision_count,
        "auto_iteration": {
            **(
                refreshed.last_review.get("auto_iteration", {})
                if isinstance(refreshed.last_review, dict)
                else {}
            ),
            "status": "queued",
            "receipt_id": craft_response.receipt_id,
            "generation_id": craft_response.generation,
            "rollback_receipt": {
                "job_id": str(job.id),
                "variant_id": str(session.target_variant_id),
                "previous_generation_id": str(session.target_generation_id),
                "craft_receipt_id": craft_response.receipt_id,
            },
        },
    }
    await append_event(
        db,
        refreshed,
        event_type="assistant_auto_iteration_queued",
        payload={"message": "One bounded objective revision is rendering."},
    )
    await db.commit()
    return await _response(db, refreshed)


@router.post("/{item_id}/creator-agent/cancel", response_model=CreatorSessionResponse)
async def cancel_creator_session(
    item_id: str,
    body: CancelBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse:
    _require_feature(user.id)
    item, _plan, _persona = await _owned_context(db, item_id, user.id)
    session = await _load_session(db, body.session_id, user.id, item.id, for_update=True)
    if session.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creator session changed")
    if session.status in {"executing", "rendering", "reviewing"}:
        raise HTTPException(status_code=409, detail="A running render cannot be cancelled here")
    if session.status not in ACTIVE_CREATOR_PHASES:
        return await _response(db, session)
    session.status = "cancelled"
    await append_event(
        db,
        session,
        event_type="system_cancelled",
        payload={"message": "Creator session cancelled."},
    )
    return await _response(db, session)
