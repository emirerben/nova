"""Runtime-v2 planner adapter over Nova's production creative intelligence.

The planner returns inert intents only.  It never receives storage paths and
cannot author risk, target pins, idempotency identities, or completion claims.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import json
import re
import time
import unicodedata
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents._schemas.brief_extractor import BriefExtractionOutput
from app.agents._schemas.creator_agent import (
    AskUser,
    ProposeStrategy,
    ResolvedCreatorManifest,
    ReviewDecision,
)
from app.agents._schemas.edit_format import CLIP_INTENT_FREE_EDIT_FORMATS
from app.agents.brief_extractor import BriefExtractionInput, BriefExtractorAgent
from app.agents.main_creator import (
    MAIN_CREATOR_CONVERSATION_MAX,
    CreativeCopyDecision,
    MainCreatorAgent,
    MainCreatorInput,
    MainCreatorOutput,
)
from app.config import settings
from app.kria.brief import (
    BriefCoverageError,
    BriefUpdate,
    BriefUpdateBatchError,
    CreativeBrief,
    CurrentPlanShape,
    Route,
    apply_updates,
    brief_context,
    load_latest_brief,
    new_requirements,
    plan_shape_from_editor_snapshot,
    render_brief_request,
    route_requirements,
)
from app.kria.brief_route import loose_text
from app.kria.contracts import KriaTurnPlan
from app.kria.reply_language import current_reply_language, say
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
from app.pipeline.pinned_text import pin_range_grounded
from app.routes._copilot import CopilotTurnBody, is_overlay_display_ask, run_copilot_turn
from app.routes.generative_jobs import variant_render_baseline
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S, PinnedText
from app.schemas.user_song import SONG_ALIGNMENT_VERSION
from app.services.choice_questions import (
    CONFLICT_ORDER_VS_GROUP,
    ORDER_VS_GROUP_OPTIONS,
    ChoiceCapability,
    answered_brief,
    ask_user_choice,
    build_choice_question,
    choice_question_text,
    detect_order_vs_group,
    fold_choice_answers,
    resolve_choices,
    tag_event,
)
from app.services.clip_intent_answers import persist_clip_intent_vision_answers
from app.services.clip_intent_planning import plan_and_resolve_clip_intents
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import fold_clip_selections
from app.services.clip_understanding import understanding_incomplete
from app.services.creative_copy_decisions import (
    authorship_question,
    fold_creative_copy,
    localize_question,
    media_digest,
    question_message,
    wording_question,
)
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
    takes_needing_order,
    thread_keeps_lipsync,
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
    brief_coverage: dict | None = None
    brief_expected_version: int | None = None
    media_snapshot: dict | None = None
    # KRI-219 latency: the editor copilot served this in-place edit BEFORE the slow
    # Main Creator requirement extraction; the completed turn schedules that
    # extraction off the critical path (`extract_deferred_brief`).
    defer_brief: bool = False
    # KRI-142: what the server strategy check repaired or left out, so the
    # receipts reply still says it when it replaces the model's summary.
    policy_notices: tuple[str, ...] = ()
    creative_copy_resolution: dict | None = None


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
        if settings.kria_choice_questions_enabled:
            # KRI-476: the agent's own options become tappable (and stay listed in the
            # text for clients without the card). No/one option keeps the text question.
            asked = ask_user_choice(action.question, action.reason_code, action.options)
            if asked is not None:
                return KriaTurnPlan(
                    mode="respond",
                    turn_value="question",
                    response=asked[0],
                    choice_question=asked[1],
                )
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=action.question,
        )
    if isinstance(action, ReviewDecision):
        return KriaTurnPlan(
            mode="respond",
            turn_value="review",
            response=action.summary
            or say(
                en="The current cut is ready for your review.",
                tr="Şu anki kesim incelemen için hazır.",
            ),
        )
    summary = action.summary.strip() or action.strategy.rationale.strip()
    if not summary:
        summary = say(
            en="I shaped a focused draft around the strongest available footage.",
            tr="Elindeki en güçlü çekimlerle odaklı bir taslak hazırladım.",
        )
    server_owned_intents = (
        server_clip_intents is not None or server_resolved_clip_intents is not None
    )
    unplaced = [
        intent.attribute
        for intent in server_resolved_clip_intents or []
        if intent.op == "order"
        and intent.status == "resolved"
        and not intent.assignments
        and intent.order_by is None
        and intent.attribute
    ]
    if unplaced:
        # KRI-458: the draft must not claim an order the footage cannot back.
        summary = say(
            en=(
                f"{summary} I found no clips of {', '.join(unplaced)}, "
                "so I can't place them where you asked."
            ),
            tr=(
                f"{summary} {', '.join(unplaced)} ile ilgili klip bulamadım, "
                "bu yüzden onları istediğin yere yerleştiremiyorum."
            ),
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
        # KRI-476: likewise; the gate in `plan_live_turn` is the only writer.
        "choice_answers": None,
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


async def _load_raw_creator_request(
    db: AsyncSession, *, thread_id: uuid.UUID, user_message: str
) -> str | None:
    """Every creator message in the thread, in order, or None when over the safe bound.

    No row limit or per-message truncation: the clip-intent planner verifies a quote
    against exactly these words, so a paraphrase (the brief) must never stand in for them.
    """
    rows = list(
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
    return _full_creator_request(rows, current_message=user_message)


# KRI-433: ask for a short follow-up, never the request again. Every user message
# joins the combined request (`_full_creator_request`, 12,000 chars), so a pasted
# resend duplicates every instruction. Answered checks are cached per clip (pool
# assets and iPhone clip assignments), so the next turn only does the rest.
_CLIP_INTENT_PENDING_REPLY = (
    "I'm still checking some of your clips against that request. "
    'Reply "go ahead" in a moment and I\'ll pick up where I left off. '
    "No need to send the whole request again."
)
# KRI-520: "devam" needs no matcher; any message re-runs the planner (see the prompt).
_CLIP_INTENT_PENDING_REPLY_TR = (
    "İsteğine göre bazı kliplerini hâlâ kontrol ediyorum. "
    'Birazdan "devam" yaz, kaldığım yerden sürdüreyim. '
    "Tüm isteği tekrar göndermene gerek yok."
)


def _clip_intent_pending_reply() -> str:
    return say(en=_CLIP_INTENT_PENDING_REPLY, tr=_CLIP_INTENT_PENDING_REPLY_TR)


def _labels_need_voiceover_reply() -> str:
    return say(
        en="Those labels need a recorded voiceover with guided visuals.",
        tr="Bu etiketler için kaydedilmiş bir seslendirme ve rehberli görseller gerekiyor.",
    )


def _clips_still_checking_reply(count: int) -> str:
    return say(
        en=(
            f"I'm still checking {count} of your clips. "
            "Your request and completed answers are saved. "
            "Ask me to continue once those clips are ready."
        ),
        tr=(
            f"Kliplerinden {count} tanesini hâlâ kontrol ediyorum. "
            "İsteğin ve tamamlanan yanıtlar kaydedildi. "
            "Bu klipler hazır olunca devam etmemi iste."
        ),
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
            response=question
            or say(
                en="Which clips should I use for that part?",
                tr="Bu bölüm için hangi klipleri kullanayım?",
            ),
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
            _clip_intent_pending_reply()
            if status == "pending"
            else say(
                en=(
                    "I couldn't reliably match that request to your clips. "
                    "Please try again shortly."
                ),
                tr=(
                    "Bu isteği kliplerinle güvenilir şekilde eşleştiremedim. Birazdan tekrar dene."
                ),
            )
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
_SONG_PENDING_REPLY_TR = (
    "Şarkını hâlâ analiz ediyorum ve kliplerini ona göre eşleştiriyorum. "
    "Birazdan mesajını tekrar gönder, kaldığım yerden sürdüreyim."
)


def _song_pending_plan() -> KriaTurnPlan:
    """The wait-timed-out reply. Its own wording: the clip-picker copy ("checking some
    of your clips against that request") describes something else entirely."""
    return KriaTurnPlan(
        mode="respond",
        turn_value="recovery",
        response=say(en=_SONG_PENDING_REPLY, tr=_SONG_PENDING_REPLY_TR),
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


async def _load_thread_events(
    db: AsyncSession, thread_id: uuid.UUID, *, release: bool = True
) -> list[tuple[str, Any]]:
    rows = (
        await db.execute(
            select(
                CreationThreadEvent.role,
                CreationThreadEvent.payload,
                CreationThreadEvent.event_type,
                CreationThreadEvent.content,
            )
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role.in_({"user", "assistant"}),
            )
            .order_by(CreationThreadEvent.sequence)
        )
    ).all()
    if release:
        await db.rollback()  # no connection pinned across what follows
    # KRI-476: tagged with the event type and a user message's text so a choice question
    # is closed only by a real reply, never by an async event (see `tag_event`).
    return [
        tag_event(role, payload, event_type, content) for role, payload, event_type, content in rows
    ]


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
    2. the likelihood assignment wants the creator's say on some take (tie, weak match
       or no evidence) and no folded creator answer -> `song_order_question`;
    3. answered -> `resolve_uncertain_takes` -> `resolved_takes`;
    4. every take clearly placed -> proceed untouched.
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
    realign_enqueued = False
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
        raw_alignment = getattr(item, "song_alignment", None)
        stale_version = (
            isinstance(raw_alignment, dict)
            and raw_alignment.get("version", 1) != SONG_ALIGNMENT_VERSION
        )
        raw_analysis = getattr(item, "song_analysis", None)
        item_song_analysis = raw_analysis
        analysis_status = (
            raw_analysis.get("status")
            if isinstance(raw_analysis, dict)
            else getattr(raw_analysis, "status", None)
        )
        await db.rollback()  # never hold a read snapshot across the sleep
        if alignment is not None:
            break
        if analysis_status == "ready" and stale_version and not realign_enqueued:
            # Song analysis is done but the stored alignment was written by an older
            # aligner version: nothing else will refresh it. A MISSING row is left
            # alone (the attach-time task is still running); once per gate call.
            realign_enqueued = True
            from app.tasks.user_song import enqueue_user_song_alignment  # noqa: PLC0415

            await asyncio.to_thread(enqueue_user_song_alignment, item_id)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return _SongGateResult(plan=_song_pending_plan())
        await asyncio.sleep(min(_SONG_ALIGNMENT_POLL_S, remaining))

    if kept_strategy is not None and events is not None:
        # Only keep lip-sync for an exchange about THIS song generation.
        if not thread_keeps_lipsync(events, song_generation):
            return _SongGateResult()

    durations = _song_take_durations(manifest, take_ids)
    song_duration_s, first_line_s = _song_timing(item_song_analysis)
    if events is None:
        events = await _load_thread_events(db, thread_id)
    folded = fold_song_orders(events, song_generation)
    answered = bool(folded and folded.covers(take_ids))
    if analysis_status is not None and analysis_status != "ready":
        # A failed (or unfinished) song analysis aligns nothing: every take would be
        # "no evidence" and the worker declines to sync anyway. Asking is pointless.
        return _SongGateResult(strategy=kept_strategy)
    if first_line_s is None and not any(
        alignment.takes[m].candidates_or_legacy() for m in take_ids if m in alignment.takes
    ):
        # No lyric lines to anchor on and nothing matched any take: no question
        # could place a take by the song.
        return _SongGateResult(strategy=kept_strategy)
    needing = await asyncio.to_thread(
        takes_needing_order, alignment, take_ids, durations, song_duration_s
    )
    if not needing:
        # Every take has a clear, strong position: nothing the creator could decide.
        return _SongGateResult(strategy=kept_strategy)
    if answered:
        resolved = await asyncio.to_thread(
            resolve_uncertain_takes,
            alignment,
            folded.ordered_media_ids,
            durations,
            song_duration_s,
            first_line_s,
        )
        return _SongGateResult(
            resolved_takes=resolved_song_takes_payload(resolved), strategy=kept_strategy
        )
    question = await asyncio.to_thread(
        functools.partial(
            build_song_order_question,
            alignment,
            take_ids,
            song_generation=song_generation,
            durations=durations,
            song_duration_s=song_duration_s,
        )
    )
    return _SongGateResult(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=song_order_question_text(question),
            song_order_question=question,
        )
    )


