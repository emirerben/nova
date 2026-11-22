"""KRI-1585: the informative text designs a creator picks from (A1, A2, A3, B1), as data.

Every informative edit opens with a hook title; the facts then follow in the chosen design.
Mode A (``title_details``) gives some clips a heading and a detail line and leaves others bare.
Mode B (``running_guide``) puts one line on every clip, with no headings.

Sizes, spacing and spots are px and fractions of the 1080x1920 canvas, taken from the script
that drew the approved teardown mock-ups (its stills are byte-identical to the published ones;
docs/reviews/kri-1585/decisions.md, rows 3.1-3.3). Every level has the same soft shadow.

The shape a design draws (KRI-1581):

* Hook: one row, ``source_params.phone_text_role == "hook"``, at its own centred spot.
* Fact pair (Mode A): two research rows sharing ``source_params.text_stack_v1.stack_id``. The
  heading row has ``heading_level`` 2, the detail row 3. They are laid out as one block whose
  bottom sits at ``pair_bottom_frac``; the design's scrim rides on the pair's first row.
* Guide line (Mode B): one research row, laid out alone at ``pair_bottom_frac``.

A row joins a design through ``source_params.text_design``. `apply_text_design` writes the
design's values as design-tier fields (`informative_text.apply_design_values`), so a field the
creator set is never overwritten and a later design switch keeps her edits.

Built to become KRI-1566's title-look registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TextDesignId = Literal["A1", "A2", "A3", "B1"]
TextDesignMode = Literal["title_details", "running_guide"]
TextLevelName = Literal["hook", "heading", "detail"]

HOOK_COLOR = "#FFFFFF"
HEADING_COLOR = "#FFE7A0"
DETAIL_COLOR = "#FFFFFF"
HOOK_WIDTH_FRAC = 0.80
HOOK_Y_FRAC = 0.42
# Teardown round 1: the hook holds 0-3.4 s over the opening shot; facts start at 4 s.
HOOK_WINDOW_S = (0.0, 3.4)
FACTS_START_S = 4.0
# The mock-ups' soft shadow: a Pillow GaussianBlur(14) (sigma 14 px) dropped 3 px, black at 0.55.
SHADOW = {
    "shadow_enabled": True,
    "shadow_color": "#000000",
    "shadow_opacity": 0.55,
    "shadow_blur": 14.0,
    "shadow_offset_y": 3.0,
}
# The mock-up renderer's own line spacing where a block set none (ours defaults to 1.15).
MOCKUP_LINE_SPACING = 1.12


@dataclass(frozen=True)
class TextLevel:
    """One level of informative text (hook, heading, detail or guide line)."""

    font_family: str
    size_px: int
    color: str
    line_spacing: float = MOCKUP_LINE_SPACING
    text_case: Literal["upper"] | None = None
    letter_spacing: float | None = None

    def style_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "font_family": self.font_family,
            "size_px": self.size_px,
            "color": self.color,
            "line_spacing": self.line_spacing,
            **SHADOW,
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
    alignment: Literal["left", "center"] = "left"
    # Left edge (left alignment) or centre (centre alignment) of the facts.
    x_frac: float = 0.07
    max_width_frac: float = 0.80
    pair_bottom_frac: float = 0.70
    # Visible space between the heading's last line box and the detail's first.
    pair_gap_px: float = 10.0
    scrim: dict[str, Any] | None = None

    def level(self, name: TextLevelName) -> TextLevel:
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
            hook=TextLevel("Literata", 92, HOOK_COLOR, line_spacing=1.02),
            heading=TextLevel("Inter", 32, HEADING_COLOR, text_case="upper", letter_spacing=0.14),
            detail=TextLevel("Literata", 54, DETAIL_COLOR, line_spacing=1.08),
            # 14 px in the mock-up, after a heading line box at its 1.12 spacing (~5 px more).
            pair_gap_px=18.0,
        ),
        TextDesign(
            id="A2",
            mode="title_details",
            hook=TextLevel("Schibsted Grotesk", 100, HOOK_COLOR, line_spacing=1.02),
            heading=TextLevel("Schibsted Grotesk", 66, HEADING_COLOR, line_spacing=1.0),
            detail=TextLevel("Inter Medium", 36, DETAIL_COLOR),
            pair_gap_px=10.0,
            scrim={"top_frac": 0.48, "opacity": 0.6, "color": "#000000"},
        ),
        TextDesign(
            id="A3",
            mode="title_details",
            hook=TextLevel("Instrument Serif", 132, HOOK_COLOR, line_spacing=1.02),
            heading=TextLevel("Instrument Serif", 88, HEADING_COLOR, line_spacing=1.0),
            detail=TextLevel("Inter Medium", 36, DETAIL_COLOR, letter_spacing=0.02),
            alignment="center",
            x_frac=0.5,
            max_width_frac=0.84,
            pair_gap_px=8.0,
        ),
        TextDesign(
            id="B1",
            mode="running_guide",
            # b1_still0 is byte-identical to a3_still0: the teardown's B1 hook is A3's.
            hook=TextLevel("Instrument Serif", 132, HOOK_COLOR, line_spacing=1.02),
            detail=TextLevel("Inter", 50, DETAIL_COLOR),
            alignment="center",
            x_frac=0.5,
            max_width_frac=0.82,
            pair_bottom_frac=0.69,
        ),
    )
}


def default_design(*, asks_for_subtitles: bool) -> TextDesign:
    """The agent's pick before the creator chooses: B1 for a subtitle ask, A2 otherwise."""
    return TEXT_DESIGNS["B1" if asks_for_subtitles else "A2"]


