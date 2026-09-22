"""Direction-specific replanning over an existing analyzed proposal snapshot."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
    maximum_distinct_sources,
    minimum_required_sources,
)
from app.config import settings
from app.schemas.clip_intents import ResolvedClipIntent
from app.schemas.edit_proposal import (
    GUIDED_STORY_MIN_MOMENT_S,
    GUIDED_TITLE_HOLD_S,
    MAX_PROPOSAL_DURATION_S,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    MontageAudioPlan,
    MontageCadenceConstraint,
    StoryBeat,
    VideoReusePolicy,
    canonical_narration_duration_s,
    closing_title_hold_s,
    media_context_group,
    mixed_media_hold_bounds,
    resolve_video_reuse_policy,
    uses_quick_photo_long_video_timing,
)
from app.services.proposal_planning import SemanticPlanningError, plan_edit_proposal

log = structlog.get_logger()

# These are the specialist output limits, not a product preference.  Keep the
# all-media preflight aligned with DraftStoryBeat.media_ids (max_length=4) and
# the persisted EditProposalSnapshot.story_beats ceiling (max_length=20,
# KRI-129: MAX_GUIDED_DRAFT_BEATS in app.agents.edit_proposal must stay
# `<=` this constant).
GUIDED_STORY_MAX_BEATS = 20
GUIDED_STORY_MAX_MEDIA_PER_BEAT = 4
GUIDED_STORY_MAX_MEDIA = GUIDED_STORY_MAX_BEATS * GUIDED_STORY_MAX_MEDIA_PER_BEAT
FAST_MONTAGE_NORMAL_MIN_CUT_FRAMES = int(round(0.8 * 30))
# Render timelines are CFR at 30fps (guided_story.py's _FRAME_S). A capacity
# estimate computed here must leave at least one frame of slack per selected
# source so the strict compiler's own frame-quantized per-beat caps
# (_allocate_beat_windows' `_round_frame` / floor-to-frame clipping) can
# never re-derive a target the real per-beat windows can't reach after
# rounding.
_RENDER_FRAME_S = 1.0 / 30.0
_CAPACITY_EPSILON_S = 1e-6


@dataclass(frozen=True)
class AllMediaCapacity:
    """Deterministic preflight result for an explicit all-media request."""

    guided_feasible: bool
    fast_montage_feasible: bool
    current_fast_target_feasible: bool
    required_fast_duration_s: int | None
    reason: str | None = None


def guided_source_floor_s(ref: Any, min_moment_s: float = GUIDED_STORY_MIN_MOMENT_S) -> float:
    """Screen time one source needs in a story: the minimum moment, or the
    clip's own length when it is shorter. A short clip is never excluded for
    being short -- it plays in full (KRI-129). Mirrors the renderer's
    `guided_story.guided_moment_floor_s`."""

    if ref.kind == "image" or ref.duration_s is None:
        return min_moment_s
    return min(min_moment_s, max(0.0, float(ref.duration_s)))


def assess_all_media_capacity(
    media: Sequence[CadenceCapacityMedia],
    target_duration_s: int | float,
    *,
    story_min_moment_s: float = GUIDED_STORY_MIN_MOMENT_S,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
) -> AllMediaCapacity:
    """Assess all-source coverage using the same source floors as the renderer.

    Guided output is structurally limited to GUIDED_STORY_MAX_MEDIA sources and
    each source needs the guided-story minimum moment.  The fast alternative
    uses the ordinary 0.8s montage floor in 30fps frames (a genuinely short
    source may use the schema's 0.4s absolute floor), then rounds the display
    duration *up* to a whole second.  This makes 33 normal clips require 27s.
    """

    refs = list(media)
    if not refs:
        return AllMediaCapacity(False, False, False, None, "no_media")
    guided_feasible = len(refs) <= GUIDED_STORY_MAX_MEDIA and float(
        target_duration_s
    ) + 0.001 >= sum(guided_source_floor_s(ref, story_min_moment_s) for ref in refs)
    minimum_frames = 0
    maximum_frames = 0
    quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_media_timing)
    # A short video is never left out for being short: any clip the cut schema
    # can express (0.1 s) is usable, and it plays for its own length.
    absolute_minimum_frames = int(round(0.1 * 30))
    for ref in refs:
        if quick_mixed_timing:
            bounds = mixed_media_hold_bounds(ref.kind, mixed_media_timing)
            minimum = int(math.ceil(bounds.minimum_s * 30 - 1e-6))
            maximum = int(math.floor(bounds.maximum_s * 30 + 1e-6))
        elif ref.kind == "image":
            minimum = FAST_MONTAGE_NORMAL_MIN_CUT_FRAMES
            maximum = int(round(1.2 * 30))
        else:
            minimum = FAST_MONTAGE_NORMAL_MIN_CUT_FRAMES
            maximum = 0
        if ref.kind == "video":
            if ref.duration_s is None:
                return AllMediaCapacity(guided_feasible, False, False, None, "missing_duration")
            source_frames = math.floor(float(ref.duration_s) * 30 + 1e-6)
            if source_frames < absolute_minimum_frames:
                return AllMediaCapacity(guided_feasible, False, False, None, "unusable_source")
            minimum = min(minimum, source_frames)
            maximum = min(maximum or source_frames, source_frames)
        if maximum < minimum:
            return AllMediaCapacity(guided_feasible, False, False, None, "unusable_source")
        minimum_frames += minimum
        maximum_frames += maximum
    required_duration_s = math.ceil(minimum_frames / 30)
    maximum_duration_s = maximum_frames / 30
    fast_feasible = (
        3 <= required_duration_s <= MAX_PROPOSAL_DURATION_S
        and required_duration_s <= maximum_duration_s + 1e-6
    )
    target_frames = int(round(float(target_duration_s) * 30))
    current_fast_target_feasible = (
        fast_feasible
        and 3 <= float(target_duration_s) <= MAX_PROPOSAL_DURATION_S
        and minimum_frames <= target_frames <= maximum_frames
    )
    return AllMediaCapacity(
        guided_feasible,
        fast_feasible,
        current_fast_target_feasible,
        required_duration_s if fast_feasible else None,
        None if fast_feasible else "duration_over_limit",
    )


class CreatorTextInfeasibleError(ValueError):
    """The creator's exact shot labels cannot be placed on the available media."""


class CadenceCapacityMedia(Protocol):
    """Structural media contract shared by creator manifests and proposals."""

    media_id: str
    kind: str
    duration_s: float | None


def round_robin_capacity_s(
    media: Sequence[CadenceCapacityMedia], cadence: MontageCadenceConstraint
) -> float:
    """Return the longest balanced cadence supported without source reuse."""

    by_id = {ref.media_id: ref for ref in media}
    capacities: list[int] = []
    for media_id in cadence.source_media_ids:
        ref = by_id.get(media_id)
        if ref is None or ref.kind != "video" or ref.duration_s is None:
            return 0.0
        capacities.append(math.floor((float(ref.duration_s) + 0.001) / cadence.cut_duration_s))
    cycles = min(capacities, default=0)
    return cycles * len(cadence.source_media_ids) * cadence.cut_duration_s


def _ranked_non_overlapping_windows(
    ref: MediaRef, *, count: int, cut_duration_s: float
) -> list[tuple[float, float]]:
    """Choose cadence-aligned windows from strongest moments, then source order.

    Keeping every candidate on the source's cadence grid guarantees that
    selecting a strong window cannot fragment otherwise usable footage.  That
    keeps this allocator consistent with ``round_robin_capacity_s`` even when
    an analyzed moment starts halfway through a cadence interval.
    """

    duration_s = float(ref.duration_s or 0.0)
    ranked_moments: list[tuple[float, float, float]] = []
    for moment in (ref.analysis or {}).get("best_moments") or []:
        if not isinstance(moment, dict):
            continue
        try:
            start_s = max(0.0, float(moment.get("start_s", 0.0)))
            end_s = min(duration_s, float(moment.get("end_s", 0.0)))
        except (TypeError, ValueError):
            continue
        if end_s - start_s + 0.001 >= cut_duration_s:
            ranked_moments.append((_moment_energy(moment), start_s, end_s))
    ranked_moments.sort(key=lambda value: (-value[0], value[1]))

    total_slots = math.floor((duration_s + 0.001) / cut_duration_s)
    selected_slots: list[int] = []
    selected_slot_set: set[int] = set()

    def try_select(slot_index: int) -> bool:
        if slot_index < 0 or slot_index >= total_slots or slot_index in selected_slot_set:
            return False
        selected_slots.append(slot_index)
        selected_slot_set.add(slot_index)
        return len(selected_slots) == count

    # Prefer full cadence windows contained by the strongest analyzed moments.
    # Stop at the exact requested count so even multi-hour footage remains
    # bounded by the render contract rather than source duration.
    for _energy, moment_start_s, moment_end_s in ranked_moments:
        first_slot = math.ceil((moment_start_s - 0.001) / cut_duration_s)
        last_slot = math.floor((moment_end_s - cut_duration_s + 0.001) / cut_duration_s)
        for slot_index in range(first_slot, last_slot + 1):
            if try_select(slot_index):
                break
        if len(selected_slots) == count:
            break

    if len(selected_slots) < count:
        for slot_index in range(total_slots):
            if try_select(slot_index):
                break

    if len(selected_slots) == count:
        return [
            (
                round(slot_index * cut_duration_s, 3),
                round((slot_index + 1) * cut_duration_s, 3),
            )
            for slot_index in selected_slots
        ]
    raise ValueError("round-robin cadence cannot allocate enough distinct source windows")


def deterministic_round_robin_cuts(
    media: list[MediaRef],
    duration_s: int | float,
    cadence: MontageCadenceConstraint,
) -> list[FastMontageCut]:
    """Compile exact creator-requested cadence from ranked source windows."""

    cut_count_float = float(duration_s) / cadence.cut_duration_s
    cut_count = round(cut_count_float)
    if abs(cut_count_float - cut_count) > 0.001 or cut_count % len(cadence.source_media_ids):
        raise ValueError("round-robin target must contain complete exact cadence cycles")
    by_id = {ref.media_id: ref for ref in media}
    per_source_count = cut_count // len(cadence.source_media_ids)
    windows: dict[str, list[tuple[float, float]]] = {}
    for media_id in cadence.source_media_ids:
        ref = by_id.get(media_id)
        if ref is None:
            raise ValueError("round-robin cadence references unavailable media")
        unique_count = min(
            per_source_count,
            math.floor((float(ref.duration_s or 0.0) + 0.001) / cadence.cut_duration_s),
        )
        if unique_count < per_source_count and cadence.reuse_policy == "no_repeat":
            raise ValueError("round-robin cadence exceeds non-repeating source capacity")
        ranked = _ranked_non_overlapping_windows(
            ref, count=max(1, unique_count), cut_duration_s=cadence.cut_duration_s
        )
        windows[media_id] = [ranked[index % len(ranked)] for index in range(per_source_count)]
    source_offsets = {media_id: 0 for media_id in cadence.source_media_ids}
    cuts: list[FastMontageCut] = []
    for index in range(cut_count):
        media_id = cadence.source_media_ids[index % len(cadence.source_media_ids)]
        window_index = source_offsets[media_id]
        source_offsets[media_id] += 1
        start_s, end_s = windows[media_id][window_index]
        cuts.append(
            FastMontageCut(
                cut_id=f"cadence-cut-{index + 1}",
                media_id=media_id,
                source_start_s=start_s,
                source_end_s=end_s,
                output_duration_s=cadence.cut_duration_s,
                role="hook" if index == 0 else "payoff" if index == cut_count - 1 else "build",
                transition="none",
                beat_align=False,
            )
        )
    return cuts


def _moment_energy(moment: dict) -> float:
    raw = moment.get("energy", 0)
    if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
        return float(raw)
    return {"low": 2.0, "medium": 5.0, "high": 8.0}.get(str(raw).casefold(), 0.0)


def _ranked_fast_media(media: list[MediaRef]) -> list:
    def score(ref) -> float:  # noqa: ANN001
        moments = (ref.analysis or {}).get("best_moments")
        energies = [_moment_energy(moment) for moment in moments or [] if isinstance(moment, dict)]
        return max(energies, default=0.0)

    return sorted(
        enumerate(media),
        key=lambda row: (-score(row[1]), row[0]),
    )


def clamp_fast_montage_target_duration_s(
    media: list[MediaRef],
    duration_s: int | float,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
    video_reuse_policy: VideoReusePolicy = "once",
) -> int | float:
    """Clamp to one continuous appearance per source unless reuse was requested.

    Typed mixed-media holds still apply. Explicit distinct-window requests
    retain their adjacency-aware capacity calculation; explicit loops may
    exceed the unique footage duration. Saved snapshots are not rewritten.
    """

    requested = max(3, min(MAX_PROPOSAL_DURATION_S, duration_s))
    if video_reuse_policy == "allow_repeat":
        return requested
    if video_reuse_policy == "once":
        capacities = [
            min(
                float(ref.duration_s or 0),
                mixed_media_hold_bounds("video", mixed_media_timing).maximum_s,
            )
            if ref.kind == "video" and uses_quick_photo_long_video_timing(mixed_media_timing)
            else float(ref.duration_s or 0)
            if ref.kind == "video"
            else mixed_media_hold_bounds("image", mixed_media_timing).maximum_s
            if uses_quick_photo_long_video_timing(mixed_media_timing)
            else 1.2
            for ref in media
        ]
        # Use the same per-source frame budget as deterministic_fast_cuts.
        # Whole-second flooring used to discard valid fractional footage.
        capacity = round(
            sum(math.floor((value + 0.001) * 30 + 1e-6) for value in capacities) / 30, 6
        )
        if capacity < 3 - 0.001:
            raise ValueError("fast montage has less than the minimum 3s without repeating videos")
        return min(requested, capacity)
    if not uses_quick_photo_long_video_timing(mixed_media_timing):
        return requested

    minimum_video_s = 0.1 if uses_quick_photo_long_video_timing(mixed_media_timing) else 0.4
    eligible = [
        ref
        for ref in media
        if ref.kind == "image" or float(ref.duration_s or 0.0) >= minimum_video_s
    ]
    capacities: list[float] = []
    minimum_cuts: list[int] = []
    for ref in eligible:
        if ref.kind == "image":
            capacities.append(mixed_media_hold_bounds("image", mixed_media_timing).maximum_s)
            minimum_cuts.append(1)
            continue
        source_s = max(0.0, float(ref.duration_s or 0.0))
        capacities.append(source_s)
        minimum_cuts.append(max(1, math.ceil(source_s / 3.0)))

    # The compiler forbids adjacent cuts from the same source. If one source
    # needs more windows than every other source can separate, trim only that
    # source's usable capacity to the largest schedulable number of 3s windows.
    # Iterate because trimming one source also changes the separator budget for
    # another pathological source set.
    for _ in range(len(eligible)):
        changed = False
        total_cuts = sum(minimum_cuts)
        for index, cut_count in enumerate(minimum_cuts):
            allowed = total_cuts - cut_count + 1
            if cut_count <= allowed:
                continue
            capacities[index] = min(capacities[index], 3.0 * allowed)
            minimum_cuts[index] = max(1, math.ceil(capacities[index] / 3.0))
            changed = True
            total_cuts = sum(minimum_cuts)
        if not changed:
            break
    capacity_s = sum(capacities)
    if capacity_s < 3.0 - 0.001:
        raise ValueError(
            "mixed-media fast montage has less than the minimum 3s of usable source capacity"
        )
    return min(requested, max(3, math.floor(capacity_s * 30 + 1e-6) / 30))


def _context_group(ref: MediaRef) -> str:
    analysis = ref.analysis or {}
    return media_context_group(
        ref.user_context,
        analysis.get("subject"),
        analysis.get("description"),
        analysis.get("on_screen_text"),
    )


def _photo_run_chunks(
    rows: list[tuple[MediaRef, int, int]], *, separator_count: int
) -> list[list[tuple[MediaRef, int, int]]]:
    """Split photos into visible runs without creating singleton leftovers."""

    if len(rows) <= 2:
        return [rows] if rows else []
    chunk_count = min(math.ceil(len(rows) / 5), max(1, separator_count + 1))
    base, extra = divmod(len(rows), chunk_count)
    sizes = [base + (1 if index < extra else 0) for index in range(chunk_count)]
    chunks: list[list[tuple[MediaRef, int, int]]] = []
    cursor = 0
    for size in sizes:
        chunks.append(rows[cursor : cursor + size])
        cursor += size
    return chunks


def _weave_photo_runs(
    rows: list[tuple[MediaRef, int, int]],
    *,
    photo_edge: str | None = None,
) -> list[tuple[MediaRef, int, int]]:
    """Keep photos in 3–5 shot runs while letting video moments breathe."""

    photos = [row for row in rows if row[0].kind == "image"]
    videos = [row for row in rows if row[0].kind == "video"]
    if not photos or not videos:
        return rows
    chunks = _photo_run_chunks(photos, separator_count=len(videos))
    section_count = len(chunks) + 1
    video_sections: list[list[tuple[MediaRef, int, int]]] = [[] for _ in range(section_count)]
    separator_count = max(0, len(chunks) - 1)
    for index, row in enumerate(videos[:separator_count]):
        video_sections[index + 1].append(row)
    if photo_edge == "start":
        # Leave the leading section empty so this chapter's photo block can
        # join the previous chapter's trailing photos without crossing the
        # requested semantic chapter boundary.
        section_order = [len(chunks), *range(1, len(chunks))]
    elif photo_edge == "end":
        # Symmetric pairing for a following photo-first chapter.
        section_order = [0, *range(1, len(chunks))]
    else:
        section_order = [0, len(chunks), *range(1, len(chunks))]
    for index, row in enumerate(videos[separator_count:]):
        video_sections[section_order[index % len(section_order)]].append(row)
    ordered: list[tuple[MediaRef, int, int]] = []
    for index, chunk in enumerate(chunks):
        ordered.extend(video_sections[index])
        ordered.extend(chunk)
    ordered.extend(video_sections[-1])
    return ordered


def _group_reservations_by_context(
    rows: list[tuple[MediaRef, int, int]],
    profile: MixedMediaTimingProfile,
) -> list[tuple[MediaRef, int, int]]:
    """Build contiguous semantic chapters in creator-requested order."""

    grouped: dict[str, list[tuple[MediaRef, int, int]]] = {}
    encountered: list[str] = []
    for row in rows:
        group = _context_group(row[0])
        # A context-only beach still belongs with the explicitly requested
        # beach-volleyball chapter.  The media analyzer often describes
        # scoreboards, sand, or spectators without repeating the sport name.
        if group == "beach_context" and "beach_volleyball" in profile.sequence_group_order:
            group = "beach_volleyball"
        if group not in grouped:
            grouped[group] = []
            encountered.append(group)
        grouped[group].append(row)
    requested = [group for group in profile.sequence_group_order if group in grouped]
    order = requested + [group for group in encountered if group not in requested]
    chapters = [(group, grouped[group]) for group in order]
    photo_edges: dict[int, str] = {}
    pending_photo_chapter: int | None = None
    for index, (_group, chapter) in enumerate(chapters):
        if not any(row[0].kind == "image" for row in chapter):
            pending_photo_chapter = None
            continue
        if pending_photo_chapter is None:
            pending_photo_chapter = index
            continue
        photo_edges[pending_photo_chapter] = "end"
        photo_edges[index] = "start"
        pending_photo_chapter = None

    result: list[tuple[MediaRef, int, int]] = []
    for index, (_group, chapter) in enumerate(chapters):
        result.extend(
            _weave_photo_runs(chapter, photo_edge=photo_edges.get(index))
            if profile.image_grouping == "runs"
            else chapter
        )
    return result


def deterministic_fast_cuts(
    media: list[MediaRef],
    duration_s: int | float,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
    montage_cadence: MontageCadenceConstraint | None = None,
    *,
    narration_duration_s: float | None = None,
    required_media_ids: Sequence[str] | None = None,
    video_reuse_policy: VideoReusePolicy = "once",
) -> list[FastMontageCut]:
    """Build a strict source-aware montage when the semantic planner is invalid.

    The model still owns the normal creative path. This deterministic compiler
    prevents arithmetic/schema drift from turning an explicit direction change
    into a user-visible failure. A video appears once by default. Explicit
    distinct-window requests may revisit a source; explicit loops may reuse
    its frames. Images remain bounded by their one-shot hold capacity.
    """

    if narration_duration_s is not None and (
        montage_cadence is not None or not uses_quick_photo_long_video_timing(mixed_media_timing)
    ):
        raise ValueError("narrated fallback requires the mixed-media timing contract")
    required_ids = set(required_media_ids or ())
    if required_ids - {ref.media_id for ref in media}:
        raise ValueError("fast montage fallback is missing required media")
    if required_ids:
        media = [ref for ref in media if ref.media_id in required_ids]
    if montage_cadence is not None:
        return deterministic_round_robin_cuts(media, duration_s, montage_cadence)

    quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_media_timing)
    # Eligibility is "the cut schema can express it" (0.1 s); a clip shorter
    # than the ordinary 0.4 s cut floor plays for its own length.
    minimum_video_s = 0.1
    eligible = [
        ref
        for ref in media
        if ref.kind == "image" or float(ref.duration_s or 0.0) >= minimum_video_s
    ]
    if not eligible:
        raise ValueError("fast montage fallback found no usable media")
    eligible_ids = {ref.media_id for ref in eligible}
    if required_ids - eligible_ids:
        raise ValueError("fast montage fallback cannot use every required source safely")
    ranked = [ref for _, ref in _ranked_fast_media(media) if ref.media_id in eligible_ids]
    # A recording owns its full frame budget. The music montage clamp and
    # three-second video ceiling can otherwise make a feasible all-media
    # narration fail (or silently shorten it) when the specialist rejects a draft.
    target_duration_s = (
        canonical_narration_duration_s(narration_duration_s)
        if narration_duration_s is not None
        else clamp_fast_montage_target_duration_s(
            media, duration_s, mixed_media_timing, video_reuse_policy
        )
    )
    # Render timelines are CFR at 30fps. Allocate in frames rather than
    # milliseconds so fallback source windows are always frame aligned.
    fps = 30
    target_frames = int(round(target_duration_s * fps))
    source_capacity_frames = {
        ref.media_id: (
            (
                int(
                    math.floor(
                        mixed_media_hold_bounds(ref.kind, mixed_media_timing).maximum_s * fps + 1e-6
                    )
                )
                if quick_mixed_timing
                else int(round(1.2 * fps))
            )
            if ref.kind == "image"
            else int(math.floor((float(ref.duration_s or 0.0) + 0.001) * fps + 1e-6))
        )
        for ref in ranked
    }
    minimum_supported_frames = min(
        round(
            max(
                minimum_video_s if ref.kind == "video" else 0.0,
                mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s,
            )
            * fps
        )
        for ref in ranked
    )
    if max(source_capacity_frames.values()) < (
        minimum_supported_frames if quick_mixed_timing else int(round(0.8 * fps))
    ):
        raise ValueError("fast montage fallback found no source supporting a primary cut")
    required_sources = (
        len(required_ids)
        if required_ids
        else maximum_distinct_sources(
            eligible,
            target_duration_s=target_duration_s,
            mixed_media_timing=mixed_media_timing,
        )
        if quick_mixed_timing
        else minimum_required_sources(len(eligible))
    )
    available_kinds = {ref.kind for ref in eligible}
    image_ids = {ref.media_id for ref in eligible if ref.kind == "image"}
    available_image_count = len(image_ids)
    available_video_count = len(eligible) - available_image_count
    required_image_sources = (
        min(
            available_image_count,
            required_sources - (1 if available_video_count else 0),
        )
        if quick_mixed_timing
        else 0
    )
    rank = {ref.media_id: index for index, ref in enumerate(ranked)}
    remaining_frames = dict(source_capacity_frames)
    if video_reuse_policy == "allow_repeat":
        for ref in ranked:
            if ref.kind == "video":
                remaining_frames[ref.media_id] = target_frames
    reservations: list[tuple[MediaRef, int, int]] = []
    used_ids: set[str] = set()
    used_kinds: set[str] = set()
    previous_id: str | None = None
    last_pick_was_new = False
    minimum_total_frames = 0
    maximum_total_frames = 0
    while len(reservations) < 80:
        if (
            maximum_total_frames >= target_frames
            and len(used_ids) >= required_sources
            and used_kinds == available_kinds
        ):
            break
        candidates = [
            ref
            for ref in ranked
            if (ref.media_id != previous_id or video_reuse_policy == "allow_repeat")
            and (video_reuse_policy != "once" or ref.media_id not in used_ids)
            and remaining_frames[ref.media_id]
            >= (
                round(
                    (
                        mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s
                        if ref.kind == "image"
                        else (
                            mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s
                            if float(ref.duration_s or 0.0)
                            >= mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s
                            else minimum_video_s
                        )
                    )
                    * fps
                )
                if quick_mixed_timing
                else min(int(round(0.4 * fps)), source_capacity_frames.get(ref.media_id, 0))
                if ref.kind == "video"
                else int(round(0.4 * fps))
            )
        ]
        if len(used_ids) < required_sources and (
            quick_mixed_timing or not reservations or not last_pick_was_new
        ):
            unseen = [ref for ref in candidates if ref.media_id not in used_ids]
            if unseen:
                candidates = unseen
        missing_kinds = available_kinds - used_kinds
        if missing_kinds:
            missing_kind_candidates = [ref for ref in candidates if ref.kind in missing_kinds]
            if missing_kind_candidates:
                candidates = missing_kind_candidates
        # With a single available photo, retain the legacy ranking so a long
        # video can be split around that photo. Once multiple photos exist,
        # reserve the requested group before video capacity can starve it.
        if (
            quick_mixed_timing
            and required_image_sources > 1
            and len(used_ids & image_ids) < required_image_sources
        ):
            unseen_images = [
                ref for ref in candidates if ref.kind == "image" and ref.media_id not in used_ids
            ]
            if unseen_images:
                candidates = unseen_images
        candidates = [
            ref
            for ref in candidates
            if minimum_total_frames
            + min(
                remaining_frames[ref.media_id],
                round(mixed_media_hold_bounds(ref.kind, mixed_media_timing).minimum_s * fps)
                if quick_mixed_timing
                else int(round(0.8 * fps)),
            )
            <= target_frames
        ]
        if not candidates:
            break
        ref = min(
            candidates,
            key=lambda candidate: (
                -remaining_frames[candidate.media_id],
                rank[candidate.media_id],
            ),
        )
        if quick_mixed_timing:
            bounds = mixed_media_hold_bounds(ref.kind, mixed_media_timing)
            preferred_minimum_frames = round(bounds.minimum_s * fps)
            preferred_maximum_frames = round(bounds.maximum_s * fps)
            if narration_duration_s is not None and ref.kind == "video":
                preferred_maximum_frames = remaining_frames[ref.media_id]
        else:
            preferred_minimum_frames = int(round(0.8 * fps))
            preferred_maximum_frames = int(round(1.2 * fps))
        if video_reuse_policy == "once" and ref.kind == "video" and not quick_mixed_timing:
            preferred_maximum_frames = source_capacity_frames[ref.media_id]
        maximum_frames = min(
            preferred_maximum_frames,
            remaining_frames[ref.media_id],
            source_capacity_frames[ref.media_id],
        )
        minimum_frames = min(maximum_frames, preferred_minimum_frames)
        was_new = ref.media_id not in used_ids
        reservations.append((ref, minimum_frames, maximum_frames))
        remaining_frames[ref.media_id] -= maximum_frames
        minimum_total_frames += minimum_frames
        maximum_total_frames += maximum_frames
        used_ids.add(ref.media_id)
        used_kinds.add(ref.kind)
        previous_id = ref.media_id
        last_pick_was_new = was_new
    if (
        maximum_total_frames < target_frames
        or minimum_total_frames > target_frames
        or len(used_ids) < required_sources
        or used_kinds != available_kinds
        or len(used_ids & image_ids) < required_image_sources
    ):
        raise ValueError("fast montage fallback cannot allocate distinct source windows safely")

    if quick_mixed_timing and mixed_media_timing is not None:
        image_reservations = [row for row in reservations if row[0].kind == "image"]
        video_reservations = [row for row in reservations if row[0].kind == "video"]
        if image_reservations and video_reservations:
            if mixed_media_timing.sequence_grouping == "sport_context":
                candidate = _group_reservations_by_context(reservations, mixed_media_timing)
            elif mixed_media_timing.image_grouping == "runs":
                candidate = _weave_photo_runs(reservations)
            else:
                candidate = []
                images_per_group = max(
                    1, math.ceil(len(image_reservations) / len(video_reservations))
                )
                image_index = 0
                for video_row in video_reservations:
                    candidate.append(video_row)
                    group_end = min(image_index + images_per_group, len(image_reservations))
                    candidate.extend(image_reservations[image_index:group_end])
                    image_index = group_end
                candidate.extend(image_reservations[image_index:])
            # Reordering a repeated source can accidentally make two windows
            # adjacent. Preserve the compiler's original safe order if the
            # requested grouping cannot satisfy that hard render invariant.
            if all(
                left[0].media_id != right[0].media_id
                for left, right in zip(candidate, candidate[1:], strict=False)
            ):
                reservations = candidate

    remaining_target_frames = target_frames - minimum_total_frames
    scheduled: list[tuple[MediaRef, int, int]] = []
    consumed_frames = {ref.media_id: 0 for ref in ranked}
    for ref, minimum_frames, maximum_frames in reservations:
        extra_frames = min(remaining_target_frames, maximum_frames - minimum_frames)
        duration_frames = minimum_frames + extra_frames
        remaining_target_frames -= extra_frames
        start_frames = 0 if video_reuse_policy == "allow_repeat" else consumed_frames[ref.media_id]
        consumed_frames[ref.media_id] += duration_frames
        scheduled.append((ref, duration_frames, start_frames))
    if remaining_target_frames:
        raise ValueError("fast montage fallback could not preserve the target duration")

    cuts: list[FastMontageCut] = []
    for index, (ref, duration_frames, start_frames) in enumerate(scheduled):
        exact_cut_duration_s = duration_frames / fps
        start_s = round(start_frames / fps, 3) if ref.kind == "video" else 0.0
        end_s = round(start_s + exact_cut_duration_s, 3)
        cuts.append(
            FastMontageCut(
                cut_id=f"fallback-cut-{index + 1}",
                media_id=ref.media_id,
                source_start_s=start_s,
                source_end_s=end_s,
                output_duration_s=exact_cut_duration_s,
                role="hook" if index == 0 else "payoff" if index == len(scheduled) - 1 else "build",
                transition="none",
                beat_align=False,
            )
        )
    if abs(sum(cut.output_duration_s for cut in cuts) - target_duration_s) > 0.001:
        raise ValueError("fast montage fallback could not preserve the target duration")
    return cuts


