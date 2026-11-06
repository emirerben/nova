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
import math
from typing import Any, Literal

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

TextTimingPolicy = Literal["follow", "preserve_absolute", "preserve_all"]

_FRAME_S = 1.0 / GUIDED_FPS
_EDGE_S = 0.05
_LABEL_PREFIX = "clip-label-"
_LABEL_MEDIA_PREFIX = "clip-label-media-"
# `montage-text-<cut_id>` is per-shot copy bound to one clip's window, exactly like a
# `clip-label-*` bar (guided_story._GUIDED_CLIP_TEXT_PREFIXES). Keep the two in step.
_MONTAGE_TEXT_PREFIX = "montage-text-"
_CLIP_TEXT_PREFIXES = (_LABEL_PREFIX, _MONTAGE_TEXT_PREFIX)
_MIN_BAR_S = 0.2
# A bar that carries a segment_id (server projection stamps EVERY projected bar,
# titles included) is only "clip-bound" if it still fills that segment.
_SEGMENT_FIT_TOLERANCE_S = 0.15
# Whole-video bars are identified by id BEFORE the segment fit test: a title
# exactly one clip long must not travel with that clip on a reorder.
_ANCHORED_IDS = frozenset({"guided-title", "guided-closing-title"})
# KRI-523: whole-video corner text (`guided-pinned-<i>`) is anchored to the timeline,
# never to a clip. KRI-525: a RANGED pin is not: one that fills its clip follows that clip
# (like a label), and a seconds range is re-windowed by the generic time rules.
_ANCHORED_PREFIX = "guided-pinned-"


def _is_edge_titled(bar_id: str) -> bool:
    """Titles and pins: the ids whose edge-touching windows are re-anchored, not projected."""
    return bar_id in _ANCHORED_IDS or bar_id.startswith(_ANCHORED_PREFIX)


def _is_anchored(bar: dict[str, Any], old_total: float) -> bool:
    bar_id = str(bar.get("id") or "")
    if bar_id in _ANCHORED_IDS:
        return True
    if not bar_id.startswith(_ANCHORED_PREFIX):
        return False
    return (
        float(bar.get("start_s") or 0.0) <= _EDGE_S
        and old_total > 0
        and float(bar.get("end_s") or 0.0) >= old_total - _EDGE_S
    )


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
        "tilt",
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
                "source_end_s": float(row["source_end_s"])
                if row.get("source_end_s") is not None
                else source_start + duration * rate,
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


def _bar_media(
    bar: dict[str, Any],
    links: dict[str, dict[str, Any]],
    cut_media: dict[str, str] | None = None,
) -> str | None:
    bar_id = str(bar.get("id") or "")
    if bar_id.startswith(_LABEL_MEDIA_PREFIX):
        return bar_id[len(_LABEL_MEDIA_PREFIX) :] or None
    link = links.get(bar_id)
    if link:
        return str(link["clip_id"])
    if bar_id.startswith(_MONTAGE_TEXT_PREFIX) and cut_media:
        return cut_media.get(bar_id[len(_MONTAGE_TEXT_PREFIX) :])
    return None


def _cut_media_by_id(job: Any) -> dict[str, str]:
    """cut_id -> media_id from the approved proposal (links ``montage-text-<cut_id>`` bars)."""
    assembly = getattr(job, "assembly_plan", None)
    guided = assembly.get("guided_edit") if isinstance(assembly, dict) else None
    proposal = guided.get("approved_proposal") if isinstance(guided, dict) else None
    cuts = proposal.get("fast_cuts") if isinstance(proposal, dict) else None
    return {
        str(cut["cut_id"]): str(cut["media_id"])
        for cut in cuts or []
        if isinstance(cut, dict) and cut.get("cut_id") and cut.get("media_id")
    }


