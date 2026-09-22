"""Deterministic, integer-frame scheduling for semantic edit proposals.

The semantic agent chooses narrative grouping only.  This module is the sole
authority for windows and output timing; it deliberately has no model, IO, or
clock dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from app.schemas.edit_frame_schedule import EditFrameSchedule, FrameScheduledMoment
from app.schemas.semantic_edit import SemanticEditPlan

if TYPE_CHECKING:
    from app.agents.edit_proposal import EditProposalAgentInput
    from app.schemas.edit_proposal import FastMontageCut, MontageTextBinding, StoryBeat

FPS = 30
MIN_FRAMES = 3
_PRECISION_TOLERANCE_S = 1e-6


class FeasibilityError(ValueError):
    def __init__(self, code: str, reason: str, report: Feasibility) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.report = report


@dataclass(frozen=True)
class Feasibility:
    requested_frames: int
    effective_frames: int
    min_frames: int
    max_frames: int
    status: Literal["feasible", "clamped", "infeasible"]
    reason: str = ""
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduleResult:
    schedule: EditFrameSchedule
    story_beats: list[StoryBeat]
    fast_cuts: list[FastMontageCut] | None
    montage_text_bindings: list[MontageTextBinding]
    duration_s: float
    repairs: list[str]
    feasibility: Feasibility


@dataclass
class _Slot:
    chapter_index: int
    source_index: int
    media_id: str
    candidate_index: int | None
    weight: float
    role: str
    layout: str
    chapter_id: str
    topic: str
    thought: str
    cap: int
    duration: int = MIN_FRAMES


def _frames(seconds: float, *, ceil: bool = False) -> int:
    value = float(seconds) * FPS
    return int(
        math.ceil(value - _PRECISION_TOLERANCE_S * FPS)
        if ceil
        else math.floor(value + _PRECISION_TOLERANCE_S * FPS)
    )


def _effective_target_frames(input: EditProposalAgentInput) -> int:
    if input.narration_duration_s is not None:
        return max(1, _frames(float(input.narration_duration_s), ceil=True))
    return max(1, int(round(float(input.target_duration_s) * FPS)))


def _required_ids(input: EditProposalAgentInput) -> list[str]:
    ids = (
        [media.media_id for media in input.media]
        if input.media_scope == "all"
        else list(input.selected_media_ids or [])
    )
    for intent in input.clip_intents or []:
        if intent.status == "resolved":
            ids.extend(assignment.media_id for assignment in intent.assignments)
    if input.montage_audio is not None:
        ids.extend(input.montage_audio.source_media_ids)
    if input.montage_cadence is not None:
        ids.extend(input.montage_cadence.source_media_ids)
    return list(dict.fromkeys(ids))


def _media_by_id(input: EditProposalAgentInput) -> dict[str, Any]:
    return {media.media_id: media for media in input.media}


def _source_capacity_frames(media: Any, fallback: int, input: EditProposalAgentInput) -> int:
    if media.kind == "image":
        if input.mixed_media_timing is None:
            return fallback
        from app.schemas.edit_proposal import mixed_media_hold_bounds  # noqa: PLC0415

        return _frames(mixed_media_hold_bounds("image", input.mixed_media_timing).maximum_s)
    if media.duration_s is None:
        return -1
    capacity = max(0, _frames(float(media.duration_s)))
    if input.mixed_media_timing is not None:
        from app.schemas.edit_proposal import mixed_media_hold_bounds  # noqa: PLC0415

        capacity = min(
            capacity,
            _frames(mixed_media_hold_bounds("video", input.mixed_media_timing).maximum_s),
        )
    return capacity


def _slot_minimum_frames(media: Any, input: EditProposalAgentInput) -> int:
    if input.mixed_media_timing is None:
        if input.direction == "fast_montage":
            if media.kind == "image":
                return 12
            return min(12, _source_capacity_frames(media, 12, input))
        return max(MIN_FRAMES, _transition_frames(input, 2) + 1)
    from app.schemas.edit_proposal import mixed_media_hold_bounds  # noqa: PLC0415

    policy_min = _frames(mixed_media_hold_bounds(media.kind, input.mixed_media_timing).minimum_s)
    return min(policy_min, _source_capacity_frames(media, policy_min, input))


def assess_semantic_feasibility(input: EditProposalAgentInput) -> Feasibility:
    """Preflight target capacity without interpreting model-provided chapters."""

    requested = _effective_target_frames(input)
    media_by_id = _media_by_id(input)
    if input.montage_cadence is not None:
        cadence = input.montage_cadence
        cadence_frames = _frames(cadence.cut_duration_s)
        if abs(cadence_frames / FPS - cadence.cut_duration_s) > _PRECISION_TOLERANCE_S:
            return Feasibility(
                requested,
                requested,
                0,
                0,
                "infeasible",
                "fixed cadence must align to complete 30fps frames",
            )
        if input.direction != "fast_montage":
            return Feasibility(
                requested, requested, 0, 0, "infeasible", "cadence requires fast montage"
            )
        if not set(cadence.source_media_ids) <= media_by_id.keys():
            return Feasibility(
                requested, requested, 0, 0, "infeasible", "cadence references unknown media"
            )
        if requested % cadence_frames or (requested // cadence_frames) % len(
            cadence.source_media_ids
        ):
            return Feasibility(
                requested,
                requested,
                0,
                0,
                "infeasible",
                "target cannot preserve complete fixed cadence cycles",
            )
        cut_count = requested // cadence_frames
        if cut_count > 80:
            return Feasibility(
                requested, requested, 0, 0, "infeasible", "cadence exceeds 80 cut limit"
            )
        if (
            input.video_reuse_policy == "once" or cadence.reuse_policy == "no_repeat"
        ) and cut_count > len(cadence.source_media_ids):
            return Feasibility(
                requested,
                requested,
                0,
                0,
                "infeasible",
                "fixed cadence requires source reuse but reuse is prohibited",
            )
        for media_id in cadence.source_media_ids:
            media = media_by_id[media_id]
            if media.kind != "video" or media.duration_s is None:
                return Feasibility(
                    requested, requested, 0, 0, "infeasible", "cadence needs known videos"
                )
            if _frames(float(media.duration_s)) < cadence_frames:
                return Feasibility(
                    requested, requested, 0, 0, "infeasible", "cadence cut exceeds source"
                )
    required = _required_ids(input)
    selected = [media_by_id[media_id] for media_id in required if media_id in media_by_id]
    if len(selected) != len(required):
        return Feasibility(requested, requested, 0, 0, "infeasible", "required media is unknown")
    if any(media.kind == "video" and media.duration_s is None for media in input.media):
        return Feasibility(requested, requested, 0, 0, "infeasible", "video duration is unknown")
    transition = _transition_frames(input, len(selected))
    # Required sources establish the coverage floor; optional sources establish
    # capacity. A repeat-authorized video can cover the requested target.
    minimum = max(
        0,
        sum(_slot_minimum_frames(media, input) for media in selected)
        - transition * max(0, len(selected) - 1),
    )
    capacities = [
        _source_capacity_frames(media, requested, input)
        for media in input.media
        if not (media.kind == "video" and media.duration_s is None)
    ]
    capacity_transition = _transition_frames(input, len(capacities))
    # Optional sources shorter than a crossfade cannot add usable timeline
    # capacity. Required ones are rejected below instead of silently omitted.
    contributing = [cap for cap in capacities if cap > capacity_transition]
    maximum = max(0, sum(contributing) - capacity_transition * max(0, len(contributing) - 1))
    if any(
        media.kind == "video" and _source_capacity_frames(media, requested, input) <= transition
        for media in selected
    ):
        return Feasibility(
            requested,
            requested,
            minimum,
            maximum,
            "infeasible",
            "required video is too short to survive the transition overlap",
        )
    if input.video_reuse_policy == "allow_repeat" and any(
        media.kind == "video" for media in input.media
    ):
        maximum = max(maximum, requested)
    if maximum < minimum:
        return Feasibility(
            requested, requested, minimum, maximum, "infeasible", "required media is too short"
        )
    if maximum < 3 * FPS:
        return Feasibility(
            requested,
            requested,
            minimum,
            maximum,
            "infeasible",
            "required footage cannot form the minimum three-second proposal",
        )
    if requested < minimum:
        return Feasibility(
            requested,
            requested,
            minimum,
            maximum,
            "infeasible",
            "target cannot provide the required source coverage",
        )
    if requested <= maximum:
        return Feasibility(requested, requested, minimum, maximum, "feasible")
    if input.narration_duration_s is not None or input.montage_cadence is not None:
        return Feasibility(
            requested,
            requested,
            minimum,
            maximum,
            "infeasible",
            "pinned timing exceeds footage capacity",
        )
    return Feasibility(
        requested,
        maximum,
        minimum,
        maximum,
        "clamped",
        "target exceeds footage capacity",
        ("precision_tolerance_s=0.000001",),
    )


def _transition_frames(input: EditProposalAgentInput, moment_count: int) -> int:
    if moment_count <= 1:
        return 0
    from app.pipeline.guided_story import guided_transition_params  # noqa: PLC0415

    kind, seconds = guided_transition_params(input.direction, input.pace, input.mixed_media_timing)
    if kind == "none":
        return 0
    return max(0, int(round(float(seconds) * FPS)))


def _candidate_center(media: Any, index: int | None) -> int | None:
    if index is None:
        moments = media.best_moments
        valid = [
            (position, raw)
            for position, raw in enumerate(moments)
            if isinstance(raw, dict)
            and isinstance(raw.get("start_s"), (int, float))
            and isinstance(raw.get("end_s"), (int, float))
            and raw["end_s"] > raw["start_s"]
        ]
        if not valid:
            return None
        index = max(valid, key=lambda row: (float(row[1].get("energy", 0) or 0), -row[0]))[0]
    if index >= len(media.best_moments):
        raise ValueError("requested candidate index is unknown")
    raw = media.best_moments[index]
    if not isinstance(raw, dict):
        raise ValueError("requested candidate is invalid")
    start = raw.get("start_s", raw.get("start", raw.get("start_frame")))
    end = raw.get("end_s", raw.get("end", raw.get("end_frame")))
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        raise ValueError("requested candidate has no finite bounds")
    if not math.isfinite(float(start)) or not math.isfinite(float(end)) or end <= start:
        raise ValueError("requested candidate has invalid bounds")
    if "start_frame" in raw and "end_frame" in raw:
        return max(0, int(round((float(start) + float(end)) / 2)))
    return max(0, int(round((float(start) + float(end)) * FPS / 2)))


def _allocate_weighted_frames(
    slots: list[_Slot], total: int, input: EditProposalAgentInput
) -> None:
    """Capped weighted waterfill with stable semantic-order remainder ties."""

    remaining = total - sum(slot.duration for slot in slots)
    media_remaining: dict[str, int] = {}
    if input.video_reuse_policy == "distinct_windows":
        media_remaining = {
            slot.media_id: _frames(float(_media_by_id(input)[slot.media_id].duration_s))
            for slot in slots
            if _media_by_id(input)[slot.media_id].kind == "video"
            and slot.media_id not in media_remaining
        }
        for slot in slots:
            if slot.media_id in media_remaining:
                media_remaining[slot.media_id] -= slot.duration
    while remaining > 0:
        eligible = [
            slot
            for slot in slots
            if slot.duration < slot.cap
            and (slot.media_id not in media_remaining or media_remaining[slot.media_id] > 0)
        ]
        if not eligible:
            raise ValueError("semantic schedule has insufficient source capacity")
        # Allocate one frame per pass by relative weight with stable ties.
        # Distinct windows share an aggregate media budget.
        slot = max(
            enumerate(eligible),
            key=lambda row: (row[1].weight / (row[1].duration + 1), -row[0]),
        )[1]
        slot.duration += 1
        if slot.media_id in media_remaining:
            media_remaining[slot.media_id] -= 1
        remaining -= 1


def _slots_for(
    plan: SemanticEditPlan, input: EditProposalAgentInput, required: list[str]
) -> list[_Slot]:
    media = _media_by_id(input)
    slots: list[_Slot] = []
    for chapter_index, chapter in enumerate(plan.chapters):
        source_weight_total = sum(source.weight for source in chapter.sources)
        for source_index, source in enumerate(chapter.sources):
            item = media.get(source.media_id)
            if item is None:
                raise FeasibilityError(
                    "unknown_media",
                    f"semantic source {source.media_id} is unknown",
                    assess_semantic_feasibility(input),
                )
            cap = _source_capacity_frames(item, _effective_target_frames(input), input)
            minimum = _slot_minimum_frames(item, input)
            if cap < minimum:
                raise FeasibilityError(
                    "short_media",
                    f"semantic source {source.media_id} has under 0.1s",
                    assess_semantic_feasibility(input),
                )
            slots.append(
                _Slot(
                    chapter_index,
                    source_index,
                    source.media_id,
                    source.candidate_index,
                    chapter.weight * source.weight / source_weight_total,
                    chapter.role,
                    chapter.layout,
                    chapter.chapter_id,
                    chapter.topic,
                    chapter.thought,
                    cap,
                    minimum,
                )
            )
    present = {slot.media_id for slot in slots}
    missing = [media_id for media_id in required if media_id not in present]
    if missing:
        raise FeasibilityError(
            "required_media_omitted",
            f"semantic plan omitted required media: {', '.join(missing)}",
            assess_semantic_feasibility(input),
        )
    occurrences: dict[str, int] = {}
    for slot in slots:
        occurrences[slot.media_id] = occurrences.get(slot.media_id, 0) + 1
    video_ids = {media_id for media_id, item in media.items() if item.kind == "video"}
    if input.video_reuse_policy == "once" and any(
        count > 1 for media_id, count in occurrences.items() if media_id in video_ids
    ):
        raise FeasibilityError(
            "reuse_once",
            "a source appears more than once under once reuse",
            assess_semantic_feasibility(input),
        )
    return slots


def _source_windows(slots: list[_Slot], input: EditProposalAgentInput) -> list[tuple[int, int]]:
    media = _media_by_id(input)
    cursors: dict[str, int] = {}
    windows: list[tuple[int, int]] = []
    for slot in slots:
        item = media[slot.media_id]
        raw_cap = _frames(float(item.duration_s)) if item.kind == "video" else slot.cap
        center = _candidate_center(item, slot.candidate_index)
        if input.video_reuse_policy == "distinct_windows" and item.kind == "video":
            start = cursors.get(slot.media_id, 0)
            if start + slot.duration > raw_cap:
                raise ValueError("distinct source windows exceed capacity")
            cursors[slot.media_id] = start + slot.duration
        elif center is None:
            start = 0
        else:
            # Candidate windows are a preference only; use the whole source
            # capacity and slide at boundaries so late 1s moments can support
            # a longer feasible allocation.
            start = max(0, min(raw_cap - slot.duration, center - slot.duration // 2))
        windows.append((start, start + slot.duration))
    return windows


def schedule_semantic_edit(plan: SemanticEditPlan, input: EditProposalAgentInput) -> ScheduleResult:
    """Materialize semantic intent as one canonical 30fps edit schedule."""

    feasibility = assess_semantic_feasibility(input)
    if feasibility.status == "infeasible":
        raise FeasibilityError("infeasible_input", feasibility.reason, feasibility)
    required = _required_ids(input)
    slots = _slots_for(plan, input, required)
    if input.montage_cadence is not None:
        cadence = input.montage_cadence
        cadence_frames = _frames(cadence.cut_duration_s)
        if input.direction != "fast_montage":
            raise FeasibilityError(
                "cadence_direction", "round-robin cadence requires a fast montage", feasibility
            )
        if feasibility.effective_frames % cadence_frames:
            raise FeasibilityError(
                "cadence_duration",
                "target cannot preserve the explicit cadence duration",
                feasibility,
            )
        cut_count = feasibility.effective_frames // cadence_frames
        if cut_count > 80 or cut_count % len(cadence.source_media_ids):
            raise FeasibilityError(
                "cadence_duration",
                "target cannot preserve complete cadence cycles within the cut limit",
                feasibility,
            )
        templates = {}
        for slot in slots:
            templates.setdefault(slot.media_id, slot)
        if not set(cadence.source_media_ids) <= templates.keys():
            raise FeasibilityError(
                "cadence_order", "semantic plan omits a cadence source", feasibility
            )
        slots = [
            replace(templates[media_id], source_index=index, candidate_index=None)
            for index, media_id in enumerate(
                cadence.source_media_ids[index % len(cadence.source_media_ids)]
                for index in range(cut_count)
            )
        ]
    if input.video_reuse_policy == "once":
        media_by_id = _media_by_id(input)
        seen_videos: set[str] = set()
        for slot in slots:
            if media_by_id[slot.media_id].kind == "video":
                if slot.media_id in seen_videos:
                    raise FeasibilityError(
                        "reuse_once",
                        "a cadence schedule repeats a video under once reuse",
                        feasibility,
                    )
                seen_videos.add(slot.media_id)
    transition = _transition_frames(input, len(slots))
    source_total = feasibility.effective_frames + transition * (len(slots) - 1)
    if (
        input.montage_cadence is None
        and input.video_reuse_policy == "allow_repeat"
        and source_total > sum(slot.cap for slot in slots)
    ):
        media = _media_by_id(input)
        repeatable = [slot for slot in slots if media[slot.media_id].kind == "video"]
        cursor = 0
        while source_total > sum(slot.cap for slot in slots) and len(slots) < 80:
            if not repeatable:
                break
            template = repeatable[cursor % len(repeatable)]
            insert_at = (
                max(
                    index
                    for index, slot in enumerate(slots)
                    if slot.chapter_index == template.chapter_index
                )
                + 1
            )
            slots.insert(insert_at, replace(template, source_index=len(slots)))
            cursor += 1
            transition = _transition_frames(input, len(slots))
            source_total = feasibility.effective_frames + transition * (len(slots) - 1)
    if len(slots) > 80:
        raise FeasibilityError(
            "moment_limit", "semantic schedule exceeds the 80-cut limit", feasibility
        )
    if input.direction != "fast_montage":
        beat_count = sum(
            math.ceil(sum(slot.chapter_index == chapter_index for slot in slots) / 4)
            for chapter_index in range(len(plan.chapters))
        )
        if beat_count > 20:
            raise FeasibilityError(
                "beat_limit", "semantic schedule exceeds the 20-beat limit", feasibility
            )
    # Distinct windows charge each occurrence independently.  Other policies
    # can reuse capacity but still cannot exceed a single source's window.
    if input.video_reuse_policy == "distinct_windows":
        media_by_id = _media_by_id(input)
        by_media: dict[str, int] = {}
        for slot in slots:
            if media_by_id[slot.media_id].kind == "video":
                by_media[slot.media_id] = by_media.get(slot.media_id, 0) + slot.duration
        for media_id, minimum in by_media.items():
            raw_capacity = _frames(float(media_by_id[media_id].duration_s))
            if minimum > raw_capacity:
                raise FeasibilityError(
                    "distinct_capacity", "disjoint source windows cannot fit", feasibility
                )
    if source_total < sum(slot.duration for slot in slots):
        raise FeasibilityError(
            "minimum_coverage", "target cannot show every semantic source", feasibility
        )
    if input.video_reuse_policy != "allow_repeat" and source_total > sum(
        slot.cap for slot in slots
    ):
        raise FeasibilityError(
            "semantic_capacity",
            "scheduled source windows, including crossfade overlap, exceed footage capacity",
            feasibility,
        )
    try:
        if input.montage_cadence is not None:
            for slot in slots:
                slot.duration = _frames(input.montage_cadence.cut_duration_s)
            if input.video_reuse_policy == "distinct_windows":
                media_by_id = _media_by_id(input)
                usage: dict[str, int] = {}
                for slot in slots:
                    if media_by_id[slot.media_id].kind == "video":
                        usage[slot.media_id] = usage.get(slot.media_id, 0) + slot.duration
                if any(
                    used > _frames(float(media_by_id[media_id].duration_s))
                    for media_id, used in usage.items()
                ):
                    raise ValueError("distinct source windows exceed capacity")
        else:
            _allocate_weighted_frames(slots, source_total, input)
        windows = _source_windows(slots, input)
    except ValueError as error:
        code = "candidate_bounds" if "candidate" in str(error) else "semantic_capacity"
        raise FeasibilityError(code, str(error), feasibility) from error

    output_start = 0
    moments: list[FrameScheduledMoment] = []
    for index, (slot, (source_start, source_end)) in enumerate(zip(slots, windows, strict=True)):
        output_end = output_start + slot.duration
        moment_id = f"{slot.chapter_id}:m{slot.source_index + 1}"
        # Fast compilation has one beat window per cut. Guided/text use the
        # renderer-compatible <=4-source chapter partition below.
        chapter_offset = sum(
            1
            for candidate in slots
            if candidate.chapter_index == slot.chapter_index
            and candidate.source_index < slot.source_index
        )
        split_ordinal = chapter_offset // 4
        beat_id = (
            moment_id
            if input.direction == "fast_montage"
            else slot.chapter_id
            if split_ordinal == 0
            else f"{slot.chapter_id}-{split_ordinal + 1}"
        )
        moments.append(
            FrameScheduledMoment(
                moment_id=moment_id,
                beat_id=beat_id,
                media_id=slot.media_id,
                source_start_frame=source_start,
                source_end_frame=source_end,
                output_start_frame=output_start,
                output_end_frame=output_end,
                layout=slot.layout,
                role=slot.role,
            )
        )
        output_start = output_end - transition
    schedule = EditFrameSchedule(
        total_frames=feasibility.effective_frames,
        transition_frames=transition,
        direction=input.direction,
        moments=moments,
    )

    from app.schemas.edit_proposal import (  # noqa: PLC0415
        FastMontageCut,
        MontageTextBinding,
        StoryBeat,
    )

    # Keep chapters together and split only renderer-limited >4 source beats.
    beats: list[StoryBeat] = []
    for chapter_index, chapter in enumerate(plan.chapters):
        chapter_slots = [slot for slot in slots if slot.chapter_index == chapter_index]
        for group_index in range(0, len(chapter_slots), 4):
            group = chapter_slots[group_index : group_index + 4]
            frames = sum(slot.duration for slot in group) - transition * max(0, len(group) - 1)
            beat_id = (
                chapter.chapter_id
                if group_index == 0
                else f"{chapter.chapter_id}-{group_index // 4 + 1}"
            )
            beats.append(
                StoryBeat(
                    beat_id=beat_id,
                    topic=chapter.topic,
                    thought=chapter.thought,
                    thought_source="user"
                    if chapter.thought in (input.shot_labels or [])
                    else "ai_draft",
                    media_ids=[slot.media_id for slot in group],
                    layout=chapter.layout,
                    duration_s=max(1.0, frames / FPS),
                )
            )
    cuts = None
    if input.direction == "fast_montage":
        cuts = [
            FastMontageCut(
                cut_id=moment.moment_id,
                media_id=moment.media_id,
                source_start_s=moment.source_start_frame / FPS,
                source_end_s=moment.source_end_frame / FPS,
                output_duration_s=(moment.output_end_frame - moment.output_start_frame) / FPS,
                role=moment.role,
            )
            for moment in moments
        ]
    bindings: list[MontageTextBinding] = []
    bound_text: dict[str, str] = {}
    for binding in plan.text_bindings:
        targets = binding.media_ids or [
            slot.media_id for slot in slots if slot.chapter_id in binding.chapter_ids
        ]
        if not targets:
            raise FeasibilityError(
                "text_binding_target",
                "semantic text binding has no scheduled target",
                feasibility,
            )
        for media_id in dict.fromkeys(targets):
            if media_id in bound_text:
                if bound_text[media_id] != binding.text:
                    raise FeasibilityError(
                        "conflicting_text_bindings",
                        "One source has conflicting requested captions. "
                        "Separate the captions or sources.",
                        feasibility,
                    )
                continue
            bound_text[media_id] = binding.text
            bindings.append(MontageTextBinding(media_id=media_id, text=binding.text))
    if len(bindings) > 12:
        raise FeasibilityError(
            "text_binding_limit",
            "semantic text bindings expand beyond the proposal limit",
            feasibility,
        )
    repairs = list(plan.repairs)
    if feasibility.status == "clamped":
        repairs.append("target_duration_clamped_to_capacity")
    repairs.extend(
        f"source_window:{moment.moment_id}:{moment.source_start_frame}-{moment.source_end_frame}"
        for moment in moments
    )
    repairs.append("precision_tolerance_s=0.000001")
    return ScheduleResult(
        schedule, beats, cuts, bindings, schedule.total_frames / FPS, repairs, feasibility
    )
