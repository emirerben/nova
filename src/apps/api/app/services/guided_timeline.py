"""Pure guided-story timeline math shared by the Save route and the Kria copilot.

Extracted (behaviour unchanged) from ``routes/generative_jobs.py`` so the chat
copilot can compute the same segment windows the server will persist, and keep
per-clip label bars attached to their segments (KRI-219 Lane B).

No I/O, no HTTP: failures raise :class:`GuidedTimelineError` carrying the same
machine code the route used to raise as an HTTP 422 detail.
"""

from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE
from app.schemas.editor_window import below_editor_minimum, snap_to_editor_minimum
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
    *,
    rendered_clock: bool = False,
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
        out.append(
            (transition, overlap if rendered_clock else round(overlap, 3))
            if overlap >= 0.1
            else ("cut", 0.0)
        )
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
    rendered_clock = current_revision.get("timing_mode") == "rendered"
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
    effective = effective_transitions(
        slot_durations, requested_transitions, rendered_clock=rendered_clock
    )
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
        if below_editor_minimum(duration):
            raise GuidedTimelineError("TIMELINE_INVALID_DURATION")
        duration = snap_to_editor_minimum(duration)
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
        # KRI-828: a creator's freeze stays on its occurrence (and a split child of it) and
        # on its frame: the saved ``source_end_s`` of a held segment IS the frozen frame. The
        # slot plays from ``in_s`` up to that frame at 1x and holds it for the rest of its
        # duration, so trimming the slot's end shortens (or lengthens) the hold and trimming
        # its start shortens the motion before it -- the frame never moves. The editor's
        # preview reads the same frame (``freeze_frame_s`` on the slot row). A slot that no
        # longer reaches the frame with room to move (the leading half of a split, a trim
        # into the hold's whole length) plays its own footage with no hold.
        inherited_hold_s = (
            float(inherited_segment.get("freeze_hold_s") or 0.0)
            if isinstance(inherited_segment, dict) and source.get("kind") == "video"
            else 0.0
        )
        # An occurrence that carried a hold plays at 1x whether or not it keeps it: its
        # source span is shorter than its slot by the hold, which is not a slow retime.
        inherited_hold = bool(inherited_hold_s)
        freeze_hold = 0.0
        if inherited_hold:
            held_frame = float(inherited_segment.get("source_end_s") or 0.0)
            motion = held_frame - float(slot.in_s)
            hold = round(round((duration - motion) * GUIDED_FPS) / GUIDED_FPS, 6)
            if not below_editor_minimum(motion) and hold >= 1 / GUIDED_FPS - 1e-6:
                freeze_hold = hold
        playback_rate_set = "playback_rate" in slot.model_fields_set
        if playback_rate_set:
            playback_rate = float(slot.playback_rate or 1.0)
        elif inherited_hold:
            playback_rate = 1.0
        elif isinstance(inherited_segment, dict):
            inherited_duration = float(inherited_segment.get("duration_s") or 0.0)
            inherited_span = float(inherited_segment.get("source_end_s") or 0.0) - float(
                inherited_segment.get("source_start_s") or 0.0
            )
            playback_rate = (
                float(inherited_segment.get("playback_rate") or 1)
                if inherited_segment.get("preserve_source_window")
                else inherited_span / inherited_duration
                if inherited_duration > 0
                else 1.0
            )
        else:
            playback_rate = 1.0
        selected_end = getattr(slot, "source_end_s", None)
        if (
            selected_end is None
            and isinstance(inherited_segment, dict)
            and inherited_segment.get("preserve_source_window")
        ):
            same_window = (
                abs(float(slot.in_s) - float(inherited_segment["source_start_s"])) < 1e-6
                and abs(duration - float(inherited_segment["duration_s"])) < 1e-6
                and abs(playback_rate - float(inherited_segment.get("playback_rate") or 1)) < 1e-6
            )
            if same_window:
                selected_end = float(inherited_segment["source_end_s"])
        preserve_window = selected_end is not None
        if preserve_window:
            if source.get("kind") != "video" or not math.isfinite(float(selected_end)):
                raise GuidedTimelineError("TIMELINE_SOURCE_BOUNDS")
            exact_duration = (float(selected_end) - float(slot.in_s)) / playback_rate
            if (
                below_editor_minimum(exact_duration)
                or abs(exact_duration - duration) > 1 / GUIDED_FPS + 1e-6
            ):
                raise GuidedTimelineError("TIMELINE_INVALID_DURATION")
            duration = (
                exact_duration
                if rendered_clock
                else round(round(exact_duration * GUIDED_FPS) / GUIDED_FPS, 6)
            )
        if source.get("kind") == "video" and source_duration is not None:
            if slot.in_s < 0 or (
                float(selected_end)
                if preserve_window
                else (held_frame if freeze_hold else slot.in_s + duration * playback_rate)
            ) > float(source_duration) + (1e-6 if preserve_window else 0.05):
                raise GuidedTimelineError("TIMELINE_SOURCE_BOUNDS")
        if order:
            cursor = max(0.0, cursor - effective[order - 1][1])
        transition, transition_duration = effective[order]
        segment = {
            "segment_id": segment_id,
            "parent_segment_id": slot.parent_segment_id,
            "media_id": source["media_id"],
            "source_start_s": float(slot.in_s),
            "source_end_s": float(selected_end)
            if preserve_window
            else held_frame
            if freeze_hold
            else float(slot.in_s) + duration * playback_rate,
            "duration_s": duration if rendered_clock else round(duration, 3),
            "transition_after": transition,
            "transition_duration_s": transition_duration,
            "look_preset": slot.look_preset or "none",
            "look_adjustments": slot.look_adjustments.model_dump()
            if slot.look_adjustments
            else None,
            "output_start_s": cursor if rendered_clock else round(cursor, 3),
        }
        if preserve_window:
            segment["preserve_source_window"] = True
            segment["playback_rate"] = playback_rate
            segment["duration_s"] = duration
        if "source_crop" in slot.model_fields_set:
            if slot.source_crop is not None:
                segment["source_crop"] = slot.source_crop
        elif isinstance(inherited_segment, dict) and inherited_segment.get("source_crop"):
            segment["source_crop"] = inherited_segment["source_crop"]
        if "tilt" in slot.model_fields_set:
            if slot.tilt is not None:
                segment["tilt"] = slot.tilt
        elif isinstance(inherited_segment, dict) and inherited_segment.get("tilt"):
            segment["tilt"] = inherited_segment["tilt"]
        if segment.get("tilt") and segment["look_preset"] != "none":
            raise GuidedTimelineError("TIMELINE_TILT_WITH_LOOK")
        if playback_rate_set and slot.playback_rate is not None:
            segment["playback_rate"] = playback_rate
        elif not playback_rate_set and isinstance(inherited_segment, dict):
            # Preserve an explicit persisted value; legacy source-span-only
            # retimes stay legacy so their normalized hash does not churn.
            if inherited_segment.get("playback_rate") is not None:
                segment["playback_rate"] = inherited_segment["playback_rate"]
        # Per-occurrence camera sound. Omission preserves the saved state
        # (a split child inherits its parent's); an explicit value replaces it.
        if "muted" in slot.model_fields_set and slot.muted is not None:
            muted = bool(slot.muted)
        else:
            muted = bool(
                isinstance(inherited_segment, dict) and inherited_segment.get("source_audio_muted")
            )
        if muted:
            segment["source_audio_muted"] = True
        if freeze_hold and playback_rate == 1.0 and not preserve_window:
            segment["freeze_hold_s"] = freeze_hold
        elif freeze_hold:
            # A speed change or a pinned source window on a held slot is a new program: the
            # hold no longer applies, so the slot plays its whole length.
            segment["source_end_s"] = float(slot.in_s) + duration * playback_rate
            if (
                source_duration is not None
                and segment["source_end_s"] > float(source_duration) + 0.05
            ):
                raise GuidedTimelineError("TIMELINE_SOURCE_BOUNDS")
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
    if rendered_clock:
        resolved_transitions = effective_transitions(
            [segment["duration_s"] for segment in segments],
            requested_transitions,
            rendered_clock=True,
        )
        cursor = 0.0
        for segment, (transition, overlap) in zip(segments, resolved_transitions, strict=True):
            segment["output_start_s"] = cursor
            segment["output_end_s"] = cursor + segment["duration_s"]
            segment["transition_after"] = transition
            segment["transition_duration_s"] = overlap
            cursor = segment["output_end_s"] - overlap
    if not rendered_clock and any(segment.get("preserve_source_window") for segment in segments):
        # Rebuilt editor slots are a continuous sequence. Snap each boundary
        # once using the same frame clock as normalization, avoiding stale
        # millisecond starts that become one-frame gaps after a speed change.
        cursor = layout_frame_segments(segments)
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
    old_segments: list[dict[str, Any]],
    new_segments: list[dict[str, Any]],
    *,
    interval_end: bool = False,
) -> Callable[[float], tuple[float, str] | None]:
    """Map an old output-clock time through source time onto the new segments.

    Returns ``(new_time_s, new_segment_id)`` or ``None`` when the anchor segment
    (and every descendant of it) is gone. Exact overlap boundaries are
    right-biased (later segments win), which callers that project an interval
    END can pass ``interval_end=True`` to use the preceding segment at a cut.
    Legacy callers may instead retain their one-frame inset policy.
    """
    window = _segment_window
    fps = GUIDED_FPS

    def old_anchor(time_s: float) -> tuple[dict[str, Any], float] | None:
        for segment in reversed(old_segments):
            output_start, output_end, source_start, source_end = window(segment)
            contains = (
                output_start < time_s <= output_end
                if interval_end
                else output_start <= time_s < output_end
            )
            if contains and output_end > output_start:
                source_time = min(
                    source_end,
                    source_start
                    + max(0.0, time_s - output_start)
                    * (source_end - source_start)
                    / (output_end - output_start),
                )
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
            (containing[0] if interval_end else containing[-1])
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
        source_span = source_end - source_start
        offset = (
            (clamped_source - source_start) * (output_end - output_start) / source_span
            if source_span > 0
            else 0
        )
        projected = min(output_end, max(output_start, output_start + offset))
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
    project_end = make_time_projector(old_segments, new_segments, interval_end=True)
    # Preserve the existing frame-inset policy for unit-speed revisions. A
    # retime must map the exact end, since one old frame is not one new frame.
    retimed = any(
        abs((window[3] - window[2]) - (window[1] - window[0])) > 1e-5
        for segment in old_segments + new_segments
        for window in [_segment_window(segment)]
    )
    segment_end = {
        str(segment.get("segment_id")): float(segment.get("output_end_s") or 0.0)
        for segment in new_segments
    }
    new_total = max(segment_end.values(), default=0.0)

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
            start_value = float(value[start_field])
            end_value = float(value.get(end_field, start_value))
            # The projector is right-biased on exact boundaries, so an interval that
            # ENDS on a cut would land in the next clip (and be tombstoned when that
            # clip is gone). Anchor the end one frame early and add the frame back,
            # exactly like ``rebase_guided_text``. Deliberate difference: this path
            # (lanes a client did NOT author) still tombstones an interval whose
            # start or end sits in a deleted clip, while ``rebase_guided_text``
            # (authored text) keeps whatever part survives.
            ranged = end_value - start_value > 1e-6
            start = project(start_value)
            end = (
                (
                    project_end(end_value)
                    if retimed
                    else project(max(start_value, end_value - 1.0 / fps))
                )
                if ranged
                else start
            )
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
            if ranged:
                end_cap = min(new_total, segment_end.get(end[1], new_total))
                new_end = max(
                    start[0], min(end_cap, round(end[0] + (0 if retimed else 1.0 / fps), 6))
                )
                if new_end - start[0] <= 0.5 / fps:
                    # An interval spanning clips that reorder projects inverted/tiny: fall
                    # back to the window of the clip it starts in (never a zero-length
                    # record, which the commit rejects), like ``rebase_guided_text``.
                    new_end = min(new_total, segment_end.get(start[1], new_total))
                    if new_end - start[0] <= 0.5 / fps:
                        new_end = min(new_total, start[0] + 0.2)
                updated[end_field] = new_end
            else:
                updated[end_field] = start[0]
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
        end_mapper = project_end if retimed else project
        end = end_mapper(float(value.get("end_frame_exclusive", value["start_frame"])) / fps)
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


