"""Runtime-v2 planner adapter over Nova's production creative intelligence.

The planner returns inert intents only.  It never receives storage paths and
cannot author risk, target pins, idempotency identities, or completion claims.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents._schemas.creator_agent import (
    AskUser,
    ProposeStrategy,
    ResolvedCreatorManifest,
    ReviewDecision,
)
from app.agents._schemas.edit_format import CLIP_INTENT_FREE_EDIT_FORMATS
from app.agents.main_creator import (
    MAIN_CREATOR_CONVERSATION_MAX,
    MainCreatorAgent,
    MainCreatorInput,
    MainCreatorOutput,
)
from app.config import settings
from app.kria.brief import (
    BriefUpdate,
    CreativeBrief,
    CurrentPlanShape,
    Route,
    apply_updates,
    load_latest_brief,
    new_requirements,
    plan_shape_from_editor_snapshot,
    render_brief_request,
    route_requirements,
)
from app.kria.contracts import KriaTurnPlan
from app.kria.strategy_policy import RefusedStrategy, check_strategy_for_runtime_v2
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    CreatorEditDraft,
    Job,
    Persona,
    PlanItem,
)
from app.routes._copilot import CopilotTurnBody, is_overlay_display_ask, run_copilot_turn
from app.routes.generative_jobs import variant_render_baseline
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services.choice_questions import (
    CONFLICT_ORDER_VS_GROUP,
    ORDER_VS_GROUP_OPTIONS,
    build_choice_question,
    choice_question_text,
    detect_order_vs_group,
    fold_choice_answers,
)
from app.services.clip_intent_answers import persist_clip_intent_vision_answers
from app.services.clip_intent_planning import plan_and_resolve_clip_intents
from app.services.clip_selection import fold_clip_selections
from app.services.creator_sessions import (
    creator_context,
    load_intent_clips_for_item,
    resolve_item_creator_context,
)
from app.services.kria_editor_ops import (
    EDITOR_STATE_STALE_REPLY,
    MAX_EDITOR_OPS,
    SPEECH_CUT_NEEDS_SAVE_REPLY,
    EditorStateStaleError,
    build_editor_snapshot,
    clip_facts_by_media_id,
    clip_seen_by_media_id,
    coalesce_text_style_ops,
    editor_state_has_lanes,
    resolve_editor_base,
)
from app.services.song_order import (
    build_song_order_question,
    fold_song_orders,
    load_ready_alignment,
    resolve_uncertain_takes,
    resolved_song_takes_payload,
    song_order_question_text,
    thread_keeps_lipsync,
    uncertain_media_ids,
)

log = structlog.get_logger()


@dataclass(frozen=True)
class PlannedKriaTurn:
    plan: KriaTurnPlan
    manifest_hash: str | None
    context_hash: str | None
    # KRI-188 (all empty/None when the Creative Brief is off for the creator):
    # requirements this turn newly stated, the deterministic router verdict, and
    # the footage ids the receipt checkers measure coverage against. Persisted
    # by the turn-completion transaction, never here (a requeued turn must not
    # write a brief version).
    brief_updates: tuple[BriefUpdate, ...] = ()
    brief_route: Route | None = None
    brief_clip_ids: tuple[str, ...] = ()
    # The manifest this turn planned against, so the receipt checks resolve
    # reaction beats (owned images, capability) exactly as approval will.
    brief_manifest: ResolvedCreatorManifest | None = None
    # KRI-219 latency: the editor copilot served this in-place edit BEFORE the slow
    # Main Creator requirement extraction; the completed turn schedules that
    # extraction off the critical path (`extract_deferred_brief`).
    defer_brief: bool = False
    # KRI-142: what the server strategy check repaired or left out, so the
    # receipts reply still says it when it replaces the model's summary.
    policy_notices: tuple[str, ...] = ()


def adapt_creator_action(
    action: AskUser | ProposeStrategy | ReviewDecision,
    *,
    server_clip_intents: list[ClipIntent] | None = None,
    server_resolved_clip_intents: list[ResolvedClipIntent] | None = None,
    server_resolved_song_takes: list[dict[str, Any]] | None = None,
    ordering_choice: str | None = None,
) -> KriaTurnPlan:
    """``server_resolved_song_takes`` (KRI-374) is the ONLY way ``resolved_song_takes``
    survives into the draft: it is server-owned (the song-order gate writes it after the
    creator answered), so whatever a model-authored strategy carried is discarded here."""
    if isinstance(action, AskUser):
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=action.question,
        )
    if isinstance(action, ReviewDecision):
        return KriaTurnPlan(
            mode="respond",
            turn_value="review",
            response=action.summary or "The current cut is ready for your review.",
        )
    summary = action.summary.strip() or action.strategy.rationale.strip()
    if not summary:
        summary = "I shaped a focused draft around the strongest available footage."
    server_owned_intents = (
        server_clip_intents is not None or server_resolved_clip_intents is not None
    )
    requested_intents = (
        server_clip_intents
        if server_owned_intents
        else [
            intent
            for intent in (action.strategy.clip_intents or [])
            if intent.label_source == "transcript"
        ]
    )
    strategy_update = {
        "clip_intents": requested_intents or None,
        "resolved_clip_intents": server_resolved_clip_intents or None,
        "resolved_song_takes": server_resolved_song_takes or None,
        # KRI-282: server-owned, always overwritten (a model-authored value is dropped).
        "ordering_choice": ordering_choice,
    }
    return KriaTurnPlan(
        mode="act",
        turn_value="action",
        evidence_ids=["trusted-project-manifest"],
        intents=[
            {
                "intent_id": "apply-strategy",
                "tool_name": "draft.apply_strategy",
                "tool_version": 1,
                "arguments": {
                    # KRI-127: model-authored intent fields are untrusted.
                    # This path accepts values only from the server resolver.
                    "strategy": action.strategy.model_copy(update=strategy_update).model_dump(
                        mode="json", exclude_none=True
                    ),
                    "summary": summary,
                },
            },
            {
                "intent_id": "request-render",
                "tool_name": "render.request",
                "tool_version": 1,
                "arguments": {},
                "depends_on": ["apply-strategy"],
            },
        ],
    )


def _full_creator_request(rows: list[CreationThreadEvent], *, current_message: str) -> str | None:
    """Return complete chronological user instruction text or fail closed.

    Intent extraction must see every creator instruction. Unlike the bounded
    model conversation, this input is never truncated: a request that exceeds
    the planner's safe limit gets a recovery turn instead of silently losing
    an earlier constraint.
    """
    messages = [
        str(row.content).strip()
        for row in rows
        if row.role == "user" and row.content and str(row.content).strip()
    ]
    current = current_message.strip()
    if current and messages and messages[-1] == current:
        messages.pop()
    if current:
        messages.append(current)
    request = "\n".join(messages)
    return request if len(request) <= 12_000 else None


_CLIP_INTENT_PENDING_REPLY = (
    "I'm still checking some of your clips against that request. "
    "Send it again in a moment and I'll pick up where I left off."
)


def _clip_intent_resolution_plan(
    *,
    question: str | None,
    status: str,
    diagnostics: dict[str, Any] | None = None,
    clip_question: dict[str, Any] | None = None,
) -> KriaTurnPlan:
    if status == "needs_creator":
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=question or "Which clips should I use for that part?",
            diagnostics=_safe_diagnostics(status, diagnostics),
            clip_question=clip_question,
        )
    # KRI-282: `pending` is NOT a failure -- the foreground vision budget ran out
    # with answers already cached for the next turn. Saying "couldn't match" made
    # a converging retry look like a permanent failure.
    return KriaTurnPlan(
        mode="respond",
        turn_value="recovery",
        response=(
            _CLIP_INTENT_PENDING_REPLY
            if status == "pending"
            else "I couldn't reliably match that request to your clips. Please try again shortly."
        ),
        diagnostics=_safe_diagnostics(status, diagnostics),
    )


# -- KRI-374: uncertain-takes gate for lip-sync song montages ------------------

_SONG_ALIGNMENT_POLL_S = 1.5


def _is_lipsync_song_strategy(strategy: Any) -> bool:
    """`audio_strategy`/`song_sync` are read defensively (Lane C adds them to
    CreativeStrategy); anything that is not a user-song lip-sync strategy is untouched."""
    return (
        getattr(strategy, "audio_strategy", None) == "user_song"
        and getattr(strategy, "song_sync", None) == "lipsync"
    )


def _song_take_ids(manifest: Any, strategy: Any) -> list[str]:
    """The raw video takes this strategy places: owned video media, narrowed to the
    strategy's own selection when it made one (curated `asset-*` stock is never a take)."""
    selected = set(getattr(strategy, "selected_media_ids", None) or ())
    return [
        media.media_id
        for media in getattr(manifest, "media", None) or ()
        if media.kind == "video"
        and not media.media_id.startswith("asset-")
        and (not selected or media.media_id in selected)
    ]