def _guided_fallback_order(media: list[MediaRef]) -> list[MediaRef]:
    """Renderable media, longest videos first, interleaved with photos."""

    eligible = [
        ref
        for ref in media
        # Any video with frames to show is renderable; short ones play in full.
        if ref.kind == "image" or float(ref.duration_s or 0.0) > 0.0
    ]
    images = [ref for ref in eligible if ref.kind == "image"]
    videos = sorted(
        (ref for ref in eligible if ref.kind == "video"),
        key=lambda ref: -float(ref.duration_s or 0.0),
    )
    ordered: list[MediaRef] = []
    while images or videos:
        if videos:
            ordered.append(videos.pop(0))
        if images:
            ordered.append(images.pop(0))
    return ordered


# Connectives and day/shot vocabulary that say nothing about what a shot shows.
_LABEL_MATCH_STOPWORDS = frozenset(
    {
        "and", "the", "for", "from", "into", "with", "day", "days", "week", "part",
        "shot", "clip", "video", "photo", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    }
)  # fmt: skip
# Extra seconds a video must hold beyond its beat so crossfade overlap and
# frame rounding never push the compiler past the real source.
_LABEL_VIDEO_CAPACITY_MARGIN_S = 0.25


def _label_match_tokens(text: str) -> set[str]:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    tokens: set[str] = set()
    for token in re.findall(r"\w+", plain):
        if len(token) < 3 or token.isdigit() or token in _LABEL_MATCH_STOPWORDS:
            continue
        tokens.add(token[:-1] if len(token) > 3 and token.endswith("s") else token)
    return tokens


