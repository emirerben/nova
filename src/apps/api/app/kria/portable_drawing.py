"""Resolved freehand-drawing layers painted on the phone (KRI-589).

No raster crosses this boundary: the phone replays the strokes with Core Graphics.
Times are output-timeline seconds. ``compile_drawing_layers`` (server) and
``NativeEditorRenderCompiler`` (device) both turn editor ``DrawingElement`` rows
into these layers by the same rules, frozen in
``tests/fixtures/drawing-parity/compile_v1.json``:

* a drawing starting at or after the timeline end is dropped;
* ``end`` is clamped to the timeline duration and the drawing is dropped when
  fewer than ``MIN_DRAWING_DURATION_S`` remain;
* ``draw_duration`` is clamped to ``end - start``;
* order is preserved;
* everything else (strokes, transform, canvas aspect, fades) is copied through.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents._schemas.drawing import (
    MAX_DRAWINGS,
    MAX_POINTS_PER_DRAWING,
    MAX_STROKES_PER_DRAWING,
    MIN_DRAWING_DURATION_S,
    DrawingElement,
    DrawingEntrance,
    DrawingExit,
    DrawingStroke,
    coerce_drawings,
)

MAX_DRAWING_LAYERS = MAX_DRAWINGS


class PortableDrawingLayer(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=160)
    start: float = Field(ge=0, le=1800)
    end: float = Field(gt=0, le=1800)
    canvas_aspect: float = Field(gt=0.2, lt=5.0)
    center_x: float = Field(ge=0, le=1)
    center_y: float = Field(ge=0, le=1)
    scale: float = Field(ge=0.2, le=3.0)
    rotation_deg: float = Field(ge=-180, le=180)
    entrance: DrawingEntrance
    draw_duration: float = Field(ge=0.2, le=10.0)
    exit: DrawingExit
    strokes: list[DrawingStroke] = Field(min_length=1, max_length=MAX_STROKES_PER_DRAWING)

    @model_validator(mode="after")
    def valid_layer(self) -> PortableDrawingLayer:
        if self.end - self.start < MIN_DRAWING_DURATION_S - 1e-6:
            raise ValueError("drawing layer is too short")
        if self.draw_duration > self.end - self.start + 1e-6:
            raise ValueError("draw duration exceeds the layer window")
        if sum(len(stroke.points) for stroke in self.strokes) > MAX_POINTS_PER_DRAWING:
            raise ValueError("drawing layer exceeds the point budget")
        return self


def compile_drawing_layers(
    drawings: list[DrawingElement] | list[dict] | None, *, timeline_duration_s: float
) -> list[PortableDrawingLayer]:
    """Editor ``drawings`` lane -> portable layers (rules frozen in the module docstring).

    Tolerant by design: it runs on every recipe rebuild, so a stored row that no longer
    fits the (possibly shorter) timeline is dropped or clamped, never raised.
    """
    layers: list[PortableDrawingLayer] = []
    for drawing in coerce_drawings(
        [d.model_dump(mode="json") if isinstance(d, DrawingElement) else d for d in drawings or []]
    ):
        end = min(drawing.end_s, timeline_duration_s)
        if drawing.start_s >= timeline_duration_s or end - drawing.start_s < MIN_DRAWING_DURATION_S:
            continue
        layers.append(
            PortableDrawingLayer(
                id=drawing.id,
                start=drawing.start_s,
                end=end,
                canvas_aspect=drawing.canvas_aspect,
                center_x=drawing.center_x,
                center_y=drawing.center_y,
                scale=drawing.scale,
                rotation_deg=drawing.rotation_deg,
                entrance=drawing.animation.entrance,
                draw_duration=min(drawing.animation.draw_duration_s, end - drawing.start_s),
                exit=drawing.animation.exit,
                strokes=drawing.strokes,
            )
        )
    return layers
