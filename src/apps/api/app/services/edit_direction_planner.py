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
    maximum_distinct_sources,
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
    media_context_group,
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
    return min(requested, max(3, math.floor(capacity_s)))


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

    quick_mixed_timing = uses_quick_photo_long_video_timing(mixed_media_timing)
    minimum_video_s = 0.1 if quick_mixed_timing else 0.4
    eligible = [
        ref
        for ref in media
        if ref.kind == "image" or float(ref.duration_s or 0.0) >= minimum_video_s
    ]
    if not eligible:
        raise ValueError("fast montage fallback found no usable media")
    eligible_ids = {ref.media_id for ref in eligible}
    ranked = [ref for _, ref in _ranked_fast_media(media) if ref.media_id in eligible_ids]
    target_duration_s = clamp_fast_montage_target_duration_s(media, duration_s, mixed_media_timing)
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
        maximum_distinct_sources(
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
            if ref.media_id != previous_id
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
        else:
            preferred_minimum_frames = int(round(0.8 * fps))
            preferred_maximum_frames = int(round(1.2 * fps))
        maximum_frames = min(preferred_maximum_frames, remaining_frames[ref.media_id])
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
        start_frames = consumed_frames[ref.media_id]
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
