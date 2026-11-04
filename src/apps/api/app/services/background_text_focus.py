"""Free measure of where the text sits in a landscape background (Qendresa item 18).

A 16:9 screen recording shown behind the cut-out speaker is cover-cropped to the middle
31.6 % of its width, so every README line is cut at its start. This module finds the text
column of such a picture from a few grey frames of the file the worker already downloaded
(no model, no vision API) and turns it into a focus (focal x/y + zoom) with the same meaning
as KriaMediaEngine's ``VisualMediaPlacement``: base scale = cover, drawn scale =
``max(cover * zoom, contain)``, ``x``/``y`` align the picture (0 = left edge, 0.5 = centre,
1 = right edge).

Everything here is best effort: ``measure_text_column`` never raises and returns None for
anything it cannot measure; ``focus_for_text_column`` is pure math. The feature flag is
checked by the caller. Failure modes F3-F9: docs/reviews/background-focus-failure-modes.md.
"""

from __future__ import annotations

import subprocess
from typing import Literal

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

log = structlog.get_logger()

TEXT_COLUMN_VERSION = 1
TARGET_LINE_PITCH_PX = 40.0  # one text line on the 1920-px canvas; her own videos: 39-44 px
MAX_AUTO_ZOOM = 2.5
MIN_FOCUS_ZOOM = 0.2  # wire bounds of a focus zoom
MAX_FOCUS_ZOOM = 4.0
LANDSCAPE_MARGIN = 1.25  # measure only when picture aspect >= canvas aspect * this

# Measure constants (set on a prototype over four layouts).
_SAMPLE_W, _SAMPLE_H = 960, 540
_FRAME_BYTES = _SAMPLE_W * _SAMPLE_H
_MAX_FRAMES = 6
_MAX_SPAN_S = 60.0
_FALLBACK_FPS = 0.2
_TIMEOUT_S = 40
_PROBE_TIMEOUT_S = 10
_STEP_LUMA = 48  # a glyph stroke is a neighbour-pixel luma jump at least this big
_MIN_ROW_STEPS = 6  # rows with fewer steps are lone borders/rules
_SMOOTH_FRAC = 60  # box width = max(3, n // 60)
_PEAK_FRAC = 0.2
_MERGE_GAP = 0.04
_MIN_TOTAL_STEPS = 0.002  # of pixels * frames
_MIN_RUN_MASS = 0.5
_MIN_RUN_WIDTH = 0.08
_MAX_RUN_WIDTH = 0.85
_ROW_SHARE = (0.12, 0.88)
_TEXT_ROW_FRAC = 0.15
_MIN_PITCH_DIFFS = 3


class TextColumn(BaseModel):
    """Where the text column sits, as fractions of the upright picture."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    left: float = Field(ge=0.0, le=1.0)
    right: float = Field(ge=0.0, le=1.0)
    line_pitch: float | None = Field(default=None, gt=0.0, le=1.0)  # of picture HEIGHT
    version: int = TEXT_COLUMN_VERSION

    @model_validator(mode="after")
    def _right_after_left(self) -> TextColumn:
        if not self.right > self.left:
            raise ValueError("right must be greater than left")
        return self


class BackgroundFocus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    zoom: float = Field(ge=MIN_FOCUS_ZOOM, le=MAX_FOCUS_ZOOM)


def should_measure(
    width: int | None, height: int | None, *, canvas_w: int = 1080, canvas_h: int = 1920
) -> bool:
    """True only for an upright picture clearly wider (in aspect) than the canvas."""
    if not width or not height or width <= 0 or height <= 0 or canvas_w <= 0 or canvas_h <= 0:
        return False
    return (width / height) >= (canvas_w / canvas_h) * LANDSCAPE_MARGIN


# ---------------------------------------------------------------------------- frames


def _image_frame(path: str, width: int | None, height: int | None) -> np.ndarray | None:
    from PIL import Image, ImageOps

    try:  # HEIC stills from iPhones
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:  # noqa: BLE001 -- optional dependency
        pass
    with Image.open(path) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        if not should_measure(width or im.width, height or im.height):
            return None
        gray = im.convert("L").resize((_SAMPLE_W, _SAMPLE_H), Image.BILINEAR)
        return np.asarray(gray, dtype=np.uint8).reshape(1, _SAMPLE_H, _SAMPLE_W)


def _probe_duration(path: str) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, timeout=_PROBE_TIMEOUT_S,
        )  # fmt: skip
        value = float(proc.stdout.decode().strip())
    except Exception:  # noqa: BLE001 -- unknown duration is fine
        return None
    return value if value > 0 else None


def _video_frames(path: str) -> np.ndarray | None:
    duration = _probe_duration(path)
    span = min(duration, _MAX_SPAN_S) if duration else None
    fps = _MAX_FRAMES / span if span else _FALLBACK_FPS
    args = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", path,
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-t", str(int(_MAX_SPAN_S)),
        "-vf", f"fps={fps:.6f},scale={_SAMPLE_W}:{_SAMPLE_H}:flags=area,format=gray",
        "-frames:v", str(_MAX_FRAMES), "-f", "rawvideo", "-",
    ]  # fmt: skip
    proc = subprocess.run(args, capture_output=True, timeout=_TIMEOUT_S, check=True)
    data = proc.stdout
    count = min(len(data) // _FRAME_BYTES, _MAX_FRAMES)
    if count == 0:
        return None
    arr = np.frombuffer(data[: count * _FRAME_BYTES], dtype=np.uint8)
    return arr.reshape(count, _SAMPLE_H, _SAMPLE_W)


# ---------------------------------------------------------------------------- measure


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive (start, end) index pairs of True runs."""
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b) - 1) for a, b in zip(edges[0::2], edges[1::2], strict=True)]


