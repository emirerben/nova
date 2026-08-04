"""Blossom-carousel video-effects engine: shared contract package.

Physics port of https://github.com/jespervos/blossom-carousel (packages/core),
rendered offline through Skia + FFmpeg for use as a Nova generative-edit moment.
"""

from __future__ import annotations

from .cards import CardAsset, resolve_card_media
from .effects import (
    EFFECTS,
    CardGeometry,
    CardTransform,
    cards_stack_transform,
    cover_flow_transform,
    flipbook_transform,
    scale_sweep_transform,
    snap_bounds,
    snap_positions,
    transform_for,
)
from .encode import encode_carousel_segment
from .gesture import CANONICAL_FLICK, GestureTrace, dump_json
from .renderer import CANVAS_H, CANVAS_W, FPS, lagged_virtual_scroll, render_carousel_frames
from .segment import CarouselMomentSpec, render_carousel_moment
from .spring import (
    DAMPING,
    FRICTION,
    SpringFrame,
    SpringState,
    damp,
    is_settled,
    project,
    release,
    rubberband_offset,
    simulate,
    tick,
)

__all__ = [
    "CANONICAL_FLICK",
    "CANVAS_H",
    "CANVAS_W",
    "DAMPING",
    "EFFECTS",
    "FPS",
    "FRICTION",
    "CardAsset",
    "CardGeometry",
    "CardTransform",
    "CarouselMomentSpec",
    "GestureTrace",
    "SpringFrame",
    "SpringState",
    "cards_stack_transform",
    "cover_flow_transform",
    "damp",
    "dump_json",
    "encode_carousel_segment",
    "flipbook_transform",
    "is_settled",
    "lagged_virtual_scroll",
    "project",
    "release",
    "render_carousel_frames",
    "render_carousel_moment",
    "resolve_card_media",
    "rubberband_offset",
    "scale_sweep_transform",
    "simulate",
    "snap_bounds",
    "snap_positions",
    "tick",
    "transform_for",
]
