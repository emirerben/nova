"""Explicit editor animation phases; omitted on legacy authored text."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DESIGNED_TEXT_EDGES = frozenset(
    {
        "silkReveal",
        "elasticCondensed",
        "focusPull",
        "wordRelay",
        "ribbonWipe",
        "echoStack",
    }
)

TextAnimationEdge = Literal[
    "none",
    "fade",
    "pop",
    "slide",
    "typewriter",
    "silkReveal",
    "elasticCondensed",
    "focusPull",
    "wordRelay",
    "ribbonWipe",
    "echoStack",
]


class TextAnimationPhases(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    entrance: TextAnimationEdge = "none"
    exit: TextAnimationEdge = "none"
    loop: Literal["none", "pulse", "bounce", "float"] = "none"
    speed: float = Field(default=1, ge=0.25, le=3)