def design_level_values(design_id: str, level: TextLevelName) -> dict:
    return TEXT_DESIGNS[design_id].level(level).style_values()


def row_level(row: dict, design: TextDesign) -> TextLevelName | None:
    """Which level of ``design`` a row is drawn in, or None for a row the design leaves alone."""
    from app.pipeline.informative_text import HOOK_ROLE  # noqa: PLC0415
    from app.pipeline.text_stack_layout import STACK_LAYOUT_PARAM  # noqa: PLC0415

    source = row.get("source_params") or {}
    if source.get("phone_text_role") == HOOK_ROLE:
        return "hook"
    if not isinstance(source.get("research"), dict):
        return None
    stack = source.get(STACK_LAYOUT_PARAM) or {}
    if design.heading is not None and stack.get("heading_level") == 2:
        return "heading"
    return "detail"


def _line_box_trailing_px(level: TextLevel) -> float:
    """The space a level's last line box holds below its ink box at the level's line spacing.

    The mock-ups anchor a block by the bottom of its last line box; the stack layout anchors
    the last line's own height, so the design's bottom is raised by the difference.
    """
    import skia  # noqa: PLC0415

    from app.pipeline.text_overlay_skia import _resolve_typeface_for_overlay  # noqa: PLC0415

    face = _resolve_typeface_for_overlay({"font_family": level.font_family}).typeface
    metrics = skia.Font(face, level.size_px).getMetrics()
    return (metrics.fDescent - metrics.fAscent) * (level.line_spacing - 1)


def apply_text_design(rows: list[dict]) -> list[dict]:
    """Style every hook and research row that names a design, in place.

    The hook keeps the design's spot (its position fields are design-tier, so placement leaves
    them). Facts get the design's face, size, colour, shadow and wrap width, and join a
    ``text_stack_v1`` group at the design's corner; the stack layout then measures each pair
    as one block. Rows naming no known design are left as they are.
    """
    from app.pipeline.informative_text import (  # noqa: PLC0415
        DESIGN_ID_KEY,
        apply_design_values,
        authored_fields,
    )
    from app.pipeline.text_stack_layout import STACK_LAYOUT_PARAM  # noqa: PLC0415

    scrimmed: set[str] = set()
    for row in rows:
        source = row.get("source_params") or {}
        design = TEXT_DESIGNS.get(str(source.get(DESIGN_ID_KEY) or ""))
        level = row_level(row, design) if design is not None else None
        if design is None or level is None:
            continue
        values = design.level(level).style_values()
        if level == "hook":
            values.update(
                position="custom",
                x_frac=0.5,
                y_frac=HOOK_Y_FRAC,
                alignment="center",
                max_width_frac=HOOK_WIDTH_FRAC,
            )
            apply_design_values(row, values, design.id)
            continue
        values.update(alignment=design.alignment, max_width_frac=design.max_width_frac)
        stack = dict((row.get("source_params") or {}).get(STACK_LAYOUT_PARAM) or {})
        stack_id = str(stack.get("stack_id") or row.get("id"))
        if design.scrim is not None and stack_id not in scrimmed:
            values["scrim"] = dict(design.scrim)
            scrimmed.add(stack_id)
        apply_design_values(row, values, design.id)
        stack.update(
            stack_id=stack_id,
            corner="bottom_left" if design.alignment == "left" else "bottom",
            bottom_frac=round(
                design.pair_bottom_frac - _line_box_trailing_px(design.detail) / 1920, 6
            ),
            shaped=True,
        )
        if level == "detail" and stack.get("heading_level") == 3:
            stack["gap_before_px"] = design.pair_gap_px
        row["source_params"][STACK_LAYOUT_PARAM] = stack
        # The corner's edge, not a recorded spot: the stack layout still places the block.
        if "x_frac" not in authored_fields(row):
            row.update(position="custom", x_frac=design.x_frac)
    return rows
