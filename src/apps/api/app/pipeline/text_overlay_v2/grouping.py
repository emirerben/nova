"""Stage C: temporal grouping — `FrameDetection[]` to `TextEvent[]`.

Per-frame OCR returns one detection per visible line per frame. The same
line typically appears in many consecutive frames; this stage clusters
those repeated detections into a single `TextEvent` that knows when the
line first appeared and when it last disappeared.

Two detections in adjacent frames match if their normalized text is
identical AND their axis-aligned bboxes overlap with IoU above
`iou_match_threshold` (default 0.3). The IoU check defends against same-
text-different-position cases (e.g. a watermark "Live" and a hook "Live"
at different screen positions are separate events).

A short jitter gap (`jitter_max_gap_s`, default 1.0s) is allowed for
frames where OCR drops a detection — without this tolerance, a single
missed frame would split one logical event into two. 1.0s covers two
missed frames at 2fps sampling AND is generous enough for higher framerates.

The output is sorted by `start_t_s` then by Y-coordinate (top to bottom)
so downstream phrase reconstruction sees lines in their visual reading order.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict

from app.agents._schemas.text_overlay_ocr import FrameDetection
from app.agents._schemas.text_overlay_pipeline import TextEvent, _aabb_iou, _aabb_union

DEFAULT_IOU_MATCH = 0.3
DEFAULT_JITTER_MAX_GAP_S = 1.0


def _normalize_for_match(text: str) -> str:
    """Normalize text for cross-frame matching.

    NFC normalization makes pre-composed and decomposed Unicode forms match
    (e.g. é vs e + ́). Casefold is stronger than .lower() for
    non-ASCII text. Internal whitespace is collapsed so OCR variations like
    "It's not" vs "It's  not" still match.
    """
    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join(text.split())


class _OpenEvent:
    """Mutable accumulator for an in-progress event during the walk.

    Held only inside this module; finalized into an immutable `TextEvent`
    before being returned.
    """

    __slots__ = (
        "_text_original",
        "_match_key",
        "_start_t_s",
        "_last_t_s",
        "_aabb",
        "_confidence_sum",
        "_n_frames",
    )

    def __init__(self, detection: FrameDetection, frame_t_s: float) -> None:
        self._text_original = detection.text
        self._match_key = _normalize_for_match(detection.text)
        self._start_t_s = frame_t_s
        self._last_t_s = frame_t_s
        self._aabb = detection.polygon.aabb()
        self._confidence_sum = float(detection.confidence)
        self._n_frames = 1

    @property
    def match_key(self) -> str:
        return self._match_key

    @property
    def aabb(self) -> tuple[float, float, float, float]:
        return self._aabb

    @property
    def last_t_s(self) -> float:
        return self._last_t_s

    def extend(self, detection: FrameDetection, frame_t_s: float) -> None:
        self._last_t_s = frame_t_s
        self._aabb = _aabb_union(self._aabb, detection.polygon.aabb())
        self._confidence_sum += float(detection.confidence)
        self._n_frames += 1

    def finalize(self) -> TextEvent:
        return TextEvent(
            text=self._text_original,
            start_t_s=self._start_t_s,
            end_t_s=self._last_t_s,
            aabb=self._aabb,
            mean_confidence=self._confidence_sum / max(self._n_frames, 1),
        )


def group_detections_into_events(
    detections: list[FrameDetection],
    *,
    iou_match_threshold: float = DEFAULT_IOU_MATCH,
    jitter_max_gap_s: float = DEFAULT_JITTER_MAX_GAP_S,
) -> list[TextEvent]:
    """Cluster per-frame OCR detections into contiguous events.

    Walks frames in chronological order. For each detection in the current
    frame, finds the open event with the same normalized text and the highest
    bbox IoU (above `iou_match_threshold`) and extends it. Detections without
    a match start new events. Events whose last-seen frame is older than
    `jitter_max_gap_s` are closed before processing the next frame.

    Pure function: no I/O, no state outside the call. Safe to call from any
    context, fully deterministic given the input.
    """
    if not detections:
        return []

    by_frame: dict[float, list[FrameDetection]] = defaultdict(list)
    for d in detections:
        by_frame[d.frame_t_s].append(d)
    frame_times = sorted(by_frame.keys())

    open_events: list[_OpenEvent] = []
    closed_events: list[_OpenEvent] = []

    for t in frame_times:
        # Close stale events BEFORE matching the new frame's detections.
        # Without this ordering, a stale event with the same text + position
        # would still be in the match pool and would absorb a detection that
        # logically belongs to a fresh re-appearance (after a screen clear).
        still_open: list[_OpenEvent] = []
        for ev in open_events:
            if t - ev.last_t_s > jitter_max_gap_s:
                closed_events.append(ev)
            else:
                still_open.append(ev)
        open_events = still_open

        dets_this_frame = by_frame[t]

        candidates: list[tuple[float, FrameDetection, _OpenEvent]] = []
        for det in dets_this_frame:
            det_key = _normalize_for_match(det.text)
            det_aabb = det.polygon.aabb()
            for ev in open_events:
                if ev.match_key != det_key:
                    continue
                iou = _aabb_iou(det_aabb, ev.aabb)
                if iou >= iou_match_threshold:
                    candidates.append((iou, det, ev))

        candidates.sort(key=lambda c: c[0], reverse=True)
        used_dets: set[int] = set()
        used_evs: set[int] = set()
        for _iou, det, ev in candidates:
            if id(det) in used_dets or id(ev) in used_evs:
                continue
            ev.extend(det, t)
            used_dets.add(id(det))
            used_evs.add(id(ev))

        for det in dets_this_frame:
            if id(det) in used_dets:
                continue
            open_events.append(_OpenEvent(det, t))

    closed_events.extend(open_events)

    events = [ev.finalize() for ev in closed_events]
    events.sort(key=lambda e: (e.start_t_s, e.aabb[1]))
    return events
