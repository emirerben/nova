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

Card modes (`CARD_MODES`). The layout above is ``clean``: cards never touch her. On the real
close Talking framings it fits about 1 edit in 13, so Emir ruled (2026-11-27): "it is okay for
cards to cover the face sometimes if it is shown for a short time". ``pop`` cards may cover her
face and body, each for at most `SHORT_CARD_S`, entering one after another; everything else
keeps the clean rules. The title is placed alone, exactly as without cards, and the cards go
around it. The offer (`intro_styles.fit_card_looks`) asks for ``pop`` only after every clean try
of an arrangement declined and one of them for ``no_room_for_cards``. These rules shape only what
the offer lays out: a card she resizes, retimes or drags after the offer is never checked against
them (Emir, 2026-11-27; the "her edits" tests in tests/routes/test_intro_cards_e2e.py). Failure
modes, continuing M1-M46 (`intro_scene`, `intro_styles`); guards in
tests/pipeline/test_intro_scene_props.py, tests/routes/test_intro_cards_e2e.py and
``make verify-intro-scenes`` / ``verify-intro-styles``:

* M47 pop where a clean layout exists. Every clean try runs first, as in stage B; a pop pass
  runs only for an arrangement no clean try fit, one of them for lack of room. The compile
  never pops on the stand-in speaker (``stage=None``: a typical face is never assumed, M45).
* M48 a pop card on the title. Cards keep off the title's block padded by `GAP`; the compile
  draws the title and measures its ink against every card.
* M49 a pop card on the caption row, the watermark or another card: strict `boxes_intersect`,
  cards `GAP` apart.
* M50 a pop card off the frame: every box `EDGE_INSET` inside (bleed is KRI-1616).
* M51 a pop card shown too long, past the title, or out of order. `CardMode.windows`: the first
  card enters `lead_s` after the title, each next one `stagger_s` later in slot order, each
  leaves after at most `SHORT_CARD_S` and never after the title. A card left with under
  `MIN_CARD_S` declines the look (``title_too_brief``).
* M52 her face centre not spared. At the look's width each card takes the free spot covering
  the least of her eyes and mouth (`face_centre`), then the least face, then the one nearest its
  slot's home (the arrangement's corners and rows at the frame's top and bottom).
* M53 cards shrink to chips. One width for every card of a look: the widest step of `POP_WIDTHS`
  (at most 0.45 of the frame) at which every card finds a spot; a spot never trades size.
* M54 the title moves, shrinks or is not a title the plain look would show. Its rows and receipt
  are the title placed alone, and the compile first runs the plain look's preflight.
* M55 two runs differ. No randomness: ties go to the nearer home, then the higher, then the
  further left spot.
* M56 a Turkish title, longer. Its measured block is kept out, so the cards go around it.
* M57 the Save refuses, moves or retimes a short card. The offer's dry run requires every card
  compiled where AND when it was offered (``card_moved``); ``caption_clear`` is decided over each
  card's own window, as the Save decides it.
* M58 the clean looks change. Clean cards keep the title's window exactly; a golden of stage B's
  card looks on a framing with room holds them byte for byte.
* M59 a photo that is not hers on a pop card: the Save path is stage B's, so it is refused.

Stickers and edge bleed (KRI-1616). A sticker arrangement (`intro_scene.Arrangement.stickers`)
puts her transparent pictures at anchors beside the title instead of photo rows: the ``sticker``
mode keeps them off her face for the title's whole window, ``sticker_pop`` (asked only when no
spot off her face exists) lets them cover her for a pop card's window. With ``bleed`` (a build at
`INTRO_EDGE_BLEED_PROTOCOL`) a ``pop`` or sticker card may hang past the side of the frame by up
to its mode's `CardMode.bleed` of its width; a sticker's home hangs `BLEED_HOME` past. All of it
is a preference of the offer: her own drag later is bounded only by the renderer
(`portable_visual.EDGE_BLEED_KEEP_ON_CANVAS`). Continuing the list:

* M60 bleed for a build that cannot draw or show it. ``bleed`` defaults off; the offer passes it
  only for a bleed build, and the clean and pop looks without it are stage B's byte for byte.
