"""Measured creator text stacks, shared by pinned creation and relative editor placement.

Semantic roles are resolved here into the ordinary explicit size/position fields.
Legacy corner lines have no marker and keep their existing layout and render path.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.pipeline.canvas import PORTRAIT, Canvas

STACK_LAYOUT_PARAM = "text_stack_v1"
HEADING_SIZES = {1: 72, 2: 52, 3: 40}
HEADING_GAP_PX = 24
DETAIL_GAP_PX = 8
GROUP_GAP_PX = 32


def row_canvas(row: dict) -> Canvas:
    metadata = (row.get("source_params") or {}).get(STACK_LAYOUT_PARAM) or {}
    return Canvas(
        int(metadata.get("canvas_width") or 1080), int(metadata.get("canvas_height") or 1920)
    )


def measure_text_block(row: dict, *, width: float | None = None) -> dict:
    """Use the burn's actual bundled face, wrapping and metrics at the authored size."""
    from app.pipeline import text_overlay_skia as renderer  # noqa: PLC0415

    width = row_canvas(row).width if width is None else width
    text = str(row.get("text") or "")
    case = row.get("text_case")
    if case == "upper":
        text = text.upper()
    elif case == "lower":
        text = text.lower()
    elif case == "title":
        text = text.title()
    face = renderer._resolve_typeface_for_overlay(row).typeface
    size = float(
        row.get("size_px")
        or renderer._resolve_font_size_px({"text_size": row.get("size_class") or "medium"})
    )
    spacing = renderer.resolve_letter_spacing_em(row.get("letter_spacing"))
    wrap = (
        renderer._authored_lines if row.get("wrap_lines") is False else renderer._wrap_at_fixed_size
    )
    font, size, lines = wrap(
        text, face, size, width * float(row.get("max_width_frac") or 0.84), spacing, shape_text=True
    )
    block = renderer._measure_block(
        font,
        lines,
        line_spacing=renderer.resolve_line_spacing(row.get("line_spacing")),
        letter_spacing_px=spacing * size,
        shape_text=True,
    )
    legacy_width = max(
        renderer._measure_line(font, line, spacing * size, shape_text=False) for line in lines
    )
    return {
        "height": float(block["block_h"]),
        "width": max(max(block["widths"]), legacy_width),
        "lines": lines,
    }


def stack_gap(previous: dict, current: dict) -> float:
    a = (previous.get("source_params") or {}).get(STACK_LAYOUT_PARAM) or {}
    b = (current.get("source_params") or {}).get(STACK_LAYOUT_PARAM) or {}
    if b.get("gap_before_px") is not None:
        return float(b["gap_before_px"])
    if a.get("stack_id") != b.get("stack_id"):
        return GROUP_GAP_PX
    return (
        DETAIL_GAP_PX
        if a.get("heading_level") == 2 and b.get("heading_level") == 3
        else HEADING_GAP_PX
    )


def layout_corner_stack(rows: Sequence[dict], *, bottom: bool, canvas: Canvas = PORTRAIT) -> None:
    """Lay out groups in reading order, allocating space only for overlapping windows.

    A group is fitted uniformly, including its gaps. Separate windows may share
    the same region. A bottom group is anchored as a unit rather than reversing
    its heading hierarchy. Final rows preserve exact copy and individual timing.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        metadata = (row.get("source_params") or {})[STACK_LAYOUT_PARAM]
        metadata.update(canvas_width=canvas.width, canvas_height=canvas.height)
        groups.setdefault(str(metadata["stack_id"]), []).append(row)
    placed: list[tuple[list[dict], float, float]] = []
    ordered = list(groups.values())
    if bottom:
        ordered.reverse()
    safe_top, safe_bottom = canvas.height * 0.10, canvas.height * 0.90
    for members in ordered:
        bases = [float(row["size_px"]) for row in members]
        overlaps = [
            entry
            for entry in placed
            if any(
                a["start_s"] < b["end_s"] and b["start_s"] < a["end_s"]
                for a in members
                for b in entry[0]
            )
        ]
        boundary = (
            (
                min((entry[1] for entry in overlaps), default=safe_bottom)
                - (GROUP_GAP_PX if overlaps else 0)
            )
            if bottom
            else (
                max((entry[2] for entry in overlaps), default=safe_top)
                + (GROUP_GAP_PX if overlaps else 0)
            )
        )
        available = boundary - safe_top if bottom else safe_bottom - boundary
        scale = 1.0
        for _ in range(80):
            for row, base in zip(members, bases, strict=True):
                explicit = row["source_params"][STACK_LAYOUT_PARAM].get("explicit_size")
                row["size_px"] = base if explicit else int(base * scale)
            measured = [measure_text_block(row) for row in members]
            gaps = [stack_gap(a, b) * scale for a, b in zip(members, members[1:])]
            height = sum(item["height"] for item in measured) + sum(gaps)
            fits_width = all(
                item["width"] <= float(row["max_width_frac"]) * canvas.width + 0.01
                for row, item in zip(members, measured, strict=True)
            )
            if height <= available and fits_width:
                break
            scale *= 0.96
            default_bases = [
                base
                for row, base in zip(members, bases, strict=True)
                if not row["source_params"][STACK_LAYOUT_PARAM].get("explicit_size")
            ]
            if not default_bases or min(default_bases) * scale < 24:
                raise ValueError("That text stack does not fit in the phone-safe area")
        else:
            raise ValueError("That text stack does not fit in the phone-safe area")
        top = boundary - height if bottom else boundary
        cursor = top
        for index, (row, block) in enumerate(zip(members, measured, strict=True)):
            row["y_frac"] = round((cursor + block["height"] / 2) / canvas.height, 6)
            # Share the actual measured lines with every renderer. Rewrapping
            # with a different shaping mode can turn one line into two and
            # make a measured 8px group gap into an overlap.
            row["text"] = "\n".join(block["lines"])
            row["wrap_lines"] = False
            row["source_params"][STACK_LAYOUT_PARAM]["gap_before_px"] = (
                gaps[index - 1] if index else (GROUP_GAP_PX if overlaps else 0)
            )
            cursor += block["height"] + (gaps[index] if index < len(gaps) else 0)
        placed.append((members, top, top + height))
