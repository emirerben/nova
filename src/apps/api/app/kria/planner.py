"""Runtime-v2 planner adapter over Nova's production creative intelligence.

The planner returns inert intents only.  It never receives storage paths and
cannot author risk, target pins, idempotency identities, or completion claims.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import hashlib
import json
import math
import re
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._model_client import default_client
from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    RunContext,
    TerminalError,
    TerminalSchemaError,
    TransientError,
)
from app.agents._schemas.brief_extractor import BriefExtractionOutput
from app.agents._schemas.creator_agent import (
    AskUser,
    CreativeStrategy,
    DraftVoiceoverScript,
    ProposeStrategy,
    ResolvedCreatorManifest,
    ReviewDecision,
)
from app.agents._schemas.edit_format import (
    CLIP_INTENT_FREE_EDIT_FORMATS,
    NARRATED_EDIT_FORMATS,
)
from app.agents.brief_extractor import BriefExtractionInput, BriefExtractorAgent
from app.agents.clip_intent_planner import ConfirmedTitleCopy, unexpressed_notice
from app.agents.length_options import length_options
from app.agents.main_creator import (
    MAIN_CREATOR_CONVERSATION_MAX,
    CreativeCopyDecision,
    MainCreatorAgent,
    MainCreatorInput,
    MainCreatorOutput,
)
from app.config import settings
from app.kria import voiceover_script
from app.kria.brief import (
    BriefCoverageError,
    BriefUpdate,
    BriefUpdateBatchError,
    CreativeBrief,
    CurrentPlanShape,
    Route,
    apply_updates,
    bind_updates,
    brief_context,
    load_latest_brief,
    new_requirements,
    plan_shape_from_editor_snapshot,
    render_brief_request,
    render_unrecorded_request,
    requested_media,
    route_requirements,
    start_fresh_updates,
    wants_clip_descriptions,
)
from app.kria.brief_checks import spoken_text_literals, whole_line_quotes
from app.kria.brief_route import loose_text
from app.kria.clip_exclusions import (
    carry_forward_exclusions,
    earlier_exclusions,
    exclusion_question,
    exclusions_to_ask,
    length_exclusion_notice,
    still_played_exclusions,
    still_played_notice,
    with_exclusion_categories,
)
from app.kria.clip_scope import apply_clip_exclusions, apply_clip_scope, apply_subject_scope
from app.kria.clip_ties import left_out, tie_question, voiceover_ties, with_tie_categories
from app.kria.compose_with_ops import compose_unmet_with_ops
from app.kria.contracts import KriaTurnPlan
from app.kria.draft_sound_carry import sound_asks_unchanged
from app.kria.duplicate_send import is_duplicate_answer
from app.kria.hidden_parts import carry_hidden_parts, earlier_hidden_parts, hidden_part_question
from app.kria.missing_media import (
    CHECKED,
    FOUND,
    FOUND_REASON,
    after_looking,
    answers_found_card,
    asks_for_missing_media,
    found_question,
    unplaced_reply,
)
from app.kria.narration_notes import (
    keeps_camera_sound,
    narration_note_parts,
    spoken_plan,
    with_note,
)
from app.kria.order_replacement import OrderReplacementProof
from app.kria.reply_language import current_reply_language, say
from app.kria.strategy_policy import (
    RefusedStrategy,
    check_strategy_for_runtime_v2,
    renderable_clip_intents,
    song_captions_refusal,
    transcript_labels_dropped_notice,
)
from app.kria.style_asks import brief_moment_ask, carry_moment_ask, moment_ask_in_text
from app.kria.voiceover_clips import keep_every_voiceover_clip
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    Persona,
    PlanItem,
)
from app.pipeline.best_parts import edit_cap_s
from app.pipeline.pinned_text import pin_range_grounded
from app.routes._copilot import CopilotTurnBody, is_overlay_display_ask, run_copilot_turn
from app.routes.generative_jobs import variant_render_baseline
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.schemas.edit_proposal import (
    MixedMediaTimingProfile,
    PinnedText,
)
from app.schemas.story_intents import (
    RejectedClipSelection,
    SelectionReconciliation,
    StoryIntentRefs,
)
from app.schemas.user_song import (
    SONG_ALIGNMENT_VERSION,
    SONG_ORDER_ANSWER_KEY,
    SONG_RANGE_ANSWER_KEY,
)
from app.services.choice_questions import (
    CONFLICT_ORDER_VS_GROUP,
    ORDER_VS_GROUP_OPTIONS,
    SIMPLER_OMITTED_IDS_KEY,
    SIMPLER_VERSION_REASON,
    ChoiceCapability,
    answered_brief,
    ask_user_choice,
    build_choice_question,
    change_request_selected,
    choice_question_text,
    detect_order_vs_group,
    fold_choice_answers,
    length_inputs_waiting,
    resolve_choices,
    simpler_version_accepted,
    simpler_version_question,
    tag_event,
    without_length_answer_echo,
)
from app.services.clip_intent_answers import persist_clip_intent_vision_answers
from app.services.clip_intent_planning import (
    KeptLabel,
    PlannedIntentResolution,
    brief_label_asks,
    kept_labels_from_strategy,
    plan_and_resolve_clip_intents,
    sequence_text_literals,
    shot_captions_safe,
    whole_video_text_asks,
)
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_order_sequence import stated_anchors
from app.services.clip_pick_collisions import plan_without_pick_collisions
from app.services.clip_selection import answered_rows, fold_clip_selections
from app.services.clip_understanding import understanding_incomplete
from app.services.creative_copy_decisions import (
    CreativeCopyState,
    authorship_question,
    bind_approved_copy,
    creator_quoted,
    fold_creative_copy,
    localize_question,
    media_digest,
    question_message,
    wording_question,
)
from app.services.creator_model_comparison import configure_creator_agent
from app.services.creator_sessions import (
    creator_context,
    load_intent_clips_for_item,
    resolve_item_creator_context,
)
from app.services.kept_sound_lines import kept_line_records
from app.services.kria_editor_ops import (
    EDITOR_STATE_STALE_REPLY,
    MAX_BACKGROUND_POOL_ROWS,
    MAX_EDITOR_OPS,
    SPEECH_CUT_NEEDS_SAVE_REPLY,
    EditorDraftBasis,
    EditorStateStaleError,
    background_state,
    build_editor_snapshot,
    clip_facts_by_media_id,
    clip_seen_by_media_id,
    clip_visible_text_by_media_id,
    coalesce_text_style_ops,
    editor_state_has_lanes,
    failed_editor_draft_basis,
    resolve_editor_base,
    unknown_editor_render_query,
)
from app.services.music_genre import (
    GENRE_FIT,
    asked_genres,
    genre_record,
    music_genre_notice,
)
from app.services.song_order import (
    build_song_order_question,
    fold_song_orders,
    load_ready_alignment,
    model_text,
    resolve_creator_placements,
    resolve_uncertain_takes,
    resolved_song_takes_payload,
    song_order_question_text,
    takes_needing_order,
    thread_keeps_lipsync,
)
from app.services.song_range import (
    build_song_range_question,
    fold_song_range_events,
    restrict_alignment_to_range,
    should_ask_song_range,
    song_range_of,
    song_range_question_text,
)
from app.services.spoken_quotes import (
    Transcribe,
    closing_quote_notice,
    described_lines,
    ground_closing_quote,
    ground_named_lines,
    named_line_records,
    transcribe_stored_quote,
)
from app.services.spoken_roles import SpokenSeats
from app.services.story_intent_reconciliation import reconcile_story_intents

log = structlog.get_logger()


def _held_visual_pool(manifest: Any, strategy: Mapping[str, Any]) -> tuple[str, ...] | None:
    """KRI-824: the Visual stills (``asset-*`` pool photos) a unified montage places.

    Only the montage family without a recorded voiceover places Visuals beside the
    footage (``_run_phone_unified_montage_job``), narrowed to an explicit ``selected``
    scope as the worker narrows it. None for every other format: no Visual plays there,
    so no held seconds come off the creator's total.
    """
    from app.pipeline.unified_montage import selected_visual_ids  # noqa: PLC0415

    if strategy.get("edit_format") != "montage" or strategy.get("audio_strategy") == "voiceover":
        return None
    selected = selected_visual_ids(strategy)
    return tuple(
        media.media_id
        for media in getattr(manifest, "media", None) or ()
        if media.kind == "image"
        and media.media_id.startswith("asset-")
        and (selected is None or media.media_id in selected)
    )


def _attached_visual_ids(manifest: Any) -> tuple[str, ...]:
    """KRI-989: every Visual (``asset-*`` pool media, any kind) the manifest attaches.

    The attachment authority for the length plan: "is this id attached", not "does this
    format place it" (``_held_visual_pool`` answers that, for the budget). Tolerates no manifest.
    """
    return tuple(
        media.media_id
        for media in getattr(manifest, "media", None) or ()
        if str(media.media_id).startswith("asset-")
    )


async def _snapshot_visual_rows(db: AsyncSession, item: PlanItem) -> list:
    """KRI-1069: the item's Visuals the turn's media snapshot keeps (``snapshot_visual``).

    Every creator-visible pool row, as the manifest lists them, so a photo still being
    analyzed keeps its capture time; the dispatch gate waits for it to be ready.
    """
    from app.models import PlanItemAsset  # noqa: PLC0415
    from app.services.creator_sessions import CREATOR_VISIBLE_ASSET_STATES  # noqa: PLC0415

    owner = select(ContentPlan.user_id).where(ContentPlan.id == item.content_plan_id)
    rows = await db.execute(
        select(PlanItemAsset)
        .where(
            PlanItemAsset.plan_item_id == item.id,
            PlanItemAsset.user_id == owner.scalar_subquery(),
            PlanItemAsset.status.in_(CREATOR_VISIBLE_ASSET_STATES),
            PlanItemAsset.deduplicated_to_asset_id.is_(None),
        )
        .order_by(PlanItemAsset.created_at, PlanItemAsset.id)
    )
    return list(rows.scalars())


def _timing_profile(strategy: Mapping[str, Any]) -> MixedMediaTimingProfile | None:
    """The strategy's server-read photo/video timing (``recognize_mixed_media_timing``)."""
    raw = strategy.get("mixed_media_timing")
    if isinstance(raw, MixedMediaTimingProfile):
        return raw
    if not isinstance(raw, Mapping):
        return None
    try:
        return MixedMediaTimingProfile.model_validate(raw)
    except ValueError:
        return None


def _edit_cap_s(strategy: Mapping[str, Any]) -> float:
    """The longest edit this strategy can be planned to: spoken sources run longer."""
    from app.services.length_policy import natural_length_owner  # noqa: PLC0415

    try:
        return edit_cap_s(natural_length_owner(strategy))
    except ValueError:
        return edit_cap_s(None)


def _enrich_best_parts_snapshot(
    strategy: Mapping[str, Any],
    media_snapshot: dict | None,
    brief: CreativeBrief | None,
    *,
    closing_quote: Mapping[str, Any] | None = None,
    named_lines: Sequence[Mapping[str, Any]] | None = None,
    camera_sound: bool = False,
    visual_photo_ids: Sequence[str] | None = None,
    attached_visual_ids: Sequence[str] = (),
    moment_pins: Any = None,
) -> dict | None:
    """Attach source-bounded footage capacity before the choice collector runs.

    ``moment_pins`` (KRI-723, ``named_moments.MomentDecision``): the creator's named moments of
    the one take with their pinned authority; the length plan plays exactly those windows.

    ``closing_quote`` is ``spoken_quotes.ground_closing_quote``'s record of the line the
    creator asked to end on (KRI-597); ``named_lines`` is ``_ground_spoken_lines``' record of
    the lines the creator wants heard whole (KRI-803, word timings included), else the timed
    sentences alone place them; the brief's moment ask (KRI-596, "the best six
    moments, each very short") lets a long take give several windows; a clip the creator's
    sound or order ask describes by what is said in it starts on that sentence (KRI-681), and
    one a sound ask keeps audible for that line plays the whole sentence (KRI-1028,
    ``kept_sound_lines.kept_line_records``).
    ``camera_sound`` (``narration_notes.keeps_camera_sound``, KRI-585): every clip's own
    sound plays, so a welcome the clips' words move forward keeps its line and a pinned
    closing clip that only introduces someone plays its stretch without words.
    ``visual_photo_ids`` (``_held_visual_pool``, KRI-824): the pool stills this format
    places beside the footage; a creator-held one is budgeted off the creator's total.
    ``attached_visual_ids`` (``_attached_visual_ids``, KRI-989): every manifest Visual, so a
    selected pool photo is attached rather than reported as a missing clip.
    """
    if not isinstance(media_snapshot, dict):
        return media_snapshot
    from app.kria.asset_holds import asset_holds, visual_budget  # noqa: PLC0415
    from app.pipeline.story_direction import story_floor_for  # noqa: PLC0415
    from app.pipeline.unified_montage import (  # noqa: PLC0415
        MIN_READABLE_SHOT_S,
        brief_view,
        unheld_still_frames,
    )
    from app.services.creator_render_contract import build_render_contract  # noqa: PLC0415
    from app.services.length_planning import build_length_plan  # noqa: PLC0415
    from app.services.length_policy import creator_duration, natural_length_owner  # noqa: PLC0415

    if natural_length_owner(strategy) not in {"footage", "voiceover", "song"}:
        return media_snapshot
    rows = media_snapshot.get("clip_assignments")
    if not isinstance(rows, list):
        return media_snapshot
    selected = tuple(
        dict.fromkeys(str(value) for value in strategy.get("selected_media_ids") or [])
    )
    del rows
    creator_target = strategy.get("creator_duration_s")
    proven = creator_duration(brief=brief)
    creator_target_s = (proven.seconds if proven is not None else None) or (
        float(creator_target)
        if isinstance(creator_target, (int, float)) and creator_target > 0
        else None
    )
    contract = build_render_contract(
        strategy,
        generation_id="length-planning",
        brief=brief,
        media_snapshot=media_snapshot,
    )
    recording = media_snapshot.get("voiceover_duration_s")
    if recording is None and isinstance(media_snapshot.get("voiceover"), dict):
        recording = media_snapshot["voiceover"].get("duration_s")
    moments = brief_moment_ask(brief)
    # A planner direction of text_explainer / guided_story holds every clip at least that
    # direction's own minimum moment, so the approved windows already play as the story the
    # render builds (and the length on the card is its length). Only where nothing the
    # creator asked keeps the draft a fast montage; the worker decides the rest with the
    # same rule (`story_direction.story_blocker`).
    story_floor_s = (
        story_floor_for(strategy, brief_view(brief))
        if strategy.get("edit_format") == "montage" and recording is None
        else None
    )
    held = _held_closing_ids(closing_quote)
    grounded = {str(row.get("quote")): row for row in named_lines or () if isinstance(row, Mapping)}
    named = [
        dict(grounded.get(row["quote"], row))
        for row in named_line_records(_whole_line_quotes(brief), media_snapshot, skip_ids=held)
    ]
    described = {
        media_id: found.window
        for media_id, found in described_lines(
            _described_clip_texts(brief), media_snapshot, skip_ids=held, named=named
        ).items()
    }
    named_ids = {row.get("media_id") for row in named if row.get("status") == "found"}
    kept = kept_line_records(brief, media_snapshot, skip_ids=held)
    for row in kept:
        window = (row["window_start_s"], row["window_end_s"])
        if row["media_id"] in named_ids and row["media_id"] in described:
            # A whole line the creator named in the same clip (KRI-760) plays too.
            other = described[row["media_id"]]
            window = (min(window[0], other[0]), max(window[1], other[1]))
            row.update(window_start_s=window[0], window_end_s=window[1])
        described[row["media_id"]] = window

    # KRI-824: a photo the creator gave its own seconds gets exactly that window. KRI-825:
    # so does every photo when the creator gave the photos a length as a group, and the
    # creator's total is ONE budget: every placed Visual (a pool photo outside the clip
    # record) plays its own length beside the footage -- a held one its hold, the others
    # the montage's own pacing (``unheld_still_frames``) -- so the footage, never a photo,
    # gives way, and the file plays the approved total.
    clip_rows = [
        row for row in media_snapshot.get("clip_assignments") or [] if isinstance(row, Mapping)
    ]
    image_ids = [str(row.get("media_id")) for row in clip_rows if row.get("kind") == "image"]
    own_holds = asset_holds(brief, image_ids)
    profile = _timing_profile(strategy)
    group_hold_s = profile.image_hold_s if profile is not None else None
    still_holds = {media_id: hold.hold_s for media_id, hold in own_holds.items()}
    if group_hold_s is not None:
        for media_id in image_ids:
            still_holds.setdefault(media_id, round(float(group_hold_s), 3))
    budget = visual_budget(
        brief,
        [str(row.get("media_id")) for row in clip_rows],
        visual_photo_ids,
        group_hold_s=group_hold_s,
        unheld_frames_each=unheld_still_frames(profile),
    )
    hold_conflict: dict[str, Any] | None = None
    if creator_target_s is not None and budget.total_s:
        footage_s = still_photo_floor_s = sum(still_holds.values())
        footage_s += MIN_READABLE_SHOT_S * sum(
            1
            for row in clip_rows
            if row.get("kind") != "image" and (not selected or row.get("media_id") in selected)
        )
        if creator_target_s - budget.total_s >= footage_s - 1e-6:
            creator_target_s = round(creator_target_s - budget.total_s, 3)
        elif budget.held_frames or still_photo_floor_s:
            # The creator's held photos and their total cannot both play: one question
            # (``choice_questions._holds_vs_length``), never a silent clamp.
            hold_conflict = {
                "requested_s": creator_target_s,
                "needed_s": float(math.ceil(budget.total_s + footage_s - 1e-6)),
                "held_s": round(budget.held_s + still_photo_floor_s, 3),
                "requirement_ids": sorted(
                    {*budget.requirement_ids, *(h.requirement_id for h in own_holds.values())}
                ),
                "group_hold_s": group_hold_s,
            }

    def windows_for(seats: SpokenSeats | None) -> dict[str, Any]:
        quiet_id = seats.quiet_close[0] if seats and seats.quiet_close else None
        if seats is not None and quiet_id in described:
            # The creator described that clip by what is said in it.
            seats = replace(seats, quiet_close=None)
        quiet = seats.quiet_close if seats is not None else None
        opening = seats.opening if seats is not None else None
        return build_length_plan(
            {**strategy, "voiceover_duration_s": recording},
            media_snapshot,
            creator_target_s=creator_target_s,
            # KRI-847: a brief ceiling ("at most 10 seconds") caps the plan instead of fixing it.
            creator_target_bound=proven.bound if proven is not None else "exact",
            selected_ids=selected,
            order_ids=contract.order_ids if contract is not None else (),
            closing_quote=closing_quote,
            moment_count=moments.count if moments is not None else None,
            moment_max_s=moments.max_s if moments is not None else None,
            moment_each_s=moments.each_s if moments is not None else None,
            # The creator's own description of a clip outranks its welcome line.
            spoken_moments={**dict([opening] if opening else []), **described},
            quiet_moments=dict([quiet]) if quiet else None,
            spoken_order=seats.record() if seats is not None else None,
            named_lines=named,
            kept_lines=kept,
            still_holds=still_holds,
            attached_visual_ids=attached_visual_ids,
            # The Visuals play beside the windows whether or not they fit inside the total.
            visual_budget_s=budget.total_s,
            pinned_moments=(
                [pin.json() for pin in moment_pins.pins] if moment_pins is not None else None
            ),
            skipped_moments=(
                [pin.json() for pin in moment_pins.skipped] if moment_pins is not None else None
            ),
            story_floor_s=story_floor_s,
        )

    # KRI-585: the clips' words seat the welcome's line and a quiet ending. "Fast cuts" is
    # a moment ask too; when it takes several moments per take, the reply checks what
    # those windows hold instead of trusting the seat.
    seats = (
        spoken_plan(
            strategy,
            media_snapshot,
            brief,
            contract=contract,
            clip_intents_enabled=settings.clip_intents_enabled,
            closing_quote=closing_quote,
        )
        if camera_sound
        else None
    )
    length_plan = windows_for(seats)
    if seats is not None and length_plan["conflicts"]:
        # A held welcome or a capped ending must never be what makes the length impossible:
        # the order still follows the words, the windows are the plain ones.
        plain = windows_for(replace(seats, opening=None, quiet_close=None))
        if not plain["conflicts"]:
            length_plan = plain
    enriched = dict(media_snapshot)
    enriched["length_plan"] = length_plan
    if hold_conflict is not None:
        enriched["still_hold_conflict"] = hold_conflict
    else:
        enriched.pop("still_hold_conflict", None)
    # Keep legacy readers and their receipts stable while approved workers move
    # to the versioned envelope.
    enriched["best_parts"] = list(length_plan["windows"])
    enriched["best_parts_capacity_s"] = length_plan["capacity_s"]
    enriched["best_parts_conflicts"] = [
        {"code": item["code"], "message": item["message"]} for item in length_plan["conflicts"]
    ]
    return enriched


