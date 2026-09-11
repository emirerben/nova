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
    alpha_power: int = Field(default=1, strict=True, ge=1, le=2)
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


class SmoothRevealLine(_TextModel):
    text: str = Field(max_length=2000)
    run_index: int | None = Field(default=None, ge=0, le=99)
    bounds: TextRevealBounds | None = None
    first_strong_rtl: bool

    @model_validator(mode="after")
    def valid_geometry(self):
        if bool(self.text) != (self.run_index is not None) or bool(self.text) != (
            self.bounds is not None
        ):
            raise ValueError("smooth reveal requires geometry for each nonempty line")
        return self


class SmoothRevealContent(_TextModel):
    text: str = Field(min_length=1, max_length=5000)
    lines: list[SmoothRevealLine] = Field(min_length=1, max_length=100)


class StaggeredGlyph(_TextModel):
    logical_line: int = Field(ge=0, le=99)
    glyph_index: int = Field(ge=0, le=4999)
    run: PositionedTextRun
    pivot_x: float = Field(ge=-20000, le=20000)
    pivot_y: float = Field(ge=-20000, le=20000)


class StaggeredContent(_TextModel):
    text: str = Field(min_length=1, max_length=5000)
    glyphs: list[StaggeredGlyph] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def valid_glyphs(self):
        import regex

        if "\n".join(" ".join(line.split()) for line in self.text.split("\n")) != self.text:
            raise ValueError("staggered text must be normalized")
        lines = [regex.findall(r"\X", line) for line in self.text.split("\n")]
        if len(lines) > 100:
            raise ValueError("too many logical lines")
        indices = [(glyph.logical_line, glyph.glyph_index) for glyph in self.glyphs]
        if indices != sorted(set(indices)):
            raise ValueError("staggered glyph indices must be ordered and unique")
        for glyph in self.glyphs:
            if (
                glyph.logical_line >= len(lines)
                or glyph.glyph_index >= len(lines[glyph.logical_line])
                or glyph.run.text != lines[glyph.logical_line][glyph.glyph_index]
                or glyph.run.shaped
            ):
                raise ValueError("staggered glyph requires matching unshaped text")
        if sum(len(glyph.run.glyphs or []) for glyph in self.glyphs) > 10000:
            raise ValueError("too many staggered glyphs")
        return self


class TextFadeEnvelope(_TextModel):
    kind: Literal["lyric", "sequence"]
    in_ms: int = Field(ge=0, le=1800000)
    out_ms: int = Field(ge=0, le=1800000)
    curve: Literal["square", "sqrt"]

    @model_validator(mode="after")
    def valid_head(self):
        if self.kind == "sequence" and self.in_ms != 0:
            raise ValueError("sequence envelope only controls its tail")
        return self


class KaraokeContent(_TextModel):
    starts: list[float] = Field(min_length=1, max_length=100)
    highlight: TextInk

    @model_validator(mode="after")
    def valid_starts(self):
        import math

        if any(not math.isfinite(start) or start < 0 or start > 1800 for start in self.starts):
            raise ValueError("karaoke starts must be finite local timestamps")
        return self


class GiantTitleTransition(_TextModel):
    origin_x: float = Field(ge=-30000, le=30000)
    origin_y: float = Field(ge=-30000, le=30000)


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
        "slide-in",
        "pop-in",
        "bounce",
        "ink-reveal",
        "handwriting",
        "typewriter",
        "stream-in",
        "smooth-type",
        "staggered-slice",
        "dissolve-out",
        "karaoke-line",
        "lyric-line",
    ] = "static"
    motion: ResolvedTextMotion | None = None
    reveal_bounds: TextRevealBounds | None = None
    handwriting: HandwritingContent | None = None
    discrete_reveal: DiscreteRevealContent | None = None
    smooth_reveal: SmoothRevealContent | None = None
    staggered: StaggeredContent | None = None
    karaoke: KaraokeContent | None = None
    fade: TextFadeEnvelope | None = None
    giant_title: GiantTitleTransition | None = None
    dissolve_seed: int | None = Field(default=None, strict=True, ge=0, le=4294967295)

    @model_validator(mode="after")
    def valid_window(self):
        if self.giant_title is not None and self.effect not in {
            "static",
            "none",
            "fade-in",
            "scale-up",
            "slide-up",
            "slide-down",
            "slide-in",
            "pop-in",
            "bounce",
            "ink-reveal",
            "lyric-line",
            "typewriter",
            "stream-in",
            "karaoke-line",
            "smooth-type",
            "staggered-slice",
        }:
            raise ValueError("giant title is unsupported for this text painter")
        if (self.effect == "lyric-line") != bool(self.fade and self.fade.kind == "lyric"):
            raise ValueError("lyric line requires its fade envelope")
        if (
            self.fade
            and self.fade.kind == "sequence"
            and self.effect not in {"static", "none", "fade-in", "handwriting", "ink-reveal"}
        ):
            raise ValueError("sequence fade is unsupported for this effect")
        if (self.effect == "karaoke-line") != (self.karaoke is not None):
            raise ValueError("karaoke requires timed word geometry")
        if self.karaoke and (
            len(self.karaoke.starts) != len(self.runs)
            or any(run.shaped or run.gradient is not None for run in self.runs)
        ):
            raise ValueError("karaoke requires one unshaped solid-color run per timestamp")
        if (self.effect == "dissolve-out") != (self.dissolve_seed is not None):
            raise ValueError("dissolve requires an explicit renderer seed")
        if (self.effect == "staggered-slice") != (self.staggered is not None):
            raise ValueError("staggered slice requires glyph geometry")
        if (self.effect == "smooth-type") != (self.smooth_reveal is not None):
            raise ValueError("smooth reveal requires per-line geometry")
        if self.smooth_reveal:
            if [line.run_index for line in self.smooth_reveal.lines if line.text] != list(
                range(len(self.runs))
            ):
                raise ValueError("smooth lines must reference each font run in order")
            for line in self.smooth_reveal.lines:
                if line.run_index is not None:
                    run = self.runs[line.run_index]
                    if line.text != run.text or not run.shaped:
                        raise ValueError("smooth line requires matching shaped text")
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