def _song_take_durations(manifest: Any, take_ids: list[str]) -> dict[str, float]:
    wanted = set(take_ids)
    out: dict[str, float] = {}
    for media in getattr(manifest, "media", None) or ():
        value = getattr(media, "duration_s", None)
        if media.media_id in wanted and isinstance(value, (int, float)) and value > 0:
            out[media.media_id] = float(value)
    return out


def _song_timing(raw_analysis: Any) -> tuple[float | None, float | None]:
    """``(song duration, first lyric line start)`` in seconds from the item's analysis."""
    if isinstance(raw_analysis, dict):
        duration = raw_analysis.get("duration_s")
        lines = raw_analysis.get("lines") or []
    else:
        duration = getattr(raw_analysis, "duration_s", None)
        lines = getattr(raw_analysis, "lines", None) or []
    first = None
    for line in lines:
        start = line.get("start_s") if isinstance(line, dict) else getattr(line, "start_s", None)
        if isinstance(start, (int, float)) and (first is None or start < first):
            first = float(start)
    ok = isinstance(duration, (int, float)) and duration > 0
    return (float(duration) if ok else None), first


def _with_resolved_song_takes(strategy: Any, takes: list[dict[str, Any]]) -> Any:
    """Write the server-owned `resolved_song_takes`:
    `[{media_id, order_index, delta_s: float | None, place: "pinned"|"stack"|"broll",
    position_basis, confirmed_by_creator, ...}]` (place broll => muted B-roll)."""
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
            response=say(
                en=(
                    "That is more changes than I can apply in one go, so I left the video "
                    "as it was. Ask for it in smaller steps, or for all of one kind of "
                    "change at once (for example every font)."
                ),
                tr=(
                    "Bu, tek seferde uygulayabileceğimden fazla değişiklik; o yüzden "
                    "videoyu olduğu gibi bıraktım. Daha küçük adımlarla iste ya da aynı "
                    "türden tüm değişiklikleri tek seferde iste (örneğin tüm yazı tipleri)."
                ),
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
    except Exception:  # noqa: BLE001 - legacy readers retain their behavior
        if settings.brief_binding_for(thread.creator_id):
            raise BriefCoverageError("saved request unavailable to editor") from None
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

# Job statuses meaning the render pipeline ended without producing anything watchable.
_FAILED_JOB_STATUSES = frozenset(
    {"processing_failed", "variants_failed", "matching_failed", "no_labeled_tracks"}
)


def _job_never_rendered(job: Any) -> bool:
    """The item's current job FAILED and never produced (or started) a variant.

    There is nothing to edit in place and nothing in flight, so a follow-up is a
    re-plan of the brief, not an "editor target unavailable" dead end. A job with any
    ready/in-flight variant (or one that is still queued/processing) is NOT this case.
    """
    if getattr(job, "status", None) not in _FAILED_JOB_STATUSES:
        return False
    variants = (getattr(job, "assembly_plan", None) or {}).get("variants") or []
    return not any(
        isinstance(row, dict)
        and (row.get("render_status") == "ready" or row.get("render_status") in _IN_FLIGHT_STATUSES)
        for row in variants
    )


# Why the last `_load_editor_target` in this task returned None (None = it did not miss).
_editor_target_miss: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kria_editor_target_miss", default=None
)

# `time.monotonic()` by which the running turn must have planned (set by the turn task
# from its soft time limit). None = no deadline known: only the settings cap applies.
turn_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "kria_turn_deadline", default=None
)
# What a turn keeps after the clip-understanding wait, on top of the resolver's vision
# deadline: membership calls, the answer-cache write, and the draft commit.
_POST_UNDERSTANDING_RESERVE_S = 20.0


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
    if variant is None and _job_never_rendered(job):
        # Not in _GUARDED_MISSES: nothing exists to edit, so the follow-up re-plans.
        _miss("no_render", session_id=str(session.id), job_status=job.status)
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


# The editor can refuse an edit the draft has no lane for (a spoken-excerpt montage
# only has text/title), and a re-sent request adds nothing new for the planner to
# act on. A bare refusal is a dead end, so it carries the way out: "redo" is a
# `wants_full_replan` phrase, which routes the next turn to a full re-plan.
_REDO_OFFER = (
    ' If you want a fresh version built from your whole request, reply "redo" and I\'ll '
    "render it again."
)
# KRI-520: "yeniden yap" is a `wants_full_replan` phrase (brief.py `_REDO_PATTERNS`).
_REDO_OFFER_TR = (
    ' Tüm isteğinden yeni bir sürüm istersen "yeniden yap" yaz, videoyu tekrar oluşturayım.'
)
_WEB_PROPOSED_REPLY = "I prepared this edit for the editor to validate and stage."
# The Turkish canned line `routes/_copilot._honest_outcome` writes for a Turkish turn;
# `tests/kria/test_kri520_planner_copy.py` pins that the two stay in step.
_WEB_PROPOSED_REPLY_TR = "Bu düzenlemeyi hazırladım, editör kontrol edip uygulayacak."
_PHONE_STAGED_REPLY = (
    "Updated your edit \u2014 it's in the editor now. Save when you're happy with it."
)
_PHONE_STAGED_REPLY_TR = "Düzenlemeni güncelledim, şu an editörde. Memnun kaldığında kaydet."


def _phone_editor_reply(reply: str) -> str:
    """The web copilot's canned "validate and stage" wording is wrong on phone:
    the draft is already live in the editor, unsaved until the creator saves."""
    text = reply.strip()
    staged = say(en=_PHONE_STAGED_REPLY, tr=_PHONE_STAGED_REPLY_TR)
    for canned in (_WEB_PROPOSED_REPLY, _WEB_PROPOSED_REPLY_TR):
        if text == canned:
            return staged
        if text.startswith(canned + " "):
            # Server notes (time zone, clips without a filming time) ride after the canned line.
            return f"{staged} {text[len(canned) + 1 :]}"
    return reply