def _described_clip_texts(brief: CreativeBrief | None) -> list[str]:
    """The creator's live sound and order asks, whose words may describe a clip by what
    is said in it ("the clip where the boat won't start: keep its sound", KRI-681)."""
    if brief is None:
        return []
    texts: list[str] = []
    for req in brief.live():
        if whole_line_quotes(req):
            # KRI-760: "'...' dediği cümle baştan sona duyulsun" names its clip by the line
            # (`_whole_line_quotes`); the rest of the sentence describes no clip.
            continue
        if req.kind not in ("audio", "order"):
            continue
        texts.extend(text for text in (req.description, req.literal) if text)
        texts.extend(value for value in req.facts.values() if isinstance(value, str))
    return texts


def _whole_line_quotes(brief: CreativeBrief | None) -> list[str]:
    """The lines the creator wants heard whole (KRI-760), without repeats."""
    if brief is None:
        return []
    return list(dict.fromkeys(quote for req in brief.live() for quote in whole_line_quotes(req)))


def _held_closing_ids(closing_quote: Mapping[str, Any] | None) -> list[str]:
    """The clip a found closing line holds: its window belongs to the ending."""
    if isinstance(closing_quote, Mapping) and closing_quote.get("status") == "found":
        return [str(closing_quote.get("media_id"))]
    return []


async def _ground_spoken_lines(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any] | None,
    brief: CreativeBrief | None,
    *,
    transcribe: Transcribe | None = transcribe_stored_quote,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
    """(closing line, lines heard whole) on a montage, before the length plan freezes them.

    KRI-597: "end on the moment I say '...'"; KRI-760/803: "keep '...' audible". Each is
    found in the timed sentences, else by one word-timed pass of the clip whose transcript
    says it. Any other edit format grounds neither.
    """
    if strategy.get("edit_format") != "montage":
        return None, None
    closing_quote = await ground_closing_quote(strategy, media_snapshot, transcribe=transcribe)
    quotes = _whole_line_quotes(brief)
    named = (
        await ground_named_lines(
            quotes,
            media_snapshot,
            skip_ids=_held_closing_ids(closing_quote),
            transcribe=transcribe,
        )
        if quotes
        else None
    )
    return closing_quote, named


def _moment_count_notice(
    strategy: Mapping[str, Any],
    brief: CreativeBrief | None,
    length_plan: object,
    *,
    summary: str = "",
) -> str | None:
    """KRI-596: the reply says so when the montage can't hold the moments asked for.

    KRI-776: also when the draft's own summary counts more moments than the plan holds ("I
    will cut the 6 exact moments" over one cut of the take), whoever gave that number.
    """
    if (
        strategy.get("edit_format") != "montage"
        or not isinstance(length_plan, Mapping)
        or length_plan.get("window_eligible") is not True
    ):
        return None
    planned = len(length_plan.get("windows") or [])
    ask = brief_moment_ask(brief)
    promised = moment_ask_in_text(summary) if summary else None
    if ask is not None and ask.count is not None:
        if planned == ask.count:
            return None
        count = ask.count
    elif promised is not None and promised.count is not None and promised.count > planned:
        count = promised.count
    else:
        return None
    return say(
        en=f"I could fit {planned} {'moment' if planned == 1 else 'moments'}, not {count}.",
        tr=f"{count} değil, {planned} an sığdırabildim.",
    )


def _fit_music_section_to_footage(
    section: dict[str, Any], rows: Sequence[Any], *, selected_ids: set[str]
) -> dict[str, Any]:
    """Trim an auto-selected song section so the footage can always fill it (KRI-600).

    A frozen licensed-music section becomes the edit's length owner, so a 12-18 s
    "best section" over one 5 s clip is unfillable and the creator is told to "add
    longer footage" for music they never asked for. The song is a background bed: keep
    the section's start (the matcher's best moment) and end it where the footage ends.

    Only trims when the capacity is known exactly: every selected row must be a video
    with a finite duration. Stills (synthetic holds) or unmeasured clips leave the
    section untouched, as does a section that already fits.
    """
    if section.get("status") != "selected":
        return section
    start, end = section.get("start_s"), section.get("end_s")
    if isinstance(start, bool) or isinstance(end, bool):
        return section
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return section
    capacity = 0.0
    counted = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if selected_ids and str(row.get("media_id")) not in selected_ids:
            continue
        duration = row.get("duration_s")
        if (
            row.get("kind", "video") != "video"
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not 0 < duration < float("inf")
        ):
            return section
        capacity += float(duration)
        counted += 1
    if not counted:
        return section
    fitted_end = math.floor((start + capacity) * 1000) / 1000
    if fitted_end >= end - 1e-9 or fitted_end <= start:
        return section
    return {**section, "end_s": fitted_end}


def _fit_music_section_to_length(section: dict[str, Any], target_s: float | None) -> dict[str, Any]:
    """Make an auto-selected song section as long as the creator asked (KRI-679).

    The matcher's "best section" is often 10-17 s. Frozen as the length owner, it capped a
    footage-rich "25 seconds" montage at 16.6 s and asked the creator to accept that. The
    song is a background bed, so the section follows the creator's length instead: keep the
    matcher's start, run forward, and start earlier only when the track ends first. A track
    shorter than the ask plays whole. Without a known catalog length the section can only be
    shortened. Run before `_fit_music_section_to_footage`, which still wins when the footage
    is shorter than the ask.
    """
    if section.get("status") != "selected" or not isinstance(target_s, (int, float)):
        return section
    start, end = section.get("start_s"), section.get("end_s")
    if isinstance(start, bool) or isinstance(end, bool) or isinstance(target_s, bool):
        return section
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return section
    if not 0 < target_s < float("inf") or abs((end - start) - target_s) <= 1e-3:
        return section
    if end - start > target_s:
        return {**section, "end_s": math.floor((start + target_s) * 1000) / 1000}
    catalog = section.get("catalog_duration_s")
    if (
        isinstance(catalog, bool)
        or not isinstance(catalog, (int, float))
        or not end <= catalog < float("inf")
    ):
        return section
    if start + target_s <= catalog:
        return {**section, "end_s": math.floor((start + target_s) * 1000) / 1000}
    new_start = max(0.0, math.ceil((catalog - target_s) * 1000) / 1000)
    return {**section, "start_s": new_start, "end_s": float(catalog)}


def _creator_music_ask(brief: CreativeBrief | None) -> str:
    """The creator's own words about the song, from the live brief (KRI-679).

    Only live audio asks for the whole edit: a clip-scoped audio ask is a beat sound,
    and a superseded one was replaced by a later ask.
    """
    if brief is None:
        return ""
    asks = (
        req.text().strip() for req in brief.live() if req.kind == "audio" and req.scope == "global"
    )
    return "; ".join(ask for ask in asks if ask)


def _music_pool_digest(rows: Any, audio: Any, music_ask: str = "") -> str:
    """The uploaded clips and audio choice a frozen song was matched against (KRI-679).

    Unlike the full music input digest, this ignores which clips the edit currently
    selects and their analysis, so a later turn that only re-selects clips keeps the song.
    A creator music ask is part of it, so a new mood re-matches; without one the digest
    stays what earlier length questions stored.
    """
    media_ids = sorted(
        str(row.get("media_id"))
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, Mapping) and row.get("media_id")
    )
    pool: dict[str, Any] = {"media": media_ids, "audio": audio}
    if music_ask:
        pool["music_ask"] = music_ask
    return hashlib.sha256(json.dumps(pool, sort_keys=True).encode()).hexdigest()


def _prior_frozen_music(
    events: Sequence[tuple[str, Any]], input_digest: str, pool_digest: str
) -> dict[str, Any] | None:
    """The song an earlier length question in this thread already froze.

    An exact input match reuses whatever was frozen. Otherwise a selected song frozen
    over the same clip pool and audio choice is kept (KRI-679): answering "use the
    available length" changed the selection, the matcher ran again, and every answer
    produced a new, shorter section to ask about.
    """
    pool_match: dict[str, Any] | None = None
    for role, payload in reversed(events):
        question = payload.get("choice_question") if isinstance(payload, dict) else None
        context = question.get("length_context") if isinstance(question, dict) else None
        if role != "assistant" or not isinstance(context, dict):
            continue
        if context.get("input_digest") == input_digest:
            return context.get("licensed_music")
        music = context.get("licensed_music")
        if (
            pool_match is None
            and context.get("pool_digest") == pool_digest
            and isinstance(music, dict)
            and music.get("status") == "selected"
        ):
            pool_match = music
    return pool_match


async def _freeze_licensed_music_section(
    db: Any,
    strategy: Mapping[str, Any],
    snapshot: dict | None,
    *,
    run_context: RunContext,
    target_s: float | None = None,
    music_ask: str = "",
) -> dict[str, Any] | None:
    """Match once in a real live session and return an immutable catalog section.

    ``target_s`` is the creator's own length; the section follows it (KRI-679).
    ``music_ask`` is the creator's own words about the song; the matcher weighs it first.
    ``run_context`` owns the matcher run: no render Job exists yet (KRI-809).
    """
    if not isinstance(db, AsyncSession) or not isinstance(snapshot, dict):
        return None
    if strategy.get("audio_strategy", "licensed_music") != "licensed_music":
        return None
    if strategy.get("has_voiceover") or strategy.get("voice_mode") == "continuous":
        return None
    rows = snapshot.get("clip_assignments")
    if not isinstance(rows, list) or not rows:
        return {"status": "fallback_footage"}
    selected = {str(value) for value in strategy.get("selected_media_ids") or []}
    playable = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("duration_s"), (int, float))
        and not isinstance(row.get("duration_s"), bool)
        and 0.1 <= row["duration_s"] < float("inf")
        and (not selected or str(row.get("media_id")) in selected)
    ]
    if not playable:
        return {"status": "fallback_footage"}
    from app.pipeline.agents.gemini_analyzer import ClipMeta  # noqa: PLC0415
    from app.services.music_sections import track_config_with_rank_one  # noqa: PLC0415
    from app.tasks.generative_build import _match_best_track  # noqa: PLC0415

    metas = [
        ClipMeta(
            clip_id=str(row["media_id"]),
            transcript="",
            hook_text="",
            hook_score=7.0,
            best_moments=list((row.get("analysis") or {}).get("best_moments") or []),
            detected_subject=str((row.get("analysis") or {}).get("description") or ""),
            clip_path=str(row.get("gcs_path") or row.get("proxy_path") or ""),
        )
        for row in playable
        if row.get("media_id")
    ]
    # KRI-848: a genre the creator named is recorded with whether the song's labels say it.
    genres = asked_genres(music_ask)
    track = await asyncio.to_thread(
        _match_best_track, metas, job_id=None, music_ask=music_ask, run_context=run_context
    )
    no_song = {"status": "fallback_footage", **genre_record(genres, None)}
    if track is None:
        return no_song
    cfg = track_config_with_rank_one(track)
    start = cfg.get("best_start_s")
    end = cfg.get("best_end_s")
    catalog_duration = float(getattr(track, "duration_s", 0) or 0)
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
        or not 0 <= start < end <= catalog_duration < float("inf")
    ):
        return no_song
    section = _fit_music_section_to_length(
        {
            "status": "selected",
            "track_id": str(track.id),
            "title": str(getattr(track, "title", "")),
            "artist": str(getattr(track, "artist", "")),
            "start_s": float(start),
            "end_s": float(end),
            "catalog_duration_s": catalog_duration,
            "beat_timestamps_s": list(getattr(track, "beat_timestamps_s", None) or []),
            **genre_record(genres, track),
        },
        target_s,
    )
    return _fit_music_section_to_footage(section, rows, selected_ids=selected)


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
    # KRI-814: every extraction batch typed `delegates_all_choices` and the turn has no
    # update, so the turn-completion transaction records an empty brief version: the
    # server-owned evidence that the request was read and asks for nothing specific.
    brief_delegated: bool = False
    # KRI-814: the earlier creator lines that delegation reading also read (see
    # `_CreatorInputs.delegation_read_ids`); persistence checks them against the thread.
    brief_delegation_read_ids: tuple[str, ...] = ()
    brief_clip_ids: tuple[str, ...] = ()
    # The manifest this turn planned against, so the receipt checks resolve
    # reaction beats (owned images, capability) exactly as approval will.
    brief_manifest: ResolvedCreatorManifest | None = None
    # KRI-721: resolved for the proposed format, without applying it to the item.
    readiness_manifest: ResolvedCreatorManifest | None = None
    brief_coverage: dict | None = None
    brief_expected_version: int | None = None
    media_snapshot: dict | None = None
    # KRI-898: the app build that sent this message (`client_protocol` stamped on the turn at
    # submit); the choice gate asks about a freeze only when that build can play one.
    # ``None`` = unknown (web, an older stored turn, a test), so no freeze is planned.
    client_protocol: int | None = None
    # KRI-219 latency: the editor copilot served this in-place edit BEFORE the slow
    # Main Creator requirement extraction; the completed turn schedules that
    # extraction off the critical path (`extract_deferred_brief`).
    defer_brief: bool = False
    # KRI-142: what the server strategy check repaired or left out, so the
    # receipts reply still says it when it replaces the model's summary.
    policy_notices: tuple[str, ...] = ()
    # Server compiler evidence, never model-authored: exact text removed because
    # the selected renderer cannot draw that field. Runtime binds the same omission.
    policy_omitted_texts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    creative_copy_resolution: dict | None = None
    # KRI-709: the plan is a "still checking, come back later" reply, and the work behind
    # it can still progress (cached vision answers, clip analysis or song alignment in
    # flight). The runtime re-runs the same turn instead of asking the creator to say
    # "go ahead". Server-only: never reaches `KriaTurnPlan`, which forbids extra fields.
    continuable: bool = False
    prepared_collection_story: dict | None = None
    # KRI-881: research done before approval (`app.kria.research_preview`), server-owned.
    prepared_research: dict | None = None
    # The basis inputs research read that the persisted draft does not hold.
    prepared_research_terms: dict | None = None
    # KRI-1068: what the turn just withdrew ("skip that" about the clip the last reply was
    # stuck on), said by the draft or by whatever reply holds it back.
    pin_notices: tuple[str, ...] = ()


async def _withdraw_stuck_pins(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    prior: CreativeBrief | None,
    updates: tuple[BriefUpdate, ...],
    user_message: str,
) -> tuple[tuple[BriefUpdate, ...], tuple[str, ...]]:
    """KRI-1068: right after a reply stuck on a first / last clip, "skip that" drops it.

    The events are read only when the brief names such a clip at all.
    """
    from app.services.clip_order_sequence import stated_anchors  # noqa: PLC0415
    from app.services.pin_withdrawal import withdraw_stuck_pins  # noqa: PLC0415

    if prior is None or not stated_anchors(prior):
        return updates, ()
    withdrawal = withdraw_stuck_pins(
        prior, updates, await _load_thread_events(db, thread_id), user_message
    )
    if withdrawal.notices:
        log.info("kria_stuck_pin_withdrawn", thread_id=str(thread_id))
    return withdrawal.updates, withdrawal.notices


def _fresh_if_declared(prior, updates, outputs):  # noqa: ANN001, ANN202
    """KRI-816: an extraction batch typed `starts_fresh`, so the saved brief is withdrawn.

    The brief extractor expands its own declaration in parse; this covers the Main
    Creator's brief updates when no separate extraction ran.
    """
    if any(getattr(output, "starts_fresh", False) is True for output in outputs):
        return start_fresh_updates(prior, updates)
    return tuple(updates)


def _declares_delegation(outputs, updates) -> bool:  # noqa: ANN001
    """KRI-814: every extraction batch declared delegation and the turn changes nothing.

    Only the extraction step's typed field counts, never the strategy, the summary or the
    chat text, and an update (including a moment ask the server carried) always wins.
    """
    return (
        bool(outputs)
        and not updates
        and all(getattr(output, "delegates_all_choices", False) is True for output in outputs)
    )


async def _with_approved_copy(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    prior: CreativeBrief | None,
    updates: tuple[BriefUpdate, ...],
    states: Mapping[str, CreativeCopyState],
) -> tuple[BriefUpdate, ...]:
    """KRI-945: this turn's updates plus the approved opening wording as the title literal.

    An approval may fill only a title requirement stated no later than the creator's
    approving tap: its source turn's message comes at or before the tap in the thread.
    """
    state = states.get("opening_title")
    unbound: dict[uuid.UUID, list[str]] = {}
    for req in prior.live() if prior is not None else ():
        if req.kind == "text" and req.scope == "title" and req.literal is None:
            try:
                unbound.setdefault(uuid.UUID(str(req.source_turn_id)), []).append(req.id)
            except ValueError:
                continue
    answered: set[str] = set()
    if state is not None and state.approved and state.question_id and unbound:
        tapped = (
            await db.execute(
                select(func.max(CreationThreadEvent.sequence)).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role == "user",
                    CreationThreadEvent.payload["choice_selection"]["question_id"].astext
                    == state.question_id,
                )
            )
        ).scalar_one_or_none()
        stated = (
            await db.execute(
                select(CreatorAgentTurn.id, CreationThreadEvent.sequence)
                .join(
                    CreationThreadEvent, CreationThreadEvent.id == CreatorAgentTurn.source_event_id
                )
                .where(
                    CreatorAgentTurn.thread_id == thread_id,
                    CreatorAgentTurn.id.in_(list(unbound)),
                )
            )
        ).all()
        await db.rollback()  # no connection pinned across what follows
        if tapped is not None:
            answered = {
                req_id
                for turn_id, sequence in stated
                if sequence <= tapped
                for req_id in unbound.get(turn_id, ())
            }
    return bind_approved_copy(prior, updates, states, answered_ids=answered)


def brief_order_fields(
    resolved_intents: list[ResolvedClipIntent],
    brief: CreativeBrief | None,
    ordering_choice: str | None,
) -> tuple[list[ResolvedClipIntent], str | None]:
    """The server-owned order fields a strategy draft carries for the brief's order ask.

    29 Ekim: a random-order ask rides the strategy as its own choice; the seed itself stays
    on the requirement so every re-run draws the same permutation. A creator's answer to an
    order question (``ordering_choice``) is never replaced. Under that ask, or a filming-order
    one, a described group with no stated inner order takes it as its inner order
    (`confirm_unordered_groups`), so the draft never asks the creator to order a group they
    asked to have shuffled or kept in filming order.
    """
    from app.services.order_mode import confirm_unordered_groups  # noqa: PLC0415

    if ordering_choice is None and brief is not None:
        from app.pipeline.unified_montage import brief_view  # noqa: PLC0415

        if brief_view(brief).random_order_seed is not None:
            ordering_choice = "random"
    return confirm_unordered_groups(resolved_intents, brief), ordering_choice


