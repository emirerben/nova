"""Internal types for the temporal-grouping and phrase-reconstruction stages
of the Layer-2 text-overlay pipeline.

These shapes sit between raw `FrameDetection` (PR 1) and the public
`TemplateTextOverlay` output (existing schema). They are not exposed outside
the pipeline module — callers only see `TemplateTextOutput` at the end.

PR 2 slice 1 (this file) ships the schemas + stages C (temporal grouping)
and D (phrase reconstruction). Stages A/B (frame extraction, OCR) reuse
PR 1's wrapper; stages E/F (alignment, classification) land in a follow-up
slice; integration with `template_text_extraction.py` lands behind the
`text_overlay_v2_enabled` flag in the final slice.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


def _aabb_union(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Axis-aligned bounding-box union in normalized coords."""
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _aabb_x_center(a: tuple[float, float, float, float]) -> float:
    return (a[0] + a[2]) / 2.0


def _aabb_y_center(a: tuple[float, float, float, float]) -> float:
    return (a[1] + a[3]) / 2.0


def _aabb_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection-over-union for two axis-aligned bboxes in normalized coords."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    if union <= 0.0:
        return 0.0
    return inter / union


class TextEvent(BaseModel):
    """One contiguous appearance of one OCR'd line across multiple frames.

    Aggregated from the per-frame `FrameDetection` stream by the temporal-
    grouping stage. An event tracks the text from its first frame of visibility
    to its last frame of visibility, allowing a small jitter gap (1s by default)
    for missed OCR detections between non-consecutive frames.

    `aabb` is the union of bboxes across all constituent frames — generous
    enough that downstream phrase-reconstruction spatial checks are not
    sensitive to per-frame OCR jitter.
    """

    text: str = Field(min_length=1)
    start_t_s: float = Field(ge=0.0)
    end_t_s: float = Field(ge=0.0)
    aabb: tuple[float, float, float, float]
    mean_confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_window(self) -> TextEvent:
        if self.end_t_s < self.start_t_s:
            raise ValueError(f"end_t_s ({self.end_t_s}) must be >= start_t_s ({self.start_t_s})")
        x_min, y_min, x_max, y_max = self.aabb
        if not (
            0.0 <= x_min <= 1.0
            and 0.0 <= y_min <= 1.0
            and 0.0 <= x_max <= 1.0
            and 0.0 <= y_max <= 1.0
        ):
            raise ValueError(f"aabb outside unit square: {self.aabb}")
        if x_max < x_min or y_max < y_min:
            raise ValueError(f"aabb has negative extent: {self.aabb}")
        return self

    def x_center(self) -> float:
        return _aabb_x_center(self.aabb)

    def y_center(self) -> float:
        return _aabb_y_center(self.aabb)


class Phrase(BaseModel):
    """A cluster of `TextEvent`s that visually belong to the same caption.

    Captures three real-world cases:

    1. Single-line caption: one event becomes one phrase with one line.
    2. Simultaneous multi-line: events that appear together at different Y
       positions but share an X-band — one phrase, multiple lines, sorted
       top-to-bottom.
    3. Build-up / typewriter captions: events that appear sequentially, each
       starting before the prior ones end. One phrase whose lines accumulate
       in the order they appeared on screen.

    A new phrase starts when the screen clears (all prior events end) before
    a new event begins. The phrase-reconstruction stage handles this.

    `sample_text` joins lines with "\\n" in Y-order, matching what a human
    sees on the final frame of the build-up — this is the field downstream
    alignment + classification will key off.
    """

    lines: list[str] = Field(min_length=1)
    start_t_s: float = Field(ge=0.0)
    end_t_s: float = Field(ge=0.0)
    aabb: tuple[float, float, float, float]
    mean_confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate(self) -> Phrase:
        if self.end_t_s < self.start_t_s:
            raise ValueError(f"end_t_s ({self.end_t_s}) must be >= start_t_s ({self.start_t_s})")
        x_min, y_min, x_max, y_max = self.aabb
        if not (
            0.0 <= x_min <= 1.0
            and 0.0 <= y_min <= 1.0
            and 0.0 <= x_max <= 1.0
            and 0.0 <= y_max <= 1.0
        ):
            raise ValueError(f"aabb outside unit square: {self.aabb}")
        return self

    @property
    def sample_text(self) -> str:
        return "\n".join(self.lines)
