"""KRI-1585: the informative text designs a creator picks from (A1, A2, A3, B1), as data.

Every informative edit opens with a hook title; the facts then follow in the chosen design.
Mode A (``title_details``) gives some clips a heading and a detail line and leaves others bare.
Mode B (``running_guide``) puts one line on every clip, with no headings.

Sizes are px on the 1080x1920 canvas, measured from the approved teardown stills
(docs/reviews/kri-1585/decisions.md, "Teardown measurements"). The hook size is the
measured one; the teardown hooks all fill about 0.78 of the width (830-850 px), so the
renderer may fit to ``HOOK_WIDTH_FRAC`` instead. This module is data only: drawing a heading
and detail pair, the scrim and the fonts the phone lacks are KRI-1581.

Built to become KRI-1566's title-look registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TextDesignId = Literal["A1", "A2", "A3", "B1"]
TextDesignMode = Literal["title_details", "running_guide"]

HOOK_COLOR = "#FFFFFF"
HEADING_COLOR = "#FFE7A0"
DETAIL_COLOR = "#FFFFFF"
HOOK_WIDTH_FRAC = 0.78
# Teardown round 1: the hook holds 0-3.4 s over the opening shot; facts start at 4 s.
HOOK_WINDOW_S = (0.0, 3.4)
FACTS_START_S = 4.0


@dataclass(frozen=True)
class TextLevel:
    """One level of informative text (hook, heading, detail or guide line)."""

    font_family: str
    size_px: int
    color: str
    alignment: Literal["left", "center"]
    text_case: Literal["upper"] | None = None
    letter_spacing: float | None = None
    # Teardown round 1: every level is drawn with a soft shadow.
    shadow_enabled: bool = True

    def style_values(self) -> dict:
        values = {
            "font_family": self.font_family,
            "size_px": self.size_px,
            "color": self.color,
            "alignment": self.alignment,
            "shadow_enabled": self.shadow_enabled,
        }
        if self.text_case is not None:
            values["text_case"] = self.text_case
        if self.letter_spacing is not None:
            values["letter_spacing"] = self.letter_spacing
        return values


@dataclass(frozen=True)
class TextDesign:
    id: TextDesignId
    mode: TextDesignMode
    hook: TextLevel
    detail: TextLevel
    heading: TextLevel | None = None
    anchor: Literal["bottom_left", "bottom"] = "bottom_left"
    scrim: Literal["bottom_gradient"] | None = None

    def level(self, name: Literal["hook", "heading", "detail"]) -> TextLevel:
        found = getattr(self, name)
        if found is None:
            raise ValueError(f"design {self.id} has no {name} level")
        return found


TEXT_DESIGNS: dict[str, TextDesign] = {
    design.id: design
    for design in (
        TextDesign(
            id="A1",
            mode="title_details",
            hook=TextLevel("Literata", 91, HOOK_COLOR, "left"),
            # Tracked caps, about 32 px by cap height. Neither the font (it looks like Inter)
            # nor the tracking was fitted to the still.
            heading=TextLevel(
                "Inter Medium", 32, HEADING_COLOR, "left", text_case="upper", letter_spacing=0.12
            ),
            detail=TextLevel("Literata", 53, DETAIL_COLOR, "left"),
            anchor="bottom_left",
        ),
        TextDesign(
            id="A2",
            mode="title_details",
            hook=TextLevel("Schibsted Grotesk", 98, HOOK_COLOR, "left"),
            heading=TextLevel("Schibsted Grotesk", 64, HEADING_COLOR, "left"),
            detail=TextLevel("Inter Regular", 36, DETAIL_COLOR, "left"),
            anchor="bottom_left",
            scrim="bottom_gradient",
        ),
        TextDesign(
            id="A3",
            mode="title_details",
            hook=TextLevel("Instrument Serif", 130, HOOK_COLOR, "center"),
            heading=TextLevel("Instrument Serif", 82, HEADING_COLOR, "center"),
            detail=TextLevel("Inter Regular", 37, DETAIL_COLOR, "center"),
            # Centred across; the vertical spot was not measured.
            anchor="bottom",
        ),
        TextDesign(
            id="B1",
            mode="running_guide",
            # b1_still0 is byte-identical to a3_still0: the teardown's B1 hook is A3's.
            hook=TextLevel("Instrument Serif", 130, HOOK_COLOR, "center"),
            # 49-51 px measured; Medium fits 51 by width, Bold 49-50. Weight left to KRI-1581.
            detail=TextLevel("Inter Medium", 50, DETAIL_COLOR, "center"),
            anchor="bottom",
        ),
    )
}


def default_design(*, asks_for_subtitles: bool) -> TextDesign:
    """The agent's pick before the creator chooses: B1 for a subtitle ask, A2 otherwise."""
    return TEXT_DESIGNS["B1" if asks_for_subtitles else "A2"]


def design_level_values(design_id: str, level: Literal["hook", "heading", "detail"]) -> dict:
    return TEXT_DESIGNS[design_id].level(level).style_values()