async def _plan_editor_revision(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item: PlanItem,
    user_message: str,
    editor_state: Any = None,
    original_request: str | None = None,
) -> KriaTurnPlan | None:
    try:
        target = await _load_editor_target(
            db, thread_id=thread_id, item=item, **_state_kw(editor_state)
        )
    except BriefCoverageError:
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=say(
                en=(
                    "Your saved request is too large or unavailable for this editor step. "
                    "Your draft is unchanged. Which clip or part should I work on first?"
                ),
                tr=(
                    "Kayıtlı isteğin bu düzenleme adımı için çok büyük ya da şu an "
                    "ulaşılamıyor. Taslağın değişmedi. Önce hangi klip ya da bölüm "
                    "üzerinde çalışayım?"
                ),
            ),
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
            original_request=original_request,
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
            mode="respond",
            turn_value="recovery",
            response=localized_editor_state_reply(SPEECH_CUT_NEEDS_SAVE_REPLY),
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
        reply = response.reply
        if (
            response.outcome == "unsupported"
            and _REDO_OFFER not in reply
            and _REDO_OFFER_TR not in reply
        ):
            reply = f"{reply}{say(en=_REDO_OFFER, tr=_REDO_OFFER_TR)}"
        return KriaTurnPlan(
            mode="respond",
            turn_value=("question" if response.outcome == "clarification" else "recovery"),
            response=reply,
        )
    return None