* M61 a card hangs further than its mode allows, or past the top or bottom. `card_problems`
  checks each side edge against ``bleed`` x the card's width; the top and bottom keep
  `EDGE_INSET` (the reach is centred well inside [0, 1], so every build draws it).
* M62 a sticker over the title, the caption row, the watermark or another sticker: the same
  keep-outs as a pop card, and the compile measures the drawn title ink against every sticker.
* M63 a sticker over her face for the title's whole window. ``sticker`` keeps every face out;
  only ``sticker_pop`` may cover her, and only for a pop card's short window.
* M64 a sticker the size of a card. `STICKER_WIDTHS` (0.30 down to 0.18 of the frame): one width
  for every sticker of a look, the widest at which each finds a spot.
* M65 a sticker drawn under a photo card. Stickers take z from `STICKER_Z` up.
* M66 a photo arrangement asked in a sticker mode, or the reverse: declined
  (``mode_not_for_arrangement``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline.intro_scene import (
    ARRANGEMENTS,
    Arrangement,
    CardRow,
    place_on_stage,
    placement_is_clean,
)
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
NO_ROOM = "no_room_for_cards"

# --- card modes ---------------------------------------------------------------------------

# Emir's "short time" (2026-11-27): no card that may cover her face shows longer than this.
SHORT_CARD_S = 1.5
# A card shows at least as long as the shortest title the product draws
# (`phone_subtitled_title._TITLE_MIN_S`); a shorter flash reads as a glitch.
MIN_CARD_S = 0.5
CLEAN = "clean"
POP = "pop"
STICKER = "sticker"
STICKER_POP = "sticker_pop"
STICKER_MODES = (STICKER, STICKER_POP)
# Stickers are decoration (Emir, 2026-11-27): a share of the frame's width, never a card's size.
STICKER_WIDTHS = (0.30, 0.26, 0.22, 0.18)
# Stickers draw over any photo card (an intro card's z follows its slot from 0).
STICKER_Z = 10
# How far past the side of the frame a card hangs at most, and a sticker at home, as a share of
# its own width (Emir: 20-35%).
BLEED_MAX = 0.35
BLEED_HOME = 0.25
# Pop card widths tried, widest first; every card of a look takes the same one.
POP_WIDTHS = (0.45, 0.40, 0.35, 0.30, 0.25, 0.20, MIN_WIDTH)


@dataclass(frozen=True)
class CardMode:
    """How a look's cards may meet her and when they show.

    ``over_her``: a card may cover her face and body (never the title, the caption row, the
    watermark, the frame edge or another card). ``max_width``: the widest a card may be, canvas
    width. ``lead_s``: the first card enters this long after the title; ``stagger_s``: each next
    card this long after the one before it, slot order; ``hold_s``: each card shows at most this
    long and never after the title leaves (``None``: exactly the title's window)."""

    over_her: bool
    max_width: float
    lead_s: float
    stagger_s: float
    hold_s: float | None
    # Widths tried, widest first, for a card placed spot by spot (pop, stickers); empty: rows.
    widths: tuple[float, ...] = ()
    # How far past the side of the frame a card may hang where the build allows bleed, a share
    # of its width (0: never).
    bleed: float = 0.0

    def windows(self, count: int, start: float, end: float) -> list[tuple[float, float]] | None:
        """Each card's ``(start, end)`` while the title shows over ``[start, end]``, slot order;
        ``None`` when a card would show under `MIN_CARD_S`."""
        if self.hold_s is None:
            return [(start, end)] * count
        out = []
        for index in range(count):
            enter = round(start + self.lead_s + index * self.stagger_s, 3)
            leave = min(end, round(enter + self.hold_s, 3))
            if leave - enter < MIN_CARD_S - _EPS:
                return None
            out.append((enter, leave))
        return out


CARD_MODES: dict[str, CardMode] = {
    CLEAN: CardMode(
        over_her=False, max_width=1.0 - 2 * EDGE_INSET, lead_s=0.0, stagger_s=0.0, hold_s=None
    ),
    # A short moodboard flash: the title lands first, the cards follow a beat apart.
    POP: CardMode(
        over_her=True,
        max_width=0.45,
        lead_s=0.25,
        stagger_s=0.25,
        hold_s=SHORT_CARD_S,
        widths=POP_WIDTHS,
        bleed=BLEED_MAX,
    ),
    STICKER: CardMode(
        over_her=False,
        max_width=STICKER_WIDTHS[0],
        lead_s=0.0,
        stagger_s=0.0,
        hold_s=None,
        widths=STICKER_WIDTHS,
        bleed=BLEED_MAX,
    ),
    STICKER_POP: CardMode(
        over_her=True,
        max_width=STICKER_WIDTHS[0],
        lead_s=0.25,
        stagger_s=0.25,
        hold_s=SHORT_CARD_S,
        widths=STICKER_WIDTHS,
        bleed=BLEED_MAX,
    ),
}
# Her eyes and mouth inside a saved face box (a padded union of detector boxes), as fractions of
# it: checked by eye on five real Talking stills (docs/pipelines/intro-scene.md).
FACE_CENTRE = (0.20, 0.25, 0.80, 0.85)
_COVER_TIE = 0.001  # frame area under which two coverages count as equal (about 45 x 45 px)


def face_centre(face: NormalizedBox) -> NormalizedBox:
    left, top, right, bottom = FACE_CENTRE
    return NormalizedBox(
        face.left + left * face.width,
        face.top + top * face.height,
        face.left + right * face.width,
        face.top + bottom * face.height,
    )


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
    another, so it only orders the layers). ``start_s``/``end_s``: when it shows, on the same
    clock as the rows it was laid out with (`CardMode.windows`)."""

    slot: str
    box: NormalizedBox
    z: int
    start_s: float
    end_s: float

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
    rows: list[dict[str, Any]]  # the title rows, placed with the cards
    receipt: dict[str, Any]  # `place_title_look`'s receipt for ``rows`` (clean)
    mode: str  # a `CARD_MODES` key


@dataclass(frozen=True)
class PropsDecline:
    reason: str


def slot_name(index: int, kind: str = "photo") -> str:
    return f"{'sticker' if kind == 'sticker' else 'prop'}_{index + 1}"


def side_reach(mode: str, width: float, *, bleed: bool) -> float:
    """How far past each side of the frame a card ``width`` wide may reach: negative is an inset."""
    return CARD_MODES[mode].bleed * width if bleed and CARD_MODES[mode].bleed else -EDGE_INSET


# --- the rules every box keeps ------------------------------------------------------------


def card_problems(
    boxes: Sequence[NormalizedBox],
    aspects: Sequence[float],
    *,
    stage: Stage,
    canvas: Any,
    mode: str = CLEAN,
    bleed: bool = False,
) -> list[str]:
    """Why these card boxes (slot order, one aspect each) may not be drawn; empty when they may."""
    rules = CARD_MODES[mode]
    ratio = canvas.width / canvas.height
    problems: list[str] = []
    for index, (box, aspect) in enumerate(zip(boxes, aspects, strict=True)):
        slot = slot_name(index)
        reach = side_reach(mode, box.width, bleed=bleed)
        if (
            box.left < -reach - _EPS
            or box.top < EDGE_INSET - _EPS
            or box.right > 1 + reach + _EPS
            or box.bottom > 1 - EDGE_INSET + _EPS
        ):
            problems.append(f"{slot}: off the frame {box.as_dict()}")
        if not rules.over_her and any(boxes_intersect(box, face) for face in stage.faces):
            problems.append(f"{slot}: on the face")
        if box.width > rules.max_width + _EPS:
            problems.append(f"{slot}: wider than a {mode} card")
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


_Placed = tuple[list[NormalizedBox], list[dict[str, Any]], dict[str, Any]]


def layout_intro_props(
    arrangement: str,
    aspects: Sequence[float],
    rows: list[dict[str, Any]],
    *,
    stage: Stage,
    canvas: Any,
    position: str | None = None,
    mode: str = CLEAN,
    bleed: bool = False,
) -> PropsLayout | PropsDecline:
    """The cards of ``arrangement`` (``aspects``: width over height of each picture the caller
    issued, slot order) and ``rows`` (the compiled title, unplaced) placed together on
    ``stage``, by the rules of ``mode`` (`CARD_MODES`), past the side of the frame only with
    ``bleed``. Never raises a geometry problem: every one is a `PropsDecline`."""
    from app.pipeline.title_family import title_window  # noqa: PLC0415

    spec = ARRANGEMENTS.get(arrangement)
    if spec is None or not spec.slots:
        return PropsDecline("no_arrangement")
    if (spec.kind == "sticker") != (mode in STICKER_MODES):
        return PropsDecline("mode_not_for_arrangement")  # M66
    aspects = [float(a) for a in aspects[: spec.slots]]
    if len(aspects) < spec.slots or any(not a > 0 for a in aspects):
        return PropsDecline("too_few_photos")
    if not stage.faces:
        return PropsDecline("no_face")
    windows = CARD_MODES[mode].windows(spec.slots, *title_window(rows))
    if windows is None:
        return PropsDecline("title_too_brief")
    alone_rows, alone = place_on_stage(
        rows, position, canvas, faces=stage.faces, avoid=[stage.caption_box]
    )
    if not placement_is_clean(alone):
        return PropsDecline("title_unclean")
    placed = (
        _spot_layout(
            spec, aspects, alone_rows, alone, stage=stage, canvas=canvas, mode=mode, bleed=bleed
        )
        if CARD_MODES[mode].widths
        else _clean_layout(
            spec, aspects, rows, alone_rows, alone, stage=stage, canvas=canvas, position=position
        )
    )
    if isinstance(placed, PropsDecline):
        return placed
    boxes, placed_rows, receipt = placed
    z_from = STICKER_Z if spec.kind == "sticker" else 0
    cards = tuple(
        PropCard(slot_name(i, spec.kind), box, z_from + i, *window)
        for i, (box, window) in enumerate(zip(boxes, windows, strict=True))
    )
    return PropsLayout(arrangement, cards, placed_rows, receipt, mode)


def _clean_layout(
    spec: Arrangement,
    aspects: list[float],
    rows: list[dict[str, Any]],
    alone_rows: list[dict[str, Any]],
    alone: dict[str, Any],
    *,
    stage: Stage,
    canvas: Any,
    position: str | None,
) -> _Placed | PropsDecline:
    """Card rows in the room the title alone leaves, then the title again between them."""
    from app.pipeline.phone_title_look import _FACE_GAP  # noqa: PLC0415

    captions = stage.caption_box
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
        return PropsDecline(NO_ROOM)
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
    return boxes, placed, receipt


# --- cards placed spot by spot: pop cards and stickers ---------------------------------------

NO_SPOT = "no_spot_for_cards"


def _spot_layout(
    spec: Arrangement,
    aspects: list[float],
    alone_rows: list[dict[str, Any]],
    alone: dict[str, Any],
    *,
    stage: Stage,
    canvas: Any,
    mode: str,
    bleed: bool,
) -> _Placed | PropsDecline:
    """The title exactly as it sits alone (M54) and the cards around it (M48-M53, M60-M64)."""
    title, lines = _block(alone_rows, canvas)
    boxes = _spot_boxes(
        spec, aspects, title=title, stage=stage, canvas=canvas, mode=mode, bleed=bleed
    )
    if boxes is None:
        return PropsDecline(NO_SPOT)
    if card_problems(boxes, aspects, stage=stage, canvas=canvas, mode=mode, bleed=bleed):
        return PropsDecline("card_off_limits")
    if any(boxes_intersect(line, box) for line in lines for box in boxes):
        return PropsDecline("title_touches_a_card")
    return boxes, alone_rows, alone


def _homes(spec: Arrangement, width: float, heights: Sequence[float]) -> list[tuple[float, float]]:
    """Each slot's home centre at ``width``: the arrangement's rows against the frame's top and
    bottom edges, aligned as the row says (around_speaker's corners, a grid's two rows)."""
    homes: list[tuple[float, float]] = []
    for row in spec.rows:
        total = row.cards * width + (row.cards - 1) * GAP
        left = {
            "left": EDGE_INSET,
            "center": (1.0 - total) / 2,
            "right": 1.0 - EDGE_INSET - total,
        }[row.align]
        for column in range(row.cards):
            height = heights[len(homes)]
            y = EDGE_INSET + height / 2 if row.band == "top" else 1.0 - EDGE_INSET - height / 2
            homes.append((left + column * (width + GAP) + width / 2, y))
    return homes


def _sticker_homes(
    spec: Arrangement, width: float, heights: Sequence[float], title: NormalizedBox, reach: float
) -> list[tuple[float, float]]:
    """Each sticker's home centre: against its side of the frame (hanging `BLEED_HOME` of its
    width past it when ``reach`` allows bleed), level with its corner of the title block."""
    home_reach = min(reach, BLEED_HOME * width) if reach > 0 else reach
    homes: list[tuple[float, float]] = []
    for anchor, height in zip(spec.stickers, heights, strict=True):
        x = 1.0 + home_reach - width / 2 if anchor.edge == "right" else width / 2 - home_reach
        y = (
            title.top - GAP - height / 2
            if anchor.corner == "top"
            else title.bottom + GAP + height / 2
        )
        homes.append((x, y))
    return homes


def _spot_boxes(
    spec: Arrangement,
    aspects: Sequence[float],
    *,
    title: NormalizedBox,
    stage: Stage,
    canvas: Any,
    mode: str,
    bleed: bool,
) -> list[NormalizedBox] | None:
    """Every card at the widest of its mode's widths where all of them find a spot, slot order."""
    rules = CARD_MODES[mode]
    ratio = canvas.width / canvas.height
    fixed = [title.padded(GAP), stage.caption_box, WATERMARK_BOX]
    if not rules.over_her:
        fixed += list(stage.faces)
    for width in rules.widths:
        heights = [width * ratio / aspect for aspect in aspects]
        if any(
            min(width, height / ratio) < MIN_WIDTH - _EPS or height > 1.0 - 2 * EDGE_INSET
            for height in heights
        ):
            continue
        reach = side_reach(mode, width, bleed=bleed)
        homes = (
            _sticker_homes(spec, width, heights, title, reach)
            if spec.kind == "sticker"
            else _homes(spec, width, heights)
        )
        boxes: list[NormalizedBox] = []
        for home, height in zip(homes, heights, strict=True):
            keep_out = [*fixed, *(box.padded(GAP) for box in boxes)]
            box = _best_spot(width, height, home, keep_out, stage=stage, canvas=canvas, reach=reach)
            if box is None:
                break
            boxes.append(box)
        else:
            return boxes
    return None