@dataclass(frozen=True)
class _SongGateResult:
    """`plan` set => answer the turn with it (pending / question). Otherwise proceed,
    with `resolved_takes` (the strategy's `resolved_song_takes`) when the creator's
    confirmed order was applied, and `strategy` when the gate had to keep a lip-sync
    strategy the model flipped away from mid-exchange."""

    plan: KriaTurnPlan | None = None
    resolved_takes: list[dict[str, Any]] | None = None
    strategy: Any | None = None


_SONG_PENDING_REPLY = (
    "I'm still analysing your song and matching your clips to it. "
    "Send your message again in a moment and I'll pick up where I left off."
)


def _song_pending_plan() -> KriaTurnPlan:
    """The wait-timed-out reply. Its own wording: the clip-picker copy ("checking some
    of your clips against that request") describes something else entirely."""
    return KriaTurnPlan(
        mode="respond",
        turn_value="recovery",
        response=_SONG_PENDING_REPLY,
        diagnostics=_safe_diagnostics(
            "pending", {"stage": "song_alignment", "reason": "song_alignment_pending"}
        ),
    )


def _with_song_sync(strategy: Any, song_sync: str) -> Any:
    if hasattr(strategy, "model_copy"):
        return strategy.model_copy(update={"song_sync": song_sync})
    clone = copy.copy(strategy)
    clone.song_sync = song_sync
    return clone


async def _load_thread_events(db: AsyncSession, thread_id: uuid.UUID) -> list[tuple[str, Any]]:
    rows = (
        await db.execute(
            select(CreationThreadEvent.role, CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role.in_({"user", "assistant"}),
            )
            .order_by(CreationThreadEvent.sequence)
        )
    ).all()
    await db.rollback()  # no connection pinned across what follows
    return [(role, payload) for role, payload in rows]


async def _song_order_gate(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    manifest: Any,
    strategy: Any,
) -> _SongGateResult:
    """Hold a lip-sync song strategy until every take has a trustworthy song position.

    1. alignment missing/stale/incomplete -> bounded wait
       (`song_alignment_turn_deadline_s`), then the "still checking" pending reply;
    2. any ambiguous/unmatched take and no folded creator answer -> `song_order_question`;
    3. answered -> `resolve_uncertain_takes` -> `resolved_takes`;
    4. all takes confident -> proceed untouched.
    """
    if not settings.user_song_montage_enabled:
        return _SongGateResult()
    lipsync = _is_lipsync_song_strategy(strategy)
    if not lipsync and getattr(strategy, "audio_strategy", None) != "user_song":
        return _SongGateResult()
    take_ids = _song_take_ids(manifest, strategy)
    if not take_ids:
        return _SongGateResult()

    events: list[tuple[str, Any]] | None = None
    kept_strategy: Any | None = None
    if not lipsync:
        # The answer turn re-runs the model, which may now say `background`. Without
        # this the gate is skipped and the creator's confirmed order is dropped.
        events = await _load_thread_events(db, thread_id)
        if not thread_keeps_lipsync(events, None):
            return _SongGateResult()
        kept_strategy = _with_song_sync(strategy, "lipsync")
        strategy = kept_strategy

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(settings.song_alignment_turn_deadline_s))
    song_generation: int | None = None
    while True:
        item = await db.get(PlanItem, item_id, populate_existing=True)
        if item is None:
            raise RuntimeError("Kria target item is unavailable")
        song_generation = getattr(item, "song_generation", None)
        alignment = load_ready_alignment(
            getattr(item, "song_alignment", None),
            media_ids=take_ids,
            song_generation=song_generation,
            raw_analysis=getattr(item, "song_analysis", None),
        )
        await db.rollback()  # never hold a read snapshot across the sleep
        if alignment is not None:
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            return _SongGateResult(plan=_song_pending_plan())
        await asyncio.sleep(min(_SONG_ALIGNMENT_POLL_S, remaining))

    if kept_strategy is not None and events is not None:
        # Only keep lip-sync for an exchange about THIS song generation.
        if not thread_keeps_lipsync(events, song_generation):
            return _SongGateResult()

    if not uncertain_media_ids(alignment, take_ids):
        return _SongGateResult(strategy=kept_strategy)
    if events is None:
        events = await _load_thread_events(db, thread_id)
    folded = fold_song_orders(events, song_generation)
    if folded and folded.covers(take_ids):
        resolved = resolve_uncertain_takes(alignment, folded.ordered_media_ids)
        return _SongGateResult(
            resolved_takes=resolved_song_takes_payload(resolved), strategy=kept_strategy
        )
    question = build_song_order_question(alignment, take_ids, song_generation=song_generation)
    return _SongGateResult(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=song_order_question_text(question),
            song_order_question=question,
        )
    )


