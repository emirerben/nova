"""Pure guided-story timeline math shared by the Save route and the Kria copilot.

Extracted (behaviour unchanged) from ``routes/generative_jobs.py`` so the chat
copilot can compute the same segment windows the server will persist, and keep
per-clip label bars attached to their segments (KRI-219 Lane B).

No I/O, no HTTP: failures raise :class:`GuidedTimelineError` carrying the same
machine code the route used to raise as an HTTP 422 detail.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE
from app.schemas.guided_edit_revision import MAX_GUIDED_EDITOR_TOMBSTONES

GUIDED_FPS = 30.0


class GuidedTimelineError(ValueError):
    """A timeline could not be turned into guided segments; ``code`` is the wire code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def effective_transitions(
    durations: Sequence[float],
    transitions: Sequence[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Per-boundary (transition, overlap_s) after the 0.3 cap / 30%-of-neighbour rule.

    ``transitions[i]`` is (transition_after, requested_duration_s) of active slot i.
    The final slot never carries a transition.
    """
    out: list[tuple[str, float]] = []
    for index, (transition, requested) in enumerate(transitions):
        if index == len(transitions) - 1:
            out.append(("cut", 0.0))
            continue
        requested = float(requested or 0.0) if transition != "cut" else 0.0
        overlap = min(requested, 0.3, durations[index] * 0.3, durations[index + 1] * 0.3)
        out.append((transition, round(overlap, 3)) if overlap >= 0.1 else ("cut", 0.0))
    return out


def build_guided_segments(
    current_revision: dict[str, Any],
    slots: Sequence[Any],
    *,
    transitions_enabled: bool,
    max_slots: int,
    max_total_s: float,
) -> list[dict[str, Any]]:
    """Turn editor ``TimelineSlotEdit`` rows into guided-revision segments.

    ``slots`` are ``TimelineSlotEdit`` models (duck-typed). Raises
    :class:`GuidedTimelineError` with the same codes the route surfaced.
    """
    sources = list(current_revision.get("sources") or [])
    if not slots:
        raise GuidedTimelineError("TIMELINE_EMPTY")
    active = [slot for slot in slots if not slot.removed]
    if not active:
        raise GuidedTimelineError("TIMELINE_EMPTY")
    if len(active) > max_slots:
        raise GuidedTimelineError("TIMELINE_TOO_LONG")
    segments: list[dict[str, Any]] = []
    cursor = 0.0
    source_by_index = {index: source for index, source in enumerate(sources)}
    slot_durations = [float(slot.duration_s or 0.0) for slot in active]
    requested_transitions: list[tuple[str, float]] = []
    for slot in active:
        if not transitions_enabled and slot.transition_after != "cut":
            raise GuidedTimelineError("transitions_disabled")
        transition = slot.transition_after if transitions_enabled else "cut"
        requested_transitions.append(
            (transition, float(slot.transition_duration_s or 0.0) if transition != "cut" else 0.0)
        )
    effective = effective_transitions(slot_durations, requested_transitions)
    current_segment_by_id = {
        str(segment.get("segment_id")): segment
        for segment in current_revision.get("segments") or []
        if isinstance(segment, dict) and segment.get("segment_id")
    }
    active_slot_by_id = {str(slot.slot_id): slot for slot in active if slot.slot_id}
    for order, slot in enumerate(active):
        source = source_by_index.get(slot.clip_index)
        if source is None:
            raise GuidedTimelineError("TIMELINE_UNKNOWN_CLIP")
        if slot.parent_segment_id:
            if source.get("kind") == "image":
                raise GuidedTimelineError("TIMELINE_IMAGE_SPLIT_UNSUPPORTED")
            parent_id = str(slot.parent_segment_id)
            persisted_parent = current_segment_by_id.get(parent_id)
            draft_parent = active_slot_by_id.get(parent_id)
            parent_matches = (
                persisted_parent is not None
                and str(persisted_parent.get("media_id")) == str(source.get("media_id"))
            ) or (
                draft_parent is not None
                and draft_parent is not slot
                and draft_parent.clip_index == slot.clip_index
            )
            if not parent_matches:
                raise GuidedTimelineError("TIMELINE_INVALID_PARENT")
        duration = float(slot.duration_s or 0.0)
        if duration < 0.1:
            raise GuidedTimelineError("TIMELINE_INVALID_DURATION")
        source_duration = source.get("duration_s")
        segment_id = slot.slot_id or uuid.uuid4().hex
        inherited_segment = current_segment_by_id.get(segment_id)
        if inherited_segment is None and slot.parent_segment_id:
            inherited_segment = current_segment_by_id.get(str(slot.parent_segment_id))

        # Omitted source controls mean "leave this occurrence alone".  Older
        # guided revisions encoded a retime only in source_end_s, so recover
        # that effective rate instead of silently reverting it to 1x on an
        # otherwise conventional Save.  An explicit null is the clear/reset
        # operation and deliberately returns to 1x.
        playback_rate_set = "playback_rate" in slot.model_fields_set
        if playback_rate_set:
            playback_rate = float(slot.playback_rate or 1.0)
        elif isinstance(inherited_segment, dict):
            inherited_duration = float(inherited_segment.get("duration_s") or 0.0)
            inherited_span = float(inherited_segment.get("source_end_s") or 0.0) - float(
                inherited_segment.get("source_start_s") or 0.0
            )
            playback_rate = inherited_span / inherited_duration if inherited_duration > 0 else 1.0
        else:
            playback_rate = 1.0
        if source.get("kind") == "video" and source_duration is not None:
            if (
                slot.in_s < 0
                or slot.in_s + duration * playback_rate > float(source_duration) + 0.05
            ):
                raise GuidedTimelineError("TIMELINE_SOURCE_BOUNDS")
        if order:
            cursor = max(0.0, cursor - effective[order - 1][1])
        transition, transition_duration = effective[order]
        segment = {
            "segment_id": segment_id,
            "parent_segment_id": slot.parent_segment_id,
            "media_id": source["media_id"],
            "source_start_s": float(slot.in_s),
            "source_end_s": float(slot.in_s) + duration * playback_rate,
            "duration_s": round(duration, 3),
            "transition_after": transition,
            "transition_duration_s": transition_duration,
            "look_preset": slot.look_preset or "none",
            "look_adjustments": slot.look_adjustments.model_dump()
            if slot.look_adjustments
            else None,
            "output_start_s": round(cursor, 3),
        }
        if "source_crop" in slot.model_fields_set:
            if slot.source_crop is not None:
                segment["source_crop"] = slot.source_crop
        elif isinstance(inherited_segment, dict) and inherited_segment.get("source_crop"):
            segment["source_crop"] = inherited_segment["source_crop"]
        if playback_rate_set and slot.playback_rate is not None:
            segment["playback_rate"] = playback_rate
        elif not playback_rate_set and isinstance(inherited_segment, dict):
            # Preserve an explicit persisted value; legacy source-span-only
            # retimes stay legacy so their normalized hash does not churn.
            if inherited_segment.get("playback_rate") is not None:
                segment["playback_rate"] = inherited_segment["playback_rate"]
        # New clients send the occurrence's canonical fit. Older clients omit
        # it; preserve a layout already stored on this segment (including its
        # parent when a split child is being written) and otherwise retain the
        # compiler's legacy first-by-media fallback.
        if slot.layout is not None:
            segment["layout"] = slot.layout
        else:
            inherited = current_segment_by_id.get(segment_id)
            if inherited is None and slot.parent_segment_id:
                inherited = current_segment_by_id.get(str(slot.parent_segment_id))
            inherited_layout = inherited.get("layout") if isinstance(inherited, dict) else None
            if isinstance(inherited_layout, str) and inherited_layout in {
                "fullscreen",
                "supporting_card",
            }:
                segment["layout"] = inherited_layout
        segments.append(segment)
        cursor += duration
    if cursor > max_total_s + 1e-6:
        raise GuidedTimelineError("TIMELINE_TOO_LONG")
    return segments


def _segment_window(segment: dict[str, Any]) -> tuple[float, float, float, float]:
    output_start = float(segment.get("output_start_s") or 0.0)
    duration = float(segment.get("duration_s") or 0.0)
    output_end = float(segment.get("output_end_s") or output_start + duration)
    source_start = float(segment.get("source_start_s") or 0.0)
    source_end = float(segment.get("source_end_s") or source_start + duration)
    return output_start, output_end, source_start, source_end


def make_time_projector(
    old_segments: list[dict[str, Any]], new_segments: list[dict[str, Any]]
) -> Callable[[float], tuple[float, str] | None]:
    """Map an old output-clock time through source time onto the new segments.

    Returns ``(new_time_s, new_segment_id)`` or ``None`` when the anchor segment
    (and every descendant of it) is gone. Exact overlap boundaries are
    right-biased (later segments win), which callers that project an interval
    END must compensate for by anchoring one frame earlier.
    """
    window = _segment_window
    fps = GUIDED_FPS

    def old_anchor(time_s: float) -> tuple[dict[str, Any], float] | None:
        for segment in reversed(old_segments):
            output_start, output_end, source_start, source_end = window(segment)
            if output_start <= time_s < output_end:
                source_time = min(source_end, source_start + max(0.0, time_s - output_start))
                return segment, source_time
        if old_segments and abs(time_s - window(old_segments[-1])[1]) <= 1e-6:
            segment = old_segments[-1]
            return segment, window(segment)[3]
        return None

    def project(time_s: float) -> tuple[float, str] | None:
        anchored = old_anchor(time_s)
        if anchored is None:
            return None
        old_segment, source_time = anchored
        old_segment_id = str(old_segment.get("segment_id") or "")
        if old_segment_id:
            candidates = [
                segment
                for segment in new_segments
                if str(segment.get("segment_id") or "") == old_segment_id
                or str(segment.get("parent_segment_id") or "") == old_segment_id
            ]
        else:
            candidates = [
                segment
                for segment in new_segments
                if str(segment.get("media_id")) == str(old_segment.get("media_id"))
            ]
        if not candidates:
            return None
        containing = [
            segment
            for segment in candidates
            if window(segment)[2] - 1e-6 <= source_time <= window(segment)[3] + 1e-6
        ]
        segment = (
            containing[-1]
            if containing
            else min(
                candidates,
                key=lambda candidate: min(
                    abs(source_time - window(candidate)[2]),
                    abs(source_time - window(candidate)[3]),
                ),
            )
        )
        output_start, output_end, source_start, source_end = window(segment)
        clamped_source = min(source_end, max(source_start, source_time))
        projected = min(output_end, max(output_start, output_start + clamped_source - source_start))
        return round(round(projected * fps) / fps, 6), str(segment["segment_id"])

    return project


def project_lane_times(
    raw: dict[str, Any],
    *,
    old_segments: list[dict[str, Any]],
    new_segments: list[dict[str, Any]],
    baseline_lanes: dict[str, Any] | None = None,
    authored_lanes: set[str] | None = None,
) -> None:
    """Project baseline-clock lane records through one structural edit.

    Lanes omitted from the Save remain in the baseline coordinate system, so
    this mapper resolves their endpoints through media/source time. Explicitly
    submitted full-replacement lanes are already authored against the staged
    output clock and are left untouched. This keeps trims, splits, repeated
    sources, deletion, and reorder deterministic without double-shifting a
    simultaneous lane retime. Exact overlap boundaries are right-biased by
    iterating later segments first.

    Raises :class:`GuidedTimelineError` ``GUIDED_TOMBSTONE_LIMIT``.
    """

    fps = GUIDED_FPS
    old_raw = copy.deepcopy(baseline_lanes if baseline_lanes is not None else raw)
    project = make_time_projector(old_segments, new_segments)

    tombstones = list(raw.get("tombstones") or [])
    lane_time_fields = {
        "text_elements": ("start_s", "end_s"),
        "media_overlays": ("start_s", "end_s"),
        "visual_blocks": ("start_s", "end_s"),
    }
    for lane, (start_field, end_field) in lane_time_fields.items():
        if lane in (authored_lanes or set()):
            continue
        current_lane_ids = {
            str(value.get("id"))
            for value in old_raw.get(lane) or []
            if isinstance(value, dict) and value.get("id")
        }
        projected_values: list[dict[str, Any]] = []
        for value in raw.get(lane) or []:
            if not isinstance(value, dict) or start_field not in value:
                projected_values.append(value)
                continue
            # Captions and narration annotations are authored on the narration
            # clock. Only PLAYER placeholders are tied to their source moment;
            # score/topic labels must keep their absolute narration timestamps
            # when the visual sequence is reordered or trimmed.
            source_params = value.get("source_params") or {}
            narration_label_kind = (
                source_params.get("narration_label_kind")
                if isinstance(source_params, dict)
                else None
            )
            if (
                lane == "text_elements"
                and isinstance(source_params, dict)
                and (
                    source_params.get("source") == CAPTION_CUE_SOURCE
                    or (narration_label_kind is not None and narration_label_kind != "participant")
                )
            ):
                projected_values.append(value)
                continue
            # New records are already authored in the submitted revision's
            # output clock.  Projecting them through the old timeline would
            # either shift them twice or drop them when they sit in a newly
            # extended tail.  Existing records alone need source-time mapping.
            if str(value.get("id") or "") not in current_lane_ids:
                projected_values.append(value)
                continue
            start = project(float(value[start_field]))
            end = project(float(value.get(end_field, value[start_field])))
            if start is None or end is None:
                tombstones.append(
                    {
                        "lane": lane,
                        "record_id": str(value.get("id") or ""),
                        "segment_id": value.get("segment_id"),
                        "reason": "anchored_interval_removed",
                        "record": value,
                    }
                )
                continue
            updated = dict(value)
            updated[start_field] = start[0]
            updated[end_field] = max(start[0], end[0])
            updated["segment_id"] = start[1]
            projected_values.append(updated)
        raw[lane] = projected_values

    # Sound effects are point placements, not intervals.  Project only their
    # firing point and never invent an `end_s` field (the SFX schema has no
    # such field).  A point whose anchor was removed is tombstoned.
    current_sfx_ids = {
        str(value.get("id"))
        for value in old_raw.get("sound_effects") or []
        if isinstance(value, dict) and value.get("id")
    }
    if "sound_effects" in (authored_lanes or set()):
        projected_sfx = list(raw.get("sound_effects") or [])
        raw["sound_effects"] = projected_sfx
    else:
        projected_sfx = []
    if "sound_effects" not in (authored_lanes or set()):
        for value in raw.get("sound_effects") or []:
            if not isinstance(value, dict) or "at_s" not in value:
                projected_sfx.append(value)
                continue
            if str(value.get("id") or "") not in current_sfx_ids:
                projected_sfx.append(value)
                continue
            point = project(float(value["at_s"]))
            if point is None:
                tombstones.append(
                    {
                        "lane": "sound_effects",
                        "record_id": str(value.get("id") or ""),
                        "segment_id": value.get("segment_id"),
                        "reason": "anchored_interval_removed",
                        "record": value,
                    }
                )
                continue
            updated = dict(value)
            updated["at_s"] = point[0]
            updated.pop("end_s", None)
            updated["segment_id"] = point[1]
            projected_sfx.append(updated)
        raw["sound_effects"] = projected_sfx

    projected_motion: list[dict[str, Any]] = []
    current_motion_ids = {
        str(value.get("id"))
        for value in old_raw.get("motion_scenes") or []
        if isinstance(value, dict) and value.get("id")
    }
    for value in raw.get("motion_scenes") or []:
        if "motion_scenes" in (authored_lanes or set()):
            projected_motion.append(value)
            continue
        if not isinstance(value, dict) or "start_frame" not in value:
            projected_motion.append(value)
            continue
        if str(value.get("id") or "") not in current_motion_ids:
            projected_motion.append(value)
            continue
        start = project(float(value["start_frame"]) / fps)
        end = project(float(value.get("end_frame_exclusive", value["start_frame"])) / fps)
        if start is None or end is None:
            tombstones.append(
                {
                    "lane": "motion_scenes",
                    "record_id": str(value.get("id") or ""),
                    "segment_id": value.get("segment_id"),
                    "reason": "anchored_interval_removed",
                    "record": value,
                }
            )
            continue
        updated = dict(value)
        updated["start_frame"] = round(start[0] * fps)
        updated["end_frame_exclusive"] = max(updated["start_frame"] + 1, round(end[0] * fps))
        updated["segment_id"] = start[1]
        projected_motion.append(updated)
    raw["motion_scenes"] = projected_motion
    if len(tombstones) > MAX_GUIDED_EDITOR_TOMBSTONES:
        raise GuidedTimelineError("GUIDED_TOMBSTONE_LIMIT")
    raw["tombstones"] = tombstones
