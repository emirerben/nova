"""Guided-story timeline support for the Kria chat copilot (KRI-219 Lane B).

A guided (story-native) variant times its per-clip label bars on absolute output
windows, so a reorder / trim / retime / remove used to strand them. The
compiler now calls :func:`rebase_guided_text` whenever the timeline changed on a
guided-native variant: every clip-bound bar is re-windowed onto the segment it
follows (same ``segment_id`` -> split child -> same media), title / whole-video
bars are re-anchored, captions and narration are left alone, and the rebased
text is ALWAYS included in the payload so the server treats it as authored and
skips its own (right-biased, label-unaware) projection.

Pure functions over the compile state; no I/O. ``kria_editor_ops`` is imported
lazily (it imports this module lazily too).
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE
from app.config import settings
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S
from app.schemas.guided_edit_revision import normalize_guided_editor_revision
from app.services.editor_limits import EDITOR_MAX_TIMELINE_SLOTS
from app.services.guided_timeline import (
    GUIDED_FPS,
    GuidedTimelineError,
    build_guided_segments,
    make_time_projector,
    project_lane_times,
)

_FRAME_S = 1.0 / GUIDED_FPS
_EDGE_S = 0.05
_LABEL_PREFIX = "clip-label-"
_LABEL_MEDIA_PREFIX = "clip-label-media-"
_MIN_BAR_S = 0.2
# A bar that carries a segment_id (server projection stamps EVERY projected bar,
# titles included) is only "clip-bound" if it still fills that segment.
_SEGMENT_FIT_TOLERANCE_S = 0.15
# Whole-video bars are identified by id BEFORE the segment fit test: a title
# exactly one clip long must not travel with that clip on a reorder.
_ANCHORED_IDS = frozenset({"guided-title", "guided-closing-title"})

_CODE_TEXT = {
    "TIMELINE_EMPTY": "The video needs at least one clip",
    "TIMELINE_TOO_LONG": "That would make the video longer than the maximum length",
    "transitions_disabled": "Transitions are turned off for this edit",
    "TIMELINE_UNKNOWN_CLIP": "A clip on the timeline is no longer available",
    "TIMELINE_IMAGE_SPLIT_UNSUPPORTED": "Photos cannot be split",
    "TIMELINE_INVALID_PARENT": "A split clip lost its parent",
    "TIMELINE_INVALID_DURATION": "A clip would be shorter than 0.1 seconds",
    "TIMELINE_SOURCE_BOUNDS": "A clip does not have enough footage for that length or speed",
}


def _ops():
    from app.services import kria_editor_ops  # noqa: PLC0415

    return kria_editor_ops


def _op_error(message: str):
    return _ops().KriaEditorOpError(message)


def _round(value: float) -> float:
    # Segments live on the 1/30s grid at 6 decimals; ms rounding drifts off it.
    return round(float(value), 6)


# ── Slot rows <-> segments ────────────────────────────────────────────────────


def assign_slot_ids(rows: list[dict[str, Any]]) -> None:
    """Give id-less rows (split children) a deterministic id so labels can follow them."""
    taken = {str(row["slot_id"]) for row in rows if row.get("slot_id")}
    for row in rows:
        if row.get("slot_id"):
            continue
        seed = "|".join(
            str(row.get(key)) for key in ("parent_segment_id", "clip_index", "in_s", "duration_s")
        )
        base = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]  # noqa: S324
        candidate, salt = base, 0
        while candidate in taken:
            salt += 1
            candidate = f"{base}{salt}"
        taken.add(candidate)
        row["slot_id"] = candidate


def guided_segments_for_rows(
    guided: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalized segments (exact output windows) the server will persist for ``rows``."""
    ops = _ops()
    try:
        segments = build_guided_segments(
            guided,
            ops._timeline_models(rows),
            transitions_enabled=bool(settings.edit_transitions_enabled),
            max_slots=EDITOR_MAX_TIMELINE_SLOTS,
            max_total_s=float(MAX_PROPOSAL_DURATION_S),
        )
    except GuidedTimelineError as exc:
        raise _op_error(_CODE_TEXT.get(exc.code, "That timeline change is not possible")) from exc
    raw = {
        **guided,
        "revision_number": int(guided["revision_number"]) + 1,
        "segments": segments,
        "state_hash": "",
    }
    try:
        normalized = normalize_guided_editor_revision(
            raw,
            expected_approval_version=int(guided["approval_proposal_version"]),
            expected_media_digest=str(guided["approval_media_digest"]),
        )
    except ValueError as exc:
        raise _op_error("That timeline change could not be laid out") from exc
    return list(normalized["segments"])