def _with_resolved_song_takes(strategy: Any, takes: list[dict[str, Any]]) -> Any:
    """Write the server-owned `resolved_song_takes`:
    `[{media_id, delta_s: float | None, status: "confident"|"unmatched",
    confirmed_by_creator: bool}]` (delta_s None => B-roll, never placed by song time)."""
    if hasattr(strategy, "model_copy"):
        return strategy.model_copy(update={"resolved_song_takes": takes})
    clone = copy.copy(strategy)
    clone.resolved_song_takes = takes
    return clone


_DIAGNOSTIC_SCALARS = (str, int, float, bool)


def _resolution_diagnostics(resolution: Any) -> dict[str, Any]:
    base = {"reason": resolution.error_code or resolution.status}
    if resolution.deferred_queries:
        base["deferred_queries"] = len(resolution.deferred_queries)
    return {**base, **(resolution.diagnostics or {})}


def _safe_diagnostics(status: str, diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    """Whitelist-shaped, size-bounded diagnostics: scalars, short scalar lists only."""
    out: dict[str, Any] = {"status": status}
    for key, value in (diagnostics or {}).items():
        if isinstance(value, _DIAGNOSTIC_SCALARS):
            out[str(key)[:40]] = value if not isinstance(value, str) else value[:80]
        elif isinstance(value, list) and all(isinstance(v, _DIAGNOSTIC_SCALARS) for v in value):
            out[str(key)[:40]] = [v if not isinstance(v, str) else v[:80] for v in value[:24]]
    return out


def adapt_editor_action(
    *, reply: str, ops: list[dict], request_render: bool = False
) -> KriaTurnPlan:
    """Draft edits are reversible; rendering is a distinct, policy-gated action."""
    if not ops:
        return KriaTurnPlan(mode="respond", turn_value="question", response=reply)
    # "Change all fonts" arrives as one op per bar; merge identical per-bar style
    # patches so a many-bar edit fits the MAX_EDITOR_OPS tool bound instead of failing
    # the whole turn (KRI-203).
    ops = coalesce_text_style_ops(ops)
    if len(ops) > MAX_EDITOR_OPS:
        return KriaTurnPlan(
            mode="respond",
            turn_value="recovery",
            response=(
                "That is more changes than I can apply in one go, so I left the video "
                "as it was. Ask for it in smaller steps, or for all of one kind of "
                "change at once (for example every font)."
            ),
        )
    intents = [
        {
            "intent_id": "apply-editor-ops",
            "tool_name": "draft.apply_editor_ops",
            "tool_version": 1,
            "arguments": {"operations": ops, "summary": reply},
        },
    ]
    if request_render:
        intents.append(
            {
                "intent_id": "request-render",
                "tool_name": "render.request",
                "tool_version": 1,
                "arguments": {},
                "depends_on": ["apply-editor-ops"],
            }
        )
    return KriaTurnPlan(
        mode="act", turn_value="action", evidence_ids=["trusted-editor-snapshot"], intents=intents
    )


@dataclass(frozen=True)
class _EditorTarget:
    """Immutable projection of the current editable render (copied rows only)."""

    job_id: uuid.UUID
    snapshot: dict
    conversation: list[dict]
    # The projected variant the snapshot was built from (plain JSON copy). Only
    # the dev harness (`app.cli.kria_ask`) reads it, to dry-run the compiler.
    variant: dict | None = None


async def _copilot_clip_context(
    db: AsyncSession,
    *,
    thread: CreationThread,
    thread_id: uuid.UUID,
    job: Job,
    variant: dict,
    item: PlanItem,
) -> dict:
    """KRI-191: the Creative Brief and per-clip facts the copilot may see.

    Context only ever ADDS to what the copilot knows, so any failure here
    (a bad stored fact, a brief read error) logs and degrades to no context
    rather than failing a turn that could still edit. The brief read runs in a
    savepoint so a database error cannot poison the outer read transaction.
    """
    context: dict = {}
    try:
        if settings.clip_facts_for(job.user_id):
            facts = clip_facts_by_media_id(job, variant, list(item.clip_assignments or []))
            if facts:
                context["facts"] = facts
    except Exception:  # noqa: BLE001 - fail open to no clip facts
        log.warning("kria_copilot_clip_facts_unavailable", thread_id=str(thread_id), exc_info=True)
    try:
        # What the vision analyzer saw in each clip (stored understanding), when any.
        seen = clip_seen_by_media_id(job, variant, list(item.clip_assignments or []))
        if seen:
            context["seen"] = seen
    except Exception:  # noqa: BLE001 - fail open to no descriptions
        log.warning("kria_copilot_clip_seen_unavailable", thread_id=str(thread_id), exc_info=True)
    try:
        if settings.creative_brief_for(thread.creator_id):
            async with db.begin_nested():
                brief = await load_latest_brief(db, thread_id)
            if brief is not None and brief.live():
                context["brief"] = render_brief_request(brief)
    except Exception:  # noqa: BLE001 - fail open to no brief
        log.warning("kria_copilot_brief_unavailable", thread_id=str(thread_id), exc_info=True)
    try:
        # Mirrors the sfx capability: no query when the lane is server-disabled.
        catalog = await _sfx_catalog_rows(db) if settings.sound_effects_enabled else []
        if catalog:
            context["sfx_catalog"] = catalog
    except Exception:  # noqa: BLE001 - fail closed: empty catalog => no add_sfx
        log.warning("kria_copilot_sfx_catalog_unavailable", thread_id=str(thread_id), exc_info=True)
    return context


SFX_CATALOG_LIMIT = 40


async def _sfx_catalog_rows(db: AsyncSession) -> list[dict]:
    """Public sound-effects catalog for the copilot's add_sfx (one query).

    Same publish filter as GET /sound-effects. Runs in a savepoint so a DB error
    cannot poison the outer read transaction.
    """
    from app.models import SoundEffect  # noqa: PLC0415

    async with db.begin_nested():
        rows = (
            (
                await db.execute(
                    select(SoundEffect)
                    .where(SoundEffect.published_at.isnot(None))
                    .where(SoundEffect.archived_at.is_(None))
                    .where(SoundEffect.status == "ready")
                    .where(SoundEffect.audio_gcs_path.isnot(None))
                    .order_by(
                        SoundEffect.catalog_rank.asc().nulls_last(),
                        SoundEffect.created_at.desc(),
                    )
                    .limit(SFX_CATALOG_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "label": row.name,
            "category": row.category,
            "duration_s": row.duration_s,
        }
        for row in rows
    ]


# Variant render_status values meaning "a render is on its way" (generative_jobs.py).
_IN_FLIGHT_STATUSES = frozenset({"rendering", "pending"})
# Misses that must not fall through to a re-plan; no_current_job / no_active_session are
# legitimate (a thread without an editor session) and keep the normal path.
_GUARDED_MISSES = frozenset(
    {
        "render_in_flight",
        "session_job_mismatch",
        "no_ready_variant",
        "no_allowed_families",
        "editor_state_stale",
    }
)

# Why the last `_load_editor_target` in this task returned None (None = it did not miss).
_editor_target_miss: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kria_editor_target_miss", default=None
)


async def _load_editor_target(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item: PlanItem,
    editor_state: Any = None,
) -> _EditorTarget | None:
    """Load the editor target for the item's current render.

    Every ``None`` exit is logged as ``kria_editor_target_unavailable`` with a reason
    and recorded in ``_editor_target_miss`` so ``plan_live_turn`` can react to it.
    """
    item_id = getattr(item, "id", None)
    job_id = item.current_job_id
    _editor_target_miss.set(None)

    def _miss(reason: str, **extra: object) -> None:
        _editor_target_miss.set(reason)
        log.info(
            "kria_editor_target_unavailable",
            reason=reason,
            thread_id=str(thread_id),
            item_id=str(item_id),
            job_id=str(job_id) if job_id else None,
            **extra,
        )

    if job_id is None:
        _miss("no_current_job", session_id=None)
        return None
    thread = await db.get(CreationThread, thread_id)
    if thread is None or thread.active_creator_agent_session_id is None:
        _miss("no_active_session", session_id=None)
        return None
    session = await db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
    job = await db.get(Job, job_id)
    if session is None or job is None or session.target_job_id != job.id:
        _miss(
            "session_job_mismatch",
            session_id=str(thread.active_creator_agent_session_id),
            session_found=session is not None,
            job_found=job is not None,
            session_target_job_id=str(session.target_job_id) if session else None,
        )
        return None
    variants = [
        row for row in (job.assembly_plan or {}).get("variants") or [] if isinstance(row, dict)
    ]
    target_row = next(
        (row for row in variants if row.get("variant_id") == session.target_variant_id),
        None,
    )
    variant = target_row if target_row and target_row.get("render_status") == "ready" else None
    # The editor snapshot reads only editable lanes (never rendered artifacts), so a
    # creator's CURRENT editor state may be answered while a render is in flight; a
    # state built on an older render is caught as stale just below.
    if (
        variant is None
        and editor_state is not None
        and target_row
        and target_row.get("render_status") in _IN_FLIGHT_STATUSES
    ):
        variant = target_row
    if variant is None and target_row and target_row.get("render_status") in _IN_FLIGHT_STATUSES:
        # The creator just saved the editor (or a render is queued): the target exists
        # and will be ready again shortly. Not the same as a missing/failed variant.
        _miss(
            "render_in_flight",
            session_id=str(session.id),
            target_variant_id=session.target_variant_id,
            render_status=target_row.get("render_status"),
        )
        return None
    if variant is None:
        _miss(
            "no_ready_variant",
            session_id=str(session.id),
            target_variant_id=session.target_variant_id,
            variants_seen=[
                {"variant_id": row.get("variant_id"), "render_status": row.get("render_status")}
                for row in variants
            ],
        )
        return None
    head = (
        await db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == session.plan_item_id,
                CreatorEditDraft.variant_key == session.target_variant_id,
                CreatorEditDraft.base_job_id == job.id,
                CreatorEditDraft.is_head.is_(True),
            )
        )
    ).scalar_one_or_none()
    # ONE resolver (client state > fresh head > saved variant) shared with the draft
    # compile in tasks/kria_runtime so the snapshot and the compile base cannot diverge.
    try:
        resolved = resolve_editor_base(job, variant, head, editor_state)
    except EditorStateStaleError:
        _miss(
            "editor_state_stale",
            session_id=str(session.id),
            state_base_generation=getattr(editor_state, "base_generation", None),
            variant_baseline=variant_render_baseline(variant),
        )
        return None
    variant = resolved.projected
    if resolved.fallback_reason:
        log.warning(
            "kria_editor_state_fallback",
            reason=resolved.fallback_reason,
            source=resolved.source,
            thread_id=str(thread_id),
        )
    clip_context = await _copilot_clip_context(
        db, thread=thread, thread_id=thread_id, job=job, variant=variant, item=item
    )
    snapshot = build_editor_snapshot(job, variant, clip_context=clip_context)
    if not snapshot["allowed_op_families"]:
        _miss("no_allowed_families", session_id=str(session.id))
        return None
    rows = list(
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role.in_({"user", "assistant"}),
                    CreationThreadEvent.content.is_not(None),
                )
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    conversation = [
        {"role": row.role, "content": str(row.content)[:1000]} for row in rows if row.content
    ]
    return _EditorTarget(
        job_id=job.id, snapshot=snapshot, conversation=conversation, variant=variant
    )


