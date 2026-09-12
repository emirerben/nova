"""Explicit editor animation phases; omitted on legacy authored text."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TextAnimationPhases(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    entrance: Literal["none", "fade", "pop", "slide", "typewriter"] = "none"
    exit: Literal["none", "fade", "pop", "slide", "typewriter"] = "none"
    loop: Literal["none", "pulse", "bounce", "float"] = "none"
    speed: float = Field(default=1, ge=0.25, le=3)