def _media_match_tokens(ref: MediaRef) -> set[str]:
    analysis = ref.analysis if isinstance(ref.analysis, dict) else {}
    return _label_match_tokens(
        " ".join(
            str(value or "")
            for value in (
                ref.user_context,
                analysis.get("subject"),
                analysis.get("description"),
                analysis.get("on_screen_text"),
            )
        )
    )


def deterministic_labeled_beats(
    media: list[MediaRef],
    duration_s: int | float,
    *,
    shot_labels: Sequence[str],
    opening_title: str | None = None,
    opening_title_duration_s: float | None = None,
    closing_title: str | None = None,
    required_media_ids: Sequence[str] | None = None,
) -> list[StoryBeat]:
    """Place the creator's exact shot labels on renderable media.

    Recovery for a failed planner must never substitute generic copy for
    confirmed creator words. Each label gets its own shot (the best subject
    match, else the next source in fallback order); a separate unlabeled hold
    beat carries the opening/closing title only when a spare source exists.
    With ``required_media_ids`` (a brief scoped to all or selected media) only
    those sources are used, and any left after one per label and title hold
    join the labeled shot they match best: the renderer refuses a timeline that
    drops a required source (plan item 5016d555).
    Raises CreatorTextInfeasibleError when the labels cannot fit the media.
    """

    labels = list(shot_labels)
    if not labels:
        raise CreatorTextInfeasibleError("no creator shot labels to place")
    ordered = _guided_fallback_order(media)
    required_ids = set(required_media_ids or ())
    if required_ids:
        if required_ids - {ref.media_id for ref in ordered}:
            raise CreatorTextInfeasibleError(
                "guided story fallback cannot use every required source safely"
            )
        ordered = [ref for ref in ordered if ref.media_id in required_ids]
    if not ordered:
        raise CreatorTextInfeasibleError("guided story fallback found no usable media")
    target_s = float(max(3, min(MAX_PROPOSAL_DURATION_S, duration_s)))
    if required_ids and target_s + 0.001 < sum(guided_source_floor_s(ref) for ref in ordered):
        raise CreatorTextInfeasibleError(
            f"{len(ordered)} required sources need at least {GUIDED_STORY_MIN_MOMENT_S:g}s each"
        )
    title_hold_s = float(opening_title_duration_s or GUIDED_TITLE_HOLD_S) if opening_title else 0.0
    closing_hold_s = closing_title_hold_s(target_s) if closing_title else 0.0
    shot_s = (target_s - title_hold_s - closing_hold_s) / len(labels)
    if shot_s < GUIDED_STORY_MIN_MOMENT_S - 1e-6:
        raise CreatorTextInfeasibleError(
            f"{len(labels)} shot labels need at least {GUIDED_STORY_MIN_MOMENT_S:g}s each"
        )
    # Assign shots as if the title holds play over the first/last labeled shot
    # (the worst case for clip length); spare sources can take them over below.
    label_seconds = [shot_s] * len(labels)
    label_seconds[0] += title_hold_s
    label_seconds[-1] += closing_hold_s

    def capacity_s(ref: MediaRef) -> float:
        return math.inf if ref.kind == "image" else float(ref.duration_s or 0.0)

    order_index = {ref.media_id: index for index, ref in enumerate(ordered)}
    media_tokens = {ref.media_id: _media_match_tokens(ref) for ref in ordered}
    used: set[str] = set()
    assigned: list[MediaRef] = []
    for label, seconds in zip(labels, label_seconds, strict=True):
        needed_s = seconds + _LABEL_VIDEO_CAPACITY_MARGIN_S
        label_tokens = _label_match_tokens(label)
        candidates = [
            ref for ref in ordered if ref.media_id not in used and capacity_s(ref) >= needed_s
        ]
        if not candidates:
            # A photo can hold any length and may carry more than one label;
            # a video may appear only once.
            candidates = [ref for ref in ordered if ref.kind == "image"]
        if not candidates:
            raise CreatorTextInfeasibleError(
                f"{len(labels)} shot labels need {len(labels)} usable clips or a photo"
            )
        choice = max(
            candidates,
            key=lambda ref: (
                len(label_tokens & media_tokens[ref.media_id]),
                -order_index[ref.media_id],
            ),
        )
        used.add(choice.media_id)
        assigned.append(choice)

    spares = [ref for ref in ordered if ref.media_id not in used]
    intro_ref = None
    if opening_title and title_hold_s >= GUIDED_STORY_MIN_MOMENT_S:
        intro_ref = next(
            (
                ref
                for ref in spares
                if capacity_s(ref) >= title_hold_s + _LABEL_VIDEO_CAPACITY_MARGIN_S
            ),
            None,
        )
    outro_ref = None
    if closing_title and closing_hold_s >= GUIDED_STORY_MIN_MOMENT_S:
        outro_ref = next(
            (
                ref
                for ref in spares
                if (intro_ref is None or ref.media_id != intro_ref.media_id)
                and capacity_s(ref) >= closing_hold_s + _LABEL_VIDEO_CAPACITY_MARGIN_S
            ),
            None,
        )
    if intro_ref is not None:
        label_seconds[0] -= title_hold_s
    if outro_ref is not None:
        label_seconds[-1] -= closing_hold_s

    labeled_media = [[ref] for ref in assigned]
    placed = used | {ref.media_id for ref in (intro_ref, outro_ref) if ref is not None}
    for ref in ordered:
        if not required_ids or ref.media_id in placed:
            continue
        open_beats = [
            index
            for index, beat_refs in enumerate(labeled_media)
            if len(beat_refs) < GUIDED_STORY_MAX_MEDIA_PER_BEAT
        ]
        if not open_beats:
            raise CreatorTextInfeasibleError(
                f"{len(labels)} shot labels cannot carry {len(ordered)} required sources"
            )
        # Best subject match, then the least crowded and earliest labeled shot.
        beat_index = max(
            open_beats,
            key=lambda index, tokens=media_tokens[ref.media_id]: (
                len(_label_match_tokens(labels[index]) & tokens),
                -len(labeled_media[index]),
                -index,
            ),
        )
        labeled_media[beat_index].append(ref)
        placed.add(ref.media_id)

    raw: list[tuple[str, str, list[MediaRef], float, bool]] = []
    if intro_ref is not None:
        raw.append(("Opening", "", [intro_ref], title_hold_s, False))
    for label, beat_refs, seconds in zip(labels, labeled_media, label_seconds, strict=True):
        raw.append((label[:80], label, beat_refs, seconds, True))
    if outro_ref is not None:
        raw.append(("Closing", "", [outro_ref], closing_hold_s, False))
    # StoryBeat.duration_s is a weight the compiler scales to the target;
    # keep the ratios while respecting the schema bounds (clips may now run
    # their full length, so only the schema's overall ceiling applies here).
    scale = min(1.0, MAX_PROPOSAL_DURATION_S / max(item[3] for item in raw))
    return [
        StoryBeat(
            beat_id=f"creator-label-beat-{index + 1}",
            topic=topic,
            thought=thought,
            thought_source="user" if labeled else "ai_draft",
            media_ids=[ref.media_id for ref in beat_refs],
            layout="fullscreen",
            duration_s=max(1.0, round(seconds * scale, 3)),
        )
        for index, (topic, thought, beat_refs, seconds, labeled) in enumerate(raw)
    ]


