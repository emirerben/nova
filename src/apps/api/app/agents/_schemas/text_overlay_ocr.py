"""Internal types for the per-frame OCR stage of the Layer-2 text-overlay
pipeline.

These shapes are NOT the public `TemplateTextOverlay` schema. They sit
between raw OCR backend output and the temporal-grouping / phrase-
reconstruction stages that follow. The conversion to TemplateTextOverlay
happens once, at the end of the pipeline (stage G in the design doc).

PR 1 ships these schemas + the OCR backend wrapper only. No code calls them
yet; they exist so subsequent PRs can import a stable shape.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OcrPolygon(BaseModel):
    """Four-point quadrilateral in normalized [0, 1] frame coordinates.

    Vertex order is clockwise starting top-left RELATIVE TO THE TEXT
    BASELINE — not the image axes. For axis-aligned (rotated text included)
    captions the two agree, but for tilted/rotated text Cloud Vision's
    `points[0]` is the upper-left of the text rectangle in its own
    orientation, which may be in any quadrant of the image frame. Callers
    that need an axis-aligned image-coord bbox must use `.aabb()`; never
    read `points[0]` directly as "top-left of the image."

    Backends that emit axis-aligned bboxes (Apple Vision) still fill all
    four points so the schema stays uniform.
    """

    points: list[tuple[float, float]] = Field(min_length=4, max_length=4)

    def aabb(self) -> tuple[float, float, float, float]:
        """Axis-aligned bounding box as (x_min, y_min, x_max, y_max).

        Loses rotation information. Use this when feeding the renderer's
        `text_bbox` which is itself axis-aligned.
        """
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))


class FrameDetection(BaseModel):
    """One OCR text detection on one video frame.

    `frame_t_s` is the GLOBAL seconds offset of the frame the detection
    came from (i.e. ffmpeg `-ss` value). The temporal-grouping stage uses
    this to cluster the same line across consecutive frames into events.
    """

    frame_t_s: float = Field(ge=0.0)
    text: str = Field(min_length=1)
    polygon: OcrPolygon
    confidence: float = Field(ge=0.0, le=1.0)