@dataclass(frozen=True)
class _CreatorInputs:
    agent_input: MainCreatorInput
    intent_clips: list
    creator_request: str | None
    brief_batches: tuple = ()
    creative_copy_state: dict | None = None
    creative_copy_digest: str = ""
    # KRI-522: the creator's own messages, in order. Brief-on turns plan clip
    # intents from this (the brief is a paraphrase and loses "start with the blue
    # video" / "placeholder"). None when over the safe bound: the brief is used.
    raw_creator_request: str | None = None


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
    if settings.clip_intents_enabled and not brief_on:
        # This deliberately has no row limit or per-message truncation. The
        # inventory agent must see every creator instruction; a request over
        # the bound is rejected below rather than silently dropping context.
        creator_request = await _load_raw_creator_request(
            db, thread_id=thread_id, user_message=user_message
        )
        if creator_request is None:
            return PlannedKriaTurn(
                plan=KriaTurnPlan(
                    mode="respond",
                    turn_value="recovery",
                    response=say(
                        en=(
                            "Your edit instructions are too long for me to match safely. "
                            "Please start a new request with the key clip directions."
                        ),
                        tr=(
                            "Düzenleme talimatların, kliplerle güvenle eşleştirmem için çok "
                            "uzun. Lütfen önemli klip yönlendirmelerini içeren yeni bir istek "
                            "başlat."
                        ),
                    ),
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        # Capture DB-backed clip identity before releasing the transaction for
        # the external planner/resolver calls below.
        intent_clips = await load_intent_clips_for_item(db, item, persona)
    raw_creator_request: str | None = None
    if settings.clip_intents_enabled and brief_on:
        intent_clips = await load_intent_clips_for_item(db, item, persona)
        # Over the bound is not an error here: the brief still carries the ask.
        raw_creator_request = await _load_raw_creator_request(
            db, thread_id=thread_id, user_message=user_message
        )
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
    batches = ()
    if brief_on:
        extra["brief_enabled"] = True
        context = brief_context(prior_brief, latest_message=user_message, max_batches=3)
        batches = context.batches
        if batches:
            extra["creator_request"] = batches[0].text
    # Full event history is folded into a tiny state summary: a decision cannot
    # disappear merely because the model only receives its newest 40 turns.
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415

    creative_copy_digest = media_digest(snapshot_media(item))
    decision_states = fold_creative_copy(
        await _load_thread_events(db, thread_id, release=False),
        dependency_digest=creative_copy_digest,
    )
    compact_copy_state = [
        {
            "target": target,
            "status": state.status,
            "candidate": state.candidate,
            "dependency_digest": state.dependency_digest,
        }
        for target, state in decision_states.items()
    ]
    agent_input = MainCreatorInput(
        user_message=user_message,
        creator_context=creator_summary,
        item_context=item_summary,
        media_context=media_context,
        conversation=[
            {"role": row.role, "content": str(row.content)[:1000]} for row in rows if row.content
        ],
        capability_manifest=manifest,
        creative_copy_state=compact_copy_state,
        # KRI-520: bound per Kria turn by the task; None leaves the prompt unchanged.
        reply_language=current_reply_language(),
        **extra,
    )
    # Do not pin an async DB connection or block the event loop that renews the
    # durable turn lease while the synchronous model client is in flight.
    await db.rollback()
    return _CreatorInputs(
        agent_input=agent_input,
        intent_clips=intent_clips,
        creator_request=creator_request,
        brief_batches=batches,
        creative_copy_state=decision_states,
        creative_copy_digest=creative_copy_digest,
        raw_creator_request=raw_creator_request,
    )


async def _call_main_creator(
    inputs: _CreatorInputs,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    item_id: uuid.UUID | None = None,
    creator_agent_session_id: uuid.UUID | None = None,
) -> MainCreatorOutput:
    from app.services.thought_summaries import current_thought_publisher  # noqa: PLC0415

    def _run_agent():  # noqa: ANN202 - inferred MainCreatorOutput
        return MainCreatorAgent(default_client()).run(
            inputs.agent_input,
            ctx=RunContext(
                request_id=str(thread_id),
                creator_id=str(creator_id),
                plan_item_id=str(item_id) if item_id else None,
                creator_agent_session_id=(
                    str(creator_agent_session_id) if creator_agent_session_id else None
                ),
                thought_summary_callback=current_thought_publisher(),
            ),
        )

    try:
        return await asyncio.to_thread(_run_agent)
    except TerminalError as exc:
        raise RuntimeError("Kria could not produce a reliable editorial plan") from exc


async def _call_brief_extractor(
    inputs: _CreatorInputs,
    *,
    creator_request: str,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_agent_session_id: uuid.UUID | None,
    prior_brief: CreativeBrief | None = None,
) -> BriefExtractionOutput:
    """Extract only brief updates; rendered edits must never invoke Main Creator here."""

    agent_input = BriefExtractionInput(
        creator_request=creator_request,
        user_message=inputs.agent_input.user_message,
        conversation=inputs.agent_input.conversation,
        current_brief=prior_brief,
        require_request_scope=True,
    )

    def _run_agent():  # noqa: ANN202 - inferred BriefExtractionOutput
        return BriefExtractorAgent(default_client()).run(
            agent_input,
            ctx=RunContext(
                request_id=str(thread_id),
                creator_id=str(creator_id),
                plan_item_id=str(item_id),
                creator_agent_session_id=(
                    str(creator_agent_session_id) if creator_agent_session_id else None
                ),
            ),
        )

    try:
        return await asyncio.to_thread(_run_agent)
    except TerminalError as exc:
        raise RuntimeError("Kria could not extract the creative brief reliably") from exc


def _extraction_request_scope(
    outputs: Sequence[BriefExtractionOutput],
) -> tuple[str, str | None]:
    """Resolve one semantic routing verdict across bounded brief batches.

    The live extractor always supplies a scope.  The ``edit`` fallback preserves
    old injected fixtures/direct callers whose output predates this contract; it
    is unreachable through ``_call_brief_extractor`` because that call requires
    the field at schema validation time.
    """
    scoped = [output for output in outputs if getattr(output, "request_scope", None) is not None]
    if not scoped:
        return "edit", None
    scopes = {getattr(output, "request_scope") for output in scoped}
    if len(scopes) != 1:
        raise BriefUpdateBatchError("brief extractor batches disagree on request scope")
    scope = scopes.pop()
    if scope == "clarify":
        clarifications = {
            clarification.strip()
            for output in scoped
            if (clarification := getattr(output, "clarification", None))
        }
        if len(clarifications) != 1:
            raise BriefUpdateBatchError("brief extractor clarification is ambiguous")
        return scope, clarifications.pop()
    return scope, None


def _clip_understanding_wait_until() -> float:
    """``time.monotonic()`` by which the clip-analysis wait ends so the turn still finishes.

    Absolute, so time the clip-intent planner call spends before the wait shortens the
    wait instead of the resolver's share of the turn.
    """
    until = time.monotonic() + settings.kria_clip_understanding_wait_s
    deadline = turn_deadline.get()
    if deadline is not None:
        reserve = settings.kria_clip_intents_vision_deadline_s + _POST_UNDERSTANDING_RESERVE_S
        until = min(until, deadline - reserve)
    return until


async def _reload_intent_clips(
    db: AsyncSession, *, item_id: uuid.UUID, creator_id: uuid.UUID
) -> list[IntentClip]:
    """Fresh clip analysis for the understanding wait; never holds a connection across a poll."""
    try:
        item = await db.get(PlanItem, item_id, populate_existing=True)
        plan = (
            await db.get(ContentPlan, item.content_plan_id, populate_existing=True)
            if item is not None
            else None
        )
        persona = (
            await db.get(Persona, plan.persona_id, populate_existing=True)
            if plan is not None
            else None
        )
        if item is None or persona is None or persona.user_id != creator_id:
            return []
        return await load_intent_clips_for_item(db, item, persona)
    finally:
        await db.rollback()


async def _kick_clip_understanding(item_id: uuid.UUID, clips: list[IntentClip]) -> None:
    """Re-enqueue background analysis for clips that still have none. Never raises.

    Attach enqueues it once per burst; a run killed mid-analysis (deploy, OOM) left its
    clips blank with nothing to restart it, so every turn would answer "still checking".
    The task is single-flight per item, so a live run makes this a no-op.
    """
    try:
        if not settings.kria_clip_understanding_enabled or not any(
            understanding_incomplete(clip.analysis, kind=clip.kind) for clip in clips
        ):
            return
        from app.tasks.kria_clip_understanding import enqueue_clip_understanding  # noqa: PLC0415

        await asyncio.to_thread(enqueue_clip_understanding, item_id)
    except Exception:  # noqa: BLE001 - a best-effort kick must never fail the turn
        log.warning("kria_clip_understanding_kick_failed", item_id=str(item_id), exc_info=True)


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
    decision = getattr(output, "creative_decision", None)
    # Only the current creator message can authorize a replacement. Brief prose and
    # earlier assistant suggestions are not creator-authored evidence.
    resolution = None
    if decision is not None and decision.status in {"creator_supplied", "cancelled"}:
        evidence = decision.source_evidence or ""
        text = decision.proposed_text or ""
        if (
            evidence
            and evidence in user_message
            and (decision.status == "cancelled" or (text and text in evidence))
        ):
            resolution = {
                "target": decision.target,
                "status": decision.status,
                "text": text if decision.status == "creator_supplied" else None,
                "dependency_digest": inputs.creative_copy_digest,
            }
    creative_turn = _creative_copy_turn(
        decision,
        inputs=inputs,
        creator_request=user_message,
        manifest=manifest,
        response=output.action.question if isinstance(output.action, AskUser) else None,
    )
    if creative_turn is not None:
        return creative_turn
    # Persist validated replacements alongside whichever result the normal planner
    # produces, within the same revision-fenced completion transaction.
    planned = await _plan_creator_action(
        db,
        thread_id=thread_id,
        item_id=item_id,
        creator_id=creator_id,
        user_message=user_message,
        manifest=manifest,
        inputs=inputs,
        output=output,
        brief_request=brief_request,
        wants_capture_order=wants_capture_order,
    )
    return replace(planned, creative_copy_resolution=resolution)


_PIN_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_MIN_PIN_CHARS = 2


def _pin_key(text: str) -> str:
    """NFC, straight quotes, collapsed spaces, casefolded: smart punctuation typed on a
    phone must still ground the straight-quoted copy the model returns."""
    return " ".join(unicodedata.normalize("NFC", text).translate(_PIN_QUOTES).casefold().split())


def ground_pinned_texts(
    pins: Sequence[PinnedText], *, evidence: str, user_sources: Sequence[str]
) -> tuple[list[PinnedText], int]:
    """KRI-523: keep only whole-video corner text the creator actually wrote.

    A pin is burned verbatim on every frame, so (like the opening/closing title) it must
    be the creator's own words: the exact text appears, as whole words, in one of their
    messages, or in the verbatim evidence excerpt the model quoted from one. Returns the
    kept pins and the number dropped.
    """

    sources = [_pin_key(source) for source in user_sources if source]
    quoted = _pin_key(evidence)
    quote_grounded = bool(quoted) and any(quoted in source for source in sources)

    def appears(key: str, haystack: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", haystack) is not None

    kept = []
    for pin in pins:
        key = _pin_key(pin.text)
        if len(key) < _MIN_PIN_CHARS:
            continue
        if any(appears(key, source) for source in sources) or (
            quote_grounded and appears(key, quoted)
        ):
            kept.append(pin)
    # KRI-525: a ranged pin also needs its seconds in the creator's own words (a clip scope has
    # no language-independent form; `_pin_range_refusal` rejects a clip that does not exist).
    pin_texts = tuple(pin.text for pin in pins)
    haystack = " ".join(source for source in user_sources if source)
    kept = [pin for pin in kept if pin_range_grounded(pin, haystack, strip=pin_texts)]
    return kept, len(pins) - len(kept)


async def _plan_creator_action(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
    manifest: Any,
    inputs: _CreatorInputs,
    output: MainCreatorOutput,
    brief_request: str | None = None,
    wants_capture_order: bool = False,
) -> PlannedKriaTurn:
    action = output.action
    if isinstance(action, ProposeStrategy):
        user_sources = [
            user_message,
            *[
                str(row.get("content") or "")
                for row in getattr(getattr(inputs, "agent_input", None), "conversation", [])
                if row.get("role") == "user"
            ],
        ]
        for target in ("opening_title", "closing_title"):
            value = getattr(action.strategy, target)
            state = (getattr(inputs, "creative_copy_state", None) or {}).get(target)
            evidence = getattr(action.render_intent_evidence, target, None) or ""
            decision = getattr(output, "creative_decision", None)
            authored = (
                value is not None
                and decision is not None
                and decision.status == "creator_supplied"
                and decision.target == target
                and decision.proposed_text == value
                and decision.source_evidence
                and decision.source_evidence in user_message
                and value in decision.source_evidence
            )
            grounded = (
                evidence
                and value
                and value in evidence
                and any(evidence in source for source in user_sources)
            )
            if value and not (state and state.approved == value) and not (authored or grounded):
                return _creative_copy_turn(
                    CreativeCopyDecision(target=target, status="candidate", proposed_text=value),
                    inputs=inputs,
                    creator_request=user_message,
                    manifest=manifest,
                )
    policy_notices: tuple[str, ...] = ()
    server_song_takes: list[dict[str, Any]] | None = None
    if isinstance(action, ProposeStrategy) and action.strategy.pinned_texts:
        grounded_pins, dropped_pins = ground_pinned_texts(
            action.strategy.pinned_texts,
            evidence=getattr(action.render_intent_evidence, "pinned_texts", None) or "",
            user_sources=user_sources,
        )
        if dropped_pins:
            log.info("kria_pinned_texts_ungrounded", thread_id=str(thread_id), count=dropped_pins)
            action = action.model_copy(
                update={
                    "strategy": action.strategy.model_copy(
                        update={"pinned_texts": grounded_pins or None}
                    ),
                    "summary": f"{action.summary.strip()} I left out on-screen text that "
                    "wasn't in your words.".strip(),
                }
            )
    if isinstance(action, ProposeStrategy):
        # KRI-142: the same server compile v1 runs, so a phone render never
        # silently drops what it can't draw while the reply claims it.
        checked = check_strategy_for_runtime_v2(
            manifest,
            action.strategy,
            **({"ask_before_simplifying": True} if settings.brief_binding_for(creator_id) else {}),
            # KRI-476: also ask before a repair drops a title hold / full-screen ask the
            # creator stated. Off with the choice-questions flag: identical to before.
            **(
                {"ask_about_stated_settings": True}
                if settings.brief_binding_for(creator_id) and settings.kria_choice_questions_enabled
                else {}
            ),
        )
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
    creator_request = brief_request or inputs.creator_request
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
        await _kick_clip_understanding(item_id, intent_clips)
        try:
            planned = await plan_and_resolve_clip_intents(
                # KRI-522: the creator's own words (verified quote source), never
                # only the brief paraphrase; the brief rides along as recall aid.
                creator_request=inputs.raw_creator_request or creator_request or user_message,
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
                # KRI-282 L2: hold the turn while background clip analysis is incomplete,
                # re-reading it (bounded) instead of trusting the pre-Main-Creator snapshot.
                require_clip_understanding=settings.kria_clip_understanding_enabled,
                refresh_clips=lambda: _reload_intent_clips(
                    db, item_id=item_id, creator_id=creator_id
                ),
                understanding_wait_until=_clip_understanding_wait_until(),
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
                    question=_labels_need_voiceover_reply(),
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
                    # KRI-433: keep iPhone clip answers so a follow-up finishes the
                    # checks. Safe here: this session holds no row lock (rolled back
                    # before provider I/O), so locking the PlanItem keeps lock order.
                    cache_clip_assignments=True,
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
                question=_labels_need_voiceover_reply(),
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


def _creative_copy_turn(
    decision: CreativeCopyDecision | None,
    *,
    inputs: _CreatorInputs,
    creator_request: str,
    manifest: Any,
    response: str | None = None,
) -> PlannedKriaTurn | None:
    """Suggestions are inert questions; only a later persisted answer grants consent."""
    if decision is None or decision.status in {"creator_supplied", "cancelled"}:
        return None
    state = (getattr(inputs, "creative_copy_state", None) or {}).get(decision.target)
    digest = inputs.creative_copy_digest
    if not digest:
        return None
    if state and (state.approved or state.cancelled) and decision.status == "unresolved":
        return None
    candidate = decision.proposed_text
    if state and state.status == "stale":
        candidate = candidate or state.candidate
    # Even unsolicited model wording must become a suggestion, never pixels. An
    # explicit delegation can therefore produce its candidate on the first turn.
    if (
        candidate
        and decision.status in {"candidate", "delegated"}
        or (state and state.status == "stale" and candidate)
    ):
        question = wording_question(
            target=decision.target,
            candidate=candidate,
            dependency_digest=digest,
        )
        candidate = question["candidate"]
        message = response or say(
            en=f"I’d try “{candidate}”. Does this wording work?",
            tr=f"Şunu deneyebilirim: “{candidate}”. Bu ifade sana uyar mı?",
        )
        if candidate not in message:
            message = f"“{candidate}” — {message}"
    else:
        question = authorship_question(target=decision.target, dependency_digest=digest)
        message = response or say(
            en="Do you have an idea, or would you like me to write one?",
            tr="Aklında bir fikir var mı, yoksa ben mi yazayım?",
        )
    # KRI-520: a Turkish chat gets Turkish option labels whatever language the wording is
    # in (English labels stay as aliases, so typed English answers still match).
    localize_question(question, "tr" if current_reply_language() == "tr" else decision.language)
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=question_message(question, message),
            choice_question=question,
        ),
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
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


def _recovery_cause(stage: str, exc: BaseException) -> dict[str, str]:
    """Which step failed and with what, for the admin turn/event views (KRI-536).

    Class names only, never ``str(exc)``: the message can quote the creator's words.
    ``cause_type`` is the wrapped error (``TerminalSchemaError`` vs a transient
    ``TerminalError``), which is what tells a bad extractor output from a provider fault.
    """
    cause: dict[str, str] = {"stage": stage, "error_type": type(exc).__name__}
    if exc.__cause__ is not None:
        cause["cause_type"] = type(exc.__cause__).__name__
    return cause


def _request_recovery(
    manifest, prior, *, updates=(), reason="context_limit", cause=None
) -> PlannedKriaTurn:
    effective = apply_updates(prior, updates, source_turn_id=None)
    ids = [req.id for req in effective.live()]
    log.info("kria_request_recovery", stage="planning", reason=reason, requirement_ids=ids)
    detail = {
        "request_extraction_failed": say(
            en="I couldn't reliably read every requested change.",
            tr="İstediğin her değişikliği güvenilir şekilde okuyamadım.",
        ),
        "planning_batches_disagree": say(
            en="The separate parts of your brief produced conflicting plans.",
            tr="İsteğinin ayrı bölümleri birbiriyle çelişen planlar çıkardı.",
        ),
        "clip_planner_context_limit": say(
            en="Your complete brief exceeds the clip planner's 12,000-character limit.",
            tr="Tüm isteğin, klip planlayıcının 12.000 karakterlik sınırını aşıyor.",
        ),
        # KRI-523: the planner itself failed (not the reading of the request), so say so.
        "creator_planning_failed": say(
            en="I couldn't turn that into a plan this time.",
            tr="Bu sefer bunu bir plana dönüştüremedim.",
        ),
        "editor_planning_failed": say(
            en="I couldn't prepare those edits this time.",
            tr="Bu sefer bu düzenlemeleri hazırlayamadım.",
        ),
    }.get(
        reason,
        say(
            en="Your complete request exceeds the context this planning step can safely read.",
            tr="Tüm isteğin, bu planlama adımının güvenle okuyabileceği sınırı aşıyor.",
        ),
    )
    follow_up = (
        say(
            en="Try again, or tell me the most important change first.",
            tr="Tekrar dene ya da önce en önemli değişikliği söyle.",
        )
        if reason in {"creator_planning_failed", "editor_planning_failed"}
        else say(
            en="Which clip or part of the edit should I work on first?",
            tr="Önce düzenlemenin hangi klibi ya da bölümü üzerinde çalışayım?",
        )
    )
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=say(
                en=(
                    f"{detail} Your complete request is saved and your draft is unchanged. "
                    f"{follow_up}"
                ),
                tr=f"{detail} Tüm isteğin kaydedildi ve taslağın değişmedi. {follow_up}",
            ),
        ),
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
        brief_updates=tuple(updates),
        brief_expected_version=prior.version if prior else 0,
        brief_coverage={
            "applicable_ids": ids,
            "retrieved_ids": [],
            "enforced_ids": [],
            "unresolved_ids": ids,
            "stage": "planning",
            "reason": reason,
            **({"cause": cause} if cause else {}),
        },
    )


async def _refetch_item(db: AsyncSession, item_id: uuid.UUID) -> PlanItem:
    item = await db.get(PlanItem, item_id)
    if item is None:
        raise RuntimeError("Kria target item is unavailable")
    return item


async def _serve_unextracted_edit(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    user_message: str,
    prior_brief: CreativeBrief | None,
    manifest: ResolvedCreatorManifest,
    editor_state: Any,
    cause: dict[str, str],
) -> PlannedKriaTurn | None:
    """A short in-place text edit whose requirement extraction failed (KRI-536).

    Abandoning "add fade-in to all texts" because the extractor's JSON was malformed left
    the creator with "I couldn't reliably read every requested change" for an ordinary ask.
    The edit copilot reads the whole ledger plus the message and its ops are compiled
    deterministically into a reversible draft, so a result made ONLY of in-place text ops
    (the KRI-219 fast-path set) is served. Request preservation (KRI-459) holds because the
    creator's full message is recorded as one requirement that no checker can judge, so it
    stays visible as "can't check automatically" instead of being dropped.

    Returns None whenever that is not the case (a re-plan cue, a long message, no editor
    target, the copilot asking a question or proposing structural ops): the caller then
    keeps the honest recovery reply and the draft is untouched.
    """
    # Only a clearly SINGLE text ask: when extraction failed the typed requirements are
    # unknown, so a compound ask ("smaller and keep the whole video") must not get half of
    # it applied (KRI-524). Anything not obviously one text edit keeps the honest recovery.
    if not (
        _fast_path_eligible(user_message)
        and _is_text_edit_ask(user_message)
        and _is_single_ask(user_message)
    ):
        return None
    try:
        update = BriefUpdate(
            operation="add", kind="style", scope="global", description=user_message
        )
    except ValidationError:
        return None
    await db.rollback()
    item = await _refetch_item(db, item_id)
    editor_plan = await _plan_editor_revision(
        db,
        thread_id=thread_id,
        item=item,
        user_message=user_message,
        original_request=(
            render_brief_request(prior_brief, latest_message=user_message) if prior_brief else None
        ),
        **_state_kw(editor_state),
    )
    if not _is_fast_path_plan(editor_plan):
        return None
    log.warning("kria_request_degraded_to_copilot", **cause)
    ids = [req.id for req in apply_updates(prior_brief, (update,), source_turn_id=None).live()]
    return PlannedKriaTurn(
        plan=editor_plan,
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
        brief_updates=(update,),
        brief_route="editor_ops",
        brief_clip_ids=tuple(str(media.media_id) for media in manifest.media),
        brief_manifest=manifest,
        brief_coverage={
            "applicable_ids": ids,
            "retrieved_ids": [],
            "enforced_ids": [],
            "unresolved_ids": ids,
            "degraded_from": "request_extraction_failed",
            "cause": cause,
        },
        brief_expected_version=prior_brief.version if prior_brief else 0,
    )


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
# KRI-520: the same ask in Turkish, matched on `loose_text` (diacritics stripped, so
# "başlığı" and "basligi" both land). Stems take any suffix: baslik/basligi/basliklar,
# yazi/yazilar/altyazi (alt yazi), metin/metni, etiket, ifade (wording). "font" takes only
# Turkish case endings, so English "fonts" keeps its old (non-)match.
# "yazın" alone is "in summer", "Metin'in" is a name, "yüz ifadesi" is a facial
# expression: none of them is a text ask.
_TEXT_EDIT_ASK_TR = re.compile(
    r"\b(?:(?:bas(?:lik|lig)|etiket)\w*|(?:alt)?yazi(?!n\b)\w*|met(?:in|ni)(?![\'’])\w*|"
    r"(?<!yuz )ifade\w*|font(?:lar|lari|u|un|a|i|ta|tan)?)\b"
)
# Wording that needs the planner even when the copilot could stage something.
_REPLAN_CUES = re.compile(
    r"\b(vibe|different|another version|new (edit|video|version|cut)|recut|re-?cut|re-?do|"
    r"from scratch|best \d+|top \d+|\d+ best|use (only|just)|only (the )?(best|funniest|top)|"
    r"funniest|shuffle|more clips|fewer clips|farkl\w*|yeniden|bastan|ba\u015ftan)\b"
)
# KRI-520: Turkish re-plan wording on `loose_text`, at the English cues' precision: a new
# version, "from scratch", a best/funniest-N selection, shuffle, more/fewer clips. A bare
# "en iyi" or "tekrar" stays out (they also appear in ordinary edits).
_REPLAN_CUES_TR = re.compile(
    r"\b(?:farkl|yeniden|bastan|sifirdan|karistir|en iyi \d|\d+ en iyi|"
    r"(?:sadece|yalnizca) en (?:iyi|komik)|en komik|"
    r"daha (?:fazla|cok|az) klip|"
    r"yeni (?:bir )?(?:video|versiyon|surum|duzenleme|kesim)|"
    r"baska (?:bir )?(?:versiyon|surum|video|duzenleme|kesim))\w*"
)


def _is_text_edit_ask(message: str) -> bool:
    """A text/label/caption/title ask, in English or Turkish."""
    return bool(
        _TEXT_EDIT_ASK.search(" ".join(message.casefold().split()))
        or _TEXT_EDIT_ASK_TR.search(loose_text(message))
    )


# Joiners that make a message more than one ask (English and Turkish), plus list punctuation.
_COMPOUND_CUES = re.compile(
    r"[;,&+]|\b(?:and|also|then|plus|as well|too|after that|ve|ayr[iı]ca|sonra|bir de)\b"
)


def _is_single_ask(message: str) -> bool:
    """One sentence with no joiner: the only shape safe to serve without typed requirements."""
    text = " ".join(message.casefold().split())
    if not text or "\n" in message.strip():
        return False
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip(" .!?")]
    return len(sentences) == 1 and _COMPOUND_CUES.search(text) is None