def _project_span(
    project: Any,
    old_segments: list[dict[str, Any]],
    start: float,
    end: float,
    *,
    project_end: Any = None,
) -> tuple[tuple[float, str], tuple[float, str]] | None:
    """Project a text range ``[start, end)`` through source time onto the new clips.

    The END is anchored one frame early (the projector is right-biased on exact
    boundaries); the caller adds the frame back. When either edge sits in a clip
    that no longer exists the range still survives as long as ANY clip under it does:
    it is re-anchored to the earliest and latest surviving pieces. ``None`` only when
    every clip under the range is gone (or the range overlaps no clip at all).
    """
    first = project(start)
    last = project_end(end) if project_end is not None else project(max(start, end - _FRAME_S))
    if first is not None and last is not None:
        return first, last
    pieces: list[tuple[tuple[float, str], tuple[float, str]]] = []
    for segment in old_segments:
        lo = max(start, float(segment["output_start_s"]))
        hi = min(end, float(segment["output_end_s"]))
        if hi - lo <= 1e-6:
            continue
        head = project(lo)
        tail = project_end(hi) if project_end is not None else project(max(lo, hi - _FRAME_S))
        if head is not None and tail is not None:
            pieces.append((head, tail))
    if not pieces:
        return None
    return min((p[0] for p in pieces), key=lambda x: x[0]), max(
        (p[1] for p in pieces), key=lambda x: x[0]
    )


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


