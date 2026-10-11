"""Freehand drawing lane for the phone editor (KRI-589).

A drawing is a creator-authored set of finger strokes shown over the picture for a
bounded window. It is its own editor section (``drawings``), not a visual block:
visual-block consumers assume media clips.

Coordinates are normalised to the authoring canvas (``canvas_aspect`` = width /
height) so a drawing never distorts when the output canvas has another aspect;
renderers aspect-fit the authoring canvas into the output canvas. Scale and
rotation pivot on the strokes' bounding-box centre, which ``center_x`` /
``center_y`` position on the output canvas (a freshly drawn element has them at
the bbox centre, so the transform is the identity).

The device compiles ``DrawingElement`` into ``PortableDrawingLayer``
(``app.kria.portable_drawing``) with ``compile_drawing_layers``; the rules are
frozen by ``tests/fixtures/drawing-parity/compile_v1.json`` and asserted by both
the Python and the Swift compilers.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DRAWINGS = 12
MAX_STROKES_PER_DRAWING = 60
MAX_POINTS_PER_STROKE = 1500
MAX_POINTS_PER_DRAWING = 3000
MAX_POINTS_PER_VARIANT = 8000
MIN_DRAWING_DURATION_S = 0.1
MAX_DRAW_DURATION_S = 10.0

DrawingTexture = Literal["pen", "marker", "highlighter", "neon", "chalk", "spray"]
DrawingEntrance = Literal["draw", "fade", "none"]
DrawingExit = Literal["none", "fade"]

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class DrawingStroke(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    color: str
    opacity: float = Field(default=1.0, ge=0.1, le=1.0)
    width_frac: float = Field(ge=0.004, le=0.08)
    texture: DrawingTexture = "pen"
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    points: list[tuple[float, float]] = Field(min_length=2, max_length=MAX_POINTS_PER_STROKE)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        if not _HEX_COLOR.match(value):
            raise ValueError("stroke color must be #RRGGBB")
        return value.upper()

    @field_validator("points")
    @classmethod
    def _points(cls, value: list[tuple[float, float]]) -> list[tuple[float, float]]:
        for x, y in value:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("stroke points must be normalised to 0..1")
        return [(round(x, 4), round(y, 4)) for x, y in value]


class DrawingAnimation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    entrance: DrawingEntrance = "draw"
    draw_duration_s: float = Field(default=1.2, ge=0.2, le=MAX_DRAW_DURATION_S)
    exit: DrawingExit = "none"


class DrawingElement(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=160)
    start_s: float = Field(ge=0.0, le=1800.0)
    end_s: float = Field(gt=0.0, le=1800.0)
    canvas_aspect: float = Field(gt=0.2, lt=5.0)
    center_x: float = Field(default=0.5, ge=0.0, le=1.0)
    center_y: float = Field(default=0.5, ge=0.0, le=1.0)
    scale: float = Field(default=1.0, ge=0.2, le=3.0)
    rotation_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    animation: DrawingAnimation = Field(default_factory=DrawingAnimation)
    strokes: list[DrawingStroke] = Field(min_length=1, max_length=MAX_STROKES_PER_DRAWING)

    @model_validator(mode="after")
    def _window_and_points(self) -> DrawingElement:
        if self.end_s - self.start_s < MIN_DRAWING_DURATION_S - 1e-6:
            raise ValueError(f"drawing must last at least {MIN_DRAWING_DURATION_S:g}s")
        if sum(len(stroke.points) for stroke in self.strokes) > MAX_POINTS_PER_DRAWING:
            raise ValueError(f"drawing exceeds {MAX_POINTS_PER_DRAWING} points")
        return self


def coerce_drawings(raw: list[dict] | None) -> list[DrawingElement]:
    """Tolerant read: invalid legacy rows are dropped, valid ones kept."""
    if not raw:
        return []
    out: list[DrawingElement] = []
    for item in raw[:MAX_DRAWINGS]:
        try:
            out.append(DrawingElement.model_validate(item))
        except Exception:  # noqa: BLE001 - tolerant read path by design
            continue
    return out


def validate_drawings(raw: list[dict] | list[DrawingElement], *, duration_s: float) -> list[dict]:
    """Strict write validation: caps, unique ids, inside the variant duration."""
    if len(raw) > MAX_DRAWINGS:
        raise ValueError(f"Maximum {MAX_DRAWINGS} drawings allowed")
    drawings = [
        item if isinstance(item, DrawingElement) else DrawingElement.model_validate(item)
        for item in raw
    ]
    ids: set[str] = set()
    total_points = 0
    for drawing in drawings:
        if drawing.id in ids:
            raise ValueError("drawing ids must be unique")
        ids.add(drawing.id)
        if drawing.end_s > duration_s + 1.0 / 24.0:
            raise ValueError("drawing exceeds the variant duration")
        total_points += sum(len(stroke.points) for stroke in drawing.strokes)
    if total_points > MAX_POINTS_PER_VARIANT:
        raise ValueError(f"drawings exceed {MAX_POINTS_PER_VARIANT} points in total")
    return [drawing.model_dump(mode="json") for drawing in drawings]