def _fast_path_eligible(message: str) -> bool:
    from app.kria.brief import wants_full_replan  # noqa: PLC0415

    text = " ".join(message.casefold().split())
    return (
        0 < len(text) <= _FAST_PATH_MAX_CHARS
        and not wants_full_replan(message)
        and _REPLAN_CUES.search(text) is None
        and _REPLAN_CUES_TR.search(loose_text(message)) is None
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
_EDITOR_TARGET_RECOVERY_REPLY_TR = (
    "Şu anki düzenlemeni açıp yerinde değiştiremedim. Tekrar dene ya da benden yeni bir sürüm iste."
)


_EDITOR_TARGET_IN_FLIGHT_REPLY = (
    "Your edit is still rendering \u2014 give it a moment, then ask again."
)
_EDITOR_TARGET_IN_FLIGHT_REPLY_TR = (
    "Düzenlemen hâlâ hazırlanıyor, biraz bekle ve sonra tekrar iste."
)


def _state_kw(editor_state: Any) -> dict[str, Any]:
    """Pass `editor_state` only when present: the no-state call stays byte-identical."""
    return {"editor_state": editor_state} if editor_state is not None else {}


def _editor_state_stale() -> bool:
    return _editor_target_miss.get() == "editor_state_stale"


_EDITOR_STATE_STALE_REPLY_TR = "Videon değişti. Editörü yeniden açıp tekrar dene."
_SPEECH_CUT_NEEDS_SAVE_REPLY_TR = "Önce düzenlemelerini kaydet, sonra sessiz kısımları kesebilirim."


def _editor_state_stale_reply() -> str:
    return say(en=EDITOR_STATE_STALE_REPLY, tr=_EDITOR_STATE_STALE_REPLY_TR)


def localized_editor_state_reply(reply: str) -> str:
    """The chat-language wording of an ``EditorStateReplyError.reply`` (KRI-520).

    The two replies are module constants in ``kria_editor_ops`` (English, shared with the
    compile-time refusal in ``tasks/kria_runtime``); any other text passes through.
    """
    if reply == EDITOR_STATE_STALE_REPLY:
        return _editor_state_stale_reply()
    if reply == SPEECH_CUT_NEEDS_SAVE_REPLY:
        return say(en=SPEECH_CUT_NEEDS_SAVE_REPLY, tr=_SPEECH_CUT_NEEDS_SAVE_REPLY_TR)
    return reply


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
                say(en=_EDITOR_TARGET_IN_FLIGHT_REPLY, tr=_EDITOR_TARGET_IN_FLIGHT_REPLY_TR)
                if miss == "render_in_flight"
                else _editor_state_stale_reply()
                if miss == "editor_state_stale"
                else say(en=_EDITOR_TARGET_RECOVERY_REPLY, tr=_EDITOR_TARGET_RECOVERY_REPLY_TR)
            ),
        ),
        manifest_hash=manifest.manifest_hash,  # type: ignore[attr-defined]
        context_hash=manifest.context_hash,  # type: ignore[attr-defined]
        defer_brief=True,
    )