def layout_frame_segments(segments: list[dict[str, Any]]) -> float:
    """Place a continuous source-window timeline on the shared 30 fps clock."""
    cursor = 0.0
    for segment in segments:
        segment["duration_s"] = round(round(segment["duration_s"] * GUIDED_FPS) / GUIDED_FPS, 6)
        segment["output_start_s"] = round(cursor, 6)
        segment["output_end_s"] = round(
            round((cursor + segment["duration_s"]) * GUIDED_FPS) / GUIDED_FPS, 6
        )
        overlap = segment["transition_duration_s"] if segment["transition_after"] != "cut" else 0
        cursor = segment["output_end_s"] - round(round(overlap * GUIDED_FPS) / GUIDED_FPS, 6)
    return cursor


def carousel_boundary_duration(
    requested_s: Any, before_duration_s: float, after_duration_s: float
) -> float:
    """The renderer's capped overlap at a Carousel splice boundary."""
    requested = 0.4 if requested_s is None else float(requested_s)
    return round(
        max(0.0, min(1.0, max(0.1, requested), before_duration_s * 0.3, after_duration_s * 0.3)), 3
    )


def carousel_ripple_duration(
    duration_s: float,
    old_boundary_overlap_s: float,
    incoming_overlap_s: float,
    outgoing_overlap_s: float,
) -> float:
    return max(0.0, duration_s + old_boundary_overlap_s - incoming_overlap_s - outgoing_overlap_s)