def rebase_guided_text(
    state: Any,
    guided: dict[str, Any] | None,
    *,
    old_segments_override: list[dict[str, Any]] | None = None,
    new_segments_override: list[dict[str, Any]] | None = None,
    text_timing: TextTimingPolicy = "follow",
) -> None:
    """Re-window ``state.text`` (and any same-bundle lanes) onto the new timeline.

    Bars whose timing was explicitly authored in this bundle already use the
    new output clock.  Preserve those windows and only validate their bounds;
    all other authored text continues to follow the old-clock projection.
    """
    ops = _ops()
    assign_slot_ids(state.slots)
    if (old_segments_override is None) != (new_segments_override is None):
        raise ValueError("timeline rebasing needs both source-window segment sets")
    old_rows = [row for row in state.initial_slots if not row.get("removed")]
    old_segments = (
        old_segments_override
        if old_segments_override is not None
        else _old_segments(old_rows, guided or {})
    )
    old_total = max((float(s["output_end_s"]) for s in old_segments), default=0.0)
    new_segments = (
        new_segments_override
        if new_segments_override is not None
        else guided_segments_for_rows(guided or {}, state.slots)
    )
    new_total = max((float(s["output_end_s"]) for s in new_segments), default=0.0)
    explicit_text_ids = getattr(state, "explicit_text_ids", set()) or set()
    if text_timing == "preserve_all":
        protected = [bar for bar in state.text if not bar.get("removed") and "end_s" in bar]
        overflowing = [bar for bar in protected if float(bar["end_s"]) > new_total + 1e-6]
        if overflowing:
            raise ops.TextTimingConflict(
                new_total,
                max(float(bar["end_s"]) for bar in overflowing),
                tuple(str(bar.get("id") or "") for bar in overflowing),
            )
        explicit_text_ids = explicit_text_ids | {str(bar.get("id") or "") for bar in protected}
    links = ops._clip_label_links(state.job, state.variant)
    cut_media = _cut_media_by_id(state.job)
    project = make_time_projector(old_segments, new_segments)
    retimed = any(
        abs(
            (float(s["source_end_s"]) - float(s["source_start_s"]))
            - (float(s["output_end_s"]) - float(s["output_start_s"]))
        )
        > 1e-5
        for s in old_segments + new_segments
    )
    # Exact source-clock endpoints are required when a frame changes length.
    # Keep the historical inset for unchanged unit-speed timelines.
    project_end = (
        make_time_projector(old_segments, new_segments, interval_end=True) if retimed else None
    )
    old_index = {str(s.get("segment_id")): i for i, s in enumerate(old_segments)}

    notes: list[str] = []
    rebased: list[dict[str, Any]] = []
    overflowing_text: list[dict[str, Any]] = []

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
        if str(bar.get("id") or "") in explicit_text_ids:
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0.0
                or end <= start
                or end > new_total + 1e-6
            ):
                raise _op_error("That text timing does not fit on the video")
            rebased.append(bar)
            continue
        media_id = _bar_media(bar, links, cut_media)
        bar_id = str(bar.get("id") or "")
        is_label = bar_id.startswith(_CLIP_TEXT_PREFIXES)
        old_segment = None
        clip_bound = False
        if is_label:
            old_segment = _identify_old_segment(bar, media_id, old_segments)
            # A per-shot montage text whose clip cannot be identified is ordinary
            # timed text: it follows the footage it sits over instead of vanishing.
            clip_bound = (
                old_segment is not None
                or media_id is not None
                or not bar_id.startswith(_MONTAGE_TEXT_PREFIX)
            )
            # A montage text the creator stretched past its clip is a span, not a clip-bound
            # bar: it falls through to the source-time projection below.
            if (
                bar_id.startswith(_MONTAGE_TEXT_PREFIX)
                and old_segment is not None
                and (
                    start < float(old_segment["output_start_s"]) - _SEGMENT_FIT_TOLERANCE_S
                    or end > float(old_segment["output_end_s"]) + _SEGMENT_FIT_TOLERANCE_S
                )
            ):
                clip_bound = False
        elif bar.get("segment_id") and not _is_anchored(bar, old_total):
            candidate = _identify_old_segment(bar, None, old_segments)
            if (
                candidate is not None
                and abs(start - float(candidate["output_start_s"])) <= _SEGMENT_FIT_TOLERANCE_S
                and abs(end - float(candidate["output_end_s"])) <= _SEGMENT_FIT_TOLERANCE_S
            ):
                old_segment, clip_bound = candidate, True
        params = bar.get("source_params") or {}
        sequential = bool(bar.get("sequence_source_id")) or (
            isinstance(params, dict) and bool(params.get("sequence_source_id"))
        )
        opening = start <= _EDGE_S
        closing = old_total > 0 and end >= old_total - _EDGE_S
        anchored = (opening and closing) or ((opening or closing) and _is_edge_titled(bar_id))
        if text_timing == "preserve_absolute" and (sequential or not (clip_bound or anchored)):
            if end > new_total + 1e-6:
                overflowing_text.append(bar)
            rebased.append(bar)
            continue
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
        # Edge anchoring ("keep the start / pin to the end") belongs to titles and pins.
        # Ordinary text that merely touches one edge (a group title over the first clips,
        # chat text over the last) follows its footage through source time instead;
        # only text spanning the WHOLE video keeps tracking the whole video.
        if (opening and closing) or ((opening or closing) and _is_edge_titled(bar_id)):
            updated = dict(bar)
            if opening and closing:
                new_start, new_end = 0.0, new_total
            elif opening:
                new_start, new_end = start, min(end, new_total)
            else:
                length = end - start
                new_end = new_total
                new_start = max(0.0, new_total - length)
            # Keep already-authored short words short. The duration floor only
            # protects bars that were at least this long before the edit.
            if end - start >= _MIN_BAR_S and new_end - new_start < _MIN_BAR_S:
                new_end = min(new_total, new_start + _MIN_BAR_S)
                new_start = max(0.0, new_end - _MIN_BAR_S)
            updated["start_s"], updated["end_s"] = _round(new_start), _round(new_end)
            rebased.append(updated)
            continue
        span = _project_span(project, old_segments, start, end, project_end=project_end)
        if span is None:
            drop(bar, None, False)
            continue
        start_point, end_point = span
        # The projector is right-biased on exact overlaps; anchoring the END one
        # frame early and shifting back keeps a bar that ended on a cut inside
        # its own clip.
        new_start = start_point[0]
        new_end = min(new_total, end_point[0] + (0 if retimed else _FRAME_S))
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

    if overflowing_text:
        raise ops.TextTimingConflict(
            new_total,
            max(float(bar["end_s"]) for bar in overflowing_text),
            tuple(str(bar.get("id") or "") for bar in overflowing_text),
        )
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
