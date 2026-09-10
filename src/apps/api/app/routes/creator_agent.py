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
from app.agents._runtime import AiBudgetExceededError, RunContext, TerminalError
from app.agents._schemas.creator_agent import (
    CREATOR_REQUEST_MAX_CHARS,
    ApplySpeechCutCommand,
    AskUser,
    ContextLabelIntent,
    CreativeStrategy,
    CreatorCraftBundle,
    CreatorEditPlan,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
    ReviewDecision,
    SetLicensedSfxCommand,
    canonical_context_hash,
    normalize_creator_text_color,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_GUIDED_VOICEOVER,
    GUIDED_VOICEOVER_EXECUTION_CONTRACT,
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
    MixedMediaTimingUnavailableError,
    MontageCadenceUnavailableError,
    normalize_creator_strategy_media,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.db_locks import acquire_locked_rows
from app.limiter import limiter
from app.models import (
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
from app.schemas.edit_proposal import (
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
    reconcile_render_state,
    resolve_item_creator_context,
    rollout_eligible,
    serialize_session,
)
from app.services.edit_direction_planner import round_robin_capacity_s
from app.services.job_phases import mark_reattempt
from app.services.job_status import PLAN_ITEM_JOB_READY, PLAN_ITEM_JOB_TERMINAL
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
    await db.commit()
    loaded = await _load_session(db, session.id, session.creator_id, session.plan_item_id)
    return _creator_session_response(loaded)


def _conversation(events: list[CreatorAgentEvent]) -> list[dict[str, str]]:
    return [
        {
            "role": event.role,
            "content": str((event.payload or {}).get("message") or "")[:CREATOR_REQUEST_MAX_CHARS],
        }
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
    return "\n".join(messages)[:CREATOR_REQUEST_MAX_CHARS]


def _explicit_media_scope(request: str) -> Literal["all", "selected"]:
    """Resolve only an explicit all-media request; preserve the legacy default."""

    normalized = " ".join(str(request or "").casefold().split())
    if re.search(
        r"\b(?:do not|don't|dont|never|without|no)\b.{0,40}"
        r"\b(?:use|include|keep|select)\b.{0,20}\b(?:all|everything|every)\b"
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
    if re.search(
        r"\b(?:only|just)\s+(?:the\s+)?(?:selected|specified|chosen|listed)\b"
        r"|\bselected\s+(?:media|files?|clips?)\b",
        normalized,
    ):
        return "selected"
    return "selected"


def _has_explicit_media_scope(request: str) -> bool:
    normalized = " ".join(str(request or "").casefold().split())
    return bool(
        re.search(
            r"\b(?:all|every|each)\s+(?:the\s+)?(?:images?|photos?|videos?|clips?|media|footage)\b"
            r"|\buse\s+(?:all|everything)\b"
            r"|\b(?:all|every)\s+(?:uploaded|provided)\s+(?:media|files?|images?|photos?|videos?)\b"
            r"|\b(?:do not|don't|dont|never|without|no)\b.{0,40}"
            r"\b(?:use|include|keep|select)\b.{0,20}\b(?:all|everything|every)\b"
            r"|\b(?:all|everything|every)\b.{0,20}\b(?:not|excluded|omit)\b"
            r"|\b(?:only|just)\s+(?:the\s+)?(?:selected|specified|chosen|listed)\b"
            r"|\bselected\s+(?:media|files?|clips?)\b",
            normalized,
        )
    )


def _explicit_guided_voiceover_request(request: str, manifest: Any) -> bool:
    return bool(
        manifest.has_voiceover
        and (
            recognize_mixed_media_timing(request) is not None
            or _explicit_media_scope(request) == "all"
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
        for field in ("opening_title", "font_family", "text_color"):
            quote = " ".join(str(getattr(render_intent_evidence, field) or "").split())
            if not quote or not any(quote in source for source in creator_sources):
                continue
            value = getattr(strategy, field)
            # Typography and color are semantic choices from a validated
            # schema. Literal on-screen words must also occur in the excerpt.
            if field == "opening_title" and value is not None:
                if " ".join(value.split()) not in quote:
                    continue
            semantic_updates[field] = value
    # Ungrounded model values cannot become pixels. Only creator excerpts or
    # the legacy literal recognizer below can restore these fields.
    updates: dict[str, object] = {
        "opening_title": None,
        "font_family": None,
        "text_color": None,
        # A model-authored label is only retained when the typed companion
        # flag records the same intent.  This keeps an unrelated request from
        # inheriting a stale context label while allowing multilingual or
        # otherwise non-regex wording to survive the boundary.
        "context_label": strategy.context_label if strategy.sport_labels else None,
        "image_layout": None,
        "licensed_sfx": None,
        "execution_contract": strategy.execution_contract,
        "media_scope": (
            _explicit_media_scope(creator_request)
            if _has_explicit_media_scope(creator_request)
            else strategy.media_scope
        ),
        "participant_labels": strategy.participant_labels,
        "score_labels": strategy.score_labels,
        "sport_labels": strategy.sport_labels,
    }

    latest = " ".join(str(latest_user_message or "").casefold().split())
    combined = " ".join(request.casefold().split())
    latest_negates = lambda *terms: bool(  # noqa: E731
        re.search(
            r"\b(?:do not|don't|dont|never|without|no)\b.{0,60}\b(?:"
            + "|".join(re.escape(term) for term in terms)
            + r")\b",
            latest,
            re.IGNORECASE,
        )
    )

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
    title_match = re.search(
        r"[\"'“‘](.{1,280}?)[\"'”’]\s+(?:opening\s+)?(?:title|intro|hook|text)\b",
        request,
        re.IGNORECASE,
    )
    if title_match is None:
        title_match = re.search(
            r"\b(?:opening\s+)?(?:title|intro|hook|text)(?!\s+(?:texts|copies)\b)"
            r"\s*(?:text|copy)?\b\s*"
            r"(?:is|to|should\s+say|saying|that\s+says|which\s+says|as|:)?\s*"
            r"[\"'“‘](.{1,280}?)[\"'”’]",
            request,
            re.IGNORECASE,
        )
    if title_match is None:
        # Chat-first users commonly omit quoting for a short title. Stop at
        # the first comma or style qualifier so the rest of the request can
        # never become on-screen copy. Unlike quoted copy, an unquoted title
        # needs an explicit connector ("is", "should say", or a colon); a
        # bare "add intro text" is a treatment directive, not literal pixels.
        title_match = re.search(
            r"\b(?:opening\s+)?(?:title|intro|hook)(?!\s+(?:texts|copies)\b)"
            r"\s*(?:text|copy)?\b\s*"
            r"(?:is|to|should\s+say|saying|that\s+says|which\s+says|as|:)\s*"
            r"([A-Za-z0-9][^,\n]{0,279}?)(?=\s*(?:,|$)|\s+(?:using|with|font|colou?r)\b"
            r"|\s+(?:use|make|set)\b(?=[^.]{0,80}\b(?:font|text|colou?r)\b))",
            request,
            re.IGNORECASE,
        )
    if title_match is None:
        # The legacy title grammar permits multi-sentence literal copy. Keep
        # that contract intact; the newer unquoted "text saying" fallback
        # stops at punctuation before a following style sentence.
        title_match = re.search(
            r"\b(?:opening\s+)?(?:text)(?!\s+(?:texts|copies)\b)"
            r"\s*(?:text|copy)?\b\s*"
            r"(?:is|to|should\s+say|saying|that\s+says|which\s+says|as|:)\s*"
            r"([A-Za-z0-9][^,.;!?\n]{0,279}?)(?=\s*(?:[,.;!?]|$)|\s+(?:using|with|font|colou?r)\b"
            r"|\s+(?:use|make|set)\b(?=[^.]{0,80}\b(?:font|text|colou?r)\b))",
            request,
            re.IGNORECASE,
        )
    if title_match is not None:
        clause_start = max(
            request.rfind(delimiter, 0, title_match.start())
            for delimiter in (".", "!", "?", ";", ",")
        )
        clause_prefix = request[clause_start + 1 : title_match.start()]
        if re.search(r"\b(?:do\s+not|don't|dont|without|no)\b", clause_prefix, re.IGNORECASE):
            title_match = None
    if title_match and title_match.group(1).strip():
        updates["opening_title"] = title_match.group(1).strip()

    from app.agents._schemas.text_element import _ALLOWED_FONTS  # noqa: PLC0415

    # Match the canonical registry names directly (longest first), rather
    # than greedily capturing prose such as “using Rascal” as the font.
    for name in sorted(_ALLOWED_FONTS, key=len, reverse=True):
        escaped = re.escape(name)
        if re.search(
            rf"(?<!\w){escaped}(?!\w)\s+font\b|"
            rf"\b(?:font|typeface)\s*(?:is|to|:)?\s*{escaped}(?!\w)|"
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

    # This is intent, not model-authored copy. The trusted render pipeline
    # must resolve the actual label from server-side evidence before rendering.
    sport_label_requested = bool(
        re.search(
            r"\b(?:name|label|text)\s+(?:of\s+)?(?:the\s+)?sports?\b"
            r"|\b(?:label|identify|show|display|add|highlight)\b.{0,80}\b(?:each\s+)?sports?\b"
            r"|\bsports?\b.{0,80}\b(?:bottom\s+right|bottom-right)\b",
            request,
            re.IGNORECASE,
        )
    )
    if sport_label_requested:
        updates["context_label"] = ContextLabelIntent(
            kind="sport",
            source="clip_metadata",
            placement="bottom_right",
            size="small",
            per_clip=True,
        )
        updates["sport_labels"] = True

    participant_label_requested = bool(
        re.search(
            r"\b(?:placeholder|temporary|player|participant)\s+(?:name|label)s?\b"
            r"|\bname\s+(?:each|every|the)\s+(?:player|participant)\b",
            combined,
            re.IGNORECASE,
        )
    )
    if participant_label_requested:
        updates["participant_labels"] = "single_subject"
    if latest_negates("player", "participant", "placeholder", "name"):
        updates["participant_labels"] = "none"
    score_requested = bool(
        re.search(
            r"\b(?:add|show|include|highlight|display|use)\b.{0,80}\bscore(?:s)?\b"
            r"|\bscore(?:s)?\b.{0,80}\b(?:audio|spoken|mentioned|narration|voiceover)\b",
            combined,
            re.IGNORECASE,
        )
    )
    if score_requested and not latest_negates("score", "scores"):
        updates["score_labels"] = True
    if latest_negates("score", "scores"):
        updates["score_labels"] = False
    if latest_negates("sport", "sports"):
        updates["sport_labels"] = False
        updates["context_label"] = None

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
    if latest and _has_explicit_media_scope(latest):
        updates["media_scope"] = _explicit_media_scope(latest)
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


def _latest_planned_cadence(
    events: list[CreatorAgentEvent],
) -> tuple[MontageCadenceConstraint | None, int | None]:
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
        return cadence, int(target_s) if isinstance(target_s, (int, float)) else None
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
    target_duration_s: int | None = None,
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


def _balanced_integer_duration_s(*, limit_s: float, cycle_s: float) -> int:
    """Find the longest whole-second target made of complete cadence cycles."""

    cycle_count = int((limit_s + 0.001) // cycle_s)
    for cycles in range(cycle_count, 0, -1):
        duration_s = cycles * cycle_s
        rounded_s = round(duration_s)
        if 3 <= rounded_s <= 60 and abs(duration_s - rounded_s) <= 0.001:
            return int(rounded_s)
    return 0


def _next_balanced_integer_duration_s(*, minimum_s: float, limit_s: float, cycle_s: float) -> int:
    """Find the shortest whole-second complete-cycle target above a lower bound."""

    first_cycle = max(1, math.ceil((minimum_s - 0.001) / cycle_s))
    last_cycle = int((limit_s + 0.001) // cycle_s)
    for cycles in range(first_cycle, last_cycle + 1):
        duration_s = cycles * cycle_s
        rounded_s = round(duration_s)
        if 3 <= rounded_s <= 60 and abs(duration_s - rounded_s) <= 0.001:
            return int(rounded_s)
    return 0


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
) -> tuple[MontageCadenceConstraint | None, int | None]:
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
                    or int(prior.get("target_duration_s") or 0)
                    or recognize_total_duration_s(creator_request)
                    or None,
                )
            recommendation = str(prior.get("recommendation") or "")
            requested_s = int(prior.get("requested_duration_s") or 24)
            recommended_s = int(prior.get("recommended_duration_s") or 0)
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
            target_match = re.search(r"\b(\d{1,2})\s*(?:seconds?|secs?|s)\b", normalized)
            if target_match:
                return cadence, int(target_match.group(1))

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


def _pinned_narration_target_duration_s(manifest: Any) -> int | None:
    narration = getattr(manifest, "narration", None)
    if not getattr(manifest, "has_voiceover", False) or narration is None:
        return None
    duration_s = int(round(float(narration.duration_s)))
    max_duration_s = int(getattr(getattr(manifest, "limits", None), "max_output_duration_s", 60))
    return max(3, min(max_duration_s, duration_s))


def _fallback_strategy(manifest: Any, *, user_message: str = "") -> CreativeStrategy:
    """Build a compiler-safe draft from pinned state and explicit request facts only."""

    current_format = manifest.edit_format
    current_available = manifest.capabilities.get(f"edit_format:{current_format}")
    safe_format = current_format if current_available and current_available.available else "montage"
    mixed_media_timing = recognize_mixed_media_timing(user_message)
    media_scope = (
        _explicit_media_scope(user_message) if _has_explicit_media_scope(user_message) else None
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
    max_duration_s = int(getattr(getattr(manifest, "limits", None), "max_output_duration_s", 60))
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
) -> CreatorSessionResponse:
    item, plan, persona = await _owned_context(db, item_id, user.id)
    session = await _load_session(db, session_id, user.id, item.id)
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
            payload={
                "message": "Add at least one clip first, then I can design the edit around it."
            },
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
    # Reserve the session's existing eight-call allowance under the row lock
    # before contacting Pro. The previous post-call increment allowed the
    # ninth request to reach Gemini and only failed the session afterwards.
    locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
    if locked.revision != expected_revision or locked.status not in {"planning", "revising"}:
        raise HTTPException(status_code=409, detail="Creator session changed while planning")
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
    action: AskUser | ProposeStrategy | ReviewDecision
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
    except AiBudgetExceededError:
        locked = await _load_session(db, session.id, user.id, item.id, for_update=True)
        if locked.revision == expected_revision and locked.status in {"planning", "revising"}:
            locked.agent_call_count = max(0, locked.agent_call_count - 1)
            locked.status = "briefing"
            await db.commit()
        raise
    except TerminalError as exc:
        log.warning(
            "main_creator.planning_fallback", session_id=str(session.id), error=str(exc)[:300]
        )
        action = ProposeStrategy(
            kind="propose_strategy",
            strategy=_fallback_strategy(manifest, user_message=creator_request),
            summary=MAIN_CREATOR_FALLBACK_SUMMARY,
        )

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
                balanced_s = _balanced_integer_duration_s(limit_s=limit_s, cycle_s=cycle_s)
                if balanced_s < 3:
                    # A minimum-length request can fall between complete
                    # cycles (for example 3s requested with a 2s A/B cycle).
                    # Recommend the next renderable cycle instead of offering
                    # reuse, which cannot make an incomplete cycle valid.
                    expansion_limit_s = 60 if cadence.reuse_policy == "allow_repeat" else capacity_s
                    balanced_s = _next_balanced_integer_duration_s(
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
            locked.active_plan = compile_active_plan(
                locked,
                manifest=manifest,
                strategy=strategy,
                summary=MAIN_CREATOR_FALLBACK_SUMMARY,
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
) -> CreatorSessionResponse:
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
        allow_chat=allow_chat,
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
        allow_chat=allow_chat,
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
    optional_values = {
        "execution_contract": plan.strategy.execution_contract,
        "media_scope": plan.strategy.media_scope if plan.strategy.media_scope == "all" else None,
        "participant_labels": plan.strategy.participant_labels,
        "score_labels": plan.strategy.score_labels,
        "sport_labels": plan.strategy.sport_labels,
        "narration": (
            {**narration_identity.model_dump(mode="json"), "words": []}
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
) -> CreatorSessionResponse:
    _require_feature(user.id, execution=True, allow_chat=allow_chat)
    recovery_values = (
        speech_cleanup_recovery_action,
        speech_cleanup_recovery_job_id,
        speech_cleanup_recovery_generation_id,
        speech_cleanup_recovery_analysis_id,
    )
    recovery_requested = any(value is not None for value in recovery_values)
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
        if manifest.manifest_hash != session.manifest_hash:
            raise HTTPException(
                status_code=409, detail="Footage or capabilities changed; review the plan again"
            )
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
            expected_manifest_hash=manifest.manifest_hash,
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
            and (guided_matches or (not guided and candidate_strategy == expected_strategy))
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
