"""Qendresa item 20: a multi-line opening title with a face and colour per line.

She types a title as ``5 PROJECTS / the AI builder community / can't stop talking about!!!!``.
Each `` / `` is a line break. On a phone Talking edit the title becomes one ordinary
``TextElement`` row per line (so the iPhone renderer and editor need no change), stacked
as one block, kept off her face, with an optional logo sticker right above it.

Ways this could go wrong, and the guard for each:

* F1: a slash inside ordinary text ("24/7", "A/B testing") splits a title.
  `split_title_lines` only splits on a spaced slash or a newline and needs 2-5 parts of
  two or more characters.
* F2: her words are recased, reordered or dropped. Rows carry her exact text in her order;
  nothing here edits a word.
* F3: a per-line ask lands on the wrong line, or one line's colour leaks onto every line.
  `title_look_ask` maps style words to lines in order and keeps a colour to its own chunk.
* F4: rows overlap each other, run off the frame or cover her face. `place_title_look`
  measures every row, stacks them with a fixed gap, and only accepts a spot that is inside
  the frame and clear of every face and marked object; then it moves, then shrinks.
* F8: the logo covers the title, rises into the platform header, or sits on her face.
  `place_title_logo` anchors the logo's bottom edge above the block, slides it sideways,
  then shrinks it, and names the reason when nothing fits.
* F17: the approval check accepts a stack that is not her title. `stacked_title_matches`
  needs every line's exact text, shown together, in top-to-bottom order.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from app.pipeline import phone_subtitled_title as title_mod
from app.pipeline.cloud_render_evidence import normalize_text
from app.pipeline.narrated_title import TITLE_FONT_FAMILY, narrated_title_placement
from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

TITLE_LOOK_PARAM = "title_look"
_MIN_LINES = 2
_MAX_LINES = 5
_MIN_LINE_CHARS = 2

# The measured look (her V5, calibrated with the kit OCR on our renderer).
_LOOKS: dict[str, dict[str, Any]] = {
    "bold": {"font_family": "Bricolage Grotesque", "size_px": 145, "color": "#FFF1E6"},
    "serif": {
        "font_family": "Instrument Serif",
        "size_px": 100,
        "color": "#FFF0DF",
        "line_spacing": 0.95,
    },
    "handwritten": {"font_family": "Patrick Hand", "size_px": 105, "color": "#F5D893"},
}
_DEFAULT_KINDS = ("bold", "serif", "handwritten")
_ASKED_SIZE_PX = 120
_GAP_FRAC = 24 / 1920  # 24 px on a 1920-high canvas
_MAX_WIDTH_FRAC = 0.88
_LEFT_X_FRAC = 0.06
_HOLD_S = title_mod._TITLE_MAX_S

# Placement limits.
_X_MIN, _X_MAX = 0.03, 0.97
_Y_MIN, _Y_MAX = 0.06, 0.95
_BLOCK_TOP = 0.10
_FACE_GAP = 0.01
_MAX_COVERAGE = title_mod._MAX_FACE_COVERAGE
_SHRINK = (1.0, 0.85, 0.72, 0.6)
_Y_DECIMALS = 4

# Logo limits.
_LOGO_WIDTH = 0.32
_LOGO_MAX_HEIGHT = 0.16
_LOGO_GAP = 0.01
_LOGO_X_MIN, _LOGO_X_MAX = 0.02, 0.98
_LOGO_SHRINKS = (1.0, 0.8, 0.6)


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


# --- lines -------------------------------------------------------------------------------


def split_title_lines(title: str | None) -> list[str] | None:
    """Her typed lines, or ``None`` when the title is not a stack.

    Splits on newlines and on a spaced slash. Returns the parts only when there are 2-5 of
    them and each has at least two non-space characters. Words are never edited."""
    if not title or not title.strip():
        return None
    parts = [part.strip() for part in re.split(r"\n+|\s+/\s+", title.strip())]
    if not _MIN_LINES <= len(parts) <= _MAX_LINES:
        return None
    if any(len(part.replace(" ", "")) < _MIN_LINE_CHARS for part in parts):
        return None
    return parts


def _look_params(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    params = row.get("source_params")
    look = params.get(TITLE_LOOK_PARAM) if isinstance(params, Mapping) else None
    return look if isinstance(look, Mapping) else None


def is_title_look_row(row: Mapping[str, Any]) -> bool:
    return _look_params(row) is not None


def title_look_text(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Her title in typed form (`` / `` between lines), or ``None`` without look rows."""
    look_rows = [row for row in rows if is_title_look_row(row)]
    if not look_rows:
        return None
    look_rows.sort(key=lambda row: int(_look_params(row).get("line") or 0))  # type: ignore[union-attr]
    return " / ".join(str(row.get("text") or "") for row in look_rows)


