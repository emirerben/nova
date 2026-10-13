"""Resolve phone text once, before recipe signing and editor-row persistence.

No text, ids or timing is changed. Actual font coverage and Skia shaping are
shared with the portable compiler. Existing recipes never enter this pass.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache
from typing import Any

from app.pipeline.canvas import PORTRAIT, Canvas
from app.schemas.phone_text_style import CURATED_PHONE_FONTS, effective_text_rule

_MEASUREMENTS: ContextVar[dict | None] = ContextVar("phone_text_measurements", default=None)


@contextmanager
def phone_text_measurement_scope():
    token = _MEASUREMENTS.set({"count": 0, "cache": {}})
    try:
        yield
    finally:
        _MEASUREMENTS.reset(token)


def _style_decline(message: str):
    from app.services.creator_render_contract import CreatorRenderContractError

    return CreatorRenderContractError(
        message,
        decline_reason="needs_choice",
        field_path="phone_text_style",
        alternative="Choose another font or placement",
    )


def text_role(row: dict) -> str:
    key = str(row.get("id", "")).casefold()
    source = row.get("source_params") or {}
    if "pinned" in key or key.startswith("pin-") or row.get("role") == "pinned_text":
        return "pinned"
    if "lyric" in key or source.get("source") == "song_caption":
        return "lyric"
    if "caption" in key or source.get("source") == "caption_cue":
        return "caption"
    if "label" in key or row.get("role") in {"label", "context_label", "narration_label"}:
        return "label"
    return "title"


@lru_cache(maxsize=2048)
def _covers(family: str, text: str) -> bool:
    from app.pipeline.unified_montage import skia_font_covers

    return skia_font_covers(family, text)


def measure_box(row: dict, canvas: Canvas = PORTRAIT) -> tuple[float, float, float, float]:
    import json

    from app.agents._schemas.text_element import TextElement
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.text_overlay_skia import measure_text_overlay_box

    element = TextElement.model_validate({k: v for k, v in row.items() if k != "phone_text_style"})
    overlays = build_overlays_from_text_elements(
        [element], video_duration_s=float(element.end_s), independent_box_alignment=True
    )
    boxes = []
    budget = _MEASUREMENTS.get()
    for overlay in overlays:
        key = (canvas.width, canvas.height, json.dumps(overlay, sort_keys=True))
        cached = budget["cache"].get(key) if budget else None
        if cached is None:
            if budget:
                if budget["count"] >= 8192:
                    raise _style_decline(
                        "This video has too many complex text layouts. "
                        "Simplify a text lane and retry."
                    )
                budget["count"] += 1
            cached = measure_text_overlay_box(overlay, render_canvas=canvas, resolved_size=True)
            if budget and len(budget["cache"]) < 2048:
                budget["cache"][key] = cached
        boxes.append(cached)
    rendered_size = min(box.get("size_px", float(row.get("size_px") or 76)) for box in boxes)
    if rendered_size < 40:
        raise _style_decline(
            "This text cannot wrap at a readable size. Choose another font or shorter text."
        )
    authored = (row.get("source_params") or {}).get("creator_style_fields") or []
    if "size_px" in authored and rendered_size < float(row["size_px"]):
        raise _style_decline(
            "This text cannot fit at the requested size. Choose another size or placement."
        )
    row["size_px"] = int(rendered_size)
    return (
        min(b["left"] for b in boxes),
        min(b["top"] for b in boxes),
        max(b["right"] for b in boxes),
        max(b["bottom"] for b in boxes),
    )


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _same_window(a: dict, b: dict) -> bool:
    return float(a.get("start_s", 0)) < float(b.get("end_s", 0)) and float(
        b.get("start_s", 0)
    ) < float(a.get("end_s", 0))


def animation_box(row: dict, box, canvas: Canvas = PORTRAIT):
    """A conservative swept envelope from the actual native motion parameters."""
    from app.pipeline.text_motion_v2 import normalize_text_motion

    effect = row.get("effect") or "static"
    motion = normalize_text_motion(effect, row.get("motion"))
    travel = motion.travel_px * motion.intensity
    dx = travel / canvas.width if motion.direction in {"left", "right"} else 0
    dy = travel / canvas.height if motion.direction in {"up", "down"} else 0
    scale = (
        (0.25 if effect == "bounce" else 0.15) * motion.overshoot / 0.15 * motion.intensity
        if effect in {"pop-in", "bounce"}
        else 0
    )
    phases = row.get("animation_phases") or {}
    if phases.get("entrance") == "slide" or phases.get("exit") == "slide":
        dx = max(dx, 40 / canvas.width)
    if phases.get("loop") == "pulse":
        scale = max(scale, 0.04)
    if phases.get("loop") in {"bounce", "float"}:
        dy = max(dy, 12 / canvas.height)
    dx += (box[2] - box[0]) * scale / 2 + 0.006
    dy += (box[3] - box[1]) * scale / 2 + 0.006
    if effect == "staggered-slice":
        dx += 0.01
        dy += float(row.get("size_px") or 76) * 0.25 / canvas.height
    # Match the portable compiler's enabled outline and shadow footprint.
    outline = float(row.get("stroke_width") or 0)
    effect_padding = outline + (10 if row.get("shadow_enabled") else 0)
    dx += effect_padding / canvas.width
    dy += effect_padding / canvas.height
    return box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy


def _place(row: dict, *, role: str, wanted: dict, obstacles: list, canvas: Canvas) -> dict:
    from app.schemas.text_style_intent import LABEL_ANCHORS

    original = deepcopy(row)
    authored = set((row.get("source_params") or {}).get("creator_style_fields") or [])
    # Pins/captions retain their lane's established anchors unless the creator
    # asks otherwise. Reserve their complete measured rectangles for titles.
    if role in {"pinned", "label"} and "position" not in wanted:
        side = (
            "left"
            if float(row.get("x_frac") or 0.5) <= 0.3
            else "right"
            if float(row.get("x_frac") or 0.5) >= 0.7
            else ""
        )
        zone = "top" if float(row.get("y_frac") or 0.5) < 0.5 else "bottom"
        wanted = {**wanted, "position": zone + ("_" + side if side else "")}
    if role not in {"title", "caption", "lyric"} and "position" not in wanted:
        return row
    positions = (
        [wanted["position"]]
        if "position" in wanted
        else ["bottom_left", "top_left", "middle", "top_right"]
    )
    if authored & {"position", "x_frac", "y_frac", "alignment"}:
        positions = ["authored"]
    candidates = [
        (position, offset)
        for position in positions
        for offset in (
            (0, 0.06, 0.12, 0.18)
            if role != "title" and position.startswith(("top", "bottom"))
            else (0,)
        )
    ]
    for position, offset in candidates:
        if position == "authored":
            if original.get("position") != "custom":
                x, y, alignment = LABEL_ANCHORS.get(
                    original.get("position"), LABEL_ANCHORS["middle"]
                )
            else:
                x, y, alignment = (
                    original.get("x_frac", 0.5),
                    original.get("y_frac", 0.5),
                    original.get("alignment", "center"),
                )
        else:
            x, y, alignment = LABEL_ANCHORS[position]
        if position in {"bottom_left", "bottom", "bottom_right"}:
            y = 0.70 - offset  # above the actual wordmark, stacking within the corner
            if role == "pinned":
                y -= max(0.0, 0.86 - float(original.get("y_frac") or 0.86))
        elif position.startswith("top"):
            y += offset
        if alignment == "right" and position != "authored":
            x = 0.82  # provisional platform action-rail keep-out
        if alignment == "left" and position != "authored":
            x = 0.06
        row.update(position="custom", x_frac=x, y_frac=y, alignment=alignment)
        row["max_width_frac"] = min(float(original.get("max_width_frac") or 0.68), 0.68)
        if "max_width_frac" in authored:
            row["max_width_frac"] = original["max_width_frac"]
        sizes = (
            [int(original["size_px"])]
            if "size_px" in authored
            else range(min(104, int(original.get("size_px") or 76)), 39, -2)
        )
        for size in sizes:
            row["size_px"] = size
            box = measure_box(row, canvas)
            height = box[3] - box[1]
            if position.startswith("bottom"):
                row["y_frac"] = round(y - height / 2, 5)
                box = measure_box(row, canvas)
            elif position.startswith("top"):
                row["y_frac"] = round(y + height / 2, 5)
                box = measure_box(row, canvas)
            swept = animation_box(row, box, canvas)
            if position != "authored" and alignment in {"left", "right"}:
                shift = (
                    max(0.0, 0.0501 - swept[0])
                    if alignment == "left"
                    else -max(0.0, swept[2] - 0.8399)
                )
                if shift:
                    row["x_frac"] += shift
                    box = measure_box(row, canvas)
                    swept = animation_box(row, box, canvas)
            if (
                swept[0] >= 0.05
                and swept[2] <= 0.84
                and swept[1] >= 0.095
                and swept[3] <= 0.79
                and height <= 0.18
                and not any(_overlap(swept, b) for b in obstacles)
            ):
                return row
    raise _style_decline(
        "This text cannot fit clearly in the requested area. "
        "Choose another placement or shorter text."
    )


def resolve_phone_text_style(
    rows: list[dict],
    *,
    preferences: dict | None = None,
    requested: dict | None = None,
    selected_fonts: dict | None = None,
    excluded_boxes: list | None = None,
    background_frames: list | None = None,
    canvas: Canvas = PORTRAIT,
) -> list[dict]:
    if _MEASUREMENTS.get() is None:
        with phone_text_measurement_scope():
            return resolve_phone_text_style(
                rows,
                preferences=preferences,
                requested=requested,
                selected_fonts=selected_fonts,
                excluded_boxes=excluded_boxes,
                background_frames=background_frames,
                canvas=canvas,
            )
    rows = deepcopy(rows)
    original_rows = deepcopy(rows)
    preferences, requested, selected_fonts = (
        preferences or {},
        requested or {},
        selected_fonts or {},
    )
    rules: dict[str, dict[str, Any]] = {}
    for role in {text_role(row) for row in rows}:
        rules[role] = effective_text_rule(preferences, requested, role)
    for role, wanted in rules.items():
        role_rows = [row for row in rows if text_role(row) == role]
        texts = [
            str(row.get("text") or "")
            for row in role_rows
            if "font_family"
            not in ((row.get("source_params") or {}).get("creator_style_fields") or [])
        ]
        excluded = set(wanted.get("excluded_fonts") or [])
        required = wanted.get("font_family")
        favorites = wanted.get("favorite_fonts") or []
        pool = [f for f in dict.fromkeys([*favorites, *CURATED_PHONE_FONTS]) if f not in excluded]
        if favorites:
            pool = [f for f in pool if f in favorites] + [f for f in pool if f not in favorites]
        chosen_by_agent = selected_fonts.get(role)
        authored = next(
            (row.get("font_family") for row in role_rows if row.get("font_family") in pool), None
        )
        candidates = (
            [required]
            if required
            else list(
                dict.fromkeys(
                    [
                        f
                        for f in [chosen_by_agent, *([authored] if not favorites else []), *pool]
                        if f in pool
                    ]
                )
            )
        )
        family = next(
            (f for f in candidates if f and all(_covers(f, text) for text in texts)), None
        )
        if family is None:
            raise _style_decline(
                "The eligible fonts cannot draw all of this text. Choose another font."
            )
        for row in role_rows:
            row["source_params"] = {
                **(row.get("source_params") or {}),
                "phone_text_style_version": 1,
            }
            row["font_family"] = family
            row["stroke_width"] = wanted.get("stroke_width", 0)
            row["shadow_enabled"] = wanted.get("shadow_enabled", False)
            if "color" in wanted:
                row["color"] = wanted["color"]
            source_fields = (row.get("source_params") or {}).get("creator_style_fields") or []
            before = next(original for original in original_rows if original["id"] == row["id"])
            for field in source_fields:
                if (
                    field
                    in {
                        "font_family",
                        "color",
                        "stroke_width",
                        "shadow_enabled",
                        "size_px",
                        "alignment",
                        "max_width_frac",
                    }
                    and before.get(field) is not None
                ):
                    row[field] = before[field]
            if "font_family" in source_fields and not _covers(
                row["font_family"], str(row.get("text") or "")
            ):
                raise _style_decline("This font cannot draw all of your text. Choose another font.")
    # The watermark is measured in the reference frame; keep its padding too.
    from app.pipeline.phone_captions import watermark_keepout_rect

    mark = watermark_keepout_rect(canvas.width, canvas.height)
    watermark = (
        mark[0] / canvas.width,
        mark[1] / canvas.height,
        mark[2] / canvas.width,
        mark[3] / canvas.height,
    )

    def obstacles_for(row):
        return [
            watermark,
            *(
                other["box"] if isinstance(other, dict) else other
                for other in excluded_boxes or []
                if not isinstance(other, dict) or _same_window(row, other)
            ),
        ]

    fixed = [row for row in rows if text_role(row) != "title"]
    fitted = []
    for row in fixed:
        _place(
            row,
            role=text_role(row),
            wanted=rules[text_role(row)],
            obstacles=[
                *obstacles_for(row),
                *(
                    animation_box(other, measure_box(other, canvas), canvas)
                    for other in fitted
                    if _same_window(row, other)
                ),
            ],
            canvas=canvas,
        )
        fitted.append(row)
    placed: list[dict] = []
    for row in rows:
        if text_role(row) == "title":
            occupied = [
                animation_box(other, measure_box(other, canvas), canvas)
                for other in [*fixed, *placed]
                if _same_window(row, other)
            ]
            _place(
                row,
                role="title",
                wanted=rules["title"],
                obstacles=[*obstacles_for(row), *occupied],
                canvas=canvas,
            )
            placed.append(row)
    from app.pipeline.phone_text_background import automatic_fill

    for row in rows:
        authored = (row.get("source_params") or {}).get("creator_style_fields") or []
        frames = [frame for frame in background_frames or [] if _same_window(row, frame)]
        if frames and "color" not in rules[text_role(row)] and "color" not in authored:
            row["color"], _score = automatic_fill(measure_box(row, canvas), frames)
    from app.services.pipeline_trace import record_pipeline_event

    record_pipeline_event(
        "text",
        "phone_text_style_resolved",
        {
            "version": 1,
            "roles": sorted(rules),
            "row_count": len(rows),
            "measurement_count": _MEASUREMENTS.get()["count"],
            "font_fallback_roles": sorted(
                {
                    text_role(row)
                    for row in rows
                    if selected_fonts.get(text_role(row))
                    and selected_fonts[text_role(row)] != row["font_family"]
                }
            ),
            "layout_fallback_count": sum(
                row.get("position") != before.get("position")
                or row.get("size_px") != before.get("size_px")
                for row, before in zip(rows, original_rows, strict=True)
            ),
        },
    )
    return rows
