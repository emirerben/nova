#!/usr/bin/env python3
"""Generate Nova's shared monoline handwriting glyph asset.

The source face is the bundled OFL-licensed Patrick Hand font. We rasterize
each supported glyph, reduce it to a centerline skeleton, then trace and
simplify those centerlines into deterministic polylines. Both the browser and
Skia renderer consume the resulting JSON so preview and burn use identical
letterforms and stroke order.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
FONT_PATH = ROOT / "src/apps/api/assets/fonts/PatrickHand-Regular.ttf"
API_OUTPUT = ROOT / "src/apps/api/assets/fonts/handwriting-strokes.json"
WEB_OUTPUT = ROOT / "src/apps/web/src/data/handwriting-strokes.json"

FONT_PX = 256
PADDING = 28
STROKE_WIDTH_EM = 0.095

EXTRA_CHARS = (
    "ĞğİıŞş"
    "ŐőŰű"
    "ĀāĂăĄąĆćĈĉĊċČčĎďĐđ"
    "ĒēĔĕĖėĘęĚěĜĝĠġĢģĤĥĦħ"
    "ĨĩĪīĬĭĮįĴĵĶķĹĺĻļĽľŁł"
    "ŃńŅņŇňŊŋŌōŎŏŔŕŖŗŘř"
    "ŚśŜŝŞşŠšŢţŤťŦŧŨũŪūŬŭŮů"
    "ŴŵŶŷŸŹźŻżŽž"
    "‘’“”–—…•"
)
SUPPORTED = "".join(dict.fromkeys(chr(code) for code in range(32, 256))) + EXTRA_CHARS


Point = tuple[int, int]
Edge = tuple[Point, Point]


def _edge(a: Point, b: Point) -> Edge:
    return (a, b) if a <= b else (b, a)


def _neighbors(points: set[Point], point: Point) -> list[Point]:
    y, x = point
    adjacent: list[Point] = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            candidate = (y + dy, x + dx)
            if candidate in points:
                adjacent.append(candidate)
    return adjacent


def _polyline_length(points: Iterable[Point]) -> float:
    total = 0.0
    previous: Point | None = None
    for point in points:
        if previous is not None:
            total += math.dist(previous, point)
        previous = point
    return total


def _trace_skeleton(skeleton: np.ndarray) -> list[list[Point]]:
    all_points = {(int(y), int(x)) for y, x in np.argwhere(skeleton > 0)}
    if not all_points:
        return []

    graph = {point: _neighbors(all_points, point) for point in all_points}
    unvisited = {
        _edge(point, neighbor)
        for point, neighbors in graph.items()
        for neighbor in neighbors
    }
    trails: list[list[Point]] = []

    while unvisited:
        active_points = {point for edge in unvisited for point in edge}
        endpoints = [
            point
            for point in active_points
            if sum(_edge(point, neighbor) in unvisited for neighbor in graph[point])
            == 1
        ]
        junctions = [
            point
            for point in active_points
            if sum(_edge(point, neighbor) in unvisited for neighbor in graph[point]) > 2
        ]
        start = min(
            endpoints or junctions or active_points,
            key=lambda point: (point[1], point[0]),
        )
        trail = [start]
        previous: Point | None = None
        current = start

        while True:
            candidates = [
                neighbor
                for neighbor in graph[current]
                if _edge(current, neighbor) in unvisited
            ]
            if not candidates:
                break
            if previous is None or len(candidates) == 1:
                next_point = min(candidates, key=lambda point: (point[1], point[0]))
            else:
                incoming = (current[0] - previous[0], current[1] - previous[1])

                def turn_cost(point: Point) -> tuple[float, int, int]:
                    outgoing = (point[0] - current[0], point[1] - current[1])
                    dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
                    denom = max(
                        math.hypot(*incoming) * math.hypot(*outgoing),
                        1e-6,
                    )
                    return (-dot / denom, point[1], point[0])

                next_point = min(candidates, key=turn_cost)
            unvisited.remove(_edge(current, next_point))
            previous, current = current, next_point
            trail.append(current)
            if len(trail) > 2 and len(graph[current]) != 2:
                break
        trails.append(trail)

    return trails


def _simplify(points: list[Point]) -> list[Point]:
    if len(points) <= 2:
        return points
    xy = np.array([(x, y) for y, x in points], dtype=np.float32).reshape(-1, 1, 2)
    simplified = cv2.approxPolyDP(xy, epsilon=1.65, closed=False).reshape(-1, 2)
    return [(int(round(y)), int(round(x))) for x, y in simplified]


def _glyph(font: ImageFont.FreeTypeFont, char: str) -> dict[str, object]:
    advance = float(font.getlength(char)) / FONT_PX
    bbox = font.getbbox(char, anchor="ls")
    if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return {"advance": round(advance, 5), "paths": []}

    left, top, right, bottom = bbox
    width = right - left + PADDING * 2
    height = bottom - top + PADDING * 2
    baseline_origin = (PADDING - left, PADDING - top)
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    draw.text(
        baseline_origin,
        char,
        font=font,
        fill=255,
        anchor="ls",
        stroke_width=0,
    )
    binary = (np.asarray(image, dtype=np.uint8) > 80).astype(np.uint8) * 255
    skeleton = cv2.ximgproc.thinning(binary)

    path_records: list[tuple[bool, float, list[list[float]]]] = []
    components, labels, stats, _ = cv2.connectedComponentsWithStats(
        skeleton,
        connectivity=8,
    )
    for index in range(1, components):
        component = (labels == index).astype(np.uint8) * 255
        area = int(stats[index, cv2.CC_STAT_AREA])
        trails = _trace_skeleton(component)
        if not trails and area > 0:
            y, x = np.argwhere(component > 0)[0]
            trails = [[(int(y), int(x)), (int(y), int(x) + 1)]]
        for trail in trails:
            simplified = _simplify(trail)
            if len(simplified) == 1:
                y, x = simplified[0]
                simplified.append(
                    (y, x + max(1, int(FONT_PX * STROKE_WIDTH_EM * 0.32)))
                )
            length = _polyline_length(simplified) / FONT_PX
            if length < 0.008:
                continue
            normalized = [
                [
                    round((x - baseline_origin[0]) / FONT_PX, 5),
                    round((y - baseline_origin[1]) / FONT_PX, 5),
                ]
                for y, x in simplified
            ]
            is_mark = length < 0.13
            path_records.append((is_mark, length, normalized))

    # Long structural strokes first, then dots/crossbars. Within each group,
    # preserve a deterministic left-to-right writing order.
    path_records.sort(
        key=lambda record: (
            record[0],
            min(point[0] for point in record[2]),
            min(point[1] for point in record[2]),
            -record[1],
        )
    )
    return {
        "advance": round(advance, 5),
        "paths": [record[2] for record in path_records],
    }


def main() -> None:
    font = ImageFont.truetype(str(FONT_PATH), FONT_PX)
    ascent, descent = font.getmetrics()
    glyphs = {char: _glyph(font, char) for char in SUPPORTED}
    data = {
        "version": 1,
        "source": "Patrick Hand (OFL-1.1), centerline-derived for Nova",
        "units_per_em": 1,
        "ascent": round(ascent / FONT_PX, 5),
        "descent": round(descent / FONT_PX, 5),
        "stroke_width": STROKE_WIDTH_EM,
        "glyphs": glyphs,
    }
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    API_OUTPUT.write_text(encoded + "\n", encoding="utf-8")
    WEB_OUTPUT.write_text(encoded + "\n", encoding="utf-8")
    print(f"Wrote {len(glyphs)} glyphs ({len(encoded):,} bytes) to:")
    print(f"  {API_OUTPUT.relative_to(ROOT)}")
    print(f"  {WEB_OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