# --- her ask -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TitleLookAsk:
    line_kinds: tuple[str | None, ...] = ()
    line_colors: tuple[str | None, ...] = ()
    font_family: str | None = None
    color: str | None = None
    position: str | None = None


_STYLE_WORDS = re.compile(
    r"\b(?P<bold>bold\s+caps|block\s+letters|bold|caps)\b"
    r"|(?<!sans )(?<!sans-)\b(?P<serif>serif)\b"
    r"|\b(?P<handwritten>hand[\s-]?written|handwriting|script)\b"
)
# The repo's colour names (`phone_text_style.explicit_text_rule`).
_COLOUR_WORDS = (
    ("white|beyaz", "#FFFFFF"),
    ("black|siyah", "#000000"),
    ("yellow|sar[ıi]", "#FFFF00"),
    ("red|k[ıi]rm[ıi]z[ıi]", "#FF0000"),
    ("blue|mavi", "#0000FF"),
)
_CHUNK_SPLIT = re.compile(r"[,;:\n.]|\s(?:and|then|ve)\s")
_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")
_TITLE_WORDS = re.compile(r"\b(?:title|heading|ba[şs]l[ıi]k\w*)\b")
_TOP_LEFT = re.compile(
    r"\b(?:title|heading)\b[^.;!?\n]{0,40}\btop[\s-]*left\b"
    r"|\btop[\s-]*left\b[^.;!?\n]{0,40}\b(?:title|heading)\b"
)
_RULE_POSITIONS = {"top_left", "top", "middle"}


def _colour_in(chunk: str) -> str | None:
    found = {color for words, color in _COLOUR_WORDS if re.search(rf"\b(?:{words})\b", chunk)}
    return next(iter(found)) if len(found) == 1 else None


def _per_line_styles(request_text: str, line_count: int) -> tuple[list[str], list[str | None]]:
    kinds: list[str] = []
    colors: list[str | None] = []
    # Her quoted title is text, not an ask ("bold" inside the quotes is a word).
    asked = re.sub(r'"[^"]*"|“[^”]*”', " ", request_text.casefold())
    # Only sentences about the title: "captions in bold, labels in script" style other text.
    about_title = " . ".join(
        sentence for sentence in _SENTENCE_SPLIT.split(asked) if _TITLE_WORDS.search(sentence)
    )
    for chunk in _CHUNK_SPLIT.split(about_title):
        found = [m.lastgroup for m in _STYLE_WORDS.finditer(chunk)]
        kinds.extend(kind for kind in found if kind)
        colors.extend([_colour_in(chunk)] if len(found) == 1 else [None] * len(found))
    if len(kinds) < 2 or len(kinds) > line_count:
        return [], []
    return kinds, colors


def title_look_ask(
    request_text: str, *, line_count: int, title_rule: Mapping[str, Any] | None = None
) -> TitleLookAsk:
    """What she asked of the title's look, from her own words and the parsed title rule."""
    rule = title_rule or {}
    text = request_text or ""
    kinds, colors = _per_line_styles(text, line_count)
    position = "top_left" if _TOP_LEFT.search(text.casefold()) else None
    if position is None and rule.get("position") in _RULE_POSITIONS:
        position = rule["position"]
    return TitleLookAsk(
        line_kinds=tuple(kinds),
        line_colors=tuple(colors) if any(colors) else (),
        font_family=rule.get("font_family") or None,
        # A whole-title colour read from the same words as per-line styles is the
        # per-line colour, not a colour for every line.
        color=None if kinds else (rule.get("color") or None),
        position=position,
    )


# --- rows --------------------------------------------------------------------------------


