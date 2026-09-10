"""Resolved typography instructions; all positions are output-canvas pixels.

The planner owns wrapping and baselines. The phone loads fingerprint-bound font
bytes and paints text locally. No rendered glyph images cross this boundary.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _TextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TextInk(_TextModel):
    red: float = Field(ge=0, le=1)
    green: float = Field(ge=0, le=1)
    blue: float = Field(ge=0, le=1)
    alpha: float = Field(ge=0, le=1)


class TextBlurLayer(_TextModel):
    color: TextInk
    sigma: float = Field(ge=0, le=100)
    dx: float = Field(ge=-1000, le=1000)
    dy: float = Field(ge=-1000, le=1000)


class TextGradientStop(_TextModel):
    position: float = Field(ge=0, le=1)
    color: TextInk


class TextGradient(_TextModel):
    start_x: float = Field(ge=-10000, le=10000)
    start_y: float = Field(ge=-10000, le=10000)
    end_x: float = Field(ge=-10000, le=10000)
    end_y: float = Field(ge=-10000, le=10000)
    stops: list[TextGradientStop] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def valid_gradient(self):
        if self.start_x == self.end_x and self.start_y == self.end_y:
            raise ValueError("gradient endpoints must differ")
        positions = [stop.position for stop in self.stops]
        if positions != sorted(positions):
            raise ValueError("gradient stops must be ordered")
        return self


class PositionedGlyph(_TextModel):
    glyph_id: int = Field(ge=1, le=65535)
    x: float = Field(ge=-10000, le=10000)
    y: float = Field(ge=-10000, le=10000)


class PositionedTextRun(_TextModel):
    text: str = Field(min_length=1, max_length=2000)
    font_asset_id: str = Field(min_length=1, max_length=160)
    font_size: float = Field(gt=0, le=1000)
    x: float = Field(ge=-10000, le=10000)
    baseline_y: float = Field(ge=-10000, le=10000)
    letter_spacing: float = Field(ge=-100, le=1000)
    shaped: bool
    glyphs: list[PositionedGlyph] | None = Field(default=None, min_length=1, max_length=4000)
    fill: TextInk
    stroke: TextInk
    # Full centered stroke width in pixels (cloud stroke_px is half this value).
    stroke_width: float = Field(ge=0, le=100)
    blur_layers: list[TextBlurLayer] = Field(default_factory=list, max_length=8)
    gradient: TextGradient | None = None

    @model_validator(mode="after")
    def valid_glyph_layout(self):
        if not self.shaped and self.glyphs is None:
            raise ValueError("unshaped text requires resolved glyph positions")
        return self


class ResolvedTextMotion(_TextModel):
    speed: float = Field(ge=0.25, le=4)
    intensity: float = Field(ge=0, le=1)
    easing: Literal["linear", "ease-out-cubic", "ease-in-out-cubic"]
    stagger_ms: float = Field(ge=0, le=250)
    order: Literal["forward", "reverse", "center-out"]
    direction: Literal["none", "up", "down", "left", "right"]
    travel_px: float = Field(ge=0, le=600)
    overshoot: float = Field(ge=0, le=1)
    blur_px: float = Field(ge=0, le=12)
    cursor_style: Literal["none", "bar", "block", "underscore"]
    cursor_blink_ms: float = Field(ge=100, le=2000)
    hold_s: float = Field(ge=0, le=3600)
    exit_s: float = Field(ge=0, le=2)
    reveal_ramp_ms: float = Field(ge=40, le=400)


class TextRevealBounds(_TextModel):
    left: float = Field(ge=-20000, le=20000)
    top: float = Field(ge=-20000, le=20000)
    right: float = Field(ge=-20000, le=20000)
    bottom: float = Field(ge=-20000, le=20000)

    @model_validator(mode="after")
    def positive_area(self):
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("reveal bounds require a positive area")
        return self


class TextStrokePoint(_TextModel):
    x: float = Field(ge=-10000, le=10000)
    y: float = Field(ge=-10000, le=10000)


class TextPenStroke(_TextModel):
    points: list[TextStrokePoint] = Field(min_length=2, max_length=2000)
    start_progress: float = Field(ge=0, le=1)
    end_progress: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def positive_window(self):
        if self.end_progress <= self.start_progress:
            raise ValueError("pen stroke must have a positive progress window")
        return self


class HandwritingContent(_TextModel):
    text: str = Field(min_length=1, max_length=5000)
    strokes: list[TextPenStroke] = Field(min_length=1, max_length=4000)
    ink_width: float = Field(gt=0, le=1000)
    fill: TextInk
    outline: TextInk
    outline_width: float = Field(ge=0, le=200)
    blur_layers: list[TextBlurLayer] = Field(default_factory=list, max_length=8)
    gradient: TextGradient | None = None

    @model_validator(mode="after")
    def bounded_points(self):
        if sum(len(stroke.points) for stroke in self.strokes) > 40000:
            raise ValueError("too many handwriting points")
        return self


class DiscreteRevealLine(_TextModel):
    text: str = Field(max_length=2000)
    run_index: int | None = Field(default=None, ge=0, le=99)
    # Cursor positions for each Unicode-scalar prefix, including the empty one.
    cursor_offsets: list[float] = Field(min_length=1, max_length=2001)
    cursor_run: PositionedTextRun

    @model_validator(mode="after")
    def valid_prefixes(self):
        if len(self.cursor_offsets) != len(self.text) + 1:
            raise ValueError("cursor offsets must cover every prefix")
        if any(abs(value) > 10000 for value in self.cursor_offsets):
            raise ValueError("cursor offset out of bounds")
        if bool(self.text) != (self.run_index is not None):
            raise ValueError("nonempty reveal lines require a font run")
        if self.cursor_run.text not in {" |", " _", " ▮"}:
            raise ValueError("unsupported reveal cursor")
        return self


class DiscreteRevealContent(_TextModel):
    text: str = Field(min_length=1, max_length=5000)
    schedule: list[float] | None = Field(default=None, max_length=5000)
    lines: list[DiscreteRevealLine] = Field(min_length=1, max_length=100)


class PortableTextLayer(_TextModel):
    id: str = Field(min_length=1, max_length=160)
    start: float = Field(ge=0, le=1800)
    end: float = Field(gt=0, le=1800)
    anchor_x: float = Field(ge=-10000, le=10000)
    anchor_y: float = Field(ge=-10000, le=10000)
    rotation_degrees: float = Field(ge=-3600, le=3600)
    runs: list[PositionedTextRun] = Field(default_factory=list, max_length=100)
    effect: Literal[
        "static",
        "none",
        "fade-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "pop-in",
        "bounce",
        "ink-reveal",
        "handwriting",
        "typewriter",
        "stream-in",
    ] = "static"
    motion: ResolvedTextMotion | None = None
    reveal_bounds: TextRevealBounds | None = None
    handwriting: HandwritingContent | None = None
    discrete_reveal: DiscreteRevealContent | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if (self.effect in {"typewriter", "stream-in"}) != (self.discrete_reveal is not None):
            raise ValueError("discrete reveal requires prefix geometry")
        if self.discrete_reveal:
            indices = [line.run_index for line in self.discrete_reveal.lines if line.text]
            if indices != list(range(len(self.runs))):
                raise ValueError("reveal lines must reference each font run in order")
            for line in self.discrete_reveal.lines:
                if line.run_index is not None and line.text != self.runs[line.run_index].text:
                    raise ValueError("reveal line text must match its font run")
                if line.run_index is not None:
                    run = self.runs[line.run_index]
                    if not run.shaped and len(run.glyphs or []) != len(line.text):
                        raise ValueError("legacy reveal requires one glyph per scalar")
        if self.effect == "handwriting":
            if self.handwriting is None or self.runs:
                raise ValueError("handwriting requires pen paths instead of font runs")
        elif self.handwriting is not None or not self.runs:
            raise ValueError("font text requires positioned runs")
        if (self.effect == "ink-reveal") != (self.reveal_bounds is not None):
            raise ValueError("ink reveal requires exact reveal bounds")
        if self.end <= self.start:
            raise ValueError("text layer must have a positive time window")
        if sum(len(run.glyphs or []) for run in self.runs) > 10000:
            raise ValueError("too many resolved glyphs")
        if sum(len(run.text) for run in self.runs) > 5000:
            raise ValueError("text layer is too large")
        return self
