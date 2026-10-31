"""Server-authored duration-plan envelopes shared by proposal and montage paths."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from app.kria.media_sources import PROXY_ORIGINAL_DURATION_TOLERANCE_S, MediaUploadContract
from app.pipeline.best_parts import (
    BestPart,
    BestPartsConflict,
    BestPartsResult,
    edit_cap_s,
    is_fixed_moment,
    select_best_parts,
    select_moment_windows,
    select_pinned_moment_windows,
)
from app.pipeline.hidden_parts import (
    HiddenPart,
    hidden_parts_from_strategy,
    keep_out,
    parts_by_media,
    shows_hidden,
    usable_s,
)
from app.schemas.edit_proposal import (
    MIN_PROPOSAL_DURATION_S,
    MixedMediaTimingProfile,
    mixed_media_hold_bounds,
    pace_selection_limits,
    uses_quick_photo_long_video_timing,
)
from app.services.length_policy import (
    DurationBound,
    creator_duration_satisfied,
    effective_pace,
    length_owner,
    natural_length_owner,
)
from app.services.single_hero_policy import (
    SingleHeroPolicyError,
    _build_single_hero_recipe,
    _single_hero_order,
)

LENGTH_PLAN_VERSION = 1
# KRI-723: an envelope that records the creator's named moments (``moments``). Only such an
# envelope is written as v2, so every other plan stays byte-identical v1.
MOMENT_LENGTH_PLAN_VERSION = 2
SUPPORTED_LENGTH_PLAN_VERSIONS = frozenset({LENGTH_PLAN_VERSION, MOMENT_LENGTH_PLAN_VERSION})


def supported_length_plan_version(envelope: Mapping[str, Any] | None) -> bool:
    """The one version check every reader of an approved envelope uses (KRI-723)."""
    version = envelope.get("version") if isinstance(envelope, Mapping) else None
    return type(version) is int and version in SUPPORTED_LENGTH_PLAN_VERSIONS


# A still has no measured source duration.  This is deliberately kept here,
# rather than imported from ``unified_montage``, so the shared proposal plan
# does not depend on the renderer (which reads this envelope).
_STILL_CAPACITY_S = 3.0
_STILL_PREFERRED_WINDOW_S = 1.2
# "Six moments" with no length: each moment gets this much (KRI-596).
DEFAULT_MOMENT_S = 2.5
# KRI-681: above every analysed moment's energy, so a sentence the creator described a
# clip by (``spoken_quotes.described_line_moments``) is that clip's window.
DESCRIBED_MOMENT_ENERGY = 100.0
# ...and the clip keeps at least that sentence (up to this long) when lengths are shared.
DESCRIBED_MOMENT_FLOOR_MAX_S = 4.0
# KRI-585: a quiet stretch (``spoken_roles.quiet_window``) is its clip's window and its
# ceiling, and keeps at least a readable shot when lengths are shared.
QUIET_MOMENT_FLOOR_S = 1.2


def _number(value: object) -> float | None:
    return (
        float(value)
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
        else None
    )


def _sources(
    snapshot: Mapping[str, Any],
    selected_ids: Sequence[str],
    strategy: Mapping[str, Any],
    spoken_moments: Mapping[str, tuple[float, float]] | None = None,
    quiet_moments: Mapping[str, tuple[float, float]] | None = None,
    still_holds: Mapping[str, float] | None = None,
    kept_windows: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    selected = set(selected_ids)
    still_holds = still_holds or {}
    rows = snapshot.get("clip_assignments")
    if not isinstance(rows, list):
        return []
    sources = []
    raw_profile = strategy.get("mixed_media_timing")
    profile = (
        MixedMediaTimingProfile.model_validate(raw_profile) if raw_profile is not None else None
    )
    timing_bounds_apply = uses_quick_photo_long_video_timing(profile)
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("media_id"), str):
            continue
        if selected and row["media_id"] not in selected:
            continue
        kind = row.get("kind")
        kind = kind if kind in {"image", "video"} else "video"
        duration_s = _number(row.get("duration_s"))
        upload = row.get("upload_contract")
        if (
            kind == "video"
            and isinstance(upload, Mapping)
            and upload.get("purpose") == "analysis_proxy"
        ):
            contract = MediaUploadContract.model_validate(upload)
            proxy = contract.proxy
            if proxy is None or duration_s is None or not isinstance(row.get("has_audio"), bool):
                raise ValueError("phone timing requires verified source metadata")
            proxy.verify_registered(duration_s, row["has_audio"])
            if proxy.original.kind != "video":
                raise ValueError("video timing requires a video original")
            # V1 proxy timestamps share the original clock, but a CFR preview
            # may carry a partial extra frame. Plan against the bytes the phone
            # actually renders, before the creator approves the source windows.
            duration_s = proxy.original.duration_s
        analysis = row.get("analysis")
        analysis = dict(analysis) if isinstance(analysis, Mapping) else {}
        held_s = still_holds.get(row["media_id"]) if kind == "image" else None
        bounds = (
            mixed_media_hold_bounds(kind, profile, held_s)
            if timing_bounds_apply or held_s is not None
            else None
        )
        if kind == "image":
            # Photos are rendered as synthetic holds.  Seed the selector with
            # the normal 1.2s montage hold, while retaining 3s of capacity for
            # a requested or minimum-duration allocation.
            duration_s = bounds.maximum_s if bounds else _STILL_CAPACITY_S
            preferred_s = bounds.preferred_s if bounds else _STILL_PREFERRED_WINDOW_S
            analysis["best_moments"] = [{"start_s": 0.0, "end_s": preferred_s, "energy": 0.0}]
        spoken_floor_s = None
        if kind == "video" and spoken_moments and row["media_id"] in spoken_moments:
            # KRI-681: the sentence the creator described this clip by is its window.
            start, end = spoken_moments[row["media_id"]]
            analysis["best_moments"] = [
                {"start_s": start, "end_s": end, "energy": DESCRIBED_MOMENT_ENERGY},
                *(analysis.get("best_moments") or []),
            ]
            spoken_floor_s = round(min(end - start, DESCRIBED_MOMENT_FLOOR_MAX_S), 3)
        quiet_ceiling_s = None
        if kind == "video" and quiet_moments and row["media_id"] in quiet_moments:
            # KRI-585: a pinned closing clip whose words only introduce someone plays its
            # stretch without words, and never more than that stretch.
            start, end = quiet_moments[row["media_id"]]
            analysis["best_moments"] = [
                {"start_s": start, "end_s": end, "energy": DESCRIBED_MOMENT_ENERGY},
                *(analysis.get("best_moments") or []),
            ]
            quiet_ceiling_s = round(end - start, 3)
            spoken_floor_s = round(min(quiet_ceiling_s, QUIET_MOMENT_FLOOR_S), 3)
        if kind == "video" and kept_windows and row["media_id"] in kept_windows:
            # KRI-1028: a clip kept audible for a line the creator named keeps its whole
            # sentence when lengths are shared, past the 4 s cap: the silent clips give way.
            spoken_floor_s = round(kept_windows[row["media_id"]], 3)
        sources.append(
            {
                "media_id": row["media_id"],
                # ``generation`` is the proposal identity; storage_generation
                # maps it to the immutable object selected at runtime.
                "generation": row.get("generation"),
                "storage_generation": row.get("storage_generation", row.get("generation")),
                "gcs_path": row.get("gcs_path", row.get("proxy_path")),
                "kind": kind,
                "duration_s": duration_s or 0.0,
                "minimum_duration_s": bounds.minimum_s
                if bounds is not None
                else spoken_floor_s or 0.1,
                # Allocation policy is distinct from video source duration:
                # the latter remains in the approved identity envelope so a
                # late analysed moment (for example 7s–10s of a 10s clip)
                # stays valid when the allowed rendered hold is only 3s.
                "maximum_duration_s": bounds.maximum_s if bounds is not None else None,
                # A quiet stretch caps its window but is no capacity policy: the
                # clarification capacity below stays the measured footage.
                "quiet_ceiling_s": quiet_ceiling_s,
                "hook_score": _number(analysis.get("hook_score"))
                or _number(row.get("hook_score"))
                or 0.0,
                "analysis": analysis,
            }
        )
    return sources


def _fit_kept_floors(
    sources: Sequence[dict[str, Any]], kept_windows: Mapping[str, float], budget: float | None
) -> None:
    """Shrink a kept sentence's floor to what the creator's length leaves it (KRI-1028).

    Every other clip keeps its own floor; a kept window the length cannot hold beside them is
    trimmed by the selector, and the plan's ``held`` flag and the receipt say so.
    """
    if not kept_windows or budget is None:
        return
    for source in sources:
        if source["media_id"] not in kept_windows:
            continue
        others = sum(s["minimum_duration_s"] for s in sources if s is not source)
        room = round(budget - others, 3)
        if source["minimum_duration_s"] > room:
            source["minimum_duration_s"] = max(0.1, room)


def _editorial_reading_floors(
    strategy: Mapping[str, Any], sources: list[dict[str, Any]], budget: float | None
) -> list[BestPartsConflict]:
    """Reserve readable explanations before windows are shown for approval.

    Three seconds accommodates the writer's maximum minimum reading time. This
    only allocates the existing duration budget; it never extends an approved edit.
    """
    if strategy.get("editorial_plan") is None:
        return []
    from app.schemas.editorial_plan import EditorialPlan

    plan = EditorialPlan.model_validate(strategy["editorial_plan"])
    by_id = {source["media_id"]: source for source in sources}
    conflicts = []
    for beat in plan.beats:
        if not beat.text_goal:
            continue
        group = [by_id[mid] for mid in beat.media_ids if mid in by_id]
        floor = max(3.0, sum(source["minimum_duration_s"] for source in group))
        allocation = select_best_parts(
            group,
            target_duration_s=floor,
            minimum_duration_s={row["media_id"]: row["minimum_duration_s"] for row in group},
            maximum_duration_s={
                row["media_id"]: row["maximum_duration_s"]
                for row in group
                if row["maximum_duration_s"] is not None
            },
        )
        if allocation.conflicts or allocation.duration_s < floor - 1 / 30:
            conflicts.append(
                BestPartsConflict(
                    "editorial_text_capacity",
                    floor,
                    allocation.capacity_s,
                    "This story beat needs more footage time for its explanation: " + beat.purpose,
                )
            )
        else:
            for part in allocation.parts:
                by_id[part.media_id]["minimum_duration_s"] = part.duration_s
    required = sum(source["minimum_duration_s"] for source in sources)
    if budget is not None and required > budget + 0.001:
        conflicts.append(
            BestPartsConflict(
                "editorial_text_capacity",
                required,
                budget,
                "The selected duration cannot fit readable explanations for every planned beat.",
            )
        )
    return conflicts


def _field(source: Any, name: str) -> Any:
    return source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)


PACE_MIN_HEADROOM_S = 0.1


def paced_selection(
    sources: Sequence[Any],
    pace: str | None,
    *,
    budget: float | None,
    extend_budget: bool,
    cap_s: float,
    minimums: Mapping[str, float],
    maximums: Mapping[str, float],
    story_floor_s: float | None = None,
) -> BestPartsResult | None:
    """The source windows with the planner's pace applied, or None to keep the plain ones.

    Used by approval-time planning and by the render, so a pre-approval plan and the worker's
    plan follow one rule (``schemas.edit_proposal.pace_selection_limits``). Pace is a
    preference, never a reason to fail: it changes how long each VIDEO clip is held (photos
    keep their own hold rules), never drops a clip, and the result is used only when the
    selector reports no conflict and the video is still long enough. The one place the total
    may move is a relaxed pace over a catalog song section (``extend_budget``): that section
    is a default nobody stated, so the clips' longer holds extend it by the least they need
    rather than dropping a clip or ignoring the pace.

    ``story_floor_s`` (text-explainer unit) is the shortest hold a story direction gives a clip
    (``length_policy.story_hold_floor_s``); it rides the same rule, with ``pace`` None when the
    planner said nothing about pace.
    """

    floor_s, ceiling_s = (
        pace_selection_limits(pace, total_free=budget is None) if pace is not None else (None, None)
    )
    if story_floor_s is not None:
        floor_s = max(floor_s or 0.0, story_floor_s)
    if floor_s is None and ceiling_s is None:
        return None
    paced_min: dict[str, float] = dict(minimums)
    paced_max: dict[str, float] = dict(maximums)
    videos = [source for source in sources if _field(source, "kind") != "image"]
    for source in videos:
        media_id = str(_field(source, "media_id"))
        if floor_s is not None:
            paced_min[media_id] = max(paced_min.get(media_id, 0.0), floor_s)
        if ceiling_s is not None:
            # Never below a floor another rule set (a quoted sentence, a reading time).
            current = paced_max.get(media_id)
            ceiling = ceiling_s if current is None else min(current, ceiling_s)
            paced_max[media_id] = max(ceiling, paced_min.get(media_id, 0.0))
    paced_budget = budget
    if budget is not None and extend_budget and floor_s is not None:
        needed = round(
            sum(
                min(paced_min[str(_field(s, "media_id"))], float(_field(s, "duration_s") or 0.0))
                for s in videos
            )
            + sum(
                paced_min.get(str(_field(s, "media_id")), 0.0)
                for s in sources
                if _field(s, "kind") == "image"
            ),
            3,
        )
        if budget < needed <= cap_s:
            paced_budget = needed
    result = select_best_parts(
        list(sources),
        target_duration_s=paced_budget,
        minimum_total_duration_s=MIN_PROPOSAL_DURATION_S if paced_budget is None else None,
        minimum_duration_s=paced_min,
        maximum_duration_s=paced_max,
        cap_duration_s=cap_s,
    )
    if result.conflicts or result.duration_s < MIN_PROPOSAL_DURATION_S:
        return None
    if paced_budget is None and result.duration_s < MIN_PROPOSAL_DURATION_S + PACE_MIN_HEADROOM_S:
        # A free total that lands exactly on the minimum is 2.967 s after whole-frame rounding
        # and the montage is refused (KRI-993 steak edit): keep the plain windows. Here, not in
        # the render alone, so the approval-time plan and the worker's follow one rule.
        return None
    return result


def build_length_plan(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any],
    *,
    creator_target_s: float | None = None,
    creator_target_bound: DurationBound = "exact",
    selected_ids: Sequence[str] = (),
    order_ids: Sequence[str] = (),
    licensed_music: Mapping[str, Any] | None = None,
    closing_quote: Mapping[str, Any] | None = None,
    moment_count: int | None = None,
    moment_max_s: float | None = None,
    moment_each_s: float | None = None,
    spoken_moments: Mapping[str, tuple[float, float]] | None = None,
    quiet_moments: Mapping[str, tuple[float, float]] | None = None,
    spoken_order: Mapping[str, Any] | None = None,
    named_lines: Sequence[Mapping[str, Any]] | None = None,
    kept_lines: Sequence[Mapping[str, Any]] | None = None,
    still_holds: Mapping[str, float] | None = None,
    pinned_moments: Sequence[Mapping[str, Any]] | None = None,
    skipped_moments: Sequence[Mapping[str, Any]] | None = None,
    attached_visual_ids: Collection[str] = (),
    visual_budget_s: float = 0.0,
    story_floor_s: float | None = None,
) -> dict[str, Any]:
    """Build the approved, serializable source-window plan.

    KRI-989: ``attached_visual_ids`` are manifest Visuals (``asset-*`` pool media) the worker
    places beside the footage: attached, not source rows, never missing. Their seconds stay
    with ``asset_holds.visual_budget`` (already off the creator's target), so they are neither
    sources nor counted twice; an id in neither the clip rows nor this set is truly gone.
    ``visual_budget_s`` is that budget's total, recorded on the plan as ``visual_budget_s``
    so a reader of its length counts the Visuals too (the windows alone ran a 25 s montage
    with three photos as 21.4 s, Leverkusen M1 run 2); a plan with no placed Visual omits it.

    KRI-723: ``pinned_moments`` (``named_moments.Pin.json()``, the creator's final order) are
    the named moments of the edit's one take with their authority: the creator's own pointed
    window, a grounded interval, or "you pick". Each gets exactly one window, in that order,
    and the plan records them as ``moments`` (envelope v2) so the worker plays them unchanged
    and the receipt judges each by its evidence. ``skipped_moments`` ("not in my clips") ride
    along as ``skipped_moments``.

    KRI-824: ``still_holds`` (media id -> seconds, ``asset_holds.asset_holds``) are photos
    the creator gave their own length; each one's window is exactly that long.

    Only ``creator_target_s`` supplied by a trusted boundary becomes a hard
    target. Strategy fields are intentionally never treated as proof here.
    ``creator_target_bound="at_most"`` (KRI-847, "at most 10 seconds") drops only the lower
    side of the ten-percent check: footage shorter than the target is a plan, not a shortfall.
    Every upper tolerance (floors, lifts) stays exactly the exact path's.

    A montage may take several moments from one take (KRI-596): ``moment_count``
    moments, or as many ``moment_max_s`` moments as the target needs, when that is
    more than one per take; with no creator total they are ``moment_each_s`` long, the
    length the creator gave each one (KRI-776). A found ``closing_quote`` (KRI-597,
    ``spoken_quotes.ground_closing_quote``) pins its take's last window to the
    creator's quoted line; the record rides on the plan either way, so the
    receipt can say whether the line was found and held; a song-owned montage pins it
    too (KRI-681). ``spoken_moments`` (``spoken_quotes.described_line_moments``) seats a
    clip's window on the sentence the creator described it by.

    KRI-585: ``quiet_moments`` seats a clip's window inside a stretch with no words and
    caps it there; ``spoken_order`` (``spoken_roles.SpokenSeats.record``) rides on the plan
    as ``spoken_order`` so the reply reads what was seated.

    KRI-760: ``named_lines`` (``spoken_quotes.named_line_records``) rides on the plan as
    ``named_lines``, where each line the creator wants heard whole is said, so the receipt
    can check the finished cut holds it.

    KRI-1028: ``kept_lines`` (``kept_sound_lines.kept_line_records``) are clips that keep their
    sound for a line the creator named; the caller seats each one's window through
    ``spoken_moments``. Here that whole window is the clip's floor, so the clips around it share
    what is left, and a floor the creator's length can't hold shrinks to what fits (the receipt
    then says the sentence isn't whole). Each record rides on the plan as ``kept_lines`` with
    ``held`` true when its clip's window holds the sentence.

    ``story_floor_s`` (text-explainer unit, ``length_policy.story_hold_floor_s``): the planner's
    direction is a story and nothing the creator asked keeps it a fast montage, so every video
    clip is held at least this long. It rides the pace rule: with no creator length a catalog
    song section nobody stated is extended by the least the holds need; a creator's length is
    never moved (when the holds do not fit it the plain windows stand and the render cuts a fast
    montage and says so). Recorded as ``story`` only when it changed the windows.
    """

    natural_owner = natural_length_owner(strategy)
    cap_s = edit_cap_s(natural_owner)
    owner = length_owner(strategy, creator_duration_proven=creator_target_s is not None)
    kept_windows = {
        str(row["media_id"]): float(row["window_end_s"]) - float(row["window_start_s"])
        for row in kept_lines or ()
        if spoken_moments and row.get("media_id") in spoken_moments
    }
    sources = _sources(
        media_snapshot,
        selected_ids,
        strategy,
        spoken_moments,
        quiet_moments,
        still_holds,
        kept_windows,
    )
    by_id = {source["media_id"]: source for source in sources}
    if len(by_id) != len(sources):
        raise ValueError("duplicate length plan source")
    # Leverkusen M2: a clip with a part the creator hid offers only what it may still show.
    hidden = {
        media_id: parts
        for media_id, parts in parts_by_media(hidden_parts_from_strategy(strategy)).items()
        if media_id in by_id and by_id[media_id]["kind"] == "video"
    }
    for media_id, parts in hidden.items():
        source = by_id[media_id]
        room = usable_s(source["duration_s"], parts)
        current = source["maximum_duration_s"]
        source["maximum_duration_s"] = room if current is None else min(current, room)
    ordered_sources = [
        by_id[media_id] for media_id in dict.fromkeys(order_ids) if media_id in by_id
    ]
    ordered_sources.extend(source for source in sources if source not in ordered_sources)
    result: BestPartsResult | None = None
    pace_record: dict[str, Any] | None = None
    story_record: dict[str, Any] | None = None
    audio = strategy.get("audio_strategy")
    section = strategy.get("selected_music_section")
    section_duration = None
    if isinstance(section, Mapping):
        start, end = _number(section.get("start_s")), _number(section.get("end_s"))
        if start is not None and end is not None and end > start:
            section_duration = end - start
    voice_duration = _number(strategy.get("voiceover_duration_s"))
    eligible = natural_owner in {"footage", "voiceover"} or (
        natural_owner == "song" and audio == "licensed_music" and section_duration is not None
    )
    natural_duration_s = (
        voice_duration
        if natural_owner == "voiceover"
        else section_duration
        if natural_owner == "song"
        else None
    )
    _fit_kept_floors(
        ordered_sources,
        kept_windows,
        creator_target_s if creator_target_s is not None else natural_duration_s,
    )
    pre_conflicts: list[BestPartsConflict] = (
        _editorial_reading_floors(
            strategy,
            ordered_sources,
            creator_target_s if creator_target_s is not None else natural_duration_s,
        )
        if eligible
        else []
    )
    is_single_hero = strategy.get("edit_format") == "single_hero"
    # A proven creator target changes the owner label, not the source-window
    # allocator: footage still supplies the exact realizable windows.
    single_hero_eligible = is_single_hero and eligible
    if single_hero_eligible:
        physical_durations = {source["media_id"]: source["duration_s"] for source in sources}
        hero_order = _single_hero_order(
            [
                SimpleNamespace(clip_id=source["media_id"], hook_score=source["hook_score"])
                for source in sources
            ],
            {source["media_id"]: source["media_id"] for source in sources},
            physical_durations,
        )
        pinned_order = [media_id for media_id in dict.fromkeys(order_ids) if media_id in by_id]
        if pinned_order and pinned_order != hero_order:
            pre_conflicts.append(
                BestPartsConflict(
                    "single_hero_order_conflict",
                    float(len(pinned_order)),
                    float(len(hero_order)),
                    "The approved source order conflicts with the required "
                    "single-hero composition.",
                )
            )
        hero_sources = [by_id[media_id] for media_id in hero_order]
        maximums = {
            source["media_id"]: source["maximum_duration_s"]
            for source in hero_sources
            if source["maximum_duration_s"] is not None
        }
        minimums = {source["media_id"]: source["minimum_duration_s"] for source in hero_sources}
        natural_parts = select_best_parts(
            hero_sources,
            minimum_duration_s=minimums,
            maximum_duration_s=maximums,
            cap_duration_s=cap_s,
        )
        requested = (
            creator_target_s
            if creator_target_s is not None
            else natural_duration_s
            if natural_duration_s is not None
            else natural_parts.duration_s
        )
        physical_allowable_s = sum(
            min(
                source["duration_s"],
                source["maximum_duration_s"]
                if source["maximum_duration_s"] is not None
                else source["duration_s"],
            )
            for source in hero_sources
        )
        # One 3-second hero and a 100 ms inclusion for every cutaway is the
        # smallest realizable composition. As with the portable source floor,
        # accept that tiny adjustment only when it remains inside the creator's
        # approved duration tolerance; retain their original target on the
        # envelope for the answer/audit boundary.
        composition_floor = 3.0 + 0.1 * max(0, len(hero_order) - 1)
        if (
            creator_target_s is not None
            and requested < composition_floor
            and creator_duration_satisfied(
                composition_floor, creator_target_s, creator_target_bound
            )
        ):
            requested = composition_floor
        natural_floor_expansion = (
            creator_target_s is None
            and natural_duration_s is None
            and requested < composition_floor
            and physical_allowable_s >= composition_floor
        )
        if natural_floor_expansion:
            requested = composition_floor
        available_footage_s = (
            physical_allowable_s
            if (
                creator_target_s is not None
                or natural_duration_s is not None
                or natural_floor_expansion
            )
            else natural_parts.duration_s
        )
        try:
            recipe = _build_single_hero_recipe(
                hero_order,
                available_footage_s=available_footage_s,
                clip_durations_s={
                    source["media_id"]: source["duration_s"] for source in hero_sources
                },
                max_duration_s=requested,
            )
        except SingleHeroPolicyError as exc:
            pre_conflicts.append(BestPartsConflict(exc.reason, requested, 0.0, str(exc)))
            result = BestPartsResult((), round(requested, 3), 0.0, 0.0)
        else:
            slot_results = [
                select_best_parts(
                    [by_id[media_id]],
                    target_duration_s=slot["target_duration_s"],
                    minimum_duration_s={media_id: by_id[media_id]["minimum_duration_s"]},
                    maximum_duration_s={media_id: by_id[media_id]["maximum_duration_s"]}
                    if by_id[media_id]["maximum_duration_s"] is not None
                    else {},
                    cap_duration_s=cap_s,
                )
                for media_id, slot in zip(hero_order, recipe["slots"], strict=True)
            ]
            parts = tuple(part for selected in slot_results for part in selected.parts)
            selector_conflicts = tuple(
                conflict for selected in slot_results for conflict in selected.conflicts
            )
            actual_duration = round(sum(part.duration_s for part in parts), 3)
            required_duration = (
                creator_target_s if creator_target_s is not None else natural_duration_s
            )
            if required_duration is not None and (
                not creator_duration_satisfied(
                    actual_duration, required_duration, creator_target_bound
                )
                if creator_target_s is not None
                else abs(actual_duration - required_duration) > 0.001
            ):
                pre_conflicts.append(
                    BestPartsConflict(
                        "insufficient_capacity",
                        required_duration,
                        actual_duration,
                        "The single-hero source windows cannot reach the required duration.",
                    )
                )
            result = BestPartsResult(
                parts,
                round(requested, 3),
                actual_duration,
                round(sum(selected.capacity_s for selected in slot_results), 3),
                selector_conflicts,
            )
    elif eligible and (
        moment_windows := _moment_or_quote_windows(
            strategy,
            ordered_sources,
            creator_target_s,
            natural_owner,
            closing_quote,
            moment_count,
            moment_max_s,
            natural_duration_s,
            moment_each_s,
            pinned_moments,
        )
    ):
        result = moment_windows
    elif eligible:
        budget = creator_target_s if creator_target_s is not None else natural_duration_s
        raw_capacity = min(cap_s, round(sum(source["duration_s"] for source in ordered_sources), 3))
        if creator_target_s is not None and creator_target_s <= cap_s:
            # Source bounds and the portable minimum can satisfy a target
            # within its approved tolerance without inventing more footage.
            # Keep the creator's original target in the contract/envelope.
            if budget > raw_capacity and creator_duration_satisfied(
                raw_capacity, creator_target_s, creator_target_bound
            ):
                budget = raw_capacity
            if budget < 3 <= raw_capacity and creator_duration_satisfied(
                3, creator_target_s, creator_target_bound
            ):
                budget = 3.0
        coverage_floor = round(sum(source["minimum_duration_s"] for source in ordered_sources), 3)
        if budget is not None and budget < coverage_floor <= budget * 1.1:
            budget = coverage_floor
        minimums = {source["media_id"]: source["minimum_duration_s"] for source in ordered_sources}
        maximums = {
            source["media_id"]: source["maximum_duration_s"]
            if source["maximum_duration_s"] is not None
            else source["quiet_ceiling_s"]
            for source in ordered_sources
            if source["maximum_duration_s"] is not None or source["quiet_ceiling_s"] is not None
        }
        result = select_best_parts(
            ordered_sources,
            target_duration_s=budget,
            minimum_total_duration_s=3 if budget is None else None,
            minimum_duration_s=minimums,
            maximum_duration_s=maximums,
            cap_duration_s=cap_s,
        )
        # A creator-stated length always wins over pace; without one, the planner's pace sets
        # how long each clip is held (``pace_selection_limits``).
        pace = effective_pace(strategy) if creator_target_s is None else None
        if pace is not None or story_floor_s is not None:
            paced = paced_selection(
                ordered_sources,
                pace,
                budget=budget,
                extend_budget=natural_owner == "song" and creator_target_s is None,
                cap_s=cap_s,
                minimums=minimums,
                maximums=maximums,
                story_floor_s=story_floor_s,
            )
            if paced is not None and (paced.duration_s, paced.parts) != (
                result.duration_s,
                result.parts,
            ):
                result = paced
                moved = {
                    "total_s": paced.duration_s,
                    **(
                        {"natural_duration_s": round(budget, 3)}
                        if budget is not None and paced.duration_s > budget + 0.001
                        else {}
                    ),
                }
                if pace is not None:
                    pace_record = {"pace": pace, **moved}
                if story_floor_s is not None:
                    story_record = {"floor_s": story_floor_s, **moved}
    if hidden and result is not None:
        result, unplaceable = _kept_out_of_hidden(result, by_id, hidden)
        if unplaceable:
            pre_conflicts.append(
                BestPartsConflict(
                    HIDDEN_PART_UNPLACEABLE,
                    float(len(unplaceable)),
                    0.0,
                    "A window cannot leave out the part of its clip the creator hid.",
                )
            )
    windows = result.receipt() if result is not None else []
    conflicts = [
        {
            "code": conflict.code,
            "requested_duration_s": conflict.requested_duration_s,
            "available_duration_s": conflict.available_duration_s,
            "message": conflict.message,
        }
        for conflict in pre_conflicts
    ] + (
        [
            {
                "code": conflict.code,
                "requested_duration_s": conflict.requested_duration_s,
                "available_duration_s": conflict.available_duration_s,
                "message": conflict.message,
            }
            for conflict in result.conflicts
        ]
        if result is not None
        else []
    )
    if (
        (eligible or single_hero_eligible)
        and result is not None
        and result.duration_s < MIN_PROPOSAL_DURATION_S
        and not conflicts
    ):
        conflicts.append(
            {
                "code": "snapshot_min_duration",
                "requested_duration_s": MIN_PROPOSAL_DURATION_S,
                "available_duration_s": result.duration_s,
                "message": "The selected originals cannot supply the minimum montage duration.",
            }
        )
    # The selector deliberately omits unusable rows from its allocation.  The
    # plan cannot do that: every selected source is an approved input and a
    # missing/too-short metadata row must remain a visible constraint even when
    # another clip can supply the requested duration.
    if any(source["duration_s"] < 0.1 for source in ordered_sources) and not any(
        conflict["code"] == "source_below_minimum" for conflict in conflicts
    ):
        conflicts.append(
            {
                "code": "source_below_minimum",
                "requested_duration_s": 0.1,
                "available_duration_s": result.capacity_s if result is not None else 0.0,
                "message": "Every selected source needs at least a 0.1 second usable window.",
            }
        )
    missing_ids = set(selected_ids) - set(by_id) - set(attached_visual_ids)
    max_source_count = 4 if is_single_hero else 80
    if (eligible or single_hero_eligible) and len(sources) > max_source_count:
        conflicts.append(
            {
                "code": "source_count_limit",
                "requested_duration_s": creator_target_s,
                "available_duration_s": float(max_source_count),
                "message": f"The portable recipe can include at most {max_source_count} sources.",
            }
        )
    if missing_ids:
        conflicts.append(
            {
                "code": "no_usable_sources",
                "requested_duration_s": creator_target_s,
                "available_duration_s": 0.0,
                "message": "Some selected clips no longer have usable source metadata.",
            }
        )
    elif selected_ids and not sources:
        # KRI-990: every selected id is an attached Visual (a photo): nothing to plan around.
        conflicts.append(
            {
                "code": "no_video_sources",
                "requested_duration_s": creator_target_s,
                "available_duration_s": 0.0,
                "message": "The selection has no video clip to plan around.",
            }
        )
    physical_capacity_s = min(
        cap_s, round(sum(source["duration_s"] for source in ordered_sources), 3)
    )
    # Durable clarification reads ``capacity_raw_s`` rather than the transient
    # selector receipt. For bounded mixed-media edits it must therefore expose
    # the largest policy-compliant allocation, not the measured video runtime.
    # Single-hero is stricter: its recipe can reduce otherwise usable footage
    # to preserve the hero share, so the actual allocated total is the only
    # safe alternate duration for an impossible creator target.
    if is_single_hero and result is not None:
        capacity_raw_s = result.duration_s
    elif any(source["maximum_duration_s"] is not None for source in ordered_sources):
        capacity_raw_s = result.capacity_s if result is not None else 0.0
    else:
        capacity_raw_s = physical_capacity_s
    moments_record = (
        _moments_record(pinned_moments, windows)
        if pinned_moments and result is not None and result.parts
        else None
    )
    plan: dict[str, Any] = {
        "version": LENGTH_PLAN_VERSION if moments_record is None else MOMENT_LENGTH_PLAN_VERSION,
        "owner": owner,
        "natural_owner": natural_owner,
        "target_duration_s": creator_target_s,
        # KRI-847: present only for a ceiling, so a plan for an exact length is unchanged.
        **({"target_duration_bound": "at_most"} if creator_target_bound == "at_most" else {}),
        "natural_duration_s": natural_duration_s,
        "capacity_raw_s": capacity_raw_s,
        "window_eligible": eligible or single_hero_eligible,
        "capacity_s": result.capacity_s if result is not None else None,
        "max_source_count": max_source_count,
        "sources": [
            {
                field_name: source[field_name]
                for field_name in (
                    "media_id",
                    "generation",
                    "storage_generation",
                    "gcs_path",
                    "kind",
                    "duration_s",
                )
            }
            for source in ordered_sources
        ],
        "windows": windows,
        "conflicts": conflicts,
    }
    if visual_budget_s > 0:
        plan["visual_budget_s"] = round(float(visual_budget_s), 3)
    if pace_record is not None:
        # Present only when pace changed the windows, so every other plan is unchanged.
        plan["pace"] = pace_record
    if story_record is not None:
        # Likewise for a story direction's minimum hold.
        plan["story"] = story_record
    if missing_ids:
        # KRI-989: the ids that are genuinely gone, so the question can offer to leave them out.
        plan["missing_source_ids"] = sorted(missing_ids)
    if moments_record is not None:
        plan["moments"] = moments_record
        if skipped_moments:
            plan["skipped_moments"] = [dict(row) for row in skipped_moments]
    if closing_quote is not None:
        quote = dict(closing_quote)
        held = any(part.reason == "closing_quote" for part in (result.parts if result else ()))
        if quote.get("status") == "found" and not held:
            line_s = (_number(quote.get("end_s")) or 0.0) - (_number(quote.get("start_s")) or 0.0)
            # Found, but the creator's length cannot also hold the whole line.
            too_long = creator_target_s is not None and line_s >= creator_target_s
            quote["status"] = "too_long" if too_long else "not_held"
        plan["closing_quote"] = quote
    if spoken_order:
        plan["spoken_order"] = dict(spoken_order)
    if named_lines:
        plan["named_lines"] = [dict(row) for row in named_lines]
    if kept_lines:
        from app.services.kept_sound_lines import kept_line_held  # noqa: PLC0415

        # A clip left out of the edit has no sentence to hold; its toggle says "not in edit".
        plan["kept_lines"] = [
            {**row, "held": kept_line_held(row, windows)}
            for row in kept_lines
            if row and row.get("media_id") in by_id
        ]
    if licensed_music is not None:
        plan["licensed_music"] = dict(licensed_music)
    elif isinstance(strategy.get("selected_music_section"), Mapping):
        plan["licensed_music"] = dict(strategy["selected_music_section"])
    elif isinstance(strategy.get("licensed_music_selection"), Mapping):
        plan["licensed_music"] = dict(strategy["licensed_music_selection"])
    return plan


# Leverkusen M2: the code of the conflict a window that cannot leave out a hidden part raises.
HIDDEN_PART_UNPLACEABLE = "hidden_part_unplaceable"
# Windows the creator or their words pinned to an exact interval: never slid out of a part.
_PINNED_WINDOW_REASONS = frozenset({"creator_answer", "closing_quote", "grounded_moment"})


def _kept_out_of_hidden(
    result: BestPartsResult,
    by_id: Mapping[str, Mapping[str, Any]],
    hidden: Mapping[str, Sequence[HiddenPart]],
) -> tuple[BestPartsResult, list[str]]:
    """``result`` with every window slid out of its clip's hidden parts (same length).

    A window pinned to the creator's interval, one with no room left, or one that would land
    on another window of its clip is not moved and is returned by media id: the plan then
    carries a conflict, so no approval card offers it."""
    parts = list(result.parts)
    unplaceable: list[str] = []
    for index, part in enumerate(parts):
        own = hidden.get(part.media_id)
        duration = float(by_id[part.media_id]["duration_s"]) if own else 0.0
        if not own or not shows_hidden(part.source_start_s, part.source_end_s, duration, own):
            continue
        moved = (
            keep_out(part.source_start_s, part.source_end_s, duration, own)
            if part.reason not in _PINNED_WINDOW_REASONS
            else None
        )
        if moved is not None and any(
            other is not part
            and other.media_id == part.media_id
            and moved[0] < other.source_end_s - 0.001
            and moved[1] > other.source_start_s + 0.001
            for other in parts
        ):
            moved = None
        if moved is None:
            unplaceable.append(part.media_id)
            continue
        parts[index] = BestPart(
            part.media_id, round(moved[0], 3), round(moved[1], 3), part.score, part.reason
        )
    return replace(result, parts=tuple(parts)), unplaceable


def _moment_or_quote_windows(
    strategy: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    creator_target_s: float | None,
    natural_owner: str,
    closing_quote: Mapping[str, Any] | None,
    moment_count: int | None,
    moment_max_s: float | None,
    natural_duration_s: float | None = None,
    moment_each_s: float | None = None,
    pinned_moments: Sequence[Mapping[str, Any]] | None = None,
) -> BestPartsResult | None:
    """Moments inside long takes, and/or a held closing line; None keeps the selector.

    Only a montage of videos qualifies: voiceover edits and photos keep one window per
    source exactly as before. Moments (footage-owned only) share the creator's length; with
    no length they are the creator's own length each ("about 2 seconds each", KRI-776), else
    ``DEFAULT_MOMENT_S`` each. A closing line is also held when a
    song section owns the length (KRI-681: music on the other clips, the line in its own
    sound); the section's length is then the target the creator did not give.
    """
    if (
        strategy.get("edit_format") != "montage"
        or natural_owner not in {"footage", "song"}
        or not sources
        or any(source["kind"] != "video" for source in sources)
    ):
        return None
    creator_each_s, creator_length_s = moment_each_s, creator_target_s
    if natural_owner == "song":
        moment_count = moment_max_s = moment_each_s = None
        if creator_target_s is None:
            creator_target_s = natural_duration_s
    if pinned_moments and len(sources) == 1:
        # KRI-723: the creator's named moments of the one take, each where it really is,
        # whoever owns the length (a licensed-music bed follows the creator's windows).
        return _pinned_moment_windows(
            sources[0], pinned_moments, creator_target_s, creator_each_s, creator_length_s
        )
    pinned: tuple[str, float, float] | None = None
    if isinstance(closing_quote, Mapping) and closing_quote.get("status") == "found":
        media_id = closing_quote.get("media_id")
        start, end = _number(closing_quote.get("start_s")), _number(closing_quote.get("end_s"))
        if (
            any(source["media_id"] == media_id for source in sources)
            and start is not None
            and end is not None
            and (creator_target_s is None or end - start < creator_target_s)
        ):
            pinned = (str(media_id), start, end)
    count = moment_count
    if count is None and moment_max_s and creator_target_s is not None:
        count = math.ceil(creator_target_s / moment_max_s - 1e-9)
    if count is not None and count > len(sources):
        moments = select_moment_windows(
            sources,
            target_duration_s=(
                creator_target_s
                if creator_target_s is not None
                else count * (moment_each_s or DEFAULT_MOMENT_S)
            ),
            moment_count=count,
            pinned_last=pinned,
        )
        if moments is not None:
            return moments
    if pinned is None:
        return None
    # One window per take, the closing take's being the creator's line.
    media_id, start, end = pinned
    others = [source for source in sources if source["media_id"] != media_id]
    if not others:
        # The line alone is the video: it grows backwards to the length asked (or 3 s).
        start = max(0.0, min(start, end - (creator_target_s or 3.0)))
        part = BestPart(media_id, round(start, 3), round(end, 3), 0.0, "closing_quote")
        return BestPartsResult(
            (part,), creator_target_s or part.duration_s, part.duration_s, part.duration_s
        )
    line = BestPart(media_id, round(start, 3), round(end, 3), 0.0, "closing_quote")
    rest = select_best_parts(
        others,
        target_duration_s=(
            round(creator_target_s - line.duration_s, 3) if creator_target_s is not None else None
        ),
        minimum_duration_s={source["media_id"]: source["minimum_duration_s"] for source in others},
    )
    if rest.conflicts or not rest.parts:
        return None
    total = round(rest.duration_s + line.duration_s, 3)
    return BestPartsResult(
        (*rest.parts, line),
        creator_target_s if creator_target_s is not None else total,
        total,
        round(rest.capacity_s + line.duration_s, 3),
    )


def _pinned_moment_windows(
    source: Mapping[str, Any],
    pins: Sequence[Mapping[str, Any]],
    target_s: float | None,
    each_s: float | None,
    creator_length_s: float | None,
) -> BestPartsResult:
    """The pinned windows, or a blocking conflict: a creator's answer is never dropped for
    ordinal windows (KRI-723)."""
    fixed = sum(
        (_number(pin.get("source_end_s")) or 0.0) - (_number(pin.get("source_start_s")) or 0.0)
        for pin in pins
        if is_fixed_moment(pin)
    )
    flexible = sum(1 for pin in pins if not is_fixed_moment(pin))
    # The creator's own length for each moment wins; else what the total leaves.
    width = each_s or DEFAULT_MOMENT_S
    if each_s is None and target_s is not None and flexible and target_s - fixed > 0:
        width = (target_s - fixed) / flexible
    width = max(width, 0.8)
    widths = [width]
    if each_s is None and creator_length_s is None:
        # No length of the creator's to keep: shorter flexible windows may still fit.
        while widths[-1] > 0.8:
            widths.append(max(0.8, widths[-1] * 0.75))
    for candidate in widths:
        placed = select_pinned_moment_windows(source, pins, width_s=candidate)
        if placed is not None:
            return placed
    duration = _number(source.get("duration_s")) or 0.0
    requested = round(fixed + flexible * widths[-1], 3)
    return BestPartsResult(
        (),
        round(target_s if target_s is not None else requested, 3),
        0.0,
        round(duration, 3),
        (
            BestPartsConflict(
                NAMED_MOMENTS_UNPLACEABLE,
                requested,
                round(duration, 3),
                "The named moments cannot all get their own window in this clip.",
            ),
        ),
    )


# KRI-723: the creator's moments cannot all be placed (``choice_questions`` asks about it).
NAMED_MOMENTS_UNPLACEABLE = "named_moments_unplaceable"


def _moments_record(
    pins: Sequence[Mapping[str, Any]], windows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]] | None:
    """``moments[]`` of a v2 envelope: each pin with the window that plays it.

    The pinned selector returns exactly one window per pin, in pin order; anything else (a
    fallback to the plain windows) records no moments, so no receipt can claim them.
    """
    if len(windows) != len(pins):
        return None
    out: list[dict[str, Any]] = []
    for index, (pin, window) in enumerate(zip(pins, windows, strict=True)):
        if window.get("reason") not in _MOMENT_WINDOW_REASONS.get(str(pin.get("origin")), ()):
            return None
        row = {**dict(pin), "media_id": window["media_id"], "window_index": index}
        if is_fixed_moment(pin):
            # The played window keeps the fixed source interval.
            row["source_start_s"] = window["source_start_s"]
            row["source_end_s"] = window["source_end_s"]
        out.append(row)
    return out


_MOMENT_WINDOW_REASONS = {
    "creator_answer": ("creator_answer",),
    "grounded": ("grounded_moment",),
    "ai_pick_by_request": ("ai_pick_by_request",),
    "unlocated": ("unlocated_moment",),
}


def _check_moments(envelope: Mapping[str, Any], windows: Sequence[Any]) -> None:
    """KRI-723: a v2 envelope's moments point at their windows, and a creator-pointed window
    is exactly the creator's interval. Anything else was changed after approval."""
    moments = envelope.get("moments")
    if not isinstance(moments, list) or not moments:
        raise ValueError("invalid length plan moments")
    seen: set[int] = set()
    for moment in moments:
        if not isinstance(moment, Mapping) or moment.get("origin") not in _MOMENT_WINDOW_REASONS:
            raise ValueError("invalid length plan moments")
        index = moment.get("window_index")
        if type(index) is not int or not 0 <= index < len(windows) or index in seen:
            raise ValueError("invalid length plan moments")
        seen.add(index)
        window = windows[index]
        if not isinstance(window, Mapping) or window.get("media_id") != moment.get("media_id"):
            raise ValueError("length plan moment window changed")
        if window.get("reason") not in _MOMENT_WINDOW_REASONS[str(moment["origin"])]:
            raise ValueError("length plan moment window changed")
        if is_fixed_moment(moment):
            for key in ("source_start_s", "source_end_s"):
                got, want = _number(window.get(key)), _number(moment.get(key))
                if got is None or want is None or abs(got - want) > 0.001:
                    raise ValueError("length plan moment window changed")


def _overlaps_other_approved_window(
    windows: list, current: Mapping[str, Any], start: float, end: float
) -> bool:
    for other in windows:
        if (
            not isinstance(other, Mapping)
            or other is current
            or other.get("media_id") != current.get("media_id")
        ):
            continue
        other_start = _number(other.get("source_start_s"))
        other_end = _number(other.get("source_end_s"))
        if (
            other_start is not None
            and other_end is not None
            and start < other_end - 0.001
            and end > other_start + 0.001
        ):
            return True
    return False


def read_length_plan(
    envelope: Mapping[str, Any] | None, clips: Sequence[Any]
) -> tuple[BestPart, ...] | None:
    """Validate an approved envelope against current media identities.

    ``None`` signals that legacy callers may select their historic windows;
    malformed approved plans are rejected rather than silently reselected.
    """

    if envelope is None:
        return None
    if not supported_length_plan_version(envelope):
        raise ValueError("unsupported length plan version")
    sources = envelope.get("sources")
    windows = envelope.get("windows")
    if not isinstance(sources, list) or not isinstance(windows, list):
        raise ValueError("invalid length plan envelope")
    if envelope.get("version") == MOMENT_LENGTH_PLAN_VERSION:
        _check_moments(envelope, windows)
    elif "moments" in envelope:
        raise ValueError("invalid length plan moments")
    if envelope.get("conflicts"):
        raise ValueError("approved length plan still needs a constraint answer")
    actual = {str(getattr(clip, "media_id", "")): clip for clip in clips}
    expected: dict[str, Mapping[str, Any]] = {}
    for source in sources:
        if not isinstance(source, Mapping) or not isinstance(source.get("media_id"), str):
            raise ValueError("invalid length plan source")
        if source["media_id"] in expected:
            raise ValueError("duplicate length plan source")
        clip = actual.get(source["media_id"])
        source_generation = source.get("storage_generation", source.get("generation"))
        clip_generation = getattr(clip, "storage_generation", getattr(clip, "generation", None))
        source_path = source.get("gcs_path")
        clip_path = getattr(clip, "proxy_path", None)
        if (
            clip is None
            or str(source_generation) != str(clip_generation)
            or (source_path is not None and source_path != clip_path)
        ):
            raise ValueError("length plan source identity changed")
        kind = source.get("kind", "video")
        if kind not in {"image", "video"} or kind != getattr(clip, "kind", "video"):
            raise ValueError("length plan source kind changed")
        expected_duration = _number(source.get("duration_s"))
        actual_duration = _number(getattr(clip, "duration_s", None))
        # A still's recorded capacity is an allocation rule, not media
        # metadata.  Its upload duration is commonly None or zero and must not
        # invalidate the approved synthetic hold.
        #
        # The plan records the analysis proxy's duration, which a frame-rate
        # encode rounds to whole frames (a 4.968 s original becomes a 5.0 s
        # proxy), while the worker measures the original.  The identity checks
        # above (path + storage generation) are what detect a replaced file;
        # this one only catches a grossly different one, with the same slack the
        # upload contract allows between a proxy and its original (KRI-595).
        if expected_duration is None or (
            kind != "image"
            and (
                actual_duration is None
                or abs(expected_duration - actual_duration) > PROXY_ORIGINAL_DURATION_TOLERANCE_S
            )
        ):
            raise ValueError("length plan source duration changed")
        expected[source["media_id"]] = source
    parts: list[BestPart] = []
    included: set[str] = set()
    # KRI-596: a take may give several moments, never the same footage twice.
    spans: dict[str, list[tuple[float, float]]] = {}
    for window in windows:
        if not isinstance(window, Mapping) or window.get("media_id") not in expected:
            raise ValueError("invalid length plan window")
        start = _number(window.get("source_start_s"))
        end = _number(window.get("source_end_s"))
        score = _number(window.get("score", 0))
        clip = actual[window["media_id"]]
        source_duration = expected[window["media_id"]]["duration_s"]
        if (
            start is None
            or end is None
            or score is None
            or end - start < 0.1 - 1e-9
            or end > float(source_duration) + 0.001
        ):
            raise ValueError("length plan window exceeds source")
        if expected[window["media_id"]].get("kind", "video") == "video":
            # A window the proxy allowed may run a frame past the real clip.
            actual_end = _number(getattr(clip, "duration_s", None))
            if actual_end is not None and end > actual_end:
                approved_duration = end - start
                end = math.floor(actual_end * 1000 + 1e-9) / 1000
                # Legacy approved envelopes used the preview's rounded end.
                # Keep their duration by moving only this bounded window back
                # into unused original footage; never overlap another cut,
                # repeat frames, change speed or relax source-identity checks.
                shifted_start = round(end - approved_duration, 6)
                # A creator-pointed window (KRI-723) is never moved: only its end is clamped.
                if window.get("reason") == "creator_answer":
                    shifted_start = -1.0
                if shifted_start >= 0 and not _overlaps_other_approved_window(
                    windows, window, shifted_start, end
                ):
                    start = shifted_start
                if end - start < 0.1 - 1e-9:
                    raise ValueError("length plan window exceeds source")
        if any(
            start < other_end - 0.001 and end > other_start + 0.001
            for other_start, other_end in spans.get(window["media_id"], [])
        ):
            raise ValueError("length plan windows overlap")
        spans.setdefault(window["media_id"], []).append((start, end))
        included.add(window["media_id"])
        parts.append(
            BestPart(
                window["media_id"],
                start,
                end,
                score,
                str(window.get("reason") or "approved_length_plan"),
            )
        )
    if envelope.get("window_eligible") is True and included != set(expected):
        raise ValueError("length plan window coverage changed")
    return tuple(parts)