def _line_look(index: int, text: str, ask: TitleLookAsk) -> tuple[str, dict[str, Any]]:
    """The kind label and the font / size / colour fields for line ``index`` (0-based)."""
    from app.pipeline.phone_text_style import drawable_family  # noqa: PLC0415

    named = ask.line_kinds[index] if index < len(ask.line_kinds) else None
    if named in _LOOKS:
        kind, look = named, dict(_LOOKS[named])
    elif ask.font_family:
        kind = "asked"
        look = {**_LOOKS["bold"], "font_family": ask.font_family, "size_px": _ASKED_SIZE_PX}
    else:
        kind = _DEFAULT_KINDS[index % len(_DEFAULT_KINDS)]
        look = dict(_LOOKS[kind])
    line_color = ask.line_colors[index] if index < len(ask.line_colors) else None
    look["color"] = line_color or ask.color or look["color"]
    look["font_family"] = drawable_family(look["font_family"], [text]) or TITLE_FONT_FAMILY
    return kind, look


def title_look_rows(
    title: str,
    *,
    duration_s: float | None,
    first_word_end_s: float | None,
    timeline_duration_s: float,
    canvas: _Canvas,
    ask: TitleLookAsk,
) -> list[dict] | None:
    """One TextElement row per line at a provisional spot, or ``None`` for a plain title."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    lines = split_title_lines(title)
    if lines is None:
        return None
    hold = float(duration_s) if duration_s is not None else _HOLD_S
    end_s = round(min(hold, float(timeline_duration_s)), 3)
    if end_s < title_mod._TITLE_MIN_S / 5:
        return None
    left = ask.position == "top_left"
    rows: list[dict] = []
    for index, text in enumerate(lines):
        kind, look = _line_look(index, text, ask)
        base = narrated_title_placement(text, canvas=canvas, explicit=True)
        element = TextElement(
            id=title_mod.TALKING_TITLE_ELEMENT_ID
            if index == 0
            else f"{title_mod.TALKING_TITLE_ELEMENT_ID}-line-{index + 1}",
            text=text,
            start_s=0.0,
            end_s=end_s,
            role="generative_intro",
            effect="fade-in",
            **{
                **base,
                **look,
                "alignment": "left" if left else "center",
                "x_frac": _LEFT_X_FRAC if left else 0.5,
                "y_frac": 0.15,
                "max_width_frac": _MAX_WIDTH_FRAC,
            },
            source_params={
                "source": title_mod.TALKING_TITLE_SOURCE,
                TITLE_LOOK_PARAM: {"line": index + 1, "lines": len(lines), "style": kind},
            },
        )
        rows.append(element.model_dump(mode="json", exclude_none=True))
    return rows


# --- placement ---------------------------------------------------------------------------


def _measure(row: dict, canvas: _Canvas) -> NormalizedBox:
    return title_mod._measure_title_box(row, canvas=canvas)


def title_block_box(rows: Sequence[Mapping[str, Any]], *, canvas: _Canvas) -> NormalizedBox:
    """The union of the rows' measured boxes."""
    boxes = [_measure(dict(row), canvas) for row in rows]
    return NormalizedBox(
        min(b.left for b in boxes),
        min(b.top for b in boxes),
        max(b.right for b in boxes),
        max(b.bottom for b in boxes),
    )


def _stack(boxes: Sequence[NormalizedBox], top: float) -> list[NormalizedBox]:
    stacked = []
    for box in boxes:
        stacked.append(NormalizedBox(box.left, top, box.right, top + box.height))
        top += box.height + _GAP_FRAC
    return stacked


def _candidates(
    height: float, faces: Sequence[NormalizedBox], position: str | None
) -> list[tuple[str, float]]:
    """(kind, block top) in the order to try them."""
    first = 0.5 - height / 2 if position == "middle" else _BLOCK_TOP
    found = [("preset", first)]
    if faces:
        found.append(("below_face", max(f.bottom for f in faces) + _FACE_GAP))
        above = min(f.top for f in faces) - _FACE_GAP - height
        found.append(("above_face", above))
    return found


def _worst(rows: Sequence[NormalizedBox], others: Sequence[NormalizedBox]) -> float:
    return max((row.coverage_by(o) for row in rows for o in others), default=0.0)


