"""KRI-1615 stage A: her own photos as cards in an intro scene, laid out together with the title.

An `IntroScene` names an arrangement (`intro_scene.ARRANGEMENTS`: rows of cards above the title
and under the speaker). This module turns it into card boxes and the title rows placed between
them, around the speaker and the captions it is given (`Stage`). No I/O and no settings; the title
is measured and placed by the real `phone_title_look.place_title_look`.

1. The title alone, clear of the face and the captions: the size it takes and its height.
2. The card rows in the room that leaves: the top row above the title, which sits right above
   the face; the bottom row under the face, above or below the caption row.
3. The title again, with the cards in ``avoid``. It must come back clean, at the size it had
   alone, clear of every card and between the rows. Anything else declines (`PropsDecline`):
   the caller keeps today's title and shows no card.

Every box keeps the rules of the Talking card layout (`card_problems`, strict `boxes_intersect`):
inside the frame by `EDGE_INSET` (no bleed: KRI-1616), never on a face, the caption row (from
`CAPTION_BAND_TOP_FRAC` down when the band was not measured) or the Kria watermark, never on
another card, and the photo's own shape (a phone card has a width only). `place_card` spans the
full width and picks one band, so the rows here have their own cell fit.

The cards share the title's time. `phone_card_layout.shift_past_text_windows` keeps cards a
chooser placed off a title's window; these cards are laid out with the title, so it does not
apply to them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline.intro_scene import ARRANGEMENTS, CardRow, place_on_stage, placement_is_clean
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC
from app.pipeline.render_geometry import NormalizedBox
from app.services.phone_card_layout import (
    EDGE_INSET,
    GAP,
    MIN_WIDTH,
    WATERMARK_BOX,
    boxes_intersect,
)

_EPS = 1e-6
_TIE = 0.01  # areas within 1% count as equal (`phone_card_layout._TIE_FRACTION`)


@dataclass(frozen=True)
class Stage:
    """The speaker and captions the cards and the title are laid out around, in canvas fractions.

    ``faces``: every face on screen while the title shows (a caller without a sampled face passes
    a fallback box: never assume the face left). ``caption_band``: ``(top, bottom)`` of every
    caption drawn then, or ``None`` when it was not measured (the cards then stay above
    `CAPTION_BAND_TOP_FRAC`, so the phone never has to clamp them).
    """

    faces: tuple[NormalizedBox, ...]
    caption_band: tuple[float, float] | None = None

    @property
    def caption_box(self) -> NormalizedBox:
        top, bottom = self.caption_band or (CAPTION_BAND_TOP_FRAC, 1.0)
        return NormalizedBox(0.0, top, 1.0, bottom)


@dataclass(frozen=True)
class PropCard:
    """One card: ``slot`` ``prop_<n>`` holds the n-th photo the caller issued; ``box`` is canvas
    fractions, the photo's own shape. Drawn upright; ``z`` follows the slots (no card overlaps
    another, so it only orders the layers)."""

    slot: str
    box: NormalizedBox
    z: int

    @property
    def x_frac(self) -> float:
        return (self.box.left + self.box.right) / 2

    @property
    def y_frac(self) -> float:
        return (self.box.top + self.box.bottom) / 2

    @property
    def scale(self) -> float:
        return self.box.width


@dataclass(frozen=True)
class PropsLayout:
    arrangement: str
    cards: tuple[PropCard, ...]
    rows: list[dict[str, Any]]  # the title rows, placed between the cards
    receipt: dict[str, Any]  # `place_title_look`'s receipt for ``rows`` (clean)


@dataclass(frozen=True)
class PropsDecline:
    reason: str


def slot_name(index: int) -> str:
    return f"prop_{index + 1}"


# --- the rules every box keeps ------------------------------------------------------------


def card_problems(
    boxes: Sequence[NormalizedBox], aspects: Sequence[float], *, stage: Stage, canvas: Any
) -> list[str]:
    """Why these card boxes (slot order, one aspect each) may not be drawn; empty when they may."""
    ratio = canvas.width / canvas.height
    problems: list[str] = []
    for index, (box, aspect) in enumerate(zip(boxes, aspects, strict=True)):
        slot = slot_name(index)
        if (
            box.left < EDGE_INSET - _EPS
            or box.top < EDGE_INSET - _EPS
            or box.right > 1 - EDGE_INSET + _EPS
            or box.bottom > 1 - EDGE_INSET + _EPS
        ):
            problems.append(f"{slot}: off the frame {box.as_dict()}")
        if any(boxes_intersect(box, face) for face in stage.faces):
            problems.append(f"{slot}: on the face")
        if boxes_intersect(box, stage.caption_box):
            problems.append(f"{slot}: on the captions")
        if boxes_intersect(box, WATERMARK_BOX):
            problems.append(f"{slot}: on the watermark")
        if box.height <= 0 or abs(box.width * ratio / box.height - aspect) > 1e-3 * aspect:
            problems.append(f"{slot}: not its photo's shape")
        if min(box.width, box.height / ratio) < MIN_WIDTH - _EPS:
            problems.append(f"{slot}: smaller than a card is drawn")
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            if boxes_intersect(boxes[a], boxes[b]):
                problems.append(f"{slot_name(a)} and {slot_name(b)}: on each other")
    return problems


# --- the card rows ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Spot:
    top: float
    bottom: float
    left: float
    right: float


def _spots(top: float, bottom: float, align: str) -> list[_Spot]:
    """The band across the frame, then the same band clear of the watermark: beside it (a
    centred row narrows on both sides, so a grid's columns stay over each other), above it or
    below it."""
    inner = (EDGE_INSET, 1.0 - EDGE_INSET)
    spots = [_Spot(top, bottom, *inner)]
    mark = WATERMARK_BOX
    if mark.top < bottom and top < mark.bottom:
        if align != "center":
            spots += [
                _Spot(top, bottom, mark.right + GAP, inner[1]),
                _Spot(top, bottom, inner[0], mark.left - GAP),
            ]
        elif mark.right < 0.5 or mark.left > 0.5:
            inset = mark.right + GAP if mark.right < 0.5 else 1.0 - mark.left + GAP
            spots.append(_Spot(top, bottom, inset, 1.0 - inset))
        spots += [_Spot(top, mark.top - GAP, *inner), _Spot(mark.bottom + GAP, bottom, *inner)]
    return [s for s in spots if s.bottom - s.top > _EPS and s.right - s.left > _EPS]


def _fit_row(
    row: CardRow, aspects: Sequence[float], spot: _Spot, ratio: float, max_height: float
) -> list[NormalizedBox]:
    """The row's cards side by side at one height, as tall as the spot, its width share and
    ``max_height`` allow; a top row hugs the spot's bottom (the title), a bottom row its top."""
    room = min(spot.right - spot.left, row.width_share * (1.0 - 2 * EDGE_INSET))
    gaps = GAP * (len(aspects) - 1)
    height = min(spot.bottom - spot.top, max_height, (room - gaps) * ratio / sum(aspects))
    widths = [aspect * height / ratio for aspect in aspects]
    total = sum(widths) + gaps
    x = {
        "left": spot.left,
        "center": (spot.left + spot.right - total) / 2,
        "right": spot.right - total,
    }[row.align]
    y = spot.bottom - height if row.band == "top" else spot.top
    boxes = []
    for width in widths:
        boxes.append(NormalizedBox(x, y, x + width, y + height))
        x += width + GAP
    return boxes


def _place_rows(
    rows: Sequence[CardRow],
    aspects: Sequence[float],
    bands: dict[str, list[tuple[float, float]]],
    *,
    same_height: bool,
    stage: Stage,
    canvas: Any,
) -> list[NormalizedBox] | None:
    """Every row in its biggest clear spot (slot order), or ``None`` when one does not fit."""
    ratio = canvas.width / canvas.height
    chosen: list[tuple[CardRow, list[float], _Spot, list[NormalizedBox]]] = []
    start = 0
    for row in rows:
        row_aspects = list(aspects[start : start + row.cards])
        start += row.cards
        fits = [
            (spot, boxes)
            for top, bottom in bands[row.band]
            for spot in _spots(top, bottom, row.align)
            if not card_problems(
                boxes := _fit_row(row, row_aspects, spot, ratio, 1.0),
                row_aspects,
                stage=stage,
                canvas=canvas,
            )
        ]
        if not fits:
            return None
        # The band across the frame comes first; a spot beside the watermark wins only when its
        # cards are really bigger, so equal fits keep a grid's columns over each other.
        best = max(sum(box.area for box in boxes) for _spot, boxes in fits)
        spot, boxes = next(f for f in fits if sum(b.area for b in f[1]) >= best * (1 - _TIE))
        chosen.append((row, row_aspects, spot, boxes))
    if same_height:
        height = min(boxes[0].height for _row, _aspects, _spot, boxes in chosen)
        chosen = [
            (row, row_aspects, spot, _fit_row(row, row_aspects, spot, ratio, height))
            for row, row_aspects, spot, _boxes in chosen
        ]
    return [box for _row, _aspects, _spot, boxes in chosen for box in boxes]


# --- the joint layout ---------------------------------------------------------------------


def _block(
    rows: Sequence[dict[str, Any]], canvas: Any
) -> tuple[NormalizedBox, list[NormalizedBox]]:
    from app.pipeline.phone_subtitled_title import _measure_title_box  # noqa: PLC0415
    from app.pipeline.phone_title_look import title_block_box  # noqa: PLC0415

    lines = [_measure_title_box(dict(row), canvas=canvas) for row in rows]
    return title_block_box(rows, canvas=canvas), lines


def layout_intro_props(
    arrangement: str,
    aspects: Sequence[float],
    rows: list[dict[str, Any]],
    *,
    stage: Stage,
    canvas: Any,
    position: str | None = None,
) -> PropsLayout | PropsDecline:
    """The cards of ``arrangement`` (``aspects``: width over height of each photo the caller
    issued, slot order) and ``rows`` (the compiled title, unplaced) placed together on
    ``stage``. Never raises a geometry problem: every one is a `PropsDecline`."""
    from app.pipeline.phone_title_look import _FACE_GAP  # noqa: PLC0415

    spec = ARRANGEMENTS.get(arrangement)
    if spec is None or not spec.rows:
        return PropsDecline("no_arrangement")
    aspects = [float(a) for a in aspects[: spec.slots]]
    if len(aspects) < spec.slots or any(not a > 0 for a in aspects):
        return PropsDecline("too_few_photos")
    if not stage.faces:
        return PropsDecline("no_face")
    captions = stage.caption_box
    alone_rows, alone = place_on_stage(rows, position, canvas, faces=stage.faces, avoid=[captions])
    if not placement_is_clean(alone):
        return PropsDecline("title_unclean")
    title, _lines = _block(alone_rows, canvas)
    face_top = min(face.top for face in stage.faces)
    face_bottom = max(face.bottom for face in stage.faces)
    bands = {
        "top": [(EDGE_INSET, face_top - _FACE_GAP - title.height - GAP)],
        "bottom": [
            (face_bottom + GAP, captions.top - GAP),
            (captions.bottom + GAP, 1.0 - EDGE_INSET),
        ],
    }
    boxes = _place_rows(
        spec.rows, aspects, bands, same_height=spec.same_height, stage=stage, canvas=canvas
    )
    if boxes is None:
        return PropsDecline("no_room_for_cards")
    if card_problems(boxes, aspects, stage=stage, canvas=canvas):
        return PropsDecline("card_off_limits")
    bands_of = [row.band for row in spec.rows for _ in range(row.cards)]
    above = [box for box, band in zip(boxes, bands_of, strict=True) if band == "top"]
    below = [box for box, band in zip(boxes, bands_of, strict=True) if band == "bottom"]
    # `place_title_look` lets a line cover 5% of an ``avoid`` box, and beside a narrow card a
    # line could slip into the card's row; the rows themselves, full width and padded, keep
    # the title between them, and the strict checks below decide.
    keep_out = [
        NormalizedBox(0.0, min(b.top for b in row) - GAP, 1.0, max(b.bottom for b in row) + GAP)
        for row in (above, below)
        if row
    ]
    placed, receipt = place_on_stage(
        rows, position, canvas, faces=stage.faces, avoid=[captions, *keep_out]
    )
    if not placement_is_clean(receipt):
        return PropsDecline("title_unclean_with_cards")
    if float(receipt.get("scale") or 0.0) < float(alone.get("scale") or 0.0) - _EPS:
        return PropsDecline("cards_shrink_the_title")
    block, lines = _block(placed, canvas)
    if any(boxes_intersect(line, box) for line in lines for box in boxes):
        return PropsDecline("title_touches_a_card")
    if any(box.bottom > block.top + _EPS for box in above) or any(
        box.top < block.bottom - _EPS for box in below
    ):
        return PropsDecline("title_not_between_the_cards")
    cards = tuple(PropCard(slot_name(i), box, i) for i, box in enumerate(boxes))
    return PropsLayout(arrangement, cards, placed, receipt)
