"""Authenticated durable Main Creator Agent v1 routes.

The model can only propose an inert strategy. This route owns revision fences,
explicit confirmation, manifest revalidation, typed PlanItem mutations, and
dispatch through the existing render entry point.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents._model_client import default_client
from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderQuotaExceededError,
    RunContext,
    TerminalError,
)
from app.agents._schemas.creator_agent import (
    CREATOR_REQUEST_MAX_CHARS,
    ApplySpeechCutCommand,
    AskUser,
    CreativeStrategy,
    CreatorCraftBundle,
    CreatorEditPlan,
    CreatorEditSnapshot,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
    ResolvedCreatorManifest,
    ReviewDecision,
    SetLicensedSfxCommand,
    canonical_context_hash,
    canonical_manifest_hash,
    creator_strategies_equal,
    normalize_creator_text_color,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_GUIDED_VOICEOVER,
    CAPABILITY_PHONE_SOURCE_AUDIO,
    GUIDED_VOICEOVER_EXECUTION_CONTRACT,
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
    MixedMediaTimingUnavailableError,
    MontageCadenceUnavailableError,
    PhoneFormatUnavailableError,
    PhoneMediaUnavailableError,
    explicit_scope_from_stated_media_count,
    normalize_creator_strategy_media,
    states_explicit_media_narrowing_cue,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput, MainCreatorOutput
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.db_locks import acquire_locked_rows
from app.limiter import limiter
from app.models import (
    AgentRun,
    ContentPlan,
    CreationThread,
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
from app.schemas.clip_intents import ResolvedClipIntent
from app.schemas.edit_proposal import (
    MAX_PROPOSAL_DURATION_S,
    MixedMediaTimingProfile,
    MontageCadenceConstraint,
    recognize_cadence_reuse_policy,
    recognize_explicit_cadence_reuse_policy,
    recognize_image_layout,
    recognize_mixed_media_timing,
    recognize_round_robin_cadence,
    recognize_total_duration_s,
    rejects_round_robin_cadence,
)
from app.services.ai_usage_headers import paid_call_headers
from app.services.clip_intent_answers import (
    persist_clip_intent_vision_answers as _persist_clip_intent_vision_answers,
)
from app.services.clip_intent_planning import plan_and_resolve_clip_intents
from app.services.clip_intent_resolution import IntentResolution
from app.services.content_plan_persona import load_owned_plan_persona
from app.services.creator_autonomy import (
    build_auto_bundle,
    evaluate_auto_iteration,
    recover_auto_bundle,
)
from app.services.creator_capabilities import (
    CreatorSfxUnavailableError,
    resolve_creator_sfx_catalog_ref,
)
from app.services.creator_craft import (
    CreatorCraftValidationError,
    build_core_craft_editor_commit,
    build_media_overlay_craft_editor_commit,
    craft_preview,
)
from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError
from app.services.creator_sessions import (
    ACTIVE_CREATOR_PHASES,
    append_event,
    compile_active_plan,
    creator_context,
    creator_narration_identity,
    load_intent_clips_for_item,
    reconcile_render_state,
    resolve_item_creator_context,
    rollout_eligible,
    serialize_session,
)
from app.services.edit_direction_planner import (
    GUIDED_STORY_MAX_MEDIA,
    assess_all_media_capacity,
    round_robin_capacity_s,
)
from app.services.job_phases import mark_reattempt
from app.services.job_status import (
    PLAN_ITEM_JOB_FAILED,
    PLAN_ITEM_JOB_READY,
    PLAN_ITEM_JOB_TERMINAL,
)
from app.services.public_assembly_plan import project_public_assembly_plan
from app.services.speech_cleanup_terminal import classify_route_speech_cut_rollback
from app.services.variant_generation_guard import (
    VariantInitialRenderInProgress,
    assert_required_speech_dispatch_quiescent,
    assert_variant_generation_editable,
)

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
    message: str = Field(min_length=1, max_length=CREATOR_REQUEST_MAX_CHARS)
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
    # Chat-first speech preflight passes these through to the row-locked Job
    # mint. They are optional here so non-chat Creator surfaces retain their
    # existing contract until that rollout reaches them.
    speech_cleanup_analysis_id: uuid.UUID | None = None
    speech_cleanup_choice: Literal["clean", "keep_original", "create_without_cleanup"] | None = None


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
    preparation: dict | None = None
    created_at: str
    updated_at: str


def _assert_variant_generation_editable_or_409(job: Job, variant_id: str) -> None:
    """Reject creator writes while an initial required-speech render is private."""

    try:
        assert_variant_generation_editable(job, variant_id)
    except VariantInitialRenderInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="variant_initial_render_in_progress",
        ) from exc


def _creator_session_response(session: CreatorAgentSession) -> CreatorSessionResponse:
    payload = project_public_assembly_plan(serialize_session(session))
    return CreatorSessionResponse.model_validate(payload)


def _require_feature(
    user_id: uuid.UUID, *, execution: bool = False, allow_chat: bool = False
) -> None:
    # Chat creation is the canonical product and may always reuse these
    # controllers. Direct PlanItem endpoints retain their own rollout gates.
    chat_enabled = allow_chat
    if not chat_enabled and not rollout_eligible(user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Creator agent unavailable"
        )
    if execution and not chat_enabled and not settings.main_creator_agent_execution_enabled:
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
    runtime_v2_thread_id = (
        await db.execute(
            select(CreationThread.id)
            .where(
                CreationThread.creator_id == user_id,
                CreationThread.active_plan_item_id == iid,
                CreationThread.runtime_version == 2,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if runtime_v2_thread_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This project is controlled by the new Kria runtime.",
        )
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
    from app.services.creator_preparation import finish_preparation

    await finish_preparation(db, session)
    await db.commit()
    loaded = await _load_session(db, session.id, session.creator_id, session.plan_item_id)
    return _creator_session_response(loaded)


# A fresh session that replaces a failed one in the same chat project opens
# with this system event holding the failed session's brief, so every later
# turn of the fresh session (a question's answer, a revision, "Retry preparing
# my clips") still reads it. Its payload has no "message": transcript
# projection and role-"user" history readers ignore it.
CARRIED_BRIEF_EVENT = "carried_brief"


def _carried_brief(events: list[CreatorAgentEvent]) -> str:
    """The failed session's brief this session was opened with, or ``""``."""

    for event in sorted(events, key=lambda value: value.sequence):
        if getattr(event, "event_type", None) == CARRIED_BRIEF_EVENT:
            payload = event.payload if isinstance(event.payload, dict) else {}
            return str(payload.get("creator_request") or "").strip()[:CREATOR_REQUEST_MAX_CHARS]
    return ""


def _carried_brief_seed(previous_active_plan: dict[str, Any] | None, message: str) -> str:
    """The failed session's brief to open a fresh session with, or ``""``.

    Lines the new message already repeats (a creator re-pasting the original
    prompt) are dropped, and the brief (not the new message) is trimmed so
    both fit the shared request bound together.
    """

    if not isinstance(previous_active_plan, dict):
        return ""

    def normalized(text: str) -> str:
        return " ".join(text.split()).casefold()

    repeated = normalized(message)
    lines = [
        line.strip()
        for line in str(previous_active_plan.get("creator_request") or "").splitlines()
        if line.strip() and not re.search(rf"(?<!\w){re.escape(normalized(line))}(?!\w)", repeated)
    ]
    budget = CREATOR_REQUEST_MAX_CHARS - len(message.strip()) - 1
    return "\n".join(lines)[: max(budget, 0)].strip()


def _conversation(events: list[CreatorAgentEvent]) -> list[dict[str, str]]:
    turns = [
        {
            "role": event.role,
            "content": str((event.payload or {}).get("message") or "")[:CREATOR_REQUEST_MAX_CHARS],
        }
        for event in sorted(events, key=lambda value: value.sequence)[-20:]
        if (event.payload or {}).get("message")
    ]
    carried = _carried_brief(events)
    if carried:
        # The failed session's brief opens a fresh session's history.
        return [{"role": "user", "content": carried}, *turns[-19:]]
    return turns


def _confirmed_creator_request(
    events: list[CreatorAgentEvent], current_message: str, *, truncate: bool = True
) -> str:
    """Preserve bounded creator-authored intent across clarification turns."""

    messages = [
        str((event.payload or {}).get("message") or "").strip()
        for event in sorted(events, key=lambda value: value.sequence)
        if event.role == "user" and str((event.payload or {}).get("message") or "").strip()
    ]
    if current_message.strip() and (not messages or messages[-1] != current_message.strip()):
        messages.append(current_message.strip())
    carried = _carried_brief(events)
    if carried:
        messages.insert(0, carried)
    request = "\n".join(messages)
    return request[:CREATOR_REQUEST_MAX_CHARS] if truncate else request


# Sent verbatim by the iOS confirmation card's "refresh direction" affordance
# (`CreationConfirmationConflict.refreshDirectionMessage` in
# CreationConfirmationStage.swift) when the server rejects a stale/failed plan
# as unconfirmable and the creator taps Retry. Recognized here so a re-plan
# triggered by this canned message is treated as a refresh of the existing
# direction, never as a brand-new creative instruction — the Main Creator
# model is not trusted to infer that on its own from the message text alone.
REFRESH_DIRECTION_MESSAGE = "Keep the same plan with my current footage"

# Fields carried over from the creator's last ACCEPTED strategy when a re-plan
# is triggered by the refresh/retry affordance, rather than re-derived by the
# model. `render_program` is included alongside `direction` even though it is
# not creator-facing: the two are 1:1 coupled (a direction is only meaningful
# through the render program that executes it), so pinning `direction` alone
# while leaving `render_program` to the model would restore the label without
# restoring the actual behavior. `edit_format` is deliberately NOT pinned here
# — it is independently fenced by the Paper format the creator already
# selected (`planned_format != state.get("edit_format")` in
# creation_threads.py's action_thread), so the Main Creator is expected to
# keep proposing that same edit_format regardless of direction.
_PINNED_STRATEGY_FIELDS_ON_REFRESH: tuple[str, ...] = (
    "direction",
    "render_program",
    "pacing",
    "target_duration_s",
    "opening_title",
    "closing_title",
    "shot_labels",
    "clip_intents",
    "audio_strategy",
    "video_reuse_policy",
    "montage_cadence",
    "mixed_media_timing",
)


def _is_refresh_retry_message(user_message: str) -> bool:
    """True when this turn's message is the canned refresh/retry affordance."""

    return user_message.strip() == REFRESH_DIRECTION_MESSAGE


def _previous_accepted_strategy(
    previous_active_plan: dict[str, Any] | None,
) -> CreativeStrategy | None:
    """Extract the last confirmed strategy from a session's prior active_plan.

    ``previous_active_plan`` must be captured by the caller BEFORE
    ``_reset_render_target`` clears ``session.active_plan`` for the new turn.
    """

    if not isinstance(previous_active_plan, dict):
        return None
    raw_edit_plan = previous_active_plan.get("edit_plan")
    if not isinstance(raw_edit_plan, dict):
        return None
    raw_strategy = raw_edit_plan.get("strategy")
    if not isinstance(raw_strategy, dict):
        return None
    try:
        return CreativeStrategy.model_validate(raw_strategy)
    except ValueError:
        return None


def _original_creator_request_for_refresh(
    previous_active_plan: dict[str, Any] | None,
    events: list[CreatorAgentEvent],
) -> str:
    """The creator's real, previously-stated request — never the canned text.

    A refresh/retry turn's ``user_message`` is the throwaway confirmation-
    screen affordance, not creative intent. Feeding it to the Main Creator (or
    persisting it as the specialist's ``creator_request``/``goal`` source) can
    bias the re-plan away from what the creator actually asked for. Prefer the
    request already pinned to the prior accepted plan; fall back to every past
    user turn that is not itself the canned message.
    """

    if isinstance(previous_active_plan, dict):
        prior = str(previous_active_plan.get("creator_request") or "").strip()
        if prior:
            return prior
    messages = [
        str((event.payload or {}).get("message") or "").strip()
        for event in sorted(events, key=lambda value: value.sequence)
        if event.role == "user" and str((event.payload or {}).get("message") or "").strip()
    ]
    messages = [message for message in messages if message != REFRESH_DIRECTION_MESSAGE]
    carried = _carried_brief(events)
    request = "\n".join([carried, *messages] if carried else messages)
    return request[:CREATOR_REQUEST_MAX_CHARS]


def _pin_strategy_fields_on_refresh(
    strategy: CreativeStrategy,
    previous: CreativeStrategy,
) -> tuple[CreativeStrategy, list[str]]:
    """Overlay the creator's previously accepted pinned fields onto `strategy`.

    Deterministic and prompt-independent by design: the Main Creator model is
    not trusted to preserve direction/pace/duration/titles on its own when the
    triggering message is the canned refresh/retry text rather than a genuine
    new instruction. Returns the (possibly unchanged) strategy plus the list
    of field names actually restored, for structured logging.
    """

    updates: dict[str, Any] = {}
    restored: list[str] = []
    for field in _PINNED_STRATEGY_FIELDS_ON_REFRESH:
        previous_value = getattr(previous, field, None)
        current_value = getattr(strategy, field, None)
        if previous_value != current_value:
            updates[field] = previous_value
            restored.append(field)
    if not updates:
        return strategy, restored
    return strategy.model_copy(update=updates), restored


def _apply_refresh_pin_if_needed(
    strategy: CreativeStrategy,
    *,
    previous_strategy: CreativeStrategy | None,
    manifest: Any,
    session_id: uuid.UUID,
    item_id: str,
) -> CreativeStrategy:
    """Restore the pinned fields, then re-normalize so render_program and any
    other derived fields stay consistent with the restored direction.

    Called as the last step before a strategy is compiled into
    ``active_plan``, so nothing downstream can clobber the pin again.
    """

    if previous_strategy is None:
        return strategy
    pinned, restored_fields = _pin_strategy_fields_on_refresh(strategy, previous_strategy)
    if not restored_fields:
        return strategy
    try:
        pinned = normalize_creator_strategy_media(manifest, pinned, repair_model_output=True)
    except (MixedMediaTimingUnavailableError, MontageCadenceUnavailableError, ValueError):
        # The deterministic guardrail against a re-plan silently swapping the
        # creator's accepted direction takes priority over a defensive
        # re-derivation failing against the current manifest; keep the pinned
        # fields even if this particular re-normalization could not be proven.
        pass
    log.info(
        "creator_strategy_pinned_on_refresh",
        session_id=str(session_id),
        item_id=str(item_id),
        restored_fields=restored_fields,
    )
    return pinned


# A creator can name the whole manifest by an exact count instead of the word
# "all" ("continue with 30 clips", "use the 30 videos"). The noun anchor keeps
# a duration ("30 seconds", "in 30s") from ever being read as a media count.
# The count-matching logic itself (_stated_media_count / _attached_media_count
# / _stated_count_matches_manifest) lives in creator_policy.py as the public
# ``explicit_scope_from_stated_media_count`` so main_creator.py can apply the
# identical rule without importing this route module (KRI-129 part C).
_MEDIA_COUNT_NOUN = r"(?:clips?|videos?|photos?|images?|pictures?|stills?|media|footage)"


def _explicit_media_scope(request: str, manifest: Any | None = None) -> Literal["all", "selected"]:
    """Resolve only an explicit all-media request; preserve the legacy default."""

    normalized = " ".join(str(request or "").casefold().split())
    if re.search(
        r"\b(?:do not|don't|dont|never|without|no)\b.{0,40}"
        r"\b(?:use|include|keep|select)\b.{0,20}"
        rf"\b(?:all|everything|every|\d{{1,4}}\s+{_MEDIA_COUNT_NOUN})\b"
        r"|\b(?:all|everything|every)\b.{0,20}\b(?:not|excluded|omit)\b",
        normalized,
    ):
        return "selected"
    if re.search(
        r"\b(?:all|every|each)\s+(?:the\s+)?(?:images?|photos?|videos?|clips?|media|footage)\b"
        r"|\buse\s+(?:all|everything)\b"
        r"|\b(?:all|every)\s+(?:uploaded|provided)\s+(?:media|files?|images?|photos?|videos?)\b",
        normalized,
    ):
        return "all"
    if explicit_scope_from_stated_media_count(request, manifest):
        return "all"
    if re.search(
        r"\b(?:only|just)\s+(?:the\s+)?(?:selected|specified|chosen|listed)\b"
        r"|\bselected\s+(?:media|files?|clips?)\b",
        normalized,
    ):
        return "selected"
    return "selected"


def _has_explicit_media_scope(request: str, manifest: Any | None = None) -> bool:
    normalized = " ".join(str(request or "").casefold().split())
    if re.search(
        r"\b(?:all|every|each)\s+(?:the\s+)?(?:images?|photos?|videos?|clips?|media|footage)\b"
        r"|\buse\s+(?:all|everything)\b"
        r"|\b(?:all|every)\s+(?:uploaded|provided)\s+(?:media|files?|images?|photos?|videos?)\b"
        r"|\b(?:do not|don't|dont|never|without|no)\b.{0,40}"
        r"\b(?:use|include|keep|select)\b.{0,20}"
        rf"\b(?:all|everything|every|\d{{1,4}}\s+{_MEDIA_COUNT_NOUN})\b"
        r"|\b(?:all|everything|every)\b.{0,20}\b(?:not|excluded|omit)\b"
        r"|\b(?:only|just)\s+(?:the\s+)?(?:selected|specified|chosen|listed)\b"
        r"|\bselected\s+(?:media|files?|clips?)\b",
        normalized,
    ):
        return True
    return explicit_scope_from_stated_media_count(request, manifest)


def _explicit_guided_voiceover_request(request: str, manifest: Any) -> bool:
    return bool(
        manifest.has_voiceover
        and (
            recognize_mixed_media_timing(request) is not None
            or _explicit_media_scope(request, manifest) == "all"
        )
    )