def _column_from_frames(frames: np.ndarray) -> TextColumn | None:
    n = _SAMPLE_W - 1
    steps_all = []
    profile = np.zeros(n, dtype=np.float64)
    total = 0
    for frame in frames:
        steps = np.abs(np.diff(frame.astype(np.int16), axis=1)) >= _STEP_LUMA
        steps[steps.sum(axis=1) < _MIN_ROW_STEPS] = False
        steps_all.append(steps)
        profile += steps.sum(axis=0)
        total += int(steps.sum())
    if total < _MIN_TOTAL_STEPS * _FRAME_BYTES * len(frames):
        return None

    k = max(3, n // _SMOOTH_FRAC)
    smooth = np.convolve(profile, np.ones(k) / k, mode="same")
    peak = float(smooth.max())
    if peak <= 0:
        return None
    runs = _runs(smooth >= _PEAK_FRAC * peak)
    if not runs:
        return None
    merged: list[list[int]] = [list(runs[0])]
    for a, b in runs[1:]:
        if a - merged[-1][1] - 1 < _MERGE_GAP * n:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    best = max(merged, key=lambda r: float(smooth[r[0] : r[1] + 1].sum()))
    start, end = best
    mass = float(smooth[start : end + 1].sum())
    if mass < _MIN_RUN_MASS * float(smooth.sum()):
        return None
    width = (end + 1 - start) / n
    if width < _MIN_RUN_WIDTH or width > _MAX_RUN_WIDTH:
        return None

    shares = []
    diffs: list[int] = []
    for steps in steps_all:
        r = steps[:, start : end + 1].sum(axis=1)
        shares.append(float((r >= _MIN_ROW_STEPS).mean()))
        if r.max() <= 0:
            continue
        starts = [a for a, _ in _runs(r >= _TEXT_ROW_FRAC * r.max())]
        diffs.extend(int(d) for d in np.diff(starts))
    share = float(np.mean(shares))
    if not _ROW_SHARE[0] <= share <= _ROW_SHARE[1]:
        return None

    pitch = None
    if len(diffs) >= _MIN_PITCH_DIFFS:
        pitch = float(np.median(diffs)) / _SAMPLE_H
        if not 0.0 < pitch <= 1.0:
            pitch = None
    return TextColumn(left=start / n, right=(end + 1) / n, line_pitch=pitch)


def measure_text_column(
    path: str,
    *,
    kind: Literal["image", "video"],
    width: int | None = None,
    height: int | None = None,
) -> TextColumn | None:
    """The text column of one image/video file, or None. Never raises.

    ``width``/``height`` are the UPRIGHT (display) size when the caller knows it. An image
    reads its own size when they are missing; a video without a known size is not measured.
    """
    try:
        if kind == "image":
            if (width or height) and not should_measure(width, height):
                return None
            frames = _image_frame(path, width, height)
        else:
            if not should_measure(width, height):
                return None
            frames = _video_frames(path)
        if frames is None:
            return None
        return _column_from_frames(frames)
    except Exception as exc:  # noqa: BLE001 -- best-effort, never fails a render
        log.warning("background_text_focus.measure_failed", error_type=type(exc).__name__)
        return None


# ---------------------------------------------------------------------------- focus


def focus_for_text_column(
    column: TextColumn,
    *,
    source_w: int,
    source_h: int,
    canvas_w: int = 1080,
    canvas_h: int = 1920,
) -> BackgroundFocus | None:
    """Zoom/align so the column fills the width, lines stay readable and start in view."""
    if min(source_w, source_h, canvas_w, canvas_h) <= 0:
        return None
    cover = max(canvas_w / source_w, canvas_h / source_h)
    contain = min(canvas_w / source_w, canvas_h / source_h)
    v_cover = canvas_w / (source_w * cover)  # visible share of the width at zoom 1
    if v_cover >= 0.999:
        return None
    pad = 0.02
    a = max(0.0, column.left - pad)
    b = min(1.0, column.right + pad)
    if b <= a:
        return None
    z_fit = v_cover / (b - a)
    if column.line_pitch:
        z_read = TARGET_LINE_PITCH_PX / (column.line_pitch * source_h * cover)
    else:
        z_read = v_cover / 0.5  # no pitch: show at most half the width
    z = min(MAX_AUTO_ZOOM, max(z_fit, z_read, contain / cover, MIN_FOCUS_ZOOM))
    v = v_cover / z
    # Wider than what shows: start at the column's left edge so lines read from their start.
    start = a if (b - a) > v else (a + b) / 2 - v / 2
    start = min(max(start, 0.0), max(0.0, 1.0 - v))
    x = start / (1.0 - v) if v < 1.0 else 0.5
    return BackgroundFocus(x=round(min(max(x, 0.0), 1.0), 3), y=0.5, zoom=round(z, 3))


__all__ = [
    "LANDSCAPE_MARGIN",
    "MAX_AUTO_ZOOM",
    "MAX_FOCUS_ZOOM",
    "MIN_FOCUS_ZOOM",
    "TARGET_LINE_PITCH_PX",
    "TEXT_COLUMN_VERSION",
    "BackgroundFocus",
    "TextColumn",
    "focus_for_text_column",
    "measure_text_column",
    "should_measure",
]
