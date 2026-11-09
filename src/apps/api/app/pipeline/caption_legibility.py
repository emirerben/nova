"""Caption legibility on any background (KRI-1343 / KRI-1344).

Two decisions, both measured on the pixels the caption will actually sit on:

1. **Treatment** (whole caption track). The worst WCAG contrast between the caption fill and the
   pixels under each line where it is finally drawn (take frames, full-screen photos and B-roll
   while it is up) decides whether the look needs help. Below 7:1 on the sampled frames (4.5:1
   with headroom for unsampled moments) the look gains an outline in the ink that reads against
   the fill (black under white, white under black), sized to the text. A creator who set the outline keeps
   it; then a firm shadow is the treatment; a creator who set both gets neither (their call,
   recorded on the receipt). The fill colour is never changed: "small, white" is size and colour,
   not a request to drop legibility.

2. **Placement during a full-screen picture** (per caption line). While a full-screen photo or
   video card is up, the line moves to the nearest calm band of that picture (no edges: no text,
   no busy texture), or, when the picture has none and its least busy band is still dense with
   strong edges (a page of text, a cluttered ad), the line is hidden for that window.

The measures are deliberately simple (no model, no paid call): a morphological gradient on a
270x480 grey cover-fit of the picture. Text always produces strong edges, so a band with no
edges holds no text; validated against Apple Vision OCR boxes on the test-kit photos.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

CAPTION_LEGIBILITY_VERSION = 1
# WCAG AA for normal text; captions are read in motion, so the body-text bar applies.
MIN_CONTRAST = 4.5
# A plain caption is left without help only when the SAMPLED backgrounds clear WCAG AAA (7:1):
# a handful of frames per take can miss a moment of light clothing or a bright window, so the
# AA bar (4.5:1) must hold with headroom (corpus, 2026-11-09: AA-only sampling let 2% of lines
# ship below 4.5:1).
UNTREATED_MIN_CONTRAST = 7.0
# The grey grid a full-screen picture is judged on (cover-fit, like the phone draws it).
GRID_W, GRID_H = 270, 480
# Gradient strengths (0-255 grey steps across 3 px): any visible edge / a hard, text-like edge.
_WEAK_EDGE = 28
_STRONG_EDGE = 90
# A band is calm when at most this share of it touches an edge.
_CALM_COVERAGE = 0.02
# With no calm band, a band whose strong-edge share stays above this is text-dense: hide there.
_TEXT_DENSE_COVERAGE = 0.08
# Clear space kept above and below the caption block, canvas fraction.
_BAND_MARGIN = 0.02
# Where a moved line may sit (block centre, canvas fraction): inside the phone's caption frame.
_SCAN_TOP, _SCAN_BOTTOM = 0.12, 0.86


def stroke_px_for(size_px: float) -> int:
    """Outline width for a caption of ``size_px`` on the 1080x1920 canvas: about 7.5% of the
    size, so 42 px small captions get 3 px and 78 px default captions 6 px (never 0, never
    heavier than the 12 px the look allows)."""
    return int(max(2, min(6, round(float(size_px) * 0.075))))


def _linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    r, g, b = (_linear(int(color[i : i + 2], 16) / 255) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: float, b: float) -> float:
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def readable_ink(fill: str) -> str:
    """Black or white, whichever has more contrast on ``fill`` (always >= 4.5:1)."""
    lum = relative_luminance(fill)
    return "#000000" if contrast_ratio(lum, 0.0) >= contrast_ratio(lum, 1.0) else "#FFFFFF"


@dataclass(frozen=True, slots=True)
class GreyFrame:
    """One sampled background: grey bytes (row-major), its size, and the timeline window it
    stands for (cut-timeline seconds)."""

    pixels: bytes
    width: int
    height: int
    start_s: float
    end_s: float


def worst_contrast(fill: str, box: Sequence[float], frames: Iterable[GreyFrame]) -> float | None:
    """The lowest contrast between ``fill`` and the pixels under ``box`` (canvas fractions)
    across ``frames``: each frame's darkest and brightest 10% (p10/p90, the same reading as
    `phone_text_background.automatic_fill`), so a bright band under part of the line counts.
    None when no frame covers the box."""
    fill_lum = relative_luminance(fill)
    worst: float | None = None
    for frame in frames:
        x0, y0 = max(0, int(box[0] * frame.width)), max(0, int(box[1] * frame.height))
        x1 = min(frame.width, int(box[2] * frame.width) + 1)
        y1 = min(frame.height, int(box[3] * frame.height) + 1)
        values = sorted(
            frame.pixels[y * frame.width + x] for y in range(y0, y1) for x in range(x0, x1)
        )
        if not values:
            continue
        for level in (values[int(len(values) * 0.1)], values[int((len(values) - 1) * 0.9)]):
            ratio = contrast_ratio(fill_lum, _linear(level / 255))
            worst = ratio if worst is None else min(worst, ratio)
    return worst


@dataclass(frozen=True, slots=True)
class Treatment:
    """What the look gains: ``kind`` is "none" (already readable), "outline", "shadow" or
    "declined" (the creator set both outline and shadow themselves)."""

    kind: str
    outline_px: int | None = None
    stroke_color: str | None = None
    shadow_enabled: bool | None = None
    shadow_color: str | None = None
    shadow_opacity: float | None = None


def choose_treatment(
    *,
    fill: str,
    size_px: float,
    outline_px: int,
    shadow_enabled: bool | None,
    has_box: bool,
    contrast: float | None,
    outline_asked: bool,
    shadow_asked: bool,
) -> Treatment:
    """The treatment rule (see the module docstring). A caption box, an outline already at
    least as wide as the rule's, or sampled contrast at or above 7:1 (`UNTREATED_MIN_CONTRAST`)
    needs nothing."""
    if has_box or contrast is None or contrast >= UNTREATED_MIN_CONTRAST:
        return Treatment("none")
    wanted = stroke_px_for(size_px)
    if outline_px >= wanted:
        return Treatment("none")
    ink = readable_ink(fill)
    if not outline_asked:
        return Treatment("outline", outline_px=wanted, stroke_color=ink)
    if not shadow_asked:
        return Treatment("shadow", shadow_enabled=True, shadow_color=ink, shadow_opacity=0.85)
    return Treatment("declined")


# --- full-screen pictures -------------------------------------------------------------------


def sample_grey(path: str, at_s: float | None, *, timeout_s: float = 2.0) -> bytes | None:
    """``path`` at ``at_s`` (None: a still, first frame) as a GRID_W x GRID_H grey cover-fit,
    the way the phone fills the canvas with a full-screen card."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                *(() if at_s is None else ("-ss", str(max(0.0, at_s)))),
                "-i",
                path,
                "-vf",
                f"scale={GRID_W}:{GRID_H}:force_original_aspect_ratio=increase,"
                f"crop={GRID_W}:{GRID_H},format=gray",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    size = GRID_W * GRID_H
    return result.stdout if result.returncode == 0 and len(result.stdout) == size else None


