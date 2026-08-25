"""Direction-specific replanning over an existing analyzed proposal snapshot."""

from __future__ import annotations

import math

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
    minimum_required_sources,
)
from app.schemas.edit_proposal import EditProposalSnapshot, FastMontageCut, MediaRef, StoryBeat

log = structlog.get_logger()


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


def _fallback_source_window(ref, duration_s: float, occurrence: int) -> tuple[float, float]:  # noqa: ANN001
    if ref.kind == "image":
        return 0.0, round(duration_s, 3)
    source_duration_s = float(ref.duration_s or 0.0)
    raw_moments = (ref.analysis or {}).get("best_moments")
    moments = [moment for moment in raw_moments or [] if isinstance(moment, dict)]
    if moments:
        moment = sorted(
            enumerate(moments),
            key=lambda row: (-_moment_energy(row[1]), row[0]),
        )[occurrence % len(moments)][1]
        try:
            moment_start_s = max(0.0, float(moment.get("start_s", 0.0)))
            moment_end_s = min(source_duration_s, float(moment.get("end_s", source_duration_s)))
        except (TypeError, ValueError):
            moment_start_s, moment_end_s = 0.0, source_duration_s
        if moment_end_s > moment_start_s:
            center_s = (moment_start_s + moment_end_s) / 2
            start_s = min(
                max(0.0, center_s - duration_s / 2),
                source_duration_s - duration_s,
            )
            start_s = round(start_s, 3)
            return start_s, round(start_s + duration_s, 3)
    start_s = max(0.0, (source_duration_s - duration_s) / 2)
    start_s = round(start_s, 3)
    return start_s, round(start_s + duration_s, 3)


def deterministic_fast_cuts(media: list[MediaRef], duration_s: int) -> list[FastMontageCut]:
    """Build a strict source-aware montage when the semantic planner is invalid.

    The model still owns the normal creative path. This deterministic compiler
    prevents arithmetic/schema drift from turning an explicit direction change
    into a user-visible failure, using analyzed strongest moments first.
    """

    eligible = [ref for ref in media if ref.kind == "image" or float(ref.duration_s or 0.0) >= 0.4]
    if not eligible:
        raise ValueError("fast montage fallback found no usable media")
    eligible_ids = {ref.media_id for ref in eligible}
    ranked = [ref for _, ref in _ranked_fast_media(media) if ref.media_id in eligible_ids]
    target_duration_s = max(3, min(60, int(duration_s)))
    target_ms = target_duration_s * 1000
    capacity_ms = {
        ref.media_id: (
            1200
            if ref.kind == "image"
            else min(1200, math.floor(float(ref.duration_s or 0.0) * 1000))
        )
        for ref in ranked
    }
    primary_ceiling_ms = max(capacity_ms.values())
    if primary_ceiling_ms < 800:
        raise ValueError("fast montage fallback found no source supporting a primary cut")
    long_sources = [ref for ref in ranked if capacity_ms[ref.media_id] >= primary_ceiling_ms]
    short_sources = [ref for ref in ranked if ref not in long_sources]

    required_sources = minimum_required_sources(len(eligible))
    selected_short: list[tuple[MediaRef, int]] = []
    short_budget_ms = target_ms - 800
    for ref in short_sources:
        if len(selected_short) >= max(0, required_sources - 1):
            break
        safe_ms = capacity_ms[ref.media_id]
        if sum(duration_ms for _, duration_ms in selected_short) + safe_ms > short_budget_ms:
            continue
        selected_short.append((ref, safe_ms))

    remaining_ms = target_ms - sum(duration_ms for _, duration_ms in selected_short)
    minimum_long_cuts = math.ceil(remaining_ms / primary_ceiling_ms)
    maximum_long_cuts = math.floor(remaining_ms / 800)
    required_long_sources = max(1, required_sources - len(selected_short))
    if maximum_long_cuts < max(minimum_long_cuts, required_long_sources):
        raise ValueError("fast montage fallback cannot satisfy source variety and cut duration")
    long_cut_count = min(
        max(round(remaining_ms / 1000), minimum_long_cuts, required_long_sources),
        maximum_long_cuts,
    )
    if long_cut_count + len(selected_short) > 80:
        raise ValueError("fast montage fallback needs more than 80 safe cuts")
    base_cut_ms, remainder_ms = divmod(remaining_ms, long_cut_count)
    long_durations_ms = [
        base_cut_ms + (1 if index < remainder_ms else 0) for index in range(long_cut_count)
    ]

    scheduled: list[tuple[MediaRef, int]] = []
    short_queue = list(selected_short)
    insertion_interval = math.ceil(long_cut_count / (len(short_queue) + 1))
    for index, duration_ms in enumerate(long_durations_ms):
        scheduled.append((long_sources[index % len(long_sources)], duration_ms))
        if short_queue and (index + 1) % insertion_interval == 0:
            scheduled.append(short_queue.pop(0))
    scheduled.extend(short_queue)

    occurrences: dict[str, int] = {}
    cuts: list[FastMontageCut] = []
    for index, (ref, duration_ms) in enumerate(scheduled):
        exact_cut_duration_s = duration_ms / 1000
        occurrence = occurrences.get(ref.media_id, 0)
        occurrences[ref.media_id] = occurrence + 1
        start_s, end_s = _fallback_source_window(ref, exact_cut_duration_s, occurrence)
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

    eligible = [ref for ref in media if ref.kind == "image" or float(ref.duration_s or 0.0) >= 1.4]
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
    source_count = max(1, min(7, len(ordered), math.floor(target_s / 1.4)))
    selected = ordered[:source_count]
    beat_count = min(5, source_count)
    groups: list[list[MediaRef]] = [[] for _ in range(beat_count)]
    for index, ref in enumerate(selected):
        groups[index % beat_count].append(ref)

    durations = [round(1.4 * len(group), 3) for group in groups]
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
                target_duration_s=max(3, min(60, int(duration_s))),
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
        else deterministic_fast_cuts(source.media, duration_s)
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
            "duration_s": int(duration_s)
            if output is None or direction == "fast_montage"
            else output.duration_s,
            "title": output.title if output is not None else source.title,
            "story_beats": beats,
            "fast_cuts": cuts,
        }
    )
    result = EditProposalSnapshot.model_validate(planned.model_dump(mode="json"))
    if used_fallback:
        from app.pipeline.guided_story import validate_proposal_timing  # noqa: PLC0415

        validate_proposal_timing(result)
    return result
