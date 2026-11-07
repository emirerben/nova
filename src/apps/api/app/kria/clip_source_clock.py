"""Source-clock transformations for moving footage followed by a held frame."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceWindow:
    start: float
    end: float
    rate: float
    moving: float
    hold: float
    frame: float | None


def source_window(row: dict) -> SourceWindow:
    start = float(row.get("in_s") or 0)
    rate = float(row.get("playback_rate") or 1)
    duration = float(row.get("duration_s") or 0)
    hold = float(row.get("freeze_hold_s") or 0)
    moving = max(0.0, duration - hold)
    end = (
        float(row["source_end_s"]) if row.get("source_end_s") is not None else start + moving * rate
    )
    frame = float(row.get("freeze_frame_s", end)) if hold else None
    if (
        not all(math.isfinite(v) for v in (start, end, rate, duration, hold))
        or rate <= 0
        or hold < 0
        or duration <= 0
    ):
        raise ValueError("Invalid source clock")
    return SourceWindow(start, end, rate, moving, hold, frame)


def slice_source_window(row: dict, start: float, end: float) -> dict:
    clock = source_window(row)
    duration = clock.moving + clock.hold
    if start < 0 or end > duration + 1e-6 or end - start < 0.1 - 1e-6:
        raise ValueError("A source slice must fit the clip and last at least 0.1 seconds")
    result = copy.deepcopy(row)
    moving = max(0.0, min(end, clock.moving) - min(start, clock.moving))
    hold = max(0.0, end - start - moving)
    source_start = clock.start + min(start, clock.moving) * clock.rate
    source_end = source_start + moving * clock.rate
    rate = clock.rate
    if moving <= 1e-9 and hold:
        frame = clock.frame
        if frame is None or frame <= 0:
            raise ValueError("The held frame needs a positive source sample endpoint")
        source_start, source_end, rate = max(0.0, frame - 1 / 30), frame, 1.0
        hold = end - start - (source_end - source_start)
    result.update(in_s=source_start, duration_s=end - start, duration_beats=None)
    if "source_end_s" in row or clock.hold:
        result["source_end_s"] = source_end
    if "playback_rate" in row or clock.hold:
        result["playback_rate"] = rate
    if clock.hold:
        result["freeze_hold_s"] = hold
        result["freeze_frame_s"] = clock.frame if hold else None
    return result


def retime_source_window(row: dict, rate: float) -> dict:
    if not math.isfinite(rate) or not 0.25 <= rate <= 4:
        raise ValueError("Playback rate must be between 0.25 and 4")
    clock = source_window(row)
    result = copy.deepcopy(row)
    result.update(
        playback_rate=rate,
        source_end_s=clock.end,
        duration_s=(clock.end - clock.start) / rate + clock.hold,
        duration_beats=None,
    )
    return result


def resize_source_window(row: dict, duration: float) -> dict:
    clock = source_window(row)
    current = clock.moving + clock.hold
    if (
        not clock.hold
        and row.get("media_kind") != "image"
        and row.get("source_duration_s") is not None
    ):
        duration = min(duration, (float(row["source_duration_s"]) - clock.start) / clock.rate)
    if duration < 0.1:
        raise ValueError("There is not enough source footage")
    if duration <= current:
        return slice_source_window(row, 0, duration)
    result = copy.deepcopy(row)
    if clock.hold:
        result.update(
            duration_s=duration, duration_beats=None, freeze_hold_s=clock.hold + duration - current
        )
        return result
    if row.get("media_kind") != "image" and row.get("source_duration_s") is not None:
        duration = min(duration, (float(row["source_duration_s"]) - clock.start) / clock.rate)
    if duration < 0.1:
        raise ValueError("There is not enough source footage")
    result.update(duration_s=duration, duration_beats=None)
    if "source_end_s" in row:
        result["source_end_s"] = clock.start + duration * clock.rate
    return result


def set_source_start(row: dict, start: float) -> dict:
    clock = source_window(row)
    if start < 0 or start >= clock.end:
        raise ValueError("The source start must precede its selected end")
    result = copy.deepcopy(row)
    result.update(
        in_s=start,
        duration_s=(clock.end - start) / clock.rate + clock.hold,
        source_end_s=clock.end,
        source_start_locked=True,
        duration_beats=None,
    )
    return result