async def plan_live_turn(db: AsyncSession, **kwargs) -> PlannedKriaTurn:
    """Fence planning against source replacement while providers are running."""
    from app.kria.brief_binding import media_identity, snapshot_media  # noqa: PLC0415

    item = await db.get(PlanItem, kwargs["item_id"])
    before = snapshot_media(item) if item is not None else {}
    planned = await _plan_live_turn(db, **kwargs)
    if planned.plan.mode != "act":
        return planned
    current = await db.get(PlanItem, kwargs["item_id"], populate_existing=True)
    after = snapshot_media(current) if current is not None else {}
    if media_identity(before) != media_identity(after):
        return replace(
            planned,
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=say(
                    en=(
                        "Your clips changed while I was planning. Your draft is unchanged; "
                        "please ask again using the current clips."
                    ),
                    tr=(
                        "Ben planlarken kliplerin değişti. Taslağın değişmedi; lütfen "
                        "güncel kliplerle tekrar iste."
                    ),
                ),
            ),
        )
    planned = replace(planned, media_snapshot=after)
    return await _gate_unresolved_choices(
        db, planned, thread_id=kwargs["thread_id"], creator_id=kwargs["creator_id"]
    )


async def _gate_unresolved_choices(
    db: AsyncSession,
    planned: PlannedKriaTurn,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
) -> PlannedKriaTurn:
    """KRI-476: an unresolved material choice never becomes an approvable draft.

    Runs HERE, after ``media_snapshot`` is attached, because the order/duration
    conflicts depend on the exact snapshot approval later binds (a dry run inside
    ``_plan_from_creator_output`` would invent "missing capture dates"). One question
    per turn; earlier answers are folded into server-owned strategy fields (digest
    scoped) so a retry or a re-sent prompt never re-asks an answered question.
    """

    plan = planned.plan
    if plan.mode != "act" or not plan.intents:
        return planned
    apply_intent = plan.intents[0]
    arguments = apply_intent.arguments
    strategy = arguments.get("strategy") if isinstance(arguments, dict) else None
    if apply_intent.tool_name != "draft.apply_strategy" or not isinstance(strategy, dict):
        return planned
    # KRI-506: a generated wording may enter the executable strategy only through
    # this event-folded server path.  The question id, offered option, exact candidate
    # and current owned-media digest are all checked by ``fold_creative_copy``; a model
    # cannot manufacture an approval by emitting a field in its strategy.
    creative = fold_creative_copy(
        [
            *await _load_thread_events(db, thread_id),
            ("assistant", {"creative_copy_resolution": planned.creative_copy_resolution}),
        ],
        dependency_digest=media_digest(planned.media_snapshot),
    )
    for state in creative.values():
        if not state.approved and not state.cancelled:
            question = (
                wording_question(
                    target=state.target,
                    candidate=state.candidate,
                    dependency_digest=media_digest(planned.media_snapshot),
                )
                if state.candidate
                else authorship_question(
                    target=state.target, dependency_digest=media_digest(planned.media_snapshot)
                )
            )
            if current_reply_language() == "tr":
                # KRI-520: English option labels stay as aliases.
                localize_question(question, "tr")
            message = (
                say(
                    en=f"“{state.candidate}” — does this wording work?",
                    tr=f"“{state.candidate}” — bu ifade sana uyar mı?",
                )
                if state.candidate
                else say(
                    en=(
                        "Do you have an idea, or would you like me to write one? "
                        "You can also skip this text."
                    ),
                    tr=(
                        "Aklında bir fikir var mı, yoksa ben mi yazayım? "
                        "İstersen bu yazıyı atlayabilirsin."
                    ),
                )
            )
            return replace(
                planned,
                plan=KriaTurnPlan(
                    mode="respond",
                    turn_value="question",
                    response=question_message(question, message),
                    choice_question=question,
                ),
            )
    approved_copy = {
        target: None if state.cancelled else state.approved
        for target, state in creative.items()
        if state.approved is not None or state.cancelled
    }
    omitted = [target for target, state in creative.items() if state.cancelled]
    if omitted:
        strategy = {**strategy, "omitted_copy_targets": omitted}
    elif "omitted_copy_targets" in strategy:
        strategy = {key: value for key, value in strategy.items() if key != "omitted_copy_targets"}
    if approved_copy or strategy != arguments.get("strategy"):
        strategy = {**strategy, **approved_copy}
        apply_intent = apply_intent.model_copy(
            update={"arguments": {**arguments, "strategy": strategy}}
        )
        plan = plan.model_copy(update={"intents": [apply_intent, *plan.intents[1:]]})
        arguments = apply_intent.arguments
    planned = replace(planned, plan=plan)
    if not settings.kria_choice_questions_enabled:
        return planned
    brief: CreativeBrief | None = None
    # Only a creator with a brief BINDING has a dispatch contract that reads the brief, so
    # only they can be asked a brief-derived question that anything would later refuse
    # (the completion backstop applies the same guard).
    if settings.creative_brief_for(creator_id) and settings.brief_binding_for(creator_id):
        brief = await load_latest_brief(db, thread_id)
        if planned.brief_updates:
            with suppress(BriefUpdateBatchError):
                brief = apply_updates(brief, planned.brief_updates, source_turn_id=None)
    events = await _load_thread_events(db, thread_id)
    resolution = resolve_choices(
        strategy,
        answered_brief(brief, strategy),
        planned.media_snapshot,
        events,
        ChoiceCapability(
            max_duration_s=float(MAX_PROPOSAL_DURATION_S),
            creator_id=creator_id,
            voice_route=settings.voice_behind_footage_enabled,
        ),
    )
    if resolution.question is not None:
        if resolution.question.kind == "title_text":
            title_question = authorship_question(
                target="opening_title",
                dependency_digest=media_digest(planned.media_snapshot),
            )
            if current_reply_language() == "tr":
                localize_question(title_question, "tr")
            return replace(
                planned,
                plan=KriaTurnPlan(
                    mode="respond",
                    turn_value="question",
                    response=say(
                        en="Do you have an idea, or would you like me to write one?",
                        tr="Aklında bir fikir var mı, yoksa ben mi yazayım?",
                    ),
                    choice_question=title_question,
                ),
            )
        candidate = resolution.question.candidate()
        asked = KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=choice_question_text(candidate),
            choice_question=build_choice_question(candidate),
        )
        return replace(planned, plan=asked)
    if not resolution.answers:
        return planned
    summary = " ".join([str(arguments.get("summary") or ""), *resolution.notices]).strip()
    rewritten = apply_intent.model_copy(
        update={
            "arguments": {**arguments, "strategy": resolution.strategy, "summary": summary[:1000]}
        }
    )
    return replace(
        planned, plan=plan.model_copy(update={"intents": [rewritten, *plan.intents[1:]]})
    )


