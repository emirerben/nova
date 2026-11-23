"""Q1 run 4: the stacked title look stays readable on the footage it lands on.

`phone_title_look` rows keep their own cream / yellow fills (`_style_rows_keeping_title_look`
skips the restyle), and the caption legibility pass reads captions only, so nothing looked at the
pixels under the title: cream text on her cream t-shirt shipped at about 1.2:1.

This pass measures each title-look line where the FINAL block is drawn (after the caption
clearing, before the compile) with `caption_legibility.worst_contrast` on the sampled take frames.
A line under 4.5:1 gets the outline the numbered list labels already carry (`stroke_color` +
`stroke_width` row fields, so the phone and the editor draw it with no new protocol), in the
ink that reads on its fill. A line she styled herself (colour or outline) is left alone.
`TITLE_LEGIBILITY_ENABLED=false` returns the rows untouched.

KRI-1583 (informative text, every phone writer): on a job stamped for informative designs the
resolver calls `settle_informative_text` for each hook and research row instead of picking a
fill per row. Each level keeps one colour for the whole video (hook white, heading #FFE7A0,
detail white). A row under 4.5:1 keeps that colour and gains the designs' soft shadow (an A2
pair already carries its scrim). Her colour or shadow always wins.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import structlog

from app.config import settings
from app.pipeline import caption_legibility as legibility
from app.pipeline import phone_subtitled_title as title_mod
from app.pipeline import phone_title_look as look_mod

log = structlog.get_logger(__name__)

TITLE_LEGIBILITY_VERSION = 1
# The label outline is 8 px on a 120 px line (`phone_list_labels.LABEL_STROKE_WIDTH`).
_MAX_STROKE_PX = 8.0
_STROKE_FRAC = 0.067
_MARK = "legibility_stroke"


def title_stroke_px(size_px: float) -> float:
    """Outline width for a title line of ``size_px``: the label's proportion, never thinner than
    a caption's."""
    return float(
        max(legibility.stroke_px_for(size_px), min(_MAX_STROKE_PX, size_px * _STROKE_FRAC))
    )


def _creator_styled(row: Mapping[str, Any]) -> bool:
    params = row.get("source_params") or {}
    authored = params.get("creator_style_fields") or []
    look = params.get(look_mod.TITLE_LOOK_PARAM) or {}
    # An outline this pass drew itself (marked) is not hers: a second compile decides again.
    own_outline = bool(look.get(_MARK))
    return (
        bool(look.get("colour_asked"))
        or "color" in authored
        or "stroke_width" in authored
        or (
            not own_outline
            and (row.get("stroke_width") is not None or row.get("stroke_color") is not None)
        )
    )


def _without_own_outline(row: dict) -> dict:
    params = dict(row.get("source_params") or {})
    look = dict(params.get(look_mod.TITLE_LOOK_PARAM) or {})
    if not look.pop(_MARK, False):
        return row
    params[look_mod.TITLE_LOOK_PARAM] = look
    bare = {k: v for k, v in row.items() if k not in ("stroke_color", "stroke_width")}
    return {**bare, "source_params": params}


def title_legibility_pass(
    rows: list[dict], frames: Sequence[legibility.GreyFrame], *, canvas: Any
) -> tuple[list[dict], dict[str, Any] | None]:
    """``rows`` with an outline on each title-look line whose measured contrast is under 4.5:1,
    plus the receipt. Rows that need nothing are the same objects; with the switch off, no
    title-look row, or no frame under the title, ``(rows, None)``. Never raises."""
    if not settings.title_legibility_enabled or not frames:
        return rows, None
    if not any(look_mod.is_title_look_row(row) for row in rows):
        return rows, None
    out: list[dict] = []
    lines: list[dict[str, Any]] = []
    try:
        for row in rows:
            if not look_mod.is_title_look_row(row):
                out.append(row)
                continue
            under = [
                f
                for f in frames
                if f.start_s < float(row["end_s"]) and float(row["start_s"]) < f.end_s
            ]
            box = title_mod._measure_title_box(row, canvas=canvas)
            contrast = legibility.worst_contrast(
                str(row["color"]), (box.left, box.top, box.right, box.bottom), under
            )
            entry: dict[str, Any] = {
                "id": row["id"],
                "contrast": None if contrast is None else round(contrast, 2),
                "treated": False,
            }
            lines.append(entry)
            if contrast is None or contrast >= legibility.MIN_CONTRAST:
                # An outline this pass drew on an earlier compile goes once the line reads
                # without it (the title moved, or the footage changed); hers always stays.
                out.append(_without_own_outline(row) if contrast is not None else row)
                continue
            if _creator_styled(row):
                entry["skipped"] = "creator_styled"
                out.append(row)
                continue
            ink = legibility.readable_ink(str(row["color"]))
            width = title_stroke_px(float(row["size_px"]))
            params = dict(row.get("source_params") or {})
            params[look_mod.TITLE_LOOK_PARAM] = {**params[look_mod.TITLE_LOOK_PARAM], _MARK: True}
            out.append({**row, "stroke_color": ink, "stroke_width": width, "source_params": params})
            entry.update(treated=True, stroke_color=ink, stroke_width=width)
    except Exception as exc:  # noqa: BLE001 - a legibility nicety never blocks a render
        log.warning("phone_title_legibility.failed", error=str(exc)[:200])
        return rows, None
    return out, {"version": TITLE_LEGIBILITY_VERSION, "frames": len(frames), "lines": lines}