def _guided_capacities_s(
    selected: Sequence[MediaRef],
    *,
    transition_type: str,
    transition_duration_s: float,
) -> list[float]:
    """Per-moment usable capacity across `selected` in flat fallback order.

    Mirrors `_allocate_beat_windows`'s per-moment overlap accounting: every
    moment pays one transition overlap into the next moment except the
    sequence's globally last one. `selected` order therefore only affects
    *which* moment is treated as last -- since every video pays the same
    overlap, the total is order-independent; only the presence of an image
    (an unbounded hold) changes the result, whose capacity is ``math.inf``.
    """

    from app.pipeline.guided_story import guided_moment_capacity_s  # noqa: PLC0415

    if not selected:
        return []
    overlap_s = transition_duration_s if transition_type != "none" else 0.0
    last_index = len(selected) - 1
    return [
        guided_moment_capacity_s(ref, overlap_s=0.0 if index == last_index else overlap_s)
        for index, ref in enumerate(selected)
    ]


def _guided_capacity_s(
    selected: Sequence[MediaRef],
    *,
    transition_type: str,
    transition_duration_s: float,
) -> float:
    """Total achievable story duration across `selected` in flat fallback order.

    See `_guided_capacities_s` for the per-moment accounting this sums.
    """

    capacities = _guided_capacities_s(
        selected, transition_type=transition_type, transition_duration_s=transition_duration_s
    )
    if not capacities:
        return 0.0
    if any(math.isinf(capacity) for capacity in capacities):
        return math.inf
    return sum(capacities)


