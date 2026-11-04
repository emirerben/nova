"""The creator's CTA ending for a phone Talking edit (item 14, Qendresa).

Her posts (V4/V5/V7) end the same way for the last ~3 s:

* a small white handwritten "comment" (box ~0.064-0.08 of frame height, centred x ~0.5),
* her keyword big and bold in cream (#FCF3C3, sampled) centred under it (box ~0.10-0.15
  high, gap ~0.015),
* the block sits just under her face (comment centre y ~0.65, keyword ~0.78),
* a white hand-drawn arrow (~9 px stroke) starting right of "comment", curling out to the
  right and down, ending in an open triangle that points right toward TikTok's buttons.

Today Kria draws one lime pill (`phone_subtitled_title.talking_closing_element`). This
module builds the two text rows and the arrow drawing. The caller wires them in and keeps
the lime tag when `build_cta_ending` returns None.

Placement: all geometry is in canvas pixels (so a landscape canvas does not distort).
The block (comment + keyword + arrow) tries, in order: just under the face, her spot
(top at 0.60), then every 0.02 step by distance from 0.60. The first spot clear of the
face and the captions wins; else the first clear of the face only (`text_overlap`);
else None.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.pipeline import phone_subtitled_title as pst
from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

CTA_LEAD_FONT = "Caveat Brush"
CTA_LEAD_COLOR = "#FFFFFF"
CTA_KEYWORD_FONT = "Bricolage Grotesque"
CTA_KEYWORD_COLOR = "#FCF3C3"
CTA_ARROW_ID = "closing-arrow"
CTA_KEYWORD_ELEMENT_ID = "closing-title-keyword"
CTA_PART_KEY = "cta_part"

_MIN_WINDOW_S = 0.5
_LEAD_SIZE_FRAC = 0.0625
_KEYWORD_SIZE_FRAC = 0.11
_KEYWORD_MIN_SIZE_FRAC = 0.055
_KEYWORD_MAX_WIDTH_FRAC = 0.62
_KEYWORD_ONE_LINE_HEIGHT = 1.3
_SHRINK_STEP = 0.9
_GAP_FRAC = 0.012
_DEFAULT_TOP_FRAC = 0.60
_SAFE_TOP = 0.06
_SAFE_BOTTOM = 0.94
_BELOW_FACE_GAP = 0.015
_MAX_COVERAGE = 0.05
_ARROW_WIDTH_FRAC = 0.009
_ARROW_DRAW_S = 0.6
_ARROW_RIGHT_MAX = 0.96
_ROW_LEFT_MIN = 0.04
_CURVE_POINTS = 24

_PARTS_RE = re.compile(r"^(comment)\s+(\S.*)$", re.IGNORECASE)


def cta_ending_parts(text: str | None) -> tuple[str, str] | None:
    """("comment", KEYWORD) from "comment KEYWORD", else None.

    The two parts joined by one space equal the whitespace-collapsed text exactly (the
    approved contract matches the creator's exact closing text), so the keyword keeps its
    punctuation and quotes. "Comment: READ" or a text wrapped in quotes is not split."""
    if not text:
        return None
    collapsed = " ".join(str(text).split())
    match = _PARTS_RE.match(collapsed)
    if match is None:
        return None
    keyword = match.group(2)
    if not any(ch.isalnum() for ch in keyword):
        return None
    if len(keyword.split()) > 3 or len(keyword) > 24:
        return None
    return match.group(1), keyword


_QUOTED_RE = re.compile(r'"[^"]*"|“[^”]*”')
_ARROW_RES = (
    re.compile(r"\barrows?\b", re.IGNORECASE),
    re.compile(r"\bok işaret", re.IGNORECASE),
    re.compile(r"\bbir ok\b", re.IGNORECASE),
    re.compile(r"\bok çiz", re.IGNORECASE),
    re.compile(r"\bok ekle\b", re.IGNORECASE),
    re.compile(r"\bok koy\b", re.IGNORECASE),
    re.compile(r"\boklu\b", re.IGNORECASE),
    re.compile(r"\bokla\b", re.IGNORECASE),
    # Not a bare "oku": it is also "read!", a likely keyword ("yorum yaz OKU").
    re.compile(r"[→↘⤵➡⬇↓➜➝↗]"),
)
# Refusals: "no arrow", "without an arrow", "don't add an arrow", and the Turkish "oksuz",
# "ok olmasın", "ok koyma", "ok ekleme".
_NEGATED_RES = (
    # Only plain refusals: "no arrow(s)", "without an arrow", "don't add an arrow". A wider
    # window would swallow real asks ("no captions and an arrow", "not just text but an
    # arrow", "don't forget the arrow").
    re.compile(r"\b(?:no|without)\s+(?:an?\s+|any\s+|the\s+)?arrows?\b", re.IGNORECASE),
    re.compile(
        r"\b(?:don['’]?t|do not|never)\s+(?:add|put|draw|use|include|want|need)\s+"
        r"(?:an?\s+|any\s+|the\s+)?arrows?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\boksuz", re.IGNORECASE),
    re.compile(r"\bok (?:olmas[ıi]n|koyma|ekleme)", re.IGNORECASE),
)


def requirement_asks_arrow(req: Any) -> bool:
    """Does this brief requirement ask for an arrow? Quoted text and the literal are
    the creator's words to show, not a request, so they are ignored; a negated mention
    ("no arrow", "oksuz") is not an ask."""
    try:
        pieces = [getattr(req, "description", None)]
        facts = getattr(req, "facts", None)
        if isinstance(facts, dict):
            pieces.extend(v for v in facts.values() if isinstance(v, str))
        literal = getattr(req, "literal", None)
        for piece in pieces:
            if not isinstance(piece, str) or not piece:
                continue
            text = _QUOTED_RE.sub(" ", piece)
            if isinstance(literal, str) and literal:
                text = re.sub(re.escape(literal), " ", text, flags=re.IGNORECASE)
            for rx in _NEGATED_RES:
                text = rx.sub(" ", text)
            if any(rx.search(text) for rx in _ARROW_RES):
                return True
    except Exception:  # noqa: BLE001 - detection never breaks a draft
        return False
    return False


_ENDING_NAMED_RES = (
    re.compile(r"\b(?:end|ending|ends|last|closing|close|finish|outro)\b", re.IGNORECASE),
    re.compile(r"\b(?:son|sonunda|sonda|kapanış|bitiş)", re.IGNORECASE),
)


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _is_about_ending(req: Any, closing_text: str) -> bool:
    target = _fold(closing_text)
    if not target:
        return False
    literal = getattr(req, "literal", None)
    if isinstance(literal, str) and _fold(literal) == target:
        return True
    facts = getattr(req, "facts", None)
    if isinstance(facts, dict):
        closing = facts.get("closing")
        if isinstance(closing, str) and _fold(closing) == target:
            return True
    description = getattr(req, "description", None)
    if not isinstance(description, str) or not description:
        return False
    if target in _fold(description):
        return True
    unquoted = _QUOTED_RE.sub(" ", description)
    return any(rx.search(unquoted) for rx in _ENDING_NAMED_RES)


def closing_arrow_asked(requirements: Iterable[Any], closing_text: str) -> bool:
    """An arrow asked for the ENDING: a requirement about the closing text (its literal or
    closing fact is that text, or its description names it or the ending) that asks for one.
    An arrow asked for other text ("MENU" pointing at the board) is not the closing arrow."""
    try:
        return any(
            _is_about_ending(req, closing_text) and requirement_asks_arrow(req)
            for req in requirements
        )
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class CtaEnding:
    rows: list[dict]
    drawings: list[dict]
    receipt: dict


def _row(
    *,
    element_id: str,
    text: str,
    part: str,
    font: str,
    color: str,
    size_px: int,
    start_s: float,
    end_s: float,
    x_frac: float,
    y_frac: float,
) -> dict:
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    element = TextElement(
        id=element_id,
        text=text,
        start_s=start_s,
        end_s=end_s,
        role="generative_intro",
        position="custom",
        x_frac=round(x_frac, 4),
        y_frac=round(y_frac, 4),
        font_family=font,
        size_px=size_px,
        color=color,
        shadow_enabled=True,
        shadow_color="#000000",
        shadow_opacity=0.35,
        effect="fade-in",
        source_params={"source": pst.TALKING_CLOSING_SOURCE, CTA_PART_KEY: part},
    )
    return element.model_dump(mode="json", exclude_none=True)


def _place(row: dict, canvas: Any, cx: float, centre_y: float) -> tuple[dict, NormalizedBox]:
    """Move ``row`` so its measured ink box is centred on (cx, centre_y); return the
    row and its final measured box. The box is re-measured, never assumed."""
    row = {**row, "x_frac": round(cx, 4), "y_frac": round(centre_y, 4)}
    box = pst._measure_title_box(row, canvas=canvas)
    for _ in range(2):
        dx = cx - (box.left + box.right) / 2
        dy = centre_y - (box.top + box.bottom) / 2
        if abs(dx) < 0.0005 and abs(dy) < 0.0005:
            break
        row = {
            **row,
            "x_frac": round(float(row["x_frac"]) + dx, 4),
            "y_frac": round(float(row["y_frac"]) + dy, 4),
        }
        box = pst._measure_title_box(row, canvas=canvas)
    return row, box


def _arrow_points(
    lead: NormalizedBox,
    keyword: NormalizedBox,
    *,
    width: float,
    height: float,
    h_scale: float,
) -> list[list[tuple[float, float]]]:
    """Curl + open-triangle head as pixel-space strokes.

    The phone draws each stroke as midpoint-smoothed curves through its points, so a
    corner inside a stroke renders as a loop: the head is three 2-point strokes (straight
    lines), and the curl turns gently between its points. The head clears the keyword's
    right edge; for a keyword wider than the room beside "comment" the curl runs above the
    keyword and ends at its top-right instead of crossing it."""
    sx = lead.right * width + 0.025 * width
    sy = (lead.top + lead.bottom) / 2 * height
    head_h = 0.018 * height
    ex = max(sx + 0.065 * width * h_scale, keyword.right * width + 0.02 * width)
    span = ex - sx

    def curl(c1, c2, ey):
        pts = []
        for i in range(_CURVE_POINTS + 1):
            t = i / _CURVE_POINTS
            u = 1 - t
            pts.append(
                (
                    u**3 * sx + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * ex,
                    u**3 * sy + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * ey,
                )
            )
        tip = (ex + 0.035 * width * h_scale, ey)
        top = (ex, ey - head_h)
        bottom = (ex, ey + head_h)
        return [pts, [top, tip], [tip, bottom], [bottom, top]]

    # The usual curl: right of "comment", down to the keyword's middle.
    ey = (keyword.top + keyword.bottom) / 2 * height + 0.01 * height
    strokes = curl(
        (sx + 1.385 * span, sy + 0.01 * height), (sx + 1.54 * span, ey - 0.04 * height), ey
    )
    if not _ink_crosses(strokes, keyword, width=width, height=height):
        return strokes
    # A keyword wider than the room beside "comment": the usual curl would cross it, so the
    # curl runs above the keyword and ends at its top-right.
    ey = max(sy, keyword.top * height - head_h - 0.004 * height)
    return curl((sx + 0.4 * span, sy - 0.03 * height), (sx + 0.85 * span, ey - 0.3 * (ey - sy)), ey)


def _ink_crosses(
    strokes: list[list[tuple[float, float]]], box: NormalizedBox, *, width: float, height: float
) -> bool:
    """Does the arrow's line (not its bounding box) touch ``box``? Each segment is walked in
    small steps against the box grown by the stroke's half width plus a little air."""
    pad = (_ARROW_WIDTH_FRAC * min(width, height)) / 2 + 0.006 * width
    left, right = box.left * width - pad, box.right * width + pad
    top, bottom = box.top * height - pad, box.bottom * height + pad
    for pts in strokes:
        for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
            for step in range(9):
                t = step / 8
                x, y = ax + (bx - ax) * t, ay + (by - ay) * t
                if left <= x <= right and top <= y <= bottom:
                    return True
    return False


def _build_arrow(
    strokes_px: list[list[tuple[float, float]]],
    *,
    width: float,
    height: float,
    start_s: float,
    end_s: float,
) -> tuple[dict, NormalizedBox]:
    from app.agents._schemas.drawing import DrawingElement  # noqa: PLC0415

    def norm(value: float, total: float) -> float:
        return round(min(1.0, max(0.0, value / total)), 4)

    strokes = [[(norm(x, width), norm(y, height)) for x, y in pts] for pts in strokes_px]
    xs = [x for pts in strokes for x, _ in pts]
    ys = [y for pts in strokes for _, y in pts]
    box = NormalizedBox(min(xs), min(ys), max(xs), max(ys))
    drawing = DrawingElement.model_validate(
        {
            "id": CTA_ARROW_ID,
            "start_s": start_s,
            "end_s": end_s,
            "canvas_aspect": width / height,
            "center_x": round((box.left + box.right) / 2, 4),
            "center_y": round((box.top + box.bottom) / 2, 4),
            "scale": 1.0,
            "rotation_deg": 0.0,
            "animation": {"entrance": "draw", "draw_duration_s": _ARROW_DRAW_S, "exit": "none"},
            "strokes": [
                {
                    "color": "#FFFFFF",
                    "opacity": 1.0,
                    "width_frac": _ARROW_WIDTH_FRAC,
                    "texture": "pen",
                    "points": pts,
                }
                for pts in strokes
            ],
        }
    )
    return drawing.model_dump(mode="json"), box


def _union(boxes: Sequence[NormalizedBox]) -> NormalizedBox:
    return NormalizedBox(
        min(b.left for b in boxes),
        min(b.top for b in boxes),
        max(b.right for b in boxes),
        max(b.bottom for b in boxes),
    )


def _clears(parts: Sequence[NormalizedBox], others: Sequence[NormalizedBox]) -> bool:
    return all(p.coverage_by(o) <= _MAX_COVERAGE for p in parts for o in others)


def build_cta_ending(
    text: str,
    *,
    arrow: bool,
    start_s: float,
    end_s: float,
    canvas: Any,
    clip_path: str | None,
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    scale: float = 1.0,
    position_x: float = 0.0,
    caption_boxes: Callable[[tuple[float, float]], Sequence[NormalizedBox]] | None = None,
    sample: Any = None,
) -> CtaEnding | None:
    """The CTA ending for ``text`` ("comment READ"), or None to keep the lime tag."""
    try:
        return _build(
            text,
            arrow=arrow,
            start_s=start_s,
            end_s=end_s,
            canvas=canvas,
            clip_path=clip_path,
            keep_segments=keep_segments,
            display_width=display_width,
            display_height=display_height,
            scale=scale,
            position_x=position_x,
            caption_boxes=caption_boxes,
            sample=sample,
        )
    except Exception as exc:  # noqa: BLE001 - fall back to the lime tag, never fail a draft
        log.warning("cta_ending_failed", error=str(exc)[:200])
        return None


def _build(
    text: str,
    *,
    arrow: bool,
    start_s: float,
    end_s: float,
    canvas: Any,
    clip_path: str | None,
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    scale: float,
    position_x: float,
    caption_boxes: Callable[[tuple[float, float]], Sequence[NormalizedBox]] | None,
    sample: Any,
) -> CtaEnding | None:
    from app.pipeline.phone_text_style import undrawable_text  # noqa: PLC0415

    parts = cta_ending_parts(text)
    if parts is None or end_s - start_s < _MIN_WINDOW_S:
        return None
    lead, keyword = parts
    if undrawable_text([lead], fonts=(CTA_LEAD_FONT,)) is not None:
        return None
    if undrawable_text([keyword], fonts=(CTA_KEYWORD_FONT,)) is not None:
        return None
    width, height = float(canvas.width), float(canvas.height)
    common = {"start_s": start_s, "end_s": end_s}

    lead_row = _row(
        element_id=pst.TALKING_CLOSING_ELEMENT_ID,
        text=lead,
        part="lead",
        font=CTA_LEAD_FONT,
        color=CTA_LEAD_COLOR,
        size_px=round(_LEAD_SIZE_FRAC * height),
        x_frac=0.5,
        y_frac=0.65,
        **common,
    )
    size = round(_KEYWORD_SIZE_FRAC * height)
    min_size = round(_KEYWORD_MIN_SIZE_FRAC * height)
    while True:
        keyword_row = _row(
            element_id=CTA_KEYWORD_ELEMENT_ID,
            text=keyword,
            part="keyword",
            font=CTA_KEYWORD_FONT,
            color=CTA_KEYWORD_COLOR,
            size_px=size,
            x_frac=0.5,
            y_frac=0.78,
            **common,
        )
        keyword_box = pst._measure_title_box(keyword_row, canvas=canvas)
        one_line = keyword_box.height * height <= _KEYWORD_ONE_LINE_HEIGHT * size
        if (keyword_box.width <= _KEYWORD_MAX_WIDTH_FRAC and one_line) or size <= min_size:
            break
        size = max(min_size, round(size * _SHRINK_STEP))
    lead_box = pst._measure_title_box(lead_row, canvas=canvas)

    faces, face_receipt = pst._face_boxes(
        clip_path,
        window=(start_s, end_s),
        keep_segments=keep_segments,
        display_width=display_width,
        display_height=display_height,
        canvas=canvas,
        scale=scale,
        sample=sample or pst.sample_face_regions,
        position_x=position_x,
    )
    captions = list(caption_boxes((start_s, end_s))) if caption_boxes else []

    def layout(top: float, cx: float, h_scale: float):
        gap = _GAP_FRAC
        lead_cy = top + lead_box.height / 2
        key_cy = top + lead_box.height + gap + keyword_box.height / 2
        l_row, l_box = _place(lead_row, canvas, cx, lead_cy)
        k_row, k_box = _place(keyword_row, canvas, cx, key_cy)
        drawing = None
        a_box = None
        if arrow:
            strokes = _arrow_points(l_box, k_box, width=width, height=height, h_scale=h_scale)
            drawing, a_box = _build_arrow(
                strokes, width=width, height=height, start_s=start_s, end_s=end_s
            )
        return l_row, l_box, k_row, k_box, drawing, a_box

    # Block height is the same at any top; measure once to size the candidates.
    probe = layout(_DEFAULT_TOP_FRAC, 0.5, 1.0)
    block_h = _union([b for b in (probe[1], probe[3], probe[5]) if b is not None]).height

    candidates: list[float] = []
    if faces and face_receipt.get("faces") != "fallback":
        # Just under a face the sampler saw. The unconfirmed fallback face (KRI-183's
        # centre-top box) ends at 0.52, above a real talking-head chin (hers reach ~0.60),
        # so with it the block starts at her own spot instead.
        candidates.append(max(f.bottom for f in faces) + _BELOW_FACE_GAP)
    candidates.append(_DEFAULT_TOP_FRAC)
    steps = []
    top = _SAFE_TOP
    while top <= _SAFE_BOTTOM - block_h + 1e-9:
        steps.append(round(top, 4))
        top += 0.02
    candidates.extend(sorted(steps, key=lambda t: abs(t - _DEFAULT_TOP_FRAC)))

    face_only = None
    chosen = None
    for top in candidates:
        if top < _SAFE_TOP - 1e-9 or top + block_h > _SAFE_BOTTOM + 1e-9:
            continue
        cx, h_scale = 0.5, 1.0
        result = layout(top, cx, h_scale)
        if arrow and result[5] is not None:
            # Keep the arrow inside the frame: slide left, then squeeze the curl.
            over = result[5].right - _ARROW_RIGHT_MAX
            if over > 0:
                room = result[1].left - _ROW_LEFT_MIN
                room = min(room, result[3].left - _ROW_LEFT_MIN)
                cx = 0.5 - min(over, max(room, 0.0))
                result = layout(top, cx, h_scale)
                over = result[5].right - _ARROW_RIGHT_MAX
                if over > 0:
                    span = result[5].right - result[1].right
                    h_scale = max(0.3, 1.0 - over / max(span, 1e-6))
                    result = layout(top, cx, h_scale)
        parts_boxes = [b for b in (result[1], result[3], result[5]) if b is not None]
        if not _clears(parts_boxes, faces):
            continue
        if _clears(parts_boxes, captions):
            chosen = (top, result, parts_boxes, False)
            break
        if face_only is None:
            face_only = (top, result, parts_boxes, True)
    chosen = chosen or face_only
    if chosen is None:
        return None
    top, (l_row, l_box, k_row, k_box, drawing, a_box), parts_boxes, overlap = chosen
    block = _union(parts_boxes)
    coverage = max((p.coverage_by(f) for p in parts_boxes for f in faces), default=0.0)
    receipt = {
        "status": "cta_ending",
        "lead": lead,
        "keyword": keyword,
        "arrow": drawing is not None,
        "chosen_top_frac": round(top, 4),
        "default_top_frac": _DEFAULT_TOP_FRAC,
        "block": block.as_dict(),
        "coverage": round(coverage, 5),
        "face_collision": False,
        "text_overlap": overlap,
        **face_receipt,
    }
    return CtaEnding(rows=[l_row, k_row], drawings=[drawing] if drawing else [], receipt=receipt)