def project_guided_draft_slots(
    job: Any, variant: dict[str, Any], timeline_slots: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], float]:
    """Draft slot rows (with media identity + output windows) and the draft total."""
    ops = _ops()
    guided = ops._guided_v2_revision(job, variant)
    prior = ops._variant_slots(variant, job)
    by_id = {str(row["slot_id"]): row for row in prior if row.get("slot_id")}
    sources = list((guided or {}).get("sources") or [])
    inherit = (
        "media_id",
        "media_kind",
        "source_duration_s",
        "layout",
        "source_crop",
        "playback_rate",
    )
    rows: list[dict[str, Any]] = []
    for raw in timeline_slots:
        raw = copy.deepcopy(raw)
        base = by_id.get(str(raw.get("slot_id")))
        if base is None:
            parent = by_id.get(str(raw.get("parent_segment_id")))
            base = {key: parent[key] for key in inherit if parent and key in parent}
        row = {**base, **raw}
        index = row.get("clip_index")
        if isinstance(index, int) and 0 <= index < len(sources):
            row["media_id"] = str(sources[index].get("media_id"))
            row.setdefault("source_duration_s", sources[index].get("duration_s"))
            if sources[index].get("kind") in {"video", "image"}:
                row.setdefault("media_kind", sources[index]["kind"])
        rows.append(row)
    total = 0.0
    if guided is not None and rows:
        try:
            segments = guided_segments_for_rows(guided, rows)
        except Exception:  # noqa: BLE001 - a draft must never crash the planner
            return rows, total
        active = [row for row in rows if not row.get("removed")]
        for order, (row, segment) in enumerate(zip(active, segments, strict=False)):
            row["output_start_s"] = segment["output_start_s"]
            row["output_end_s"] = segment["output_end_s"]
            row["order"] = order
        total = max((float(segment["output_end_s"]) for segment in segments), default=0.0)
    return rows, total


def _row_media(row: dict[str, Any], guided: dict[str, Any]) -> str | None:
    media = row.get("media_id")
    if isinstance(media, str) and media:
        return media
    sources = list(guided.get("sources") or [])
    index = row.get("clip_index")
    if isinstance(index, int) and 0 <= index < len(sources):
        return str(sources[index].get("media_id"))
    return None


