"""Explicit editor animation phases; omitted on legacy authored text."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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


@dataclass(frozen=True)
class EdgeVocab:
    label: str
    label_tr: str
    # Creator phrases that name this edge in free text, folded (lowercase, ASCII). A bare
    # "pop" or "slide" is left out: "make it pop" and "slide 3" are not animation asks.
    aliases: tuple[str, ...]


# The one place an edge gets the words the AI layers use for it: prompts, brief checks,
# receipts and creator-phrase detection all derive from this table. Labels match the
# iPhone picker titles.
TEXT_ANIMATION_VOCAB: dict[TextAnimationEdge, EdgeVocab] = {
    "none": EdgeVocab("None", "yok", ()),
    "fade": EdgeVocab("Fade", "solma", ("fade", "fades", "fading", "fade in", "solma")),
    "pop": EdgeVocab("Pop", "pop", ("pop in", "pop animation", "pop effect")),
    "slide": EdgeVocab(
        "Slide", "kayma", ("slide in", "slide up", "slide down", "slide animation", "kayma")
    ),
    "typewriter": EdgeVocab("Typewriter", "daktilo", ("typewriter", "type writer", "daktilo")),
    "silkReveal": EdgeVocab("Silk Reveal", "Silk Reveal", ("silk reveal",)),
    "elasticCondensed": EdgeVocab("Elastic Condensed", "Elastic Condensed", ("elastic condensed",)),
    "focusPull": EdgeVocab("Focus Pull", "Focus Pull", ("focus pull",)),
    "wordRelay": EdgeVocab("Word Relay", "Word Relay", ("word relay",)),
    "ribbonWipe": EdgeVocab("Ribbon Wipe", "Ribbon Wipe", ("ribbon wipe",)),
    "echoStack": EdgeVocab("Echo Stack", "Echo Stack", ("echo stack",)),
}
_vocab_mismatch = set(get_args(TextAnimationEdge)) ^ set(TEXT_ANIMATION_VOCAB)
if _vocab_mismatch:
    raise AssertionError(
        "TEXT_ANIMATION_VOCAB must have exactly one entry per TextAnimationEdge; "
        f"add or remove: {sorted(_vocab_mismatch)}"
    )


# The entrance a row without `animation_phases` plays, from its pre-phase `effect`.
LEGACY_EFFECT_ENTRANCE: dict[str, TextAnimationEdge] = {
    "static": "none",
    "none": "none",
    "fade-in": "fade",
    "pop-in": "pop",
    "slide-in": "slide",
    "typewriter": "typewriter",
}


def edge_alias_pattern(edges: Iterable[str]) -> str:
    """Regex alternation of the creator phrases naming ``edges``, for folded text.

    A space inside a phrase is optional, so "word relay" also matches "wordrelay".
    """
    return "|".join(
        re.escape(alias).replace(r"\ ", " ?")
        for edge in edges
        for alias in TEXT_ANIMATION_VOCAB[edge].aliases
    )


class TextAnimationPhases(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    entrance: TextAnimationEdge = "none"
    exit: TextAnimationEdge = "none"
    loop: Literal["none", "pulse", "bounce", "float"] = "none"
    speed: float = Field(default=1, ge=0.25, le=3)


def phases_patch(value: object) -> dict[str, Any] | None:
    """``value`` as a partial phases patch (only the keys it sets), or None when invalid."""
    if (
        not isinstance(value, dict)
        or not value
        or set(value) - set(TextAnimationPhases.model_fields)
    ):
        return None
    try:
        TextAnimationPhases(**value)
    except ValidationError:
        return None
    return dict(value)
