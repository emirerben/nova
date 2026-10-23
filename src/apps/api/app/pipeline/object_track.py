"""Where a creator-marked object is over time, and where that lands on the canvas (KRI-827).

"No text on the plate": the creator marks the plate once (a box on one frame of one source,
`app.kria.protected_targets.ProtectedRegionSelection`). `track_object` follows it through
the clip in a killable subprocess (`object_track_worker`), at a fixed 0.1 s step, and
returns one anchor per step in SOURCE time and upright-source-normalized coordinates. The
track never stores canvas boxes: `object_canvas_boxes` maps it onto the canvas for one
output window at placement time, through the current cut (``keep_segments``) and the
current crop (cover-fill, letterbox ``scale``, KRI-547 ``position_x``), so a trim, a cleanup
cut or a crop change re-projects instead of reusing stale boxes.

Never a false "clear": an anchor where the tracker lost the object (occluded, out of frame,
or no longer matching the creator's seed patch) protects a conservative region around where
it was last and next seen, and the whole track is ``unverified``; a timeout or a worker
failure is ``unverified`` too.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.pipeline.render_geometry import NormalizedBox

STEP_S = 0.1
# A lost anchor protects the nearest good box grown by this much (canvas-agnostic,
# source-normalized): the object cannot have gone far in a few tenths of a second.
_LOST_PAD = 0.08
_TIMEOUT_BASE_S = 4.0
_TIMEOUT_PER_ANCHOR_S = 0.06
_MAX_TIMEOUT_S = 90.0

TrackStatus = Literal["tracked", "unverified", "failed"]


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


@dataclass(frozen=True)
class TrackAnchor:
    at_s: float  # source seconds
    box: NormalizedBox | None
    status: Literal["tracked", "lost"]


@dataclass(frozen=True)
class ObjectTrack:
    """One marked object through one source clip."""

    label: str
    media_id: str
    generation: str | None
    seed_t_s: float
    seed_box: NormalizedBox
    anchors: tuple[TrackAnchor, ...] = ()
    status: TrackStatus = "tracked"
    receipt: dict[str, Any] = field(default_factory=dict)

    def provenance(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "media_id": self.media_id,
            "generation": self.generation,
            "seed_t_s": self.seed_t_s,
            "seed_box": self.seed_box.as_dict(),
        }

    def box_at(self, at_s: float) -> tuple[NormalizedBox | None, bool]:
        """The object's source box at source second ``at_s`` and whether it was seen there.

        Lost (or untracked) moments get the nearest seen boxes on either side, grown by
        `_LOST_PAD`; with nothing seen at all (a timeout, a worker failure) the creator's own
        mark, grown the same way -- never ``None``."""
        if not self.anchors:
            return self.seed_box.padded(_LOST_PAD), False
        nearest = min(self.anchors, key=lambda a: abs(a.at_s - at_s))
        if nearest.status == "tracked" and abs(nearest.at_s - at_s) <= STEP_S * 0.75:
            return nearest.box, True
        seen = [a for a in self.anchors if a.status == "tracked" and a.box is not None]
        if not seen:
            return self.seed_box.padded(_LOST_PAD), False
        earlier = [a for a in seen if a.at_s <= at_s]
        later = [a for a in seen if a.at_s >= at_s]
        around = [
            *(sorted(earlier, key=lambda a: a.at_s)[-1:]),
            *(sorted(later, key=lambda a: a.at_s)[:1]),
        ] or [min(seen, key=lambda a: abs(a.at_s - at_s))]
        box = around[0].box
        assert box is not None
        for anchor in around[1:]:
            assert anchor.box is not None
            box = _union(box, anchor.box)
        return box.padded(_LOST_PAD), False


def _union(a: NormalizedBox, b: NormalizedBox) -> NormalizedBox:
    return NormalizedBox(
        min(a.left, b.left), min(a.top, b.top), max(a.right, b.right), max(a.bottom, b.bottom)
    )


def track_object(
    clip_path: str,
    *,
    label: str,
    media_id: str,
    generation: str | None,
    seed_t_s: float,
    seed_box: NormalizedBox,
    start_s: float,
    end_s: float,
    timeout_s: float | None = None,
) -> ObjectTrack:
    """Follow the marked object over source seconds [start_s, end_s] at `STEP_S`."""
    start_s, end_s = max(0.0, float(start_s)), max(0.0, float(end_s))
    count = max(1, int(round((end_s - start_s) / STEP_S)) + 1)
    anchors_s = [round(start_s + index * STEP_S, 3) for index in range(count)]
    budget = (
        float(timeout_s)
        if timeout_s is not None
        else min(_MAX_TIMEOUT_S, _TIMEOUT_BASE_S + _TIMEOUT_PER_ANCHOR_S * count)
    )
    spec = {
        "seed_t_s": float(seed_t_s),
        "box": [seed_box.left, seed_box.top, seed_box.right, seed_box.bottom],
        "anchors_s": anchors_s,
    }
    base = {
        "label": label,
        "media_id": media_id,
        "generation": generation,
        "seed_t_s": float(seed_t_s),
        "seed_box": seed_box,
    }
    started = time.monotonic()
    timed_out = False
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.pipeline.object_track_worker", clip_path, json.dumps(spec)],
            capture_output=True,
            text=True,
            timeout=max(0.01, budget),
            check=False,
        )
        stdout, returncode = result.stdout or "", result.returncode
        stderr = result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        raw = exc.stdout
        stdout = raw.decode() if isinstance(raw, bytes) else (raw or "")
        returncode, stderr = None, ""
    anchors: list[TrackAnchor] = []
    summary: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("done"):
            summary = payload
            continue
        raw_box = payload.get("box")
        box = NormalizedBox(*[float(v) for v in raw_box]) if raw_box else None
        anchors.append(
            TrackAnchor(
                at_s=float(payload["at_s"]),
                box=box,
                status="tracked" if payload.get("status") == "tracked" and box else "lost",
            )
        )
    anchors.sort(key=lambda a: a.at_s)
    lost = sum(1 for a in anchors if a.status != "tracked")
    missing = count - len(anchors)
    receipt: dict[str, Any] = {
        "anchors": count,
        "tracked": len(anchors) - lost,
        "lost": lost,
        "missing": missing,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "step_s": STEP_S,
        "window_s": [round(start_s, 3), round(end_s, 3)],
    }
    if timed_out:
        receipt["timed_out"] = True
    if summary.get("error") or (returncode not in (0, None)):
        receipt["worker_error"] = str(summary.get("error") or stderr[-200:] or returncode)
    if not any(a.status == "tracked" for a in anchors):
        status: TrackStatus = "failed"
    elif timed_out or lost or missing or receipt.get("worker_error"):
        status = "unverified"
    else:
        status = "tracked"
    return ObjectTrack(**base, anchors=tuple(anchors), status=status, receipt=receipt)


def output_to_source_s(at_s: float, keep_segments: Sequence[tuple[float, float]]) -> float | None:
    """Source-clip time of cut-timeline ``at_s`` (``None`` past the last kept span)."""
    cursor = 0.0
    for start, end in keep_segments:
        length = float(end) - float(start)
        if length <= 0:
            continue
        if at_s < cursor + length:
            return float(start) + (at_s - cursor)
        cursor += length
    return None


def object_canvas_boxes(
    track: ObjectTrack,
    *,
    window: tuple[float, float],
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    scale: float = 1.0,
    position_x: float = 0.0,
) -> tuple[list[NormalizedBox], dict[str, Any]]:
    """The object on the canvas over OUTPUT window ``window``, every `STEP_S`.

    Consecutive boxes are unioned pairwise (the swept area between two samples), so a fast
    pass between two anchors is still protected. The receipt says how many samples were
    seen vs inferred (lost -> grown nearest box) vs unknown."""
    from app.pipeline.phone_subtitled_title import source_box_to_canvas  # noqa: PLC0415

    start_s, end_s = window
    steps = max(1, int(round((end_s - start_s) / STEP_S)))
    times = [start_s + (end_s - start_s) * index / steps for index in range(steps + 1)]
    mapped: list[NormalizedBox | None] = []
    seen = inferred = unknown = 0
    for at_s in times:
        source_s = output_to_source_s(min(at_s, end_s - 1e-3), keep_segments)
        if source_s is None:
            mapped.append(None)
            continue
        box, was_seen = track.box_at(source_s)
        if box is None:
            unknown += 1
            mapped.append(None)
            continue
        if was_seen:
            seen += 1
        else:
            inferred += 1
        mapped.append(
            source_box_to_canvas(
                box,
                display_width=display_width,
                display_height=display_height,
                canvas=canvas,
                scale=scale,
                position_x=position_x,
            )
        )
    boxes: list[NormalizedBox] = []
    previous: NormalizedBox | None = None
    for box in mapped:
        if box is None:
            previous = None
            continue
        boxes.append(_union(previous, box) if previous is not None else box)
        previous = box
    status = "seen" if inferred == 0 and unknown == 0 and seen else "unverified"
    if not seen and not inferred:
        status = "unknown"
    return boxes, {
        "label": track.label,
        "samples": len(times),
        "seen": seen,
        "inferred": inferred,
        "unknown": unknown,
        "status": status,
    }


def object_collision(text_box: NormalizedBox, object_box: NormalizedBox) -> float:
    """How much text and object overlap: the shared area over the SMALLER of the two, so a
    small plate wholly under a wide title counts as fully covered."""
    shared = text_box.intersection_area(object_box)
    smaller = min(text_box.area, object_box.area)
    return shared / smaller if smaller > 0 else 0.0


__all__ = [
    "STEP_S",
    "ObjectTrack",
    "TrackAnchor",
    "object_canvas_boxes",
    "object_collision",
    "output_to_source_s",
    "track_object",
]