def adapt_creator_action(
    action: AskUser | ProposeStrategy | ReviewDecision | DraftVoiceoverScript,
    *,
    server_clip_intents: list[ClipIntent] | None = None,
    server_resolved_clip_intents: list[ResolvedClipIntent] | None = None,
    server_resolved_song_takes: list[dict[str, Any]] | None = None,
    server_song_captions: str | None = None,
    ordering_choice: str | None = None,
    server_rejected_selections: Sequence[RejectedClipSelection] = (),
    server_checked_intents: Sequence[ClipIntent] = (),
    server_story_refs: StoryIntentRefs | None = None,
    server_order_replacement: OrderReplacementProof | None = None,
) -> KriaTurnPlan:
    """``server_resolved_song_takes`` (KRI-374) is the ONLY way ``resolved_song_takes``
    survives into the draft: it is server-owned (the song-order gate writes it after the
    creator answered), so whatever a model-authored strategy carried is discarded here.
    ``server_song_captions`` (KRI-578) is the same for the lyric-caption decision."""
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
    if isinstance(action, DraftVoiceoverScript):
        # KRI-815: planned by `_plan_voiceover_script`; never a strategy or a draft.
        return voiceover_script.record_first_plan()
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
    reconciled_story = list(action.strategy.story_structure)
    selection_reconciliation = None
    if server_rejected_selections:
        result = reconcile_story_intents(
            reconciled_story,
            refs=server_story_refs,
            checked_intents=server_checked_intents,
            rejected_intents=[selection.intent for selection in server_rejected_selections],
        )
        unresolved = result.status == "unresolved"
        selection_reconciliation = SelectionReconciliation(
            rejected_selections=tuple(server_rejected_selections),
            status="unresolved" if unresolved else "reconciled",
            unresolved_story_rows=tuple(reconciled_story) if unresolved else (),
            story_refs=result.refs,
        )
        reconciled_story = [] if unresolved else list(result.rows)
        omitted = ", ".join(selection.intent.attribute for selection in server_rejected_selections)
        summary = say(
            en=f"I left out {omitted} based on your clip answer.",
            tr=f"Klip yanıtına göre {omitted} bölümünü çıkardım.",
        )
        if unresolved:
            summary += " " + say(
                en="The earlier story outline is saved for review and is not part of this plan.",
                tr="Önceki hikâye taslağı inceleme için saklandı ve bu planın parçası değil.",
            )
    strategy_update = {
        "story_structure": reconciled_story,
        "selection_reconciliation": selection_reconciliation,
        "confirmed_order_replacement": server_order_replacement,
        **({"rationale": summary} if selection_reconciliation else {}),
        "clip_intents": requested_intents or None,
        "resolved_clip_intents": server_resolved_clip_intents or None,
        "resolved_song_takes": server_resolved_song_takes or None,
        # KRI-578: the plan-time lyric-caption decision; a model-authored value is dropped.
        "song_captions": server_song_captions,
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
        model_text(getattr(row, "payload", None), row.content).strip()
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
    # KRI-552: a re-sent copy of a message adds no instruction; read twice it doubled every ask.
    rows = [row for row in rows if not is_duplicate_answer(getattr(row, "payload", None))]
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


def continuation_superseded_reply() -> str:
    """KRI-709: a turn the runtime would have re-run, closed because the creator already
    sent a newer message. Asks for nothing: that message is the next turn."""
    return say(
        en="I'm still checking some of your clips. I'll carry on with your new message.",
        tr="Kliplerinden bazılarını hâlâ kontrol ediyorum. Yeni mesajınla devam edeceğim.",
    )


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
    reply_options: Sequence[str] = (),
    clip_intent_inventory: dict[str, Any] | None = None,
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
            # KRI-950: the checked list behind the picker, reused when the tap is all that comes.
            clip_intent_inventory=clip_intent_inventory if clip_question else None,
            # KRI-804: tappable answers, e.g. "Keep ... exactly as written" for a label.
            reply_options=list(reply_options) or None,
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


def _is_voiceover_strategy(strategy: Any) -> bool:
    """A narrated edit spined by the creator's recorded voiceover (KRI-692)."""
    return (
        getattr(strategy, "edit_format", None) in NARRATED_EDIT_FORMATS
        and getattr(strategy, "audio_strategy", None) == "voiceover"
    )


def _voiceover_tie_shots(
    planned: Any, strategy: Any, clips: Sequence[Any], thread_id: Any
) -> list[Any]:
    """KRI-962: the Voiceover shots to ask about (``clip_ties``); [] when off, on another
    format, or when the check itself fails (it never costs the turn)."""
    if not (
        settings.kria_voiceover_tie_questions_enabled
        and settings.kria_clip_selection_questions_enabled
        and _is_voiceover_strategy(strategy)
    ):
        return []
    try:
        return voiceover_ties(planned.resolution.intents, clips)
    except Exception as exc:  # noqa: BLE001 -- a tie check never costs the turn
        log.warning(
            "kria_voiceover_tie_check_failed",
            thread_id=str(thread_id),
            error_type=type(exc).__name__,
        )
        return []


def _narrated_renders_once_recorded(manifest: Any, strategy: Any) -> bool:
    """KRI-815: a narrated voiceover edit would render here once a voiceover is attached.

    Server rollout state only (never this item's media): the narrated format is on, and on an
    iPhone project its phone compiler is rolled out (`phone_format_pending_voiceover:*`)."""
    if not settings.narrated_archetype_enabled:
        return False
    pending = manifest.capabilities.get(
        f"phone_format_pending_voiceover:{getattr(strategy, 'edit_format', '')}"
    )
    return pending is None or pending.available


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
    # KRI-709: `plan` is the alignment-still-running reply, which the runtime may re-run.
    pending: bool = False


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


async def _load_kept_labels(db: AsyncSession, item_id: uuid.UUID) -> list[KeptLabel]:
    """Labels the item's current draft already placed on clips (KRI-766), then release the txn."""
    snapshot = (
        await db.execute(
            select(CreatorEditDraft.snapshot_json)
            .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True))
            .order_by(CreatorEditDraft.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    await db.rollback()  # no connection pinned across the model calls that follow
    strategy = snapshot.get("strategy") if isinstance(snapshot, dict) else None
    return kept_labels_from_strategy(strategy)


async def _load_draft_strategy(db: AsyncSession, item_id: uuid.UUID) -> dict | None:
    """The item's current draft strategy (KRI-942), then release the txn."""
    snapshot = (
        await db.execute(
            select(CreatorEditDraft.snapshot_json)
            .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True))
            .order_by(CreatorEditDraft.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    await db.rollback()  # no connection pinned across the model calls that follow
    strategy = snapshot.get("strategy") if isinstance(snapshot, dict) else None
    return strategy if isinstance(strategy, dict) else None


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
                *(
                    ()
                    if (through := answering_through_sequence.get()) is None
                    else (
                        or_(
                            CreationThreadEvent.role != "user",
                            CreationThreadEvent.sequence <= through,
                        ),
                    )
                ),
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


# KRI-950: answers that can change what the creator asked for; a tap riding with one re-plans.
_OTHER_ANSWER_KEYS = frozenset(
    {
        "choice_selection",
        "protected_region",
        "moment_order",
        SONG_ORDER_ANSWER_KEY,
        SONG_RANGE_ANSWER_KEY,
    }
)
# KRI-1125: every payload key that makes a creator event a tap or card answer, not a typed line.
_TAP_ANSWER_KEYS = frozenset({"clip_selection", *_OTHER_ANSWER_KEYS})


def _picker_answer_turn_id(events: Sequence[tuple[str, Any]]) -> str | None:
    """The turn whose clip picker the latest creator event answers, when it ONLY answers it.

    The latest creator event must be a ``clip_selection`` (a tap or "Skip") with no other
    answer, for the picker in the reply just before it; events the server wrote on its own
    after the tap (a finished render, a review) are not the creator's. A typed line after that
    reply means new words, so this returns None and the turn re-plans (KRI-950)."""
    latest = next((i for i in range(len(events) - 1, -1, -1) if events[i][0] == "user"), None)
    if latest is None:
        return None
    payload = events[latest][1] if isinstance(events[latest][1], dict) else {}
    selection = payload.get("clip_selection")
    if not isinstance(selection, dict) or _OTHER_ANSWER_KEYS & payload.keys():
        return None
    for role, earlier in reversed(events[:latest]):
        if role == "user":
            return None
        question = earlier.get("clip_question") if isinstance(earlier, dict) else None
        if isinstance(question, dict):
            if question.get("question_id") != selection.get("question_id"):
                return None
            turn_id = earlier.get("turn_id")
            return turn_id if isinstance(turn_id, str) else None
    return None


async def _load_checked_inventory(db: AsyncSession, turn_id: str) -> Any:
    """KRI-950: the clip-intent list the asking turn kept (``clip_intent_inventory``), or None."""
    try:
        plan = (
            await db.execute(
                select(CreatorAgentTurn.plan_json).where(CreatorAgentTurn.id == uuid.UUID(turn_id))
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - a lost list only means a re-plan, as before
        log.warning("kria_checked_inventory_unreadable", error_type=type(exc).__name__)
        return None
    finally:
        await db.rollback()  # no connection pinned across the model calls that follow
    return plan.get("clip_intent_inventory") if isinstance(plan, dict) else None


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
        song_range = song_range_of(item)
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
            return _SongGateResult(plan=_song_pending_plan(), pending=True)
        await asyncio.sleep(min(_SONG_ALIGNMENT_POLL_S, remaining))

    # KRI-580: the creator said which stretch of the song they filmed in; a take is only matched
    # there (a repeated verse elsewhere must not split the group).
    durations = _song_take_durations(manifest, take_ids)
    alignment = restrict_alignment_to_range(alignment, song_range, durations=durations)

    if kept_strategy is not None and events is not None:
        # Only keep lip-sync for an exchange about THIS song generation.
        if not thread_keeps_lipsync(events, song_generation):
            return _SongGateResult()

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
    if (
        song_range is None
        and not answered
        and settings.song_filming_range_enabled
        and song_generation not in fold_song_range_events(events).asked_generations
        and should_ask_song_range(alignment, take_ids)
    ):
        # KRI-588/650: the song repeats its verses and a clip fits more than one repeat, or
        # several clips could not be placed with confidence. The creator knows which part of
        # the song they filmed in: ask ONCE per song, before any order question, and match the
        # clips only inside the answer. Skippable ("the whole song"), and never asked again
        # whether it was answered, skipped or ignored (a client that cannot answer must not
        # loop).
        range_question = build_song_range_question(song_generation, song_duration_s)
        return _SongGateResult(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response=song_range_question_text(range_question),
                song_range_question=range_question,
            )
        )
    if answered and folded.arranged:
        # KRI-561: the creator arranged the takes on the song timeline. Their layout stands
        # even when a re-alignment since made every take look confident, so this runs
        # BEFORE the "nothing to ask" exit below.
        arranged = await asyncio.to_thread(
            resolve_creator_placements,
            alignment,
            folded.ordered_media_ids,
            folded.placements,
            durations,
            song_duration_s,
        )
        return _SongGateResult(
            resolved_takes=resolved_song_takes_payload(arranged), strategy=kept_strategy
        )
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
            first_line_s=first_line_s,
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
    *,
    reply: str,
    ops: list[dict],
    request_render: bool = False,
    unmet_requests: Sequence[Mapping[str, str]] = (),
    notes: str | None = None,
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
            "arguments": {
                "operations": ops,
                "summary": reply,
                **(
                    {
                        "unmet_requests": [
                            {
                                "request": str(item.get("request") or "")[:160],
                                "reason": str(item.get("reason") or "")[:200],
                            }
                            for item in list(unmet_requests)[:6]
                        ]
                    }
                    if unmet_requests
                    else {}
                ),
                **({"notes": notes.strip()[:600]} if notes and notes.strip() else {}),
            },
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
    recovery_basis: EditorDraftBasis | None = None


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
        visible_text = clip_visible_text_by_media_id(
            job, variant, list(item.clip_assignments or [])
        )
        if visible_text:
            context["visible_text"] = visible_text
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
        catalog = (
            await _sfx_catalog_rows(db, device=variant.get("render_destination") == "device")
            if settings.sound_effects_enabled
            else []
        )
        if catalog:
            context["sfx_catalog"] = catalog
    except Exception:  # noqa: BLE001 - fail closed: empty catalog => no add_sfx
        log.warning("kria_copilot_sfx_catalog_unavailable", thread_id=str(thread_id), exc_info=True)
    try:
        # KRI-1117: the photos and videos a Talking edit's background can be. No query while
        # the lane is closed for this variant, so every other turn reads exactly what it read.
        if background_state(job, variant)[0]:
            pool = await _background_pool_rows(db, item)
            if pool:
                context["background_pool"] = pool
    except Exception:  # noqa: BLE001 - fail closed: empty pool => no set_background
        log.warning(
            "kria_copilot_background_pool_unavailable", thread_id=str(thread_id), exc_info=True
        )
    return context


async def _background_pool_rows(db: AsyncSession, item: PlanItem) -> list[dict]:
    """The item's own ready photos and videos for ``set_background`` (one query, savepoint).

    Only THIS item's pool (S19): the id the model copies is the pool row id the Save pins.
    The speaker's own clips are not Visuals and never appear here.
    """
    from app.models import PlanItemAsset  # noqa: PLC0415

    owner = select(ContentPlan.user_id).where(ContentPlan.id == item.content_plan_id)
    async with db.begin_nested():
        rows = (
            (
                await db.execute(
                    select(PlanItemAsset)
                    .where(
                        PlanItemAsset.plan_item_id == item.id,
                        PlanItemAsset.user_id == owner.scalar_subquery(),
                        PlanItemAsset.status == "ready",
                        PlanItemAsset.kind.in_(("image", "video")),
                        PlanItemAsset.deduplicated_to_asset_id.is_(None),
                    )
                    .order_by(PlanItemAsset.created_at, PlanItemAsset.id)
                    .limit(MAX_BACKGROUND_POOL_ROWS)
                )
            )
            .scalars()
            .all()
        )
    pool: list[dict] = []
    for row in rows:
        entry: dict[str, Any] = {"id": str(row.id), "kind": row.kind}
        label = " ".join(str(row.source_filename or "").split())[:60]
        if label:
            entry["label"] = label
        if row.kind == "video" and isinstance(row.duration_s, (int, float)):
            entry["duration_s"] = round(float(row.duration_s), 2)
        context_note = " ".join(str(getattr(row, "user_context", None) or "").split())[:80]
        if context_note:
            entry["context"] = context_note
        pool.append(entry)
    return pool


SFX_CATALOG_LIMIT = 40


async def _sfx_catalog_rows(db: AsyncSession, *, device: bool = False) -> list[dict]:
    """Public sound-effects catalog for the copilot's add_sfx (one query).

    Same publish filter as GET /sound-effects. Runs in a savepoint so a DB error
    cannot poison the outer read transaction.

    ``device`` (KRI-608): the edit renders on the iPhone, which plays only m4a/wav/mp3/aac
    (`PLAYABLE_SFX_EXTENSIONS`); offering any other effect would end in a refused Save.
    """
    from app.models import SoundEffect  # noqa: PLC0415

    query = (
        select(SoundEffect)
        .where(SoundEffect.published_at.isnot(None))
        .where(SoundEffect.archived_at.is_(None))
        .where(SoundEffect.status == "ready")
        .where(SoundEffect.audio_gcs_path.isnot(None))
    )
    if device:
        from sqlalchemy import or_  # noqa: PLC0415

        from app.pipeline.phone_subtitled_lanes import PLAYABLE_SFX_EXTENSIONS  # noqa: PLC0415

        query = query.where(
            or_(*(SoundEffect.audio_gcs_path.ilike(f"%{ext}") for ext in PLAYABLE_SFX_EXTENSIONS))
        )
    async with db.begin_nested():
        rows = (
            (
                await db.execute(
                    query.order_by(
                        SoundEffect.catalog_rank.asc().nulls_last(),
                        SoundEffect.created_at.desc(),
                    ).limit(SFX_CATALOG_LIMIT)
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
        "failed_draft_unavailable",
        "render_outcome_unknown",
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
# KRI-1037: set by the turn task when the thread's last turn ended at a provider deadline, so
# the Main Creator starts on its lighter rung instead of repeating the call that timed out.
start_degraded_retry: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kria_start_degraded_retry", default=False
)
# Sequence of the creator message the running turn answers (set by the turn task). A later
# message is a queued follow-up with its own turn, so the history this turn plans from stops
# here. None = no bound.
answering_through_sequence: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "kria_answering_through_sequence", default=None
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
    head = (
        (
            await db.execute(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.item_id == item_id,
                    CreatorEditDraft.variant_key == session.target_variant_id,
                    CreatorEditDraft.base_job_id == job.id,
                    CreatorEditDraft.is_head.is_(True),
                )
            )
        ).scalar_one_or_none()
        if target_row is not None
        else None
    )
    recovery_basis = None
    if (
        compose_unmet_with_ops()
        and target_row
        and target_row.get("render_status") in {"failed", "needs_attention"}
    ):
        if (
            await db.execute(unknown_editor_render_query(session, job, target_row))
        ).scalar_one_or_none() is not None:
            _miss("render_outcome_unknown", session_id=str(session.id))
            return None
        recovery_basis = failed_editor_draft_basis(
            head,
            job,
            target_row,
            thread_id=thread_id,
            creator_id=getattr(thread, "creator_id", None),
            item_id=item_id,
        )
        if recovery_basis is None:
            _miss(
                "failed_draft_unavailable",
                session_id=str(session.id),
                target_variant_id=session.target_variant_id,
                variants_seen=[
                    {"variant_id": row.get("variant_id"), "render_status": row.get("render_status")}
                    for row in variants
                ],
            )
            return None
        if session.creator_id != thread.creator_id or session.plan_item_id != item_id:
            _miss("session_job_mismatch", session_id=str(session.id))
            return None
    variant = (
        target_row
        if target_row and (target_row.get("render_status") == "ready" or recovery_basis is not None)
        else None
    )
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
    # KRI-552: a re-sent copy of a message is not conversation.
    rows = [row for row in rows if not is_duplicate_answer(getattr(row, "payload", None))]
    rows.reverse()
    conversation = [
        {"role": row.role, "content": model_text(getattr(row, "payload", None), row.content)[:1000]}
        for row in rows
        if row.content
    ]
    return _EditorTarget(
        job_id=job.id,
        snapshot=snapshot,
        conversation=conversation,
        variant=variant,
        recovery_basis=recovery_basis,
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
    planner_deadline = turn_deadline.get()
    response = await run_copilot_turn(
        CopilotTurnBody(
            message=user_message,
            turns=target.conversation,
            original_request=original_request,
            snapshot=target.snapshot,
            client_contract_version=2,
        ),
        job_id=target.job_id,
        deadline_monotonic=(
            planner_deadline - _POST_UNDERSTANDING_RESERVE_S
            if planner_deadline is not None
            else None
        ),
        timeout_override_s=120.0,
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
    if target.recovery_basis is not None:
        draft_plan = adapt_editor_action(
            reply=_phone_editor_reply(response.reply), ops=response.ops or []
        )
        if getattr(response, "unmet_requests", None) or (
            response.ops and not _is_fast_path_plan(draft_plan)
        ):
            return KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=_failed_correction_alternative(),
            )
        if response.ops:
            plan = adapt_editor_action(
                reply=_phone_editor_reply(response.reply),
                ops=response.ops,
                notes=getattr(response, "reply_notes", None) or None,
            )
            return plan.model_copy(
                update={
                    "diagnostics": {
                        **(plan.diagnostics or {}),
                        "editor_recovery_basis": target.recovery_basis._asdict(),
                    }
                }
            )
    if response.ops:
        return adapt_editor_action(
            reply=_phone_editor_reply(response.reply),
            ops=response.ops,
            # This portable operation invokes server speech processing; ordinary
            # text/timeline/mix edits stay drafts until an explicit Save.
            request_render=any(op.get("op") == "apply_speech_cut_candidate" for op in response.ops),
            unmet_requests=getattr(response, "unmet_requests", None) or (),
            notes=getattr(response, "reply_notes", None) or None,
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
            # KRI-738: tappable lengths, only on a question and only those the reply states.
            reply_options=(
                [
                    option["label"]
                    for option in length_options(getattr(response, "length_options", None), reply)
                ]
                if response.outcome == "clarification"
                else None
            ),
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
    # KRI-814: with no brief version yet, the ids of the earlier creator lines the server fed
    # to this turn's brief reading (`render_unrecorded_request`). A delegation the reading
    # declares covers exactly these lines and the current message, nothing else.
    delegation_read_ids: tuple[str, ...] = ()


async def _unrecorded_creator_lines(
    db: AsyncSession, *, thread_id: uuid.UUID, user_message: str
) -> list[CreationThreadEvent]:
    """KRI-814: earlier creator lines (before the current message) in thread order."""
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
    rows = [
        row
        for row in rows
        if str(row.content).strip() and not is_duplicate_answer(getattr(row, "payload", None))
    ]
    # Same rule as `_full_creator_request`: the newest line is the current message.
    if rows and str(rows[-1].content).strip() == user_message.strip():
        rows.pop()
    return rows


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
    # KRI-552: a re-sent copy of a message, and the one-line answer it got, are not conversation.
    rows = [row for row in rows if not is_duplicate_answer(getattr(row, "payload", None))]
    rows.reverse()
    creator_summary, item_summary = creator_context(persona, item)
    extra: dict = {}
    batches = ()
    delegation_read_ids: tuple[str, ...] = ()
    if brief_on:
        extra["brief_enabled"] = True
        context = brief_context(prior_brief, latest_message=user_message, max_batches=3)
        batches = context.batches
        if batches:
            extra["creator_request"] = batches[0].text
        elif prior_brief is None:
            # KRI-814: no brief version means no earlier line was ever read into the brief
            # (a held or pre-brief first turn). Read them now with the latest message, so
            # their requirements are recorded and a delegation can speak for the whole chat.
            earlier = await _unrecorded_creator_lines(
                db, thread_id=thread_id, user_message=user_message
            )
            unrecorded = render_unrecorded_request(
                (model_text(getattr(row, "payload", None), row.content) for row in earlier),
                user_message,
            )
            if unrecorded is not None:
                extra["creator_request"] = unrecorded
                delegation_read_ids = tuple(str(row.id) for row in earlier)
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
            {
                "role": row.role,
                "content": model_text(getattr(row, "payload", None), row.content)[:1000],
            }
            for row in rows
            if row.content
        ],
        capability_manifest=manifest,
        creative_copy_state=compact_copy_state,
        # KRI-520: bound per Kria turn by the task; None leaves the prompt unchanged.
        reply_language=current_reply_language(),
        # KRI-815: footage attached and no voiceover (the claim's own predicate).
        voiceover_script_available=voiceover_script.available(
            manifest, voiceover_attached=bool(getattr(item, "voiceover_gcs_path", None))
        ),
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
        delegation_read_ids=delegation_read_ids,
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
        deadline = turn_deadline.get()
        return configure_creator_agent(MainCreatorAgent(default_client()), "main_creator").run(
            inputs.agent_input,
            ctx=RunContext(
                request_id=str(thread_id),
                creator_id=str(creator_id),
                plan_item_id=str(item_id) if item_id else None,
                creator_agent_session_id=(
                    str(creator_agent_session_id) if creator_agent_session_id else None
                ),
                deadline_monotonic=(
                    deadline - _POST_UNDERSTANDING_RESERVE_S if deadline is not None else None
                ),
                thought_summary_callback=current_thought_publisher(),
                start_degraded=start_degraded_retry.get(),
            ),
        )

    try:
        return await asyncio.to_thread(_run_agent)
    except (ProviderOutcomeUnknownError, ProviderQuotaExceededError, AiBudgetExceededError):
        # Provider work may still be billable, or a spend cap answers the same on every
        # retry. Never disguise either as a recoverable planning failure ("try again"):
        # the turn fails with its own code and copy.
        raise
    except TerminalError as exc:
        raise RuntimeError("Kria could not produce a reliable editorial plan") from exc


# Prompt fidelity (errors unit): the extractor is read-only, so ONE bounded retry after a
# provider/transport failure is safe. The agent runtime already spent its own attempts and
# fallback models, so a short pause gives a transient outage a chance to pass.
_EXTRACTOR_RETRY_BACKOFF_S = 1.0
_EXTRACTOR_RETRY_MIN_BUDGET_S = 20.0


def _error_honesty_enabled() -> bool:
    return bool(settings.kria_error_honesty_enabled)


def _is_provider_fault(exc: BaseException) -> bool:
    """True when an agent failed for provider/transport reasons, not because its answer was bad.

    ``TerminalSchemaError`` is the extractor producing output the schema rejects (a real
    misread: the creator is asked to rephrase). Any other wrapped ``TerminalError`` (retries
    exhausted on 5xx/429/timeouts, a truncated answer, a refusal) says nothing about the
    creator's words, so the honest reply is "try again", not "I couldn't read your request".
    """
    cause = exc.__cause__
    return isinstance(cause, TerminalError) and not isinstance(cause, TerminalSchemaError)


def _extractor_retry_allowed(exc: TerminalError) -> bool:
    if not _error_honesty_enabled():
        return False
    # Only an exhausted-transient failure; schema, refusal and fenced outcomes never retry.
    if type(exc) is not TerminalError or not isinstance(exc.__cause__, TransientError):
        return False
    deadline = turn_deadline.get()
    return (
        deadline is None
        or deadline - _POST_UNDERSTANDING_RESERVE_S - time.monotonic()
        >= _EXTRACTOR_RETRY_MIN_BUDGET_S
    )


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

    def _run_agent(request_id: str):  # noqa: ANN202 - inferred BriefExtractionOutput
        deadline = turn_deadline.get()
        return BriefExtractorAgent(default_client()).run(
            agent_input,
            ctx=RunContext(
                request_id=request_id,
                creator_id=str(creator_id),
                plan_item_id=str(item_id),
                creator_agent_session_id=(
                    str(creator_agent_session_id) if creator_agent_session_id else None
                ),
                deadline_monotonic=(
                    deadline - _POST_UNDERSTANDING_RESERVE_S if deadline is not None else None
                ),
            ),
        )

    try:
        return await asyncio.to_thread(_run_agent, str(thread_id))
    except (ProviderOutcomeUnknownError, ProviderQuotaExceededError, AiBudgetExceededError):
        # A spend cap is not a reading failure: the turn fails with its own code (A1).
        raise
    except TerminalError as exc:
        if not _extractor_retry_allowed(exc):
            raise RuntimeError("Kria could not extract the creative brief reliably") from exc
        log.warning("kria_extractor_retry", **_recovery_cause("followup_extraction", exc))
        await asyncio.sleep(_EXTRACTOR_RETRY_BACKOFF_S)
        try:
            # A distinct request id: the paid-call reservation key is stable per request id
            # and attempt number, so the retry must not collide with the failed run's.
            return await asyncio.to_thread(_run_agent, f"{thread_id}:extractor-retry")
        except (ProviderOutcomeUnknownError, ProviderQuotaExceededError, AiBudgetExceededError):
            raise
        except TerminalError as retry_exc:
            raise RuntimeError("Kria could not extract the creative brief reliably") from retry_exc


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
    brief_selects_clips: bool = False,
    clip_scope_texts: Sequence[str] = (),
    requested_media: Mapping[str, str] | None = None,
    describe_clips: bool = False,
    spoken_text: Sequence[str] = (),
    effective_brief: CreativeBrief | None = None,
    batch_questions: bool = False,
    keeps_sound: bool = False,
) -> PlannedKriaTurn:
    """Turn a Main Creator answer into an inert plan (clip-intent resolution incl.).

    KRI-802: ``batch_questions`` (the app answers a reply's cards together) holds a title
    wording question while the clip checks run, so a clip picker they end on is asked in
    the same reply instead of one full re-plan later.
    """
    if isinstance(output.action, DraftVoiceoverScript):
        return await _plan_voiceover_script(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=creator_id,
            user_message=user_message,
            manifest=manifest,
            inputs=inputs,
            action=output.action,
        )
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
    hold_wording = batch_questions and isinstance(output.action, ProposeStrategy)
    if creative_turn is not None and not hold_wording:
        return creative_turn
    held: list[PlannedKriaTurn] = [creative_turn] if creative_turn is not None else []
    # Persist validated replacements alongside whichever result the normal planner
    # produces, within the same revision-fenced completion transaction.
    try:
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
            brief_selects_clips=brief_selects_clips,
            clip_scope_texts=clip_scope_texts,
            requested_media=requested_media,
            describe_clips=describe_clips,
            spoken_text=spoken_text,
            effective_brief=effective_brief,
            keeps_sound=keeps_sound,
            **({"held_wording": held} if hold_wording else {}),
        )
    except Exception as exc:  # noqa: BLE001 - re-raised unless a question is held
        if not held:
            raise
        # KRI-802: planning on past a held question must never cost the creator that
        # question: the turn asks it, exactly as it did before batching.
        log.warning("kria_question_batch_failed_open", error_type=type(exc).__name__)
        with suppress(Exception):  # a failed statement must not poison the rest of the turn
            await db.rollback()
        planned = held[0]
    else:
        if held:
            planned = _with_wording_question(held[0], planned)
    return replace(planned, creative_copy_resolution=resolution)


def _creator_words(inputs: _CreatorInputs, user_message: str) -> list[str]:
    """The creator's own messages this turn saw, oldest first, the current one last."""
    earlier = [
        str(row.get("content") or "")
        for row in getattr(getattr(inputs, "agent_input", None), "conversation", [])
        if row.get("role") == "user"
    ]
    if earlier and earlier[-1].strip() == user_message.strip():
        earlier.pop()
    return [*earlier, user_message]


def _quoted_by_creator(evidence: str, words: Sequence[str]) -> bool:
    """Whitespace-insensitive verbatim check: the model's excerpt is the creator's own text."""
    excerpt = " ".join(evidence.split())
    return bool(excerpt) and any(excerpt in " ".join(text.split()) for text in words)


async def _item_has_voiceover(db: AsyncSession, item_id: uuid.UUID) -> bool:
    """The approval claim's voiceover predicate, read without pinning a connection."""
    attached = bool((await _refetch_item(db, item_id)).voiceover_gcs_path)
    await db.rollback()
    return attached


async def _plan_voiceover_script(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
    manifest: Any,
    inputs: _CreatorInputs,
    action: DraftVoiceoverScript | None,
) -> PlannedKriaTurn:
    """KRI-815: answer with a footage-grounded script (or the honest record/import ask).

    ``action`` is the Main Creator's script action, or None when a narrated strategy was
    proposed with no voiceover attached (the doomed-approval gate). Mints no approval, no
    draft and no Job, and writes nothing to the brief or the item.
    """
    words = _creator_words(inputs, user_message)
    hashes = {"manifest_hash": manifest.manifest_hash, "context_hash": manifest.context_hash}
    if action is not None and (
        not _quoted_by_creator(action.request_evidence, words)
        # A change to the draft must be asked for now, in the creator's own words.
        or (action.revision_note and not _quoted_by_creator(action.revision_note, [user_message]))
    ):
        # The model's reading is never the creator's request: ask instead of writing.
        log.info("kria_voiceover_script_ungrounded", thread_id=str(thread_id))
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response=say(
                    en="Do you want me to write a voiceover script from your clips?",
                    tr="Kliplerinden bir seslendirme metni yazmamı ister misin?",
                ),
            ),
            **hashes,
        )
    item = await _refetch_item(db, item_id)
    voiceover_attached = bool(item.voiceover_gcs_path)
    await db.rollback()
    if not voiceover_script.available(manifest, voiceover_attached=voiceover_attached):
        return PlannedKriaTurn(plan=voiceover_script.record_first_plan(), **hashes)
    prior = await voiceover_script.latest_script(db, thread_id)
    await db.rollback()
    if prior is not None and (
        action is None
        or (
            not action.revision_note
            and not _quoted_by_creator(action.request_evidence, [user_message])
        )
    ):
        # A draft exists and nothing new was asked: keep it (never silently re-draft).
        return PlannedKriaTurn(plan=voiceover_script.record_first_plan(), **hashes)
    clips = list(inputs.intent_clips or []) or await _reload_intent_clips(
        db, item_id=item_id, creator_id=creator_id
    )
    video_ids = {media.media_id for media in manifest.media if media.kind == "video"}
    pending = [
        clip.media_id
        for clip in clips
        if settings.kria_clip_understanding_enabled
        and clip.media_id in video_ids
        and understanding_incomplete(clip.analysis, kind=clip.kind)
    ]
    if pending:
        # A script written from blank clips would be generic: wait, and re-run the turn.
        await _kick_clip_understanding(item_id, clips)
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=_clips_still_checking_reply(len(pending)),
            ),
            continuable=True,
            **hashes,
        )
    deadline = turn_deadline.get()
    plan = await voiceover_script.draft_script(
        thread_id=thread_id,
        item_id=item_id,
        creator_id=creator_id,
        manifest=manifest,
        clips=clips,
        creator_words=words,
        prior=prior,
        revision_note=(action.revision_note if action is not None else None),
        deadline_monotonic=(deadline - 5.0) if deadline is not None else None,
    )
    return PlannedKriaTurn(plan=plan, **hashes)


