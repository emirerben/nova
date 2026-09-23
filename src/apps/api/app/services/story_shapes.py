"""Deterministic repairs for chat-picked story shapes (KRI-118 lane L3).

``story_shape`` ("day_vlog" / "single_hero") and ``hero_media_id`` are
optional planning hints threaded ``ProposalBrief`` -> ``EditProposalAgentInput``
(see ``app.schemas.edit_proposal`` and ``app.agents.edit_proposal``). The
specialist model is told how to interpret them (``prompts/edit_proposal.txt``),
but -- per the KRI-129 rule ("the creator's prompt wins" / a render contract
must never silently depend on the model complying) -- each shape's geometry
is enforced deterministically here, the same way every other
``repairs.extend(...)`` step in ``app.agents.edit_proposal`` repairs an
authored plan instead of rejecting it.

Called from ``EditProposalAgent.parse()`` alongside the other repair helpers,
after the moment-budget trim so it operates on the final beat list.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from app.schemas.edit_proposal import GUIDED_STORY_MIN_MOMENT_S, MediaRef

if TYPE_CHECKING:
    from app.agents.edit_proposal import (
        DraftStoryBeat,
        EditProposalAgentInput,
        EditProposalAgentOutput,
    )

# Mirrors GUIDED_DRAFT_MEDIA_PER_BEAT in app.agents.edit_proposal -- kept as an
# independent constant to avoid a circular import (that module imports this
# one). Keep the two values in sync.
_MAX_MEDIA_PER_BEAT = 4

DAY_VLOG_TRANSITION_CAP_S = 0.2
DAY_VLOG_MIN_SOURCES = 2
# A small margin so a grown hero share clears the rival strictly, not just
# ties it, after rounding.
_SINGLE_HERO_MARGIN_S = 0.2


class StoryShapeInfeasibleError(ValueError):
    """A story shape's own contract cannot be honored at all (KRI-129: a
    genuine render limit is signaled to the caller, never silently downgraded
    to a generic edit). Mirrors CreatorTextInfeasibleError
    (app.services.edit_direction_planner) -- a plain ValueError subclass the
    task layer catches to fail the attempt with a friendly message, instead
    of falling through to the direction's generic deterministic fallback."""


def usable_media_count(media: list[MediaRef]) -> int:
    """Count sources a beat-based shape could actually use: every image, and
    every video with a positive probed duration.

    Mirrors the "usable source" test `repair_day_vlog` applies to the final
    authored beat list, so `app.tasks.edit_proposal_build.draft_edit_proposal`
    can run the same day_vlog feasibility check BEFORE the specialist agent
    (or the deterministic fallback) is even invoked -- neither knows about
    story_shape, so a plan built from too little footage would otherwise
    silently proceed as a generic edit instead of failing visibly.
    """

    return sum(
        1
        for ref in media
        if ref.kind == "image" or (ref.kind == "video" and float(ref.duration_s or 0.0) > 0)
    )


def _media_attachment_order(input: EditProposalAgentInput) -> dict[str, int]:  # noqa: A002
    """Order media was attached to the item -- the creator's own upload order."""

    return {media.media_id: index for index, media in enumerate(input.media)}


def _has_explicit_pinned_order(input: EditProposalAgentInput) -> bool:  # noqa: A002
    """True when a resolved ORDER clip intent already pins an explicit
    sequence (KRI-127/KRI-129: the creator's own explicit order always wins,
    day_vlog's chronological reorder must never override it)."""

    if not input.clip_intents:
        return False
    return any(
        intent.status == "resolved" and intent.op == "order" for intent in input.clip_intents
    )


def repair_day_vlog(
    output: EditProposalAgentOutput,
    input: EditProposalAgentInput,  # noqa: A002
) -> list[str]:
    """day_vlog contract: chronological (attachment) order, capped transitions.

    Raises StoryShapeInfeasibleError -- a genuine planning-time refusal, not a
    silent downgrade -- when fewer than two usable sources are available.
    `draft_edit_proposal` (app.tasks.edit_proposal_build) already runs this
    same check against the raw media pool via `usable_media_count` before the
    specialist agent is invoked at all; this is defense in depth for the rare
    case where the model's own repairs still leave the authored plan short.
    """

    beats = output.story_beats
    usable_sources = {media_id for beat in beats for media_id in beat.media_ids}
    if len(usable_sources) < DAY_VLOG_MIN_SOURCES:
        raise StoryShapeInfeasibleError("A day vlog needs at least two moments.")

    repairs: list[str] = []
    if not _has_explicit_pinned_order(input):
        order = _media_attachment_order(input)
        fallback_index = len(order)
        beats.sort(
            key=lambda beat: min(
                (order.get(media_id, fallback_index) for media_id in beat.media_ids),
                default=fallback_index,
            )
        )
        repairs.append("day_vlog_reordered_by_attachment")
    for beat in beats:
        transition_s = getattr(beat, "transition_duration_s", None)
        if transition_s is not None and transition_s > DAY_VLOG_TRANSITION_CAP_S:
            beat.transition_duration_s = DAY_VLOG_TRANSITION_CAP_S
            repairs.append("day_vlog_transition_clamped")
    return repairs


