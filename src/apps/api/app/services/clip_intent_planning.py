"""Inventory creator instructions independently before grounding them in footage."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalSchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    salvage_question,
)
from app.config import settings
from app.kria.reply_language import current_reply_language, say
from app.schemas.clip_intents import (
    ClipAssignment,
    ClipIntent,
    ResolvedClipIntent,
    ground_caption,
    ground_label,
    ground_placeholder_label,
)
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    picker_eligible,
    resolve_clip_intents_for_turn,
)
from app.services.clip_selection import ClipSelections
from app.services.clip_understanding import clip_record, understanding_incomplete

log = structlog.get_logger()

ClipRefresh = Callable[[], Awaitable[list[IntentClip]]]


@dataclass(frozen=True)
class PlannedIntentResolution:
    requested_intents: list[ClipIntent]
    resolution: IntentResolution


# How often a turn re-reads clip analysis while background understanding lands.
UNDERSTANDING_POLL_S = 2.0


def _unanalysed(clips: list[IntentClip]) -> list[IntentClip]:
    return [c for c in clips if understanding_incomplete(c.analysis, kind=c.kind)]


def _with_fresh_analysis(clips: list[IntentClip], fresh: list[IntentClip]) -> list[IntentClip]:
    """Take newly landed analysis for the SAME clips; never add, drop, or re-identify one.

    The turn's manifest was pinned before the wait, so a clip attached, removed, or
    replaced meanwhile keeps its original record and the confirm fence handles it.
    """
    by_id = {clip.media_id: clip for clip in fresh}
    out: list[IntentClip] = []
    for clip in clips:
        update = by_id.get(clip.media_id)
        same_object = (
            update is not None
            and update.gcs_path == clip.gcs_path
            and update.generation == clip.generation
        )
        out.append(replace(clip, analysis=update.analysis) if same_object else clip)
    return out


async def wait_for_clip_understanding(
    clips: list[IntentClip], *, refresh: ClipRefresh, until: float
) -> tuple[list[IntentClip], float]:
    """Re-read clip analysis until every clip is understood or ``time.monotonic()`` passes
    ``until``. Returns the clips and the seconds spent.

    The turn's clip snapshot is taken before the Main Creator call, so analysis that
    landed during it was invisible and the creator was told to resend a request the
    server could already answer (prod thread b2a41da6: "still checking" at 10:56:02Z,
    all 7 clips analysed at 10:56:03Z). The first re-read happens even past ``until``.
    A failed re-read keeps the clips as they were (the caller replies pending).
    """
    started = time.monotonic()
    while True:
        try:
            clips = _with_fresh_analysis(clips, await refresh())
        except Exception:  # noqa: BLE001 - a missed re-read is the old "still checking" reply
            log.warning("clip_intent_understanding_refresh_failed", exc_info=True)
            break
        remaining = until - time.monotonic()
        if not _unanalysed(clips) or remaining <= 0:
            break
        await asyncio.sleep(min(UNDERSTANDING_POLL_S, remaining))
    return clips, round(time.monotonic() - started, 1)


def resolve_order_by_intent(intent: ClipIntent, clips: list[IntentClip]) -> ResolvedClipIntent:
    """Resolve a ``by_capture_time`` / ``by_route`` order deterministically (KRI-189).

    It orders EVERY clip by a fact the phone recorded, so there is nothing for a
    vision model to decide: membership is all clips, at full confidence, and the
    planner sorts by each clip's capture time.
    """
    return ResolvedClipIntent(
        **intent.model_dump(),
        status="resolved",
        assignments=[
            ClipAssignment(
                media_id=clip.media_id,
                evidence="filming order",
                confidence=1.0,
            )
            for clip in clips
        ],
    )


def apply_creator_selections(
    intents: list[ClipIntent],
    *,
    clips: list[IntentClip],
    selections: ClipSelections | None,
    creator_request: str,
) -> tuple[list[ResolvedClipIntent], list[ClipIntent], set[str]]:
    """Honour the creator's clip-picker answers BEFORE the resolver runs (KRI-282).

    Returns ``(settled, remaining, dropped_ids)``:

    * ``settled``   intents the creator's tapped clips fully answer, as resolved intents
                    (confidence 1.0, evidence "creator selected"); they never reach the
                    resolver or the vision lane.
    * ``remaining`` everything else, resolved as usual.
    * ``dropped``   intents the creator said have no clips: empty-by-creator, removed from
                    the request entirely so they are never asked about again.
    """
    if not selections:
        return [], list(intents), set()
    by_id = {c.media_id: c for c in clips}
    order = {c.media_id: i for i, c in enumerate(clips)}
    settled: list[ResolvedClipIntent] = []
    remaining: list[ClipIntent] = []
    dropped: set[str] = set()
    for intent in intents:
        entry = selections.match(intent)
        if entry is None:
            remaining.append(intent)
            continue
        if entry.none:
            dropped.add(intent.intent_id)
            continue
        media_ids = sorted((m for m in entry.media_ids if m in by_id), key=order.__getitem__)
        assignments = _selected_assignments(intent, media_ids, by_id, creator_request)
        if not media_ids or not picker_eligible(intent) or assignments is None:
            remaining.append(intent)
            continue
        caption_text = caption_grounding = None
        if intent.op == "caption":
            grounded = ground_caption(
                value=intent.creator_text,
                confidence=1.0,
                creator_request=creator_request,
                records=[clip_record(by_id[m].analysis, kind=by_id[m].kind) for m in media_ids],
                intent_id=intent.intent_id,
            )
            if grounded is None:
                remaining.append(intent)
                continue
            caption_text, caption_grounding = grounded.text, grounded.grounding
        settled.append(
            ResolvedClipIntent(
                **intent.model_dump(),
                status="resolved",
                assignments=assignments,
                caption_text=caption_text,
                caption_grounding=caption_grounding,
            )
        )
    return settled, remaining, dropped


def _selected_assignments(
    intent: ClipIntent,
    media_ids: list[str],
    by_id: dict[str, IntentClip],
    creator_request: str,
) -> list[ClipAssignment] | None:
    """Assignments for tapped clips, or None when the intent's text cannot be grounded."""
    assignments: list[ClipAssignment] = []
    for media_id in media_ids:
        value = grounding = None
        if intent.op == "label":
            if intent.placeholder:
                label = ground_placeholder_label(media_id=media_id, intent_id=intent.intent_id)
            else:
                clip = by_id[media_id]
                label = ground_label(
                    media_id=media_id,
                    value=intent.creator_text,
                    confidence=1.0,
                    creator_request=creator_request,
                    record=clip_record(clip.analysis, kind=clip.kind),
                    intent_id=intent.intent_id,
                )
            if label is None:
                return None
            value, grounding = label.text, label.grounding
        assignments.append(
            ClipAssignment(
                media_id=media_id,
                value=value,
                evidence="creator selected",
                confidence=1.0,
                grounding=grounding,
            )
        )
    return assignments