def _select_capacity_aware_sources(
    ordered: list[MediaRef],
    *,
    target_s: float,
    transition_type: str,
    transition_duration_s: float,
) -> list[MediaRef]:
    """Grow the standard fallback order until it can structurally cover target_s.

    Starts from the same "up to 7, up to floor(target/min_moment)" story-shape
    heuristic the fallback has always used (fine whenever that subset already
    has enough real capacity), then keeps adding the next source in
    `_guided_fallback_order` (longest-video-first, interleaved with photos)
    while real capacity still falls short. If that order runs out before the
    target is reachable, switch the tie-break to the single longest
    remaining eligible clip first -- once the standard order has already
    proven insufficient, maximizing capacity gained per extra source beats
    preserving its story-shape interleave.
    """

    max_media = min(GUIDED_STORY_MAX_MEDIA, len(ordered))
    base_count = max(1, min(7, len(ordered), math.floor(target_s / GUIDED_STORY_MIN_MOMENT_S)))
    selected = list(ordered[:base_count])
    selected_ids = {ref.media_id for ref in selected}

    def capacity_s() -> float:
        return _guided_capacity_s(
            selected, transition_type=transition_type, transition_duration_s=transition_duration_s
        )

    for ref in ordered[base_count:]:
        if len(selected) >= max_media or capacity_s() + _CAPACITY_EPSILON_S >= target_s:
            break
        selected.append(ref)
        selected_ids.add(ref.media_id)

    if capacity_s() + _CAPACITY_EPSILON_S < target_s:
        leftovers = sorted(
            (ref for ref in ordered if ref.media_id not in selected_ids),
            key=lambda ref: -(float(ref.duration_s or 0.0)),
        )
        for ref in leftovers:
            if len(selected) >= max_media or capacity_s() + _CAPACITY_EPSILON_S >= target_s:
                break
            selected.append(ref)
            selected_ids.add(ref.media_id)

    return selected