def _source_screen_time_s(beats: list[DraftStoryBeat]) -> dict[str, float]:
    """Approximate per-source screen time from beat-level duration weights.

    A beat's duration is a weight shared by every media_id it holds (mirrors
    how guided_story.py scales these weights at render time); split it evenly
    across the beat's own sources so a multi-clip beat does not credit its
    full duration to every member.
    """

    totals: dict[str, float] = defaultdict(float)
    for beat in beats:
        if not beat.media_ids:
            continue
        share = beat.duration_s / len(beat.media_ids)
        for media_id in beat.media_ids:
            totals[media_id] += share
    return totals


def repair_single_hero(
    output: EditProposalAgentOutput,
    input: EditProposalAgentInput,  # noqa: A002
) -> list[str]:
    """single_hero contract: hero is included, opens the edit, and holds the
    largest single share of screen time.

    Deliberately does NOT port the old native archetype's 60%-of-timeline
    floor or "at most 3 supporting clips" cap -- taste, dropped by design.
    """

    hero_id = input.hero_media_id
    beats = output.story_beats
    if not hero_id or not beats:
        return []
    known_media_ids = {media.media_id for media in input.media}
    if hero_id not in known_media_ids:
        return []

    all_used = {media_id for beat in beats for media_id in beat.media_ids}
    if len(all_used) <= 1:
        return ["single_hero_single_clip_fallback"]

    repairs: list[str] = []
    if hero_id not in all_used:
        target = next(
            (beat for beat in beats if len(beat.media_ids) < _MAX_MEDIA_PER_BEAT),
            beats[0],
        )
        target.media_ids.append(hero_id)
        repairs.append("single_hero_included")

    hero_index = next(
        (index for index, beat in enumerate(beats) if hero_id in beat.media_ids), None
    )
    if hero_index is not None and hero_index != 0:
        beats.insert(0, beats.pop(hero_index))
        repairs.append("single_hero_reordered_to_open")

    totals = _source_screen_time_s(beats)
    hero_total = totals.get(hero_id, 0.0)
    rivals = {media_id: total for media_id, total in totals.items() if media_id != hero_id}
    if rivals:
        rival_id, rival_total = max(rivals.items(), key=lambda item: item[1])
        if rival_total >= hero_total:
            deficit = (rival_total - hero_total) + _SINGLE_HERO_MARGIN_S
            donor_beats = sorted(
                (beat for beat in beats if hero_id not in beat.media_ids and beat.media_ids),
                key=lambda beat: beat.duration_s,
                reverse=True,
            )
            receiver_beats = [beat for beat in beats if hero_id in beat.media_ids]
            remaining = deficit
            for beat in donor_beats:
                if remaining <= 1e-9:
                    break
                room = max(0.0, beat.duration_s - GUIDED_STORY_MIN_MOMENT_S)
                take = min(room, remaining)
                if take <= 0:
                    continue
                beat.duration_s = round(beat.duration_s - take, 3)
                remaining -= take
            transferred = deficit - remaining
            if transferred > 1e-9 and receiver_beats:
                per_receiver = transferred / len(receiver_beats)
                for beat in receiver_beats:
                    beat.duration_s = round(beat.duration_s + per_receiver, 3)
                repairs.append("single_hero_share_grown")
            del rival_id
    return repairs


# Short, human-readable sentences for every machine repair code this
# specialist can emit -- both the pre-existing codes (app.agents.edit_proposal
# `repairs.append`/`repairs.extend` call sites) and the story-shape codes
# above. This is the single source of truth other lanes should call rather
# than inventing their own mapping.
_REPAIR_MESSAGES: dict[str, str] = {
    "caption_applied": "Applied your requested caption to its clips",
    "blanked_thought_for_caption": "Removed an on-screen line that conflicted with your caption",
    "clamped_cut_window": "Adjusted a cut to fit inside its source clip",
    "split_beat": "Split an oversized chapter across multiple beats",
    "merged_beat": "Combined two small chapters to fit the beat limit",
    "reordered_adjacent_sources": "Reordered clips to avoid repeating the same source back-to-back",
    "relabelled_roles": "Adjusted a cut's hook/build/payoff role",
    "split_fast_cuts": "Split a cut to fit the target length",
    "accepted_short_fast_montage_total": (
        "Used a slightly shorter total to fit the available footage"
    ),
    "deduped_beat_media": "Removed a duplicate clip from a chapter",
    "blanked_unrequested_thought": "Removed an on-screen caption you didn't ask for",
    "truncated_thought": "Shortened an on-screen caption",
    "blanked_thought": "Removed an unsupported on-screen claim",
    "scaled_durations": "Rescaled chapter lengths to match your target duration",
    "dropped_media_for_duration": "Dropped a clip that didn't fit the target duration",
    "day_vlog_reordered_by_attachment": "Reordered clips into day-vlog (shooting) order",
    "day_vlog_transition_clamped": "Shortened a transition to fit the day-vlog pace",
    "single_hero_included": "Added your hero clip to the edit",
    "single_hero_reordered_to_open": "Moved your hero clip to open the edit",
    "single_hero_share_grown": "Gave your hero clip more screen time than any other clip",
    "single_hero_single_clip_fallback": "Only one usable clip, so this rendered as a simple edit",
}


def humanize_repairs(repair_codes: list[str]) -> list[str]:
    """Map machine repair codes to short, user-facing sentences, in order,
    with duplicates collapsed. An unrecognized code is dropped rather than
    leaked to a creator as a raw internal string."""

    seen: set[str] = set()
    messages: list[str] = []
    for code in repair_codes:
        prefix = code.split(":", 1)[0]
        message = _REPAIR_MESSAGES.get(prefix)
        if message is None or message in seen:
            continue
        seen.add(message)
        messages.append(message)
    return messages