_WEB_PROPOSED_REPLY = "I prepared this edit for the editor to validate and stage."
_PHONE_STAGED_REPLY = (
    "Updated your edit \u2014 it's in the editor now. Save when you're happy with it."
)


def _phone_editor_reply(reply: str) -> str:
    """The web copilot's canned "validate and stage" wording is wrong on phone:
    the draft is already live in the editor, unsaved until the creator saves."""
    text = reply.strip()
    if text == _WEB_PROPOSED_REPLY:
        return _PHONE_STAGED_REPLY
    if text.startswith(_WEB_PROPOSED_REPLY + " "):
        # Server notes (time zone, clips without a filming time) ride after the canned line.
        return f"{_PHONE_STAGED_REPLY} {text[len(_WEB_PROPOSED_REPLY) + 1 :]}"
    return reply


async def _plan_editor_revision(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item: PlanItem,
    user_message: str,
    editor_state: Any = None,
) -> KriaTurnPlan | None:
    target = await _load_editor_target(
        db, thread_id=thread_id, item=item, **_state_kw(editor_state)
    )
    if target is None:
        return None
    # Release the read transaction before Copilot model I/O. The response is
    # derived only from the immutable snapshot and copied conversation rows.
    await db.rollback()
    response = await run_copilot_turn(
        CopilotTurnBody(
            message=user_message,
            turns=target.conversation,
            snapshot=target.snapshot,
            client_contract_version=2,
        ),
        job_id=target.job_id,
    )
    if (
        response.ops
        and editor_state is not None
        and editor_state_has_lanes(editor_state)
        and any(op.get("op") == "apply_speech_cut_candidate" for op in response.ops)
    ):
        # Cutting silences renders from PERSISTED state, which would drop the creator's
        # unsaved edits: say so instead of crashing at compile time.
        return KriaTurnPlan(
            mode="respond", turn_value="recovery", response=SPEECH_CUT_NEEDS_SAVE_REPLY
        )
    if response.ops:
        return adapt_editor_action(
            reply=_phone_editor_reply(response.reply),
            ops=response.ops,
            # This portable operation invokes server speech processing; ordinary
            # text/timeline/mix edits stay drafts until an explicit Save.
            request_render=any(op.get("op") == "apply_speech_cut_candidate" for op in response.ops),
        )
    if response.outcome in {"unsupported", "no_effect"} and is_overlay_display_ask(user_message):
        # KRI-297: a display-mode change (full-screen overlays) is not an in-place
        # editor op. Decline the copilot refusal so the planner re-plans it as a
        # fresh edit (strategy.overlay_display) -- or states the real limit.
        log.info("kria_copilot_deferred_overlay_display", thread_id=str(thread_id))
        return None
    if response.outcome in {"clarification", "unsupported", "stale", "failed", "no_effect"}:
        return KriaTurnPlan(
            mode="respond",
            turn_value=("question" if response.outcome == "clarification" else "recovery"),
            response=response.reply,
        )
    return None


