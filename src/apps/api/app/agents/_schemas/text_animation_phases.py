"""Explicit editor animation phases; omitted on legacy authored text."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Entrance and exit length at speed 1, for every edge. One value so a designed edge is never
# slower than the pop or fade beside it; the native mirror is generated from this constant.
TEXT_ANIMATION_EDGE_SECONDS = 0.4

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