def guided_story_capacity_s(
    media: Sequence[CadenceCapacityMedia],
    *,
    pace: str = "balanced",
    mixed_media_timing: MixedMediaTimingProfile | None = None,
) -> float:
    """Best-case guided-story duration this footage can structurally support.

    Uses every eligible source (`_guided_fallback_order`, capped at
    `GUIDED_STORY_MAX_MEDIA`) with the exact crossfade-overlap accounting
    `_allocate_beat_windows` applies at render time, so the specialist LLM is
    never handed a target neither it nor the deterministic fallback could
    ever actually deliver -- the same capacity model `deterministic_guided_beats`
    selects against. Returns ``math.inf`` when any eligible source is a still
    image (an image hold has no length limit); callers still clamp against
    `MAX_PROPOSAL_DURATION_S`.
    """

    from app.pipeline.guided_story import guided_transition_params  # noqa: PLC0415

    ordered = _guided_fallback_order(list(media))[:GUIDED_STORY_MAX_MEDIA]
    if not ordered:
        return 0.0
    transition_type, transition_duration_s = guided_transition_params(
        "guided_story", pace, mixed_media_timing
    )
    return _guided_capacity_s(
        ordered, transition_type=transition_type, transition_duration_s=transition_duration_s
    )


def deterministic_guided_beats(
    media: list[MediaRef],
    duration_s: int | float,
    *,
    required_media_ids: Sequence[str] | None = None,
    pace: str = "balanced",
    mixed_media_timing: MixedMediaTimingProfile | None = None,
) -> list[StoryBeat]:
    """Build conservative, metadata-free story structure from renderable owned media.

    Capacity-aware (job b2242487): source selection and the final target
    duration are both checked against each selected clip's real screen-time
    capacity (source duration minus the crossfade overlap
    `_allocate_beat_windows` charges at render time -- see
    `guided_story.guided_moment_capacity_s`), not just the flat 12s/beat
    schema cap. When even every eligible source can't reach the requested
    duration, the target is SHRUNK to the best achievable total instead of
    raising -- this fallback's whole job is to always yield a story
    `_allocate_beat_windows` can compile. Raising is reserved for the case
    where even the per-source minimum-moment floor can't be met.
    """

    from app.pipeline.guided_story import guided_transition_params  # noqa: PLC0415

    ordered = _guided_fallback_order(media)
    required_ids = set(required_media_ids or ())
    if required_ids - {ref.media_id for ref in media}:
        raise ValueError("guided story fallback is missing required media")
    if required_ids - {ref.media_id for ref in ordered}:
        raise ValueError("guided story fallback cannot use every required source safely")
    if not ordered:
        raise ValueError("guided story fallback found no usable media")

    target_s = float(max(3, min(MAX_PROPOSAL_DURATION_S, duration_s)))
    transition_type, transition_duration_s = guided_transition_params(
        "guided_story", pace, mixed_media_timing
    )
    if required_ids:
        selected = [ref for ref in ordered if ref.media_id in required_ids]
    else:
        selected = _select_capacity_aware_sources(
            ordered,
            target_s=target_s,
            transition_type=transition_type,
            transition_duration_s=transition_duration_s,
        )
    if len(selected) > GUIDED_STORY_MAX_MEDIA:
        raise ValueError("guided story fallback exceeds specialist media capacity")
    floor_s = sum(guided_source_floor_s(ref) for ref in selected)
    if target_s + 0.001 < floor_s:
        raise ValueError("guided story fallback cannot fit every required source")

    capacity_s = _guided_capacity_s(
        selected, transition_type=transition_type, transition_duration_s=transition_duration_s
    )
    if capacity_s + 0.001 < target_s:
        if capacity_s + 0.001 < floor_s:
            raise ValueError("guided story fallback found no achievable duration for this footage")
        # Never hard-fail a recovery: shrink to what the selected sources can
        # actually deliver, leaving a one-frame-per-source safety margin so
        # `_allocate_beat_windows`'s own frame-quantized per-beat caps can
        # never re-derive a target the real windows can't reach after
        # rounding.
        safety_margin_s = _RENDER_FRAME_S * len(selected)
        target_s = max(floor_s, min(target_s, capacity_s - safety_margin_s))

    beat_count = min(GUIDED_STORY_MAX_BEATS, len(selected))
    groups: list[list[MediaRef]] = [[] for _ in range(beat_count)]
    for index, ref in enumerate(selected):
        groups[index % beat_count].append(ref)

    # Each beat's ceiling is its own clips' real (crossfade-overlap-adjusted)
    # capacity -- not a flat per-beat schema number -- so a single long clip
    # can hold for its full usable length instead of being scaled down to
    # match its shorter beat-mates (job b2242487 follow-up: StoryBeat.duration_s
    # is a schema-bound weight, but the weight this fallback hands the compiler
    # must never claim more than the beat can structurally deliver).
    per_ref_capacity_s = _guided_capacities_s(
        selected, transition_type=transition_type, transition_duration_s=transition_duration_s
    )
    beat_capacity_s = [0.0] * beat_count
    for index, capacity in enumerate(per_ref_capacity_s):
        beat_capacity_s[index % beat_count] += capacity

    durations = [round(sum(guided_source_floor_s(ref) for ref in group), 3) for group in groups]
    remaining = round(target_s - sum(durations), 3)
    index = 0
    while remaining > 0.001:
        beat_index = index % beat_count
        capacity = round(beat_capacity_s[beat_index] - durations[beat_index], 3)
        if capacity > 0:
            addition = min(remaining, capacity)
            durations[beat_index] = round(durations[beat_index] + addition, 3)
            remaining = round(remaining - addition, 3)
        index += 1
        if index > beat_count * 2 and remaining > 0.001:
            raise ValueError("guided story fallback cannot allocate target duration")

    # `topic` is internal bookkeeping only (distinct-topic checks, debugging)
    # and is never rendered. `thought` IS rendered as on-screen text by
    # `guided_story._text_elements`, so this metadata-free recovery path must
    # never invent generic captions ("A few moments, together.", ...) the
    # creator never asked for -- leave it blank (KRI-126, job 506d2993).
    topics = [
        "Opening",
        "Details",
        "Closing",
        "Another view",
        "Final frame",
        "Next chapter",
        "More detail",
        "Later moment",
        "Before the close",
        "Last moment",
    ]
    return [
        StoryBeat(
            beat_id=f"fallback-beat-{beat_index + 1}",
            # KRI-129: GUIDED_STORY_MAX_BEATS raised beyond len(topics); fall
            # back to a plain numbered label past the hand-written list
            # rather than index-erroring. Still never rendered.
            topic=topics[beat_index] if beat_index < len(topics) else f"Chapter {beat_index + 1}",
            thought="",
            thought_source="ai_draft",
            media_ids=[ref.media_id for ref in group],
            layout="fullscreen",
            duration_s=durations[beat_index],
        )
        for beat_index, group in enumerate(groups)
    ]


