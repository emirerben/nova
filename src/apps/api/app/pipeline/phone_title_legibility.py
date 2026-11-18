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
