"""Masonry-collage montage compositor for plan-item generative renders.

Builds a white-canvas collage of rounded video tiles directly with FFmpeg. The
source videos are never buffered in Python; Pillow is only used to create small
alpha-mask PNGs for rounded corners.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from itertools import cycle, islice
from typing import Any

import structlog

from app.config import settings
from app.schemas.montage_preset import (
    MASONRY_MONTAGE_PRESET,
    POLAROID_WALL_MONTAGE_PRESET,
    MontagePreset,
    coerce_montage_preset,
)

log = structlog.get_logger()

MASONRY_MAX_DURATION_S = 15.0
MASONRY_MAX_TILES = 18
MASONRY_TIMEOUT_S = 900
MASONRY_TILE_RADIUS_PX = 34
_PLACEMENT_SAMPLE_COUNT = 7
_PLACEMENT_MARGIN_PX = 42
_PLACEMENT_FRAME_MARGIN_PX = 36
_PLACEMENT_MIN_WIDTH_FRAC = 0.20
_PLACEMENT_MIN_HEIGHT_FRAC = 0.055
_POLAROID_FRAME_PX = 24
_POLAROID_BOTTOM_FRAME_PX = 62
_POLAROID_SHADOW_PX = 16
_POLAROID_LAYOUT_SPACING_PX = 28
_POLAROID_SEARCH_STEP_PX = 56
_POLAROID_MAX_TOP_Y = 1580


@dataclass(frozen=True)
class MasonryTile:
    """One rounded video tile on the scrolling board."""

    input_index: int
    clip_id: str
    local_path: str
    x: int
    y: int
    width: int
    height: int
    mask_path: str
    is_image: bool = False
    content_x: int = 0
    content_y: int = 0
    content_width: int = 0
    content_height: int = 0
    frame_path: str | None = None
    z_index: int = 0
    rotation_deg: float = 0.0


@dataclass(frozen=True)
class MasonryPresetConfig:
    """Visual configuration for a collage-style montage preset."""

    preset: MontagePreset
    layout: tuple[tuple[int, int, int, int], ...]
    source: str
    tile_radius_px: int = MASONRY_TILE_RADIUS_PX
    content_radius_px: int = MASONRY_TILE_RADIUS_PX
    frame_px: int = 0
    bottom_frame_px: int = 0
    shadow_px: int = 0
    featured_tiles: tuple[tuple[int, float, int], ...] = ()
    tile_variations: tuple[tuple[int, float, int, int, float], ...] = ()
    layout_spacing_px: int = 0
    placement_margin_px: int = _PLACEMENT_MARGIN_PX


_MASONRY_LAYOUT: tuple[tuple[int, int, int, int], ...] = (
    (34, 46, 270, 480),
    (334, 28, 420, 250),
    (784, 64, 285, 500),
    (1098, 24, 440, 264),
    (1568, 74, 265, 472),
    (26, 568, 420, 244),
    (474, 330, 280, 498),
    (784, 600, 410, 250),
    (1224, 330, 292, 520),
    (1548, 600, 430, 248),
    (48, 850, 270, 480),
    (348, 872, 430, 260),
    (808, 892, 270, 480),
    (1110, 904, 420, 250),
    (1560, 886, 285, 506),
    (28, 1372, 430, 254),
    (488, 1412, 284, 474),
    (804, 1414, 424, 254),
)


def _scale_rect(rect: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int]:
    if scale == 1.0:
        return rect
    x, y, width, height = rect
    scaled_w = int(round(width * scale))
    scaled_h = int(round(height * scale))
    return (
        int(round(x - (scaled_w - width) / 2)),
        int(round(y - (scaled_h - height) / 2)),
        scaled_w,
        scaled_h,
    )


def _preset_config(preset: str | None = None) -> MasonryPresetConfig:
    resolved = coerce_montage_preset(preset)
    if resolved == POLAROID_WALL_MONTAGE_PRESET:
        return MasonryPresetConfig(
            preset=POLAROID_WALL_MONTAGE_PRESET,
            layout=_MASONRY_LAYOUT,
            source="polaroid_wall_whitespace",
            tile_radius_px=26,
            content_radius_px=20,
            frame_px=_POLAROID_FRAME_PX,
            bottom_frame_px=_POLAROID_BOTTOM_FRAME_PX,
            shadow_px=_POLAROID_SHADOW_PX,
            featured_tiles=(
                (3, 1.40, 20),
                (6, 1.65, 40),
                (11, 1.35, 18),
                (15, 1.35, 14),
            ),
            tile_variations=(
                (0, 0.92, -18, 12, -2.6),
                (1, 1.08, 8, -16, 1.7),
                (2, 0.96, 20, 8, -1.4),
                (3, 1.00, -10, -8, 2.1),
                (4, 1.10, 22, 10, -3.0),
                (5, 0.94, -14, 18, 1.2),
                (6, 1.00, 6, -6, -2.4),
                (7, 1.07, 18, 12, 2.8),
                (8, 0.91, -10, 24, -1.8),
                (9, 1.12, 28, -6, 1.5),
                (10, 0.88, -20, -8, 3.2),
                (11, 1.00, 12, 10, -1.9),
                (12, 1.05, -8, 18, 2.4),
                (13, 0.95, 16, -12, -2.2),
                (14, 1.08, 24, 6, 1.1),
                (15, 1.00, -12, 16, 2.6),
                (16, 0.90, 8, -14, -1.6),
                (17, 1.12, 30, 12, 2.0),
            ),
            layout_spacing_px=_POLAROID_LAYOUT_SPACING_PX,
            placement_margin_px=72,
        )
    return MasonryPresetConfig(
        preset=MASONRY_MONTAGE_PRESET,
        layout=_MASONRY_LAYOUT,
        source="masonry_whitespace",
    )


def _layout_for_config(config: MasonryPresetConfig) -> list[tuple[int, int, int, int]]:
    layout = list(config.layout)
    for tile_index, scale, _z_index in config.featured_tiles:
        if 0 <= tile_index < len(layout):
            layout[tile_index] = _scale_rect(layout[tile_index], scale)
    for tile_index, scale, dx, dy, _rotation_deg in config.tile_variations:
        if 0 <= tile_index < len(layout):
            x, y, width, height = _scale_rect(layout[tile_index], scale)
            layout[tile_index] = (x + dx, y + dy, width, height)
    if config.layout_spacing_px > 0:
        layout = _relax_layout_around_featured_tiles(layout, config)
    return layout


def _z_index_for_config(config: MasonryPresetConfig, tile_index: int) -> int:
    for featured_index, _scale, z_index in config.featured_tiles:
        if featured_index == tile_index:
            return z_index
    return 0


def _rotation_for_config(config: MasonryPresetConfig, tile_index: int) -> float:
    for varied_index, _scale, _dx, _dy, rotation_deg in config.tile_variations:
        if varied_index == tile_index:
            return rotation_deg
    return 0.0


def _rotated_bounds(width: int, height: int, rotation_deg: float) -> tuple[int, int]:
    if abs(rotation_deg) < 0.001:
        return width, height
    radians = math.radians(abs(rotation_deg))
    rotated_w = int(math.ceil(width * math.cos(radians) + height * math.sin(radians)))
    rotated_h = int(math.ceil(width * math.sin(radians) + height * math.cos(radians)))
    return rotated_w, rotated_h


def _rects_overlap(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    spacing_px: int = 0,
) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    margin = spacing_px / 2.0
    return not (
        ax + aw + margin <= bx - margin
        or bx + bw + margin <= ax - margin
        or ay + ah + margin <= by - margin
        or by + bh + margin <= ay - margin
    )


def _collides_with_any(
    rect: tuple[int, int, int, int],
    placed: dict[int, tuple[int, int, int, int]],
    spacing_px: int,
) -> bool:
    return any(_rects_overlap(rect, other, spacing_px) for other in placed.values())


def _candidate_positions_for_rect(
    target: tuple[int, int, int, int],
    placed: dict[int, tuple[int, int, int, int]],
    spacing_px: int,
):
    tx, ty, width, height = target
    yield (max(24, tx), max(24, ty), width, height)

    for bx, by, bw, bh in placed.values():
        xs = (
            tx,
            bx,
            bx + bw - width,
            bx - spacing_px - width,
            bx + bw + spacing_px,
        )
        ys = (
            ty,
            by,
            by + bh - height,
            by - spacing_px - height,
            by + bh + spacing_px,
        )
        for x in xs:
            for y in (by - spacing_px - height, by + bh + spacing_px, ty):
                yield (round(max(24, x)), round(max(24, y)), width, height)
        for y in ys:
            for x in (bx - spacing_px - width, bx + bw + spacing_px, tx):
                yield (round(max(24, x)), round(max(24, y)), width, height)

    for radius in range(1, 18):
        for dx in range(-radius, radius + 1):
            for dy in (-radius, radius):
                yield (
                    max(24, tx + dx * _POLAROID_SEARCH_STEP_PX),
                    max(24, ty + dy * _POLAROID_SEARCH_STEP_PX),
                    width,
                    height,
                )
        for dy in range(-radius + 1, radius):
            for dx in (-radius, radius):
                yield (
                    max(24, tx + dx * _POLAROID_SEARCH_STEP_PX),
                    max(24, ty + dy * _POLAROID_SEARCH_STEP_PX),
                    width,
                    height,
                )


def _relax_layout_around_featured_tiles(
    layout: list[tuple[int, int, int, int]],
    config: MasonryPresetConfig,
) -> list[tuple[int, int, int, int]]:
    """Pack cards around enlarged Polaroids while preserving nearby positions.

    Featured cards are placed first, in visual priority order. Each remaining card
    searches for the closest non-colliding position around its original masonry
    target, so a growing hero card pushes neighbors into the nearest open lanes
    instead of simply drawing over them.
    """
    featured_z = {tile_index: z_index for tile_index, _scale, z_index in config.featured_tiles}
    order = sorted(
        range(len(layout)),
        key=lambda idx: (
            0 if idx in featured_z else 1,
            -featured_z.get(idx, 0),
            layout[idx][1],
            layout[idx][0],
        ),
    )
    placed: dict[int, tuple[int, int, int, int]] = {}
    final: list[tuple[int, int, int, int] | None] = [None] * len(layout)

    for tile_index in order:
        target = layout[tile_index]
        best: tuple[int, int, int, int] | None = None
        best_cost = float("inf")
        seen: set[tuple[int, int, int, int]] = set()
        for candidate in _candidate_positions_for_rect(
            target,
            placed,
            config.layout_spacing_px,
        ):
            if candidate in seen:
                continue
            seen.add(candidate)
            x, y, width, height = candidate
            if y > _POLAROID_MAX_TOP_Y:
                continue
            if _collides_with_any(candidate, placed, config.layout_spacing_px):
                continue
            target_x, target_y, _target_w, _target_h = target
            dx = x - target_x
            dy = y - target_y
            cost = dx * dx + dy * dy * 1.4 + x * 0.03
            if cost < best_cost:
                best = candidate
                best_cost = cost

        if best is None:
            right = max((x + width for x, _y, width, _h in placed.values()), default=24)
            right += config.layout_spacing_px
            for row_y in (max(24, target[1]), 24, 568, 850, 1372):
                fallback = (right, row_y, target[2], target[3])
                if not _collides_with_any(fallback, placed, config.layout_spacing_px):
                    best = fallback
                    break
        if best is None:
            right = max((x + width for x, _y, width, _h in placed.values()), default=24)
            best = (right + config.layout_spacing_px, max(24, target[1]), target[2], target[3])

        placed[tile_index] = best
        final[tile_index] = best

    resolved: list[tuple[int, int, int, int]] = []
    for rect in final:
        if rect is None:
            raise RuntimeError("polaroid layout solver left an unplaced tile")
        resolved.append(rect)
    return resolved


def clamp_masonry_duration(duration_s: float) -> float:
    """Clamp masonry renders to the short reference-style window."""
    try:
        duration = float(duration_s)
    except (TypeError, ValueError, OverflowError):
        duration = MASONRY_MAX_DURATION_S
    if duration <= 0:
        duration = MASONRY_MAX_DURATION_S
    return round(max(0.1, min(duration, MASONRY_MAX_DURATION_S)), 3)


def masonry_board_width_for_preset(preset: str | None = None) -> int:
    config = _preset_config(preset)
    layout = _layout_for_config(config)
    output_w = int(settings.output_width)
    return max((x + w for x, _y, w, _h in layout), default=output_w) + 34


def _masonry_board_width() -> int:
    return masonry_board_width_for_preset(MASONRY_MONTAGE_PRESET)


def _masonry_pan_px(*, board_width: int | None = None) -> int:
    output_w = int(settings.output_width)
    return max(0, int(board_width or _masonry_board_width()) - output_w)


def _masonry_motion_metadata(
    *,
    duration_s: float,
    board_width: int | None = None,
    pocket: tuple[float, float, float, float] | None = None,
) -> dict:
    output_w = int(settings.output_width)
    resolved_board_width = int(board_width or _masonry_board_width())
    metadata = {
        "mode": "masonry_pan_x",
        "duration_s": clamp_masonry_duration(duration_s),
        "pan_px": _masonry_pan_px(board_width=resolved_board_width),
        "board_width_px": resolved_board_width,
        "frame_width_px": output_w,
    }
    if pocket is not None:
        left, top, right, bottom = pocket
        metadata["pocket_left_px"] = round(left, 3)
        metadata["pocket_top_px"] = round(top, 3)
        metadata["pocket_right_px"] = round(right, 3)
        metadata["pocket_bottom_px"] = round(bottom, 3)
    return metadata


def _masonry_candidate_from_rect(
    *,
    left: float,
    top: float,
    width: float,
    height: float,
    duration_s: float,
    max_width_frac: float | None = None,
    rotation_deg: float = 0.0,
    confidence: float = 0.75,
    source: str = "masonry_whitespace",
    board_width: int | None = None,
) -> dict:
    output_w = int(settings.output_width)
    output_h = int(settings.output_height)
    if max_width_frac is None:
        if rotation_deg:
            max_width_frac = min(0.82, max(0.36, (height / output_w) * 0.86))
        else:
            max_width_frac = max(_PLACEMENT_MIN_WIDTH_FRAC, min(0.9, (width / output_w) * 0.92))
    return {
        "source": source,
        "x_frac": round((left + width / 2.0) / output_w, 4),
        "y_frac": round((top + height / 2.0) / output_h, 4),
        "max_width_frac": round(max(_PLACEMENT_MIN_WIDTH_FRAC, min(0.9, max_width_frac)), 4),
        "rotation_deg": round(float(rotation_deg), 3),
        "confidence": round(max(0.0, min(0.98, confidence)), 3),
        "masonry_motion": _masonry_motion_metadata(
            duration_s=duration_s,
            board_width=board_width,
            pocket=(left, top, left + width, top + height),
        ),
    }


def masonry_text_placement_candidates(
    *,
    duration_s: float,
    reveal_window_s: float = 4.0,
    max_candidates: int = 3,
    preset: str | None = None,
) -> list[dict]:
    """Find stable whitespace regions over the masonry reveal window.

    Finds empty rectangles in board coordinates, then scores how long each pocket
    remains visible while the board pans. Tiles and text are projected through the
    same pan, so a zero-overlap board pocket stays clear in the final render.
    """
    config = _preset_config(preset)
    layout = _layout_for_config(config)
    output_w = int(settings.output_width)
    output_h = int(settings.output_height)
    board_width = masonry_board_width_for_preset(config.preset)
    pan_px = _masonry_pan_px(board_width=board_width)
    duration = clamp_masonry_duration(duration_s)
    wanted = max(1, int(max_candidates))

    window = max(0.1, min(float(reveal_window_s), duration))
    sample_count = max(2, _PLACEMENT_SAMPLE_COUNT)
    sample_times = [window * i / (sample_count - 1) for i in range(sample_count)]

    stable_obstacles = []
    for index, (x, y, width, height) in enumerate(layout):
        rotated_width, rotated_height = _rotated_bounds(
            width,
            height,
            _rotation_for_config(config, index),
        )
        visual_left = x - (rotated_width - width) / 2.0
        visual_top = y - (rotated_height - height) / 2.0
        stable_obstacles.append(
            (
                visual_left - config.placement_margin_px,
                visual_top - config.placement_margin_px,
                visual_left + rotated_width + config.placement_margin_px,
                visual_top + rotated_height + config.placement_margin_px,
            )
        )
    empty_rects = _largest_empty_masonry_rects(
        stable_obstacles,
        output_w=output_w,
        output_h=output_h,
    )

    ranked: list[tuple[float, float, float, tuple[float, float, float, float]]] = []
    for area_score, rect in empty_rects:
        left, _top, right, _bottom = rect
        width = max(1.0, right - left)
        visible_ratios = []
        for t in sample_times:
            progress = min(1.0, max(0.0, t / duration))
            scroll = pan_px * progress
            visible_width = max(
                0.0,
                min(right - scroll, float(output_w)) - max(left - scroll, 0.0),
            )
            visible_ratios.append(visible_width / width)
        reveal_visibility = sum(visible_ratios) / len(visible_ratios)
        anchor_scroll = pan_px * (window / 2.0) / duration
        anchor_visible_width = max(
            0.0,
            min(right - anchor_scroll, output_w - _PLACEMENT_FRAME_MARGIN_PX)
            - max(left - anchor_scroll, _PLACEMENT_FRAME_MARGIN_PX),
        )
        if anchor_visible_width / width < 0.98:
            continue
        center_x = (left + right) / 2.0 / output_w
        spatial_score = abs(center_x - 0.5)
        ranked.append((reveal_visibility, area_score, spatial_score, rect))

    ranked.sort(
        key=lambda item: (
            -item[0],
            -item[1],
            -item[2],
            item[3][1],
            item[3][0],
        )
    )

    candidates: list[dict] = []
    selected_rects: list[tuple[float, float, float, float]] = []
    for reveal_visibility, area_score, _spatial_score, (left, top, right, bottom) in ranked:
        rect = (left, top, right, bottom)
        if any(_ltrb_rects_overlap(rect, selected) for selected in selected_rects):
            continue
        width = right - left
        height = bottom - top
        area_ratio = (width * height) / max(1.0, float(output_w * output_h))
        rotation_deg = _rotation_for_empty_pocket(width=width, height=height)
        candidate = _masonry_candidate_from_rect(
            left=left,
            top=top,
            width=width,
            height=height,
            duration_s=duration,
            rotation_deg=rotation_deg,
            confidence=max(
                0.35,
                min(0.98, 0.42 + reveal_visibility * 0.38 + area_ratio * 2.2),
            ),
            source=config.source,
            board_width=board_width,
        )
        candidates.append(candidate)
        selected_rects.append(rect)
        if len(candidates) >= wanted:
            break

    return candidates


def _rotation_for_empty_pocket(*, width: float, height: float) -> float:
    """Rotate only when the discovered pocket is genuinely portrait-shaped."""
    if width <= 0 or height <= 0:
        return 0.0
    if height >= width * 1.75 and height >= int(settings.output_height) * 0.18:
        return 90.0
    return 0.0


def _largest_empty_masonry_rects(
    obstacles: list[tuple[float, float, float, float]],
    *,
    output_w: int,
    output_h: int,
    safe_left: float | None = None,
    safe_right: float | None = None,
) -> list[tuple[float, tuple[float, float, float, float]]]:
    """Return all maximal empty rectangles inside the reveal-stable corridor."""
    resolved_safe_left = float(_PLACEMENT_FRAME_MARGIN_PX if safe_left is None else safe_left)
    safe_top = float(_PLACEMENT_FRAME_MARGIN_PX)
    resolved_safe_right = float(
        output_w - _PLACEMENT_FRAME_MARGIN_PX if safe_right is None else safe_right
    )
    safe_bottom = float(output_h - _PLACEMENT_FRAME_MARGIN_PX)
    if resolved_safe_right <= resolved_safe_left or safe_bottom <= safe_top:
        return []

    clipped: list[tuple[float, float, float, float]] = []
    for left, top, right, bottom in obstacles:
        clipped_left = max(resolved_safe_left, min(resolved_safe_right, left))
        clipped_top = max(safe_top, min(safe_bottom, top))
        clipped_right = max(resolved_safe_left, min(resolved_safe_right, right))
        clipped_bottom = max(safe_top, min(safe_bottom, bottom))
        if clipped_right > clipped_left and clipped_bottom > clipped_top:
            clipped.append((clipped_left, clipped_top, clipped_right, clipped_bottom))

    x_edges = sorted(
        {
            resolved_safe_left,
            resolved_safe_right,
            *(v for rect in clipped for v in (rect[0], rect[2])),
        }
    )
    y_edges = sorted({safe_top, safe_bottom, *(v for rect in clipped for v in (rect[1], rect[3]))})
    if len(x_edges) < 2 or len(y_edges) < 2:
        return []

    cols = len(x_edges) - 1
    rows = len(y_edges) - 1
    free = [[True for _ in range(cols)] for _ in range(rows)]
    for left, top, right, bottom in clipped:
        for y_idx in range(rows):
            if y_edges[y_idx] >= bottom or y_edges[y_idx + 1] <= top:
                continue
            for x_idx in range(cols):
                if x_edges[x_idx] >= right or x_edges[x_idx + 1] <= left:
                    continue
                free[y_idx][x_idx] = False

    x_prefix = [0.0]
    for x_idx in range(cols):
        x_prefix.append(x_prefix[-1] + x_edges[x_idx + 1] - x_edges[x_idx])

    min_width = output_w * _PLACEMENT_MIN_WIDTH_FRAC
    min_height = output_h * _PLACEMENT_MIN_HEIGHT_FRAC
    heights = [0.0 for _ in range(cols)]
    scored: list[tuple[float, tuple[float, float, float, float]]] = []

    for y_idx in range(rows):
        row_h = y_edges[y_idx + 1] - y_edges[y_idx]
        for x_idx in range(cols):
            heights[x_idx] = heights[x_idx] + row_h if free[y_idx][x_idx] else 0.0

        stack: list[int] = []
        for scan_idx in range(cols + 1):
            current_h = heights[scan_idx] if scan_idx < cols else 0.0
            while stack and current_h < heights[stack[-1]]:
                height = heights[stack.pop()]
                left_idx = stack[-1] + 1 if stack else 0
                right_idx = scan_idx
                width = x_prefix[right_idx] - x_prefix[left_idx]
                if width >= min_width and height >= min_height:
                    bottom = y_edges[y_idx + 1]
                    top = bottom - height
                    rect = (x_edges[left_idx], top, x_edges[right_idx], bottom)
                    area = width * height
                    scored.append((area / (output_w * output_h), rect))
            stack.append(scan_idx)

    unique = {rect: score for score, rect in scored}
    return sorted(
        ((score, rect) for rect, score in unique.items()),
        key=lambda item: (-item[0], item[1][1], item[1][0]),
    )


def _ltrb_rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    inter_w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    inter_h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return inter_w > 0 and inter_h > 0


def _write_mask(path: str, width: int, height: int, radius: int = MASONRY_TILE_RADIUS_PX) -> None:
    """Create a grayscale rounded-rectangle alpha mask."""
    if os.path.exists(path):
        return
    from PIL import Image, ImageDraw  # noqa: PLC0415

    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    image.save(path)


def _write_polaroid_frame(
    path: str,
    *,
    width: int,
    height: int,
    radius: int,
    shadow_px: int,
) -> None:
    """Create one transparent PNG containing the Polaroid matte and soft shadow."""
    if os.path.exists(path):
        return
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (shadow_px // 2, shadow_px // 2, width - 1, height - 1),
        radius=radius,
        fill=(0, 0, 0, 44),
    )
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(1, shadow_px // 2))))

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, width - shadow_px - 1, height - shadow_px - 1),
        radius=radius,
        fill=(255, 255, 255, 255),
    )
    image.save(path)


def _normalize_image_for_ffmpeg(input_path: str, output_path: str) -> str:
    """Decode a still image with Pillow and write an FFmpeg-friendly PNG."""
    if os.path.exists(output_path):
        return output_path
    try:
        import pillow_heif  # type: ignore[import]  # noqa: PLC0415

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    from PIL import Image, ImageOps  # noqa: PLC0415

    try:
        with Image.open(input_path) as image:
            normalized = ImageOps.exif_transpose(image)
            if normalized.mode not in {"RGB", "RGBA"}:
                normalized = normalized.convert("RGB")
            normalized.save(output_path, format="PNG", optimize=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"masonry image normalization failed for {os.path.basename(input_path)}: {exc}"
        ) from exc
    return output_path


def build_masonry_tiles(
    *,
    steps: list,
    clip_id_to_local: dict[str, str],
    mask_dir: str,
    max_tiles: int = MASONRY_MAX_TILES,
    normalize_images: bool = False,
    preset: str | None = None,
) -> list[MasonryTile]:
    """Return deterministic tile specs, cycling uploaded clips when needed."""
    config = _preset_config(preset)
    layout = _layout_for_config(config)
    ordered_clip_ids = [str(getattr(step, "clip_id", "") or "") for step in steps]
    ordered_clip_ids = [cid for cid in ordered_clip_ids if cid in clip_id_to_local]
    if not ordered_clip_ids:
        ordered_clip_ids = [cid for cid in clip_id_to_local if clip_id_to_local.get(cid)]
    if not ordered_clip_ids:
        raise ValueError("masonry montage requires at least one local clip")

    os.makedirs(mask_dir, exist_ok=True)
    image_dir = os.path.join(os.path.dirname(mask_dir), "masonry_images")
    if normalize_images:
        os.makedirs(image_dir, exist_ok=True)
    frame_dir = os.path.join(os.path.dirname(mask_dir), "masonry_frames")
    if config.frame_px > 0:
        os.makedirs(frame_dir, exist_ok=True)
    count = min(max_tiles, len(layout))
    cycled_ids = list(islice(cycle(ordered_clip_ids), count))
    tiles: list[MasonryTile] = []
    from app.pipeline.image_clip import is_image_file  # noqa: PLC0415

    for idx, (clip_id, (x, y, width, height)) in enumerate(zip(cycled_ids, layout)):
        local_path = clip_id_to_local[clip_id]
        is_image = is_image_file(local_path)
        if normalize_images and is_image:
            local_path = _normalize_image_for_ffmpeg(
                local_path,
                os.path.join(image_dir, f"tile_{idx}.png"),
            )
        content_x = config.frame_px
        content_y = config.frame_px
        content_width = max(1, width - config.shadow_px - config.frame_px * 2)
        content_height = max(
            1,
            height - config.shadow_px - config.frame_px - config.bottom_frame_px,
        )
        mask_path = os.path.join(mask_dir, f"mask_{content_width}x{content_height}.png")
        _write_mask(mask_path, content_width, content_height, radius=config.content_radius_px)
        frame_path = None
        if config.frame_px > 0:
            frame_path = os.path.join(frame_dir, f"frame_{idx}_{width}x{height}.png")
            _write_polaroid_frame(
                frame_path,
                width=width,
                height=height,
                radius=config.tile_radius_px,
                shadow_px=config.shadow_px,
            )
        tiles.append(
            MasonryTile(
                input_index=idx + 1,
                clip_id=clip_id,
                local_path=local_path,
                x=x,
                y=y,
                width=width,
                height=height,
                mask_path=mask_path,
                is_image=is_image,
                content_x=content_x,
                content_y=content_y,
                content_width=content_width,
                content_height=content_height,
                frame_path=frame_path,
                z_index=_z_index_for_config(config, idx),
                rotation_deg=_rotation_for_config(config, idx),
            )
        )
    return tiles


def build_masonry_command(
    *,
    tiles: list[MasonryTile],
    output_path: str,
    duration_s: float,
    board_width: int,
    audio_source_path: str | None = None,
) -> list[str]:
    """Build the FFmpeg command for the masonry collage final encode."""
    if not tiles:
        raise ValueError("masonry montage requires at least one tile")
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    duration = clamp_masonry_duration(duration_s)
    output_w = int(settings.output_width)
    output_h = int(settings.output_height)
    output_fps = int(settings.output_fps)
    pan_px = max(0, board_width - output_w)

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{duration:.3f}",
        "-i",
        f"color=c=white:s={output_w}x{output_h}:r={output_fps}",
    ]
    for tile in tiles:
        if tile.is_image:
            cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", tile.local_path])
        else:
            cmd.extend(["-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", tile.local_path])
    for tile in tiles:
        cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", tile.mask_path])

    next_input_index = 1 + len(tiles) * 2
    frame_input_indices: dict[int, int] = {}
    for idx, tile in enumerate(tiles):
        if not tile.frame_path:
            continue
        frame_input_indices[idx] = next_input_index
        next_input_index += 1
        cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", tile.frame_path])

    audio_input_index = next_input_index
    if audio_source_path:
        cmd.extend(["-t", f"{duration:.3f}", "-i", audio_source_path])
    else:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        )

    filters: list[str] = []
    for idx, tile in enumerate(tiles):
        mask_index = 1 + len(tiles) + idx
        filters.append(
            f"[{tile.input_index}:v]"
            "format=rgba,"
            f"scale={tile.content_width}:{tile.content_height}:force_original_aspect_ratio=increase,"
            f"crop={tile.content_width}:{tile.content_height},fps={output_fps},"
            "setpts=PTS-STARTPTS,setsar=1,format=rgba"
            f"[tile{idx}raw]"
        )
        filters.append(f"[{mask_index}:v]format=gray[mask{idx}]")
        filters.append(f"[tile{idx}raw][mask{idx}]alphamerge[tile{idx}]")
        frame_input_index = frame_input_indices.get(idx)
        if frame_input_index is not None:
            filters.append(f"[{frame_input_index}:v]format=rgba[frame{idx}]")
            filters.append(
                f"[frame{idx}][tile{idx}]overlay=x={tile.content_x}:"
                f"y={tile.content_y}:eval=init:shortest=1,format=rgba[card{idx}]"
            )
            if abs(tile.rotation_deg) >= 0.001:
                radians = math.radians(tile.rotation_deg)
                filters.append(
                    f"[card{idx}]rotate={radians:.6f}:ow=rotw({radians:.6f}):"
                    f"oh=roth({radians:.6f}):c=none:bilinear=1,format=rgba"
                    f"[card{idx}rot]"
                )

    previous = "[0:v]"
    chain_index = 0
    draw_order = sorted(range(len(tiles)), key=lambda i: (tiles[i].z_index, i))
    for order_idx, idx in enumerate(draw_order):
        tile = tiles[idx]
        # Escape the comma in min(t\,N); otherwise FFmpeg parses it as the next
        # filter option inside the overlay expression.
        x_expr = f"{tile.x}-min(t\\,{duration:.3f})/{duration:.3f}*{pan_px}"
        if tile.frame_path:
            card_label = f"[card{idx}rot]" if abs(tile.rotation_deg) >= 0.001 else f"[card{idx}]"
            rotated_w, rotated_h = _rotated_bounds(tile.width, tile.height, tile.rotation_deg)
            x_expr = (
                f"{tile.x - round((rotated_w - tile.width) / 2)}"
                f"-min(t\\,{duration:.3f})/{duration:.3f}*{pan_px}"
            )
            out = "[outv]" if order_idx == len(draw_order) - 1 else f"[base{chain_index}]"
            if out != "[outv]":
                chain_index += 1
            filters.append(
                f"{previous}{card_label}overlay=x={x_expr}:"
                f"y={tile.y - round((rotated_h - tile.height) / 2)}:"
                f"eval=frame:shortest=1{out}"
            )
            previous = out
            continue

        out = "[outv]" if order_idx == len(draw_order) - 1 else f"[base{chain_index}]"
        if out != "[outv]":
            chain_index += 1
        filters.append(
            f"{previous}[tile{idx}]overlay=x={x_expr}:y={tile.y}:eval=frame:shortest=1{out}"
        )
        previous = out

    cmd.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-map",
            f"{audio_input_index}:a:0?",
            "-shortest",
            *_encoding_args(output_path, preset="fast"),
        ]
    )
    return cmd


def build_masonry_text_burn_command(
    *,
    input_path: str,
    sequences: list[dict[str, Any]],
    output_path: str,
    duration_s: float,
    board_width: int | None = None,
) -> list[str]:
    """Build an FFmpeg command that burns full-canvas text streams with board pan."""
    if not sequences:
        raise ValueError("masonry text burn requires at least one text sequence")
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    duration = clamp_masonry_duration(duration_s)
    pan_px = _masonry_pan_px(board_width=board_width)
    cmd: list[str] = ["ffmpeg", "-y", "-i", input_path]

    for seq in sequences:
        if seq.get("is_animated"):
            cmd.extend(
                [
                    "-framerate",
                    str(seq.get("fps", int(settings.output_fps))),
                    "-start_number",
                    "0",
                    "-i",
                    str(seq["pattern"]),
                ]
            )
        else:
            cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(seq["first_frame"])])

    filters = ["[0:v]null[base]"]
    previous = "base"
    x_expr = f"-min(t\\,{duration:.3f})/{duration:.3f}*{pan_px}"
    for idx, seq in enumerate(sequences):
        src = f"{idx + 1}:v"
        start = float(seq.get("start_s", 0.0))
        end = float(seq.get("end_s", duration))
        layer = f"text{idx}"
        if seq.get("is_animated"):
            filters.append(f"[{src}]format=rgba,setpts=PTS+{start:.4f}/TB[{layer}]")
        else:
            filters.append(f"[{src}]format=rgba[{layer}]")
        out = f"v{idx}"
        filters.append(
            f"[{previous}][{layer}]overlay=x={x_expr}:y=0:eval=frame"
            f":enable='between(t,{start:.4f},{end:.4f})'"
            f":eof_action=pass[{out}]"
        )
        previous = out

    cmd.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{previous}]",
            "-map",
            "0:a?",
            *_encoding_args(output_path, preset="fast"),
        ]
    )
    return cmd


def burn_masonry_text_overlays(
    input_path: str,
    overlays: list[dict],
    output_path: str,
    tmpdir: str,
    *,
    duration_s: float,
    board_width: int | None = None,
) -> None:
    """Burn text onto a masonry render while moving it with the tile board."""
    if not overlays:
        shutil.copy2(input_path, output_path)
        return

    from app.pipeline.text_overlay_skia import render_text_overlay_sequences  # noqa: PLC0415

    sequences, work_dir = render_text_overlay_sequences(
        overlays,
        tmpdir,
        work_dir_name="masonry_text_burn",
    )
    try:
        if not sequences:
            shutil.copy2(input_path, output_path)
            return
        cmd = build_masonry_text_burn_command(
            input_path=input_path,
            sequences=sequences,
            output_path=output_path,
            duration_s=duration_s,
            board_width=board_width,
        )
        burn_t0 = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, timeout=MASONRY_TIMEOUT_S, check=False)
        elapsed_ms = int((time.monotonic() - burn_t0) * 1000)
        if result.returncode != 0:
            log.warning(
                "masonry_text_burn_failed",
                rc=result.returncode,
                elapsed_ms=elapsed_ms,
                stderr=result.stderr.decode("utf-8", "replace")[-800:],
            )
            shutil.copy2(input_path, output_path)
            return
        log.info(
            "masonry_text_burn_done",
            elapsed_ms=elapsed_ms,
            sequence_count=len(sequences),
            total_frames=sum(int(s.get("n_frames") or 0) for s in sequences),
        )
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def assemble_masonry_montage(
    *,
    steps: list,
    clip_id_to_local: dict[str, str],
    output_path: str,
    tmpdir: str,
    duration_s: float,
    audio_source_path: str | None = None,
    job_id: str | None = None,
    preset: str | None = None,
) -> None:
    """Render the masonry collage to ``output_path``."""
    resolved_preset = coerce_montage_preset(preset)
    mask_dir = os.path.join(tmpdir, "masonry_masks")
    tiles = build_masonry_tiles(
        steps=steps,
        clip_id_to_local=clip_id_to_local,
        mask_dir=mask_dir,
        normalize_images=True,
        preset=resolved_preset,
    )
    board_width = max(tile.x + tile.width for tile in tiles) + 34
    cmd = build_masonry_command(
        tiles=tiles,
        output_path=output_path,
        duration_s=duration_s,
        board_width=board_width,
        audio_source_path=audio_source_path,
    )
    result = subprocess.run(cmd, capture_output=True, timeout=MASONRY_TIMEOUT_S, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr[-2000:].decode("utf-8", "replace")
        raise RuntimeError(f"ffmpeg masonry montage failed (rc={result.returncode}): {stderr_tail}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("ffmpeg masonry montage produced empty output")
    log.info(
        "masonry_montage_rendered",
        job_id=job_id,
        preset=resolved_preset,
        tiles=len(tiles),
        duration_s=clamp_masonry_duration(duration_s),
    )