def apply_caption_clip_intents(
    beats: list[StoryBeat],
    clip_intents: list[ResolvedClipIntent] | None,
) -> list[StoryBeat]:
    """KRI-129: carry a resolved caption clip-intent's exact text onto the
    metadata-free `deterministic_guided_beats` fallback, so an agent failure
    never silently drops a creator-authored on-screen caption.

    Finds, per caption intent, the fallback beat holding the MOST of its
    clips (this fallback's grouping is round-robin, not topic-aware, so a
    caption's clips can land split across several beats -- there is no
    reorder to attempt here, only a best-effort placement) and sets that
    beat's thought and thought_source, mirroring how
    `deterministic_labeled_beats` already marks creator-authored beats
    "user". Every other beat's thought is left untouched --
    `deterministic_guided_beats` already leaves every beat's thought ""
    (KRI-126: never invent generic captions), so there is nothing else to
    blank here.
    """

    if not clip_intents or not beats:
        return beats
    for intent in clip_intents:
        if (
            intent.status != "resolved"
            or intent.op != "caption"
            or not (intent.caption_text or "").strip()
        ):
            continue
        ids = set(intent.media_ids())
        if not ids:
            continue
        target = max(beats, key=lambda beat: len(set(beat.media_ids) & ids))
        if not (set(target.media_ids) & ids):
            continue
        target.thought = intent.caption_text.strip()
        target.thought_source = "user"
    return beats


def _compatibility_beats(cuts: list[FastMontageCut]) -> list[StoryBeat]:
    """Keep older readers functional while fast_cuts remain authoritative."""

    beats: list[StoryBeat] = []
    for index in range(0, len(cuts), 4):
        group = cuts[index : index + 4]
        media_ids = list(dict.fromkeys(cut.media_id for cut in group))
        beats.append(
            StoryBeat(
                beat_id=f"fast-beat-{index // 4 + 1}",
                topic="Fast montage",
                thought="",
                thought_source="ai_draft",
                media_ids=media_ids,
                layout="fullscreen",
                duration_s=max(
                    1.0, min(MAX_PROPOSAL_DURATION_S, sum(cut.output_duration_s for cut in group))
                ),
            )
        )
    return beats


