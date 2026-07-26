"""Deterministic centerline handwriting layout shared by video renderers.

The glyph asset is generated from the bundled OFL Patrick Hand face by
``scripts/generate_handwriting_strokes.py``. It stores normalized centerline
polylines, not font outlines, so a partial frame can draw the actual path a
pen would travel instead of clipping an already-painted text block.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_ASSET_PATH = Path(__file__).resolve().parents[2] / "assets/fonts/handwriting-strokes.json"
_BASE_TRACKING_EM = 0.035
_CHARACTER_PAUSE_EM = 0.055
_SPACE_PAUSE_EM = 0.14
_MIN_STROKE_WEIGHT_EM = 0.045


@dataclass(frozen=True)
class HandwritingStroke:
    points: tuple[tuple[float, float], ...]
    start_progress: float
    end_progress: float
    line_index: int
    glyph_index: int


@dataclass(frozen=True)
class HandwritingLayout:
    strokes: tuple[HandwritingStroke, ...]
    lines: tuple[str, ...]
    line_widths_em: tuple[float, ...]
    width_em: float
    height_em: float
    line_step_em: float
    ascent_em: float
    descent_em: float
    stroke_width_em: float


@lru_cache(maxsize=1)
def handwriting_asset() -> dict[str, Any]:
    return json.loads(_ASSET_PATH.read_text(encoding="utf-8"))


def _glyph_for(char: str) -> dict[str, Any]:
    glyphs = handwriting_asset()["glyphs"]
    glyph = glyphs.get(char)
    if glyph is not None:
        return glyph
    return glyphs.get("?", {"advance": 0.5, "paths": []})


def glyph_advance_em(char: str, letter_spacing_em: float = 0.0) -> float:
    return float(_glyph_for(char)["advance"]) + _BASE_TRACKING_EM + letter_spacing_em


def measure_handwriting_line_em(line: str, letter_spacing_em: float = 0.0) -> float:
    if not line:
        return 0.0
    width = sum(glyph_advance_em(char, letter_spacing_em) for char in line)
    return max(0.0, width - (_BASE_TRACKING_EM + letter_spacing_em))


def _break_long_token(
    token: str,
    max_width_em: float,
    letter_spacing_em: float,
) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in token:
        candidate = current + char
        if current and measure_handwriting_line_em(candidate, letter_spacing_em) > max_width_em:
            pieces.append(current)
            current = char
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [""]


def wrap_handwriting_text(
    text: str,
    max_width_em: float,
    letter_spacing_em: float = 0.0,
) -> tuple[str, ...]:
    max_width_em = max(0.1, max_width_em)
    output: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line == "":
            output.append("")
            continue
        words = re.findall(r"\S+", raw_line)
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if measure_handwriting_line_em(candidate, letter_spacing_em) <= max_width_em:
                current = candidate
                continue
            if current:
                output.append(current)
                current = ""
            if measure_handwriting_line_em(word, letter_spacing_em) <= max_width_em:
                current = word
                continue
            pieces = _break_long_token(word, max_width_em, letter_spacing_em)
            output.extend(pieces[:-1])
            current = pieces[-1]
        output.append(current)
    return tuple(output or [""])


def _polyline_length(points: tuple[tuple[float, float], ...]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))


def layout_handwriting_text(
    text: str,
    *,
    max_width_em: float,
    letter_spacing_em: float = 0.0,
    line_spacing: float = 1.15,
) -> HandwritingLayout:
    asset = handwriting_asset()
    ascent = float(asset["ascent"])
    descent = float(asset["descent"])
    stroke_width = float(asset["stroke_width"])
    lines = wrap_handwriting_text(text, max_width_em, letter_spacing_em)
    widths = tuple(measure_handwriting_line_em(line, letter_spacing_em) for line in lines)
    line_step = (ascent + descent) * max(0.5, line_spacing)
    height = ascent + descent + line_step * max(0, len(lines) - 1)

    raw: list[tuple[tuple[tuple[float, float], ...], float, int, int]] = []
    cursor_weight = 0.0
    for line_index, line in enumerate(lines):
        x = 0.0
        baseline_y = ascent + line_index * line_step
        for glyph_index, char in enumerate(line):
            glyph = _glyph_for(char)
            if char.isspace():
                cursor_weight += _SPACE_PAUSE_EM
                x += glyph_advance_em(char, letter_spacing_em)
                continue
            for raw_path in glyph.get("paths", []):
                points = tuple(
                    (x + float(point[0]), baseline_y + float(point[1])) for point in raw_path
                )
                if len(points) < 2:
                    continue
                length = max(_MIN_STROKE_WEIGHT_EM, _polyline_length(points))
                raw.append((points, length, line_index, glyph_index))
                cursor_weight += length
            cursor_weight += _CHARACTER_PAUSE_EM
            x += glyph_advance_em(char, letter_spacing_em)
        if line_index < len(lines) - 1:
            cursor_weight += _SPACE_PAUSE_EM * 1.5

    total_weight = max(cursor_weight, 1e-6)
    strokes: list[HandwritingStroke] = []
    cursor_weight = 0.0
    raw_index = 0
    for line_index, line in enumerate(lines):
        for glyph_index, char in enumerate(line):
            glyph = _glyph_for(char)
            if char.isspace():
                cursor_weight += _SPACE_PAUSE_EM
                continue
            for _ in glyph.get("paths", []):
                if raw_index >= len(raw):
                    break
                points, length, raw_line_index, raw_glyph_index = raw[raw_index]
                raw_index += 1
                start = cursor_weight / total_weight
                cursor_weight += length
                end = cursor_weight / total_weight
                strokes.append(
                    HandwritingStroke(
                        points=points,
                        start_progress=start,
                        end_progress=end,
                        line_index=raw_line_index,
                        glyph_index=raw_glyph_index,
                    )
                )
            cursor_weight += _CHARACTER_PAUSE_EM
        if line_index < len(lines) - 1:
            cursor_weight += _SPACE_PAUSE_EM * 1.5

    return HandwritingLayout(
        strokes=tuple(strokes),
        lines=lines,
        line_widths_em=widths,
        width_em=max(widths, default=0.0),
        height_em=height,
        line_step_em=line_step,
        ascent_em=ascent,
        descent_em=descent,
        stroke_width_em=stroke_width,
    )


def stroke_local_progress(
    stroke: HandwritingStroke,
    reveal_progress: float,
) -> float:
    if reveal_progress <= stroke.start_progress:
        return 0.0
    if reveal_progress >= stroke.end_progress:
        return 1.0
    span = max(1e-9, stroke.end_progress - stroke.start_progress)
    return (reveal_progress - stroke.start_progress) / span


def partial_polyline(
    points: tuple[tuple[float, float], ...],
    progress: float,
) -> tuple[tuple[float, float], ...]:
    if progress <= 0.0 or len(points) < 2:
        return ()
    if progress >= 1.0:
        return points
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:], strict=False)]
    total = sum(lengths)
    if total <= 1e-9:
        return points[:2]
    target = total * progress
    walked = 0.0
    result = [points[0]]
    for index, segment_length in enumerate(lengths):
        next_walked = walked + segment_length
        if target >= next_walked:
            result.append(points[index + 1])
            walked = next_walked
            continue
        ratio = (target - walked) / max(segment_length, 1e-9)
        x0, y0 = points[index]
        x1, y1 = points[index + 1]
        result.append((x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio))
        break
    return tuple(result)