def _old_segments(old_rows: list[dict[str, Any]], guided: dict[str, Any]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for row in old_rows:
        duration = float(row.get("duration_s") or 0.0)
        rate = float(row.get("playback_rate") or 1.0)
        source_start = float(row.get("in_s") or 0.0)
        start = float(row.get("output_start_s") or 0.0)
        end = row.get("output_end_s")
        segments.append(
            {
                "segment_id": row.get("slot_id"),
                "parent_segment_id": row.get("parent_segment_id"),
                "media_id": _row_media(row, guided),
                "output_start_s": start,
                "output_end_s": float(end) if end is not None else start + duration,
                "duration_s": duration,
                "source_start_s": source_start,
                "source_end_s": source_start + duration * rate,
            }
        )
    return segments


# ── Text rebase ───────────────────────────────────────────────────────────────


def _is_untouched(bar: dict[str, Any]) -> bool:
    params = bar.get("source_params")
    params = params if isinstance(params, dict) else {}
    kind = params.get("narration_label_kind")
    return bool(
        params.get("source") == CAPTION_CUE_SOURCE
        or (kind is not None and kind != "participant")
        or str(bar.get("id") or "").startswith("lyric_")
    )


def _bar_media(bar: dict[str, Any], links: dict[str, dict[str, Any]]) -> str | None:
    bar_id = str(bar.get("id") or "")
    if bar_id.startswith(_LABEL_MEDIA_PREFIX):
        return bar_id[len(_LABEL_MEDIA_PREFIX) :] or None
    link = links.get(bar_id)
    return str(link["clip_id"]) if link else None


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _identify_old_segment(
    bar: dict[str, Any], media_id: str | None, old_segments: list[dict[str, Any]]
) -> dict[str, Any] | None:
    seg_id = bar.get("segment_id")
    if seg_id:
        for segment in old_segments:
            if str(segment.get("segment_id")) == str(seg_id):
                return segment
    if media_id is None:
        return None
    candidates = [s for s in old_segments if str(s.get("media_id")) == media_id]
    if not candidates:
        return None
    start, end = float(bar.get("start_s") or 0.0), float(bar.get("end_s") or 0.0)
    return max(
        candidates,
        key=lambda s: _overlap(start, end, float(s["output_start_s"]), float(s["output_end_s"])),
    )


def _descendants(old_id: str, new_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    family = {old_id}
    grew = True
    while grew:
        grew = False
        for segment in new_segments:
            if str(segment.get("parent_segment_id") or "") in family and (
                str(segment["segment_id"]) not in family
            ):
                family.add(str(segment["segment_id"]))
                grew = True
    return [s for s in new_segments if str(s["segment_id"]) in family]


def _follow_window(
    old_segment: dict[str, Any] | None,
    media_id: str | None,
    new_segments: list[dict[str, Any]],
) -> tuple[float, float, str] | None:
    """Output window of the new segment(s) a clip-bound bar should now span."""
    targets: list[dict[str, Any]] = []
    old_id = str(old_segment.get("segment_id") or "") if old_segment else ""
    if old_id:
        targets = _descendants(old_id, new_segments)
    elif media_id is not None:
        # No identifiable old occurrence: fall back to the clip itself.
        targets = [s for s in new_segments if str(s.get("media_id")) == media_id][:1]
    if not targets:
        return None
    indexes = sorted(new_segments.index(s) for s in targets)
    contiguous = indexes == list(range(indexes[0], indexes[0] + len(indexes)))
    picked = [new_segments[i] for i in indexes] if contiguous else [new_segments[indexes[0]]]
    return (
        float(picked[0]["output_start_s"]),
        float(picked[-1]["output_end_s"]),
        str(picked[0]["segment_id"]),
    )


def _preserve_label_offsets(
    start: float,
    end: float,
    old_segment: dict[str, Any],
    window: tuple[float, float],
) -> tuple[float, float]:
    """Keep a clip label's manual inset from its segment edges through a retime.

    Start inset and end gap (distance from the old window edges) are scaled by
    new_len/old_len and re-applied inside the new window, clamped so the bar
    stays inside it with at least ``_MIN_BAR_S``. A bar flush with both old
    edges (the default) snaps to the full new window exactly as before.
    """
    new_start, new_end = window
    old_start = float(old_segment["output_start_s"])
    old_end = float(old_segment["output_end_s"])
    inset = max(0.0, start - old_start)
    gap = max(0.0, old_end - end)
    if inset <= _EDGE_S / 2 and gap <= _EDGE_S / 2:
        return new_start, new_end
    new_len = new_end - new_start
    if new_len <= _MIN_BAR_S:
        return new_start, new_end
    old_len = old_end - old_start
    ratio = new_len / old_len if old_len > 0 else 1.0
    inset = inset * ratio if inset > _EDGE_S / 2 else 0.0
    gap = gap * ratio if gap > _EDGE_S / 2 else 0.0
    out_start = min(new_start + inset, new_end - _MIN_BAR_S)
    out_end = max(min(new_end - gap, new_end), out_start + _MIN_BAR_S)
    return out_start, out_end


def rebase_guided_text(state: Any, guided: dict[str, Any]) -> None:
    """Re-window ``state.text`` (and any same-bundle lanes) onto the new timeline."""
    ops = _ops()
    assign_slot_ids(state.slots)
    old_rows = [row for row in state.initial_slots if not row.get("removed")]
    old_segments = _old_segments(old_rows, guided)
    old_total = max((float(s["output_end_s"]) for s in old_segments), default=0.0)
    new_segments = guided_segments_for_rows(guided, state.slots)
    new_total = max((float(s["output_end_s"]) for s in new_segments), default=0.0)
    links = ops._clip_label_links(state.job, state.variant)
    project = make_time_projector(old_segments, new_segments)
    old_index = {str(s.get("segment_id")): i for i, s in enumerate(old_segments)}

    notes: list[str] = []
    rebased: list[dict[str, Any]] = []

    def drop(bar: dict[str, Any], old_segment: dict[str, Any] | None, label: bool) -> None:
        if label and old_segment is not None:
            number = old_index.get(str(old_segment.get("segment_id")), 0) + 1
            notes.append(f"Removed label for clip {number}")
        else:
            notes.append(f'Removed text "{str(bar.get("text") or "")[:24]}"')

    for bar in state.text:
        if _is_untouched(bar) or "start_s" not in bar or "end_s" not in bar:
            rebased.append(bar)
            continue
        start, end = float(bar["start_s"]), float(bar["end_s"])
        media_id = _bar_media(bar, links)
        is_label = str(bar.get("id") or "").startswith(_LABEL_PREFIX)
        old_segment = None
        clip_bound = False
        if is_label:
            old_segment = _identify_old_segment(bar, media_id, old_segments)
            clip_bound = True
        elif bar.get("segment_id") and str(bar.get("id") or "") not in _ANCHORED_IDS:
            candidate = _identify_old_segment(bar, None, old_segments)
            if (
                candidate is not None
                and abs(start - float(candidate["output_start_s"])) <= _SEGMENT_FIT_TOLERANCE_S
                and abs(end - float(candidate["output_end_s"])) <= _SEGMENT_FIT_TOLERANCE_S
            ):
                old_segment, clip_bound = candidate, True
        if clip_bound:
            window = _follow_window(old_segment, media_id, new_segments)
            if window is None:
                drop(bar, old_segment, is_label)
                continue
            updated = dict(bar)
            new_start, new_end = window[0], window[1]
            if is_label and old_segment is not None:
                new_start, new_end = _preserve_label_offsets(
                    start, end, old_segment, (window[0], window[1])
                )
            updated["start_s"], updated["end_s"] = _round(new_start), _round(new_end)
            updated["segment_id"] = window[2]
            rebased.append(updated)
            continue
        opening = start <= _EDGE_S
        closing = old_total > 0 and end >= old_total - _EDGE_S
        if opening or closing:
            updated = dict(bar)
            if opening and closing:
                new_start, new_end = 0.0, new_total
            elif opening:
                new_start, new_end = start, min(end, new_total)
            else:
                length = end - start
                new_end = new_total
                new_start = max(0.0, new_total - length)
            if new_end - new_start < _MIN_BAR_S:
                new_end = min(new_total, new_start + _MIN_BAR_S)
                new_start = max(0.0, new_end - _MIN_BAR_S)
            updated["start_s"], updated["end_s"] = _round(new_start), _round(new_end)
            rebased.append(updated)
            continue
        start_point = project(start)
        end_point = project(max(start, end - _FRAME_S))
        if start_point is None or end_point is None:
            drop(bar, None, False)
            continue
        # The projector is right-biased on exact overlaps; anchoring the END one
        # frame early and shifting back keeps a bar that ended on a cut inside
        # its own clip.
        new_start = start_point[0]
        new_end = min(new_total, end_point[0] + _FRAME_S)
        if new_end - new_start <= _FRAME_S / 2:
            # A bar spanning clips that reorder projects inverted/tiny: fall back
            # to the window of the segment it starts in, never lose the text.
            start_segment = next(
                (s for s in new_segments if str(s["segment_id"]) == str(start_point[1])), None
            )
            if start_segment is not None:
                new_end = min(new_total, float(start_segment["output_end_s"]))
            if new_end - new_start <= _FRAME_S / 2:
                new_end = min(new_total, new_start + _MIN_BAR_S)
                new_start = max(0.0, new_end - _MIN_BAR_S)
        if end - start >= _MIN_BAR_S and new_end - new_start < _MIN_BAR_S:
            new_end = min(new_total, new_start + _MIN_BAR_S)
            new_start = max(0.0, new_end - _MIN_BAR_S)
        updated = dict(bar)
        updated["start_s"], updated["end_s"] = _round(new_start), _round(new_end)
        updated["segment_id"] = start_point[1]
        rebased.append(updated)

    state.text = rebased
    state.changed.add("text")

    # Same-bundle lanes were authored on the OLD clock: project them with the
    # server's own function. Lanes the bundle did not touch stay with the
    # server's projection.
    lanes: dict[str, Any] = {"tombstones": []}
    if "sound_effects" in state.changed:
        lanes["sound_effects"] = state.sound_effects
    if state.visual_blocks is not None:
        lanes["visual_blocks"] = state.visual_blocks
    if len(lanes) > 1:
        baseline = copy.deepcopy(lanes)
        try:
            project_lane_times(
                lanes,
                old_segments=old_segments,
                new_segments=new_segments,
                baseline_lanes=baseline,
                authored_lanes=set(),
            )
        except GuidedTimelineError as exc:
            raise _op_error("Too many effects would be removed by that timeline change") from exc
        for lane in ("sound_effects", "visual_blocks"):
            if lane in lanes:
                before = len(baseline[lane])
                after = len(lanes[lane])
                if after < before:
                    notes.append(
                        f"Removed {before - after} {lane.replace('_', ' ')} on removed clips"
                    )
                if lane == "sound_effects":
                    state.sound_effects = lanes[lane]
                else:
                    state.visual_blocks = lanes[lane]

    # Surface removals first: the receipt keeps only a few change lines.
    for note in reversed(list(dict.fromkeys(notes))):
        state.changes.insert(0, note)