def _explicit_sfx_name(creator_request: str, *, manifest: Any | None = None) -> str | None:
    """Extract one explicitly named effect without granting catalog authority."""

    request = " ".join(str(creator_request or "").split())
    patterns = (
        r"\b(?:sound\s+effect|sfx)\s+(?:named|called|titled)\s+[\"'“‘]([^\"'”’]{1,160})[\"'”’]",
        r"\b(?:sound\s+effect|sfx)\s*[=:]\s*[\"'“‘]([^\"'”’]{1,160})[\"'”’]",
        r"[\"'“‘]([^\"'”’]{1,160})[\"'”’]\s+(?:sound\s+effect|sfx)\b",
        r"\b(?:add|use|choose|pick|place|include)\s+(?:the\s+)?"
        r"([A-Za-z0-9][A-Za-z0-9 _-]{0,159}?)\s+(?:sound\s+effect|sfx)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, request, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    if manifest is None:
        return None
    catalog_effects = sorted(
        (item for item in manifest.catalog if item.kind == "sound_effect" and item.label),
        key=lambda item: len(item.label or ""),
        reverse=True,
    )
    for effect in catalog_effects:
        label = str(effect.label or "").strip()
        label_re = rf"(?<!\w){re.escape(label)}(?!\w)"
        near_effect = (
            rf"{label_re}.{{0,80}}\b(?:sound\s+effect|sfx)\b"
            rf"|\b(?:sound\s+effect|sfx)\b.{{0,80}}{label_re}"
        )
        if re.search(near_effect, request, re.IGNORECASE):
            return label
    return None


async def _resolve_explicit_sfx_outside_manifest(
    db: AsyncSession,
    requested: str | None,
    *,
    manifest: Any,
) -> Any:
    """Add an exact live DB match to the planning view without widening the prompt catalog."""

    if not requested or resolve_creator_sfx_catalog_ref(manifest, requested) is not None:
        return manifest
    needle = " ".join(requested.split()).casefold()
    rows = (
        (
            await db.execute(
                select(SoundEffect).where(
                    or_(
                        func.lower(SoundEffect.id) == needle,
                        func.lower(SoundEffect.name) == needle,
                    ),
                    SoundEffect.status == "ready",
                    SoundEffect.published_at.is_not(None),
                    SoundEffect.archived_at.is_(None),
                    SoundEffect.audio_gcs_path.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        return manifest
    effect = rows[0]
    from app.agents._schemas.creator_agent import CreatorCatalogRef  # noqa: PLC0415

    trusted_ref = CreatorCatalogRef(
        catalog_id=str(effect.id),
        kind="sound_effect",
        label=str(effect.name)[:160],
    )
    # Preserve the original manifest hash: this exact row was resolved from a
    # user-authored name at the authenticated DB boundary, is included in the
    # plan hash, and is revalidated again immediately before materialization.
    return manifest.model_copy(update={"catalog": [*manifest.catalog, trusted_ref]})


_SECONDS_UNIT = r"(?:s|sec|secs|seconds?|saniye|segundos?|secondes?|sekunden?|secondi|secondo)\b"
_SECONDS_WORDS = {
    "half a": 0.5,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
}


def _excerpt_states_seconds(excerpt: str, seconds: float) -> bool:
    """Whether a verbatim creator excerpt states ``seconds`` as a duration."""

    text = " ".join(excerpt.casefold().split())
    for match in re.finditer(rf"(?<![\w.])(\d+(?:[.,]\d+)?)\s*-?\s*{_SECONDS_UNIT}", text):
        if abs(float(match.group(1).replace(",", ".")) - float(seconds)) < 1e-6:
            return True
    return any(
        abs(number - float(seconds)) < 1e-6 and re.search(rf"\b{word}\s*-?\s*{_SECONDS_UNIT}", text)
        for word, number in _SECONDS_WORDS.items()
    )


def _apply_explicit_render_intent(
    strategy: CreativeStrategy,
    creator_request: str,
    manifest: Any | None = None,
    latest_user_message: str = "",
    render_intent_evidence: CreatorRenderIntentEvidence | None = None,
) -> CreativeStrategy:
    """Preserve grounded semantic intent, with legacy literal extraction as fallback.

    The model interprets the creator's wording and returns typed values with
    verbatim supporting excerpts. Verify provenance and exact title copy here;
    do not require English keywords to authorize a semantic interpretation.
    The legacy recognizer remains a fallback for old or failed model responses.
    """

    request = " ".join(str(creator_request or "").split())
    semantic_updates: dict[str, object] = {}
    creator_sources = (request, " ".join(str(latest_user_message or "").split()))
    if render_intent_evidence is not None:
        for field in (
            "opening_title",
            "font_family",
            "text_color",
            "opening_title_duration_s",
            "shot_labels",
            "closing_title",
        ):
            quote = " ".join(str(getattr(render_intent_evidence, field, None) or "").split())
            if not quote or not any(quote in source for source in creator_sources):
                continue
            value = getattr(strategy, field)
            # Typography and color are semantic choices from a validated
            # schema. Literal on-screen words must also occur in the excerpt.
            if field in {"opening_title", "closing_title"} and value is not None:
                if " ".join(value.split()) not in quote:
                    continue
            if field == "shot_labels" and value is not None:
                if not all(" ".join(label.split()) in quote for label in value):
                    continue
            if field == "opening_title_duration_s" and value is not None:
                if not _excerpt_states_seconds(quote, value):
                    continue
            semantic_updates[field] = value
    # Ungrounded model values cannot become pixels. Only creator excerpts or
    # the legacy literal recognizer below can restore these fields.
    updates: dict[str, object] = {
        "opening_title": None,
        "font_family": None,
        "text_color": None,
        "opening_title_duration_s": None,
        "shot_labels": None,
        "closing_title": None,
        "image_layout": None,
        "licensed_sfx": None,
        "execution_contract": strategy.execution_contract,
        "media_scope": (
            _explicit_media_scope(creator_request, manifest)
            if _has_explicit_media_scope(creator_request, manifest)
            else strategy.media_scope
        ),
    }

    latest = " ".join(str(latest_user_message or "").casefold().split())

    # A named SFX is a required request, never an optional treatment. Resolve
    # names only by exact case-insensitive match against the server manifest;
    # an unresolved name is retained as an inert id so compilation fails
    # visibly rather than silently dropping to optional_treatments.
    sfx_name = _explicit_sfx_name(request, manifest=manifest)
    if sfx_name:
        resolved = (
            resolve_creator_sfx_catalog_ref(manifest, sfx_name) if manifest is not None else None
        )
        updates["licensed_sfx"] = {
            "effect_id": resolved.catalog_id if resolved is not None else sfx_name,
            "semantics": "funny_moments",
            "max_placements": 6,
        }

    # Creators use both ``title 'Emir Olympics'`` and the equally natural
    # ``'Emir Olympics' title``. Keep the quoted text bounded and require the
    # title noun next to it so unrelated quoted direction never reaches pixels.
    #
    # KRI-129 part E: a single message can name BOTH ends of the video
    # ("title 'A', closing title 'B'"). The old code took only the first
    # `re.search` hit across the whole request, so the second title mention
    # was silently lost. Each tier below now uses `finditer` to collect every
    # mention that tier's grammar matches, classifies EACH one by its own
    # clause prefix (negated clauses are skipped one at a time, never nulling
    # out the whole tier), and the first opening-classified match wins
    # opening_title while the first closing-classified match wins
    # closing_title. Tiers are still tried in the same priority order as
    # before, falling through only when a tier produces no usable match at
    # all -- once a tier classifies anything, later (more permissive) tiers
    # are never consulted, exactly like the old single-match fallback chain.
    def _classify_title_matches(
        pattern: re.Pattern[str],
    ) -> tuple[str | None, str | None]:
        opening_text: str | None = None
        closing_text: str | None = None
        for match in pattern.finditer(request):
            text = match.group(1).strip()
            if not text:
                continue
            clause_start = max(
                request.rfind(delimiter, 0, match.start())
                for delimiter in (".", "!", "?", ";", ",")
            )
            clause_prefix = request[clause_start + 1 : match.start()]
            if re.search(r"\b(?:do\s+not|don't|dont|without|no)\b", clause_prefix, re.IGNORECASE):
                continue
            if re.search(r"\b(?:closing|ending|end|outro)\s*$", clause_prefix, re.IGNORECASE):
                if closing_text is None:
                    closing_text = text
            elif opening_text is None:
                opening_text = text
        return opening_text, closing_text

    _title_patterns = (
        re.compile(
            r"[\"'“‘](.{1,280}?)[\"'”’]\s+(?:opening\s+)?(?:title|intro|hook|text)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:opening\s+)?(?:title|intro|hook|text)(?!\s+(?:texts|copies)\b)"
            r"\s*(?:text|copy)?\b\s*"
            r"(?:is|to|should\s+say|saying|that\s+says|which\s+says|as|:)?\s*"
            r"[\"'“‘](.{1,280}?)[\"'”’]",
            re.IGNORECASE,
        ),
        # Chat-first users commonly omit quoting for a short title. Stop at
        # the first comma or style qualifier so the rest of the request can
        # never become on-screen copy. Unlike quoted copy, an unquoted title
        # needs an explicit connector ("is", "should say", or a colon); a
        # bare "add intro text" is a treatment directive, not literal pixels.
        re.compile(
            r"\b(?:opening\s+)?(?:title|intro|hook)(?!\s+(?:texts|copies)\b)"
            r"\s*(?:text|copy)?\b\s*"
            r"(?:is|to|should\s+say|saying|that\s+says|which\s+says|as|:)\s*"
            r"([A-Za-z0-9][^,\n]{0,279}?)(?=\s*(?:,|$)|\s+(?:using|with|font|colou?r)\b"
            r"|\s+(?:use|make|set)\b(?=[^.]{0,80}\b(?:font|text|colou?r)\b))",
            re.IGNORECASE,
        ),
        # The legacy title grammar permits multi-sentence literal copy. Keep
        # that contract intact; the newer unquoted "text saying" fallback
        # stops at punctuation before a following style sentence.
        re.compile(
            r"\b(?:opening\s+)?(?:text)(?!\s+(?:texts|copies)\b)"
            r"\s*(?:text|copy)?\b\s*"
            r"(?:is|to|should\s+say|saying|that\s+says|which\s+says|as|:)\s*"
            r"([A-Za-z0-9][^,.;!?\n]{0,279}?)(?=\s*(?:[,.;!?]|$)|\s+(?:using|with|font|colou?r)\b"
            r"|\s+(?:use|make|set)\b(?=[^.]{0,80}\b(?:font|text|colou?r)\b))",
            re.IGNORECASE,
        ),
    )
    for pattern in _title_patterns:
        opening_title_text, closing_title_text = _classify_title_matches(pattern)
        if opening_title_text is not None or closing_title_text is not None:
            if opening_title_text is not None:
                updates["opening_title"] = opening_title_text
            if closing_title_text is not None:
                updates["closing_title"] = closing_title_text
            break

    from app.agents._schemas.text_element import _ALLOWED_FONTS  # noqa: PLC0415

    # Match the canonical registry names directly (longest first), rather
    # than greedily capturing prose such as “using Rascal” as the font.
    for name in sorted(_ALLOWED_FONTS, key=len, reverse=True):
        escaped = re.escape(name)
        if re.search(
            rf"(?<!\w){escaped}(?!\w)\s+font\b|"
            rf"\b(?:font|typeface)\s*(?:is|to|:)?\s*{escaped}(?!\w)|"
            # KRI-129 part D: "use Impact for the font" was previously
            # dropped -- the name and the word "font" are both present but
            # neither of the two patterns above is adjacent-word shaped.
            rf"(?<!\w){escaped}(?!\w)\s+for\s+(?:the\s+)?(?:font|typeface)\b|"
            rf"(?<!\w){escaped}(?!\w)(?=\s*(?:[,.;!?]|$))",
            request,
            re.IGNORECASE,
        ):
            updates["font_family"] = name
            break

    color_match = re.search(
        r"\b(?:text\s+)?colou?r\s*(?:is|to|:)?\s*(#[0-9A-Fa-f]{6}|(?:pastel\s+yellow|[A-Za-z]+))\b"
        r"|\b(?:make|set)\s+(?:the\s+)?(?:text|title|intro|it)\s+"
        r"(#[0-9A-Fa-f]{6}|pastel\s+yellow|yellow|gold|white|black)\b"
        r"|\b(pastel\s+yellow|yellow|gold|white|black)\s+(?:text|title|intro)\b",
        # Also accept a comma-delimited style list such as
        # "title Emir Olympics, Rascal, yellow".
        # The bounded vocabulary keeps arbitrary adjectives out.
        request,
        re.IGNORECASE,
    )
    if color_match is None:
        color_match = re.search(
            r"\b(pastel\s+yellow|yellow|gold|white|black)(?=\s*(?:[,.;!?]|$))",
            request,
            re.IGNORECASE,
        )
    if color_match:
        updates["text_color"] = normalize_creator_text_color(
            color_match.group(1) or color_match.group(2) or color_match.group(3)
        )

    updates["image_layout"] = recognize_image_layout(request)

    mixed_media_timing = recognize_mixed_media_timing(request)
    if mixed_media_timing is None:
        # The existing PlanItem contract represents the fastest supported
        # still-image cadence as ``very_fast`` (0.5-0.8s). Creators often ask
        # for that intent numerically, as in "photos ... 0.1 seconds", without
        # also spelling out "hold videos longer". Requiring both phrases left
        # valid pool images outside the guided proposal while the assistant
        # still promised a photo sequence. Promote only an affirmative,
        # sub-second photo request that explicitly places photos among videos.
        normalized = request.casefold()
        photo_subsecond = bool(
            re.search(
                r"\b(?:photos?|images?|stills?|pictures?)\b.{0,80}"
                r"\b0?\.[0-9]+\s*(?:s|sec(?:ond)?s?)\b",
                normalized,
            )
            or re.search(
                r"\b(?:photos?|images?|stills?|pictures?)\b.{0,80}"
                r"\b[1-9][0-9]{0,2}\s*(?:ms|milliseconds?)\b",
                normalized,
            )
        )
        has_video_context = bool(re.search(r"\b(?:videos?|clips?|footage)\b", normalized))
        timing_negated = bool(
            re.search(
                r"\b(?:do not|don't|dont|never|not)\b.{0,40}"
                r"\b(?:photos?|images?|stills?|pictures?)\b",
                normalized,
            )
        )
        if photo_subsecond and has_video_context and not timing_negated:
            numeric_match = re.search(
                r"\b(?:photos?|images?|stills?|pictures?)\b.{0,80}?"
                r"\b(0?\.[0-9]+|[1-9][0-9]{0,2})\s*"
                r"(?:s|sec(?:ond)?s?|ms|milliseconds?)\b",
                normalized,
            )
            image_hold_s = None
            if numeric_match:
                image_hold_s = float(numeric_match.group(1))
                if "ms" in numeric_match.group(0) or "millisecond" in numeric_match.group(0):
                    image_hold_s /= 1000
                if not 0.1 <= image_hold_s <= 0.8:
                    image_hold_s = None
            mixed_media_timing = MixedMediaTimingProfile(
                image_hold="very_fast",
                image_hold_s=image_hold_s,
                video_hold="longer",
                boundary_style="cut",
            )
    updates["mixed_media_timing"] = mixed_media_timing
    if latest and _has_explicit_media_scope(latest, manifest):
        updates["media_scope"] = _explicit_media_scope(latest, manifest)
    if manifest is not None and _explicit_guided_voiceover_request(creator_request, manifest):
        guided_voiceover = manifest.capabilities.get(CAPABILITY_GUIDED_VOICEOVER)
        if guided_voiceover is not None and guided_voiceover.available:
            updates["execution_contract"] = GUIDED_VOICEOVER_EXECUTION_CONTRACT
            updates["render_program"] = "guided"
    if mixed_media_timing is not None:
        # A rapid still sequence among longer video moments is an exact-cut
        # montage contract. Keep the guided render program (so pool photos are
        # first-class media), but send the specialist through its source-aware
        # fast-cut schema instead of asking a story-beat plan to express
        # sub-second image timing it cannot represent.
        updates["direction"] = "fast_montage"

    if re.search(r"\b(?:fast|snappy|quick)\s+(?:pace|pacing)\b", request, re.IGNORECASE):
        updates["pacing"] = "fast"
    elif re.search(r"\b(?:relaxed|slow|calm)\s+(?:pace|pacing)\b", request, re.IGNORECASE):
        updates["pacing"] = "relaxed"
    elif re.search(r"\bbalanced\s+(?:pace|pacing)\b", request, re.IGNORECASE):
        updates["pacing"] = "balanced"

    target_duration_s = recognize_total_duration_s(request)
    if target_duration_s is None and manifest is not None:
        target_duration_s = _pinned_narration_target_duration_s(manifest)
    if target_duration_s is not None:
        updates["target_duration_s"] = target_duration_s
    updates.update(semantic_updates)
    return CreativeStrategy.model_validate({**strategy.model_dump(mode="json"), **updates})


def _requests_preserved_clip_order(creator_request: str) -> bool:
    """Recognize an explicit request to keep the current source sequence."""

    normalized = " ".join(creator_request.casefold().split())
    return bool(
        re.search(
            r"\b(?:keep|preserve|maintain)\b.{0,80}\b(?:clip\s+)?(?:order|sequence)\b",
            normalized,
        )
        or re.search(r"\b(?:same|unchanged)\s+(?:clip\s+)?(?:order|sequence)\b", normalized)
    )


def _source_basename(path: str) -> str:
    return str(path or "").rsplit("/", 1)[-1]


def _source_matches_item_path(previous_path: str, item_path: str) -> bool:
    """Match durable Job snapshots back to their owner-scoped upload paths."""

    previous_name = _source_basename(previous_path)
    item_name = _source_basename(item_path)
    return previous_name == item_name or previous_name.split("_", 1)[-1] == item_name


async def _previous_creator_clip_order(
    db: AsyncSession,
    item: PlanItem,
    session: CreatorAgentSession,
    creator_request: str,
) -> list[int] | None:
    """Resolve the prior rendered first-appearance order for an explicit preserve request."""

    if not _requests_preserved_clip_order(creator_request) or not item.current_job_id:
        return None
    previous_job = await db.get(Job, item.current_job_id)
    if previous_job is None or previous_job.user_id != session.creator_id:
        return None
    if (
        previous_job.content_plan_item_id != item.id
        or previous_job.status not in PLAN_ITEM_JOB_READY
    ):
        return None
    previous_paths = list((previous_job.all_candidates or {}).get("clip_paths") or [])
    variants = list((previous_job.assembly_plan or {}).get("variants") or [])
    target_variant_id = session.target_variant_id
    variant = next(
        (
            value
            for value in variants
            if isinstance(value, dict)
            and (target_variant_id is None or value.get("variant_id") == target_variant_id)
            and value.get("render_status") == "ready"
        ),
        None,
    )
    # The editor's user_timeline is authoritative when present; ai_timeline is
    # only the fallback for cuts that have not been manually rearranged.
    raw_timeline = (variant or {}).get("user_timeline") or (variant or {}).get("ai_timeline")
    timeline = raw_timeline if isinstance(raw_timeline, dict) else {}
    slots = [slot for slot in timeline.get("slots") or [] if isinstance(slot, dict)]
    if not previous_paths or not slots:
        return None
    item_paths = list(item.clip_gcs_paths or [])
    order: list[int] = []

    def _slot_order(value: dict) -> int:
        try:
            return int(value.get("order", 0) or 0)
        except (TypeError, ValueError):
            return 0

    for slot in sorted(slots, key=_slot_order):
        try:
            previous_index = int(slot["clip_index"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= previous_index < len(previous_paths):
            continue
        previous_path = str(previous_paths[previous_index])
        item_index = next(
            (
                index
                for index, item_path in enumerate(item_paths)
                if _source_matches_item_path(previous_path, str(item_path))
            ),
            None,
        )
        if item_index is not None and item_index not in order:
            order.append(item_index)
    return order or None


def _latest_cadence_question(events: list[CreatorAgentEvent]) -> dict[str, Any] | None:
    ordered = sorted(events, key=lambda value: value.sequence)
    if ordered and ordered[-1].role == "user":
        ordered = ordered[:-1]
    if not ordered or ordered[-1].event_type != "assistant_question":
        return None
    payload = ordered[-1].payload if isinstance(ordered[-1].payload, dict) else {}
    context = payload.get("cadence_context")
    return context if isinstance(context, dict) else None


def _latest_all_media_capacity_question(events: list[CreatorAgentEvent]) -> dict[str, Any] | None:
    """Return the active, hash-fenced all-media choice when one is pending."""

    ordered = sorted(events, key=lambda value: value.sequence)
    if ordered and ordered[-1].role == "user":
        ordered = ordered[:-1]
    if not ordered or ordered[-1].event_type != "assistant_question":
        return None
    payload = ordered[-1].payload if isinstance(ordered[-1].payload, dict) else {}
    if payload.get("reason_code") != "all_media_capacity":
        return None
    context = payload.get("all_media_capacity")
    return context if isinstance(context, dict) else None


def _all_media_capacity_choice(
    context: dict[str, Any], message: str, manifest: Any
) -> CreativeStrategy | None:
    """Resolve only an exact displayed option against its manifest fence."""

    if context.get("manifest_hash") != manifest.manifest_hash:
        return None
    normalized = " ".join(message.casefold().split())
    for mapping in context.get("option_mappings") or []:
        if not isinstance(mapping, dict):
            continue
        option = " ".join(str(mapping.get("option") or "").casefold().split())
        strategy = mapping.get("strategy")
        if option and normalized == option and isinstance(strategy, dict):
            try:
                return CreativeStrategy.model_validate(strategy)
            except ValueError:
                return None
    return None


def _strongest_guided_subset_ids(
    manifest: Any, target_duration_s: int | float, *, min_moment_s: float
) -> list[str]:
    """Pick a stable renderable subset for the capacity recommendation."""

    limit = min(GUIDED_STORY_MAX_MEDIA, max(1, math.floor(float(target_duration_s) / min_moment_s)))

    def energy(ref: Any) -> float:
        values = [
            float(moment.get("energy", 0))
            for moment in (getattr(ref, "analysis", {}) or {}).get("best_moments", [])
            if isinstance(moment, dict) and isinstance(moment.get("energy", 0), (int, float))
        ]
        return max(values, default=0.0)

    # A short clip is renderable at its own length (it is not "unusable"); it
    # must never be excluded from the strength-ranked recommendation for
    # being short. Ranking by strength is fine, excluding for shortness is
    # not. Allowlist kinds the way `_attached_media_count` does rather than
    # `kind != "image"`, so e.g. "audio" media never enters this ranking.
    eligible = [ref for ref in manifest.media if ref.kind in {"video", "image"}]
    return [
        ref.media_id
        for _index, ref in sorted(enumerate(eligible), key=lambda row: (-energy(row[1]), row[0]))[
            :limit
        ]
    ]


def _all_media_capacity_question(
    manifest: Any,
    strategy: CreativeStrategy,
    *,
    requested_duration_is_explicit: bool = True,
) -> dict[str, Any] | None:
    """Create the deterministic choice required before an infeasible all-media plan."""

    if strategy.media_scope != "all":
        return None
    min_moment_s = 1.8 if strategy.direction == "text_explainer" else 1.4
    capacity = assess_all_media_capacity(
        manifest.media,
        strategy.target_duration_s,
        story_min_moment_s=min_moment_s,
        mixed_media_timing=strategy.mixed_media_timing,
    )
    current_strategy_feasible = (
        capacity.current_fast_target_feasible
        if strategy.direction == "fast_montage"
        else capacity.guided_feasible
    )
    if current_strategy_feasible:
        return None
    if any(
        ref.kind == "video" and ref.duration_s is None
        for ref in getattr(manifest, "media", None) or ()
    ):
        # A clip whose length is not known yet (registered, analysis still
        # running) makes capacity UNDECIDABLE, not infeasible. Asking here told
        # creators "all 2 clips cannot fit" a 20-second edit, which was false.
        return None
    # The creator agent chose this duration from the footage. Capacity math may
    # decide whether that editorial choice is renderable, but it must never
    # replace it with a clip-count-derived duration.
    subset_target_s = strategy.target_duration_s
    subset_ids = _strongest_guided_subset_ids(manifest, subset_target_s, min_moment_s=min_moment_s)
    if not subset_ids:
        return None
    recommended = (
        f"Keep {strategy.target_duration_s:g} seconds with the strongest clips"
        if requested_duration_is_explicit
        else f"Use the strongest {len(subset_ids)} clips in a {subset_target_s:g}-second edit"
    )
    base_strategy = strategy.model_dump(mode="json")
    mappings: list[dict[str, Any]] = [
        {
            "option": recommended,
            "strategy": {
                **base_strategy,
                "direction": strategy.direction,
                "media_scope": "selected",
                "selected_media_ids": subset_ids,
                "target_duration_s": subset_target_s,
            },
        }
    ]
    if capacity.current_fast_target_feasible:
        include_all = (
            f"Keep {strategy.target_duration_s:g} seconds and include everything with faster pacing"
        )
        mappings.append(
            {
                "option": include_all,
                "strategy": {
                    **base_strategy,
                    "direction": "fast_montage",
                    "media_scope": "all",
                    "selected_media_ids": [ref.media_id for ref in manifest.media],
                    "target_duration_s": strategy.target_duration_s,
                },
            }
        )
    return {
        "message": (
            (
                f"This edit cannot show all {len(manifest.media)} clips in "
                f"{strategy.target_duration_s:g} seconds. I recommend keeping the duration "
                "and using the strongest clips."
                if requested_duration_is_explicit
                else (
                    f"Based on the footage, I proposed {strategy.target_duration_s:g} seconds, "
                    f"but all {len(manifest.media)} clips cannot fit at that storytelling pace. "
                    "I recommend preserving the pace and using the strongest clips."
                )
            )
            + (
                " Or I can preserve that duration and include everything with faster pacing."
                if len(mappings) > 1
                else (
                    " Including every clip would exceed the safe two-minute limit or include "
                    "an unusable clip, so I will remove weaker clips."
                )
            )
        ),
        "reason_code": "all_media_capacity",
        "options": [mapping["option"] for mapping in mappings],
        "recommended_option": recommended,
        "all_media_capacity": {
            "manifest_hash": manifest.manifest_hash,
            "requested_duration_s": (
                strategy.target_duration_s if requested_duration_is_explicit else None
            ),
            "guided_max_media": GUIDED_STORY_MAX_MEDIA,
            "required_fast_duration_s": capacity.required_fast_duration_s,
            "option_mappings": mappings,
        },
    }


HistoricalCapacityChoiceKind = Literal["keep_everything", "strongest_subset"]


def _historical_all_media_capacity_choice_kind(
    events: list[CreatorAgentEvent], manifest: Any
) -> HistoricalCapacityChoiceKind | None:
    """Classify the creator's last real answer to an all-media capacity ask.

    ``_latest_all_media_capacity_question`` only resolves a choice when the
    pending question is still the newest event -- once another turn moves
    the conversation on, that lookup goes dark. By the time the question
    budget is exhausted, the creator has very likely already answered this
    exact question earlier in the session, and that answer must still hold
    rather than being silently replaced by a "strongest clips" default.

    KRI-129: this used to return the historical answer's full
    ``CreativeStrategy`` -- including ITS OWN ``target_duration_s``,
    ``direction``, and ``selected_media_ids`` -- verbatim. Those values were
    recorded against a possibly stale question (a different requested
    duration, an earlier manifest state) and a newer, explicit instruction in
    the same turn ("actually keep it at 6 seconds") would be silently
    discarded in favor of the OLD ones. History can only tell us WHICH KIND
    of option the creator prefers -- "keep everything" (the include-
    everything / faster-pacing mapping) or "strongest subset" (the
    strength-ranked cut) -- never the values. The caller re-applies that kind
    against the mapping computed fresh for the CURRENT turn.
    """

    ordered = sorted(events, key=lambda value: value.sequence)
    latest_kind: HistoricalCapacityChoiceKind | None = None
    for index, event in enumerate(ordered):
        if event.event_type != "assistant_question":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("reason_code") != "all_media_capacity":
            continue
        context = payload.get("all_media_capacity")
        if not isinstance(context, dict):
            continue
        reply = next((later for later in ordered[index + 1 :] if later.role == "user"), None)
        if reply is None:
            continue
        reply_payload = reply.payload if isinstance(reply.payload, dict) else {}
        message = str(reply_payload.get("message") or "")
        choice = _all_media_capacity_choice(context, message, manifest)
        if choice is None:
            continue
        latest_kind = (
            "keep_everything"
            if choice.media_scope == "all" and choice.direction == "fast_montage"
            else "strongest_subset"
        )
    return latest_kind


def _latest_message_narrows_media_scope(user_message: str, manifest: Any) -> bool:
    """True when the creator's NEWEST words themselves narrow the scope.

    A historical "keep everything"/"strongest subset" preference must never
    override an explicit, newer instruction that says otherwise (KRI-129 part
    1): e.g. history says "keep everything", but the latest message says
    "just use the best ones" or explicitly asks for a selected subset. The
    newest explicit words always win. Deliberately checked against
    ``user_message`` alone (this turn's literal words), never the
    turn-accumulating ``creator_request``, which would make yesterday's
    narrowing cue "contradict" history forever.
    """

    if states_explicit_media_narrowing_cue(user_message):
        return True
    return (
        _has_explicit_media_scope(user_message, manifest)
        and _explicit_media_scope(user_message, manifest) == "selected"
    )


def _capacity_history_kind_for_turn(
    events: list[CreatorAgentEvent], user_message: str, manifest: Any
) -> HistoricalCapacityChoiceKind | None:
    """Apply only a real historical choice to the current capacity question."""

    historical_kind = _historical_all_media_capacity_choice_kind(events, manifest)
    if _latest_message_narrows_media_scope(user_message, manifest):
        return None
    # A newer explicit all-media instruction rejects an older subset choice,
    # but it does not manufacture a prior keep-everything answer.  With no
    # valid historical preference, the normal capacity question remains.
    if (
        historical_kind == "strongest_subset"
        and _has_explicit_media_scope(user_message, manifest)
        and _explicit_media_scope(user_message, manifest) == "all"
    ):
        return None
    return historical_kind


def _latest_planned_cadence(
    events: list[CreatorAgentEvent],
) -> tuple[MontageCadenceConstraint | None, int | float | None]:
    """Recover the last accepted typed cadence after active-plan reset."""

    for event in sorted(events, key=lambda value: value.sequence, reverse=True):
        if event.event_type != "assistant_strategy":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        try:
            cadence = MontageCadenceConstraint.model_validate(payload["montage_cadence"])
        except (KeyError, ValueError):
            return None, None
        target_s = payload.get("target_duration_s")
        return cadence, target_s if isinstance(target_s, (int, float)) else None
    return None, None


def _cadence_was_cancelled(events: list[CreatorAgentEvent]) -> bool:
    """Return the latest explicit cadence-vs-cancellation intent in history."""

    for event in sorted(events, key=lambda value: value.sequence, reverse=True):
        if event.role != "user":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        message = str(payload.get("message") or "")
        if rejects_round_robin_cadence(message):
            return True
        if recognize_round_robin_cadence(message) is not None:
            return False
    return False


async def _record_required_cadence_question(
    db: AsyncSession,
    session: CreatorAgentSession,
    *,
    payload: dict[str, Any],
) -> None:
    """Ask an essential cadence question without overrunning the session budget."""

    if session.question_count >= session.question_budget:
        session.status = "failed"
        session.last_error = {"code": "question_budget_exhausted"}
        await append_event(
            db,
            session,
            event_type="assistant_error",
            payload={"message": "This edit needs a fresh creator session."},
        )
        return
    session.status = "briefing"
    session.question_count += 1
    await append_event(db, session, event_type="assistant_question", payload=payload)


async def _record_cadence_unavailable(
    db: AsyncSession,
    session: CreatorAgentSession,
    exc: MontageCadenceUnavailableError,
) -> None:
    session.status = "failed"
    session.last_error = {
        "code": "montage_cadence_unavailable",
        "message": str(exc)[:300],
    }
    await append_event(
        db,
        session,
        event_type="assistant_error",
        payload={
            "message": (
                "I can't preserve that exact alternating cadence with the current "
                "audio setup. No fallback edit was rendered. Ask Kria to remove the "
                "voiceover or change the edit direction."
            ),
            "code": "montage_cadence_unavailable",
        },
    )


_PHONE_VOICEOVER_UNAVAILABLE_MESSAGE = (
    "A voiceover can't render on your iPhone yet, and this project's videos render "
    "on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered."
)


def _phone_media_unavailable_message(exc: PhoneMediaUnavailableError) -> str:
    """Name only the limits that still apply for the verified Visuals kinds."""

    if exc.still_images_available and exc.visual_videos_available:
        limit = (
            "It can use photos and videos from Visuals, but a photo can't supply "
            "sound or cut timing."
        )
    elif exc.still_images_available:
        limit = (
            "It can show photos from Visuals, but videos from Visuals can't be used, "
            "and a photo can't supply sound or cut timing."
        )
    elif exc.visual_videos_available:
        limit = "It can use videos from Visuals, but photos from Visuals can't be used."
    else:
        limit = (
            "It can only use the videos attached to this project, not photos or "
            "videos from Visuals."
        )
    return f"This edit renders on your iPhone. {limit} No fallback edit was rendered."


async def _record_media_unavailable(
    db: AsyncSession,
    session: CreatorAgentSession,
    exc: MixedMediaTimingUnavailableError,
) -> None:
    """Fail the turn with honest copy when no renderer can honor the media or format."""

    # The phone subclasses are checked before their mixed-media parent: the
    # iPhone refusing Visuals media, a format or a voiceover is not a
    # photo/video timing outage.
    if isinstance(exc, PhoneMediaUnavailableError):
        code = "phone_media_unavailable"
        message = _phone_media_unavailable_message(exc)
    elif isinstance(exc, PhoneFormatUnavailableError):
        code = "phone_voiceover_unavailable" if exc.voiceover else "phone_format_unavailable"
        message = (
            _PHONE_VOICEOVER_UNAVAILABLE_MESSAGE
            if exc.voiceover
            # KRI-132: this copy used to hard-code "Only Montage videos" --
            # no longer true now that `subtitled` (talking-to-camera) and the
            # `narrated*` formats can also render on the phone once rolled
            # out (see `app.services.phone_rollout.phone_render_
            # supported_formats`, the single source of truth for what's
            # actually enabled). Kept deliberately format-agnostic here
            # rather than naming the current allowlist, since that set
            # changes with rollout flags and this string does not -- mirrors
            # `PHONE_GATE_MESSAGES["unsupported_format"]` in
            # content_plan_build.py (deliberately duplicated, not imported;
            # this module sits above that one in the import graph).
            else "This kind of video can't render on your iPhone right now. Choose a format "
            "that renders on this iPhone (Montage, or Talking to camera / Narrated "
            "where available). No fallback edit was rendered."
        )
    else:
        code = "mixed_media_timing_unavailable"
        message = (
            "Mixed photo and video timing is temporarily unavailable. "
            "No fallback edit was rendered."
        )
    session.status = "failed"
    session.last_error = {"code": code, "message": str(exc)[:300]}
    await append_event(
        db,
        session,
        event_type="assistant_error",
        payload={"message": message, "code": code},
    )


def _dispatch_unavailable_message(manifest: Any) -> str:
    """A phone project that cannot dispatch is not missing a clip; say why."""

    phone = manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
    if phone is None or phone.available:
        return "Add at least one clip first, then I can design the edit around it."
    if phone.reason_code == "unsupported_phone_audio":
        return (
            "A voiceover can't render on your iPhone yet, and this project's videos "
            "render on this iPhone. Remove the voiceover and I can design the edit."
        )
    if phone.reason_code == "unverified_phone_sources":
        return (
            "I couldn't verify this project's iPhone footage, so it can't render on "
            "this iPhone yet. Reconnect its original footage, then try again."
        )
    return (
        "Rendering on your iPhone is temporarily unavailable, and this project's "
        "videos render there. Your project is saved; try again later."
    )


def _enqueue_creator_clip_metadata(item: PlanItem, plan: ContentPlan) -> None:
    """Best-effort metadata extraction; creator planning never waits on it."""

    from app.tasks.creator_clip_metadata import (  # noqa: PLC0415
        CREATOR_CLIP_METADATA_QUEUE,
        analyze_creator_clip_metadata,
    )

    try:
        analyze_creator_clip_metadata.apply_async(
            args=[str(item.id), int(getattr(plan, "ownership_epoch", 0) or 0)],
            queue=CREATOR_CLIP_METADATA_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001 - the question remains a safe failure
        log.warning(
            "main_creator.clip_metadata_dispatch_failed",
            plan_item_id=str(item.id),
            error=str(exc)[:240],
        )


async def _record_cadence_duration_unavailable(
    db: AsyncSession,
    session: CreatorAgentSession,
    *,
    cadence: MontageCadenceConstraint,
    target_duration_s: int | float | None = None,
) -> None:
    await _record_required_cadence_question(
        db,
        session,
        payload={
            "message": (
                "I couldn't verify both clip lengths yet, so I won't guess at the "
                "alternating cut plan. Try again after the uploads finish processing."
            ),
            "reason_code": "cadence_duration_unavailable",
            "options": ["Try again"],
            "recommended_option": "Try again",
            "cadence_context": {
                "kind": "duration_unavailable",
                "cadence": cadence.model_dump(mode="json"),
                # Keep these flattened fields readable by rolling clients and
                # historical debugging tools while the typed cadence remains
                # the authoritative retry contract.
                "cut_duration_s": cadence.cut_duration_s,
                "source_media_ids": list(cadence.source_media_ids),
                "target_duration_s": target_duration_s,
            },
        },
    )


def _balanced_duration_s(*, limit_s: float, cycle_s: float) -> int | float:
    """Find the longest target made of complete cadence cycles."""

    cycles = math.floor((min(limit_s, MAX_PROPOSAL_DURATION_S) + 0.001) / cycle_s)
    duration_s = round(cycles * cycle_s, 6)
    return (
        (int(duration_s) if duration_s % 1 == 0 else duration_s)
        if 3 <= duration_s <= MAX_PROPOSAL_DURATION_S
        else 0
    )


def _next_balanced_duration_s(*, minimum_s: float, limit_s: float, cycle_s: float) -> int | float:
    """Find the shortest complete-cycle target at or above a lower bound."""

    cycles = max(1, math.ceil((max(3, minimum_s) - 0.001) / cycle_s))
    duration_s = round(cycles * cycle_s, 6)
    return (
        (int(duration_s) if duration_s % 1 == 0 else duration_s)
        if 3 <= duration_s <= min(limit_s, MAX_PROPOSAL_DURATION_S) + 0.001
        else 0
    )


def _selected_cadence_sources(
    context: dict[str, Any], manifest: Any, user_message: str
) -> list[str] | None:
    selections = context.get("selections")
    if not isinstance(selections, dict):
        return None
    normalized_message = " ".join(user_message.casefold().split())
    selected = next(
        (
            ids
            for label, ids in selections.items()
            if " ".join(str(label).casefold().split()) == normalized_message
        ),
        None,
    )
    if isinstance(selected, list) and len(selected) == 2:
        return [str(value) for value in selected]

    matches: list[tuple[int, int, str]] = []
    for media in manifest.media:
        if media.kind != "video":
            continue
        for name in (media.media_id, media.label):
            normalized_name = " ".join(str(name or "").casefold().split())
            if not normalized_name:
                continue
            start = normalized_message.find(normalized_name)
            if start >= 0:
                matches.append((start, start + len(normalized_name), media.media_id))
    non_overlapping: list[tuple[int, int, str]] = []
    for match in sorted(matches, key=lambda value: (-(value[1] - value[0]), value[0])):
        if any(
            match[0] < selected_match[1] and match[1] > selected_match[0]
            for selected_match in non_overlapping
        ):
            continue
        non_overlapping.append(match)
    mentioned = [media_id for _start, _end, media_id in sorted(non_overlapping)]
    return mentioned if len(mentioned) == 2 else None


def _resolved_cadence_for_turn(
    *,
    events: list[CreatorAgentEvent],
    manifest: Any,
    creator_request: str,
    user_message: str,
) -> tuple[MontageCadenceConstraint | None, int | float | None]:
    if rejects_round_robin_cadence(user_message):
        return None, None
    prior = _latest_cadence_question(events)
    if prior:
        if prior.get("kind") == "source_selection":
            selected = _selected_cadence_sources(prior, manifest, user_message)
            if selected is not None:
                return (
                    MontageCadenceConstraint(
                        source_media_ids=selected,
                        cut_duration_s=float(prior.get("cut_duration_s") or 1.0),
                        reuse_policy=str(prior.get("reuse_policy") or "no_repeat"),
                    ),
                    None,
                )
        try:
            cadence = MontageCadenceConstraint.model_validate(prior["cadence"])
        except (KeyError, ValueError):
            cadence = None
        if cadence is not None:
            if prior.get("kind") == "duration_unavailable":
                return (
                    cadence,
                    recognize_total_duration_s(user_message)
                    or float(prior.get("target_duration_s") or 0)
                    or recognize_total_duration_s(creator_request)
                    or None,
                )
            recommendation = str(prior.get("recommendation") or "")
            requested_s = float(prior.get("requested_duration_s") or 24)
            recommended_s = float(prior.get("recommended_duration_s") or 0)
            normalized = " ".join(user_message.casefold().split())
            if recommendation and normalized == recommendation.casefold():
                if recognize_cadence_reuse_policy(user_message) == "allow_repeat":
                    return (
                        cadence.model_copy(update={"reuse_policy": "allow_repeat"}),
                        requested_s,
                    )
                return cadence, recommended_s
            if recognize_cadence_reuse_policy(user_message) == "allow_repeat":
                return cadence.model_copy(update={"reuse_policy": "allow_repeat"}), requested_s
            target_match = re.search(
                r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", normalized
            )
            if target_match:
                return cadence, float(target_match.group(1))

    current_cut_duration_s = recognize_round_robin_cadence(user_message)
    if current_cut_duration_s is None:
        if _cadence_was_cancelled(events):
            return None, None
        planned_cadence, planned_target_s = _latest_planned_cadence(events)
        if planned_cadence is not None:
            return (
                planned_cadence,
                recognize_total_duration_s(user_message) or planned_target_s,
            )

    cut_duration_s = current_cut_duration_s or recognize_round_robin_cadence(creator_request)
    if cut_duration_s is None:
        return None, None
    videos = [media for media in manifest.media if media.kind == "video"]
    if len(videos) != 2:
        return None, None
    reuse_policy = recognize_explicit_cadence_reuse_policy(
        user_message
    ) or recognize_cadence_reuse_policy(creator_request)
    return (
        MontageCadenceConstraint(
            source_media_ids=[media.media_id for media in videos],
            cut_duration_s=cut_duration_s,
            reuse_policy=reuse_policy,
        ),
        recognize_total_duration_s(user_message) or recognize_total_duration_s(creator_request),
    )


MAIN_CREATOR_FALLBACK_SUMMARY = (
    "The planner response was incomplete; review this draft before confirming."
)


def _pinned_narration_target_duration_s(manifest: Any) -> float | None:
    narration = getattr(manifest, "narration", None)
    if not getattr(manifest, "has_voiceover", False) or narration is None:
        return None
    duration_s = float(narration.duration_s)
    max_duration_s = int(
        getattr(
            getattr(manifest, "limits", None),
            "max_output_duration_s",
            MAX_PROPOSAL_DURATION_S,
        )
    )
    return max(3, min(max_duration_s, duration_s))


def _fallback_strategy(manifest: Any, *, user_message: str = "") -> CreativeStrategy:
    """Build a compiler-safe draft from pinned state and explicit request facts only."""

    current_format = manifest.edit_format
    current_available = manifest.capabilities.get(f"edit_format:{current_format}")
    safe_format = current_format if current_available and current_available.available else "montage"
    mixed_media_timing = recognize_mixed_media_timing(user_message)
    media_scope = (
        _explicit_media_scope(user_message, manifest)
        if _has_explicit_media_scope(user_message, manifest)
        else None
    )
    guided_voiceover = manifest.capabilities.get(CAPABILITY_GUIDED_VOICEOVER)
    guided_draft = manifest.capabilities.get("draft_guided_proposal")
    uses_guided_voiceover = bool(
        manifest.has_voiceover
        and getattr(manifest, "narration", None) is not None
        and (mixed_media_timing is not None or media_scope == "all")
        and guided_voiceover is not None
        and guided_voiceover.available
    )
    render_program = manifest.render_program
    if uses_guided_voiceover or (media_scope == "all" and guided_draft and guided_draft.available):
        render_program = "guided"

    if manifest.has_voiceover:
        audio_strategy = "voiceover"
    else:
        audio_strategy = (
            "licensed_music"
            if any(ref.kind == "music" for ref in getattr(manifest, "catalog", []))
            else "original_audio"
        )

    requested_duration_s = recognize_total_duration_s(user_message)
    if requested_duration_s is None:
        requested_duration_s = _pinned_narration_target_duration_s(manifest)
    max_duration_s = int(
        getattr(
            getattr(manifest, "limits", None),
            "max_output_duration_s",
            MAX_PROPOSAL_DURATION_S,
        )
    )
    target_duration_s = max(3, min(max_duration_s, requested_duration_s or 24))

    return CreativeStrategy(
        direction="fast_montage" if render_program == "guided" else "native",
        edit_format=safe_format,
        audio_strategy=audio_strategy,
        execution_contract=(GUIDED_VOICEOVER_EXECUTION_CONTRACT if uses_guided_voiceover else None),
        media_scope=media_scope,
        pacing="balanced",
        target_duration_s=target_duration_s,
        render_program=render_program,
        mixed_media_timing=mixed_media_timing,
        selected_media_ids=(
            []
            if render_program == "guided"
            else [
                media.media_id
                for media in manifest.media
                if not media.media_id.startswith("asset-")
            ][:MAX_MAIN_CREATOR_SELECTED_MEDIA]
        ),
        # A failed planner must not fabricate title, hook, or story copy.
        rationale=(
            "Planner response incomplete. Preserve the uploaded media and "
            "explicit request for creator review."
        ),
    )


def _strict_creator_format(edit_format: str) -> bool:
    """Formats that must fail visibly instead of falling back to montage."""

    return edit_format in {
        "day_vlog",
        "single_hero",
        "subtitled",
        "narrated",
        "narrated_planned",
        "narrated_ready",
    }


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
    allow_chat: bool = False,
    usage_purpose: str | None = None,
    test_run_id: str | None = None,
    estimated_max_cost_usd: float | None = None,
    reservation_approved: bool = False,
    release_canary_id: str | None = None,
    previous_active_plan: dict[str, Any] | None = None,
    preparation_attempt_id: str | None = None,
    preparation_token: str | None = None,
) -> CreatorSessionResponse:
    item, plan, persona = await _owned_context(db, item_id, user.id)
    session = await _load_session(db, session_id, user.id, item.id)
    from app.services.creator_preparation import maybe_prepare, require_current_attempt

    prepared_attempt = None
    if preparation_attempt_id:
        prepared_attempt = await require_current_attempt(
            db, preparation_attempt_id, preparation_token or ""
        )
        await db.commit()
    elif settings.creator_clip_preparation_enabled:
        item, plan, persona = await _owned_context(db, item_id, user.id, for_update=True)
        session = await _load_session(db, session_id, user.id, item.id, for_update=True)
        if session.revision != expected_revision or session.status not in {"planning", "revising"}:
            raise HTTPException(409, "Creator session changed while preparing")
        if await maybe_prepare(
            db,
            item=item,
            plan=plan,
            session=session,
            inputs={
                "user_message": user_message,
                "allow_chat": allow_chat,
                "previous_active_plan": previous_active_plan,
                "usage_purpose": usage_purpose,
                "test_run_id": test_run_id,
                "estimated_max_cost_usd": estimated_max_cost_usd,
                "reservation_approved": reservation_approved,
                "release_canary_id": release_canary_id,
            },
        ):
            return await _response(db, session)
    manifest, media_context = await resolve_item_creator_context(
        db,
        item,
        persona=persona,
        guided_capability_enabled=(True if allow_chat else None),
    )
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
            payload={"message": _dispatch_unavailable_message(manifest)},
        )
        return await _response(db, locked)

    creator_summary, item_summary = creator_context(persona, item)
    direction_prompt = ""
    thread_row = None
    if isinstance(db, AsyncSession):
        thread_row = (
            await db.execute(
                select(CreationThread.creator_direction_snapshot).where(
                    CreationThread.creator_id == user.id,
                    CreationThread.active_plan_item_id == item.id,
                )
            )
        ).scalar_one_or_none()
    if isinstance(thread_row, dict):
        from app.services.creator_direction_snapshot import private_snapshot_from  # noqa: PLC0415

        effective_direction = private_snapshot_from(thread_row) or thread_row
        direction_prompt = str((effective_direction or {}).get("prompt_block") or "")[:4000]
    creator_request = _confirmed_creator_request(session.events, user_message)
    is_refresh_retry = _is_refresh_retry_message(user_message)
    previous_strategy = (
        _previous_accepted_strategy(previous_active_plan) if is_refresh_retry else None
    )
    # KRI-129 part 3: unlike `previous_strategy` above (only populated for a
    # refresh/retry, to pin specific fields), this is populated on every
    # turn. `creator_request` re-accumulates every past user message, so a
    # resolution keyed off it (e.g. an explicit "all" scope) can legitimately
    # re-fire turn after turn even when nothing changed; without this, its
    # explanatory sentence would be re-appended to the summary every single
    # turn ("make the intro title red" would still say "I left out 1 clip").
    # Used only to suppress a sentence whose resolved OUTCOME (scope, ids,
    # duration) is unchanged from what was already communicated last turn.
    previous_turn_strategy = _previous_accepted_strategy(previous_active_plan)
    if is_refresh_retry:
        # Never let the canned "keep the same plan" affordance itself read as
        # the creator's intent — to the model, to the specialist brief, or to
        # any duration/cadence recognizer downstream that reads
        # `creator_request`.
        creator_request = (
            _original_creator_request_for_refresh(previous_active_plan, session.events)
            or creator_request
        )
    explicit_guided_voiceover = _explicit_guided_voiceover_request(creator_request, manifest)
    if explicit_guided_voiceover:
        guided_voiceover = manifest.capabilities.get(CAPABILITY_GUIDED_VOICEOVER)
        if guided_voiceover is None or not guided_voiceover.available:
            locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
            if locked.revision != expected_revision:
                raise HTTPException(status_code=409, detail="Creator session changed")
            locked.status = "briefing"
            reason = (
                guided_voiceover.reason
                if guided_voiceover is not None
                else "guided voiceover is unavailable"
            )
            await append_event(
                db,
                locked,
                event_type="assistant_question",
                role="assistant",
                payload={
                    "message": (
                        "I can't preserve that explicit photo/video timing or all-media request "
                        "with the current recorded-voiceover capability. "
                        "You can remove the explicit timing or all-media requirement and use "
                        "the current narrated edit behavior."
                    ),
                    "reason_code": "guided_voiceover_unavailable",
                    "unmet_instruction": "guided voiceover with explicit visual coverage/timing",
                    "detail": reason,
                    "options": [
                        "Use the current narrated edit behavior",
                        "Remove the explicit timing or all-media requirement",
                    ],
                },
            )
            return await _response(db, locked)
    latest_cut_s = recognize_round_robin_cadence(user_message)
    current_rejects_cadence = rejects_round_robin_cadence(user_message)
    cadence_cancelled = current_rejects_cadence or (
        latest_cut_s is None and _cadence_was_cancelled(session.events)
    )
    explicit_cut_s = (
        None
        if cadence_cancelled
        else latest_cut_s or recognize_round_robin_cadence(creator_request)
    )
    pending_all_media_capacity = _latest_all_media_capacity_question(session.events)
    all_media_capacity_choice = (
        _all_media_capacity_choice(pending_all_media_capacity, user_message, manifest)
        if pending_all_media_capacity is not None
        else None
    )
    videos = [media for media in manifest.media if media.kind == "video"]
    usable_videos = [media for media in videos if media.duration_s is not None]
    pending_cadence_question = _latest_cadence_question(session.events)
    if (
        pending_cadence_question
        and pending_cadence_question.get("kind") == "source_selection"
        and not cadence_cancelled
        and _selected_cadence_sources(pending_cadence_question, manifest, user_message) is None
    ):
        locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
        if locked.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Creator session changed")
        await _record_required_cadence_question(
            db,
            locked,
            payload={
                "message": "Choose two of the listed videos so I can preserve the alternation.",
                "reason_code": "cadence_source_selection",
                "options": list((pending_cadence_question.get("selections") or {}).keys()),
                "cadence_context": pending_cadence_question,
            },
        )
        return await _response(db, locked)
    planned_cadence, _planned_target_s = _latest_planned_cadence(session.events)
    if explicit_cut_s is not None and len(videos) == 2 and len(usable_videos) != 2:
        _enqueue_creator_clip_metadata(item, plan)
        locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
        if locked.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Creator session changed")
        await _record_cadence_duration_unavailable(
            db,
            locked,
            cadence=MontageCadenceConstraint(
                source_media_ids=[media.media_id for media in videos],
                cut_duration_s=explicit_cut_s,
                reuse_policy=recognize_cadence_reuse_policy(creator_request),
            ),
            target_duration_s=recognize_total_duration_s(creator_request),
        )
        return await _response(db, locked)
    if (
        explicit_cut_s is not None
        and len(videos) > 2
        and pending_cadence_question is None
        and planned_cadence is None
    ):
        selections: dict[str, list[str]] = {}
        for index, left in enumerate(videos[:3]):
            for right in videos[index + 1 : 3]:
                left_label = left.label or left.media_id
                right_label = right.label or right.media_id
                selections[f"Alternate {left_label} and {right_label}"] = [
                    left.media_id,
                    right.media_id,
                ]
        locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
        if locked.revision != expected_revision:
            raise HTTPException(status_code=409, detail="Creator session changed")
        await _record_required_cadence_question(
            db,
            locked,
            payload={
                "message": "Which two videos should I alternate?",
                "reason_code": "cadence_source_selection",
                "options": list(selections),
                "cadence_context": {
                    "kind": "source_selection",
                    "cut_duration_s": explicit_cut_s,
                    "reuse_policy": recognize_explicit_cadence_reuse_policy(user_message)
                    or recognize_cadence_reuse_policy(creator_request),
                    "selections": selections,
                },
            },
        )
        return await _response(db, locked)
    if preparation_attempt_id:
        await require_current_attempt(db, preparation_attempt_id, preparation_token or "")
    locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
    if locked.revision != expected_revision or locked.status not in {"planning", "revising"}:
        raise HTTPException(status_code=409, detail="Creator session changed while planning")
    action: AskUser | ProposeStrategy | ReviewDecision
    if prepared_attempt is not None and prepared_attempt.planning_action:
        action = MainCreatorOutput.model_validate(
            {"action": prepared_attempt.planning_action}
        ).action
    elif all_media_capacity_choice is not None:
        # The displayed, hash-fenced mapping is authoritative. Applying it is
        # deterministic and must not spend another model call or fail against
        # the model-call budget.
        action = ProposeStrategy(
            kind="propose_strategy",
            strategy=all_media_capacity_choice,
            summary="Applied your all-media capacity choice.",
        )
    else:
        # Reserve the session's existing eight-call allowance under the row
        # lock before contacting Pro. The previous post-call increment allowed
        # the ninth request to reach Gemini and only failed afterwards.
        if locked.agent_call_count >= locked.agent_call_budget:
            locked.status = "failed"
            locked.last_error = {"code": "agent_budget_exhausted"}
            await append_event(
                db,
                locked,
                event_type="assistant_error",
                payload={"message": "This edit needs a fresh creator session."},
            )
            return await _response(db, locked)
        locked.agent_call_count += 1
        await db.commit()

        agent_input = MainCreatorInput(
            user_message=user_message,
            creator_request=creator_request,
            creator_context=creator_summary,
            creator_direction=direction_prompt,
            item_context=item_summary,
            media_context=media_context,
            conversation=_conversation(session.events),
            capability_manifest=manifest,
        )
        try:
            output = await asyncio.to_thread(
                MainCreatorAgent(default_client()).run,
                agent_input,
                ctx=RunContext(
                    creator_agent_session_id=str(session.id),
                    creator_id=str(user.id),
                    request_id=str(expected_revision),
                    usage_purpose=usage_purpose,
                    test_run_id=test_run_id,
                    estimated_max_cost_usd=estimated_max_cost_usd,
                    reservation_approved=reservation_approved,
                    release_canary_id=release_canary_id,
                ),
            )
            action = output.action
            if isinstance(action, ProposeStrategy):
                # KRI-127 model-output hygiene, applied right where the model's
                # ProposeStrategy is accepted, flag on or off. `resolved_clip_intents`
                # is server-owned and must never be trusted from the model. When the
                # flag is off, visual requests are discarded. Transcript intents
                # keep their existing pinned-narration materialization path.
                strategy_hygiene: dict[str, Any] = {"resolved_clip_intents": None}
                if not settings.clip_intents_enabled:
                    strategy_hygiene["clip_intents"] = [
                        intent
                        for intent in (action.strategy.clip_intents or [])
                        if intent.label_source == "transcript"
                    ] or None
                action = action.model_copy(
                    update={"strategy": action.strategy.model_copy(update=strategy_hygiene)}
                )
        except (AiBudgetExceededError, ProviderQuotaExceededError) as exc:
            locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
            if locked.revision == expected_revision and locked.status in {"planning", "revising"}:
                locked.agent_call_count = max(0, locked.agent_call_count - 1)
                if not preparation_attempt_id:
                    locked.status = "briefing"
                    if isinstance(exc, ProviderQuotaExceededError):
                        locked.last_error = {"code": "provider_quota_exceeded"}
                        await append_event(
                            db,
                            locked,
                            event_type="assistant_error",
                            payload={
                                "message": (
                                    "Clip analysis is unavailable right now. "
                                    "Your request is saved; try again later."
                                ),
                                "code": "provider_quota_exceeded",
                            },
                        )
                        return await _response(db, locked)
                await db.commit()
            raise
        except TerminalError as exc:
            if preparation_attempt_id:
                raise
            locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
            if locked.revision != expected_revision or locked.status not in {
                "planning",
                "revising",
            }:
                raise HTTPException(409, "Creator session changed while planning")
            locked.status = "briefing"
            locked.last_error = {"code": "provider_unavailable"}
            log.warning(
                "main_creator.planning_failed",
                session_id=str(session.id),
                error_type=type(exc).__name__,
            )
            await append_event(
                db,
                locked,
                event_type="assistant_error",
                payload={
                    "message": (
                        "Clip analysis is unavailable right now. "
                        "Your request is saved; try again later."
                    ),
                    "code": "provider_unavailable",
                },
            )
            return await _response(db, locked)

    if preparation_attempt_id:
        prepared_attempt = await require_current_attempt(
            db, preparation_attempt_id, preparation_token or ""
        )
        prepared_attempt.planning_action = action.model_dump(mode="json")
        await db.commit()
        await require_current_attempt(db, preparation_attempt_id, preparation_token or "")

    locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
    if locked.revision != expected_revision or locked.status not in {"planning", "revising"}:
        raise HTTPException(status_code=409, detail="Creator session changed while planning")
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
        cadence, clarified_target_s = _resolved_cadence_for_turn(
            events=session.events,
            manifest=manifest,
            creator_request=creator_request,
            user_message=user_message,
        )
        from app.schemas.edit_proposal import parse_edit_proposal, resolve_video_reuse_policy

        saved_proposal = parse_edit_proposal(getattr(item, "edit_proposal", None))
        previous_reuse_policy = (locked.active_plan or {}).get("video_reuse_policy")
        if previous_reuse_policy is None and saved_proposal is not None:
            previous_reuse_policy = saved_proposal.brief.video_reuse_policy
        reuse_policy = resolve_video_reuse_policy(
            user_message,
            resolve_video_reuse_policy(
                creator_request,
                previous_reuse_policy,
            ),
            cadence,
        )
        if reuse_policy == "once":
            cadence = None
        strategy = strategy.model_copy(
            update={"video_reuse_policy": reuse_policy, "montage_cadence": cadence}
        )
        if cadence is not None:
            media_by_id = {media.media_id: media for media in manifest.media}
            if any(
                media_by_id.get(media_id) is None or media_by_id[media_id].duration_s is None
                for media_id in cadence.source_media_ids
            ):
                _enqueue_creator_clip_metadata(item, plan)
                await _record_cadence_duration_unavailable(
                    db,
                    locked,
                    cadence=cadence,
                    target_duration_s=(
                        clarified_target_s
                        or recognize_total_duration_s(user_message)
                        or recognize_total_duration_s(creator_request)
                        or strategy.target_duration_s
                    ),
                )
                return await _response(db, locked)
            explicit_target_s = recognize_total_duration_s(
                user_message
            ) or recognize_total_duration_s(creator_request)
            strategy = strategy.model_copy(
                update={
                    "direction": "fast_montage",
                    "edit_format": "montage",
                    "pacing": "fast",
                    "target_duration_s": (
                        clarified_target_s or explicit_target_s or strategy.target_duration_s
                    ),
                    "montage_cadence": cadence,
                }
            )
            try:
                strategy = normalize_creator_strategy_media(
                    manifest, strategy, repair_model_output=True
                )
            except MontageCadenceUnavailableError as exc:
                await _record_cadence_unavailable(db, locked, exc)
                return await _response(db, locked)
            except MixedMediaTimingUnavailableError as exc:
                # A cadence over sources the phone can't draw (a Visuals video
                # before visualVideos is verified) is refused the same way the
                # planning path below refuses it, never left mid-turn.
                await _record_media_unavailable(db, locked, exc)
                return await _response(db, locked)
            cycle_s = cadence.cut_duration_s * len(cadence.source_media_ids)
            capacity_s = round_robin_capacity_s(manifest.media, cadence)
            requested_s = strategy.target_duration_s
            if capacity_s < cycle_s:
                balanced_s = 0
            else:
                limit_s = (
                    requested_s
                    if cadence.reuse_policy == "allow_repeat"
                    else min(capacity_s, requested_s)
                )
                balanced_s = _balanced_duration_s(limit_s=limit_s, cycle_s=cycle_s)
                if balanced_s < 3:
                    # A minimum-length request can fall between complete
                    # cycles (for example 3s requested with a 2s A/B cycle).
                    # Recommend the next renderable cycle instead of offering
                    # reuse, which cannot make an incomplete cycle valid.
                    expansion_limit_s = (
                        MAX_PROPOSAL_DURATION_S
                        if cadence.reuse_policy == "allow_repeat"
                        else capacity_s
                    )
                    balanced_s = _next_balanced_duration_s(
                        minimum_s=requested_s,
                        limit_s=expansion_limit_s,
                        cycle_s=cycle_s,
                    )
            if balanced_s != requested_s:
                if balanced_s < 3:
                    can_repeat = capacity_s >= cycle_s
                    options = ["Allow the strongest moments to repeat"] if can_repeat else []
                    await _record_required_cadence_question(
                        db,
                        locked,
                        payload={
                            "message": (
                                "There isn't enough footage for one complete alternating cycle. "
                                + (
                                    "Allow footage to repeat or add longer clips."
                                    if can_repeat
                                    else "Add longer clips before I build the edit."
                                )
                            ),
                            "reason_code": "cadence_insufficient_footage",
                            "options": options,
                            "recommended_option": options[0] if options else None,
                            "cadence_context": {
                                "kind": "capacity",
                                "cadence": cadence.model_dump(mode="json"),
                                "requested_duration_s": requested_s,
                                "recommended_duration_s": balanced_s,
                                "recommendation": "Allow the strongest moments to repeat",
                            },
                        },
                    )
                    return await _response(db, locked)
                recommendation = f"Use the best {balanced_s} seconds"
                alternatives = [recommendation]
                if cadence.reuse_policy == "no_repeat" and capacity_s < requested_s:
                    alternatives.append(f"Repeat footage to keep {requested_s} seconds")
                await _record_required_cadence_question(
                    db,
                    locked,
                    payload={
                        "message": (
                            f"I can make a balanced {balanced_s}-second edit "
                            + (
                                "without repeating footage. "
                                if cadence.reuse_policy == "no_repeat"
                                else "with exact complete alternation. "
                            )
                            + "I recommend using the strongest moments. What would you prefer?"
                        ),
                        "reason_code": "cadence_capacity",
                        "options": alternatives,
                        "recommended_option": recommendation,
                        "cadence_context": {
                            "kind": "capacity",
                            "cadence": cadence.model_dump(mode="json"),
                            "requested_duration_s": requested_s,
                            "recommended_duration_s": balanced_s,
                            "recommendation": recommendation,
                        },
                    },
                )
                return await _response(db, locked)
        try:
            # Model-authored media references are repaired only at this trust
            # boundary. The subsequent compiler remains strict, so persisted
            # plans can contain only IDs from the authoritative manifest.
            planning_manifest = await _resolve_explicit_sfx_outside_manifest(
                db,
                _explicit_sfx_name(creator_request, manifest=manifest),
                manifest=manifest,
            )
            strategy = _apply_explicit_render_intent(
                strategy,
                creator_request,
                manifest=planning_manifest,
                latest_user_message=user_message,
                render_intent_evidence=(
                    action.render_intent_evidence if isinstance(action, ProposeStrategy) else None
                ),
            )
            pre_normalize_target_duration_s = strategy.target_duration_s
            try:
                strategy = normalize_creator_strategy_media(
                    planning_manifest, strategy, repair_model_output=True
                )
            except (MixedMediaTimingUnavailableError, MontageCadenceUnavailableError):
                raise
            except ValueError as exc:
                if "edit format" in str(exc) and "unavailable" in str(exc):
                    raise CreatorCapabilityError(
                        str(exc),
                        edit_format=strategy.edit_format,
                    ) from exc
                raise CreatorStrategyError(str(exc), edit_format=strategy.edit_format) from exc
            # KRI-129 part 6: a genuine `media_scope == "selected"` subset
            # whose combined source duration can't cover the once-only
            # `video_reuse_policy` target gets that target quietly lowered by
            # `normalize_creator_strategy_media` (never changing that math
            # here) -- say so, since this is the caller with both the before
            # and after duration and the summary string.
            capacity_capped_sentence: str | None = None
            if (
                all_media_capacity_choice is None
                and strategy.media_scope == "selected"
                and pre_normalize_target_duration_s is not None
                and strategy.target_duration_s < pre_normalize_target_duration_s
            ):
                capacity_capped_sentence = (
                    f"Your selected clips add up to {strategy.target_duration_s:g} seconds, "
                    f"so I made the edit {strategy.target_duration_s:g} seconds long."
                )
            if all_media_capacity_choice is not None:
                strategy = normalize_creator_strategy_media(
                    planning_manifest,
                    all_media_capacity_choice,
                    repair_model_output=True,
                )
            requested_duration_is_explicit = (
                recognize_total_duration_s(creator_request) is not None
                or _pinned_narration_target_duration_s(planning_manifest) is not None
            )
            if not requested_duration_is_explicit and all_media_capacity_choice is None:
                # With no creator-authored duration, surface the planner's
                # content-aware rationale instead of presenting a schema
                # default or arithmetic floor as user intent.
                summary = strategy.rationale or summary
            # KRI-129: a short clip is renderable at its own length -- it is
            # never "unusable" and must never be silently dropped from an
            # explicit "all" scope. Nothing pre-filters `strategy` by clip
            # duration here; the only remaining explanatory sentences are the
            # budget-exhausted-history ones below and the selected-scope
            # duration-cap note.
            left_out_sentence: str | None = None
            capacity_question = _all_media_capacity_question(
                planning_manifest,
                strategy,
                requested_duration_is_explicit=requested_duration_is_explicit,
            )
            if capacity_question is not None:
                mappings = capacity_question["all_media_capacity"]["option_mappings"]
                # mappings[0] is always the strength-ranked subset mapping;
                # the keep-everything mapping is only present when the fast
                # target is feasible this turn (see
                # `_all_media_capacity_question`).
                strongest_subset_mapping = mappings[0]
                keep_all_mapping = next(
                    (
                        mapping
                        for mapping in mappings
                        if mapping["strategy"].get("media_scope") == "all"
                    ),
                    None,
                )
                # KRI-129: history only ever tells us WHICH KIND of option the
                # creator prefers, never the VALUES recorded against a
                # possibly-stale earlier question. The kind is re-applied
                # against the mapping computed fresh for THIS turn's strategy
                # (current target duration, direction, manifest) -- never the
                # historical strategy object itself. A newer, explicit
                # narrowing instruction in the creator's latest message
                # always overrides a "keep everything"/"strongest subset"
                # history, never the other way around.
                historical_kind = _capacity_history_kind_for_turn(
                    session.events, user_message, planning_manifest
                )
                total_clips = len(planning_manifest.media)
                target_s = strategy.target_duration_s
                chosen_mapping: dict[str, Any] | None = None
                if historical_kind == "keep_everything" and keep_all_mapping is not None:
                    chosen_mapping = keep_all_mapping
                    left_out_sentence = (
                        "I used your earlier choice to keep everything with faster pacing."
                    )
                elif historical_kind == "strongest_subset":
                    chosen_mapping = strongest_subset_mapping
                    left_out_sentence = "I used your earlier choice to keep the strongest clips."
                if chosen_mapping is None and locked.question_count < locked.question_budget:
                    locked.status = "briefing"
                    locked.question_count += 1
                    await append_event(
                        db,
                        locked,
                        event_type="assistant_question",
                        payload=capacity_question,
                    )
                    return await _response(db, locked)
                # The session cannot ask another question. An explicit "all"
                # must never be silently swapped for a mapping the creator
                # did not choose: reuse a real answer already given earlier
                # in this session when one exists; otherwise prefer keeping
                # every clip (faster pacing) over dropping clips, and always
                # tell the creator what was applied instead of asking.
                if chosen_mapping is None:
                    # No usable history (none recorded, or the current
                    # question has no mapping of that kind): fall back to the
                    # existing preference order -- keep everything (faster
                    # pacing) before dropping clips.
                    if keep_all_mapping is not None:
                        chosen_mapping = keep_all_mapping
                        left_out_sentence = (
                            f"All {total_clips} clips don't fit a {target_s:g}-second story, "
                            "so I kept everything and used faster pacing."
                        )
                    else:
                        chosen_mapping = strongest_subset_mapping
                        left_out_sentence = (
                            f"All {total_clips} clips don't fit a {target_s:g}-second story "
                            "even with faster pacing, so I kept the strongest clips."
                        )
                chosen_strategy = CreativeStrategy.model_validate(chosen_mapping["strategy"])
                strategy = normalize_creator_strategy_media(
                    planning_manifest,
                    chosen_strategy,
                    repair_model_output=True,
                )
            # KRI-129 part 3: a resolution keyed off the (turn-accumulating)
            # creator_request or off a policy that recomputes the same way
            # every turn can legitimately re-fire on a later, unrelated turn.
            # Only surface an explanatory sentence once -- when it actually
            # changes what was previously communicated, never on every turn
            # after. `previous_turn_strategy` is None on the very first turn
            # a resolution applies, so it is never suppressed there.
            explanatory_sentences = [
                sentence for sentence in (left_out_sentence, capacity_capped_sentence) if sentence
            ]
            if explanatory_sentences and previous_turn_strategy is not None:
                # `selected_media_ids` is only meaningful (and populated) for
                # an actual `media_scope == "selected"` strategy -- a guided
                # "all" scope always resets it to `[]` (it takes every
                # manifest media by construction, see
                # `normalize_creator_strategy_media`), so comparing it
                # unconditionally would report a false mismatch between an
                # "all" strategy and its own persisted-and-reloaded copy.
                same_outcome = (
                    previous_turn_strategy.media_scope == strategy.media_scope
                    and previous_turn_strategy.target_duration_s == strategy.target_duration_s
                    and (
                        strategy.media_scope != "selected"
                        or set(previous_turn_strategy.selected_media_ids or [])
                        == set(strategy.selected_media_ids or [])
                    )
                )
                if same_outcome:
                    explanatory_sentences = []
            for sentence in explanatory_sentences:
                summary = f"{summary.rstrip()} {sentence}" if summary else sentence
            strategy = _apply_refresh_pin_if_needed(
                strategy,
                previous_strategy=previous_strategy,
                manifest=planning_manifest,
                session_id=locked.id,
                item_id=item.id,
            )
            transcript_intents = [
                intent
                for intent in (strategy.clip_intents or [])
                if intent.label_source == "transcript"
            ]
            if settings.clip_intents_enabled or transcript_intents:
                intent_clips = (
                    await load_intent_clips_for_item(db, item, persona)
                    if settings.clip_intents_enabled
                    else []
                )
                requested_intents = transcript_intents
                # Never hold the session's FOR UPDATE row lock across the
                # resolver's own model/vision calls. Release it exactly the
                # way the MainCreatorAgent call above does (commit, call,
                # re-lock, re-check the revision fence) before writing
                # anything the resolution decided back onto the session.
                await db.commit()
                background_pending = False
                intent_context = RunContext(
                    creator_agent_session_id=str(session.id),
                    creator_id=str(user.id),
                    request_id=str(expected_revision),
                    usage_purpose=usage_purpose,
                    test_run_id=test_run_id,
                    estimated_max_cost_usd=estimated_max_cost_usd,
                    reservation_approved=reservation_approved,
                    release_canary_id=release_canary_id,
                )

                async def save_answers(answers):
                    from app.services.creator_preparation import checkpoint_answers

                    await checkpoint_answers(
                        db, preparation_attempt_id, preparation_token or "", answers
                    )

                try:
                    if settings.clip_intents_enabled:
                        planned_intents = await plan_and_resolve_clip_intents(
                            **(
                                {"background": True, "checkpoint": save_answers}
                                if preparation_attempt_id
                                else {}
                            ),
                            candidate_intents=strategy.clip_intents,
                            latest_user_message=user_message,
                            creator_request=(
                                creator_request
                                if is_refresh_retry
                                else _confirmed_creator_request(
                                    session.events, user_message, truncate=False
                                )
                            ),
                            clips=intent_clips,
                            run_context=intent_context,
                        )
                        resolution = planned_intents.resolution
                        requested_intents = planned_intents.requested_intents
                    else:
                        resolution = IntentResolution()
                    if any(
                        intent.label_source == "transcript" for intent in requested_intents
                    ) and (
                        planning_manifest.narration is None
                        or strategy.execution_contract != GUIDED_VOICEOVER_EXECUTION_CONTRACT
                    ):
                        resolution = IntentResolution(
                            status="needs_creator",
                            question=(
                                "Those labels need a recorded voiceover with guided visuals. "
                                "Please add a recording or choose labels based on the footage."
                            ),
                        )
                    if resolution.deferred_queries and not preparation_attempt_id:
                        from app.tasks.clip_intent_requery import (  # noqa: PLC0415
                            enqueue_clip_intent_requeries,
                        )

                        background_pending = await asyncio.to_thread(
                            enqueue_clip_intent_requeries,
                            plan_id=str(plan.id),
                            item_id=str(item.id),
                            user_id=str(user.id),
                            ownership_epoch=int(plan.ownership_epoch or 0),
                            clips=intent_clips,
                            queries=resolution.deferred_queries,
                            context=intent_context,
                        )
                except Exception as exc:  # noqa: BLE001
                    if preparation_attempt_id:
                        raise
                    # Never a 500 for a resolver failure. Asking is the safe
                    # degrade -- it can never print an unverified label.
                    log.warning(
                        "clip_intents.resolution_failed",
                        session_id=str(session.id),
                        error_type=type(exc).__name__,
                    )
                    resolution = IntentResolution(
                        status="provider_unavailable",
                        error_code="provider_unavailable",
                        question=(
                            "I couldn't confidently match that to your clips. Which "
                            "clips should it apply to, and what should each say?"
                        ),
                    )
                if preparation_attempt_id:
                    await require_current_attempt(
                        db, preparation_attempt_id, preparation_token or ""
                    )
                locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
                if locked.revision != expected_revision or locked.status not in {
                    "planning",
                    "revising",
                }:
                    raise HTTPException(
                        status_code=409, detail="Creator session changed while planning"
                    )
                if not preparation_attempt_id:
                    await _persist_clip_intent_vision_answers(db, item, resolution.vision_answers)
                if background_pending or resolution.needs_creator:
                    locked.status = "briefing"
                    await append_event(
                        db,
                        locked,
                        event_type="assistant_question",
                        role="assistant",
                        payload={
                            "message": (
                                "I'm taking a closer look at the remaining clips. "
                                "Send another message in a moment and I'll use what I find."
                                if background_pending
                                else resolution.question
                            ),
                            "reason_code": (
                                "clip_intent_pending"
                                if background_pending
                                else "clip_intent_unresolved"
                            ),
                        },
                    )
                    return await _response(db, locked)
                if resolution.status not in {"resolved", "needs_creator"}:
                    locked.status = "briefing"
                    locked.last_error = {"code": resolution.error_code or resolution.status}
                    await append_event(
                        db,
                        locked,
                        event_type="assistant_error",
                        role="assistant",
                        payload={
                            "message": (
                                "Clip analysis is unavailable right now. "
                                "Your request is saved; try again later."
                            ),
                            "code": resolution.error_code or resolution.status,
                        },
                    )
                    return await _response(db, locked)
                strategy = strategy.model_copy(
                    update={
                        "clip_intents": requested_intents or None,
                        "resolved_clip_intents": resolution.intents or None,
                    }
                )
            locked.active_plan = compile_active_plan(
                locked,
                manifest=planning_manifest,
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
            if isinstance(exc, CreatorSfxUnavailableError):
                locked.status = "failed"
                locked.last_error = {
                    "code": "licensed_sfx_unavailable",
                    "message": str(exc)[:300],
                }
                await append_event(
                    db,
                    locked,
                    event_type="assistant_error",
                    payload={
                        "message": str(exc)[:300],
                        "code": "licensed_sfx_unavailable",
                    },
                )
                return await _response(db, locked)
            if isinstance(exc, CreatorCapabilityError):
                edit_format = exc.edit_format or strategy.edit_format
                locked.status = "failed"
                locked.last_error = {
                    "code": exc.code,
                    "edit_format": edit_format,
                    "message": str(exc)[:300],
                }
                await append_event(
                    db,
                    locked,
                    event_type="assistant_error",
                    payload={
                        "message": (
                            f"{edit_format} is not available for this Creator rollout. "
                            "No fallback edit was rendered."
                        ),
                        "code": exc.code,
                        "edit_format": edit_format,
                    },
                )
                return await _response(db, locked)
            if isinstance(exc, CreatorStrategyError):
                edit_format = exc.edit_format or strategy.edit_format
                locked.status = "failed"
                locked.last_error = {
                    "code": exc.code,
                    "edit_format": edit_format,
                    "message": str(exc)[:300],
                }
                await append_event(
                    db,
                    locked,
                    event_type="assistant_error",
                    payload={
                        "message": (
                            "I couldn't apply that exact creative direction to this "
                            f"{edit_format} edit. No fallback edit was rendered."
                        ),
                        "code": exc.code,
                        "edit_format": edit_format,
                    },
                )
                return await _response(db, locked)
            if isinstance(exc, MontageCadenceUnavailableError):
                await _record_cadence_unavailable(db, locked, exc)
                return await _response(db, locked)
            if isinstance(exc, MixedMediaTimingUnavailableError):
                await _record_media_unavailable(db, locked, exc)
                return await _response(db, locked)
            if _strict_creator_format(strategy.edit_format):
                # Any real format-availability failure is classified above as
                # CreatorCapabilityError. An untyped ValueError here is a bad
                # treatment/strategy and must never masquerade as rollout state.
                locked.status = "failed"
                locked.last_error = {
                    "code": "strategy_invalid",
                    "edit_format": strategy.edit_format,
                    "message": str(exc)[:300],
                }
                await append_event(
                    db,
                    locked,
                    event_type="assistant_error",
                    payload={
                        "message": (
                            "I couldn't apply that creative treatment to this "
                            f"{strategy.edit_format} edit. Edit the direction and try again."
                        ),
                        "code": "strategy_invalid",
                        "edit_format": strategy.edit_format,
                    },
                )
                return await _response(db, locked)
            strategy = normalize_creator_strategy_media(
                manifest,
                _apply_explicit_render_intent(
                    _fallback_strategy(manifest, user_message=creator_request).model_copy(
                        update={
                            "video_reuse_policy": reuse_policy,
                            "montage_cadence": cadence,
                            "opening_title": strategy.opening_title,
                            "opening_title_duration_s": strategy.opening_title_duration_s,
                            "shot_labels": strategy.shot_labels,
                            "closing_title": strategy.closing_title,
                            "font_family": strategy.font_family,
                            "text_color": strategy.text_color,
                        }
                    ),
                    creator_request,
                    manifest=manifest,
                    latest_user_message=user_message,
                    render_intent_evidence=(
                        action.render_intent_evidence
                        if isinstance(action, ProposeStrategy)
                        else None
                    ),
                ),
                repair_model_output=True,
            )
            strategy = _apply_refresh_pin_if_needed(
                strategy,
                previous_strategy=previous_strategy,
                manifest=manifest,
                session_id=locked.id,
                item_id=item.id,
            )
            locked.active_plan = compile_active_plan(
                locked,
                manifest=manifest,
                strategy=strategy,
                summary=MAIN_CREATOR_FALLBACK_SUMMARY,
                creator_request=creator_request,
            )
        locked.manifest_hash = manifest.manifest_hash
        locked.status = "awaiting_confirmation"
        locked.last_error = None
        await append_event(
            db,
            locked,
            event_type="assistant_strategy",
            payload={
                # A model summary is a proposal, never an execution receipt.
                "message": (
                    "I prepared a proposed edit. Review the plan and confirm it "
                    "before I change the video."
                ),
                "proposal_summary": locked.active_plan["summary"],
                "plan_hash": locked.active_plan["plan_hash"],
                "target_duration_s": locked.active_plan.get("target_duration_s"),
                "montage_cadence": locked.active_plan.get("montage_cadence"),
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
    return _creator_session_response(session)


async def start_creator_session_controller(
    request: Request,
    item_id: str,
    body: StartBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    *,
    allow_chat: bool = False,
    carried_active_plan: dict[str, Any] | None = None,
) -> CreatorSessionResponse:
    """Start (or join) the active Creator session for ``item_id``.

    ``carried_active_plan`` is the plan of a failed session this fresh one
    replaces in the same chat project, so its brief and accepted strategy are
    not lost (a refresh re-plans it; a new message adds to it).
    """
    cost_headers = paid_call_headers(request)
    _require_feature(user.id, allow_chat=allow_chat)
    # Serialize session creation against direct generation's PlanItem lock.
    item, plan, _persona = await _owned_context(db, item_id, user.id, for_update=True)

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

    # Inverse side of the start-vs-generate race. Both paths acquire the same
    # Plan -> Persona -> PlanItem locks: if raw Generate won, its committed Job
    # is visible here and a new creator plan cannot begin over that render. If
    # this start won, the dispatcher waits and then sees the active session.
    current_job_id = getattr(item, "current_job_id", None)
    current_job = None
    if current_job_id is not None:
        current_job = await db.get(
            Job,
            current_job_id,
            with_for_update=True,
            populate_existing=True,
        )
        if current_job is not None and current_job.status not in PLAN_ITEM_JOB_TERMINAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Wait for the current render before starting a Kria plan",
            )

    # If one-click Generate won the PlanItem lock and durably reserved its
    # automatic guided proposal, a new Creator session must not start in the
    # gap before that worker dispatches. Main Creator-owned guided attempts use
    # the fail-closed sentinel and are part of the current session, not a rival
    # producer.
    from app.schemas.edit_proposal import (  # noqa: PLC0415
        MAIN_CREATOR_FAIL_CLOSED,
        parse_edit_proposal,
    )

    pending_auto_design = parse_edit_proposal(getattr(item, "edit_proposal", None))
    ordinary_auto_design = bool(
        pending_auto_design is not None
        and pending_auto_design.approval_mode == "auto"
        and pending_auto_design.design_fallback != MAIN_CREATOR_FAIL_CLOSED
    )
    auto_design_is_pending = bool(
        ordinary_auto_design
        and pending_auto_design is not None
        and (
            pending_auto_design.status in {"analyzing", "drafting"}
            or (
                pending_auto_design.status == "approved"
                and not _job_matches_guided_attempt(
                    current_job, pending_auto_design.generation_attempt_id
                )
            )
            or (
                pending_auto_design.status == "failed"
                and bool(pending_auto_design.design_fallback)
                and not _job_matches_guided_attempt(
                    current_job, pending_auto_design.generation_attempt_id
                )
            )
        )
    )
    if auto_design_is_pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Kria is already designing this video. Wait for it to finish.",
        )

    session = await _latest_session(db, user.id, item.id, active_only=True)
    if session is None:
        session = CreatorAgentSession(
            creator_id=user.id,
            plan_item_id=item.id,
            status="briefing",
            ownership_epoch=int(plan.ownership_epoch or 0),
            # Source selection can legitimately be followed by one capacity
            # choice. Keep both deterministic questions inside the session.
            question_budget=2,
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
    previous_active_plan = (
        session.active_plan if isinstance(session.active_plan, dict) else carried_active_plan
    )
    session.preparation = None
    session.last_error = None
    _reset_render_target(session)
    history = getattr(session, "events", None)
    carried_brief = (
        _carried_brief_seed(carried_active_plan, body.message)
        if previous_active_plan is carried_active_plan and isinstance(history, list) and not history
        else ""
    )
    if carried_brief:
        # Durable, not inferred per turn: every later turn of this fresh
        # session reads the failed session's brief from its own history.
        seed = await append_event(
            db,
            session,
            event_type=CARRIED_BRIEF_EVENT,
            role="system",
            payload={"creator_request": carried_brief},
        )
        # Events appended by foreign key are absent from the loaded
        # collection this request's planning turn reads.
        history.append(seed)
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
        allow_chat=allow_chat,
        previous_active_plan=previous_active_plan,
        **cost_headers.as_kwargs(),
    )


@router.post("/{item_id}/creator-agent/session", response_model=CreatorSessionResponse)
@limiter.limit(CREATOR_AGENT_MUTATION_RATE_LIMIT)
async def start_creator_session(
    request: Request,
    item_id: str,
    body: StartBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse:
    return await start_creator_session_controller(request, item_id, body, user, db)


async def creator_session_turn_controller(
    request: Request,
    item_id: str,
    body: TurnBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    *,
    allow_chat: bool = False,
) -> CreatorSessionResponse:
    cost_headers = paid_call_headers(request)
    _require_feature(user.id, allow_chat=allow_chat)
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
    from app.services.creator_preparation import retry_inputs

    saved_inputs = await retry_inputs(db, session, body.message)
    session.status = "revising" if session.render_attempts else "planning"
    previous_active_plan = session.active_plan if isinstance(session.active_plan, dict) else None
    session.preparation = None
    session.last_error = None
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
        user_message=(saved_inputs or {}).get("user_message", body.message.strip()),
        allow_chat=allow_chat,
        previous_active_plan=(saved_inputs or {}).get("previous_active_plan", previous_active_plan),
        **cost_headers.as_kwargs(),
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
    return await creator_session_turn_controller(request, item_id, body, user, db)


def _apply_plan_intent(
    item: PlanItem,
    plan: CreatorEditPlan,
    current_analysis: object | None = None,
) -> None:
    edit_format = item.edit_format
    for command in plan.commands:
        if command.command == "set_item_intent":
            edit_format = command.edit_format
    strategy = plan.strategy
    audio = getattr(strategy, "audio_strategy", None)
    audio_mode = item.audio_mode
    if audio == "original_audio":
        audio_mode = "original"
    elif audio == "voiceover":
        if not item.voiceover_gcs_path:
            raise HTTPException(
                status_code=409, detail="Record a voiceover before confirming this plan"
            )
        audio_mode = "voiceover"
    elif audio == "licensed_music":
        audio_mode = "kria"
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )

    mutate_plan_item_media(
        item,
        detector_policy=current_detector_policy(),
        edit_format=edit_format,
        audio_mode=audio_mode,
        current_analysis=current_analysis,
    )
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


def _specialist_clip_intents(
    intents: list[ResolvedClipIntent] | None,
) -> list[ResolvedClipIntent] | None:
    """Translate resolved intents from chat media ids to planner/render media ids.

    The chat manifest names pool assets ``asset-{uuid}``; the specialist brief,
    the planner input and the render snapshot use the bare ``{uuid}`` (same
    translation as ``montage_audio``/``montage_cadence`` source ids below).
    """
    if not intents:
        return None
    return [
        intent.model_copy(
            update={
                "assignments": [
                    a.model_copy(update={"media_id": a.media_id.removeprefix("asset-")})
                    for a in intent.assignments
                ]
            }
        )
        for intent in intents
    ]


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
    specialist_audio = plan.strategy.montage_audio
    if specialist_audio is not None:
        specialist_audio = specialist_audio.model_copy(
            update={
                "source_media_ids": [
                    media_id.removeprefix("asset-")
                    for media_id in specialist_audio.source_media_ids
                ]
            }
        )
    specialist_cadence = plan.strategy.montage_cadence
    if specialist_cadence is not None:
        specialist_cadence = specialist_cadence.model_copy(
            update={
                "source_media_ids": [
                    media_id.removeprefix("asset-")
                    for media_id in specialist_cadence.source_media_ids
                ]
            }
        )
    brief_values: dict[str, Any] = {
        "direction": direction,
        "goal": goal,
        "pace": plan.strategy.pacing,
        "duration_s": plan.strategy.target_duration_s,
        "creator_request": creator_request,
        "opening_title": plan.strategy.opening_title,
        "opening_title_duration_s": plan.strategy.opening_title_duration_s,
        "shot_labels": plan.strategy.shot_labels,
        "closing_title": plan.strategy.closing_title,
        "font_family": plan.strategy.font_family,
        "text_color": plan.strategy.text_color,
        "image_layout": plan.strategy.image_layout,
        "licensed_sfx": plan.strategy.licensed_sfx,
        "mixed_media_timing": plan.strategy.mixed_media_timing,
        "montage_audio": specialist_audio,
        "montage_cadence": specialist_cadence,
        "video_reuse_policy": plan.strategy.video_reuse_policy,
        "output_orientation": (
            "portrait" if plan.strategy.mixed_media_timing is not None else None
        ),
    }
    # These fields are optional on the worker-owned proposal contract. Passing
    # them by field name keeps this route compatible with legacy snapshots
    # while allowing the shared typed intent to reach the specialist.
    narration_identity = creator_narration_identity(item)
    # KRI-129: an explicit "selected" scope used to be dropped here (only
    # "all" was forwarded), so the guided specialist received
    # required_media_ids=None and could pick any media it liked, silently
    # discarding a creator's explicit subset (see normalize_creator_strategy_media
    # in creator_policy.py, which now preserves selected_media_ids for guided
    # programs). A "selected" scope with no ids left (fully repaired away)
    # falls back to today's behavior of not constraining the specialist.
    guided_selected_scope = plan.strategy.media_scope == "selected" and bool(
        plan.strategy.selected_media_ids
    )
    optional_values = {
        "execution_contract": plan.strategy.execution_contract,
        "media_scope": (
            plan.strategy.media_scope
            if plan.strategy.media_scope == "all" or guided_selected_scope
            else None
        ),
        "selected_media_ids": (plan.strategy.selected_media_ids if guided_selected_scope else None),
        # KRI-127: server-owned, resolved-at-confirm-time only. None whenever
        # the flag is off or no turn resolved any intent, so a stored brief
        # stays byte-identical until this ever actually resolves something.
        "clip_intents": _specialist_clip_intents(plan.strategy.resolved_clip_intents),
        "narration": (
            {
                **narration_identity.model_dump(mode="json"),
                "words": [],
                "caption_style": {
                    "clean": "sentence",
                    "editorial": "sentence",
                    "kinetic": "word",
                    "karaoke": "word",
                }.get(plan.strategy.caption_style),
            }
            if narration_identity is not None
            else None
        ),
    }
    brief_fields = getattr(ProposalBrief, "model_fields", {})
    brief_values.update(
        {
            key: value
            for key, value in optional_values.items()
            if key in brief_fields and value is not None and value is not False and value != "none"
        }
    )
    save_edit_conversation_turn(
        item,
        expected_version=expected_version,
        brief=ProposalBrief(**brief_values),
        user_message=creator_request or "Use the confirmed Main Creator direction.",
        agent_reply=summary or plan.strategy.rationale or "Build this direction.",
        suggestions=[],
        ready_to_plan=True,
    )
    # Keep legacy no-title briefs byte-compatible. These keys become part of
    # the durable contract only when the creator explicitly supplied them.
    stored_brief = (item.edit_proposal or {}).get("brief")
    if isinstance(stored_brief, dict):
        for field in ("opening_title", "font_family", "text_color", "image_layout"):
            if getattr(plan.strategy, field) is None:
                stored_brief.pop(field, None)


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


async def _lock_confirmed_render_graph(
    db: AsyncSession,
    *,
    item_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    job_id: uuid.UUID,
) -> CreatorAgentSession:
    """Lock a dispatched render before binding it to the Creator session.

    The generative worker enters through Plan -> Persona -> PlanItem -> Job.
    Finalization must take those same locks before CreatorAgentSession: setting
    ``target_job_id`` otherwise holds a Job foreign-key lock while a later
    session flush asks PostgreSQL for PlanItem key-share, deadlocking against
    a worker that already owns PlanItem and is waiting for Job.
    """

    item, _plan, _persona = await _owned_context(db, str(item_id), user_id, for_update=True)
    job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
    if job is None or job.user_id != user_id or job.content_plan_item_id != item.id:
        raise RuntimeError("creator render job changed during finalization")
    return await _load_session(db, session_id, user_id, item.id, for_update=True)


class _SpeechCleanupRecoveryChanged(RuntimeError):
    """The task-side immutable recovery fence rejected a stale request."""


class _CommittedRenderPublishFailure(RuntimeError):
    """Dispatch minted a terminal Job but could not publish it to the broker."""

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__("render dispatch publication failed")
        self.job_id = job_id


class _PhoneGateRejected(RuntimeError):
    """`_dispatch_item_render`'s analysis-proxy phone-source fence rejected
    the dispatch before a Job was minted (KRI-132).

    Without this, `outcome.outcome == "invalid_clips"` fell through to the
    generic `raise RuntimeError(f"render dispatch failed: {outcome.outcome}")`
    a few lines below, caught by the blanket `except Exception` and surfaced
    to chat as the unfixable-by-retry `execution_failed` / "I couldn't start
    that render" message — even when the true, typed reason
    (`DispatchResult.reason`, one of `PHONE_GATE_MESSAGES`) was already known.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"phone dispatch gate rejected: {reason}")
        self.reason = reason


def _retry_target_matches_confirmed_plan(
    *,
    item: PlanItem,
    plan: ContentPlan,
    session: CreatorAgentSession,
    job: Job | None,
    target_job_id: uuid.UUID,
    strategy: CreativeStrategy,
) -> bool:
    """Prove a private retry target is the exact failed Creator render."""

    if (
        job is None
        or job.id != target_job_id
        or item.current_job_id != target_job_id
        or session.target_job_id != target_job_id
        or job.status not in PLAN_ITEM_JOB_FAILED
        or job.user_id != session.creator_id
        or job.content_plan_item_id != item.id
        or job.content_plan_ownership_epoch != session.ownership_epoch
        or session.ownership_epoch != int(plan.ownership_epoch or 0)
    ):
        return False
    variants = (getattr(job, "assembly_plan", None) or {}).get("variants") or []
    if any(
        isinstance(variant, dict) and variant.get("render_status") in {"pending", "rendering"}
        for variant in variants
    ):
        return False
    target_variant_id = getattr(session, "target_variant_id", None)
    target_generation_id = getattr(session, "target_generation_id", None)
    if target_variant_id or target_generation_id:
        if not target_variant_id or not target_generation_id:
            return False
        target = next(
            (
                variant
                for variant in variants
                if isinstance(variant, dict)
                and variant.get("variant_id") == target_variant_id
                and variant.get("render_generation_id") == target_generation_id
            ),
            None,
        )
        if target is None:
            return False
    if strategy.render_program == "guided":
        return _job_matches_guided_attempt(
            job, (session.active_plan or {}).get("guided_generation_attempt_id")
        )
    return creator_strategies_equal((job.all_candidates or {}).get("creator_strategy"), strategy)


def _manifest_with_original_current_edit(
    manifest: ResolvedCreatorManifest,
    *,
    current_edit: object,
) -> ResolvedCreatorManifest | None:
    """Build a candidate manifest using only a validated opaque edit snapshot."""

    try:
        snapshot = (
            None if current_edit is None else CreatorEditSnapshot.model_validate(current_edit)
        )
    except (TypeError, ValueError):
        return None
    return manifest.model_copy(update={"current_edit": snapshot})


async def _original_current_edit_for_retry(
    db: AsyncSession,
    *,
    session: CreatorAgentSession,
    manifest: ResolvedCreatorManifest,
) -> object:
    """Recover a hash-verified original edit snapshot, failing closed for old data."""

    active = session.active_plan if isinstance(session.active_plan, dict) else {}
    if active.get("original_current_edit_present") is True:
        candidate = _manifest_with_original_current_edit(
            manifest, current_edit=active.get("original_current_edit")
        )
        if candidate is not None and canonical_manifest_hash(candidate) == session.manifest_hash:
            return active.get("original_current_edit")
        return _MISSING_RETRY_EDIT

    # Legacy plans predate the receipt field. Their original creator input is
    # private; inspect only the bounded session/agent subset and retain only
    # the opaque current_edit summary after re-hashing the complete manifest.
    rows = (
        (
            await db.execute(
                select(AgentRun.input_json)
                .where(
                    AgentRun.creator_agent_session_id == session.id,
                    AgentRun.agent_name == "nova.creator.main",
                )
                .order_by(AgentRun.created_at.desc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    for payload in rows:
        payload = payload if isinstance(payload, dict) else {}
        raw_manifest = payload.get("capability_manifest")
        if not isinstance(raw_manifest, dict) or "current_edit" not in raw_manifest:
            continue
        try:
            original = ResolvedCreatorManifest.model_validate(raw_manifest)
        except (TypeError, ValueError):
            continue
        if canonical_manifest_hash(original) != session.manifest_hash:
            continue
        candidate = _manifest_with_original_current_edit(
            manifest, current_edit=raw_manifest.get("current_edit")
        )
        if candidate is not None and canonical_manifest_hash(candidate) == session.manifest_hash:
            return raw_manifest.get("current_edit")
    return _MISSING_RETRY_EDIT


_MISSING_RETRY_EDIT = object()


async def confirm_creator_plan_controller(
    item_id: str,
    body: ConfirmBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    *,
    allow_chat: bool = False,
    speech_cleanup_recovery_action: Literal[
        "retry_required",
        "disable_and_create",
        "retry_preflight_dispatch",
    ]
    | None = None,
    speech_cleanup_recovery_job_id: uuid.UUID | None = None,
    speech_cleanup_recovery_generation_id: str | None = None,
    speech_cleanup_recovery_analysis_id: uuid.UUID | None = None,
    retry_target_job_id: uuid.UUID | None = None,
) -> CreatorSessionResponse:
    _require_feature(user.id, execution=True, allow_chat=allow_chat)
    recovery_values = (
        speech_cleanup_recovery_action,
        speech_cleanup_recovery_job_id,
        speech_cleanup_recovery_generation_id,
        speech_cleanup_recovery_analysis_id,
    )
    recovery_requested = any(value is not None for value in recovery_values)
    if retry_target_job_id is not None and (not allow_chat or recovery_requested):
        raise HTTPException(status_code=409, detail="Creator retry changed")
    if recovery_requested and (
        not allow_chat
        or speech_cleanup_recovery_action is None
        or speech_cleanup_recovery_job_id is None
        or not speech_cleanup_recovery_generation_id
        or speech_cleanup_recovery_analysis_id is None
        or body.speech_cleanup_analysis_id is not None
        or body.speech_cleanup_choice is not None
    ):
        raise HTTPException(status_code=409, detail="Speech cleanup recovery changed")
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
    digest_input = body.model_dump(mode="json")
    if recovery_requested:
        # The private recovery target participates in idempotency even though it
        # is intentionally absent from the public Creator confirm schema.
        digest_input["speech_cleanup_recovery_action"] = speech_cleanup_recovery_action
        digest_input["speech_cleanup_recovery_job_id"] = str(speech_cleanup_recovery_job_id)
        digest_input["speech_cleanup_recovery_generation_id"] = (
            speech_cleanup_recovery_generation_id
        )
        digest_input["speech_cleanup_recovery_analysis_id"] = str(
            speech_cleanup_recovery_analysis_id
        )
    request_digest = canonical_context_hash(digest_input)
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
    preflight_analysis_id: uuid.UUID | None = None
    if receipt is not None:
        if receipt.request_digest != request_digest:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        if (
            recovery_requested
            and receipt.status == "stale"
            and isinstance(receipt.error, dict)
            and receipt.error.get("code") == "speech_cleanup_recovery_changed"
        ):
            raise HTTPException(status_code=409, detail="speech_cleanup_analysis_changed")
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
        from app.services.creator_execution_contract import (  # noqa: PLC0415
            requests_guided_voiceover,
        )
        from app.services.guided_speech_cleanup import (  # noqa: PLC0415
            DISABLED_MESSAGE,
            guided_voiceover_cleanup_available,
        )

        if (
            body.speech_cleanup_choice == "clean"
            and requests_guided_voiceover((active.get("edit_plan") or {}).get("strategy"))
            and not guided_voiceover_cleanup_available()
        ):
            # Every confirm route shares this controller: a guided narrated
            # story can only honor "Clean up speech" through the planner's
            # cleaned derivative, so refuse before an attempt is reserved.
            raise HTTPException(status_code=409, detail=DISABLED_MESSAGE)
        if session.ownership_epoch != int(plan_row.ownership_epoch or 0):
            raise HTTPException(status_code=409, detail="Creator ownership changed")
        if session.render_attempts >= session.max_render_attempts:
            if speech_cleanup_recovery_action == "disable_and_create":
                # A required cleanup render and its one allowed same-snapshot
                # retry can consume the ordinary budget. Reserve exactly one
                # counted escape attempt so unchecked bypass cannot strand the
                # user's otherwise-renderable video.
                session.max_render_attempts = session.render_attempts + 1
            else:
                raise HTTPException(
                    status_code=409,
                    detail="This session has used its render attempts",
                )
        if item.current_job_id:
            current_job = await db.get(Job, item.current_job_id, with_for_update=True)
            if current_job is not None and current_job.status not in PLAN_ITEM_JOB_TERMINAL:
                raise HTTPException(
                    status_code=409, detail="Wait for the current render before confirming"
                )
        manifest, _media_context = await resolve_item_creator_context(
            db,
            item,
            persona=persona,
            guided_capability_enabled=(True if allow_chat else None),
        )
        expected_manifest_hash = manifest.manifest_hash
        if manifest.manifest_hash != session.manifest_hash:
            retry_job = (
                await db.get(Job, retry_target_job_id, with_for_update=True)
                if retry_target_job_id is not None
                else None
            )
            retry_target_is_exact = retry_target_job_id is not None and (
                _retry_target_matches_confirmed_plan(
                    item=item,
                    plan=plan_row,
                    session=session,
                    job=retry_job,
                    target_job_id=retry_target_job_id,
                    strategy=edit_plan.strategy,
                )
            )
            original_current_edit = (
                await _original_current_edit_for_retry(db, session=session, manifest=manifest)
                if retry_target_is_exact
                else _MISSING_RETRY_EDIT
            )
            restored = (
                _manifest_with_original_current_edit(manifest, current_edit=original_current_edit)
                if original_current_edit is not _MISSING_RETRY_EDIT
                else None
            )
            if restored is None or canonical_manifest_hash(restored) != session.manifest_hash:
                raise HTTPException(
                    status_code=409, detail="Footage or capabilities changed; review the plan again"
                )
            # The verified candidate is proof-only. Keep the live manifest
            # unchanged for subsequent capacity checks, and persist the
            # approved manifest identity on the new execution receipt.
            expected_manifest_hash = session.manifest_hash
        cadence = edit_plan.strategy.montage_cadence
        if cadence is not None:
            media_by_id = {media.media_id: media for media in manifest.media}
            cycle_s = cadence.cut_duration_s * len(cadence.source_media_ids)
            capacity_s = round_robin_capacity_s(manifest.media, cadence)
            target_s = edit_plan.strategy.target_duration_s
            complete_cycles = target_s / cycle_s
            cadence_is_feasible = bool(
                all(
                    media_by_id.get(media_id) is not None
                    and media_by_id[media_id].duration_s is not None
                    for media_id in cadence.source_media_ids
                )
                and capacity_s >= cycle_s - 0.001
                and abs(complete_cycles - round(complete_cycles)) <= 0.001
                and (cadence.reuse_policy == "allow_repeat" or capacity_s >= target_s - 0.001)
            )
            if not cadence_is_feasible:
                raise HTTPException(
                    status_code=409,
                    detail="Footage lengths changed; review the alternating plan again",
                )
        receipt = CreatorAgentExecution(
            session_id=session.id,
            idempotency_key=body.client_event_id,
            request_digest=request_digest,
            expected_revision=body.expected_revision,
            expected_manifest_hash=expected_manifest_hash,
            status="running",
        )
        db.add(receipt)
        if recovery_requested:
            if edit_plan.strategy.render_program != "native":
                raise HTTPException(status_code=409, detail="Speech cleanup recovery changed")
        else:
            from app.services.speech_cleanup import (  # noqa: PLC0415
                cleanup_inputs,
                reconcile_item_policy_change,
            )

            previous_speech_inputs = cleanup_inputs(item)
            from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
                mutation_current_analysis_async,
                schedule_item_preflight_async,
            )

            current_cleanup = await mutation_current_analysis_async(db, item.id, for_update=True)
            _apply_plan_intent(item, edit_plan, current_cleanup)
            reconcile_item_policy_change(item, previous_speech_inputs)
            preflight_analysis_id = await schedule_item_preflight_async(db, item)
        if edit_plan.strategy.render_program == "guided":
            _seed_guided_specialist_brief(
                item,
                edit_plan,
                summary=str(active.get("summary") or ""),
                creator_request=str(active.get("creator_request") or ""),
            )
            # Mint the immutable guided execution identity before publishing
            # any background work. A process crash after enqueue can then
            # resume against the exact proposal attempt instead of a mutable
            # proposal_version that advances during draft + approval.
            guided_generation_attempt_id = str(uuid.uuid4())
            guided_speech_cleanup = (
                {
                    "generation_attempt_id": guided_generation_attempt_id,
                    "analysis_id": (
                        str(body.speech_cleanup_analysis_id)
                        if body.speech_cleanup_analysis_id is not None
                        else None
                    ),
                    "choice": body.speech_cleanup_choice,
                }
                if body.speech_cleanup_analysis_id is not None
                or body.speech_cleanup_choice is not None
                else None
            )
            session.active_plan = {
                **active,
                "guided_generation_attempt_id": guided_generation_attempt_id,
                # This is scoped to this newly minted attempt.  Setting it to
                # None on an ordinary confirmation prevents a later worker
                # from borrowing consent recorded for an older attempt.
                "guided_speech_cleanup": guided_speech_cleanup,
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
        if preflight_analysis_id is not None:
            from app.services.plan_item_media import (  # noqa: PLC0415
                publish_preflight_after_commit,
            )

            await asyncio.to_thread(
                publish_preflight_after_commit,
                preflight_analysis_id,
            )

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
    resumed_publish_failure_job_id: uuid.UUID | None = None
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
            and (
                guided_matches
                or (not guided and creator_strategies_equal(candidate_strategy, expected_strategy))
            )
        ):
            job_id = candidate.id
            if (
                getattr(candidate, "status", None) == "processing_failed"
                and getattr(candidate, "failure_reason", None) == "dispatch_publish_failed"
            ):
                # The sync dispatcher committed the Job before broker
                # publication, then this process died before compensating the
                # still-running receipt. Resume through the same refund/bind
                # path instead of mislabeling a terminal Job as rendering.
                resumed_publish_failure_job_id = candidate.id
            if guided:
                guided_generation_attempt_id = str(expected_guided_attempt)
                guided_proposal_version = (
                    int(candidate_guided.get("proposal_version") or 0) or None
                    if isinstance(candidate_guided, dict)
                    else None
                )
    try:
        if resumed_publish_failure_job_id is not None:
            raise _CommittedRenderPublishFailure(resumed_publish_failure_job_id)
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
                creator_strategy=edit_plan.strategy.model_dump(mode="json", exclude_none=True),
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

            preserved_clip_order = await _previous_creator_clip_order(
                db,
                item,
                session,
                str(active.get("creator_request") or ""),
            )
            outcome = await asyncio.to_thread(
                dispatch_item_render_for,
                item_id,
                int(plan_row.ownership_epoch or 0),
                # This exact native plan has passed confirmation, ownership,
                # manifest and capability checks above. It has no guided
                # proposal; the dispatcher rechecks the zero-pool-assets fence.
                bypass_guided_edit_gate=edit_plan.strategy.render_program == "native",
                creator_strategy=edit_plan.strategy.model_dump(mode="json", exclude_none=True),
                creator_clip_order=preserved_clip_order,
                creator_request=str(active.get("creator_request") or ""),
                speech_cleanup_analysis_id=(
                    str(body.speech_cleanup_analysis_id)
                    if body.speech_cleanup_analysis_id is not None
                    else None
                ),
                speech_cleanup_choice=body.speech_cleanup_choice,
                speech_cleanup_action=speech_cleanup_recovery_action,
                expected_job_id=(
                    str(speech_cleanup_recovery_job_id)
                    if speech_cleanup_recovery_job_id is not None
                    else None
                ),
                expected_render_generation_id=speech_cleanup_recovery_generation_id,
                expected_speech_cleanup_analysis_id=(
                    str(speech_cleanup_recovery_analysis_id)
                    if speech_cleanup_recovery_analysis_id is not None
                    else None
                ),
            )
            if outcome.outcome == "already_active":
                return await _concurrent_render_response(
                    db,
                    session=session,
                    item=item,
                    user_id=user_id,
                    receipt=receipt,
                )
            if recovery_requested and outcome.outcome == "speech_cleanup_recovery_conflict":
                raise _SpeechCleanupRecoveryChanged
            if outcome.outcome == "publish_failed" and outcome.job_id:
                raise _CommittedRenderPublishFailure(uuid.UUID(outcome.job_id))
            if outcome.outcome == "invalid_clips" and getattr(outcome, "reason", None):
                raise _PhoneGateRejected(outcome.reason)
            if outcome.outcome != "dispatched":
                raise RuntimeError(f"render dispatch failed: {outcome.outcome}")
            job_id = uuid.UUID(outcome.job_id) if outcome.job_id else None
    except _SpeechCleanupRecoveryChanged as exc:
        # The sync dispatcher revalidated Job/source/analysis under its own
        # canonical lock graph and rejected a race after this controller's
        # intent receipt commit. Compensate the one reserved attempt exactly
        # once and surface a refreshable 409 instead of corrupting the session
        # into a durable generic failure with no Job.
        await db.rollback()
        changed = await _load_session(
            db,
            creator_session_id,
            user_id,
            plan_item_id,
            for_update=True,
        )
        changed_receipt = await db.get(
            CreatorAgentExecution,
            receipt_id,
            with_for_update=True,
        )
        if changed_receipt is not None and changed_receipt.status == "running":
            changed.status = "awaiting_confirmation"
            changed.render_attempts = max(0, int(changed.render_attempts or 0) - 1)
            changed.iteration_count = max(0, int(changed.iteration_count or 0) - 1)
            changed_receipt.status = "stale"
            changed_receipt.error = {"code": "speech_cleanup_recovery_changed"}
            changed_receipt.completed_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(status_code=409, detail="speech_cleanup_analysis_changed") from exc
    except _CommittedRenderPublishFailure as exc:
        # The Job is already durable and terminal. Bind that exact row to the
        # Creator session before returning failure so the outer chat route can
        # project/retry it instead of retaining the superseded failed Job.
        await db.rollback()
        failed = await _lock_confirmed_render_graph(
            db,
            item_id=plan_item_id,
            user_id=user_id,
            session_id=creator_session_id,
            job_id=exc.job_id,
        )
        failed.target_job_id = exc.job_id
        failed.status = "failed"
        failed.last_error = {
            "code": "dispatch_publish_failed",
            "message": "The render couldn't be handed to the queue. Give it another go.",
        }
        failed_receipt = await db.get(
            CreatorAgentExecution,
            receipt_id,
            with_for_update=True,
        )
        if failed_receipt is not None and failed_receipt.status == "running":
            # Publication failed before a render worker could run. Refund this
            # reserved attempt exactly once so a bypass taken at the ordinary
            # cap still has a bounded way to retry the queue handoff.
            failed.render_attempts = max(0, int(failed.render_attempts or 0) - 1)
            failed.iteration_count = max(0, int(failed.iteration_count or 0) - 1)
            failed_receipt.status = "failed"
            failed_receipt.error = failed.last_error
            failed_receipt.result = {"job_id": str(exc.job_id)}
            failed_receipt.completed_at = datetime.now(UTC)
        await append_event(
            db,
            failed,
            event_type="assistant_error",
            payload={
                "message": "I couldn't hand that render to the queue. Your direction is saved."
            },
        )
        return await _response(db, failed)
    except _PhoneGateRejected as exc:
        # The dispatch gate rejected this BEFORE a Job was minted (no queue
        # publication was ever attempted), so — unlike
        # `_CommittedRenderPublishFailure` — there is no Job row to bind.
        # Surfaces the exact typed reason instead of falling into the
        # blanket handler below's generic "execution_failed" dead end, which
        # the iOS Retry affordance could never recover from for a phone-gate
        # cause (see `app.tasks.content_plan_build.PHONE_GATE_MESSAGES`).
        from app.tasks.content_plan_build import PHONE_GATE_MESSAGES  # noqa: PLC0415

        await db.rollback()
        failed = await _load_session(db, creator_session_id, user_id, plan_item_id, for_update=True)
        code, message = PHONE_GATE_MESSAGES.get(
            exc.reason,
            (
                "execution_failed",
                "I couldn't start that render. Your creative plan is still saved.",
            ),
        )
        failed.status = "failed"
        failed.last_error = {"code": code, "message": message}
        failed_receipt = await db.get(CreatorAgentExecution, receipt_id, with_for_update=True)
        if failed_receipt:
            failed_receipt.status = "failed"
            failed_receipt.error = failed.last_error
            failed_receipt.completed_at = datetime.now(UTC)
        await append_event(
            db, failed, event_type="assistant_error", payload={"message": message, "code": code}
        )
        return await _response(db, failed)
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

    completed = (
        await _lock_confirmed_render_graph(
            db,
            item_id=plan_item_id,
            user_id=user_id,
            session_id=creator_session_id,
            job_id=job_id,
        )
        if job_id is not None
        else await _load_session(db, creator_session_id, user_id, plan_item_id, for_update=True)
    )
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


@router.post("/{item_id}/creator-agent/confirm", response_model=CreatorSessionResponse)
async def confirm_creator_plan(
    item_id: str,
    body: ConfirmBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CreatorSessionResponse:
    return await confirm_creator_plan_controller(item_id, body, user, db)


def _craft_response(receipt: CreatorAgentExecution) -> CreatorCraftResponse:
    result = receipt.result if isinstance(receipt.result, dict) else {}
    preview = result.get("preview") if isinstance(result.get("preview"), dict) else {}
    return CreatorCraftResponse(
        status=str(receipt.status),
        receipt_id=str(receipt.id),
        generation=(str(result.get("generation")) if result.get("generation") else None),
        preview=project_public_assembly_plan(preview),
    )


def _stable_manifest_fingerprint(manifest: Any) -> str:
    """Hash live policy/media context while excluding the expected render identity."""

    payload = manifest.model_dump(mode="json")
    payload["current_edit"] = None
    payload.pop("context_hash", None)
    payload.pop("manifest_hash", None)
    return canonical_context_hash(payload)


def _speech_craft_control_matches(
    assembly_plan: dict[str, Any] | None,
    *,
    operation_id: str | None,
    generation: str,
    variant_id: str | None = None,
) -> bool:
    """Return whether one private speech operation still owns its generation."""

    if not operation_id or not generation:
        return False
    control = (assembly_plan or {}).get("speech_cut_control")
    if not isinstance(control, dict):
        return False
    return bool(
        control.get("operation_id") == operation_id
        and control.get("render_generation_id") == generation
        and (variant_id is None or control.get("variant_id") == variant_id)
    )


async def _rollback_craft_commit(
    db: AsyncSession,
    *,
    receipt_id: uuid.UUID,
    session_id: uuid.UUID,
    job_id: uuid.UUID,
    previous_assembly_plan: dict | None,
    variant_id: str,
    generation: str,
    speech_cut_operation_id: str | None = None,
    error: Exception,
    previous_job_state: dict[str, Any] | None = None,
    previous_session_state: dict[str, Any] | None = None,
) -> str:
    """Undo only this craft generation when broker publication fails.

    The public target generation is the compare-and-swap guard for an ordinary
    editor craft. Required-v1 speech craft deliberately leaves that row on the
    last-good generation, so its private operation + generation pair is the
    ownership guard instead. Restore only when the applicable guard still
    matches, retaining sibling variants and unrelated assembly-plan keys.
    """

    creator_owned_keys = (
        "silence_cut_disabled",
        "speech_cut_control",
        "speech_cut_previous_variant",
        "speech_cut_previous_variants",
        "speech_cut_last_error",
    )

    await db.rollback()
    # Canonical lock order (app/db_locks.CANONICAL_LOCK_ORDER): Job ->
    # CreatorAgentSession -> receipt.  This used to lock the session first,
    # which inverted against every Job -> Session path in the Kria runtime and
    # in creation_threads; a fresh craft request overlapping broker-failure
    # rollback for the same session and Job deadlocked.
    _locked = await acquire_locked_rows(
        db,
        {
            Job: job_id,
            CreatorAgentSession: session_id if previous_session_state else None,
        },
        populate_existing=True,
    )
    locked_job = _locked.get(Job)
    locked_session = _locked.get(CreatorAgentSession)
    generation_still_owned = False
    enqueue_uncertain = False
    if locked_job is not None:
        current_assembly = copy.deepcopy(locked_job.assembly_plan or {})
        variants = list(current_assembly.get("variants") or [])
        if speech_cut_operation_id:
            rollback_disposition = classify_route_speech_cut_rollback(
                current_assembly,
                variant_id=variant_id,
                operation_id=speech_cut_operation_id,
                generation=generation,
            )
            matching_indexes = [
                index
                for index, value in enumerate(variants)
                if isinstance(value, dict) and value.get("variant_id") == variant_id
            ]
            current_index = matching_indexes[0] if len(matching_indexes) == 1 else None
            generation_still_owned = bool(
                rollback_disposition == "eligible" and current_index is not None
            )
            # Exact control plus any worker claim/private reservation means the
            # broker publication may have succeeded despite its failed response.
            # Preserve every owner and let the worker or terminalizer decide.
            enqueue_uncertain = rollback_disposition == "enqueue_uncertain" or (
                rollback_disposition == "eligible" and current_index is None
            )
        else:
            current_index = next(
                (
                    index
                    for index, value in enumerate(variants)
                    if value.get("variant_id") == variant_id
                    and value.get("render_generation_id") == generation
                ),
                None,
            )
            generation_still_owned = current_index is not None
        if current_index is not None:
            if not generation_still_owned:
                current_index = None
        if current_index is not None:
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
        if enqueue_uncertain:
            # ``running`` is the existing resumable receipt state. A retry with
            # the same idempotency key re-publishes the deterministic task id;
            # worker ownership prevents duplicate rendering/publication.
            failed_receipt.status = "running"
            failed_receipt.error = {
                "code": "craft_enqueue_uncertain",
                "message": str(error)[:300],
                "job_id": str(job_id),
                "generation": generation,
                "rolled_back": False,
            }
            failed_receipt.completed_at = None
        else:
            failed_receipt.status = "failed"
            failed_receipt.error = {
                "code": "craft_enqueue_failed",
                "message": str(error)[:300],
                "job_id": str(job_id),
                "generation": generation,
                "rolled_back": generation_still_owned,
            }
            failed_receipt.completed_at = datetime.now(UTC)
    await db.commit()
    return (
        "enqueue_uncertain"
        if enqueue_uncertain
        else ("rolled_back" if generation_still_owned else "not_owned")
    )


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
        await db.execute(
            select(SoundEffect).where(
                func.lower(SoundEffect.id) == command.sound_effect_id.strip().casefold()
            )
        )
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

    _assert_variant_generation_editable_or_409(job, variant_id)
    try:
        assert_required_speech_dispatch_quiescent(job, variant_id)
    except VariantInitialRenderInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="variant_initial_render_in_progress",
        ) from exc
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
    render_generation_id = uuid.uuid4().hex
    previous_variants = list((job.assembly_plan or {}).get("variants") or [])
    updated.update(
        {
            "ok": False,
            "render_status": "rendering",
            "render_generation_id": render_generation_id,
            "speech_cut_last_error": None,
        }
    )
    required_atomic = (job.assembly_plan or {}).get("speech_cleanup_contract") == "required_v1"
    variants = (
        previous_variants
        if required_atomic
        else [updated if v.get("variant_id") == variant_id else v for v in previous_variants]
    )
    control = {
        "variant_id": variant_id,
        "forced_removals": updated["speech_cut_in_flight"]["desired_forced_removals"],
        "desired_disabled": False,
        "prior_disabled": (job.assembly_plan or {}).get("silence_cut_disabled") is True,
        "operation": request,
        "operation_id": operation_id,
        "render_generation_id": render_generation_id,
        "finalizer_claim": None,
        "revision": request["revision"],
        "in_flight": updated["speech_cut_in_flight"],
    }
    job.assembly_plan = {
        **(job.assembly_plan or {}),
        "silence_cut_disabled": (
            bool((job.assembly_plan or {}).get("silence_cut_disabled"))
            if required_atomic
            else False
        ),
        "speech_cut_control": control,
        "speech_cut_previous_variant": variant,
        "speech_cut_previous_variants": previous_variants,
        "speech_cut_last_error": None,
        "variants": variants,
    }
    job.status = "processing"
    flag_modified(job, "assembly_plan")
    mark_reattempt(job)
    return request, operation_id, updated


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
    ):
        raise HTTPException(status_code=409, detail="Creator render target changed")
    if receipt is None or (receipt.status == "running" and not recovering_prepared):
        _assert_variant_generation_editable_or_409(job, body.expected_variant_id)
    if job.status not in PLAN_ITEM_JOB_READY and not (
        recovering_prepared and job.status == "processing"
    ):
        raise HTTPException(status_code=409, detail="Creator render target changed")

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
        recorded_generation = str(recorded_result.get("generation") or "")
        recorded_speech_operation_id = recorded_result.get("speech_cut_operation_id")
        generation_matches = current_generation == recorded_generation or (
            isinstance(recorded_speech_operation_id, str)
            and _speech_craft_control_matches(
                job.assembly_plan,
                operation_id=recorded_speech_operation_id,
                generation=recorded_generation,
                variant_id=body.expected_variant_id,
            )
        )
        if not recorded_generation or not generation_matches:
            raise HTTPException(status_code=409, detail="Creator craft execution is stale")
        return _craft_response(receipt)

    # A running receipt may have committed the new generation immediately
    # before a worker/process crash.  Reuse its exact prepared editor commit;
    # never compile against mutable post-crash state.
    prepared = existing_result.get("prepared") if isinstance(existing_result, dict) else None
    if prepared is not None:
        generation = str(existing_result.get("generation") or "")
        speech_operation_id = existing_result.get("speech_cut_operation_id")
        generation_matches = current_generation == generation or (
            isinstance(speech_operation_id, str)
            and _speech_craft_control_matches(
                job.assembly_plan,
                operation_id=speech_operation_id,
                generation=generation,
                variant_id=body.expected_variant_id,
            )
        )
        if not generation or not generation_matches:
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
        speech_generation: str | None = None
        speech_staged_variant: dict[str, Any] | None = None
        speech_public_variants: list[dict[str, Any]] | None = None
        resolved_sfx: dict[str, Any] | None = None
        try:
            speech_commands = [
                command for command in body.commands if isinstance(command, ApplySpeechCutCommand)
            ]
            if len(speech_commands) > 1:
                raise CreatorCraftValidationError("Only one speech-cut command is allowed")
            if speech_commands:
                _request, speech_operation_id, speech_staged_variant = _stage_creator_speech_cut(
                    job,
                    variant_id=body.expected_variant_id,
                    command=speech_commands[0],
                )
                control = (job.assembly_plan or {}).get("speech_cut_control")
                speech_generation = str(
                    control.get("render_generation_id") if isinstance(control, dict) else ""
                )
                if not _speech_craft_control_matches(
                    job.assembly_plan,
                    operation_id=speech_operation_id,
                    generation=speech_generation,
                    variant_id=body.expected_variant_id,
                ):
                    raise CreatorCraftValidationError(
                        "Speech-cut staging lost generation ownership"
                    )
                speech_public_variants = copy.deepcopy(
                    (job.assembly_plan or {}).get("speech_cut_previous_variants") or []
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
                if editor_commit.sound_effects is not None:
                    from app.routes.generative_jobs import (  # noqa: PLC0415
                        resolve_editor_sound_effect_placements,
                    )

                    editor_commit = editor_commit.model_copy(
                        update={
                            "sound_effects": await resolve_editor_sound_effect_placements(
                                editor_commit.sound_effects,
                                user_id=str(user.id),
                                plan_item_id=str(item.id),
                                db=db,
                            )
                        }
                    )
                if speech_operation_id:
                    # The editor gateway validates against the client-pinned
                    # last-good generation. Speech staging may already have
                    # placed its private desired state on the legacy public row,
                    # so validate the editor lanes against the exact pre-craft
                    # vector and combine them below under the speech token.
                    job.assembly_plan = {
                        **(job.assembly_plan or {}),
                        "variants": copy.deepcopy(speech_public_variants or []),
                    }
                prepared = prepare_editor_commit(
                    job,
                    body.expected_variant_id,
                    editor_commit,
                    user_id=str(user.id),
                    plan_item_id=str(item.id),
                    speech_cut_owner=(
                        (speech_operation_id, speech_generation)
                        if speech_operation_id and speech_generation
                        else None
                    ),
                )
                if speech_operation_id:
                    # The speech rerender projects creator-authored lanes from
                    # this private working snapshot onto its rebuilt source.
                    # Normalize the editor preparer's temporary generation to
                    # the one generation already owned by speech_cut_control.
                    editor_staged_variant = next(
                        (
                            value
                            for value in (job.assembly_plan or {}).get("variants") or []
                            if value.get("variant_id") == body.expected_variant_id
                        ),
                        None,
                    )
                    if editor_staged_variant is None or speech_staged_variant is None:
                        raise CreatorCraftValidationError("Speech-cut staged variant disappeared")
                    working_variant = copy.deepcopy(editor_staged_variant)
                    for field in (
                        "speech_cut_candidates",
                        "speech_cut_forced_removals",
                        "speech_cuts_disabled",
                        "speech_cut_in_flight",
                        "speech_cut_revision",
                        "speech_cut_last_error",
                    ):
                        if field in speech_staged_variant:
                            working_variant[field] = copy.deepcopy(speech_staged_variant[field])
                    working_variant.update(
                        {
                            "render_generation_id": speech_generation,
                            "render_status": "rendering",
                            "ok": False,
                        }
                    )
                    prepared["generation"] = speech_generation
                    required_atomic = (job.assembly_plan or {}).get(
                        "speech_cleanup_contract"
                    ) == "required_v1"
                    public_variants = (
                        copy.deepcopy(speech_public_variants or [])
                        if required_atomic
                        else [
                            copy.deepcopy(working_variant)
                            if value.get("variant_id") == body.expected_variant_id
                            else value
                            for value in (speech_public_variants or [])
                        ]
                    )
                    job.assembly_plan = {
                        **(job.assembly_plan or {}),
                        "speech_cut_previous_variant": working_variant,
                        "speech_cut_previous_variants": copy.deepcopy(speech_public_variants or []),
                        "variants": public_variants,
                    }
            elif speech_operation_id:
                if speech_staged_variant is None or not speech_generation:
                    raise CreatorCraftValidationError("Speech-cut staged variant disappeared")
                working_variant = copy.deepcopy(speech_staged_variant)
                working_variant.update(
                    {
                        "render_generation_id": speech_generation,
                        "render_status": "rendering",
                        "ok": False,
                    }
                )
                required_atomic = (job.assembly_plan or {}).get(
                    "speech_cleanup_contract"
                ) == "required_v1"
                public_variants = (
                    copy.deepcopy(speech_public_variants or [])
                    if required_atomic
                    else [
                        copy.deepcopy(working_variant)
                        if value.get("variant_id") == body.expected_variant_id
                        else value
                        for value in (speech_public_variants or [])
                    ]
                )
                job.assembly_plan = {
                    **(job.assembly_plan or {}),
                    "speech_cut_previous_variant": working_variant,
                    "speech_cut_previous_variants": copy.deepcopy(speech_public_variants or []),
                    "variants": public_variants,
                }
                prepared = {
                    "generation": speech_generation,
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
    enqueue_uncertain = False
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
            rollback_disposition = await _rollback_craft_commit(
                db,
                receipt_id=receipt_id,
                session_id=session.id,
                job_id=expected_job_id,
                previous_assembly_plan=previous_assembly_plan,
                variant_id=body.expected_variant_id,
                generation=generation,
                speech_cut_operation_id=speech_operation_id,
                error=exc,
                previous_job_state=previous_job_state,
                previous_session_state=(previous_session_state if manage_session_state else None),
            )
            enqueue_uncertain = rollback_disposition == "enqueue_uncertain"
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
            detail=(
                "The queue acknowledgement was interrupted; your render may still complete."
                if enqueue_uncertain
                else "The creator treatment could not be queued; the current video is unchanged."
            ),
        ) from exc

    receipt.status = "succeeded"
    receipt.error = None
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
    budget = max(0, int(session.max_render_attempts or 0) - int(session.render_attempts or 0))
    decision = evaluate_auto_iteration(
        marker,
        opted_in=True,
        render_budget_remaining=budget,
        automatic_revision_count=int(session.automatic_revision_count or 0),
    )
    if decision.decision != "eligible":
        session.auto_iteration_opt_in = True
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
    if not prepared_recovery:
        # This is outside the fail-open craft block: an initial required-speech
        # owner is a write barrier, not an unavailable optional treatment. Do
        # not persist opt-in/events/controller state or convert its stable 409
        # into a successful session response.
        _assert_variant_generation_editable_or_409(job, str(session.target_variant_id))
    session.auto_iteration_opt_in = True
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
