"""Resolve phone text once, before recipe signing and editor-row persistence.

No text, ids or timing is changed. Actual font coverage and Skia shaping are
shared with the portable compiler. Existing recipes never enter this pass.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.pipeline.canvas import PORTRAIT, Canvas
from app.schemas.phone_text_style import (
    CAPTION_PREFERRED_FONT,
    CURATED_PHONE_FONTS,
    DEFAULT_SHADOW_ENABLED,
    DEFAULT_STROKE_WIDTH,
    TEXT_STYLE_ROLES,
    effective_text_rule,
)

_MEASUREMENTS: ContextVar[dict | None] = ContextVar("phone_text_measurements", default=None)


@contextmanager
def phone_text_measurement_scope():
    token = _MEASUREMENTS.set({"count": 0, "cache": {}})
    try:
        yield
    finally:
        _MEASUREMENTS.reset(token)


def _style_decline(message: str, *, alternative: str = "Choose another font or placement"):
    from app.services.creator_render_contract import CreatorRenderContractError

    return CreatorRenderContractError(
        message,
        decline_reason="needs_choice",
        field_path="phone_text_style",
        alternative=alternative,
    )


def undrawable_text(texts, fonts=CURATED_PHONE_FONTS, covers=None) -> tuple[str, str] | None:
    """(text, characters) for the first text holding characters none of ``fonts`` draws.

    Color emoji are the system emoji face's, never a bundled one's (KRI-1024), so they
    are never named. ``covers`` defaults to the shared Skia coverage check.
    """
    import regex

    from app.pipeline.font_coverage import drop_color_emoji

    covers = covers or _covers
    for text in texts:
        missing = dict.fromkeys(
            grapheme
            for grapheme in regex.findall(r"\X", drop_color_emoji(text))
            if not grapheme.isspace() and not any(covers(font, grapheme) for font in fonts)
        )
        if missing:
            return text, "".join(missing)
    return None


def undrawable_message(text: str, characters: str) -> str:
    from app.kria.reply_language import say

    # The characters lead: a draft's reply line shortens the reason after ~60 characters.
    return say(
        en=f'No phone font can draw "{characters}". Remove it or change "{text}".',
        tr=f'"{characters}" hiçbir telefon yazı tipiyle yazılamıyor. '
        f'Onu çıkar ya da "{text}" metnini değiştir.',
    )


def no_font_decline(texts, covers=None, *, fallback: str):
    """The refusal when the eligible fonts cannot draw every text (KRI-1024).

    Names the text and the characters when no curated phone font draws them at all
    (choosing another font cannot help); otherwise the caller's own ``fallback``.
    """
    found = undrawable_text(texts, CURATED_PHONE_FONTS, covers)
    if found is None:
        return _style_decline(fallback)
    return _style_decline(
        undrawable_message(*found), alternative="Remove that character or change the wording"
    )


def text_role(row: dict) -> str:
    key = str(row.get("id", "")).casefold()
    source = row.get("source_params") or {}
    if "pinned" in key or key.startswith("pin-") or row.get("role") == "pinned_text":
        return "pinned"
    if "lyric" in key or key.startswith("song-caption-") or source.get("source") == "song_caption":
        return "lyric"
    if "caption" in key or source.get("source") == "caption_cue":
        return "caption"
    if (
        "label" in key
        or source.get("phone_text_role") == "label"
        or row.get("role") in {"label", "context_label", "narration_label"}
    ):
        return "label"
    return "title"


@lru_cache(maxsize=2048)
def _covers(family: str, text: str) -> bool:
    from app.pipeline.unified_montage import skia_font_covers

    return skia_font_covers(
        family, "".join(character for character in text if not character.isspace())
    )


def drawable_family(family: str | None, texts, excluded=()) -> str | None:
    """`family` when it draws every text, else its glyph-safe partner (KRI-785).

    A face missing LETTERS of the text (Turkish ğ ş İ ı in Satisfy, Big Curls,
    Alte Haas Grotesk, Permanent Marker) sets the whole run in the same
    `font_coverage.glyph_safe_family` partner the cloud renderer and the iOS
    compiler use, stamped into the row so every client draws that face.
    Symbol-only gaps (an arrow) keep the existing pick-another-font path.
    None when neither draws the text.
    """
    if not family:
        return None
    texts = list(texts)
    if all(_covers(family, text) for text in texts):
        return family
    from app.pipeline.font_coverage import glyph_safe_family, missing_glyphs

    joined = " ".join(texts)
    if not any(ch.isalpha() for ch in missing_glyphs(family, joined)):
        return None
    safe = glyph_safe_family(family, joined)
    if (
        safe
        and safe != family
        and safe not in excluded
        and all(_covers(safe, text) for text in texts)
    ):
        return safe
    return None


def displayed_text(row: dict) -> str:
    from app.agents._schemas.text_element import apply_text_case

    return apply_text_case(str(row.get("text") or ""), row.get("text_case"))


def cursor_text(row: dict) -> str:
    phases = row.get("animation_phases") or {}
    if row.get("effect") not in {"typewriter", "stream-in"} and "typewriter" not in (
        phases.get("entrance"),
        phases.get("exit"),
    ):
        return ""
    return {"block": " ▮", "underscore": " _", "none": ""}.get(
        (row.get("motion") or {}).get("cursor_style", "bar"), " |"
    )


def coverage_text(row: dict) -> str:
    return displayed_text(row) + cursor_text(row)


@lru_cache(maxsize=1024)
def _cursor_width(family: str, size: int, cursor: str) -> float:
    import skia

    from app.pipeline.text_overlay_skia import _resolve_typeface_for_overlay

    return skia.Font(
        _resolve_typeface_for_overlay({"font_family": family}).typeface, size
    ).measureText(cursor)


@lru_cache(maxsize=1024)
def _glyph_overhang(family: str, size: int, text: str) -> float:
    import skia

    from app.pipeline.text_overlay_skia import _resolve_typeface_for_overlay

    # The text rides along so the overhang is measured on the glyph-safe face
    # the box draws (KRI-785).
    face = _resolve_typeface_for_overlay({"font_family": family, "text": text}).typeface
    font = skia.Font(face, size)
    glyphs = font.textToGlyphs("".join(character for character in text if not character.isspace()))
    return max(
        (
            max(0, -bound.left(), bound.right() - width)
            for bound, width in zip(font.getBounds(glyphs), font.getWidths(glyphs), strict=True)
        ),
        default=0,
    )


_READABLE_FLOOR_PX = 40
# Lowest size the title cascade may place; every later measurement of a title must accept it.
_TITLE_FLOOR_PX = 30
_CONNECTED_FLOOR_PX = 24


def measure_box(
    row: dict, canvas: Canvas = PORTRAIT, *, floor: int | None = None
) -> tuple[float, float, float, float]:
    import json

    from app.agents._schemas.text_element import TextElement
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.text_overlay_skia import measure_text_overlay_box

    if floor is None:
        floor = _TITLE_FLOOR_PX if text_role(row) == "title" else _READABLE_FLOOR_PX

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
    connected = (row.get("source_params") or {}).get("text_stack_v1")
    if rendered_size < (_CONNECTED_FLOOR_PX if connected else floor):
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
    import math

    from app.pipeline.text_motion_v2 import normalize_text_motion
    from app.pipeline.text_overlay_skia import _text_shadow_layers, _text_shadow_style

    rotation = math.radians(float(row.get("rotation_deg") or 0))
    if rotation:
        cx = float(row.get("x_frac", 0.5)) * canvas.width
        cy = float(row.get("y_frac", 0.5)) * canvas.height
        corners = [
            (
                (
                    cx
                    + (x * canvas.width - cx) * math.cos(rotation)
                    - (y * canvas.height - cy) * math.sin(rotation)
                )
                / canvas.width,
                (
                    cy
                    + (x * canvas.width - cx) * math.sin(rotation)
                    + (y * canvas.height - cy) * math.cos(rotation)
                )
                / canvas.height,
            )
            for x in (box[0], box[2])
            for y in (box[1], box[3])
        ]
        box = (
            min(x for x, _ in corners),
            min(y for _, y in corners),
            max(x for x, _ in corners),
            max(y for _, y in corners),
        )

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
    if row.get("font_family"):
        dx += (
            _glyph_overhang(row["font_family"], int(row.get("size_px") or 76), displayed_text(row))
            / canvas.width
        )
    # Match the portable compiler's enabled outline and shadow footprint.
    outline = float(row.get("stroke_width") or 0)
    shadows = _text_shadow_layers(_text_shadow_style(row))
    glow = 62 if float(row.get("glow_strength") or 0) > 0 else 0
    dx += (
        max(outline, glow, *(math.ceil(3 * shadow.sigma + abs(shadow.dx)) for shadow in shadows))
        / canvas.width
    )
    dy += (
        max(outline, glow, *(math.ceil(3 * shadow.sigma + abs(shadow.dy)) for shadow in shadows))
        / canvas.height
    )
    if row.get("font_family") and (cursor := cursor_text(row)):
        dx += (
            _cursor_width(row["font_family"], int(row.get("size_px") or 76), cursor) / canvas.width
        )
    return box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy


def _place_connected_stacks(rows, rules, obstacles_for, canvas):
    """Reflow the selected face as a group; never independently refit headings."""
    from app.pipeline.text_stack_layout import (
        STACK_LAYOUT_PARAM,
        layout_corner_stack,
        measure_text_block,
    )
    from app.schemas.text_style_intent import LABEL_ANCHORS

    corners = {}
    for row in rows:
        if not (row.get("source_params") or {}).get(STACK_LAYOUT_PARAM):
            continue
        authored = set(row["source_params"].get("creator_style_fields") or [])
        wanted = rules[text_role(row)].get("position")
        corner = row["source_params"][STACK_LAYOUT_PARAM].get("corner")
        zone = (
            corner.split("_", 1)[0]
            if corner
            else "bottom"
            if float(row.get("y_frac") or 0.5) >= 0.5
            else "top"
        )
        alignment = row.get("alignment", "left")
        if wanted and not authored & {"position", "x_frac", "y_frac", "alignment"}:
            x, _y, alignment = LABEL_ANCHORS[wanted]
            row.update(x_frac=x, alignment=alignment)
            zone = wanted.split("_", 1)[0]
        corners.setdefault((zone, alignment), []).append(row)
    fitted = []
    for (zone, alignment), members in corners.items():
        authored_position = any(
            set(row["source_params"].get("creator_style_fields") or [])
            & {"position", "x_frac", "y_frac"}
            for row in members
        )
        if not authored_position:
            for row in members:
                authored = set(row["source_params"].get("creator_style_fields") or [])
                if "size_px" in authored:
                    row["source_params"][STACK_LAYOUT_PARAM]["explicit_size"] = True
                row["text"] = row["source_params"][STACK_LAYOUT_PARAM].get(
                    "authored_text", row["text"]
                )
                row["wrap_lines"] = True
                if "max_width_frac" not in authored:
                    row["max_width_frac"] = min(float(row.get("max_width_frac") or 0.68), 0.68)
                if alignment == "right":
                    row["x_frac"] = min(float(row["x_frac"]), 0.82)
            try:
                layout_corner_stack(
                    members,
                    bottom=zone == "bottom",
                    canvas=canvas,
                    safe_top_frac=0.102,
                    safe_bottom_frac=0.78,
                )
                if zone == "middle":
                    tops = [
                        row["y_frac"] - measure_text_block(row)["height"] / canvas.height / 2
                        for row in members
                    ]
                    bottoms = [
                        row["y_frac"] + measure_text_block(row)["height"] / canvas.height / 2
                        for row in members
                    ]
                    shift = 0.5 - (min(tops) + max(bottoms)) / 2
                    for row in members:
                        row["y_frac"] += shift
            except ValueError as error:
                raise _style_decline(str(error)) from error
        for row in members:
            box = animation_box(row, measure_box(row, canvas), canvas)
            obstacles = [
                *obstacles_for(row),
                *(
                    animation_box(other, measure_box(other, canvas), canvas)
                    for other in fitted
                    if _same_window(row, other)
                ),
            ]
            if (
                box[0] < 0.05
                or box[2] > 0.84
                or box[1] < 0.095
                or box[3] > 0.79
                or any(_overlap(box, obstacle) for obstacle in obstacles)
            ):
                raise _style_decline("This text group cannot fit clearly in the requested area.")
        fitted.extend(members)
    return fitted


@dataclass(frozen=True)
class _FitStrategy:
    """One rung of the title fit cascade. Rungs only ever relax limits the creator did not set."""

    name: str
    max_width_frac: float
    size_floor: int
    height_cap: float
    keeps_authored_width: bool = False
    offsets: tuple[float, ...] = (0,)
    extra_positions: tuple[str, ...] = ()


# Tried in order; the first rung is today's behaviour so text that already fits is untouched.
# The widest wrap stays inside the safe box (swept x 0.05..0.84 after the anchor shift), the
# floor is the smallest title that is still legible on a 1080x1920 frame, and the taller
# block only opens once the text no longer fits at the normal height.
_TITLE_FIT_CASCADE: tuple[_FitStrategy, ...] = (
    _FitStrategy("baseline", 0.68, _READABLE_FLOOR_PX, 0.18, keeps_authored_width=True),
    _FitStrategy("wider_box", 0.76, _READABLE_FLOOR_PX, 0.18),
    _FitStrategy("lower_floor", 0.76, _TITLE_FLOOR_PX, 0.18),
    _FitStrategy("taller_block", 0.76, _TITLE_FLOOR_PX, 0.26),
    _FitStrategy(
        "more_anchors",
        0.76,
        _TITLE_FLOOR_PX,
        0.34,
        offsets=(0, 0.06, 0.12, 0.18),
        extra_positions=("top", "bottom", "bottom_right"),
    ),
)
_SINGLE_FIT = _TITLE_FIT_CASCADE[:1]

# Roles that choose their own spot; label and pinned keep the spot their lane gave the row.
AUTO_PLACED_ROLES = frozenset({"title", "caption", "lyric"})
PLACEMENT_CASCADE = ("bottom_left", "top_left", "middle", "top_right")


def _place(
    row: dict, *, role: str, wanted: dict, obstacles: list, canvas: Canvas, block_rows=None
) -> dict:
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
    if role not in AUTO_PLACED_ROLES and "position" not in wanted:
        return row
    positions = [wanted["position"]] if "position" in wanted else list(PLACEMENT_CASCADE)
    pinned_position = bool(authored & {"position", "x_frac", "y_frac", "alignment"})
    if pinned_position:
        positions = ["authored"]
    cascade = _TITLE_FIT_CASCADE if role == "title" else _SINGLE_FIT
    for strategy in cascade:
        if _fit_with(
            row,
            original=original,
            authored=authored,
            role=role,
            positions=[
                *positions,
                *(
                    []
                    if pinned_position
                    else [p for p in strategy.extra_positions if p not in positions]
                ),
            ],
            strategy=strategy,
            obstacles=obstacles,
            canvas=canvas,
            block_rows=block_rows,
            anchors=LABEL_ANCHORS,
        ):
            return row
    raise _style_decline(
        "This text cannot fit clearly in the requested area. "
        "Choose another placement or shorter text."
    )


def _fit_with(
    row: dict,
    *,
    original: dict,
    authored: set,
    role: str,
    positions: list,
    strategy: _FitStrategy,
    obstacles: list,
    canvas: Canvas,
    block_rows,
    anchors: dict,
) -> bool:
    """Place `row` under one cascade rung; True (row mutated) when some anchor and size fit."""

    def measure(candidate):
        if not block_rows:
            return measure_box(candidate, canvas, floor=strategy.size_floor)
        peers = [{**candidate, "text": peer["text"]} for peer in block_rows]
        boxes = [measure_box(peer, canvas, floor=strategy.size_floor) for peer in peers]
        fitted_size = min(peer["size_px"] for peer in peers)
        if fitted_size < candidate["size_px"]:
            candidate["size_px"] = fitted_size
            boxes = [
                measure_box({**candidate, "text": peer["text"]}, canvas, floor=strategy.size_floor)
                for peer in peers
            ]
        return (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )

    # Pins, labels and captions stack within their corner; a title only does once the cascade
    # has reached a rung that offers offsets.
    stack = (0, 0.06, 0.12, 0.18) if role != "title" else strategy.offsets
    candidates = [
        (position, offset)
        for position in positions
        for offset in (stack if position.startswith(("top", "bottom")) else (0,))
    ]
    for position, offset in candidates:
        if position == "authored":
            if original.get("position") != "custom":
                x, y, alignment = anchors.get(original.get("position"), anchors["middle"])
            else:
                x, y, alignment = (
                    original.get("x_frac", 0.5),
                    original.get("y_frac", 0.5),
                    original.get("alignment", "center"),
                )
        else:
            x, y, alignment = anchors[position]
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
        row["max_width_frac"] = (
            min(float(original.get("max_width_frac") or 0.68), strategy.max_width_frac)
            if strategy.keeps_authored_width
            else strategy.max_width_frac
        )
        if "max_width_frac" in authored:
            row["max_width_frac"] = original["max_width_frac"]
        sizes = (
            [int(original["size_px"])]
            if "size_px" in authored
            else range(
                max(strategy.size_floor, min(104, int(original.get("size_px") or 76))),
                strategy.size_floor - 1,
                -2,
            )
        )
        for size in sizes:
            row["size_px"] = size
            box = measure(row)
            height = box[3] - box[1]
            if position.startswith("bottom"):
                row["y_frac"] = round(y - height / 2, 5)
                box = measure(row)
            elif position.startswith("top"):
                row["y_frac"] = round(y + height / 2, 5)
                box = measure(row)
            swept = animation_box(row, box, canvas)
            if position != "authored" and alignment in {"left", "right"}:
                shift = (
                    max(0.0, 0.0501 - swept[0])
                    if alignment == "left"
                    else -max(0.0, swept[2] - 0.8399)
                )
                if shift:
                    row["x_frac"] += shift
                    box = measure(row)
                    swept = animation_box(row, box, canvas)
            if (
                swept[0] >= 0.05
                and swept[2] <= 0.84
                and swept[1] >= 0.095
                and swept[3] <= 0.79
                and height <= strategy.height_cap
                and not any(_overlap(swept, b) for b in obstacles)
            ):
                return True
    return False


def text_style_defaults() -> dict:
    """What each text role looks like when the creator has saved nothing (GET /me/memory).

    Built from the constants the resolver and caption path read, so the Text preferences
    screen cannot advertise a default the renderer no longer applies.
    """
    from app.schemas.text_style_intent import LABEL_ANCHORS, LABEL_DEFAULT_POSITION

    def toggle(on: bool) -> str:
        return "on" if on else "off"

    def role_defaults(role: str) -> dict:
        auto_placed = role == "all_text" or role in AUTO_PLACED_ROLES
        return {
            "outline": toggle(bool(DEFAULT_STROKE_WIDTH)),
            "shadow": toggle(DEFAULT_SHADOW_ENABLED),
            "color": "auto",
            "font": "auto",
            "font_hint": CAPTION_PREFERRED_FONT if role == "caption" else None,
            "placement": "auto" if auto_placed else "usual",
            "placement_order": list(PLACEMENT_CASCADE) if auto_placed else [],
            # Labels sit at the montage's default label spot. A corner text has no
            # account-wide default: the creator names its corner on each pin.
            "usual_position": LABEL_DEFAULT_POSITION if role == "label" else None,
        }

    return {
        "roles": {role: role_defaults(role) for role in TEXT_STYLE_ROLES},
        "position_anchors": {
            name: {"x": x, "y": y, "alignment": alignment}
            for name, (x, y, alignment) in LABEL_ANCHORS.items()
        },
        "color_hint": "White, black or gray, whichever reads best on each shot.",
    }


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
        if not float(wanted.get("stroke_width", 0)).is_integer():
            raise _style_decline("Text outlines use whole pixel widths. Choose a whole number.")
        role_rows = [row for row in rows if text_role(row) == role]
        texts = [
            coverage_text(row)
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
            (d for f in candidates if (d := drawable_family(f, texts, excluded)) is not None),
            None,
        )
        if family is None:
            raise no_font_decline(
                texts,
                fallback="The eligible fonts cannot draw all of this text. Choose another font.",
            )
        for row in role_rows:
            row["source_params"] = {
                **(row.get("source_params") or {}),
                "phone_text_style_version": 1,
            }
            row["font_family"] = family
            row["stroke_width"] = wanted.get("stroke_width", DEFAULT_STROKE_WIDTH)
            row["shadow_enabled"] = wanted.get("shadow_enabled", DEFAULT_SHADOW_ENABLED)
            if "color" in wanted:
                row["color"] = wanted["color"]
            source_fields = (row.get("source_params") or {}).get("creator_style_fields") or []
            if "glow_strength" not in source_fields:
                row["glow_strength"] = 0
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
            if not float(row["stroke_width"]).is_integer():
                raise _style_decline("Text outlines use whole pixel widths. Choose a whole number.")
            if "font_family" in source_fields and not _covers(
                row["font_family"], coverage_text(row)
            ):
                drawable = drawable_family(row["font_family"], [coverage_text(row)])
                if drawable is None:
                    raise no_font_decline(
                        [coverage_text(row)],
                        fallback="This font cannot draw all of your text. Choose another font.",
                    )
                row["font_family"] = drawable
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
    fitted = _place_connected_stacks(fixed, rules, obstacles_for, canvas)
    for row in fixed:
        if (row.get("source_params") or {}).get("text_stack_v1"):
            continue
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