def narrated_title_outline(
    row: dict, frames: Sequence[legibility.GreyFrame], *, canvas: Any
) -> tuple[dict, dict[str, Any] | None]:
    """Tahini run: the same measurement for the phone narrated opening title (one row, not a
    title look). ``row`` with a dark (or light) outline when its final block reads under 4.5:1
    on the frames behind it, plus the receipt. The style pass gives every row an outline width
    of 0, so only ``creator_style_fields`` say she set a colour or an outline herself (then the
    row is left alone). An outline this pass drew is marked and dropped again once the line
    reads without it. Switch off or no frame under the title: ``(row, None)``. Never raises."""
    if not settings.title_legibility_enabled or not frames:
        return row, None
    try:
        params = dict(row.get("source_params") or {})
        authored = set(params.get("creator_style_fields") or [])
        under = [
            f for f in frames if f.start_s < float(row["end_s"]) and float(row["start_s"]) < f.end_s
        ]
        box = title_mod._measure_title_box(row, canvas=canvas)
        contrast = legibility.worst_contrast(
            str(row["color"]), (box.left, box.top, box.right, box.bottom), under
        )
        entry: dict[str, Any] = {
            "id": row["id"],
            "contrast": None if contrast is None else round(contrast, 2),
            "treated": False,
        }
        receipt = {"version": TITLE_LEGIBILITY_VERSION, "frames": len(frames), "lines": [entry]}
        if contrast is None:
            return row, None
        if contrast >= legibility.MIN_CONTRAST:
            if params.pop(_MARK, False):
                bare = {k: v for k, v in row.items() if k != "stroke_color"}
                return {**bare, "stroke_width": 0, "source_params": params}, receipt
            return row, receipt
        if authored & {"color", "stroke_width"}:
            entry["skipped"] = "creator_styled"
            return row, receipt
        ink = legibility.readable_ink(str(row["color"]))
        # Whole pixels: the style pass refuses a fractional outline on a later restyle.
        width = round(title_stroke_px(float(row["size_px"])))
        params[_MARK] = True
        entry.update(treated=True, stroke_color=ink, stroke_width=width)
        return {**row, "stroke_color": ink, "stroke_width": width, "source_params": params}, receipt
    except Exception as exc:  # noqa: BLE001 - a legibility nicety never blocks a render
        log.warning("phone_title_legibility.narrated_failed", error=str(exc)[:200])
        return row, None


INFORMATIVE_LEGIBILITY_VERSION = 1
# Receipt on each informative row of a stamped job: its level, measured contrast and treatment.
LEGIBILITY_PARAM = "legibility"
# Fields the soft shadow writes on a job whose build predates `textDesigns` (protocol 55): the
# blur and offset would mark the layer for design paint, which such a build cannot decode.
_PLAIN_SHADOW_FIELDS = ("shadow_enabled", "shadow_color", "shadow_opacity")