def _inside(boxes: Sequence[NormalizedBox]) -> bool:
    return all(
        b.left >= _X_MIN - 1e-6
        and b.right <= _X_MAX + 1e-6
        and b.top >= _Y_MIN - 1e-6
        and b.bottom <= _Y_MAX + 1e-6
        for b in boxes
    )


def _sized(rows: Sequence[dict], scale: float) -> list[dict]:
    if scale == 1.0:
        return [dict(row) for row in rows]
    return [{**row, "size_px": max(1, round(float(row["size_px"]) * scale))} for row in rows]


def place_title_look(
    rows: list[dict],
    *,
    clip_path: str | None,
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: _Canvas,
    scale: float = 1.0,
    sample: Any = None,
    position_x: float = 0.0,
    avoid: Sequence[NormalizedBox] = (),
    objects: Sequence[Any] = (),
    protect_all_faces: bool = False,
    position: str | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Stack ``rows`` top to bottom in line order, clear of faces, objects and the frame edge.

    Tries the asked (or default) spot, then under the lowest face, then above the highest;
    when none fits it shrinks every row and retries. Never raises."""
    try:
        window = (min(float(r["start_s"]) for r in rows), max(float(r["end_s"]) for r in rows))
        faces, receipt = title_mod._face_boxes(
            clip_path,
            window=window,
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            scale=scale,
            sample=sample or title_mod.sample_face_regions,
            position_x=position_x,
            all_faces=protect_all_faces,
        )
        object_boxes, object_receipts = title_mod._object_boxes_for(
            objects,
            window=window,
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            canvas=canvas,
            scale=scale,
            position_x=position_x,
        )
        hard = [*faces, *object_boxes]
        base = [dict(r) for r in rows]
        if position == "top_left":
            base = [{**r, "alignment": "left", "x_frac": _LEFT_X_FRAC} for r in base]

        best: tuple[tuple[float, float], str, float, list[dict], list[NormalizedBox]] | None = None
        chosen = None
        for factor in _SHRINK:
            sized = _sized(base, factor)
            measured = [_measure(r, canvas) for r in sized]
            height = sum(b.height for b in measured) + _GAP_FRAC * (len(measured) - 1)
            fits: list[tuple[str, list[NormalizedBox], float]] = []
            for kind, top in _candidates(height, faces, position):
                boxes = _stack(measured, top)
                if not _inside(boxes):
                    continue
                coverage = _worst(boxes, hard)
                if best is None or (coverage, -factor) < best[0]:
                    best = ((coverage, -factor), kind, factor, sized, boxes)
                if coverage <= _MAX_COVERAGE:
                    fits.append((kind, boxes, coverage))
            if fits:
                clean = [f for f in fits if _worst(f[1], avoid) <= _MAX_COVERAGE]
                kind, boxes, coverage = (clean or fits)[0]
                chosen = (kind, factor, sized, boxes, coverage, not clean and bool(avoid))
                break
        if chosen is not None:
            kind, factor, sized, boxes, coverage, overlap = chosen
            status = (
                "shrunk"
                if factor < 1.0
                else {"preset": "preset", "below_face": "moved_below_face"}.get(kind, "moved")
            )
        else:
            if best is None:
                sized = _sized(base, _SHRINK[-1])
                measured = [_measure(r, canvas) for r in sized]
                boxes = _stack(measured, _BLOCK_TOP)
                factor, coverage = _SHRINK[-1], _worst(boxes, hard)
            else:
                _, _, factor, sized, boxes = best
                coverage = best[0][0]
            status, overlap = "best_effort", _worst(boxes, avoid) > _MAX_COVERAGE
        placed = [
            {**row, "y_frac": round((box.top + box.bottom) / 2, _Y_DECIMALS)}
            for row, box in zip(sized, boxes, strict=True)
        ]
        block = NormalizedBox(
            min(b.left for b in boxes),
            min(b.top for b in boxes),
            max(b.right for b in boxes),
            max(b.bottom for b in boxes),
        )
        return placed, {
            **receipt,
            "status": status,
            "coverage": round(coverage, 5),
            "face_collision": coverage > _MAX_COVERAGE,
            "text_overlap": bool(overlap),
            "lines": len(placed),
            "block": block.as_dict(),
            "face_boxes": [f.as_dict() for f in faces],
            "faces": receipt.get("faces"),
            "scale": factor,
            "objects": object_receipts,
        }
    except Exception as exc:  # noqa: BLE001 - a placement nicety never blocks a render
        log.warning("phone_title_look.placement_failed", error=str(exc)[:200])
        return list(rows), {"status": "error", "error": str(exc)[:200]}


# --- logo --------------------------------------------------------------------------------


def logo_aspect(asset: Mapping[str, Any]) -> float:
    """Width / height of a pool asset from its ``aspect`` ("1:1", "16:9", "0.5625")."""
    raw = asset.get("aspect")
    try:
        if isinstance(raw, str) and ":" in raw:
            numerator, denominator = (float(part) for part in raw.split(":", 1))
            value = numerator / denominator
        else:
            value = float(raw)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    return value if value > 0 else 1.0


def place_title_logo(
    block: NormalizedBox,
    *,
    aspect: float,
    faces: Sequence[NormalizedBox],
    canvas: _Canvas,
    alignment: str = "center",
    avoid: Sequence[NormalizedBox] = (),
) -> tuple[dict | None, str | None]:
    """The logo's centre and width (fraction of canvas width) right above ``block``.

    Its bottom edge sits just above the block. When that covers a face it slides sideways,
    then shrinks. ``(None, "no_room_above_title")`` when nothing fits."""
    aspect = aspect if aspect > 0 else 1.0
    to_height = canvas.width / (aspect * canvas.height)  # logo height per unit of width
    width = min(_LOGO_WIDTH, _LOGO_MAX_HEIGHT / to_height)
    bottom = block.top - _LOGO_GAP
    others = [*faces, *avoid]
    for shrink in _LOGO_SHRINKS:
        w = math.floor(width * shrink * 10**_Y_DECIMALS) / 10**_Y_DECIMALS
        h = w * to_height
        top = bottom - h
        if top < _Y_MIN - 1e-6:
            continue
        home = block.left + w / 2 if alignment == "left" else (block.left + block.right) / 2
        for cx in (home, block.left + w / 2, block.right - w / 2, _Y_MIN + w / 2, 0.94 - w / 2):
            if cx - w / 2 < _LOGO_X_MIN - 1e-6 or cx + w / 2 > _LOGO_X_MAX + 1e-6:
                continue
            box = NormalizedBox(cx - w / 2, top, cx + w / 2, bottom)
            if _worst([box], others) <= _MAX_COVERAGE:
                return {
                    "x_frac": round(cx, _Y_DECIMALS),
                    "y_frac": round(top + h / 2, _Y_DECIMALS),
                    "scale": round(w, _Y_DECIMALS),
                }, None
    return None, "no_room_above_title"


# --- approval ----------------------------------------------------------------------------


def stacked_title_matches(
    rows: Sequence[Mapping[str, Any]], wanted: str, *, tolerance_s: float
) -> list[dict]:
    """The rendered stack that shows ``wanted`` line by line, or ``[]``.

    Needs a distinct non-caption row per line with her exact words, all starting (and all
    ending) together, stacked top to bottom in line order."""
    lines = split_title_lines(wanted)
    if lines is None:
        return []
    pool = [row for row in rows if not str(row.get("element_id") or "").startswith("caption-")]
    options = [
        [r for r in pool if normalize_text(r.get("text", "")) == normalize_text(line)]
        for line in lines
    ]
    if not all(options):
        return []
    for combo in itertools.product(*options):
        if len({id(r) for r in combo}) != len(combo):
            continue
        try:
            starts = [float(r["start_s"]) for r in combo]
            ends = [float(r["end_s"]) for r in combo]
            ys = [float(r["anchor_y"]) for r in combo]
        except (KeyError, TypeError, ValueError):
            continue
        if max(starts) - min(starts) > tolerance_s or max(ends) - min(ends) > tolerance_s:
            continue
        if any(lower <= upper for upper, lower in zip(ys, ys[1:], strict=False)):
            continue
        return [
            {
                "text": wanted,
                "start_s": starts[0],
                "end_s": min(ends),
                "role": None,
                "stacked_lines": [str(r.get("element_id")) for r in combo],
            }
        ]
    return []