async def plan_and_resolve_clip_intents(
    *,
    creator_request: str,
    latest_user_message: str | None,
    candidate_intents: list[ClipIntent] | None,
    clips: list[IntentClip],
    run_context: RunContext,
    generated_brief: str | None = None,
    background: bool = False,
    checkpoint: Any = None,
    max_vision_requeries: int | None = None,
    vision_deadline_s: float | None = None,
    require_clip_understanding: bool = False,
    refresh_clips: ClipRefresh | None = None,
    understanding_wait_until: float | None = None,
    clip_selections: ClipSelections | None = None,
) -> PlannedIntentResolution:
    if len(creator_request) > CREATOR_REQUEST_MAX_CHARS:
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=say(
                    en=(
                        "Please restate the complete clip instructions in a shorter message "
                        "so I can preserve all of them."
                    ),
                    tr=(
                        "Klip talimatlarının hepsini daha kısa bir mesajda yeniden yazar mısın? "
                        "Hiçbirini kaçırmak istemiyorum."
                    ),
                ),
            ),
        )
    # KRI-189: fact-based ordering is only understood for accounts with CLIP_FACTS.
    clip_facts_on = settings.clip_facts_for(getattr(run_context, "creator_id", None))
    # Candidates improve recall but are never the authority: even an empty
    # creative strategy must pass through the complete request inventory.
    try:
        output = await asyncio.to_thread(
            ClipIntentPlannerAgent(default_client()).run,
            ClipIntentPlannerInput(
                creator_request=creator_request,
                latest_user_message=latest_user_message,
                generated_brief=generated_brief,
                candidate_intents=candidate_intents,
                **({"clip_facts": True} if clip_facts_on else {}),
                reply_language=current_reply_language(),
            ),
            ctx=run_context,
        )
    except TerminalSchemaError as exc:
        # Invalid model output is a content-recovery problem, not a failed
        # preparation. Ask for a restatement and trust none of the candidate
        # intents. Refusals, transient exhaustion, and control-plane failures
        # remain terminal and are handled by their existing callers.
        cause = exc.__cause__
        error_class = getattr(cause, "error_class", None) or type(cause).__name__
        dropped = list(getattr(cause, "dropped", None) or [])
        # Redacted on purpose: the error CLASS and sizes only, never creator or
        # model text, so the next incident is diagnosable from logs alone.
        log.warning(
            "clip_intent_planner.terminal_schema",
            error_class=error_class,
            dropped_count=len(dropped),
            request_chars=len(creator_request),
            latest_chars=len(latest_user_message or ""),
            candidate_count=len(candidate_intents or []),
            clip_count=len(clips),
        )
        if dropped:
            question = salvage_question(0, dropped)
        else:
            question = say(
                en=(
                    "I couldn't safely verify the clip-specific instructions. "
                    "Please restate which clips to use, group, order, label, or caption."
                ),
                tr=(
                    "Klip talimatlarını güvenle doğrulayamadım. "
                    "Hangi klipleri kullanacağımı, gruplayacağımı, sıralayacağımı, "
                    "etiketleyeceğimi ya da yazı ekleyeceğimi yeniden yazar mısın?"
                ),
            )
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=question,
                error_code="planner_rejected_all",
                diagnostics={
                    "stage": "planning",
                    "drop_classes": list(getattr(cause, "drop_classes", None) or [error_class]),
                    "kept": 0,
                    "dropped": len(dropped),
                },
            ),
        )
    if output.silent_drops:
        # KRI-456: style asks / title lines the planner minted as caption intents and the
        # parser dropped without asking. Closed-vocabulary counts only, never text.
        log.info(
            "clip_intent_planner.silent_drops",
            silent_drops=output.silent_drops,
            kept=len(output.intents),
        )
    if output.salvage_question:
        # Some instructions were valid but others could not be verified (or the
        # inventory exceeded the cap). Never act on a silent subset: ask about
        # exactly the remainder. KRI-422: the closed-vocabulary reasons ride on
        # the turn's diagnostics, so the next incident is provable from the turn.
        log.warning(
            "clip_intent_planner.salvaged",
            reasons=output.salvage_reasons,
            kept=len(output.intents),
            request_chars=len(creator_request),
            candidate_count=len(candidate_intents or []),
            clip_count=len(clips),
        )
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=output.salvage_question,
                error_code="planner_salvage",
                diagnostics={
                    "stage": "planning",
                    "drop_classes": output.salvage_reasons,
                    "kept": len(output.intents),
                },
            ),
        )
    if output.question:
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=output.question,
            ),
        )
    intents = [
        ClipIntent.model_validate(intent.model_dump(exclude={"source_quote"}))
        for intent in output.intents
    ]
    if not clip_facts_on:
        # Defense in depth: the flag-off prompt never teaches `order_by`, so a
        # stray one is dropped rather than half-honored.
        intents = [
            intent.model_copy(update={"order_by": None}) if intent.order_by else intent
            for intent in intents
        ]
    visual_intents = [intent for intent in intents if intent.label_source == "clip"]
    order_by_intents = [intent for intent in visual_intents if intent.order_by]
    visual_intents = [intent for intent in visual_intents if not intent.order_by]
    resolved_orders = [resolve_order_by_intent(intent, clips) for intent in order_by_intents]
    # KRI-282: the creator's tapped clips are authoritative for the intents they answer.
    # Fact-ordered intents stay deterministic and are never matched to a selection.
    selected, visual_intents, dropped_ids = apply_creator_selections(
        visual_intents, clips=clips, selections=clip_selections, creator_request=creator_request
    )
    if dropped_ids:
        intents = [intent for intent in intents if intent.intent_id not in dropped_ids]
    resolved_orders = [*selected, *resolved_orders]
    # Transcript labels are fulfilled later from the pinned narration and
    # final timeline. They remain in the complete requested inventory, but
    # must never enter the vision resolver.
    if not visual_intents:
        if resolved_orders:
            return PlannedIntentResolution(intents, IntentResolution(intents=resolved_orders))
        return PlannedIntentResolution(intents, IntentResolution())
    if require_clip_understanding:
        # KRI-282 L2: a clip whose background analysis has not landed has an empty
        # record, so the resolver would ask the creator about it. That is OUR gap, not
        # theirs: wait (bounded) for the analysis already in flight, and only then
        # answer with the converging "still checking" reply.
        unanalysed = _unanalysed(clips)
        waited_s = 0.0
        if unanalysed and refresh_clips is not None:
            clips, waited_s = await wait_for_clip_understanding(
                clips,
                refresh=refresh_clips,
                until=(
                    time.monotonic()
                    if understanding_wait_until is None
                    else understanding_wait_until
                ),
            )
            log.info(
                "clip_intent_understanding_waited",
                waited_s=waited_s,
                unanalysed_before=len(unanalysed),
                unanalysed_after=len(_unanalysed(clips)),
                total_clips=len(clips),
            )
            unanalysed = _unanalysed(clips)
        if unanalysed:
            return PlannedIntentResolution(
                intents,
                IntentResolution(
                    status="pending",
                    error_code="clip_understanding_incomplete",
                    diagnostics={
                        "reason": "clip_understanding_incomplete",
                        "unanalysed_clips": len(unanalysed),
                        "total_clips": len(clips),
                        "understanding_wait_s": waited_s,
                    },
                ),
            )
    resolution = await resolve_clip_intents_for_turn(
        intents=visual_intents,
        creator_request=creator_request,
        clips=clips,
        run_context=run_context,
        background=background,
        checkpoint=checkpoint,
        max_vision_requeries=max_vision_requeries,
        vision_deadline_s=vision_deadline_s,
    )
    if resolved_orders:
        resolution = replace(resolution, intents=[*resolution.intents, *resolved_orders])
    return PlannedIntentResolution(intents, resolution)
