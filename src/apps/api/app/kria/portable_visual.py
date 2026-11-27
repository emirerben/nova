"""Bounded visual replacement windows shared with the native compositor."""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from app.agents._schemas.visual_editor import VisualEditorStyle
from app.kria.portable_text import TextInk

# KRI-1616: the renderer-safety bound for a placed card hanging past the side of the canvas, the
# only limit on her own drags (every other card rule is a preference of the intro offer). At least
# this share of the card's own width stays on the canvas, so the part she can touch never goes;
# the card's centre x then lies in `edge_bleed_x_range(width)`. Its height is its picture's,
# unknown here, so y keeps [0, 1] (at most half of it past the top or bottom). Swift:
# `VisualMediaPlacement.edgeBleedKeepOnCanvas`; both read tests/fixtures/edge-bleed-bound/.
EDGE_BLEED_KEEP_ON_CANVAS = 0.25
# The widest reach, for a full-width card: a schema bound only, the exact one is per width.
EDGE_BLEED_MAX_REACH = 0.5 - EDGE_BLEED_KEEP_ON_CANVAS
_BOUND_EPS = 1e-9


def edge_bleed_x_range(width: float) -> tuple[float, float]:
    """``(lowest, highest)`` centre x of a card ``width`` of the canvas wide."""
    reach = EDGE_BLEED_MAX_REACH * min(max(float(width), 0.0), 1.0)
    return -reach, 1.0 + reach


def within_edge_bleed(x: float, width: float | None) -> bool:
    """Is a card centred at ``x`` inside the bound? No width (a full-frame picture): [0, 1]."""
    low, high = edge_bleed_x_range(width) if width is not None else (0.0, 1.0)
    return low - _BOUND_EPS <= x <= high + _BOUND_EPS


def clamp_to_edge_bleed(x: float, width: float) -> float:
    low, high = edge_bleed_x_range(width)
    return min(max(float(x), low), high)


def is_bled(x: float) -> bool:
    """Is a centre past the frame, which a build below `INTRO_EDGE_BLEED_PROTOCOL` refuses?"""
    return not 0.0 <= x <= 1.0


class VisualMediaPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    order: int = Field(ge=0, le=1000)
    contain: bool = False
    focal_x: float = Field(default=0.5, ge=0, le=1)
    focal_y: float = Field(default=0.5, ge=0, le=1)
    zoom: float = Field(default=1, ge=1, le=4)
    pre_crop: bool = False
    motion: Literal["none", "zoom_in", "zoom_out", "pan_left", "pan_right"] = "none"
    width_fraction: float | None = Field(default=None, ge=0.05, le=1)
    x_fraction: float = Field(default=0.5, ge=-EDGE_BLEED_MAX_REACH, le=1 + EDGE_BLEED_MAX_REACH)
    y_fraction: float = Field(default=0.5, ge=0, le=1)
    window_start: float = Field(ge=0, le=1800)
    window_end: float = Field(gt=0, le=1800)
    fade_in: bool = False
    fade_out: bool = False
    # KRI-1264: a phone Talking card's zoom and animation (`VisualMediaPlacement.editorStyle` on
    # the device). Omitted while None, so every placement that predates it keeps its bytes.
    editor_style: VisualEditorStyle | None = None

    @model_validator(mode="after")
    def window(self):
        if self.window_end <= self.window_start:
            raise ValueError("visual window must have positive duration")
        if not within_edge_bleed(self.x_fraction, self.width_fraction):
            raise ValueError("a placed card keeps a quarter of its width on the canvas")
        return self

    @model_serializer(mode="wrap")
    def _omit_unstyled(self, handler: SerializerFunctionWrapHandler) -> dict:
        payload = handler(self)
        if self.editor_style is None:
            payload.pop("editor_style", None)
        return payload


class VisualCanvasFill(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=160)
    start: float = Field(ge=0, le=1800)
    end: float = Field(gt=0, le=1800)
    order: int = Field(ge=0, le=1000)
    kind: Literal["solid", "gradient", "blur_previous"]
    color: TextInk
    end_color: TextInk | None = None
    angle: float = Field(default=180, ge=0, le=360)
    blur_radius: float = Field(default=24, ge=1, le=80)
    fade_in: bool = False
    fade_out: bool = False

    @model_validator(mode="after")
    def window(self):
        if self.end <= self.start or (self.kind == "gradient" and self.end_color is None):
            raise ValueError("visual fill needs a positive window and gradient endpoints")
        return self


class AudioMuteWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start: float = Field(ge=0, le=1800)
    end: float = Field(gt=0, le=1800)
    clip_ids: list[str] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def window(self):
        if self.end <= self.start:
            raise ValueError("mute window must have positive duration")
        return self