@dataclass(frozen=True)
class _CreatorInputs:
    agent_input: MainCreatorInput
    intent_clips: list
    creator_request: str | None


async def _load_creator_inputs(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item: PlanItem,
    persona: Persona,
    creator_id: uuid.UUID,
    user_message: str,
    manifest,  # noqa: ANN001 - resolved creator manifest
    media_context: list[dict],
    prior_brief: CreativeBrief | None,
    brief_on: bool,
) -> _CreatorInputs | PlannedKriaTurn:
    """Read everything the Main Creator needs, then release the transaction."""
    intent_clips = []
    creator_request: str | None = None
    if settings.clip_intents_enabled:
        # This deliberately has no row limit or per-message truncation. The
        # inventory agent must see every creator instruction; a request over
        # the bound is rejected below rather than silently dropping context.
        creator_rows = list(
            (
                await db.execute(
                    select(CreationThreadEvent)
                    .where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.role == "user",
                        CreationThreadEvent.content.is_not(None),
                    )
                    .order_by(CreationThreadEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
        creator_request = _full_creator_request(creator_rows, current_message=user_message)
        if creator_request is None:
            return PlannedKriaTurn(
                plan=KriaTurnPlan(
                    mode="respond",
                    turn_value="recovery",
                    response=(
                        "Your edit instructions are too long for me to match safely. "
                        "Please start a new request with the key clip directions."
                    ),
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        # Capture DB-backed clip identity before releasing the transaction for
        # the external planner/resolver calls below.
        intent_clips = await load_intent_clips_for_item(db, item, persona)
    rows = list(
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role.in_({"user", "assistant"}),
                    CreationThreadEvent.content.is_not(None),
                )
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(MAIN_CREATOR_CONVERSATION_MAX)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    creator_summary, item_summary = creator_context(persona, item)
    extra: dict = {}
    if brief_on:
        extra["brief_enabled"] = True
        if prior_brief is not None and prior_brief.live():
            # The model reads the brief, never a chip-concatenated chat string.
            extra["creator_request"] = render_brief_request(
                prior_brief, latest_message=user_message
            )
    agent_input = MainCreatorInput(
        user_message=user_message,
        creator_context=creator_summary,
        item_context=item_summary,
        media_context=media_context,
        conversation=[
            {"role": row.role, "content": str(row.content)[:1000]} for row in rows if row.content
        ],
        capability_manifest=manifest,
        **extra,
    )
    # Do not pin an async DB connection or block the event loop that renews the
    # durable turn lease while the synchronous model client is in flight.
    await db.rollback()
    return _CreatorInputs(
        agent_input=agent_input, intent_clips=intent_clips, creator_request=creator_request
    )


async def _call_main_creator(
    inputs: _CreatorInputs, *, thread_id: uuid.UUID, creator_id: uuid.UUID
) -> MainCreatorOutput:
    def _run_agent():  # noqa: ANN202 - inferred MainCreatorOutput
        return MainCreatorAgent(default_client()).run(
            inputs.agent_input,
            ctx=RunContext(
                request_id=str(thread_id),
                creator_id=str(creator_id),
            ),
        )

    try:
        return await asyncio.to_thread(_run_agent)
    except TerminalError as exc:
        raise RuntimeError("Kria could not produce a reliable editorial plan") from exc


async def _plan_from_creator_output(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
    manifest,  # noqa: ANN001 - resolved creator manifest
    inputs: _CreatorInputs,
    output: MainCreatorOutput,
    brief_request: str | None = None,
    wants_capture_order: bool = False,
) -> PlannedKriaTurn:
    """Turn a Main Creator answer into an inert plan (clip-intent resolution incl.)."""
    action = output.action
    policy_notices: tuple[str, ...] = ()
    server_song_takes: list[dict[str, Any]] | None = None
    if isinstance(action, ProposeStrategy):
        # KRI-142: the same server compile v1 runs, so a phone render never
        # silently drops what it can't draw while the reply claims it.
        checked = check_strategy_for_runtime_v2(manifest, action.strategy)
        if isinstance(checked, RefusedStrategy):
            log.info("kria_strategy_refused", thread_id=str(thread_id), code=checked.code)
            return PlannedKriaTurn(
                plan=KriaTurnPlan(mode="respond", turn_value="question", response=checked.question),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        policy_notices = checked.notices
        summary = " ".join([action.summary.strip(), *policy_notices]).strip()
        action = action.model_copy(update={"strategy": checked.strategy, "summary": summary})
        # KRI-374: user-song lip-sync -- never place an uncertain take at a guessed time.
        gate = await _song_order_gate(
            db,
            thread_id=thread_id,
            item_id=item_id,
            manifest=manifest,
            strategy=action.strategy,
        )
        if gate.plan is not None:
            return PlannedKriaTurn(
                plan=gate.plan,
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        if gate.strategy is not None:
            # The model flipped away from lip-sync mid-exchange and the gate kept it:
            # compile the lip-sync strategy through the same policy so the repairs a
            # lip-sync edit needs (no hero shape, no source-audio plan) apply.
            kept = check_strategy_for_runtime_v2(manifest, gate.strategy)
            if isinstance(kept, RefusedStrategy):
                return PlannedKriaTurn(
                    plan=KriaTurnPlan(
                        mode="respond", turn_value="question", response=kept.question
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
            extra = [n for n in kept.notices if n not in policy_notices]
            action = action.model_copy(
                update={
                    "strategy": kept.strategy,
                    "summary": " ".join([action.summary.strip(), *extra]).strip(),
                }
            )
        if gate.resolved_takes is not None:
            server_song_takes = gate.resolved_takes
            action = action.model_copy(
                update={"strategy": _with_resolved_song_takes(action.strategy, gate.resolved_takes)}
            )
    intent_clips = inputs.intent_clips
    creator_request = inputs.creator_request
    if (
        settings.clip_intents_enabled
        and isinstance(action, ProposeStrategy)
        # A Talking edit renders no clip intents, and its "captions" are speech
        # captions: the inventory would only misread "Add captions" as a chapter
        # caption and ask about it. The strategy check above already stripped
        # any footage intents with a notice.
        and action.strategy.edit_format not in CLIP_INTENT_FREE_EDIT_FORMATS
    ):
        clip_selections = None
        thread_events: list[tuple[str, dict[str, Any] | None]] = []
        if settings.kria_clip_selection_questions_enabled or settings.kria_choice_questions_enabled:
            thread_events = await _load_thread_events(db, thread_id)
        if settings.kria_clip_selection_questions_enabled:
            clip_selections = fold_clip_selections(thread_events)
        try:
            planned = await plan_and_resolve_clip_intents(
                creator_request=creator_request or user_message,
                latest_user_message=user_message,
                generated_brief=brief_request,
                candidate_intents=action.strategy.clip_intents,
                clips=intent_clips,
                run_context=RunContext(
                    request_id=str(thread_id),
                    creator_id=str(creator_id),
                ),
                # KRI-282: chat clips have no background vision lane, so give the
                # turn itself a larger (cached, converging) foreground budget.
                max_vision_requeries=settings.kria_clip_intents_max_vision_requeries,
                vision_deadline_s=settings.kria_clip_intents_vision_deadline_s,
                # KRI-282 L2: hold the turn while background clip analysis is incomplete.
                require_clip_understanding=settings.kria_clip_understanding_enabled,
                **({"clip_selections": clip_selections} if clip_selections else {}),
            )
        except Exception as exc:  # noqa: BLE001 - no provider failure may mint a draft
            log.warning(
                "kria_clip_intent_planning_failed",
                thread_id=str(thread_id),
                error_type=type(exc).__name__,
                clips=len(intent_clips),
            )
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question=None,
                    status="provider_unavailable",
                    diagnostics={
                        "stage": "planning",
                        "reason": "planner_exception",
                        "error_type": type(exc).__name__,
                        "clips": len(intent_clips),
                    },
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        if any(intent.label_source == "transcript" for intent in planned.requested_intents) and (
            manifest.narration is None
            or action.strategy.execution_contract != "guided_voiceover_v1"
        ):
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question="Those labels need a recorded voiceover with guided visuals.",
                    status="needs_creator",
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        if planned.resolution.vision_answers:
            try:
                # `rollback()` before provider I/O expires ORM instances. Reload
                # the item before the cache writer reads its id, and fail closed
                # if the target disappeared while the provider was running.
                current_item = await db.get(PlanItem, item_id, populate_existing=True)
                if current_item is None:
                    return PlannedKriaTurn(
                        plan=_clip_intent_resolution_plan(
                            question=None,
                            status="provider_unavailable",
                            diagnostics={"stage": "persist", "reason": "item_unavailable"},
                        ),
                        manifest_hash=manifest.manifest_hash,
                        context_hash=manifest.context_hash,
                    )
                await persist_clip_intent_vision_answers(
                    db,
                    current_item,
                    planned.resolution.vision_answers,
                    creator_id=creator_id,
                    strict=True,
                )
                await db.commit()
            except Exception as exc:  # noqa: BLE001 - cache failure must not mint a draft
                await db.rollback()
                log.warning(
                    "kria_clip_intent_answer_persist_failed",
                    thread_id=str(thread_id),
                    error_type=type(exc).__name__,
                )
                return PlannedKriaTurn(
                    plan=_clip_intent_resolution_plan(
                        question=None,
                        status="provider_unavailable",
                        diagnostics={
                            "stage": "persist",
                            "reason": "answer_persist_failed",
                            "error_type": type(exc).__name__,
                        },
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
        if (
            planned.resolution.status != "resolved"
            or planned.resolution.needs_creator
            or planned.resolution.deferred_queries
        ):
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question=planned.resolution.question,
                    status=(
                        "needs_creator"
                        if planned.resolution.needs_creator
                        else planned.resolution.status
                    ),
                    diagnostics=_resolution_diagnostics(planned.resolution),
                    clip_question=(
                        planned.resolution.clip_question
                        if planned.resolution.needs_creator
                        else None
                    ),
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        ordering_choice: str | None = None
        if settings.kria_choice_questions_enabled:
            ordering_choice = fold_choice_answers(thread_events).get(CONFLICT_ORDER_VS_GROUP)
            if ordering_choice not in ORDER_VS_GROUP_OPTIONS:
                ordering_choice = None
            if ordering_choice is None:
                asked = _ordering_conflict_question(
                    planned.resolution.intents,
                    intent_clips,
                    wants_capture_order=wants_capture_order,
                    creator_request=creator_request or user_message,
                )
                if asked is not None:
                    return PlannedKriaTurn(
                        plan=asked,
                        manifest_hash=manifest.manifest_hash,
                        context_hash=manifest.context_hash,
                    )
        return PlannedKriaTurn(
            plan=adapt_creator_action(
                action,
                server_clip_intents=planned.requested_intents,
                server_resolved_clip_intents=planned.resolution.intents,
                server_resolved_song_takes=server_song_takes,
                ordering_choice=ordering_choice,
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
            policy_notices=policy_notices,
        )
    if (
        isinstance(action, ProposeStrategy)
        and any(
            intent.label_source == "transcript" for intent in action.strategy.clip_intents or []
        )
        and (
            manifest.narration is None
            or action.strategy.execution_contract != "guided_voiceover_v1"
        )
    ):
        return PlannedKriaTurn(
            plan=_clip_intent_resolution_plan(
                question="Those labels need a recorded voiceover with guided visuals.",
                status="needs_creator",
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
    return PlannedKriaTurn(
        plan=adapt_creator_action(action, server_resolved_song_takes=server_song_takes),
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
        policy_notices=policy_notices,
    )


def _ordering_conflict_question(
    resolved: list[ResolvedClipIntent],
    clips: list,
    *,
    wants_capture_order: bool,
    creator_request: str,
) -> KriaTurnPlan | None:
    """The one focused question for "chronological" + "group by X" over interleaved clips
    (KRI-282), or None when there is no real conflict."""
    groups = [
        (intent.attribute, [a.media_id for a in intent.assignments])
        for intent in resolved
        if intent.op == "group" and intent.status == "resolved" and not intent.placeholder
    ]
    candidate = detect_order_vs_group(
        wants_capture_order=wants_capture_order,
        groups=groups,
        clips=[(clip.media_id, getattr(clip, "capture_time", None)) for clip in clips],
        noun="sport" if "spor" in creator_request.casefold() else "group",
    )
    if candidate is None:
        return None
    return KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response=choice_question_text(candidate),
        choice_question=build_choice_question(candidate),
    )


def _brief_wants_capture_order(brief: CreativeBrief | None) -> bool:
    """Does the live brief ask for filming (capture-time) order?"""
    if brief is None or not brief.live():
        return False
    from app.pipeline.unified_montage import brief_view  # noqa: PLC0415

    return brief_view(brief).order_by_capture


async def _refetch_item(db: AsyncSession, item_id: uuid.UUID) -> PlanItem:
    item = await db.get(PlanItem, item_id)
    if item is None:
        raise RuntimeError("Kria target item is unavailable")
    return item


# Ops whose effect is confined to the current render's text/labels/order. A turn the
# copilot serves with ONLY these can skip the pro-model requirement extraction on the
# critical path (KRI-219). Anything structural (clip removal, retime, trims) or
# ambiguous keeps the router.
_FAST_PATH_OPS = frozenset(
    {
        "edit_text",
        "rewrite_text",
        "patch_text",
        "patch_text_style",
        "patch_text_appearance",
        "remove_texts",
        "add_text",
        "set_text_timing",
        "set_texts_timing",
        "realign_labels",
        "label_each_clip",
        "reorder_clips_by",
    }
)
_FAST_PATH_MAX_CHARS = 280
# A text/label/caption ask: when the copilot answers it with a question or a refusal, that
# answer stands (a full re-plan would write labels from place/time facts and re-render).
_TEXT_EDIT_ASK = re.compile(r"\b(labels?|captions?|texts?|titles?|wording|font)\b")
# Wording that needs the planner even when the copilot could stage something.
_REPLAN_CUES = re.compile(
    r"\b(vibe|different|another version|new (edit|video|version|cut)|recut|re-?cut|re-?do|"
    r"from scratch|best \d+|top \d+|\d+ best|use (only|just)|only (the )?(best|funniest|top)|"
    r"funniest|shuffle|more clips|fewer clips|farkl\w*|yeniden|bastan|ba\u015ftan)\b"
)


def _fast_path_eligible(message: str) -> bool:
    from app.kria.brief import wants_full_replan  # noqa: PLC0415

    text = " ".join(message.casefold().split())
    return (
        0 < len(text) <= _FAST_PATH_MAX_CHARS
        and not wants_full_replan(message)
        and _REPLAN_CUES.search(text) is None
    )


def _is_fast_path_plan(plan: KriaTurnPlan | None) -> bool:
    """An act plan whose editor ops are all in-place text/label/order ops."""
    if plan is None or plan.mode != "act" or not plan.intents:
        return False
    for intent in plan.intents:
        if intent.tool_name != "draft.apply_editor_ops":
            return False
        arguments = intent.arguments
        ops = (
            arguments.get("operations")
            if isinstance(arguments, dict)
            else getattr(arguments, "operations", None)
        )
        if not ops:
            return False
        for op in ops:
            name = op.get("op") if isinstance(op, dict) else getattr(op, "op", None)
            if str(name) not in _FAST_PATH_OPS:
                return False
    return True


_EDITOR_TARGET_RECOVERY_REPLY = (
    "I couldn't open your current edit to change it in place. Try again, "
    "or ask me for a new version."
)


_EDITOR_TARGET_IN_FLIGHT_REPLY = (
    "Your edit is still rendering \u2014 give it a moment, then ask again."
)


def _state_kw(editor_state: Any) -> dict[str, Any]:
    """Pass `editor_state` only when present: the no-state call stays byte-identical."""
    return {"editor_state": editor_state} if editor_state is not None else {}


def _editor_state_stale() -> bool:
    return _editor_target_miss.get() == "editor_state_stale"


def _editor_target_miss_guarded() -> bool:
    return _editor_target_miss.get() in _GUARDED_MISSES


def _editor_target_recovery(manifest: object) -> PlannedKriaTurn:
    """An editor-eligible ask on a rendered item whose editor target could not be
    loaded must not silently become a re-plan that replaces the draft with a render."""
    miss = _editor_target_miss.get()
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="recovery",
            response=(
                _EDITOR_TARGET_IN_FLIGHT_REPLY
                if miss == "render_in_flight"
                else EDITOR_STATE_STALE_REPLY
                if miss == "editor_state_stale"
                else _EDITOR_TARGET_RECOVERY_REPLY
            ),
        ),
        manifest_hash=manifest.manifest_hash,  # type: ignore[attr-defined]
        context_hash=manifest.context_hash,  # type: ignore[attr-defined]
        defer_brief=True,
    )


async def plan_live_turn(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
    allow_fast_path: bool = True,
    first_editor_result: tuple[KriaTurnPlan | None] | None = None,
    editor_state: Any = None,
) -> PlannedKriaTurn:
    item = await db.get(PlanItem, item_id)
    if item is None:
        raise RuntimeError("Kria target item is unavailable")
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != creator_id:
        raise RuntimeError("Kria target item ownership changed")
    persona = await db.get(Persona, plan.persona_id)
    if persona is None or persona.user_id != creator_id:
        raise RuntimeError("Kria creator context is unavailable")
    manifest, media_context = await resolve_item_creator_context(db, item, persona=persona)
    brief_on = settings.creative_brief_for(creator_id)
    # KRI-188: with a render present and the brief on, the requirement router
    # decides between the editor-op tool and a re-plan, so the Main Creator
    # (which extracts the requirements) runs FIRST. Everything else keeps the
    # original order: editor copilot first, planner only when no op survives.
    extract_first = (
        brief_on
        and item.current_job_id is not None
        and manifest.capabilities["dispatch_render"].available
    )
    if (
        extract_first
        and allow_fast_path
        and settings.kria_copilot_first_enabled
        and _fast_path_eligible(user_message)
    ):
        # KRI-219 latency: the copilot (flash, ~2-4 s) answers a short in-place
        # text/label/order tweak before the pro-model extraction (~12 s) is even
        # started. Only a plan made of in-place ops is taken; anything else (a
        # question, a structural op, a refusal) falls through to the router below,
        # unchanged. The requirement extraction still runs, off the critical path.
        _editor_target_miss.set(None)
        fast_plan = await _plan_editor_revision(
            db, thread_id=thread_id, item=item, user_message=user_message, **_state_kw(editor_state)
        )
        if fast_plan is None and _editor_target_miss_guarded():
            return _editor_target_recovery(manifest)
        if _is_fast_path_plan(fast_plan):
            return PlannedKriaTurn(
                plan=fast_plan,
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
                brief_route="editor_ops",
                brief_clip_ids=tuple(str(media.media_id) for media in manifest.media),
                brief_manifest=manifest,
                defer_brief=True,
            )
        if (
            fast_plan is not None
            and fast_plan.mode == "respond"
            and _TEXT_EDIT_ASK.search(" ".join(user_message.casefold().split()))
        ):
            return PlannedKriaTurn(
                plan=fast_plan,
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
                defer_brief=True,
            )
        log.info(
            "kria_copilot_skipped_replan",
            reason="fast_path_not_taken",
            route="fast_path",
            thread_id=str(thread_id),
            item_id=str(item_id),
            copilot_mode=fast_plan.mode if fast_plan is not None else None,
        )
        # The copilot call rolled the session back, which EXPIRES every loaded row
        # (item, plan, persona): reading one from async code raises MissingGreenlet.
        # Re-enter the planner from the top so the extract-first path re-reads
        # everything it needs, exactly as if the fast path had not been tried.
        return await plan_live_turn(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=creator_id,
            user_message=user_message,
            allow_fast_path=False,
            # The copilot already answered this exact message against this exact draft:
            # the router below reuses that answer instead of paying for a second call.
            first_editor_result=(fast_plan,),
            **_state_kw(editor_state),
        )
    if not extract_first:
        has_render = item.current_job_id is not None
        _editor_target_miss.set(None)
        editor_plan = await _plan_editor_revision(
            db,
            thread_id=thread_id,
            item=item,
            user_message=user_message,
            **_state_kw(editor_state),
        )
        if (
            editor_plan is None
            and _editor_target_miss_guarded()
            and has_render
            and (_fast_path_eligible(user_message) or _editor_state_stale())
        ):
            return _editor_target_recovery(manifest)
        if editor_plan is not None:
            return PlannedKriaTurn(
                plan=editor_plan,
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
    if not manifest.capabilities["dispatch_render"].available:
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response=(
                    "Add at least one clip and I can shape the edit around what is actually there."
                ),
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
    prior_brief = await load_latest_brief(db, thread_id) if brief_on else None
    try:
        inputs = await _load_creator_inputs(
            db,
            thread_id=thread_id,
            item=item,
            persona=persona,
            creator_id=creator_id,
            user_message=user_message,
            manifest=manifest,
            media_context=media_context,
            prior_brief=prior_brief,
            brief_on=brief_on,
        )
        if isinstance(inputs, PlannedKriaTurn):
            return inputs
        output = await _call_main_creator(inputs, thread_id=thread_id, creator_id=creator_id)
    except (RuntimeError, ValidationError):
        if not extract_first:
            raise
        # KRI-188: the Main Creator now runs before the copilot only to extract
        # requirements. A failure there must not block a plain edit the copilot
        # alone could have served: fall back to the legacy order, no brief update.
        item = await _refetch_item(db, item_id)
        editor_plan = await _plan_editor_revision(
            db, thread_id=thread_id, item=item, user_message=user_message, **_state_kw(editor_state)
        )
        if editor_plan is None:
            raise
        return PlannedKriaTurn(
            plan=editor_plan,
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
    if not brief_on:
        return await _plan_from_creator_output(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=creator_id,
            user_message=user_message,
            manifest=manifest,
            inputs=inputs,
            output=output,
        )

    updates = tuple(output.brief_updates)
    effective = apply_updates(prior_brief, updates, source_turn_id=None)
    fresh = new_requirements(prior_brief, effective)
    clip_ids = tuple(str(media.media_id) for media in manifest.media)
    shape = CurrentPlanShape(has_render=False)
    # Every rollback above expires loaded rows; an expired attribute read on an
    # AsyncSession raises MissingGreenlet, so re-read the item before using it.
    item = await _refetch_item(db, item_id)
    if item.current_job_id is not None:
        _editor_target_miss.set(None)
        target = await _load_editor_target(
            db, thread_id=thread_id, item=item, **_state_kw(editor_state)
        )
        shape = plan_shape_from_editor_snapshot(target.snapshot if target else None)
        await db.rollback()
        if (
            target is None
            and _editor_target_miss_guarded()
            and (_fast_path_eligible(user_message) or _editor_state_stale())
        ):
            # The route below would be a re-plan caused solely by the missing target.
            return _editor_target_recovery(manifest)
    route = route_requirements(fresh, shape, message=user_message)
    if route == "editor_ops":
        if first_editor_result is not None:
            editor_plan = first_editor_result[0]
        else:
            item = await _refetch_item(db, item_id)
            editor_plan = await _plan_editor_revision(
                db,
                thread_id=thread_id,
                item=item,
                user_message=user_message,
                **_state_kw(editor_state),
            )
        if editor_plan is not None:
            return PlannedKriaTurn(
                plan=editor_plan,
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
                brief_updates=updates,
                brief_route=route,
                brief_clip_ids=clip_ids,
                brief_manifest=manifest,
            )
        log.info(
            "kria_copilot_skipped_replan",
            reason="editor_ops_route_without_editor_plan",
            route=route,
            thread_id=str(thread_id),
            item_id=str(item_id),
        )
    planned = await _plan_from_creator_output(
        db,
        thread_id=thread_id,
        item_id=item_id,
        creator_id=creator_id,
        user_message=user_message,
        manifest=manifest,
        inputs=inputs,
        output=output,
        brief_request=(
            render_brief_request(effective, latest_message=user_message)
            if effective.live()
            else None
        ),
        wants_capture_order=_brief_wants_capture_order(effective),
    )
    return replace(
        planned,
        brief_updates=updates,
        # A re-plan that ended up asking a question or failing safe is not an
        # editor plan; only an act plan is held to the router's verdict.
        brief_route=route,
        brief_clip_ids=clip_ids,
        brief_manifest=manifest,
    )


async def extract_deferred_brief(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
) -> tuple[tuple[BriefUpdate, ...], Route | None]:
    """Run the Main Creator requirement extraction for an already-applied editor turn.

    Same inputs and the same router as the inline path, so the brief ends up exactly
    as if the extraction had run first. Returns the newly stated requirements and the
    router verdict for them (``replan`` = the copilot's in-place edit did not cover
    everything the creator asked for).
    """
    item = await db.get(PlanItem, item_id)
    if item is None:
        return (), None
    plan = await db.get(ContentPlan, item.content_plan_id)
    persona = await db.get(Persona, plan.persona_id) if plan is not None else None
    if plan is None or plan.user_id != creator_id or persona is None:
        return (), None
    manifest, media_context = await resolve_item_creator_context(db, item, persona=persona)
    prior_brief = await load_latest_brief(db, thread_id)
    inputs = await _load_creator_inputs(
        db,
        thread_id=thread_id,
        item=item,
        persona=persona,
        creator_id=creator_id,
        user_message=user_message,
        manifest=manifest,
        media_context=media_context,
        prior_brief=prior_brief,
        brief_on=True,
    )
    if isinstance(inputs, PlannedKriaTurn):
        return (), None
    output = await _call_main_creator(inputs, thread_id=thread_id, creator_id=creator_id)
    updates = tuple(output.brief_updates)
    effective = apply_updates(prior_brief, updates, source_turn_id=None)
    fresh = new_requirements(prior_brief, effective)
    item = await _refetch_item(db, item_id)
    shape = CurrentPlanShape(has_render=False)
    if item.current_job_id is not None:
        target = await _load_editor_target(db, thread_id=thread_id, item=item)
        shape = plan_shape_from_editor_snapshot(target.snapshot if target else None)
        await db.rollback()
    return updates, route_requirements(fresh, shape, message=user_message)


__all__ = [
    "extract_deferred_brief",
    "PlannedKriaTurn",
    "adapt_creator_action",
    "adapt_editor_action",
    "plan_live_turn",
]