def _with_wording_question(wording: PlannedKriaTurn, planned: PlannedKriaTurn) -> PlannedKriaTurn:
    """KRI-802: ask a held title-wording question in the same reply as a clip picker.

    Only a plain clip picker joins it: the app answers the two cards on one turn. Any other
    ending (a draft, a text question, another choice card, a refusal, a "still checking"
    reply, a picker about the title's own words, a reply too long to persist) leaves the
    wording question alone, exactly as before, so nothing is ever drafted with words the
    creator has not approved.
    """

    plan = planned.plan
    title = _pin_key(str((wording.plan.choice_question or {}).get("candidate") or ""))
    words = [
        _pin_key(str(row.get("creator_text") or ""))
        for category in (plan.clip_question or {}).get("categories") or []
        if isinstance(category, dict)
        for row in answered_rows(category)  # KRI-951: and the rows joined behind it
    ]
    if (
        plan.mode != "respond"
        or plan.turn_value != "question"
        or not plan.clip_question
        or plan.choice_question
        or plan.song_order_question is not None
        or plan.song_range_question is not None
        or plan.moment_order_question
        or plan.reply_options
        or planned.continuable
        or any(word and title and (word in title or title in word) for word in words)
    ):
        return wording
    closing = say(
        en="Answer both and I'll plan your edit.",
        tr="İkisini de yanıtla, düzenlemeni planlayayım.",
    )
    try:
        merged = KriaTurnPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "response": "\n\n".join(
                    [wording.plan.response or "", plan.response or "", closing]
                ),
                "choice_question": wording.plan.choice_question,
                "diagnostics": {**(plan.diagnostics or {}), "question_batch": "wording+clips"},
            }
        )
    except ValidationError:  # the joined reply is over the response limit
        return wording
    log.info("kria_questions_batched", batch="wording+clips")
    return replace(planned, plan=merged)


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


# KRI-591: number words a creator may type for a title hold, each paired only with its own
# language's seconds unit, so English "on second" is never Turkish ten seconds. Turkish both
# with and without diacritics ("üç"/"uc"): phone keyboards often drop them.
_EN_HOLD_WORDS = {
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
_TR_HOLD_WORDS = {
    "bir": 1.0,
    "iki": 2.0,
    "üç": 3.0,
    "uc": 3.0,
    "dört": 4.0,
    "dort": 4.0,
    "beş": 5.0,
    "bes": 5.0,
    "altı": 6.0,
    "alti": 6.0,
    "yedi": 7.0,
    "sekiz": 8.0,
    "dokuz": 9.0,
    "on": 10.0,
}
_HOLD_NUMBER_WORDS = {**_EN_HOLD_WORDS, **_TR_HOLD_WORDS}


def _words(table: dict[str, float]) -> str:
    return "|".join(sorted(table, key=len, reverse=True))


_TAIL_HALF = r"(?!\w)(?:\s+(?P<tail>and\s+a\s+half)(?!\w))?"
# A seconds expression: a digit number (".5" too) may touch any unit ("2s", ".5s"); a spelled
# number needs a space or hyphen before its own language's unit, so "ones" is not "one s".
# An optional "and a half" / "buçuk" before the unit and a trailing "and a half" add 0.5.
_HOLD_SECONDS = (
    re.compile(
        r"(?<![\w.:])(?P<num>\d+(?:[.,]\d+)?|[.,]\d+)"
        r"(?:\s+(?P<half>and\s+a\s+half|bu[çc]uk))?\s*-?\s*"
        r"(?:s|sn|secs?|seconds?|saniye\w*|segs?|segundos?|sekunden?|secondes?|second[io])"
        + _TAIL_HALF
    ),
    re.compile(
        rf"(?<![\w.:])(?P<num>{_words(_EN_HOLD_WORDS)})(?:\s+(?P<half>and\s+a\s+half))?"
        r"(?:\s+|\s*-\s*)(?:secs?|seconds?)" + _TAIL_HALF
    ),
    re.compile(
        rf"(?<![\w.:])(?P<num>{_words(_TR_HOLD_WORDS)})(?:\s+(?P<half>bu[çc]uk))?"
        r"\s+(?:saniye\w*|sn)(?!\w)(?P<tail>)"
    ),
)
# Half-second phrases with no leading number.
_HOLD_HALF_PHRASES = (
    (re.compile(r"(?<!\w)(?:a\s+)?(?:second|sec)\s+and\s+a\s+half(?!\w)"), 1.5),
    (
        re.compile(
            r"(?<!\w)(?:half\s+a\s+(?:second|sec)|a\s+half[\s-]+second|yar[ıi]m\s+(?:saniye\w*|sn))"
            r"(?!\w)"
        ),
        0.5,
    ),
)


def _stated_holds(text: str) -> list[tuple[int, int, float]]:
    """Every seconds expression in ``text`` as (start, end, seconds)."""

    holds = []
    for match in (m for pattern in _HOLD_SECONDS for m in pattern.finditer(text)):
        value = _HOLD_NUMBER_WORDS.get(match["num"])
        if value is None:
            value = float(match["num"].replace(",", "."))
        if match["half"] or match["tail"]:
            value += 0.5
        holds.append((match.start(), match.end(), value))
    for pattern, value in _HOLD_HALF_PHRASES:
        holds.extend((match.start(), match.end(), value) for match in pattern.finditer(text))
    return holds


def _hold_key(text: str) -> str:
    """`_pin_key` without the combining dot a Turkish capital "İ" casefolds into, so a
    creator typing "SANİYE" still says "saniye"."""
    return _pin_key(text).replace("\N{COMBINING DOT ABOVE}", "")


def _copy_span(text: str, key: str) -> tuple[int, int] | None:
    """Where the on-screen copy itself sits in the creator's words: its quoted occurrence,
    else its first one. Any other occurrence is the creator speaking, not the copy
    ('Keep the title "3 seconds" for 3 seconds')."""
    spans = []
    start = text.find(key)
    while start != -1:
        spans.append((start, start + len(key)))
        start = text.find(key, start + 1)
    quoted = [
        (lo, hi)
        for lo, hi in spans
        if text[:lo].rstrip()[-1:] in ("'", '"') and text[hi:].lstrip()[:1] in ("'", '"')
    ]
    return (quoted or spans or [None])[0]


def title_hold_grounded(
    hold_s: float,
    *,
    evidence: str | None,
    user_sources: Sequence[str],
    strip: Sequence[str | None] = (),
) -> bool:
    """KRI-591: whether the creator's own words state this title hold.

    The model must quote the hold (`render_intent_evidence.opening_title_duration_s`): the
    excerpt has to be the creator's own words (a verbatim piece of one of their messages,
    compared like pinned text) and say this number of seconds ("2 saniye", "2,5 sn",
    "for two seconds", "3-second", "iki buçuk saniye", "a second and a half", ".5 s").
    Seconds inside the on-screen copy's own occurrence
    (``strip``) never count, so "3 SECONDS TO MIDNIGHT" cannot ground a 3 s hold, while
    seconds said outside it do: 'title "3" for 3 seconds' and 'Keep the title "3 seconds"
    for 3 seconds' both keep the stated hold. A bare count ("7 klip") is not seconds.
    """

    quoted = _pin_key(evidence or "")
    if not quoted or not any(quoted in _pin_key(source) for source in user_sources if source):
        return False
    text = _hold_key(quoted)
    copy_spans = [
        span
        for on_screen in strip
        if (key := _hold_key(on_screen or "")) and (span := _copy_span(text, key)) is not None
    ]
    for start, end, value in _stated_holds(text):
        if any(lo <= start and end <= hi for lo, hi in copy_spans):
            continue
        if abs(value - hold_s) < 1e-6:
            return True
    return False


async def _change_request_turn(db: AsyncSession, thread_id: uuid.UUID) -> PlannedKriaTurn | None:
    """KRI-983: "Change my request" on a held draft. Nothing was made, so the reply asks what
    to change about the asks that held it, with no model call; re-planning here only made the
    same plan and asked the same question again."""
    if not settings.kria_choice_questions_enabled:
        return None
    question = change_request_selected(await _load_thread_events(db, thread_id, release=False))
    if question is None:
        return None
    held = {str(req_id) for req_id in question.get(SIMPLER_OMITTED_IDS_KEY) or ()}
    brief = await load_latest_brief(db, thread_id) if held else None
    listed = "; ".join(req.text() for req in brief.live() if req.id in held) if brief else ""
    response = (
        say(
            en=f"Nothing is made yet. Tell me what to do differently for: {listed}.",
            tr=f"Henüz bir şey yapılmadı. Şunlar için neyi farklı yapmamı istersin: {listed}.",
        )
        if listed
        else say(
            en="Nothing is made yet. Tell me what to do differently.",
            tr="Henüz bir şey yapılmadı. Neyi farklı yapmamı istediğini söyle.",
        )
    )
    log.info("kria_change_request_selected", thread_id=str(thread_id))
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response=response),
        manifest_hash=None,
        context_hash=None,
    )