def informative_level(row: Mapping[str, Any]) -> str:
    """``hook``, ``heading`` or ``detail`` for an informative row. A one-line-per-clip guide
    line is a detail. Read from the design layout when it ran, else from the writer's tag."""
    from app.pipeline.informative_text import HOOK_ROLE  # noqa: PLC0415
    from app.pipeline.text_stack_layout import STACK_LAYOUT_PARAM  # noqa: PLC0415

    source = row.get("source_params") or {}
    if source.get("phone_text_role") == HOOK_ROLE:
        return "hook"
    research = source.get("research")
    tag = research.get("design") if isinstance(research, dict) else None
    if (isinstance(tag, dict) and tag.get("level") == "heading") or (
        (source.get(STACK_LAYOUT_PARAM) or {}).get("heading_level") == 2
    ):
        return "heading"
    if isinstance(tag, dict) and tag.get("level") == "hook":
        return "hook"
    return "detail"


def _level_color(level: str) -> str:
    from app.pipeline import text_designs  # noqa: PLC0415

    return {
        "hook": text_designs.HOOK_COLOR,
        "heading": text_designs.HEADING_COLOR,
        "detail": text_designs.DETAIL_COLOR,
    }[level]


def _shadow_values(*, shapes: bool) -> dict[str, Any]:
    from app.pipeline.text_designs import SHADOW  # noqa: PLC0415

    return dict(SHADOW) if shapes else {k: SHADOW[k] for k in _PLAIN_SHADOW_FIELDS}


def settle_informative_text(
    row: dict,
    frames: Sequence[Mapping[str, Any]],
    *,
    wanted: Mapping[str, Any],
    asked: Mapping[str, Any],
    canvas: Any,
    shapes: bool,
    scrimmed: bool,
) -> None:
    """Give one informative row its level's colour and, when the footage under it reads below
    4.5:1, the soft shadow. In place.

    ``frames`` are the resolver's background samples overlapping the row (108x192 grey).
    ``wanted`` is the role's saved and asked rule, ``asked`` this turn's ask alone. A colour in
    either is hers. A shadow is hers only when she set it on the row or asked this turn: a
    saved "no shadow" is a preference, the tier a design's shadow already outranks
    (`informative_text`), and an unreadable row needs the shadow. ``scrimmed``: the row's
    pair carries a scrim. A row no frame covers is treated as unreadable, since nothing shows
    it is not.
    """
    from app.pipeline.informative_text import (  # noqa: PLC0415
        DESIGN_ID_KEY,
        authored_fields,
        creator_fields,
    )
    from app.pipeline.phone_text_background import HEIGHT, WIDTH  # noqa: PLC0415
    from app.pipeline.phone_text_style import measure_box  # noqa: PLC0415

    authored, hers = authored_fields(row), creator_fields(row)
    source = dict(row.get("source_params") or {})
    if (source.get(LEGIBILITY_PARAM) or {}).get("treatment") == "shadow":
        for field in _shadow_values(shapes=True):
            if field not in authored and field != "shadow_enabled":
                row.pop(field, None)
    level = informative_level(row)
    if "color" not in wanted and "color" not in authored:
        row["color"] = _level_color(level)
    under = [
        legibility.GreyFrame(f["pixels"], WIDTH, HEIGHT, float(f["start_s"]), float(f["end_s"]))
        for f in frames
    ]
    contrast = legibility.worst_contrast(str(row["color"]), measure_box(row, canvas), under)
    if contrast is not None and contrast >= legibility.MIN_CONTRAST:
        treatment = "none"
    elif "shadow_enabled" in hers or "shadow_enabled" in asked:
        treatment = "creator"
    elif scrimmed or (DESIGN_ID_KEY in source and row.get("shadow_enabled") is True):
        treatment = "design"
    elif row.get("shadow_enabled") is True:
        treatment = "preference" if wanted.get("shadow_enabled") is True else "design"
    elif not settings.title_legibility_enabled:
        treatment = "disabled"
    else:
        for field, value in _shadow_values(shapes=shapes).items():
            if field not in authored:
                row[field] = value
        if "shadow_color" not in authored:
            # A black shadow under her black text is no help: the ink reads against her fill.
            row["shadow_color"] = legibility.readable_ink(str(row["color"]))
        treatment = "shadow"
    source[LEGIBILITY_PARAM] = {
        "version": INFORMATIVE_LEGIBILITY_VERSION,
        "level": level,
        "contrast": None if contrast is None else round(contrast, 2),
        "frames": len(under),
        "treatment": treatment,
    }
    row["source_params"] = source