async def _plan_live_turn(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
    allow_fast_path: bool = True,  # Compatibility only; brief-enabled turns always extract first.
    first_editor_result: tuple[KriaTurnPlan | None] | None = None,
    editor_state: Any = None,
    answers_clip_question: bool = False,
) -> PlannedKriaTurn:
    # KRI-282: `answers_clip_question` marks a turn that ANSWERS a clip-picker
    # question: structured `clip_selection` plus a synthetic message ("Dodgeball:
    # clip 21"). That message must never reach the editor copilot, which reads it
    # as a text edit and prints the label on whatever montage bar the number hits.
    # Only the re-plan folds the selection, so it forces the re-plan.
    item = await db.get(PlanItem, item_id)
    if item is None:
        raise RuntimeError("Kria target item is unavailable")
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != creator_id:
        raise RuntimeError("Kria target item ownership changed")
    persona = await db.get(Persona, plan.persona_id)
    if persona is None or persona.user_id != creator_id:
        raise RuntimeError("Kria creator context is unavailable")
    pending_analysis_ids: list[str] = []
    if settings.brief_binding_for(creator_id) and settings.kria_clip_understanding_enabled:
        from app.services.clip_intent_planning import wait_for_clip_understanding  # noqa: PLC0415

        clips = await load_intent_clips_for_item(db, item, persona)
        if any(understanding_incomplete(clip.analysis, kind=clip.kind) for clip in clips):
            await _kick_clip_understanding(item_id, clips)
            clips, _ = await wait_for_clip_understanding(
                clips,
                refresh=lambda: _reload_intent_clips(db, item_id=item_id, creator_id=creator_id),
                until=_clip_understanding_wait_until(),
            )
            missing = [
                clip.media_id
                for clip in clips
                if understanding_incomplete(clip.analysis, kind=clip.kind)
            ]
            if missing:
                # Extract and persist this turn's requirements before pausing.
                # Otherwise a later "continue" would replace a long original
                # request that never reached the durable brief.
                pending_analysis_ids = missing
                log.info("kria_required_analysis_pending", stage="main_creator", media_ids=missing)
            item = await db.get(PlanItem, item_id, populate_existing=True)
            plan = await db.get(ContentPlan, item.content_plan_id, populate_existing=True)
            persona = await db.get(Persona, plan.persona_id, populate_existing=True)
    manifest, media_context = await resolve_item_creator_context(db, item, persona=persona)
    # A pending creative-copy discussion must reach Main Creator, never the editor
    # shortcut (which could otherwise stage/render unrelated text before wording is
    # settled). The durable fold reads all events, not the model's bounded context.
    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415

    creative_copy_states = fold_creative_copy(
        await _load_thread_events(db, thread_id, release=False),
        dependency_digest=media_digest(snapshot_media(item)),
    )
    # A copy-bearing edit stays on the planner path, including revisions: editor
    # text rewrites cannot silently replace separately approved words.
    creative_copy_pending = bool(creative_copy_states)
    brief_on = settings.creative_brief_for(creator_id)
    binding_on = settings.brief_binding_for(creator_id)
    # KRI-188: with a render present and the brief on, the requirement router
    # decides between the editor-op tool and a re-plan, so the Main Creator
    # (which extracts the requirements) runs FIRST. Everything else keeps the
    # original order: editor copilot first, planner only when no op survives.
    # A FAILED latest job has no ready variant to follow up on: the turn is a fresh plan
    # (the editor target is unavailable and the follow-up route has nothing to route to).
    latest_job_failed = False
    if item.current_job_id is not None:
        latest_job = await db.get(Job, item.current_job_id)
        latest_job_failed = str(getattr(latest_job, "status", "") or "").endswith("failed")
    extract_first = (
        brief_on
        and item.current_job_id is not None
        and not latest_job_failed
        and not creative_copy_pending
        and manifest.capabilities["dispatch_render"].available
    )
    # A brief-enabled edit must route the complete typed request before choosing
    # operations. Accepting a speculative subset here used to bypass that contract.
    if (
        not extract_first
        and not answers_clip_question
        and not (binding_on and brief_on)
        and not creative_copy_pending
    ):
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
                response=say(
                    en=(
                        "Add at least one clip and I can shape the edit around what is "
                        "actually there."
                    ),
                    tr=(
                        "En az bir klip ekle, düzenlemeyi gerçekten elindekilere göre "
                        "şekillendireyim."
                    ),
                ),
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
    prior_brief = await load_latest_brief(db, thread_id) if brief_on else None
    creator_agent_session_id = None
    planning_brief = prior_brief
    extraction_complete = False
    pre_extracted_updates: tuple[BriefUpdate, ...] = ()
    pre_extracted_retrieved_ids: list[str] = []
    semantic_request_scope: str | None = None
    # A rendered followup only needs typed requirement extraction before routing.
    # Keep the wide Main Creator envelope for genuine replans and first drafts.
    if extract_first and not answers_clip_question:
        try:
            extraction_inputs = await _load_creator_inputs(
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
            if isinstance(extraction_inputs, PlannedKriaTurn):
                return extraction_inputs
            extraction_outputs = []
            for batch in extraction_inputs.brief_batches or (None,):
                request = (
                    extraction_inputs.agent_input.creator_request if batch is None else batch.text
                )
                extraction_outputs.append(
                    await _call_brief_extractor(
                        extraction_inputs,
                        creator_request=request,
                        thread_id=thread_id,
                        creator_id=creator_id,
                        item_id=item_id,
                        creator_agent_session_id=creator_agent_session_id,
                        prior_brief=prior_brief,
                    )
                )
                if batch is not None:
                    pre_extracted_retrieved_ids.extend(batch.requirement_ids)
            semantic_request_scope, clarification = _extraction_request_scope(extraction_outputs)
            # A retry that cannot be distinguished from a remake must not mutate
            # the ledger or fall through to an editor/default rebuild decision.
            if semantic_request_scope == "clarify":
                return PlannedKriaTurn(
                    plan=KriaTurnPlan(
                        mode="respond",
                        turn_value="question",
                        response=clarification or "What would you like me to revise?",
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
            unique_extracted = {
                json.dumps(update.model_dump(mode="json"), sort_keys=True): update
                for result in extraction_outputs
                for update in result.brief_updates
            }
            pre_extracted_updates = tuple(unique_extracted.values())
            effective = apply_updates(prior_brief, pre_extracted_updates, source_turn_id=None)
            extraction_complete = True
            fresh = new_requirements(prior_brief, effective)
            if pending_analysis_ids:
                return PlannedKriaTurn(
                    plan=KriaTurnPlan(
                        mode="respond",
                        turn_value="recovery",
                        response=_clips_still_checking_reply(len(pending_analysis_ids)),
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                    brief_updates=pre_extracted_updates,
                    brief_expected_version=prior_brief.version if prior_brief else 0,
                    brief_coverage={
                        "applicable_ids": [req.id for req in effective.live()],
                        "retrieved_ids": list(
                            dict.fromkeys(
                                [*pre_extracted_retrieved_ids, *[req.id for req in fresh]]
                            )
                        ),
                        "enforced_ids": [],
                        "unresolved_ids": [req.id for req in effective.live()],
                        "stage": "analysis",
                        "missing_media_ids": pending_analysis_ids,
                    },
                )
            # A semantic rebuild goes straight to main planning.  Do not load an
            # editor target first: a missing/stale target is irrelevant and must
            # not replace the rebuild verdict with a recovery response.
            route = "replan"
            if semantic_request_scope != "rebuild":
                item = await _refetch_item(db, item_id)
                target = await _load_editor_target(
                    db, thread_id=thread_id, item=item, **_state_kw(editor_state)
                )
                if target is None:
                    recovery = _editor_target_recovery(manifest)
                    return replace(
                        recovery,
                        brief_updates=pre_extracted_updates,
                        brief_expected_version=prior_brief.version if prior_brief else 0,
                        defer_brief=False,
                    )
                shape = plan_shape_from_editor_snapshot(target.snapshot)
                await db.rollback()
                route = route_requirements(fresh, shape, message=user_message, full_replan=False)
            if route == "editor_ops":
                editor_plan = (
                    first_editor_result[0]
                    if first_editor_result is not None
                    else await _plan_editor_revision(
                        db,
                        thread_id=thread_id,
                        item=await _refetch_item(db, item_id),
                        user_message=user_message,
                        original_request=render_brief_request(
                            effective, latest_message=user_message
                        ),
                        **_state_kw(editor_state),
                    )
                )
                if editor_plan is not None:
                    return PlannedKriaTurn(
                        plan=editor_plan,
                        manifest_hash=manifest.manifest_hash,
                        context_hash=manifest.context_hash,
                        brief_updates=pre_extracted_updates,
                        brief_route=route,
                        brief_clip_ids=tuple(str(media.media_id) for media in manifest.media),
                        brief_manifest=manifest,
                        brief_coverage={
                            "applicable_ids": [req.id for req in effective.live()],
                            "retrieved_ids": list(
                                dict.fromkeys(
                                    [*pre_extracted_retrieved_ids, *[req.id for req in fresh]]
                                )
                            ),
                            "enforced_ids": [],
                            "unresolved_ids": [req.id for req in effective.live()],
                        },
                        brief_expected_version=prior_brief.version if prior_brief else 0,
                    )
            # Reuse the normal full planning pipeline below, including batching,
            # clip-intent resolution and brief binding. The extractor already owns
            # this turn's ledger update; Main Creator now only proposes the plan.
            planning_brief = effective
            extraction_complete = True
            item = await _refetch_item(db, item_id)
            plan = await db.get(ContentPlan, item.content_plan_id)
            if plan is None or plan.user_id != creator_id:
                raise RuntimeError("Kria target item ownership changed")
            persona = await db.get(Persona, plan.persona_id)
            if persona is None or persona.user_id != creator_id:
                raise RuntimeError("Kria creator context is unavailable")
        except BriefCoverageError as exc:
            return _request_recovery(
                manifest,
                prior_brief,
                updates=pre_extracted_updates if extraction_complete else (),
                reason=str(exc),
            )
        except (RuntimeError, ValidationError, BriefUpdateBatchError) as exc:
            log.warning(
                "kria_request_recovery_cause",
                stage="followup_extraction",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                exc_info=True,
            )
            # `extract_first` implies the brief is on, so the old "brief off: let the copilot
            # serve it" fallback that used to follow was unreachable (KRI-536).
            cause = _recovery_cause("followup_extraction", exc)
            if not extraction_complete:
                try:
                    served = await _serve_unextracted_edit(
                        db,
                        thread_id=thread_id,
                        item_id=item_id,
                        user_message=user_message,
                        prior_brief=prior_brief,
                        manifest=manifest,
                        editor_state=editor_state,
                        cause=cause,
                    )
                except Exception:  # noqa: BLE001 - best effort; the honest recovery follows
                    log.warning("kria_request_degrade_failed", **cause, exc_info=True)
                    served = None
                if served is not None:
                    return served
            return _request_recovery(
                manifest,
                prior_brief,
                updates=pre_extracted_updates if extraction_complete else (),
                reason="editor_planning_failed"
                if extraction_complete
                else "request_extraction_failed",
                cause=cause,
            )
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
            prior_brief=planning_brief,
            brief_on=brief_on,
        )
        if isinstance(inputs, PlannedKriaTurn):
            return inputs
        if extraction_complete:
            inputs = replace(
                inputs, agent_input=inputs.agent_input.model_copy(update={"brief_enabled": False})
            )
        outputs = []
        retrieved_ids = list(pre_extracted_retrieved_ids)
        for batch in inputs.brief_batches or (None,):
            batch_inputs = (
                inputs
                if batch is None
                else replace(
                    inputs,
                    agent_input=inputs.agent_input.model_copy(
                        update={"creator_request": batch.text}
                    ),
                )
            )
            outputs.append(
                await _call_main_creator(
                    batch_inputs,
                    thread_id=thread_id,
                    creator_id=creator_id,
                    item_id=item_id,
                    creator_agent_session_id=creator_agent_session_id,
                )
            )
            if batch is not None:
                retrieved_ids.extend(batch.requirement_ids)
        output = outputs[-1]
    except BriefCoverageError as exc:
        return _request_recovery(manifest, prior_brief, reason=str(exc))
    except (RuntimeError, ValidationError, BriefUpdateBatchError) as exc:
        log.warning(
            "kria_request_recovery_cause",
            stage="main_creator",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            exc_info=True,
        )
        if brief_on:
            # A malformed brief update is a reading failure; anything else is the planner.
            reading = isinstance(exc, BriefUpdateBatchError)
            return _request_recovery(
                manifest,
                prior_brief,
                updates=pre_extracted_updates,
                reason="request_extraction_failed" if reading else "creator_planning_failed",
                cause=_recovery_cause("main_creator", exc),
            )
        if not extract_first or answers_clip_question:
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

    unique_updates = {
        json.dumps(update.model_dump(mode="json"), sort_keys=True): update
        for update in (
            pre_extracted_updates
            if extraction_complete
            else tuple(update for result in outputs for update in result.brief_updates)
        )
    }
    updates = tuple(unique_updates.values())
    try:
        effective = apply_updates(prior_brief, updates, source_turn_id=None)
    except BriefUpdateBatchError as exc:
        return _request_recovery(manifest, prior_brief, reason=str(exc))
    effective_request = render_brief_request(effective, latest_message=user_message)
    if (
        len(outputs) > 1
        and len(
            {
                json.dumps(result.action.model_dump(mode="json"), sort_keys=True)
                for result in outputs
            }
        )
        > 1
    ):
        return _request_recovery(
            manifest, prior_brief, updates=updates, reason="planning_batches_disagree"
        )
    # Downstream clip inventory currently consumes one complete request. Refuse
    # explicitly if it cannot receive it; never send a sliced prefix.
    from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS  # noqa: PLC0415

    if len(effective_request) > CREATOR_REQUEST_MAX_CHARS:
        return _request_recovery(
            manifest, prior_brief, updates=updates, reason="clip_planner_context_limit"
        )
    fresh = new_requirements(prior_brief, effective)
    coverage = {
        "applicable_ids": [req.id for req in effective.live()],
        "retrieved_ids": list(dict.fromkeys([*retrieved_ids, *[req.id for req in fresh]])),
        "enforced_ids": [],
        "unresolved_ids": [req.id for req in effective.live()],
    }
    if pending_analysis_ids:
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=_clips_still_checking_reply(len(pending_analysis_ids)),
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
            brief_updates=updates,
            brief_expected_version=prior_brief.version if prior_brief else 0,
            brief_coverage={
                **coverage,
                "stage": "analysis",
                "missing_media_ids": pending_analysis_ids,
            },
        )
    clip_ids = tuple(str(media.media_id) for media in manifest.media)
    shape = CurrentPlanShape(has_render=False)
    # Every rollback above expires loaded rows; an expired attribute read on an
    # AsyncSession raises MissingGreenlet, so re-read the item before using it.
    item = await _refetch_item(db, item_id)
    if item.current_job_id is not None and semantic_request_scope != "rebuild":
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
    route = route_requirements(
        fresh,
        shape,
        message=user_message,
        full_replan=True
        if semantic_request_scope == "rebuild"
        else False
        if semantic_request_scope == "edit"
        else None,
    )
    if (
        answers_clip_question
        or creative_copy_pending
        or getattr(output, "creative_decision", None) is not None
    ):
        route = "replan"
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
                original_request=effective_request,
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
                brief_coverage=coverage,
                brief_expected_version=prior_brief.version if prior_brief else 0,
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
        brief_request=effective_request,
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
        brief_coverage=coverage,
        brief_expected_version=prior_brief.version if prior_brief else 0,
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
    session_id = None
    extraction_outputs = []
    for batch in inputs.brief_batches or (None,):
        request = inputs.agent_input.creator_request if batch is None else batch.text
        output = await _call_brief_extractor(
            inputs,
            creator_request=request,
            thread_id=thread_id,
            creator_id=creator_id,
            item_id=item_id,
            creator_agent_session_id=session_id,
            prior_brief=prior_brief,
        )
        extraction_outputs.append(output)
    scope, _clarification = _extraction_request_scope(extraction_outputs)
    if scope == "clarify":
        return (), None
    updates = tuple(update for output in extraction_outputs for update in output.brief_updates)
    effective = apply_updates(prior_brief, updates, source_turn_id=None)
    fresh = new_requirements(prior_brief, effective)
    item = await _refetch_item(db, item_id)
    shape = CurrentPlanShape(has_render=False)
    if item.current_job_id is not None and scope != "rebuild":
        target = await _load_editor_target(db, thread_id=thread_id, item=item)
        shape = plan_shape_from_editor_snapshot(target.snapshot if target else None)
        await db.rollback()
    return updates, route_requirements(
        fresh,
        shape,
        message=user_message,
        full_replan=True if scope == "rebuild" else False,
    )


__all__ = [
    "extract_deferred_brief",
    "PlannedKriaTurn",
    "adapt_creator_action",
    "adapt_editor_action",
    "localized_editor_state_reply",
    "plan_live_turn",
]
