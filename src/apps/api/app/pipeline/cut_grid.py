"""The frame grid a keep-segment cut renders on (plans/010).

``reframe._build_keep_segments_cmd`` renders each keep segment as whole CFR
frames, its boundaries snapped to the nearest frame, and cuts the audio on
the same frames. Anything placed on that cut output (caption words, b-roll
anchors) has to use the same grid: mapping through the raw float plan puts
every later segment off by the snapped remainders before it, which add up
over many cuts.

Pure (no FFmpeg, no settings) so the renderer and ``silence_cut``'s remap
share one definition without an import cycle.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class FrameGrid:
    """The grid one cut render uses.

    ``duration_s`` is the length of the window the cut renders from (the
    reframe's ``end_s - start_s``), not the plan's: a segment reaching it
    keeps every frame up to it.
    """

    fps: int
    duration_s: float


def keep_segment_frames(
    keep_segments: list[tuple[float, float]],
    fps: int,
    duration: float,
) -> list[tuple[int, int]]:
    """Return the [first, end) CFR frame range of each keep segment that snaps to a frame.

    Each boundary snaps to the nearest frame, so the audio, which follows the
    picture, is cut at most half a frame from the plan: snapping both edges up
    played up to a frame of removed audio (a discarded retake's onset) at
    full level and faded the next kept word's onset. A segment reaching the
    clip end keeps every frame up to it. A segment that snaps to no frame is
    dropped — it would add audio over a frozen picture and nothing else.
    """
    frames: list[tuple[int, int]] = []
    for seg_start, seg_end in keep_segments:
        first = math.floor(seg_start * fps + 0.5)
        if seg_end >= duration:
            # The epsilon absorbs float noise (12.0 * 30 = 360.00000000000006).
            end = math.ceil(seg_end * fps - 1e-6)
        else:
            end = math.floor(seg_end * fps + 0.5)
        if end > first:
            frames.append((first, end))
    return frames


def frames_to_spans(frames: Iterable[tuple[int, int]], fps: int) -> list[tuple[float, float]]:
    """The source seconds each [first, end) frame range covers."""
    return [(first / fps, end / fps) for first, end in frames]


def rendered_spans(
    keep_segments: list[tuple[float, float]], grid: FrameGrid
) -> list[tuple[float, float]]:
    """The source span each rendered segment plays, in output order."""
    return frames_to_spans(keep_segment_frames(keep_segments, grid.fps, grid.duration_s), grid.fps)


def output_time(t: float, spans: Iterable[tuple[float, float]]) -> float:
    """Where source ``t`` plays in a render of ``spans`` (in output order).

    The summed length of the spans up to ``t``: an instant inside span k
    plays at the spans before it plus its offset into k, and one that no
    span plays maps to the cut it falls in.
    """
    return sum(max(0.0, min(t, end) - start) for start, end in spans)


def snap_to_frame(t: float, fps: int) -> float:
    """``t`` on the nearest frame, rounded the way an interior cut boundary is."""
    return math.floor(t * fps + 0.5) / fps
