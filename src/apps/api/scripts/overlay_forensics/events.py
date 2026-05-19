"""TextEvent dataclass + contiguous-frame clustering + animation classifier.

A "text event" is a contiguous run of frames where the same overlay is
visible — same color, similar position. The analyzer sweeps frames at a
fixed step (default 0.05s), detects per-frame bboxes for each color, then
groups consecutive detections into events.

Animation classification (entrance type) is inferred from the bbox / mask
trajectory over the event's first ~0.4s:

  - slide-up   : centroid_y decreases monotonically and drops > 5% of frame
                 height during settle.
  - fade-in    : mean text-pixel brightness ramps up by > 0.3 normalized.
  - font-cycle : text pixel count has high frame-to-frame variance and
                 position stays stable.
  - scale-up   : text pixel count grows monotonically.
  - static     : nothing changes meaningfully.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .masking import BBox, RelativeBBox

AnimationEntrance = Literal["slide-up", "fade-in", "font-cycle", "scale-up", "static", "unknown"]


@dataclass
class FrameObservation:
    """A single-frame detection of an overlay in one color.

    Keep `frame_array` reference if downstream OCR / font-shape analysis
    needs the original pixels; set to None to drop memory.
    """
    t_s: float
    color_key: str
    bbox: BBox
    mask_pixel_count: int
    mean_brightness: float
    frame_array: np.ndarray | None = field(default=None, repr=False)
    mask: np.ndarray | None = field(default=None, repr=False)


@dataclass
class TextEvent:
    """A contiguous run of frame observations of the same overlay."""
    color_key: str
    observations: list[FrameObservation]
    text: str | None = None
    ocr_confidence: float | None = None
    text_source: Literal["ocr", "color-region-fallback", "unknown"] = "unknown"

    @property
    def t_start(self) -> float:
        return self.observations[0].t_s

    @property
    def t_end(self) -> float:
        return self.observations[-1].t_s

    @property
    def duration_s(self) -> float:
        return self.t_end - self.t_start

    def median_bbox(self) -> BBox:
        xs_min = [o.bbox.x_min for o in self.observations]
        ys_min = [o.bbox.y_min for o in self.observations]
        xs_max = [o.bbox.x_max for o in self.observations]
        ys_max = [o.bbox.y_max for o in self.observations]
        return BBox(
            x_min=int(np.median(xs_min)),
            y_min=int(np.median(ys_min)),
            x_max=int(np.median(xs_max)),
            y_max=int(np.median(ys_max)),
        )

    def median_relative_bbox(self, frame_w: int, frame_h: int) -> RelativeBBox:
        return self.median_bbox().to_relative(frame_w, frame_h)

    def peak_observation(self) -> FrameObservation:
        """Observation with the highest mask pixel count (best for OCR)."""
        return max(self.observations, key=lambda o: o.mask_pixel_count)


def _region_bucket(centroid: tuple[float, float], frame_w: int, frame_h: int,
                   grid: int = 6) -> tuple[int, int]:
    """Quantize a centroid to a coarse grid cell so jittery bboxes still
    cluster together. Default 6x6 grid is generous enough to absorb a
    slide-up settle while still distinguishing "top-left" from "center"."""
    cx, cy = centroid
    bx = min(grid - 1, max(0, int(cx / max(frame_w, 1) * grid)))
    by = min(grid - 1, max(0, int(cy / max(frame_h, 1) * grid)))
    return (bx, by)


def cluster_observations_to_events(
    observations: list[FrameObservation],
    frame_w: int,
    frame_h: int,
    *,
    step_s: float,
    gap_tolerance_steps: int = 1,
    min_duration_s: float = 0.10,
    region_grid: int = 6,
) -> list[TextEvent]:
    """Group per-frame observations into TextEvent runs.

    A run = consecutive observations sharing (color_key, region_bucket),
    tolerating up to `gap_tolerance_steps` missing frames in the middle.
    Runs shorter than `min_duration_s` are dropped as noise.

    Returns events sorted by t_start.
    """
    if not observations:
        return []

    # Bucket every observation, then walk in time order and grow runs.
    keyed = [
        (o, (o.color_key, _region_bucket(o.bbox.centroid, frame_w, frame_h, region_grid)))
        for o in sorted(observations, key=lambda o: o.t_s)
    ]

    # Tracks: dict[key -> (run_observations, last_t)]
    active: dict[tuple, tuple[list[FrameObservation], float]] = {}
    completed: list[TextEvent] = []
    # Tolerate up to N missing steps before closing the run. The +0.5 step
    # epsilon absorbs floating-point drift in sampled timestamps.
    gap_tol_s = (gap_tolerance_steps + 1) * step_s + step_s * 0.5

    for obs, key in keyed:
        # Close out any runs that haven't been seen recently.
        to_close = [k for k, (_, last_t) in active.items() if obs.t_s - last_t > gap_tol_s]
        for k in to_close:
            run, _ = active.pop(k)
            ev = TextEvent(color_key=k[0], observations=run)
            if ev.duration_s + step_s >= min_duration_s - 1e-6:
                completed.append(ev)

        if key in active:
            run, _ = active[key]
            run.append(obs)
            active[key] = (run, obs.t_s)
        else:
            active[key] = ([obs], obs.t_s)

    for k, (run, _) in active.items():
        ev = TextEvent(color_key=k[0], observations=run)
        if ev.duration_s + step_s >= min_duration_s - 1e-6:
            completed.append(ev)

    completed.sort(key=lambda e: e.t_start)
    return completed


def _is_monotonic_decrease(seq: list[float], min_drop: float) -> bool:
    if len(seq) < 3:
        return False
    if seq[0] - seq[-1] < min_drop:
        return False
    # Allow small upward blips; require overall trend down.
    descents = sum(1 for a, b in zip(seq, seq[1:]) if b <= a + 1e-3)
    return descents >= len(seq) - 2


def _is_monotonic_increase(seq: list[float], min_rise: float) -> bool:
    if len(seq) < 3:
        return False
    if seq[-1] - seq[0] < min_rise:
        return False
    ascents = sum(1 for a, b in zip(seq, seq[1:]) if b >= a - 1e-3)
    return ascents >= len(seq) - 2


def classify_entrance(
    event: TextEvent,
    frame_w: int,
    frame_h: int,
    *,
    settle_window_s: float = 0.4,
) -> tuple[AnimationEntrance, float]:
    """Return (entrance_type, entrance_duration_s).

    Analyzes the first `settle_window_s` of the event. Order of checks
    matters — slide motion is the most distinctive signal, so it wins over
    pixel-count growth that might also be present during a slide-up.
    """
    if not event.observations:
        return ("unknown", 0.0)

    cutoff = event.t_start + settle_window_s
    window = [o for o in event.observations if o.t_s <= cutoff]
    if len(window) < 3:
        return ("static", 0.0)

    ys = [o.bbox.centroid[1] for o in window]
    pixel_counts = [float(o.mask_pixel_count) for o in window]
    brightness = [o.mean_brightness for o in window]

    # Slide-up: y decreases by > 5% of frame height.
    slide_drop_px = frame_h * 0.05
    if _is_monotonic_decrease(ys, slide_drop_px):
        return ("slide-up", _time_to_settle(ys, window))

    # Fade-in: brightness ramps from low to high.
    if _is_monotonic_increase(brightness, 0.3):
        return ("fade-in", _time_to_settle(brightness, window))

    # Scale-up: pixel count grows monotonically. Check BEFORE font-cycle —
    # a monotonic ramp also has high variance but is not cycling.
    px_mean = float(np.mean(pixel_counts)) if pixel_counts else 1.0
    if _is_monotonic_increase(pixel_counts, px_mean * 0.5):
        return ("scale-up", _time_to_settle(pixel_counts, window))

    # Font-cycle: high relative variance in pixel count, no y motion, and
    # not monotonic (cycling means up-down-up-down, not a single ramp).
    px_std = float(np.std(pixel_counts)) if len(pixel_counts) > 1 else 0.0
    rel_std = px_std / max(px_mean, 1.0)
    y_range = max(ys) - min(ys)
    if rel_std > 0.18 and y_range < frame_h * 0.02:
        return ("font-cycle", 0.0)

    return ("static", 0.0)


def _time_to_settle(seq: list[float], window: list[FrameObservation]) -> float:
    """Approximate time to reach 90% of the final value."""
    if len(seq) < 2:
        return 0.0
    final = seq[-1]
    initial = seq[0]
    span = final - initial
    if abs(span) < 1e-6:
        return 0.0
    target = initial + 0.9 * span
    for v, obs in zip(seq, window):
        # Reached target — sign-aware for both increases and decreases.
        if (span > 0 and v >= target) or (span < 0 and v <= target):
            return float(obs.t_s - window[0].t_s)
    return float(window[-1].t_s - window[0].t_s)


def cooccurrence_intervals(events: list[TextEvent]) -> list[tuple[float, float, set[str]]]:
    """Walk the timeline and produce (t_start, t_end, set_of_event_texts)
    intervals where the set of simultaneously-visible event texts is
    constant. Useful for asking "was Africa visible while This+is were
    visible." Uses the event.text field (must be populated first).
    """
    if not events:
        return []
    # Build sweep-line events.
    boundaries = sorted({e.t_start for e in events} | {e.t_end for e in events})
    out: list[tuple[float, float, set[str]]] = []
    for a, b in zip(boundaries, boundaries[1:]):
        mid = (a + b) / 2
        visible = {e.text or e.color_key for e in events if e.t_start <= mid <= e.t_end}
        out.append((a, b, visible))
    return out