async def _simpler_version_accepted(
    db: AsyncSession, thread_id: uuid.UUID, fields: Sequence[str]
) -> bool:
    """KRI-759: this turn answers a policy refusal's "simpler version?" with yes."""
    if not fields or not settings.kria_choice_questions_enabled:
        return False
    if not isinstance(db, AsyncSession):
        return False  # no live session (a planner harness), so no thread to read an answer from
    return simpler_version_accepted(await _load_thread_events(db, thread_id), fields)


async def _plan_turn_clip_intents(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
    inputs: _CreatorInputs,
    brief_request: str | None,
    effective_brief: CreativeBrief | None,
    wants_capture_order: bool,
    describe_clips: bool,
    spoken_text: Sequence[str],
    strategy: CreativeStrategy | None,
) -> tuple[PlannedIntentResolution, list[tuple[str, Any]]]:
    """The clip-intent planner, resolver and vision checks over the creator's words.

    ``strategy`` is the Main Creator's draft (its clip intents are the planner's candidates),
    or None for a "missing" ask (Leverkusen M2), checked against the creator's words alone.
    Returns the result and the thread's events. Raises whatever the planning raises: no
    provider failure may mint a draft, and each caller answers it (`_clip_planning_failed`).
    """
    intent_clips = inputs.intent_clips
    creator_request = brief_request or inputs.creator_request
    shot_labels = getattr(strategy, "shot_labels", None) or ()
    clip_selections = None
    thread_events: list[tuple[str, Any]] = []
    if settings.kria_clip_selection_questions_enabled or settings.kria_choice_questions_enabled:
        thread_events = await _load_thread_events(db, thread_id)
    checked_inventory = None
    if settings.kria_clip_selection_questions_enabled:
        clip_selections = fold_clip_selections(thread_events)
        # KRI-950: a tap is an answer, not a new request: reuse the list the picker's turn
        # checked rather than ask the planner model again for the same words.
        asking_turn = _picker_answer_turn_id(thread_events)
        if asking_turn is not None:
            checked_inventory = await _load_checked_inventory(db, asking_turn)
    # KRI-766: the texts the creator dictated for a described shot, which this turn's
    # inventory may not repeat, and the labels the draft already placed.
    brief_labels = brief_label_asks(effective_brief, shot_labels, spoken_text)
    # KRI-804: a creator text a label can't print may become the shot's caption only
    # when that turns off no other text (positional shot labels, every-clip texts).
    shot_captions = shot_captions_safe(effective_brief, shot_labels)
    kept_labels = await _load_kept_labels(db, item_id) if effective_brief is not None else []
    whole_video_texts = whole_video_text_asks(effective_brief)
    sequence_texts = sequence_text_literals(effective_brief)
    # KRI-1067: a filming-order brief's first / last clip the inventory may leave out.
    brief_anchors = stated_anchors(effective_brief) if wants_capture_order else {}
    await _kick_clip_understanding(item_id, intent_clips)
    # KRI-1125: one photo is never silently the answer for two ordered beats, and a typed
    # correction newer than the tap that caused it sends the beat back to the resolver.
    planned = await plan_without_pick_collisions(
        plan_and_resolve_clip_intents,
        thread_events=thread_events,
        answer_keys=_TAP_ANSWER_KEYS,
        # KRI-522: the creator's own words (verified quote source), never
        # only the brief paraphrase; the brief rides along as recall aid.
        creator_request=inputs.raw_creator_request or creator_request or user_message,
        latest_user_message=user_message,
        generated_brief=brief_request,
        candidate_intents=strategy.clip_intents if strategy is not None else None,
        confirmed_titles=[
            ConfirmedTitleCopy(target=target, text=state.approved)
            for target, state in (inputs.creative_copy_state or {}).items()
            if state.approved is not None
        ],
        clips=intent_clips,
        # KRI-1076: the plan item makes the planner's and resolver's agent_run rows
        # (their input and answer) land, so a missed match can be read afterwards.
        run_context=RunContext(
            request_id=str(thread_id),
            creator_id=str(creator_id),
            plan_item_id=str(item_id),
        ),
        # KRI-282: chat clips have no background vision lane, so give the
        # turn itself a larger (cached, converging) foreground budget.
        max_vision_requeries=settings.kria_clip_intents_max_vision_requeries,
        vision_deadline_s=settings.kria_clip_intents_vision_deadline_s,
        # KRI-282 L2: hold the turn while background clip analysis is incomplete,
        # re-reading it (bounded) instead of trusting the pre-Main-Creator snapshot.
        require_clip_understanding=settings.kria_clip_understanding_enabled,
        refresh_clips=lambda: _reload_intent_clips(db, item_id=item_id, creator_id=creator_id),
        understanding_wait_until=_clip_understanding_wait_until(),
        **({"clip_selections": clip_selections} if clip_selections else {}),
        # KRI-612: "explain the footages" puts a line from the footage on each clip.
        **({"describe_clips": True} if describe_clips else {}),
        **({"brief_labels": brief_labels} if brief_labels else {}),
        **({"kept_labels": kept_labels} if kept_labels else {}),
        **({} if shot_captions else {"shot_captions": False}),
        # KRI-945: a caption for the brief's researched text or title is never asked.
        **({"whole_video_texts": whole_video_texts} if whole_video_texts else {}),
        # 29 Ekim: the brief's word-by-word text is the text lane's, never a caption.
        **({"sequence_texts": sequence_texts} if sequence_texts else {}),
        **({"checked_inventory": checked_inventory} if checked_inventory else {}),
        **({"brief_anchors": brief_anchors} if brief_anchors else {}),
        # KRI-692: a Voiceover edit draws no text on a spoken word, so such an ask is
        # never a clip question; its receipt tells the creator.
        **(
            {"spoken_text": spoken_text} if spoken_text and _is_voiceover_strategy(strategy) else {}
        ),
    )
    return planned, thread_events


def _clip_planning_failed(
    manifest: Any, exc: BaseException, *, clips: Sequence[IntentClip], thread_id: uuid.UUID
) -> PlannedKriaTurn:
    log.warning(
        "kria_clip_intent_planning_failed",
        thread_id=str(thread_id),
        error_type=type(exc).__name__,
        clips=len(clips),
    )
    return PlannedKriaTurn(
        plan=_clip_intent_resolution_plan(
            question=None,
            status="provider_unavailable",
            diagnostics={
                "stage": "planning",
                "reason": "planner_exception",
                "error_type": type(exc).__name__,
                "clips": len(clips),
            },
        ),
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
    )


async def _persist_turn_vision_answers(
    db: AsyncSession,
    *,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    thread_id: uuid.UUID,
    manifest: Any,
    planned: PlannedIntentResolution,
) -> PlannedKriaTurn | None:
    """Cache this turn's vision answers on the clips; the turn's reply when that fails."""
    if not planned.resolution.vision_answers:
        return None
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
    return None


