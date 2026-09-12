"""Bounded visual replacement windows shared with the native compositor."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kria.portable_text import TextInk


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
    x_fraction: float = Field(default=0.5, ge=0, le=1)
    y_fraction: float = Field(default=0.5, ge=0, le=1)
    window_start: float = Field(ge=0, le=1800)
    window_end: float = Field(gt=0, le=1800)
    fade_in: bool = False
    fade_out: bool = False

    @model_validator(mode="after")
    def window(self):
        if self.window_end <= self.window_start:
            raise ValueError("visual window must have positive duration")
        return self


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