@dataclass(frozen=True, slots=True)
class EdgeMaps:
    """Cumulative row sums of the weak- and strong-edge masks (dilated 5x5), for fast band
    coverage. ``weak[y][x]`` is the count of edge pixels in rows < y, columns < x."""

    weak: object
    strong: object
    width: int
    height: int


def edge_maps(pixels: bytes, width: int = GRID_W, height: int = GRID_H) -> EdgeMaps:
    import cv2
    import numpy as np

    grey = np.frombuffer(pixels, np.uint8).reshape(height, width)
    grad = cv2.morphologyEx(grey, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    grow = np.ones((5, 5), np.uint8)

    def integral(threshold: int):
        mask = cv2.dilate((grad >= threshold).astype(np.uint8), grow)
        return cv2.integral(mask)

    return EdgeMaps(integral(_WEAK_EDGE), integral(_STRONG_EDGE), width, height)


def _coverage(table, box: Sequence[float], width: int, height: int) -> float:
    x0, y0 = max(0, int(box[0] * width)), max(0, int(box[1] * height))
    x1, y1 = min(width, int(box[2] * width + 0.999)), min(height, int(box[3] * height + 0.999))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    total = table[y1][x1] - table[y0][x1] - table[y1][x0] + table[y0][x0]
    return float(total) / ((x1 - x0) * (y1 - y0))


def band_coverage(maps: Sequence[EdgeMaps], box: Sequence[float]) -> tuple[float, float]:
    """(weak, strong) edge share of ``box`` (canvas fractions, margin included), worst over the
    pictures in ``maps`` (several when one line spans more than one full-screen card)."""
    padded = (box[0], box[1] - _BAND_MARGIN, box[2], box[3] + _BAND_MARGIN)
    weak = max((_coverage(m.weak, padded, m.width, m.height) for m in maps), default=0.0)
    strong = max((_coverage(m.strong, padded, m.width, m.height) for m in maps), default=0.0)
    return weak, strong


@dataclass(frozen=True, slots=True)
class LinePlacement:
    """``action``: "keep" (already calm), "move" (to ``y_frac``) or "hide" (for the window)."""

    action: str
    y_frac: float
    weak: float
    strong: float


def place_line(
    box: Sequence[float],
    maps: Sequence[EdgeMaps],
    *,
    blocked: Sequence[Sequence[float]] = (),
) -> LinePlacement:
    """Where a caption block ``box`` (canvas fractions, as drawn) goes while ``maps``' pictures
    are up. ``blocked`` are boxes the moved block may not touch (a face during the part of the
    line outside the picture). Nearest calm band first; else the least text-dense band unless
    even that is dense, then hide."""
    from app.pipeline.phone_text_style import _overlap

    centre = (box[1] + box[3]) / 2
    half = (box[3] - box[1]) / 2
    weak, strong = band_coverage(maps, box)
    if weak <= _CALM_COVERAGE:
        return LinePlacement("keep", centre, weak, strong)
    candidates = []
    steps = int(round((_SCAN_BOTTOM - _SCAN_TOP) / 0.01))
    for index in range(steps + 1):
        y = round(_SCAN_TOP + 0.01 * index, 2)
        moved = (box[0], y - half, box[2], y + half)
        if moved[1] < 0.02 or moved[3] > 0.98:
            continue
        if any(_overlap(moved, other) for other in blocked):
            continue
        candidates.append((y, *band_coverage(maps, moved)))
    calm = [c for c in candidates if c[1] <= _CALM_COVERAGE]
    if calm:
        y, w, s = min(calm, key=lambda c: abs(c[0] - centre))
        return LinePlacement("move", y, w, s)
    if not candidates:
        return LinePlacement("hide", centre, weak, strong)
    floor = min(c[2] for c in candidates)
    if floor > _TEXT_DENSE_COVERAGE:
        return LinePlacement("hide", centre, weak, strong)
    y, w, s = min(
        (c for c in candidates if c[2] <= floor + 0.005), key=lambda c: abs(c[0] - centre)
    )
    if abs(y - centre) < 0.005:
        return LinePlacement("keep", centre, w, s)
    return LinePlacement("move", y, w, s)


def trim_cue_out_of(cue: dict, windows: Sequence[tuple[float, float]]) -> list[dict]:
    """``cue`` with ``windows`` cut out of its time: the parts before / after each window keep the
    cue's text, id and absolute word times (a word wholly inside a cut part is dropped from that
    part). Parts shorter than 0.2 s are dropped."""
    parts = [(float(cue["start_s"]), float(cue["end_s"]))]
    for w_start, w_end in windows:
        nxt = []
        for start, end in parts:
            if w_end <= start or end <= w_start:
                nxt.append((start, end))
                continue
            if start < w_start:
                nxt.append((start, w_start))
            if w_end < end:
                nxt.append((w_end, end))
        parts = nxt
    out = []
    for start, end in parts:
        if end - start < 0.2:
            continue
        piece = {**cue, "start_s": start, "end_s": end}
        words = cue.get("words")
        if isinstance(words, list):
            kept = [
                w
                for w in words
                if isinstance(w, dict)
                and float(w.get("end_s", 0)) > start
                and float(w.get("start_s", 0)) < end
            ]
            if kept and len(kept) == len(words):
                piece["words"] = words
            else:
                # The karaoke words must spell the text; a partial set would not.
                piece.pop("words", None)
        out.append(piece)
    return out