def _best_spot(
    width: float,
    height: float,
    home: tuple[float, float],
    keep_out: Sequence[NormalizedBox],
    *,
    stage: Stage,
    canvas: Any,
    reach: float = -EDGE_INSET,
) -> NormalizedBox | None:
    """The free ``width`` x ``height`` spot covering least of her face centre, then least face,
    then nearest ``home`` (M52), reaching ``reach`` past each side of the frame (`side_reach`).
    The candidates are the frame's, home's and every box's edges: coverage and clearance only
    change there."""
    centres = [face_centre(face) for face in stage.faces]
    edges = [*keep_out, *stage.faces, *centres]
    low_x, high_x = -reach, 1.0 + reach - width
    low_y, high_y = EDGE_INSET, 1.0 - EDGE_INSET - height
    xs = sorted(
        x
        for x in {low_x, high_x, home[0] - width / 2}
        | {edge.left - width for edge in edges}
        | {edge.right for edge in edges}
        if low_x - _EPS <= x <= high_x + _EPS
    )
    ys = sorted(
        y
        for y in {low_y, high_y, home[1] - height / 2}
        | {edge.top - height for edge in edges}
        | {edge.bottom for edge in edges}
        if low_y - _EPS <= y <= high_y + _EPS
    )
    best: tuple[tuple[int, int, float, float, float], NormalizedBox] | None = None
    for y in ys:
        for x in xs:
            box = NormalizedBox(x, y, x + width, y + height)
            if any(boxes_intersect(box, other) for other in keep_out):
                continue
            key = (
                round(sum(box.intersection_area(c) for c in centres) / _COVER_TIE),
                round(sum(box.intersection_area(f) for f in stage.faces) / _COVER_TIE),
                round(
                    math.hypot(
                        (x + width / 2 - home[0]) * canvas.width,
                        (y + height / 2 - home[1]) * canvas.height,
                    ),
                    3,
                ),
                y,
                x,
            )
            if best is None or key < best[0]:
                best = (key, box)
    return best[1] if best else None
