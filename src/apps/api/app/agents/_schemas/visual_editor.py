"""Versioned, additive visual placement and animation authored by the editor."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VisualAnimation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    entrance: Literal["none", "fade", "pop", "slide", "zoom"] = "none"
    exit: Literal["none", "fade", "pop", "slide", "zoom"] = "none"
    loop: Literal["none", "pulse", "bounce", "float"] = "none"
    speed: float = Field(1, ge=0.25, le=3, allow_inf_nan=False)


class VisualEditorStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    rotation_deg: float = Field(0, ge=-360, le=360, allow_inf_nan=False)
    fit_mode: Literal["contain", "cover"] = "cover"
    zoom: float = Field(1, ge=1, le=4, allow_inf_nan=False)
    animation: VisualAnimation = Field(default_factory=VisualAnimation)