async def _look_inside_clips_first(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    user_message: str,
    manifest: Any,
    inputs: _CreatorInputs,
    brief_request: str | None,
    effective_brief: CreativeBrief | None,
    wants_capture_order: bool,
    describe_clips: bool,
    spoken_text: Sequence[str],
) -> PlannedKriaTurn | None:
    """Leverkusen M2: a "that shot is missing" ask on an item with clips waits for a look.

    The Main Creator reads two moments per clip, so a shot 1.5 s inside a long take was
    "missing" or not by its coin flip. Here the creator's words go through the same planner,
    resolver and vision checks a strategy turn runs, and the reply follows what they found
    (`app.kria.missing_media`): the clip picker or a still-checking reply while a clip is
    unsure or unchecked, the clips each shot was found in, or a question naming the clips.
    None keeps the Main Creator's ask: nothing to look inside, or every clip was checked (the
    creator said the shot is in none of them, or already answered the found-it card).
    """
    clips = inputs.intent_clips
    if (
        not settings.clip_intents_enabled
        or not clips
        # A Talking / Subtitled edit plans no clip intents (see the strategy path).
        or getattr(manifest, "edit_format", None) in CLIP_INTENT_FREE_EDIT_FORMATS
    ):
        return None
    try:
        planned, thread_events = await _plan_turn_clip_intents(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=creator_id,
            user_message=user_message,
            inputs=inputs,
            brief_request=brief_request,
            effective_brief=effective_brief,
            wants_capture_order=wants_capture_order,
            describe_clips=describe_clips,
            spoken_text=spoken_text,
            strategy=None,
        )
    except Exception as exc:  # noqa: BLE001 - a failed look is never "upload it"
        return _clip_planning_failed(manifest, exc, clips=clips, thread_id=thread_id)
    unsaved = await _persist_turn_vision_answers(
        db,
        item_id=item_id,
        creator_id=creator_id,
        thread_id=thread_id,
        manifest=manifest,
        planned=planned,
    )
    if unsaved is not None:
        return unsaved
    resolution = planned.resolution
    if resolution.status != "resolved" or resolution.needs_creator or resolution.deferred_queries:
        log.info(
            "kria_missing_media_checked",
            thread_id=str(thread_id),
            outcome=resolution.status,
            clips=len(clips),
        )
        return PlannedKriaTurn(
            plan=_clip_intent_resolution_plan(
                question=resolution.question,
                status="needs_creator" if resolution.needs_creator else resolution.status,
                diagnostics=_resolution_diagnostics(resolution),
                clip_question=resolution.clip_question if resolution.needs_creator else None,
                clip_intent_inventory=planned.inventory,
                reply_options=resolution.reply_options if resolution.needs_creator else (),
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
            # KRI-709: only a budget/deadline stop continues.
            continuable=resolution.status == "pending" and not resolution.needs_creator,
        )
    looked = after_looking(resolution.intents, planned.rejected_selections)
    if looked.outcome == FOUND and answers_found_card(thread_events):
        looked = replace(looked, outcome=CHECKED)
    log.info(
        "kria_missing_media_checked",
        thread_id=str(thread_id),
        outcome=looked.outcome,
        clips=len(clips),
    )
    if looked.outcome == CHECKED:
        return None
    if looked.outcome == FOUND:
        question, options = found_question(looked.shots, clips)
        asked = (
            ask_user_choice(question, FOUND_REASON, options)
            if settings.kria_choice_questions_enabled
            else None
        )
        plan = KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=asked[0] if asked is not None else question,
            choice_question=asked[1] if asked is not None else None,
        )
    else:
        plan = KriaTurnPlan(mode="respond", turn_value="question", response=unplaced_reply(clips))
    return PlannedKriaTurn(
        plan=plan, manifest_hash=manifest.manifest_hash, context_hash=manifest.context_hash
    )


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
    brief_selects_clips: bool = False,
    clip_scope_texts: Sequence[str] = (),
    requested_media: Mapping[str, str] | None = None,
    describe_clips: bool = False,
    spoken_text: Sequence[str] = (),
    effective_brief: CreativeBrief | None = None,
    held_wording: list[PlannedKriaTurn] | None = None,
    keeps_sound: bool = False,
) -> PlannedKriaTurn:
    # KRI-802: given `held_wording`, a title-wording question is added there (one per turn)
    # and planning goes on, so the caller can ask it together with a clip picker.
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
            # KRI-682: a title the creator typed in quotes is theirs whatever excerpt
            # the model cites (it often quotes the brief's rewrite of the ask).
            quoted = value and creator_quoted(
                value, [*user_sources, getattr(inputs, "raw_creator_request", None) or ""]
            )
            if quoted and not grounded:
                log.info(
                    "kria_creative_copy_creator_quoted", thread_id=str(thread_id), target=target
                )
            grounded = grounded or quoted
            if value and not (state and state.approved == value) and not (authored or grounded):
                copy_turn = _creative_copy_turn(
                    CreativeCopyDecision(target=target, status="candidate", proposed_text=value),
                    inputs=inputs,
                    creator_request=user_message,
                    manifest=manifest,
                )
                if held_wording is None or copy_turn is None:
                    return copy_turn
                if not held_wording:
                    held_wording.append(copy_turn)
                break
    policy_notices: tuple[str, ...] = ()
    policy_omitted_texts: dict[str, tuple[str, ...]] = {}
    server_song_takes: list[dict[str, Any]] | None = None
    server_song_captions: str | None = None
    if isinstance(action, ProposeStrategy) and action.strategy.pinned_texts:
        grounded_pins, dropped_pins = ground_pinned_texts(
            action.strategy.pinned_texts,
            evidence=getattr(action.render_intent_evidence, "pinned_texts", None) or "",
            user_sources=user_sources,
        )
        if dropped_pins:
            log.info("kria_pinned_texts_ungrounded", thread_id=str(thread_id), count=dropped_pins)
            note = say(
                en="I left out on-screen text that wasn't in your words.",
                tr="Senin sözlerinde olmayan ekran yazısını eklemedim.",
            )
            action = action.model_copy(
                update={
                    "strategy": action.strategy.model_copy(
                        update={"pinned_texts": grounded_pins or None}
                    ),
                    "summary": f"{action.summary.strip()} {note}".strip(),
                }
            )
    if isinstance(action, ProposeStrategy) and action.strategy.opening_title_duration_s is not None:
        # KRI-591: a title hold is a creator-stated setting (KRI-476 asks before a renderer
        # drops it), so it must be the creator's own seconds, like the title is their own
        # words. A hold the model wrote itself is dropped here, before the policy asks about
        # a length nobody gave ("tam 3 saniye tutamıyor" on `Başlık: "Tigre'de bir gün"`).
        hold_strategy = action.strategy
        if not title_hold_grounded(
            hold_strategy.opening_title_duration_s,
            evidence=getattr(action.render_intent_evidence, "opening_title_duration_s", None),
            user_sources=user_sources,
            strip=(
                hold_strategy.opening_title,
                hold_strategy.closing_title,
                *(hold_strategy.shot_labels or ()),
                *(pin.text for pin in hold_strategy.pinned_texts or ()),
            ),
        ):
            log.info(
                "kria_title_hold_ungrounded",
                thread_id=str(thread_id),
                hold_s=hold_strategy.opening_title_duration_s,
            )
            action = action.model_copy(
                update={
                    "strategy": hold_strategy.model_copy(update={"opening_title_duration_s": None})
                }
            )
    if (
        isinstance(action, ProposeStrategy)
        and _is_voiceover_strategy(action.strategy)
        and _narrated_renders_once_recorded(manifest, action.strategy)
        # The manifest already says so when the voiceover is in use; otherwise read the
        # item's own voiceover column, the exact predicate the approval claim uses.
        and not manifest.has_voiceover
        and not await _item_has_voiceover(db, item_id)
    ):
        # KRI-815: with no voiceover this edit is either refused as "unavailable" or approved
        # and then cancelled at claim (`voiceover_required`). Neither helps: hand back a
        # script to record (or ask for the take). No draft, no approval, no Job.
        log.info("kria_narrated_without_voiceover", thread_id=str(thread_id))
        return await _plan_voiceover_script(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=creator_id,
            user_message=user_message,
            manifest=manifest,
            inputs=inputs,
            action=None,
        )
    if isinstance(action, ProposeStrategy):
        # KRI-593: a Voiceover cue ("show X when I say Y") places a clip; it never
        # chooses which clips exist. Every attached video stays unless the creator
        # limited them (their words, any turn, or a brief `select` ask).
        kept_clips = keep_every_voiceover_clip(
            manifest,
            action.strategy,
            creator_texts=[
                getattr(inputs, "raw_creator_request", None),
                getattr(inputs, "creator_request", None),
                *user_sources,
            ],
            brief_selects_clips=brief_selects_clips,
        )
        if kept_clips is not action.strategy:
            log.info(
                "kria_voiceover_clips_kept",
                thread_id=str(thread_id),
                model_selected=len(action.strategy.selected_media_ids),
                selected=len(kept_clips.selected_media_ids),
            )
            action = action.model_copy(update={"strategy": kept_clips})
        # KRI-623: "from the long takes" / "skip the short clips" leaves clips out, whatever
        # the model picked (v56 took every clip on the cabin M2 prompt). Read from the live
        # brief's `select` asks and this message, on every plan, so it cannot drift back.
        scoped = apply_clip_scope(
            manifest, action.strategy, creator_texts=[*clip_scope_texts, user_message]
        )
        if scoped is not action.strategy:
            log.info(
                "kria_clip_scope_applied",
                thread_id=str(thread_id),
                model_selected=len(action.strategy.selected_media_ids),
                model_scope=action.strategy.media_scope,
                selected=len(scoped.selected_media_ids),
            )
            action = action.model_copy(update={"strategy": scoped})
        if keeps_sound:
            # KRI-942: a turn that didn't touch the sound asks keeps the draft's sound choice
            # (the route it picks), like a confirmed label (KRI-766).
            from app.kria.draft_sound_carry import carry_draft_sound  # noqa: PLC0415

            carried = carry_draft_sound(action.strategy, await _load_draft_strategy(db, item_id))
            if carried is not action.strategy:
                log.info("kria_draft_sound_kept", thread_id=str(thread_id))
                action = action.model_copy(update={"strategy": carried})
        # KRI-142: the same server compile v1 runs, so a phone render never
        # silently drops what it can't draw while the reply claims it.
        check_kwargs: dict[str, Any] = {
            "simplify_unsupported": True,
            **({"ask_before_simplifying": True} if settings.brief_binding_for(creator_id) else {}),
            # KRI-476: also ask before a repair drops a title hold / full-screen ask the
            # creator stated. Off with the choice-questions flag: identical to before.
            **(
                {"ask_about_stated_settings": True}
                if settings.brief_binding_for(creator_id) and settings.kria_choice_questions_enabled
                else {}
            ),
            # KRI-594/KRI-598: the creator's words ("7 klibin hepsini kullan" counts the
            # clips) and the media their live brief asks for in any turn ("kırkayak" -> the
            # bathtub photo). Not only a turn that still carries the guided contract needs
            # them: the Main Creator parse drops that contract on runtime v2, and a native
            # answer can forget the beat just the same. An "all" scope needs the words too:
            # "use all my photos and videos" asked for the Visuals, "7 klibin hepsini kullan"
            # only for the clips (PR 1556 review). Omitted when none applies.
            **(
                {
                    # Oldest first, the latest message last: the latest scope words decide
                    # what "all" asked for (PR 1550 review).
                    "creator_text": "\n".join(
                        source
                        for source in (
                            *user_sources[1:],
                            *([] if user_sources[1:][-1:] == [user_message] else [user_message]),
                        )
                        if source
                    ),
                    "requested_media": dict(requested_media or {}),
                }
                if action.strategy.execution_contract is not None
                or requested_media
                or action.strategy.media_scope == "all"
                else {}
            ),
        }
        checked = check_strategy_for_runtime_v2(manifest, action.strategy, **check_kwargs)
        if (
            isinstance(checked, RefusedStrategy)
            and checked.code == SIMPLER_VERSION_REASON
            and await _simpler_version_accepted(db, thread_id, checked.fields)
        ):
            # KRI-759: the creator took the simpler version this very refusal offered
            # (a tap, or "evet"). Draft what the check keeps instead of asking again, and
            # say in the reply what was left out.
            accepted = check_strategy_for_runtime_v2(
                manifest,
                action.strategy,
                **{
                    key: value
                    for key, value in check_kwargs.items()
                    if key not in ("ask_before_simplifying", "ask_about_stated_settings")
                },
            )
            if not isinstance(accepted, RefusedStrategy):
                log.info(
                    "kria_simpler_version_accepted",
                    thread_id=str(thread_id),
                    fields=list(checked.fields),
                )
                checked = replace(
                    accepted,
                    notices=(*accepted.notices, *([checked.agreed] if checked.agreed else [])),
                )
        if isinstance(checked, RefusedStrategy):
            log.info("kria_strategy_refused", thread_id=str(thread_id), code=checked.code)
            return PlannedKriaTurn(
                plan=KriaTurnPlan(
                    mode="respond",
                    turn_value="question",
                    response=checked.question,
                    # KRI-759: a "simpler version?" needs buttons, or a "yes" is only a new
                    # message that replans into the same refusal.
                    choice_question=(
                        simpler_version_question(checked.fields)
                        if checked.code == SIMPLER_VERSION_REASON
                        and settings.kria_choice_questions_enabled
                        else None
                    ),
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        song_caption_text = "\n".join(
            text
            for text in (
                getattr(inputs, "raw_creator_request", None),
                brief_request,
                user_message,
            )
            if isinstance(text, str) and text
        )
        if settings.song_captions_enabled and settings.user_song_montage_enabled:
            # KRI-569: lyric captions come from the creator's uploaded song; ask for it
            # now instead of minting a render that cannot caption anything.
            need_song = song_captions_refusal(
                manifest, checked.strategy, creator_text=song_caption_text
            )
            if need_song is not None:
                log.info("kria_strategy_refused", thread_id=str(thread_id), code=need_song.code)
                return PlannedKriaTurn(
                    plan=KriaTurnPlan(
                        mode="respond", turn_value="question", response=need_song.question
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
        policy_notices = checked.notices
        for omitted_field in checked.omitted_fields:
            value = getattr(action.strategy, omitted_field)
            if omitted_field in {"opening_title", "closing_title"} and value:
                policy_omitted_texts[omitted_field] = (value,)
            elif omitted_field == "shot_labels" and value:
                policy_omitted_texts[omitted_field] = tuple(value)
            elif omitted_field == "pinned_texts" and value:
                policy_omitted_texts[omitted_field] = tuple(pin.text for pin in value)
        summary = " ".join(
            [
                say(en="I prepared a simpler draft.", tr="Daha sade bir taslak hazırladım.")
                if checked.omitted_fields
                else action.summary.strip(),
                *policy_notices,
            ]
        ).strip()[:1000]
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
                continuable=gate.pending,
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
        if (
            settings.song_captions_enabled
            and settings.user_song_montage_enabled
            # Only a strategy that plays the creator's song can caption its lyrics (any other
            # strategy has its song fields dropped by `repair_creator_user_song`).
            and action.strategy.audio_strategy == "user_song"
        ):
            # KRI-578: decide the lyric captions ONCE, here, from the creator's own words.
            # The song-order gate above may have rebuilt the strategy, so this reads the
            # final one; the worker of a plan-authority job reads only this pin.
            from app.pipeline.song_captions import song_captions_pin  # noqa: PLC0415

            # Brief-off turns carry the creator's whole thread in `inputs.creator_request`
            # (the latest message alone would pin None on "make the cuts faster"); history
            # first so the latest message still wins the last-line rule.
            history = getattr(inputs, "creator_request", None)
            server_song_captions = song_captions_pin(
                "\n".join(t for t in (history, song_caption_text) if isinstance(t, str) and t),
                action.strategy.caption_style,
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
        # Leverkusen M2: the parts of clips the head draft already keeps out (no flag).
        earlier_hidden = await _earlier_hidden_parts(db, item_id)
        earlier_excluded = (
            await _earlier_exclusions(db, item_id) if settings.clip_intent_exclude_enabled else []
        )
        try:
            planned, thread_events = await _plan_turn_clip_intents(
                db,
                thread_id=thread_id,
                item_id=item_id,
                creator_id=creator_id,
                user_message=user_message,
                inputs=inputs,
                brief_request=brief_request,
                effective_brief=effective_brief,
                wants_capture_order=wants_capture_order,
                describe_clips=describe_clips,
                spoken_text=spoken_text,
                strategy=action.strategy,
            )
        except Exception as exc:  # noqa: BLE001 - no provider failure may mint a draft
            return _clip_planning_failed(manifest, exc, clips=intent_clips, thread_id=thread_id)
        if any(intent.label_source == "transcript" for intent in planned.requested_intents) and (
            manifest.narration is None
            or action.strategy.execution_contract != "guided_voiceover_v1"
        ):
            kept_intents = renderable_clip_intents(manifest, planned.requested_intents)
            if kept_intents is None:
                return PlannedKriaTurn(
                    plan=_clip_intent_resolution_plan(
                        question=_labels_need_voiceover_reply(),
                        status="needs_creator",
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
            # KRI-691: a native voiceover chat can never draw a narration label, so the
            # question above dead-ended every turn. Leave the label out and say so; the
            # resolver never saw it, so the other intents are already resolved.
            planned = replace(
                planned,
                requested_intents=kept_intents,
                resolution=replace(
                    planned.resolution,
                    intents=[
                        intent
                        for intent in planned.resolution.intents
                        if intent.label_source != "transcript"
                    ],
                ),
            )
            notice = transcript_labels_dropped_notice()
            if notice not in policy_notices:
                action = action.model_copy(
                    update={"summary": " ".join([action.summary.strip(), notice]).strip()}
                )
                policy_notices = (*policy_notices, notice)
            log.info(
                "kria_transcript_labels_dropped",
                thread_id=str(thread_id),
                kept=len(kept_intents),
            )
        unsaved = await _persist_turn_vision_answers(
            db,
            item_id=item_id,
            creator_id=creator_id,
            thread_id=thread_id,
            manifest=manifest,
            planned=planned,
        )
        if unsaved is not None:
            return unsaved

        if settings.clip_intent_exclude_enabled:
            # KRI-761: the resolver is not deterministic and the whole request is re-planned
            # every turn, so an ask it matched in the draft before keeps that match when it
            # comes back empty now.
            carried_intents, carried = carry_forward_exclusions(
                planned.resolution.intents,
                earlier_excluded,
                clip_ids={clip.media_id for clip in intent_clips},
            )
            if carried:
                log.info("kria_clip_exclusions_carried", thread_id=str(thread_id), carried=carried)
                planned = replace(
                    planned, resolution=replace(planned.resolution, intents=carried_intents)
                )
        # Leverkusen M2: a part of a clip the head draft hid stays hidden, with the most seconds
        # the creator gave: a re-plan never shows it again.
        kept_hidden, carried_hidden = carry_hidden_parts(
            planned.resolution.intents,
            earlier_hidden,
            clip_ids={clip.media_id for clip in intent_clips},
        )
        if carried_hidden:
            log.info("kria_hidden_parts_carried", thread_id=str(thread_id), carried=carried_hidden)
            planned = replace(planned, resolution=replace(planned.resolution, intents=kept_hidden))
        # KRI-761: an exclude that names a clip another ask pins, or that look-alikes share,
        # is the creator's to settle: the picker, never a guess. KRI-807: so is one that
        # matched no clip ("skip the blurry shot"), never a free-text ask.
        contested = (
            exclusions_to_ask(
                planned.resolution.intents,
                action.strategy,
                suspects=planned.resolution.exclusion_suspects,
                clip_count=len(intent_clips),
            )
            if settings.clip_intent_exclude_enabled
            and settings.kria_clip_selection_questions_enabled
            else []
        )
        # KRI-962: a Voiceover shot two free footage clips match equally (N1's DON JULIO
        # sign: the window sign and Atakan at the plaque, both 0.9) is the creator's pick.
        ties = _voiceover_tie_shots(planned, action.strategy, intent_clips, thread_id)
        skipped = left_out(planned.resolution.intents) if ties else set()
        if (
            planned.resolution.status != "resolved"
            or planned.resolution.needs_creator
            or planned.resolution.deferred_queries
        ):
            question = planned.resolution.question
            clip_question = (
                planned.resolution.clip_question if planned.resolution.needs_creator else None
            )
            if clip_question is not None and contested:
                # KRI-807: the skips ride in the resolver's picker, one card and one turn.
                question, clip_question = with_exclusion_categories(
                    question, clip_question, contested, intent_clips
                )
            if clip_question is not None and ties:
                question, clip_question = with_tie_categories(
                    question, clip_question, ties, intent_clips, excluded=skipped
                )
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question=question,
                    status=(
                        "needs_creator"
                        if planned.resolution.needs_creator
                        else planned.resolution.status
                    ),
                    diagnostics=_resolution_diagnostics(planned.resolution),
                    clip_question=clip_question,
                    clip_intent_inventory=planned.inventory,
                    reply_options=(
                        planned.resolution.reply_options if planned.resolution.needs_creator else ()
                    ),
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
                # KRI-709: only a budget/deadline stop continues. A question for the
                # creator or a provider failure needs their reply or a fresh attempt.
                continuable=(
                    planned.resolution.status == "pending" and not planned.resolution.needs_creator
                ),
            )
        # Leverkusen M2: a part with its clip but no edge or seconds (or one that leaves nothing
        # of the clip) is asked about in plain words; it is never drafted with the part showing.
        hidden_question = hidden_part_question(planned.resolution.intents, intent_clips)
        if hidden_question is not None:
            log.info("kria_hidden_part_question", thread_id=str(thread_id))
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question=hidden_question,
                    status="needs_creator",
                    diagnostics={"stage": "hidden_part", "reason": "needs_creator"},
                    clip_intent_inventory=planned.inventory,
                ),
                manifest_hash=manifest.manifest_hash,
                context_hash=manifest.context_hash,
            )
        if settings.clip_intent_exclude_enabled:
            asked = exclusion_question(contested, intent_clips)
            if asked is not None:
                log.info(
                    "kria_clip_exclusions_contested",
                    thread_id=str(thread_id),
                    asks=len(contested),
                    pinned=sum(1 for c in contested if c.blocked),
                    unmatched=sum(1 for c in contested if not c.matched),
                    suspected=sum(1 for c in contested if not c.matched and c.suggested),
                )
                if ties:
                    asked = with_tie_categories(*asked, ties, intent_clips, excluded=skipped)
                return PlannedKriaTurn(
                    plan=_clip_intent_resolution_plan(
                        question=asked[0],
                        status="needs_creator",
                        diagnostics={
                            "stage": "exclusion",
                            "reason": (
                                "unmatched"
                                if all(not c.matched for c in contested)
                                else "contested"
                            ),
                        },
                        clip_question=asked[1],
                        clip_intent_inventory=planned.inventory,
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
            # KRI-667: "Skip the quad bike clip" leaves the clips the resolver confidently
            # matched out of the pick, whatever the model selected. After the strategy
            # check, so the narrowed strategy is checked again (a refusal keeps the pick).
            narrowed = _without_excluded_clips(
                manifest, action.strategy, planned.resolution.intents
            )
            if narrowed is not None:
                log.info(
                    "kria_clip_exclusions_applied",
                    thread_id=str(thread_id),
                    model_selected=len(action.strategy.selected_media_ids),
                    model_scope=action.strategy.media_scope,
                    selected=len(narrowed[0].selected_media_ids),
                )
                extra = [n for n in narrowed[1] if n not in policy_notices]
                action = action.model_copy(
                    update={
                        "strategy": narrowed[0],
                        "summary": " ".join([action.summary.strip(), *extra]).strip(),
                    }
                )
                policy_notices = (*policy_notices, *extra)
            # KRI-993: "only about the steak" limits the pick to the clips it matched
            # (after the skips, so a skipped clip is never added back).
            scoped_pick = _with_subject_scope(manifest, action.strategy, planned.resolution.intents)
            if scoped_pick is not None:
                extra = [n for n in scoped_pick[1] if n not in policy_notices]
                action = action.model_copy(
                    update={
                        "strategy": scoped_pick[0],
                        "summary": " ".join([action.summary.strip(), *extra]).strip(),
                    }
                )
                policy_notices = (*policy_notices, *extra)
            if _unapplied_exclusions(planned):
                # KRI-680: "don't use the two very dark clips" that no clip matched was
                # dropped without a word, and both clips rendered. Say so before the render.
                notice = _unapplied_exclusion_notice()
                if notice not in policy_notices:
                    action = action.model_copy(
                        update={"summary": " ".join([action.summary.strip(), notice]).strip()}
                    )
                    policy_notices = (*policy_notices, notice)
            media_ids = [media.media_id for media in manifest.media]
            still_played = still_played_exclusions(
                planned.resolution.intents, action.strategy, media_ids
            )
            if still_played:
                # KRI-1026: a skip that matched clips but could not take them out (another
                # settled ask or the strategy uses them, or nothing would be left) is said.
                log.info(
                    "kria_clip_exclusions_kept",
                    thread_id=str(thread_id),
                    kept=len(still_played),
                )
                notice = still_played_notice([media_ids.index(m) + 1 for m in still_played])
                if notice not in policy_notices:
                    action = action.model_copy(
                        update={"summary": " ".join([action.summary.strip(), notice]).strip()}
                    )
                    policy_notices = (*policy_notices, notice)
            # KRI-1031: "skip anything too short to use" names the clips it left out (or says
            # none is too short), so the creator sees what the measured rule did.
            measured = length_exclusion_notice(planned.resolution.intents, still_played, media_ids)
            if measured is not None and measured not in policy_notices:
                action = action.model_copy(
                    update={"summary": " ".join([action.summary.strip(), measured]).strip()}
                )
                policy_notices = (*policy_notices, measured)
        # Leverkusen M2: an ask no clip operation can carry ("start that clip after the
        # kids") was a "please restate" that looped. It never holds the turn; the draft says
        # once what the clip steps couldn't do and what they can.
        unexpressed = unexpressed_notice(planned.unexpressed_asks)
        if unexpressed is not None and unexpressed not in policy_notices:
            action = action.model_copy(
                update={"summary": " ".join([action.summary.strip(), unexpressed]).strip()}
            )
            policy_notices = (*policy_notices, unexpressed)
        tie_asked = tie_question(ties, intent_clips, excluded=skipped)
        if tie_asked is not None:
            log.info(
                "kria_voiceover_tie_asked",
                thread_id=str(thread_id),
                shots=len(ties),
                clips=[len(tie.media_ids) for tie in ties],
            )
            return PlannedKriaTurn(
                plan=_clip_intent_resolution_plan(
                    question=tie_asked[0],
                    status="needs_creator",
                    diagnostics={"stage": "tie", "reason": "voiceover_tie"},
                    clip_question=tie_asked[1],
                    clip_intent_inventory=planned.inventory,
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
        resolved_intents, ordering_choice = brief_order_fields(
            planned.resolution.intents, effective_brief, ordering_choice
        )
        return PlannedKriaTurn(
            plan=adapt_creator_action(
                action,
                server_clip_intents=planned.requested_intents,
                server_resolved_clip_intents=resolved_intents,
                server_resolved_song_takes=server_song_takes,
                server_song_captions=server_song_captions,
                ordering_choice=ordering_choice,
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
            policy_notices=policy_notices,
            policy_omitted_texts=policy_omitted_texts,
        )
    if isinstance(action, AskUser) and asks_for_missing_media(action.reason_code):
        # Leverkusen M2: "I couldn't find the shot ..., upload it?" is never said before the
        # clips were looked inside for it.
        looked = await _look_inside_clips_first(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=creator_id,
            user_message=user_message,
            manifest=manifest,
            inputs=inputs,
            brief_request=brief_request,
            effective_brief=effective_brief,
            wants_capture_order=wants_capture_order,
            describe_clips=describe_clips,
            spoken_text=spoken_text,
        )
        if looked is not None:
            return looked
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
        plan=adapt_creator_action(
            action,
            server_resolved_song_takes=server_song_takes,
            server_song_captions=server_song_captions,
        ),
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
        policy_notices=policy_notices,
        policy_omitted_texts=policy_omitted_texts,
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


async def _earlier_exclusions(db: AsyncSession, item_id: uuid.UUID) -> list[ResolvedClipIntent]:
    """The ``exclude`` intents the item's head draft resolved (KRI-761); [] when unreadable.

    Fails open: this only lets an ask keep a match it already had, so no read problem may
    cost the creator a turn. The read runs in a savepoint (a database error cannot poison
    the turn's transaction) and releases the transaction like the thread-event read before it.
    """
    try:
        async with db.begin_nested():
            snapshot = (
                await db.execute(
                    select(CreatorEditDraft.snapshot_json)
                    .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True))
                    .order_by(CreatorEditDraft.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        await db.rollback()  # no connection pinned across the resolver and vision calls
        return earlier_exclusions(snapshot)
    except Exception as exc:  # noqa: BLE001 - an earlier match is a convenience, never a gate
        log.info("kria_earlier_exclusions_unreadable", error_type=type(exc).__name__)
        return []


async def _earlier_hidden_parts(db: AsyncSession, item_id: uuid.UUID) -> list[ResolvedClipIntent]:
    """The settled ``hide_part`` intents of the item's head draft (Leverkusen M2).

    Read like ``_earlier_exclusions``, in a savepoint. Unreadable reads as none: the current
    turn's own ask still hides the part, and the pinned-recipe check refuses any render that
    shows a part the approved strategy hides.
    """
    try:
        async with db.begin_nested():
            snapshot = (
                await db.execute(
                    select(CreatorEditDraft.snapshot_json)
                    .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True))
                    .order_by(CreatorEditDraft.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        await db.rollback()
        return earlier_hidden_parts(snapshot)
    except Exception as exc:  # noqa: BLE001 - see above: never the only guard
        log.info("kria_earlier_hidden_parts_unreadable", error_type=type(exc).__name__)
        return []


def _unapplied_exclusions(planned: PlannedIntentResolution) -> int:
    """How many "leave these clips out" asks leave nothing out (KRI-680).

    The planner could not tie the ask to clips (`exclusion_dropped`), or the resolver
    matched no clip confidently: without a notice the ask vanishes and the clips play.
    KRI-807: an unmatched exclude asks with the clip picker first, so this is left for a
    dropped ask, the picker flag off, a one-clip project or nothing left to tap.
    """
    unmatched = sum(
        1
        for intent in planned.resolution.intents
        if intent.op == "exclude"
        and intent.status == "resolved"
        and not intent.assignments
        # KRI-1031: a measured length ask that matched nothing kept every clip on purpose.
        and intent.by_length is None
    )
    return planned.dropped_exclusions + unmatched


def _unapplied_exclusion_notice() -> str:
    return say(
        en=(
            "I couldn't tell which clips you asked me to leave out, so I haven't removed"
            " any for that. Tell me which ones and I'll take them out."
        ),
        tr=(
            "Hangi klipleri çıkarmamı istediğini anlayamadım, bu yüzden o istek için"
            " hiçbirini çıkarmadım. Hangilerini çıkarayım, söylersen çıkarırım."
        ),
    )


def _without_excluded_clips(
    manifest: ResolvedCreatorManifest,
    strategy: Any,
    resolved: list[ResolvedClipIntent],
) -> tuple[Any, tuple[str, ...]] | None:
    """(checked strategy without the excluded clips, its notices), or None to keep the pick.

    KRI-667: `apply_clip_exclusions` narrows the selection; the narrowed strategy runs the
    same runtime-v2 check the model's strategy did, so a pick the renderer cannot make
    is never drafted. A refusal keeps the checked pick: the clip stays, as before.
    """
    narrowed = apply_clip_exclusions(manifest, strategy, resolved_intents=resolved)
    if narrowed is strategy:
        return None
    checked = check_strategy_for_runtime_v2(manifest, narrowed)
    if isinstance(checked, RefusedStrategy):
        log.info("kria_clip_exclusions_refused", code=checked.code)
        return None
    return checked.strategy, tuple(checked.notices)


def _with_subject_scope(
    manifest: ResolvedCreatorManifest,
    strategy: Any,
    resolved: list[ResolvedClipIntent],
) -> tuple[Any, tuple[str, ...]] | None:
    """(checked strategy limited to the "only about X" clips, its notices), or None to keep.

    KRI-993: `apply_subject_scope` narrows the pick; the narrowed strategy runs the same
    runtime-v2 check, and a refusal keeps the checked pick (the footage stays).
    """
    scope = apply_subject_scope(manifest, strategy, resolved_intents=resolved)
    if scope.strategy is strategy:
        return None
    checked = check_strategy_for_runtime_v2(manifest, scope.strategy)
    if isinstance(checked, RefusedStrategy):
        log.info("kria_clip_subject_scope_refused", code=checked.code)
        return None
    log.info(
        "kria_clip_subject_scope_applied",
        kept=list(scope.kept),
        dropped=list(scope.dropped),
        added=list(scope.added),
    )
    return checked.strategy, tuple(checked.notices)


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


def _brief_selects_clips(brief: CreativeBrief | None) -> bool:
    """KRI-593: does the live brief carry a which-clips ask (kind ``select``)?

    It outlives the bounded chat window, so a "skip the bathroom clip" said many turns
    ago still keeps a Voiceover edit's selection the creator's.
    """
    return brief is not None and any(req.kind == "select" for req in brief.live())


def _brief_clip_scope_texts(brief: CreativeBrief | None) -> tuple[str, ...]:
    """KRI-623: the live brief's which-clips asks, oldest first ("from the long takes")."""
    if brief is None:
        return ()
    return tuple(
        req.description for req in brief.live() if req.kind == "select" and req.description
    )


def _recovery_cause(stage: str, exc: BaseException) -> dict[str, str]:
    """Which step failed and with what, for the admin turn/event views (KRI-536).

    Class names only, never ``str(exc)``: the message can quote the creator's words.
    ``cause_type`` is the wrapped error (``TerminalSchemaError`` vs a transient
    ``TerminalError``), which is what tells a bad extractor output from a provider fault.
    """
    cause: dict[str, str] = {"stage": stage, "error_type": type(exc).__name__}
    if exc.__cause__ is not None:
        cause["cause_type"] = type(exc.__cause__).__name__
    # KRI-586: a redacted agent failure carries a value-free ``error_class`` (field paths +
    # error types, never values) on some link of the chain; surface it for the admin views.
    link: BaseException | None = exc
    for _ in range(4):
        error_class = getattr(link, "error_class", None)
        if isinstance(error_class, str) and error_class:
            cause["error_class"] = error_class[:80]
            break
        link = link.__cause__ if link is not None else None
        if link is None:
            break
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
        # The reading step hit a provider/transport failure: not a misread request.
        "request_extraction_provider_failed": say(
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
        if reason
        in {
            "creator_planning_failed",
            "editor_planning_failed",
            "request_extraction_provider_failed",
        }
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
    creator's full message is recorded as one requirement, so it stays visible instead of
    being dropped; the turn's receipt then names what the ops changed (KRI-558).

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
        "retime_text_sequence",
        "realign_labels",
        "label_each_clip",
        "reorder_clips_by",
    }
)
# 29 Ekim: a word-by-word title is one in-place text op, so it rides the fast path too
# (`KRIA_COMPOSE_UNMET_WITH_OPS_ENABLED`; off = the set above, unchanged).
_COMPOSE_FAST_PATH_OPS = frozenset({"replace_text_sequence"})
_GROUNDED_REORDERS = (None, "capture_time", "random")
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
    r"funniest|more clips|fewer clips|farkl\w*|yeniden|bastan|ba\u015ftan)\b"
)
# KRI-520: Turkish re-plan wording on `loose_text`, at the English cues' precision: a new
# version, "from scratch", a best/funniest-N selection, more/fewer clips. A bare
# "en iyi" or "tekrar" stays out (they also appear in ordinary edits).
_REPLAN_CUES_TR = re.compile(
    r"\b(?:farkl|yeniden|bastan|sifirdan|en iyi \d|\d+ en iyi|"
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
            if str(name) not in _FAST_PATH_OPS and not (
                str(name) in _COMPOSE_FAST_PATH_OPS and compose_unmet_with_ops()
            ):
                return False
            # A model-authored clip order is no in-place text/label op: only the
            # server-grounded capture_time and seeded random reorders ride the fast path.
            criterion = (
                op.get("criterion") if isinstance(op, dict) else getattr(op, "criterion", None)
            )
            if str(name) == "reorder_clips_by" and criterion not in (
                None,
                "capture_time",
                "random",
            ):
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


def _failed_correction_alternative() -> str:
    return say(
        en=(
            "I can't apply this whole correction with the saved draft's supported editor tools. "
            "Nothing was changed. Ask for a single text or style change, or ask for a new "
            "version using your saved request."
        ),
        tr=(
            "Bu düzeltmenin tamamını kayıtlı taslağın desteklediği düzenleme araçlarıyla "
            "uygulayamıyorum. Hiçbir şey değişmedi. Tek bir yazı veya stil değişikliği iste "
            "ya da kayıtlı isteğinden yeni bir sürüm iste."
        ),
    )


def _editor_target_recovery(manifest: object) -> PlannedKriaTurn:
    """An editor-eligible ask on a rendered item whose editor target could not be
    loaded must not silently become a re-plan that replaces the draft with a render."""
    miss = _editor_target_miss.get()
    if miss in {"failed_draft_unavailable", "render_outcome_unknown"}:
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="recovery",
                response=(
                    say(
                        en=(
                            "The previous render's outcome is still unknown. Refresh the project "
                            "to check it before starting another render. "
                            "Your saved draft is unchanged."
                        ),
                        tr=(
                            "Önceki videonun sonucu hâlâ bilinmiyor. Başka bir video başlatmadan "
                            "önce sonucu kontrol etmek için projeyi yenile. "
                            "Kayıtlı taslağın değişmedi."
                        ),
                    )
                    if miss == "render_outcome_unknown"
                    else say(
                        en=(
                            "There isn't a current editable draft for this failed render. "
                            "Your saved request is unchanged. No new render was started. "
                            "Ask for a new version using your saved request."
                        ),
                        tr=(
                            "Bu başarısız video için güncel, düzenlenebilir bir taslak yok. "
                            "Kayıtlı isteğin değişmedi. Yeni bir video başlatılmadı. "
                            "Kayıtlı isteğinden yeni bir sürüm iste."
                        ),
                    )
                ),
            ),
            manifest_hash=manifest.manifest_hash,
            context_hash=manifest.context_hash,
        )
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

    # KRI-723: the app answers a moment-order card (stamped at submit); the gate reads it.
    answers_moment_questions = bool(kwargs.pop("moment_questions", False))
    # KRI-898: the sending app build rides the planned turn to the choice gate only.
    client_protocol = kwargs.pop("client_protocol", None)
    item = await db.get(PlanItem, kwargs["item_id"])
    before = snapshot_media(item) if item is not None else {}
    planned = await _plan_live_turn(db, **kwargs)
    if planned.plan.mode != "act":
        return planned
    current = await db.get(PlanItem, kwargs["item_id"], populate_existing=True)
    if current is None:
        after = {}
    elif _attached_visual_ids(planned.brief_manifest):
        # KRI-1069: each Visual's own capture time, so a filming order can date a photo.
        after = snapshot_media(current, visuals=await _snapshot_visual_rows(db, current))
    else:
        after = snapshot_media(current)
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
    planned = replace(planned, media_snapshot=after, client_protocol=client_protocol)
    return await _gate_unresolved_choices(
        db,
        planned,
        thread_id=kwargs["thread_id"],
        creator_id=kwargs["creator_id"],
        item_id=kwargs["item_id"],
        creator_text=kwargs.get("user_message"),
        answers_moment_questions=answers_moment_questions,
    )


async def _gate_unresolved_choices(
    db: AsyncSession,
    planned: PlannedKriaTurn,
    *,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    item_id: uuid.UUID | None = None,
    creator_text: str | None = None,
    answers_moment_questions: bool = False,
) -> PlannedKriaTurn:
    """KRI-476: an unresolved material choice never becomes an approvable draft.

    ``answers_moment_questions`` (KRI-723): the app that sent this turn can answer a
    ``moment_order_question`` (stamped at submit from its protocol). Only then is a creator
    whose named moments the clip record cannot place asked to point at them.

    ``creator_text`` is this turn's creator message: the brief it previews stamps
    authority from it exactly as the persisted version will (KRI-824), so the length plan
    approval pins and the worker's bound brief agree.

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
    brief: CreativeBrief | None = None
    # Only a creator with a brief BINDING has a dispatch contract that reads the brief, so
    # only they can be asked a brief-derived question that anything would later refuse
    # (the completion backstop applies the same guard).
    if settings.creative_brief_for(creator_id) and settings.brief_binding_for(creator_id):
        brief = await load_latest_brief(db, thread_id)
        if planned.brief_updates:
            with suppress(BriefUpdateBatchError):
                brief = apply_updates(
                    brief, planned.brief_updates, source_turn_id=None, creator_text=creator_text
                )
    from app.kria.unclear_word_ask import clarification_question  # noqa: PLC0415

    clarification = clarification_question(
        brief, planned.media_snapshot, strategy=strategy, creator_id=creator_id
    )
    if clarification is not None:
        return replace(
            planned,
            plan=KriaTurnPlan(mode="respond", turn_value="question", response=clarification),
        )
    if not settings.kria_choice_questions_enabled and not strategy.get("editorial_plan"):
        return planned
    events = await _load_thread_events(db, thread_id)
    from app.services.clip_none_answers import NoneAnswers, none_answer_choices  # noqa: PLC0415

    # KRI-1025: a pin or text whose clip the creator answered None to on the picker is
    # recorded as their answer, so nothing below (or the contract) waits on it.
    none_answers = (
        none_answer_choices(brief, strategy, events)
        if settings.kria_choice_questions_enabled
        else NoneAnswers()
    )
    typed_brief = brief
    if none_answers.answers:
        brief = answered_brief(brief, {"choice_answers": list(none_answers.answers)})
    music_inputs = {
        "media": (planned.media_snapshot or {}).get("clip_assignments"),
        "selected": strategy.get("selected_media_ids"),
        "audio": strategy.get("audio_strategy", "licensed_music"),
    }
    # KRI-679: "calm music" must reach the song matcher, and a new ask must re-match.
    music_ask = _creator_music_ask(brief)
    if music_ask:
        music_inputs["music_ask"] = music_ask
    music_input_digest = hashlib.sha256(
        json.dumps(music_inputs, sort_keys=True, default=str).encode()
    ).hexdigest()
    music_pool_digest = _music_pool_digest(music_inputs["media"], music_inputs["audio"], music_ask)
    from app.services.length_policy import creator_duration_s  # noqa: PLC0415

    creator_target = strategy.get("creator_duration_s")
    music_target_s = creator_duration_s(brief=brief) or (
        float(creator_target)
        if isinstance(creator_target, (int, float))
        and not isinstance(creator_target, bool)
        and creator_target > 0
        else None
    )
    frozen_music = _prior_frozen_music(events, music_input_digest, music_pool_digest)
    if isinstance(frozen_music, dict) and GENRE_FIT not in frozen_music and asked_genres(music_ask):
        # KRI-848: frozen before the song's genre was checked, so nobody has looked at it.
        frozen_music = None
    if frozen_music is None:
        frozen_music = await _freeze_licensed_music_section(
            db,
            strategy,
            planned.media_snapshot,
            # KRI-809: the thread id is not a Job; binding it as ``job_id`` broke the
            # agent_run FK and left the creator unattributed for paid-call cost control.
            # Record the run like Main Creator does: creator + plan item, no job. No
            # request_id either, so an identical re-match later in the same thread is a
            # new paid call rather than a replay of the settled first one.
            run_context=RunContext(
                creator_id=str(creator_id),
                plan_item_id=str(item_id) if item_id else None,
            ),
            target_s=music_target_s,
            music_ask=music_ask,
        )
    elif isinstance(frozen_music, dict):
        # A section frozen before KRI-600 may be longer than the footage can fill, and
        # one frozen before KRI-679 (or before the selection changed) shorter than asked.
        frozen_music = _fit_music_section_to_footage(
            _fit_music_section_to_length(frozen_music, music_target_s),
            (planned.media_snapshot or {}).get("clip_assignments") or [],
            selected_ids={str(v) for v in strategy.get("selected_media_ids") or []},
        )
    if frozen_music is not None:
        strategy = {**strategy, "selected_music_section": frozen_music}
    if frozen_music is not None:
        apply_intent = apply_intent.model_copy(
            update={"arguments": {**arguments, "strategy": strategy}}
        )
        plan = plan.model_copy(update={"intents": [apply_intent, *plan.intents[1:]]})
        planned = replace(planned, plan=plan)
        arguments = apply_intent.arguments
    # KRI-597 / KRI-803: find when the closing line and each line to hear whole are said
    # before the length plan freezes their windows, and say so plainly when nobody can.
    closing_quote, named_lines = await _ground_spoken_lines(strategy, planned.media_snapshot, brief)
    # KRI-585: on the phone route that plays every clip's own sound, what the clips say
    # may order them; the length plan seats the windows that keeps right.
    camera_sound = settings.spoken_order_enabled and keeps_camera_sound(
        planned.brief_manifest, strategy
    )
    # KRI-723: the creator's named moments of one take. Grounded ones are pinned; for the
    # rest an app that can answer is asked, before any approval card, to point at them.
    inspections = None
    if (
        settings.kria_prompt_moment_inspection_enabled
        and settings.clip_intents_enabled
        and answers_moment_questions
        and item_id is not None
    ):
        from app.services.length_planning import DEFAULT_MOMENT_S
        from app.services.named_moments import MomentOrderError, named_moments
        from app.services.prompt_moment_inspection import inspect_named_moments

        ask = brief_moment_ask(brief)
        try:
            moment_set = named_moments(
                strategy,
                planned.media_snapshot,
                moment_count=ask.count if ask is not None else None,
            )
        except MomentOrderError:
            moment_set = None
        if moment_set is not None:
            turn_end = turn_deadline.get()
            inspections = await inspect_named_moments(
                db,
                creator_id=creator_id,
                item_id=item_id,
                moment_set=moment_set,
                events=events,
                allow_provider=settings.kria_prompt_moment_inspection_allow_provider,
                max_window_s=ask.each_s if ask is not None and ask.each_s else DEFAULT_MOMENT_S,
                budget_s=(
                    turn_end - _POST_UNDERSTANDING_RESERVE_S - time.monotonic()
                    if turn_end is not None
                    else None
                ),
            )
    moments = _moment_decision(
        strategy,
        planned.media_snapshot,
        brief,
        events,
        can_ask=answers_moment_questions and settings.clip_intents_enabled,
        inspections=inspections,
    )
    if moments.recovery is not None:
        return replace(
            planned,
            plan=KriaTurnPlan(mode="respond", turn_value="question", response=moments.recovery),
        )
    if moments.question is not None:
        from app.services.named_moments import question_text  # noqa: PLC0415

        return replace(
            planned,
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response=question_text(moments.question),
                moment_order_question=moments.question,
            ),
        )
    enriched_snapshot = _enrich_best_parts_snapshot(
        strategy,
        planned.media_snapshot,
        brief,
        closing_quote=closing_quote,
        named_lines=named_lines,
        camera_sound=camera_sound,
        visual_photo_ids=_held_visual_pool(planned.brief_manifest, strategy),
        attached_visual_ids=_attached_visual_ids(planned.brief_manifest),
        moment_pins=moments,
    )
    if enriched_snapshot is not planned.media_snapshot:
        planned = replace(planned, media_snapshot=enriched_snapshot)
    length_plan = (
        enriched_snapshot.get("length_plan") if isinstance(enriched_snapshot, dict) else None
    )
    if strategy.get("editorial_plan") is not None:
        from app.services.editorial_plan import editorial_summary

        if isinstance(length_plan, dict) and any(
            conflict.get("code") == "editorial_text_capacity"
            for conflict in length_plan.get("conflicts", [])
        ):
            return replace(
                planned,
                plan=KriaTurnPlan(
                    mode="respond",
                    turn_value="question",
                    response=say(
                        en=(
                            "The planned explanations need more reading time than this edit "
                            "allows. Should we use a longer edit or explain fewer parts?"
                        ),
                        tr=(
                            "Planlanan açıklamaları okumak için bu videoda yeterli süre yok. "
                            "Videoyu uzatalım mı, yoksa daha az bölümü mü açıklayalım?"
                        ),
                    ),
                ),
            )

        windows = length_plan.get("windows", []) if isinstance(length_plan, dict) else []
        duration = sum(float(row["source_end_s"]) - float(row["source_start_s"]) for row in windows)
        summary = editorial_summary(strategy["editorial_plan"], duration or None)
        arguments = {**arguments, "summary": summary}
        apply_intent = apply_intent.model_copy(update={"arguments": arguments})
        plan = plan.model_copy(update={"intents": [apply_intent, *plan.intents[1:]]})
        planned = replace(planned, plan=plan)
    # KRI-848: say which song is suggested and whether its labels fit the genre asked for.
    # The receipts reply replaces the summary whenever a judged ask is unmet and keeps only
    # the policy notices, so the song notice rides both.
    song_notice = music_genre_notice(frozen_music)
    if song_notice and song_notice not in planned.policy_notices:
        planned = replace(planned, policy_notices=(*planned.policy_notices, song_notice))
    # KRI-1068: the clip the creator just dropped is said like the song, on both replies.
    pin_notices = [n for n in planned.pin_notices if n not in planned.policy_notices]
    if pin_notices:
        planned = replace(planned, policy_notices=(*planned.policy_notices, *pin_notices))
    model_summary = str(arguments.get("summary") or "")
    plan_notices = [
        notice
        for notice in (
            closing_quote_notice(
                length_plan.get("closing_quote") if isinstance(length_plan, dict) else closing_quote
            ),
            _moment_count_notice(strategy, brief, length_plan, summary=model_summary),
            song_notice,
            *planned.pin_notices,
        )
        if notice
    ]
    if plan_notices:
        arguments = {**arguments, "summary": _summary_with_notices(model_summary, plan_notices)}
        apply_intent = apply_intent.model_copy(update={"arguments": arguments})
        plan = plan.model_copy(update={"intents": [apply_intent, *plan.intents[1:]]})
        planned = replace(planned, plan=plan)
    if not settings.kria_choice_questions_enabled:
        return planned
    resolution = resolve_choices(
        strategy,
        answered_brief(brief, strategy),
        enriched_snapshot,
        events,
        ChoiceCapability(
            max_duration_s=_edit_cap_s(strategy),
            creator_id=creator_id,
            voice_route=settings.voice_behind_footage_enabled,
            speech_occurrence=settings.speech_occurrence_question_enabled,
            client_protocol=planned.client_protocol,
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
        question = build_choice_question(candidate)
        if frozen_music is not None:
            question["length_context"] = {
                "input_digest": music_input_digest,
                "pool_digest": music_pool_digest,
                "licensed_music": frozen_music,
            }
        asked = KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=choice_question_text(candidate),
            choice_question=question,
        )
        return replace(planned, plan=asked)
    waiting = length_inputs_waiting(resolution)
    if waiting is not None:
        return replace(
            planned,
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="progress",
                response=waiting,
            ),
        )
    if not resolution.answers and not none_answers.answers:
        return _with_narration_note(
            planned,
            strategy,
            enriched_snapshot,
            brief,
            camera_sound=camera_sound,
            closing_quote=closing_quote,
        )
    if none_answers.answers and resolution.answers:
        # What the video ends on instead follows the creator's other answers (their upload
        # order, say), so the notices are worded against the brief those answers give.
        none_answers = none_answer_choices(
            answered_brief(typed_brief, resolution.strategy), strategy, events
        )
    chosen = resolution.strategy
    if none_answers.answers:
        chosen = {**chosen, "choice_answers": [*resolution.answers, *none_answers.answers]}
    summary = _summary_with_notices(
        model_summary, [*plan_notices, *resolution.notices, *none_answers.notices]
    )
    rewritten = apply_intent.model_copy(
        update={"arguments": {**arguments, "strategy": chosen, "summary": summary}}
    )
    final_brief = answered_brief(brief, chosen)
    final_snapshot = _enrich_best_parts_snapshot(
        chosen,
        enriched_snapshot,
        final_brief,
        closing_quote=closing_quote,
        named_lines=named_lines,
        camera_sound=camera_sound,
        visual_photo_ids=_held_visual_pool(planned.brief_manifest, chosen),
        attached_visual_ids=_attached_visual_ids(planned.brief_manifest),
        moment_pins=moments,
    )
    # The receipts reply keeps only the policy notices when it replaces the summary.
    kept_notices = [n for n in none_answers.notices if n not in planned.policy_notices]
    return _with_narration_note(
        replace(
            planned,
            plan=plan.model_copy(update={"intents": [rewritten, *plan.intents[1:]]}),
            media_snapshot=final_snapshot,
            policy_notices=(*planned.policy_notices, *kept_notices),
        ),
        chosen,
        final_snapshot,
        final_brief,
        camera_sound=camera_sound,
        closing_quote=closing_quote,
    )


def _moment_decision(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any] | None,
    brief: CreativeBrief | None,
    events: Sequence[tuple[str, Any]],
    *,
    can_ask: bool,
    inspections: Any = None,
) -> Any:
    """KRI-723: the named moments' pins, or the question that gets them (no model call)."""
    from app.services.length_planning import DEFAULT_MOMENT_S  # noqa: PLC0415
    from app.services.named_moments import (  # noqa: PLC0415
        MomentDecision,
        MomentOrderError,
        decide,
        named_moments,
    )

    if not settings.clip_intents_enabled:
        return decide(None, (), can_ask=False, default_window_s=DEFAULT_MOMENT_S)
    ask = brief_moment_ask(brief)
    try:
        moment_set = named_moments(
            strategy, media_snapshot, moment_count=ask.count if ask is not None else None
        )
    except MomentOrderError as exc:
        if can_ask:
            return MomentDecision(recovery=str(exc))
        moment_set = None
    return decide(
        moment_set,
        events,
        can_ask=can_ask,
        default_window_s=(ask.each_s if ask is not None and ask.each_s else DEFAULT_MOMENT_S),
        inspections=inspections,
    )


# The apply-strategy summary holds 1000 characters (``ApplyStrategyArguments``).
_SUMMARY_BUDGET = 1000


def _summary_with_notices(summary: str, notices: Sequence[str]) -> str:
    """The draft summary followed by the server's own notices, inside the summary budget.

    A notice tells the creator something the draft cannot keep quiet (KRI-848: which song,
    and that it is not the genre asked for), so when the two do not fit it is the model's
    own text that is cut, never a notice.
    """
    tail = " ".join(notice for notice in notices if notice)
    if not tail:
        return summary.strip()[:_SUMMARY_BUDGET]
    room = max(0, _SUMMARY_BUDGET - len(tail) - 1)
    head = summary.strip()
    if len(head) > room:
        head = head[:room].rstrip()
    return " ".join(part for part in (head, tail) if part)[:_SUMMARY_BUDGET]


def _with_narration_note(
    planned: PlannedKriaTurn,
    strategy: Mapping[str, Any],
    media_snapshot: dict | None,
    brief: CreativeBrief | None,
    *,
    camera_sound: bool,
    closing_quote: Mapping[str, Any] | None,
) -> PlannedKriaTurn:
    """KRI-585: say what the clips' own sound does in the montage (``kria.narration_notes``).

    Read off the strategy and length plan this draft approves. Best-effort copy: a note
    that cannot be built, or would push the reply past its budget, leaves the reply as
    it was (sentences are dropped from the end first; the sound sentence goes last).
    """
    if not camera_sound:
        return planned
    from app.services.creator_render_contract import build_render_contract  # noqa: PLC0415

    try:
        contract = build_render_contract(
            strategy,
            generation_id="length-planning",
            brief=brief,
            media_snapshot={
                key: value for key, value in (media_snapshot or {}).items() if key != "length_plan"
            },
        )
        seats = spoken_plan(
            strategy,
            media_snapshot,
            brief,
            contract=contract,
            clip_intents_enabled=settings.clip_intents_enabled,
            closing_quote=closing_quote,
        )
        length_plan = (media_snapshot or {}).get("length_plan")
        if not (isinstance(length_plan, Mapping) and length_plan.get("spoken_order")):
            # The phone worker follows only the approved plan's record: without it the
            # words order nothing, so the reply claims nothing.
            seats = None
        parts = narration_note_parts(
            strategy, media_snapshot, seats, clip_intents_enabled=settings.clip_intents_enabled
        )
    except Exception as exc:  # noqa: BLE001 - reply copy must never fail the turn
        log.warning("kria_narration_note_failed", error_type=type(exc).__name__)
        return planned
    plan = planned.plan
    if not parts or not plan.intents:
        return planned
    apply_intent = plan.intents[0]
    arguments = apply_intent.arguments
    summary = with_note(str(arguments.get("summary") or ""), parts, budget=_SUMMARY_BUDGET)
    rewritten = apply_intent.model_copy(update={"arguments": {**arguments, "summary": summary}})
    return replace(
        planned, plan=plan.model_copy(update={"intents": [rewritten, *plan.intents[1:]]})
    )


async def _plan_scoped_turn(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    item: PlanItem,
    persona: Persona,
    user_message: str,
    scope: list[str],
    manual_edits: list[Any],
    editor_state: Any = None,
) -> PlannedKriaTurn:
    """KRI-441: plan a turn the creator scoped to some plan sections.

    ALWAYS editor operations (``[draft.apply_editor_ops, render.request]``): never a strategy
    re-plan, never a brief extraction, and every failure is a reply, not a fall-through to the
    unscoped planner. Enforcement layers 1 and 2 live here (``kria/plan_review.py``); layer 3
    runs at draft compile in ``tasks/kria_runtime._complete_draft_turn``.
    """
    from app.kria import plan_review  # noqa: PLC0415

    # Provider calls release the transaction and expire ORM rows.
    scoped_creator_id, scoped_item_id = persona.user_id, item.id
    # KRI-598: runtime v2 has no guided-proposal step, so it never offers guided voiceover.
    manifest, _ = await resolve_item_creator_context(
        db, item, persona=persona, runs_guided_voiceover=False
    )

    def _planned(plan: KriaTurnPlan) -> PlannedKriaTurn:
        return PlannedKriaTurn(
            plan=plan, manifest_hash=manifest.manifest_hash, context_hash=manifest.context_hash
        )

    def _reply(text: str, value: str = "recovery") -> PlannedKriaTurn:
        return _planned(KriaTurnPlan(mode="respond", turn_value=value, response=text))  # type: ignore[arg-type]

    try:
        target = await _load_editor_target(
            db, thread_id=thread_id, item=item, **_state_kw(editor_state)
        )
    except BriefCoverageError:
        return _reply(
            say(
                en="Your saved request is too large for this update. Your video is unchanged.",
                tr="Kayıtlı isteğin bu güncelleme için çok büyük. Videon değişmedi.",
            )
        )
    if target is None:
        miss = _editor_target_miss.get()
        if miss == "render_in_flight":
            return _reply(
                say(en=_EDITOR_TARGET_IN_FLIGHT_REPLY, tr=_EDITOR_TARGET_IN_FLIGHT_REPLY_TR)
            )
        if miss == "editor_state_stale":
            return _reply(_editor_state_stale_reply())
        return _reply(
            say(
                en="I couldn't open your current video to update it. Try again in a moment.",
                tr="Şu anki videonu güncellemek için açamadım. Birazdan tekrar dene.",
            )
        )
    snapshot = plan_review.scoped_snapshot(target.snapshot, scope)
    if snapshot is None:
        return _reply(plan_review.nothing_in_scope_reply([], scope))

    bars = [b for b in target.snapshot.get("text_bars") or [] if isinstance(b, dict)]
    cue_ids = {
        str(c.get("id"))
        for c in (target.snapshot.get("captions") or {}).get("cues") or []
        if isinstance(c, dict) and c.get("id")
    }
    covered: set[str] = set()
    ops: list[dict] = []
    reply = ""
    if manual_edits:
        try:
            covered = {
                plan_review.edit_section(edit, bars=bars, cue_ids=cue_ids) for edit in manual_edits
            }
            raw = plan_review.manual_edits_to_ops(manual_edits, snapshot)
        except plan_review.ManualEditTargetMissing:
            return _reply(
                say(
                    en=(
                        "One of those lines changed, so I left your video as it was. "
                        "Refresh and try again."
                    ),
                    tr=(
                        "Satırlardan biri değişmiş, o yüzden videonu olduğu gibi bıraktım. "
                        "Yenileyip tekrar dene."
                    ),
                )
            )
        # The SAME coercion the model's ops go through, with no model call.
        ops, detail = plan_review.parse_ops_without_model(raw, snapshot, user_message)
        if not ops:
            return _reply(
                detail
                or say(
                    en="I couldn't apply that edit, so your video is unchanged.",
                    tr="Bu düzenlemeyi uygulayamadım, videon değişmedi.",
                )
            )
        reply = say(
            en=f"Updated {plan_review.scope_phrase(sorted(covered, key=scope.index))}.",
            tr=f"{plan_review.scope_phrase(sorted(covered, key=scope.index))} güncellendi.",
        )

    dropped: list[dict[str, str]] = []
    # Sections flagged without a deterministic edit still need the model (scoped to them).
    model_scope = [section for section in scope if section not in covered]
    if model_scope:
        model_snapshot = plan_review.scoped_snapshot(target.snapshot, model_scope)
        if model_snapshot is None:
            if not ops:
                return _reply(plan_review.nothing_in_scope_reply([], model_scope))
        else:
            await db.rollback()
            response = await run_copilot_turn(
                CopilotTurnBody(
                    message=user_message,
                    turns=target.conversation,
                    snapshot=model_snapshot,
                    client_contract_version=2,
                ),
                job_id=target.job_id,
            )
            kept, dropped = plan_review.filter_ops_to_scope(
                list(response.ops), model_snapshot, model_scope
            )
            if kept:
                ops = [*ops, *kept]
                if not reply:
                    reply = _phone_editor_reply(response.reply)
            elif not ops:
                if dropped:
                    return _reply(plan_review.nothing_in_scope_reply(dropped, model_scope))
                # No op survived: the copilot's own honest answer (question / limit).
                return _reply(
                    response.reply
                    or say(
                        en="That change isn't available for these sections yet.",
                        tr="Bu değişiklik bu bölümler için henüz yapılamıyor.",
                    ),
                    "question" if response.outcome == "clarification" else "recovery",
                )
    note = plan_review.left_alone_note(dropped, scope)
    plan = adapt_editor_action(
        reply=f"{reply} {note}".strip() or say(en="Updated your video.", tr="Videon güncellendi."),
        ops=ops,
        request_render=True,
    )
    if plan.mode == "act":
        plan = plan.model_copy(
            update={
                "diagnostics": {
                    "scope": list(scope),
                    **({"scope_dropped": dropped} if dropped else {}),
                }
            }
        )
    planned = _planned(plan)
    if settings.approval_readiness_for(scoped_creator_id) and plan.mode == "act" and model_scope:
        prior_brief = await load_latest_brief(db, thread_id)
        expected_version = prior_brief.version if prior_brief else 0
        updates, route = await extract_deferred_brief(
            db,
            thread_id=thread_id,
            item_id=scoped_item_id,
            creator_id=scoped_creator_id,
            user_message=user_message,
        )
        if route is None:
            return _reply(
                say(
                    en=(
                        "I need to clarify the requested changes before offering this render. "
                        "Your saved edit is unchanged."
                    ),
                    tr=(
                        "Bu oluşturmayı sunmadan önce istenen değişiklikleri "
                        "netleştirmem gerekiyor. "
                        "Kayıtlı düzenlemen değişmedi."
                    ),
                )
            )
        planned = replace(
            planned,
            brief_updates=updates,
            brief_route="editor_ops",
            brief_expected_version=expected_version,
        )
    return planned


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
    scope: list[str] | None = None,
    manual_edits: list[Any] | None = None,
    batch_questions: bool = False,
) -> PlannedKriaTurn:
    # KRI-802: `batch_questions` = the app that sent this message answers a reply's choice
    # card and clip picker on one turn (`_plan_from_creator_output`).
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
    if scope:
        # KRI-441: a scoped turn is editor operations only; it never reaches the
        # strategy re-plan, the clip-understanding wait or a brief extraction below.
        return await _plan_scoped_turn(
            db,
            thread_id=thread_id,
            item=item,
            persona=persona,
            user_message=user_message,
            scope=scope,
            manual_edits=manual_edits or [],
            editor_state=editor_state,
        )
    changing = await _change_request_turn(db, thread_id)
    if changing is not None:
        return changing
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
    manifest, media_context = await resolve_item_creator_context(
        db, item, persona=persona, runs_guided_voiceover=False
    )
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
    latest_job_failed = False
    latest_job = None
    if item.current_job_id is not None:
        latest_job = await db.get(Job, item.current_job_id)
        latest_job_failed = str(getattr(latest_job, "status", "") or "").endswith("failed")
    latest_variants = [
        row
        for row in (getattr(latest_job, "assembly_plan", None) or {}).get("variants") or []
        if isinstance(row, dict)
    ]
    failed_render = bool(
        latest_job is not None
        and (
            latest_job_failed
            or any(row.get("render_status") == "needs_attention" for row in latest_variants)
        )
    )
    correction_candidate = (
        compose_unmet_with_ops()
        and failed_render
        and not answers_clip_question
        and _fast_path_eligible(user_message)
    )
    text_correction = correction_candidate and _is_text_edit_ask(user_message)
    failed_variant = any(
        row.get("render_status") in {"failed", "needs_attention"} for row in latest_variants
    )
    saved_heads = []
    if (text_correction and not latest_variants) or (
        correction_candidate and not text_correction and failed_variant
    ):
        saved_heads = (
            (
                await db.execute(
                    select(CreatorEditDraft).where(
                        CreatorEditDraft.item_id == item.id,
                        CreatorEditDraft.thread_id == thread_id,
                        CreatorEditDraft.creator_id == creator_id,
                        CreatorEditDraft.base_job_id == latest_job.id,
                        CreatorEditDraft.is_head.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    # A text ask corrects the failed render (staged on its editor head, or refused honestly)
    # only when that render left something to correct: a variant, or editor work built on the
    # job. A render that failed before making any video (Leverkusen M2 10-31: phone plan
    # refused, no `variants` key) leaves only the strategy draft, so the change re-plans like
    # every other follow-up to a failed first render.
    failed_editor_correction = text_correction and (
        bool(latest_variants)
        or any(
            isinstance(head, CreatorEditDraft)
            and (head.snapshot_json or {}).get("kind") == "editor"
            for head in saved_heads
        )
    )
    if correction_candidate and not text_correction and failed_variant:
        failed_editor_correction = any(
            failed_editor_draft_basis(
                head, latest_job, row, thread_id=thread_id, creator_id=creator_id, item_id=item.id
            )
            is not None
            for head in saved_heads
            if isinstance(head, CreatorEditDraft)
            for row in latest_variants
            if row.get("variant_id") == head.variant_key
            and row.get("render_status") in {"failed", "needs_attention"}
        )
    extract_first = (
        brief_on
        and item.current_job_id is not None
        and (not latest_job_failed or failed_editor_correction)
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
    extracted_delegation = False
    extracted_read_ids: tuple[str, ...] = ()
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
                        response=clarification
                        or say(
                            en="What would you like me to revise?",
                            tr="Neyi değiştirmemi istersin?",
                        ),
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                )
            unique_extracted = {
                json.dumps(update.model_dump(mode="json"), sort_keys=True): update
                for result in extraction_outputs
                for update in result.brief_updates
            }
            # KRI-776: the creator's own moment numbers ride on the brief, whatever the
            # extractor's paraphrase kept.
            pre_extracted_updates = carry_moment_ask(
                prior_brief, tuple(unique_extracted.values()), user_message
            )
            extracted_delegation = _declares_delegation(extraction_outputs, pre_extracted_updates)
            extracted_read_ids = extraction_inputs.delegation_read_ids
            effective = apply_updates(
                prior_brief,
                pre_extracted_updates,
                source_turn_id=None,
                creator_text=user_message,
            )
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
                    continuable=True,
                    brief_updates=pre_extracted_updates,
                    # KRI-814: a delegating request paused on analysis is still read.
                    brief_delegated=extracted_delegation,
                    brief_delegation_read_ids=extracted_read_ids,
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
                    if failed_editor_correction and _editor_target_miss.get() == "no_render":
                        _editor_target_miss.set("failed_draft_unavailable")
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
                        brief_delegated=extracted_delegation,
                        brief_delegation_read_ids=extracted_read_ids,
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
            if failed_editor_correction:
                return PlannedKriaTurn(
                    plan=KriaTurnPlan(
                        mode="respond",
                        turn_value="recovery",
                        response=_failed_correction_alternative(),
                    ),
                    manifest_hash=manifest.manifest_hash,
                    context_hash=manifest.context_hash,
                    brief_updates=pre_extracted_updates,
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
                else "request_extraction_provider_failed"
                if _error_honesty_enabled() and _is_provider_fault(exc)
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
                inputs,
                agent_input=inputs.agent_input.model_copy(
                    update={
                        "brief_enabled": False,
                        # KRI-814: the earlier lines were already read by the extraction;
                        # a brief-off Main Creator keeps its original request contract.
                        **({"creator_request": ""} if inputs.delegation_read_ids else {}),
                    }
                ),
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
            **({"batch_questions": True} if batch_questions else {}),
        )

    # KRI-611: the model owns which requirement a change/remove targets, never the
    # concurrency token. Bind its `expected_version` to the brief its prompt showed, as
    # the brief extractor does; apply_updates and the persist fence still refuse work
    # that is genuinely stale. A copied wrong number made "make it more basic" fail.
    # KRI-805: a missing number, and a change naming only the field it changes, bind too.
    unique_updates = {
        json.dumps(update.model_dump(mode="json"), sort_keys=True): update
        for update in (
            pre_extracted_updates
            if extraction_complete
            else _fresh_if_declared(
                prior_brief,
                bind_updates(
                    prior_brief, (update for result in outputs for update in result.brief_updates)
                ),
                outputs,
            )
        )
    }
    # KRI-776: the creator's own moment numbers ride on the brief, whatever the Main
    # Creator's paraphrase kept ("six moments, about 2 seconds each" became a bare order list).
    updates = carry_moment_ask(prior_brief, tuple(unique_updates.values()), user_message)
    updates = await _with_approved_copy(
        db,
        thread_id=thread_id,
        prior=prior_brief,
        updates=updates,
        states=inputs.creative_copy_state or {},
    )
    updates, pin_notices = await _withdraw_stuck_pins(
        db, thread_id=thread_id, prior=prior_brief, updates=updates, user_message=user_message
    )
    if answers_clip_question and updates:
        # KRI-966: a tapped length answer is the server's own option label, already applied by
        # the gate; the model's restatement of it is never the creator's length ask.
        echo_free = without_length_answer_echo(
            prior_brief, updates, await _load_thread_events(db, thread_id)
        )
        if echo_free != updates:
            log.info(
                "kria_length_answer_echo_dropped",
                thread_id=str(thread_id),
                dropped=len(updates) - len(echo_free),
            )
        updates = echo_free
    delegated = (
        extracted_delegation if extraction_complete else _declares_delegation(outputs, updates)
    )
    delegation_read_ids = extracted_read_ids if extraction_complete else inputs.delegation_read_ids
    try:
        effective = apply_updates(
            prior_brief, updates, source_turn_id=None, creator_text=user_message
        )
    except BriefUpdateBatchError as exc:
        # A brief update the server cannot apply is a reading failure, not a context
        # limit. The error texts are fixed server strings, never the creator's words.
        return _request_recovery(
            manifest,
            prior_brief,
            reason="request_extraction_failed",
            cause={**_recovery_cause("apply_updates", exc), "error": str(exc)[:80]},
        )
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
            continuable=True,
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
                brief_delegated=delegated,
                brief_delegation_read_ids=delegation_read_ids,
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
        brief_selects_clips=_brief_selects_clips(effective),
        clip_scope_texts=_brief_clip_scope_texts(effective),
        requested_media=requested_media(effective, (media.media_id for media in manifest.media)),
        describe_clips=wants_clip_descriptions(effective),
        spoken_text=spoken_text_literals(effective),
        effective_brief=effective,
        keeps_sound=sound_asks_unchanged(prior_brief, effective),
        **({"batch_questions": True} if batch_questions else {}),
    )
    if isinstance(output.action, DraftVoiceoverScript):
        # KRI-815: a script draft or revision is not an edit request. "Make it shorter" is
        # about the script, so it must not become a timing requirement on the video, and
        # nothing the brief already holds changes.
        return planned
    return replace(
        planned,
        brief_updates=updates,
        pin_notices=pin_notices,
        # A re-plan that ended up asking a question or failing safe is not an
        # editor plan; only an act plan is held to the router's verdict.
        brief_route=route,
        brief_delegated=delegated,
        brief_delegation_read_ids=delegation_read_ids,
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
    manifest, media_context = await resolve_item_creator_context(
        db, item, persona=persona, runs_guided_voiceover=False
    )
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
    updates = carry_moment_ask(
        prior_brief,
        tuple(update for output in extraction_outputs for update in output.brief_updates),
        user_message,
    )
    effective = apply_updates(prior_brief, updates, source_turn_id=None, creator_text=user_message)
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
