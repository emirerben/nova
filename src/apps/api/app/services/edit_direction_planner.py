"""Direction-specific replanning over an existing analyzed proposal snapshot."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
    minimum_required_sources,
)
from app.schemas.edit_proposal import (
    GUIDED_STORY_MIN_MOMENT_S,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    MontageAudioPlan,
    MontageCadenceConstraint,
    StoryBeat,
    mixed_media_hold_bounds,
    uses_quick_photo_long_video_timing,
)

log = structlog.get_logger()


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
) -> int:
    """Bound typed mixed-media targets by source capacity before planning.

    Images can be used once for at most 0.8s. Videos can consume their
    probed duration, split into 1.5–3s windows when long enough, without
    stretching source footage. Legacy plans intentionally retain their
    existing integer 3–60s clamp.
    """

    requested = max(3, min(60, int(duration_s)))
    if not uses_quick_photo_long_video_timing(mixed_media_timing):
        return requested

    eligible = [ref for ref in media if ref.kind == "image" or float(ref.duration_s or 0.0) >= 0.4]
    capacities: list[float] = []
    minimum_cuts: list[int] = []
    for ref in eligible:
        if ref.kind == "image":
            capacities.append(mixed_media_hold_bounds("image").maximum_s)
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
    return min(requested, max(3, math.floor(capacity_s)))


def deterministic_fast_cuts(
    media: list[MediaRef],
    duration_s: int,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
    montage_cadence: MontageCadenceConstraint | None = None,
) -> list[FastMontageCut]:
    """Build a strict source-aware montage when the semantic planner is invalid.

    The model still owns the normal creative path. This deterministic compiler
    prevents arithmetic/schema drift from turning an explicit direction change
    into a user-visible failure. Every video window is consumed at most once,
    image sources are used once, and adjacent cuts always use different media.
    """

    if montage_cadence is not None:
        return deterministic_round_robin_cuts(media, duration_s, montage_cadence)

    eligible = [ref for ref in media if ref.kind == "image" or float(ref.duration_s or 0.0) >= 0.4]
    if not eligible:
        raise ValueError("fast montage fallback found no usable media")
    eligible_ids = {ref.media_id for ref in eligible}
    ranked = [ref for _, ref in _ranked_fast_media(media) if ref.media_id in eligible_ids]
    target_duration_s = clamp_fast_montage_target_duration_s(media, duration_s, mixed_media_timing)
    target_ms = target_duration_s * 1000
    quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_media_timing)
    source_capacity_ms = {
        ref.media_id: (
            (
                round(mixed_media_hold_bounds(ref.kind).maximum_s * 1000)
                if quick_mixed_timing
                else 1200
            )
            if ref.kind == "image"
            else math.floor(float(ref.duration_s or 0.0) * 1000)
        )
        for ref in ranked
    }
    if max(source_capacity_ms.values()) < 800:
        raise ValueError("fast montage fallback found no source supporting a primary cut")
    required_sources = minimum_required_sources(
        len(eligible),
        target_duration_s=target_duration_s,
        media=eligible,
        mixed_media_timing=mixed_media_timing,
    )
    available_kinds = {ref.kind for ref in eligible}
    rank = {ref.media_id: index for index, ref in enumerate(ranked)}
    remaining_ms = dict(source_capacity_ms)
    reservations: list[tuple[MediaRef, int, int]] = []
    used_ids: set[str] = set()
    used_kinds: set[str] = set()
    previous_id: str | None = None
    last_pick_was_new = False
    minimum_total_ms = 0
    maximum_total_ms = 0
    while len(reservations) < 80:
        if (
            maximum_total_ms >= target_ms
            and len(used_ids) >= required_sources
            and used_kinds == available_kinds
        ):
            break
        candidates = [
            ref
            for ref in ranked
            if ref.media_id != previous_id and remaining_ms[ref.media_id] >= 400
        ]
        if len(used_ids) < required_sources and (not reservations or not last_pick_was_new):
            unseen = [ref for ref in candidates if ref.media_id not in used_ids]
            if unseen:
                candidates = unseen
        missing_kinds = available_kinds - used_kinds
        if missing_kinds:
            missing_kind_candidates = [ref for ref in candidates if ref.kind in missing_kinds]
            if missing_kind_candidates:
                candidates = missing_kind_candidates
        candidates = [
            ref
            for ref in candidates
            if minimum_total_ms
            + min(
                remaining_ms[ref.media_id],
                round(mixed_media_hold_bounds(ref.kind).minimum_s * 1000)
                if quick_mixed_timing
                else 800,
            )
            <= target_ms
        ]
        if not candidates:
            break
        ref = min(
            candidates,
            key=lambda candidate: (
                -remaining_ms[candidate.media_id],
                rank[candidate.media_id],
            ),
        )
        if quick_mixed_timing:
            bounds = mixed_media_hold_bounds(ref.kind)
            preferred_minimum_ms = round(bounds.minimum_s * 1000)
            preferred_maximum_ms = round(bounds.maximum_s * 1000)
        else:
            preferred_minimum_ms = 800
            preferred_maximum_ms = 1200
        maximum_ms = min(preferred_maximum_ms, remaining_ms[ref.media_id])
        minimum_ms = min(maximum_ms, preferred_minimum_ms)
        was_new = ref.media_id not in used_ids
        reservations.append((ref, minimum_ms, maximum_ms))
        remaining_ms[ref.media_id] -= maximum_ms
        minimum_total_ms += minimum_ms
        maximum_total_ms += maximum_ms
        used_ids.add(ref.media_id)
        used_kinds.add(ref.kind)
        previous_id = ref.media_id
        last_pick_was_new = was_new
    if (
        maximum_total_ms < target_ms
        or minimum_total_ms > target_ms
        or len(used_ids) < required_sources
        or used_kinds != available_kinds
    ):
        raise ValueError("fast montage fallback cannot allocate distinct source windows safely")

    remaining_target_ms = target_ms - minimum_total_ms
    scheduled: list[tuple[MediaRef, int, int]] = []
    consumed_ms = {ref.media_id: 0 for ref in ranked}
    for ref, minimum_ms, maximum_ms in reservations:
        extra_ms = min(remaining_target_ms, maximum_ms - minimum_ms)
        duration_ms = minimum_ms + extra_ms
        remaining_target_ms -= extra_ms
        start_ms = consumed_ms[ref.media_id]
        consumed_ms[ref.media_id] += duration_ms
        scheduled.append((ref, duration_ms, start_ms))
    if remaining_target_ms:
        raise ValueError("fast montage fallback could not preserve the target duration")

    cuts: list[FastMontageCut] = []
    for index, (ref, duration_ms, start_ms) in enumerate(scheduled):
        exact_cut_duration_s = duration_ms / 1000
        start_s = round(start_ms / 1000, 3) if ref.kind == "video" else 0.0
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


def deterministic_guided_beats(media: list[MediaRef], duration_s: int) -> list[StoryBeat]:
    """Build conservative, metadata-free story structure from renderable owned media."""

    eligible = [
        ref
        for ref in media
        if ref.kind == "image" or float(ref.duration_s or 0.0) >= GUIDED_STORY_MIN_MOMENT_S
    ]
    if not eligible:
        raise ValueError("guided story fallback found no usable media")

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

    target_s = max(3, min(60, int(duration_s)))
    source_count = max(
        1,
        min(7, len(ordered), math.floor(target_s / GUIDED_STORY_MIN_MOMENT_S)),
    )
    selected = ordered[:source_count]
    beat_count = min(5, source_count)
    groups: list[list[MediaRef]] = [[] for _ in range(beat_count)]
    for index, ref in enumerate(selected):
        groups[index % beat_count].append(ref)

    durations = [round(GUIDED_STORY_MIN_MOMENT_S * len(group), 3) for group in groups]
    remaining = round(target_s - sum(durations), 3)
    index = 0
    while remaining > 0.001:
        capacity = round(12.0 - durations[index % beat_count], 3)
        if capacity > 0:
            addition = min(remaining, capacity)
            durations[index % beat_count] = round(durations[index % beat_count] + addition, 3)
            remaining = round(remaining - addition, 3)
        index += 1
        if index > beat_count * 2 and remaining > 0.001:
            raise ValueError("guided story fallback cannot allocate target duration")

    copy = [
        ("Opening", "A few moments, together."),
        ("Details", "Details worth noticing."),
        ("Closing", "One last look."),
        ("Another view", "A different angle on the moment."),
        ("Final frame", "A final frame to remember."),
    ]
    return [
        StoryBeat(
            beat_id=f"fallback-beat-{beat_index + 1}",
            topic=copy[beat_index][0],
            thought=copy[beat_index][1],
            thought_source="ai_draft",
            media_ids=[ref.media_id for ref in group],
            layout="fullscreen",
            duration_s=durations[beat_index],
        )
        for beat_index, group in enumerate(groups)
    ]


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
                duration_s=max(1.0, min(12.0, sum(cut.output_duration_s for cut in group))),
            )
        )
    return beats


def plan_direction_snapshot(
    source: EditProposalSnapshot,
    *,
    direction: str,
    goal: str,
    pace: str,
    duration_s: int,
    mixed_media_timing: MixedMediaTimingProfile | None = None,
    montage_audio: MontageAudioPlan | None = None,
    montage_cadence: MontageCadenceConstraint | None = None,
    idea: str = "",
    theme: str = "",
    job_id: str | None = None,
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
    planning_duration_s = (
        clamp_fast_montage_target_duration_s(media, duration_s, mixed_media_timing)
        if direction == "fast_montage"
        else max(3, min(60, int(duration_s)))
    )
    output = None
    used_fallback = False
    try:
        output = EditProposalAgent(default_client()).run(
            EditProposalAgentInput(
                idea=idea[:500],
                theme=theme[:500],
                direction=direction,
                goal=goal[:500],
                pace=pace,
                target_duration_s=planning_duration_s,
                mixed_media_timing=mixed_media_timing,
                montage_audio=montage_audio,
                montage_cadence=montage_cadence,
                media=media,
            ),
            ctx=RunContext(job_id=job_id) if job_id else None,
        )
    except TerminalError as exc:
        if direction == "text_explainer":
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
        )
        if direction == "fast_montage"
        else None
    )
    if direction == "fast_montage":
        if not cuts:
            raise ValueError("fast montage planner returned no source-aware cuts")
        beats = _compatibility_beats(cuts)
    elif output is None:
        beats = deterministic_guided_beats(source.media, duration_s)
    else:
        beats = [
            StoryBeat(
                beat_id=f"beat-{index + 1}",
                topic=beat.topic,
                thought=beat.thought,
                thought_source="ai_draft",
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
            "duration_s": planning_duration_s
            if output is None or direction == "fast_montage"
            else output.duration_s,
            "title": output.title if output is not None else source.title,
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
        }
    )
    result = EditProposalSnapshot.model_validate(planned.model_dump(mode="json"))
    if used_fallback:
        from app.pipeline.guided_story import validate_proposal_timing  # noqa: PLC0415

        validate_proposal_timing(result)
    return result