def plan_direction_snapshot(
    source: EditProposalSnapshot,
    *,
    direction: str,
    goal: str,
    pace: str,
    duration_s: int | float,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
    montage_audio: MontageAudioPlan | None = None,
    montage_cadence: MontageCadenceConstraint | None = None,
    idea: str = "",
    theme: str = "",
    job_id: str | None = None,
    plan_item_id: str | None = None,
    creator_request: str = "",
    planning_diagnostics_out: dict | None = None,
) -> EditProposalSnapshot:
    """Run the canonical proposal planner using the already-analyzed media.

    This deliberately accepts an immutable server snapshot rather than a
    Copilot/browser timeline. Direction replacement can therefore reuse exact
    media identities and analysis without trusting model-authored source cuts.
    """

    media = [
        EditProposalMedia(
            media_id=ref.media_id,
            lane=ref.lane,
            kind=ref.kind,
            source_filename=ref.source_filename,
            duration_s=ref.duration_s,
            user_context=ref.user_context,
            subject=str(ref.analysis.get("subject") or ""),
            description=str(ref.analysis.get("description") or ""),
            on_screen_text=str(ref.analysis.get("on_screen_text") or ""),
            best_moments=list(ref.analysis.get("best_moments") or []),
        )
        for ref in source.media
    ]
    if source.shot_labels and direction == "fast_montage":
        # Fast cuts have no per-shot text lane. Refuse rather than approve a
        # replacement that silently drops the creator's confirmed labels.
        raise CreatorTextInfeasibleError("creator shot labels need a story-beat direction")
    video_reuse_policy = resolve_video_reuse_policy(
        creator_request,
        source.video_reuse_policy,
        montage_cadence,
    )
    if video_reuse_policy == "once":
        montage_cadence = None
    narrated = source.narration is not None
    force_semantic = source.frame_schedule is not None
    semantic_enabled = force_semantic or settings.edit_proposal_semantic_enabled
    planning_duration_s = (
        max(3, min(MAX_PROPOSAL_DURATION_S, duration_s))
        if semantic_enabled or (narrated and direction == "fast_montage")
        else clamp_fast_montage_target_duration_s(
            media, duration_s, mixed_media_timing, video_reuse_policy
        )
        if direction == "fast_montage"
        else max(3, min(MAX_PROPOSAL_DURATION_S, duration_s))
    )
    agent_input = EditProposalAgentInput(
        idea=idea[:500],
        theme=theme[:500],
        direction=direction,
        goal=goal[:500],
        creator_request=creator_request[:12000],
        video_reuse_policy=video_reuse_policy,
        pace=pace,
        target_duration_s=planning_duration_s,
        media_scope=source.media_scope,
        selected_media_ids=source.selected_media_ids,
        narration_duration_s=source.narration.duration_s if source.narration else None,
        narration_words=(
            [word.model_dump(mode="json") for word in source.narration.words]
            if source.narration
            else []
        ),
        mixed_media_timing=mixed_media_timing,
        montage_audio=montage_audio,
        montage_cadence=montage_cadence,
        opening_title=source.opening_title,
        opening_title_duration_s=source.opening_title_duration_s,
        shot_labels=source.shot_labels,
        closing_title=source.closing_title,
        clip_intents=source.clip_intents,
        media=media,
    )
    ctx = RunContext(job_id=job_id, plan_item_id=plan_item_id) if (job_id or plan_item_id) else None
    output = None
    used_fallback = False
    try:
        output = (
            plan_edit_proposal(agent_input, ctx=ctx, force_semantic=force_semantic)
            if semantic_enabled
            else EditProposalAgent(default_client()).run(agent_input, ctx=ctx)
        )
    except SemanticPlanningError as exc:
        if planning_diagnostics_out is not None:
            planning_diagnostics_out["planning_diagnostics"] = exc.diagnostics
        raise
    except TerminalError as exc:
        if direction == "text_explainer" or (
            narrated
            and (
                direction != "fast_montage"
                or not uses_quick_photo_long_video_timing(mixed_media_timing)
            )
        ):
            raise
        log.warning(
            "edit_direction_planner.deterministic_fallback",
            job_id=job_id,
            direction=direction,
            error=str(exc),
        )
        used_fallback = True
    cuts = (
        output.fast_cuts
        if output is not None and direction == "fast_montage"
        else deterministic_fast_cuts(
            source.media,
            planning_duration_s,
            mixed_media_timing,
            montage_cadence,
            video_reuse_policy=video_reuse_policy,
            narration_duration_s=source.narration.duration_s if source.narration else None,
            required_media_ids=(
                [ref.media_id for ref in source.media]
                if source.media_scope == "all"
                else source.selected_media_ids
                if source.media_scope == "selected"
                else None
            ),
        )
        if direction == "fast_montage"
        else None
    )
    if direction == "fast_montage":
        if not cuts:
            raise ValueError("fast montage planner returned no source-aware cuts")
        beats = _compatibility_beats(cuts)
    elif output is not None and output.frame_schedule is not None:
        beats = output.scheduled_story_beats or []
    elif output is None and source.shot_labels:
        beats = deterministic_labeled_beats(
            source.media,
            duration_s,
            shot_labels=source.shot_labels,
            opening_title=source.opening_title,
            opening_title_duration_s=source.opening_title_duration_s,
            closing_title=source.closing_title,
            required_media_ids=(
                [ref.media_id for ref in source.media]
                if source.media_scope == "all"
                else source.selected_media_ids
                if source.media_scope == "selected"
                else None
            ),
        )
    elif output is None:
        beats = deterministic_guided_beats(
            source.media,
            duration_s,
            required_media_ids=(
                [ref.media_id for ref in source.media]
                if source.media_scope == "all"
                else source.selected_media_ids
                if source.media_scope == "selected"
                else None
            ),
            pace=pace,
            mixed_media_timing=mixed_media_timing,
        )
    else:
        creator_labels = set(source.shot_labels or [])
        beats = [
            StoryBeat(
                beat_id=f"beat-{index + 1}",
                topic=beat.topic,
                thought=beat.thought,
                thought_source="user" if beat.thought in creator_labels else "ai_draft",
                media_ids=beat.media_ids,
                layout=beat.layout,
                duration_s=beat.duration_s,
            )
            for index, beat in enumerate(output.story_beats)
        ]
    planned = source.model_copy(
        update={
            "direction": direction,
            "goal": goal,
            "pace": pace,
            "duration_s": (
                output.duration_s
                if output is not None and output.frame_schedule is not None
                else planning_duration_s
                if output is None or direction == "fast_montage"
                else output.duration_s
            ),
            # A confirmed creator title is immutable across direction changes;
            # the renderer burns ``title``, so never let generated copy replace it.
            "title": source.opening_title or (output.title if output is not None else source.title),
            "story_beats": beats,
            "fast_cuts": cuts,
            "mixed_media_timing": mixed_media_timing,
            "montage_text_bindings": (
                output.montage_text_bindings if output is not None else source.montage_text_bindings
            ),
            "montage_audio": (
                output.montage_audio
                if output is not None and output.montage_audio is not None
                else montage_audio
            ),
            "montage_cadence": montage_cadence,
            "video_reuse_policy": video_reuse_policy,
            "narration": source.narration,
            "media_scope": source.media_scope,
            "selected_media_ids": source.selected_media_ids,
            "frame_schedule": output.frame_schedule if output is not None else None,
        }
    )
    result = EditProposalSnapshot.model_validate(planned.model_dump(mode="json"))
    if planning_diagnostics_out is not None and output is not None:
        planning_diagnostics_out["planning_diagnostics"] = output.planning_diagnostics
    if used_fallback:
        from app.pipeline.guided_story import validate_proposal_timing  # noqa: PLC0415

        validate_proposal_timing(result)
    return result
