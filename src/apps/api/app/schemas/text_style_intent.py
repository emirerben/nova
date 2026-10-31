"""Creator text-style asks the montage can honour: title animation and label corner.

KRI-522: "a hook animated with typewriter" and "placeholder location ... to the bottom
left" were paraphrased away before they reached the renderer, which already draws both.
These are the closed vocabularies the brief, the strategy and the snapshot share, so a
creator's words reach `guided_story._text_elements` as typed values instead of prose.
Unknown values normalise to None (the default look); nothing here ever raises.
"""

from __future__ import annotations

import re
from typing import Literal, get_args

from app.agents._schemas.text_animation_phases import TEXT_ANIMATION_VOCAB, TextAnimationEdge

_TITLE_ANIMATIONS = tuple(edge for edge in get_args(TextAnimationEdge) if edge != "none")
TitleAnimation = Literal[_TITLE_ANIMATIONS]  # type: ignore[valid-type]
LabelPosition = Literal[
    "top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right"
]

_SEPARATORS = re.compile(r"[\s_\-]+")


def _fold(value: object) -> str:
    return _SEPARATORS.sub(" ", str(value).strip().casefold()) if value is not None else ""


_TITLE_ANIMATION_ALIASES: dict[str, TitleAnimation] = {
    phrase: edge
    for edge in _TITLE_ANIMATIONS
    for phrase in (
        _fold(edge),
        _fold(TEXT_ANIMATION_VOCAB[edge].label),
        *TEXT_ANIMATION_VOCAB[edge].aliases,
    )
} | {
    # A `facts.animation` value, not free text, so the loose verb forms are safe here.
    "typewriting": "typewriter",
    "typing": "typewriter",
    "typed": "typewriter",
    "type": "typewriter",
}

_LABEL_POSITION_ALIASES: dict[str, LabelPosition] = {
    "top left": "top_left",
    "upper left": "top_left",
    "top": "top",
    "top center": "top",
    "top right": "top_right",
    "upper right": "top_right",
    "middle": "middle",
    "center": "middle",
    "centre": "middle",
    "bottom left": "bottom_left",
    "lower left": "bottom_left",
    "bottom": "bottom",
    "bottom center": "bottom",
    "bottom centre": "bottom",
    "bottom right": "bottom_right",
    "lower right": "bottom_right",
}


def normalize_title_animation(value: object) -> TitleAnimation | None:
    """The animation a creator asked the title to enter with, else None."""
    if not isinstance(value, str):
        return None
    return _TITLE_ANIMATION_ALIASES.get(_fold(value))


def normalize_label_position(value: object) -> LabelPosition | None:
    """The corner/edge a creator asked per-clip labels to sit in, else None."""
    if not isinstance(value, str):
        return None
    return _LABEL_POSITION_ALIASES.get(_fold(value))


# Label anchor -> (x_frac, y_frac, alignment). `x_frac` is pinned by `alignment`: the
# line's left edge for left, right edge for right (TextElement.x_frac). 0.08 / 0.12 /
# 0.78 (the label height every montage already uses) sit inside the phone-safe margin.
# "bottom" is today's fast-montage label spot, so asking for it changes nothing.
LABEL_ANCHORS: dict[LabelPosition, tuple[float, float, Literal["left", "center", "right"]]] = {
    "top_left": (0.08, 0.12, "left"),
    "top": (0.5, 0.12, "center"),
    "top_right": (0.92, 0.12, "right"),
    "middle": (0.5, 0.5, "center"),
    "bottom_left": (0.08, 0.78, "left"),
    "bottom": (0.5, 0.78, "center"),
    "bottom_right": (0.92, 0.78, "right"),
}
LABEL_DEFAULT_POSITION: LabelPosition = "bottom"

# The typewriter reveal shares the entrance envelope `min(0.4 / speed, hold / 2)`
# (pipeline/text_animation_phases.py). At speed 1 a whole sentence would appear in 0.4s,
# which reads as a pop, so typing is slowed until the title hold caps it.
TITLE_ANIMATION_SPEED: dict[TitleAnimation, float] = {
    **dict.fromkeys(_TITLE_ANIMATIONS, 1.0),
    "typewriter": 0.3,
}
