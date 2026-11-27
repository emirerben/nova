"""KRI-1615 stage A: photo cards laid out together with the intro title.

The scene names an arrangement only (no media id, no box); the engine fills ordinal slots from the
photos the caller issued and lays the cards and the title out together around the speaker. The
expected geometry comes from the requirement (the face, the caption row, the Kria watermark, the
frame inset, the title between the card rows, each photo's own shape), not from a run of the code.

Failure modes (intro_scene M19-M30) covered here:
* an arrangement nobody issued, or a grid from three photos, is accepted (M19);
* a media id or a coordinate reaches the scene (M20: the schema test keeps it closed);
* a mid-frame title is ignored instead of refused (M21);
* a card on the face, the captions or the watermark, cards on each other or off the frame
  (M22, M23);
* the title touches a card, leaves the band between the rows, or shrinks for them (M24, M25);
* a card drawn at another shape than its photo, portrait / landscape / square rows (M26, M27);
* a longer Turkish title, a title that cannot fit (M28, M29);
* a scene without an arrangement changes (M30).
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.pipeline import intro_scene as mod

TITLE_EN = "city guide: lisbon, day one"
TITLE_TR = "şehir rehberi: lizbon, ilk gün"
VOICES = ["accent", "accent", "solid", "solid", "solid"]


def _scene(**over: Any) -> mod.IntroScene:
    return mod.IntroScene.model_validate(
        {
            "decision": "compose",
            "voices": VOICES,
            "font_pair": "script_serif",
            "ink": "white",
            "zone": "above_head",
            "entrance": "fade",
            "sound": "none",
            **over,
        }
    )


# --- M19, M21, M30: the scene, the menu and the check (skia-free) ----------------------------


@pytest.mark.parametrize(
    ("photos", "issued"),
    [
        (0, {"none"}),
        (1, {"none"}),
        (2, {"none", "around_speaker"}),
        (3, {"none", "around_speaker"}),
        (4, {"none", "around_speaker", "grid_2x2"}),
        (9, {"none", "around_speaker", "grid_2x2"}),
    ],
)
def test_an_arrangement_is_issued_only_with_a_photo_for_every_slot(photos, issued):
    assert set(mod.issue_menu(TITLE_EN, photo_count=photos).arrangements) == issued


def test_a_grid_from_three_photos_is_refused():
    grid = _scene(arrangement="grid_2x2")
    three = mod.issue_menu(TITLE_EN, photo_count=3)
    assert mod.check_scene(grid, TITLE_EN, three) == "option_not_issued"
    assert mod.check_scene(grid, TITLE_EN, mod.issue_menu(TITLE_EN, photo_count=4)) is None


def test_an_arrangement_with_a_mid_frame_title_is_refused():
    menu = mod.issue_menu(TITLE_EN, photo_count=4)
    scene = _scene(arrangement="around_speaker", zone="mid_frame")
    assert mod.check_scene(scene, TITLE_EN, menu) == "arrangement_needs_a_top_zone"
    assert mod.check_scene(_scene(zone="mid_frame"), TITLE_EN, menu) is None
    left = _scene(arrangement="around_speaker", zone="top_left")
    assert mod.check_scene(left, TITLE_EN, menu) is None


def test_the_pick_the_offer_and_the_model_issue_no_arrangement():
    from app.agents.intro_scene_designer import build_input, response_schema_for

    assert mod.issue_menu(TITLE_EN).arrangements == ("none",)
    ask = build_input(TITLE_EN, language="en")
    assert ask is not None
    assert ask.menu().arrangements == ("none",)
    assert "arrangement" not in response_schema_for(ask)["properties"]
    scene = _scene(arrangement="grid_2x2")
    assert mod.check_scene(scene, TITLE_EN, ask.menu()) == "option_not_issued"


def test_a_scene_without_an_arrangement_dumps_and_declares_as_before():
    plain = _scene()
    assert "arrangement" not in plain.model_dump(mode="json")
    assert _scene(arrangement="none").model_dump(mode="json") == plain.model_dump(mode="json")
    grid = _scene(arrangement="grid_2x2")
    assert grid.model_dump(mode="json")["arrangement"] == "grid_2x2"
    assert mod.IntroScene.model_validate(grid.model_dump(mode="json")) == grid
    assert grid.moves() == plain.moves() == {}  # the title rows never declare the cards


# --- the joint layout (Skia: the title is measured and placed for real) -----------------------

skia = pytest.importorskip("skia")

from app.cli import verify_intro_scenes as check  # noqa: E402
from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC  # noqa: E402
from app.pipeline.phone_subtitled_title import _measure_title_box  # noqa: E402
from app.pipeline.phone_title_look import title_block_box  # noqa: E402
from app.services import intro_props_layout as props  # noqa: E402
from app.services.phone_card_layout import (  # noqa: E402
    EDGE_INSET,
    GAP,
    MIN_WIDTH,
    WATERMARK_BOX,
    boxes_intersect,
)

# A speaker centred with room above and below (refs 3 and 5 on the taste board), her captions
# measured under her: the stage `make verify-intro-scenes` draws the cards on.
ROOMY_FACE = check.ROOMY_FACE
CAPTIONS = (check.CAPTION_BOX.top, check.CAPTION_BOX.bottom)
ROOMY = check.ROOMY_STAGE
PORTRAIT_PHOTOS = (0.75, 0.75, 0.75, 0.75)
ASPECTS = {
    "portrait": PORTRAIT_PHOTOS,
    "square": (1.0, 1.0, 1.0, 1.0),
    "landscape": (1.5, 1.5, 1.5, 1.5),
    "mixed": (1.333, 0.75, 1.0, 1.0),
}
TITLES = {"en": TITLE_EN, "tr": TITLE_TR}


def _title_rows(title: str, language: str, **over: Any) -> tuple[list[dict], str | None]:
    out = mod.compile_intro_scene(
        _scene(**over), title, canvas=PORTRAIT, timeline_duration_s=30.0, language=language
    )
    assert out.rows is not None, out.reason
    return out.rows, out.position


def _layout(arrangement, aspects, language="en", *, stage=ROOMY, title=None, **over):
    rows, position = _title_rows(title or TITLES[language], language, **over)
    return props.layout_intro_props(
        arrangement, aspects, rows, stage=stage, canvas=PORTRAIT, position=position
    )


def _drawn_shape(box) -> float:
    return box.width * PORTRAIT.width / (box.height * PORTRAIT.height)


def _assert_drawable(layout, aspects, *, stage=ROOMY):
    """The contract, checked from the requirement: on the frame, off the face, the caption row,
    the watermark and each other, each the shape of its photo; the title clear of every card,
    under the top row and over the bottom row."""
    assert isinstance(layout, props.PropsLayout), layout
    top, bottom = stage.caption_band or (CAPTION_BAND_TOP_FRAC, 1.0)
    captions = mod.NormalizedBox(0.0, top, 1.0, bottom)  # the caption row spans the frame
    boxes = [card.box for card in layout.cards]
    assert [card.slot for card in layout.cards] == [f"prop_{n}" for n in range(1, len(boxes) + 1)]
    for box, aspect in zip(boxes, aspects, strict=False):
        assert EDGE_INSET - 1e-6 <= box.left and box.right <= 1 - EDGE_INSET + 1e-6, box
        assert EDGE_INSET - 1e-6 <= box.top and box.bottom <= 1 - EDGE_INSET + 1e-6, box
        assert not any(boxes_intersect(box, face) for face in stage.faces), box
        assert not boxes_intersect(box, captions), box
        assert not boxes_intersect(box, WATERMARK_BOX), box
        assert _drawn_shape(box) == pytest.approx(aspect, rel=1e-3)
    for a, b in itertools.combinations(boxes, 2):
        assert not boxes_intersect(a, b), (a, b)
    assert mod.placement_is_clean(layout.receipt), layout.receipt
    lines = [_measure_title_box(dict(row), canvas=PORTRAIT) for row in layout.rows]
    assert not any(boxes_intersect(line, box) for line in lines for box in boxes)
    block = title_block_box(layout.rows, canvas=PORTRAIT)
    face_top = min(face.top for face in stage.faces)
    above = [box for box in boxes if box.bottom <= face_top]
    below = [box for box in boxes if box.top >= max(face.bottom for face in stage.faces)]
    assert above and below and len(above) + len(below) == len(boxes)
    assert max(box.bottom for box in above) <= block.top
    assert block.bottom <= min(box.top for box in below)


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("aspects", sorted(ASPECTS), ids=str)
@pytest.mark.parametrize("arrangement", ["around_speaker", "grid_2x2"])
def test_cards_and_title_sit_clear_of_each_other_and_of_her(arrangement, aspects, language):
    photos = ASPECTS[aspects]
    _assert_drawable(_layout(arrangement, photos, language, stack="script_over_serif"), photos)


def test_slots_take_the_photos_in_the_order_issued():
    photos = (0.75, 1.0, 1.333, 0.8)
    grid = _layout("grid_2x2", photos, stack="script_over_serif")
    _assert_drawable(grid, photos)
    first, second, third, fourth = (card.box for card in grid.cards)
    assert first.right <= second.left and third.right <= fourth.left  # left to right
    assert first.bottom <= ROOMY_FACE.top <= ROOMY_FACE.bottom <= third.top  # top row first
    assert [card.z for card in grid.cards] == [0, 1, 2, 3]


def test_a_grid_is_two_rows_of_one_height_and_around_the_speaker_is_two_corners():
    grid = _layout("grid_2x2", PORTRAIT_PHOTOS)
    assert len({round(card.box.height, 6) for card in grid.cards}) == 1
    columns = [(round(c.box.left, 4), round(c.box.right, 4)) for c in grid.cards]
    assert columns[:2] == columns[2:]  # portrait photos: the columns sit over each other
    around = _layout("around_speaker", (1.333, 0.75))
    top, bottom = (card.box for card in around.cards)
    assert top.left == pytest.approx(EDGE_INSET) and bottom.right == pytest.approx(1 - EDGE_INSET)


def test_a_card_that_would_cover_the_watermark_moves_and_a_grid_stays_aligned():
    # Landscape photos make the bottom row wide enough to reach the wordmark's corner.
    photos = ASPECTS["landscape"]
    layout = _layout("grid_2x2", photos)
    _assert_drawable(layout, photos)
    centres = [round(card.x_frac, 4) for card in layout.cards]
    assert centres[:2] == centres[2:]


def test_without_a_measured_caption_band_every_card_ends_above_the_caption_cap():
    stage = props.Stage((mod.NormalizedBox(0.32, 0.36, 0.68, 0.48),), None)
    for arrangement, photos in (("grid_2x2", (1.0,) * 4), ("around_speaker", (1.0, 1.0))):
        layout = _layout(arrangement, photos, stage=stage)
        _assert_drawable(layout, photos, stage=stage)
        assert max(card.box.bottom for card in layout.cards) <= CAPTION_BAND_TOP_FRAC
    assert isinstance(
        _layout("grid_2x2", (1.0,) * 4, stage=props.Stage((ROOMY_FACE,))), props.PropsDecline
    )


def test_the_cards_take_the_room_the_title_leaves():
    """M28: a longer (Turkish) title wraps; the cards move up and stay clear, or decline."""
    short = _layout("grid_2x2", PORTRAIT_PHOTOS, "tr", stack="script_over_serif")
    longer = _layout(
        "grid_2x2",
        PORTRAIT_PHOTOS,
        "tr",
        title="şehir rehberi: lizbon, birinci gün",
        stack="script_over_serif",
    )
    _assert_drawable(short, PORTRAIT_PHOTOS)
    _assert_drawable(longer, PORTRAIT_PHOTOS)
    tall = title_block_box(longer.rows, canvas=PORTRAIT).height
    assert tall > title_block_box(short.rows, canvas=PORTRAIT).height
    assert longer.cards[0].box.bottom < short.cards[0].box.bottom
    too_long = _layout(
        "grid_2x2",
        PORTRAIT_PHOTOS,
        "tr",
        title="şehir rehberi: lizbon, ilk gün, bütün gün yürüyoruz",
        voices=VOICES + ["solid"] * 3,
        stack="script_over_serif",
    )
    assert too_long == props.PropsDecline("no_room_for_cards")


def test_a_close_speaker_leaves_no_room_for_cards():
    """The typical close selfie: the title alone fills the band above her head."""
    stage = props.Stage((mod.TYPICAL_FACE,), CAPTIONS)
    assert _layout("grid_2x2", PORTRAIT_PHOTOS, stage=stage) == props.PropsDecline(
        "no_room_for_cards"
    )


def test_the_title_keeps_the_size_it_has_without_the_cards():
    rows, position = _title_rows(TITLE_EN, "en", stack="script_over_serif")
    alone, _receipt = mod.place_on_stage(
        rows, position, PORTRAIT, faces=ROOMY.faces, avoid=[ROOMY.caption_box]
    )
    layout = _layout("grid_2x2", PORTRAIT_PHOTOS, stack="script_over_serif")
    assert [row["size_px"] for row in layout.rows] == [row["size_px"] for row in alone]


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("onto_the_cards", "title_touches_a_card"),
        ("under_the_bottom_row", "title_not_between_the_cards"),
        ("shrunk", "cards_shrink_the_title"),
        ("unclean", "title_unclean_with_cards"),
    ],
)
def test_a_title_placement_that_breaks_a_rule_is_declined(monkeypatch, fault, reason):
    real = mod.place_on_stage
    calls = []

    def place(rows, position, canvas, *, faces, avoid):
        placed, receipt = real(rows, position, canvas, faces=faces, avoid=avoid)
        calls.append(1)
        if len(calls) == 1:
            return placed, receipt
        if fault == "onto_the_cards":
            placed = [{**row, "y_frac": 0.19} for row in placed]
        elif fault == "under_the_bottom_row":
            placed = [{**row, "y_frac": 0.9} for row in placed]
        elif fault == "shrunk":
            receipt = {**receipt, "scale": 0.5}
        else:
            receipt = {**receipt, "status": "best_effort"}
        return placed, receipt

    monkeypatch.setattr(props, "place_on_stage", place)
    assert _layout("grid_2x2", PORTRAIT_PHOTOS) == props.PropsDecline(reason)


@pytest.mark.parametrize(
    ("arrangement", "aspects", "stage", "reason"),
    [
        ("grid_2x2", (1.0, 1.0, 1.0), ROOMY, "too_few_photos"),
        ("grid_2x2", (1.0, 1.0, 0.0, 1.0), ROOMY, "too_few_photos"),
        ("none", (1.0, 1.0), ROOMY, "no_arrangement"),
        # A 9:16 photo as tall as the band under her face is narrower than a card is drawn.
        ("grid_2x2", (1.0, 1.0, 1.0, 0.5625), ROOMY, "no_room_for_cards"),
        ("around_speaker", (1.0, 1.0), props.Stage(()), "no_face"),
    ],
)
def test_what_the_layout_cannot_use_is_declined(arrangement, aspects, stage, reason):
    assert _layout(arrangement, aspects, stage=stage) == props.PropsDecline(reason)


# --- the compile: a scene with an arrangement, end to end through the approval check ---------


def _compile(
    arrangement, photos=PORTRAIT_PHOTOS, language="en", *, stage=ROOMY, card_mode="clean", **over
):
    return mod.compile_intro_scene(
        _scene(arrangement=arrangement, **over),
        TITLES[language],
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        language=language,
        photo_aspects=photos,
        stage=stage,
        card_mode=card_mode,
    )


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("arrangement", ["around_speaker", "grid_2x2"])
def test_her_title_compiles_between_her_cards_and_still_reads_as_her_title(arrangement, language):
    out = _compile(arrangement, language=language, stack="script_over_serif")
    assert out.reason == "ok", out.reason
    assert out.props is not None and out.rows is out.props.rows
    _assert_drawable(out.props, PORTRAIT_PHOTOS)
    [match] = check.approval_matches(out.rows, TITLES[language])
    assert match["start_s"] == 0.0


def test_cards_that_do_not_fit_leave_todays_title_and_no_card():
    out = _compile("grid_2x2", stage=None)  # the typical close speaker
    assert (out.rows, out.reason, out.props) == (None, "props_do_not_fit", None)


@pytest.mark.parametrize("photos", [(), (1.0, 1.0, 1.0)])
def test_without_a_photo_for_every_slot_the_arrangement_is_not_issued(photos):
    out = _compile("grid_2x2", photos)
    assert (out.rows, out.reason) == (None, "option_not_issued")


def test_a_scene_without_an_arrangement_compiles_as_before():
    def compile_plain(**extra):
        return mod.compile_intro_scene(
            _scene(), TITLE_EN, canvas=PORTRAIT, timeline_duration_s=30.0, language="en", **extra
        )

    plain = compile_plain()
    assert plain.reason == "ok" and plain.props is None
    assert compile_plain(photo_aspects=PORTRAIT_PHOTOS, stage=ROOMY) == plain
    assert {row["y_frac"] for row in plain.rows} == {0.15}  # unplaced: the caller places them


def test_the_drawn_ink_of_the_title_never_touches_a_card(monkeypatch):
    real = props.layout_intro_props

    def card_on_the_title(*args, **kwargs):
        layout = real(*args, **kwargs)
        block = title_block_box(layout.rows, canvas=PORTRAIT)
        card = dataclasses.replace(layout.cards[0], box=block)
        return dataclasses.replace(layout, cards=(card, *layout.cards[1:]))

    monkeypatch.setattr(props, "layout_intro_props", card_on_the_title)
    assert (_compile("grid_2x2").rows, _compile("grid_2x2").reason) == (None, "props_do_not_fit")


def test_a_layout_error_never_breaks_the_compile(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("layout fell over")

    monkeypatch.setattr(props, "layout_intro_props", boom)
    assert _compile("grid_2x2").reason == "compile_error"


# --- pop: short cards that may cover her (M47-M59 in intro_props_layout) ----------------------

# A close selfie like the real Talking edits (the face box measured on a real take): the title
# alone fills the room above her head and her chin reaches the caption row.
CLOSE_FACE = mod.NormalizedBox(0.19, 0.38, 0.83, 0.77)
CLOSE = props.Stage((CLOSE_FACE,), CAPTIONS)
SHORT_S = 1.5  # Emir's "short time" (2026-11-27)
STAGGER_S = 0.25


def _pop(arrangement, aspects, language="en", *, stage=CLOSE, title=None, **over):
    rows, position = _title_rows(title or TITLES[language], language, **over)
    return props.layout_intro_props(
        arrangement, aspects, rows, stage=stage, canvas=PORTRAIT, position=position, mode="pop"
    )


def _assert_pop_drawable(layout, aspects, *, stage=CLOSE, window=(0.0, 3.0)):
    """The pop contract, from the ruling: on the frame, off the caption row, the watermark, the
    title's lines and each other (GAP apart), each the shape of its photo and between the card
    minimum and 0.45 wide; the title exactly where it sits alone; each card shown at most 1.5 s
    inside the title's window, entering one stagger after the previous one, title first."""
    assert isinstance(layout, props.PropsLayout), layout
    assert layout.mode == "pop"
    top, bottom = stage.caption_band or (CAPTION_BAND_TOP_FRAC, 1.0)
    captions = mod.NormalizedBox(0.0, top, 1.0, bottom)
    boxes = [card.box for card in layout.cards]
    assert [card.slot for card in layout.cards] == [f"prop_{n}" for n in range(1, len(boxes) + 1)]
    assert len({round(box.width, 9) for box in boxes}) == 1  # M53: one width per look
    for box, aspect in zip(boxes, aspects, strict=False):
        assert EDGE_INSET - 1e-6 <= box.left and box.right <= 1 - EDGE_INSET + 1e-6, box
        assert EDGE_INSET - 1e-6 <= box.top and box.bottom <= 1 - EDGE_INSET + 1e-6, box
        assert not boxes_intersect(box, captions), box
        assert not boxes_intersect(box, WATERMARK_BOX), box
        assert _drawn_shape(box) == pytest.approx(aspect, rel=1e-3)
        assert MIN_WIDTH - 1e-6 <= min(box.width, box.height * PORTRAIT.height / PORTRAIT.width)
        assert box.width <= 0.45 + 1e-9
    for a, b in itertools.combinations(boxes, 2):
        assert not boxes_intersect(a.padded(GAP - 1e-6), b), (a, b)
    lines = [_measure_title_box(dict(row), canvas=PORTRAIT) for row in layout.rows]
    assert not any(boxes_intersect(line, box) for line in lines for box in boxes)
    starts = [card.start_s for card in layout.cards]
    assert starts[0] > window[0]  # the title lands first
    assert starts == pytest.approx([starts[0] + n * STAGGER_S for n in range(len(starts))])
    for card in layout.cards:
        assert window[0] < card.start_s < card.end_s <= window[1] + 1e-9
        assert card.end_s - card.start_s <= SHORT_S + 1e-9


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("aspects", sorted(ASPECTS), ids=str)
@pytest.mark.parametrize("arrangement", ["around_speaker", "grid_2x2"])
def test_on_a_close_speaker_cards_pop_over_her_briefly_around_the_title(
    arrangement, aspects, language
):
    photos = ASPECTS[aspects]
    rows, position = _title_rows(TITLES[language], language)
    window = (min(r["start_s"] for r in rows), max(r["end_s"] for r in rows))
    layout = _pop(arrangement, photos, language)
    _assert_pop_drawable(layout, photos, window=window)
    alone, receipt = mod.place_on_stage(
        rows, position, PORTRAIT, faces=CLOSE.faces, avoid=[CLOSE.caption_box]
    )
    assert (layout.rows, layout.receipt) == (alone, receipt)  # M54


def test_a_longer_turkish_title_keeps_its_block_and_the_cards_go_around_it():
    longer = "şehir rehberi: lizbon, birinci gün, bütün gün"
    rows, position = _title_rows(longer, "tr", voices=VOICES + ["solid"] * 2)
    layout = _pop("grid_2x2", PORTRAIT_PHOTOS, "tr", title=longer, voices=VOICES + ["solid"] * 2)
    _assert_pop_drawable(layout, PORTRAIT_PHOTOS)
    block = title_block_box(layout.rows, canvas=PORTRAIT)
    assert not any(boxes_intersect(card.box, block) for card in layout.cards)  # M56


def test_where_cards_fit_off_her_the_layout_stays_clean():
    clean = _layout("grid_2x2", PORTRAIT_PHOTOS)
    assert clean.mode == "clean"  # M47: the default mode, unchanged where there is room
    window = (min(c.start_s for c in clean.cards), max(c.end_s for c in clean.cards))
    rows, _ = _title_rows(TITLE_EN, "en")
    assert all((c.start_s, c.end_s) == window for c in clean.cards)
    assert window == (min(r["start_s"] for r in rows), max(r["end_s"] for r in rows))


def _cover(box, faces) -> tuple[float, float]:
    centre = sum(box.intersection_area(props.face_centre(face)) for face in faces)
    return centre, sum(box.intersection_area(face) for face in faces)


# A real close take with her captions ABOVE her face (measured on a prod Talking edit).
CAPTIONS_ABOVE = props.Stage((mod.NormalizedBox(0.21, 0.434, 0.867, 0.813),), (0.15, 0.25))


@pytest.mark.parametrize(
    ("arrangement", "aspects", "stage"),
    [
        ("around_speaker", "portrait", CLOSE),
        ("grid_2x2", "portrait", CLOSE),
        ("grid_2x2", "mixed", CLOSE),
        ("around_speaker", "portrait", CAPTIONS_ABOVE),
        ("grid_2x2", "portrait", CAPTIONS_ABOVE),
    ],
)
def test_each_pop_card_takes_the_free_spot_that_covers_least_of_her_eyes_and_mouth(
    arrangement, aspects, stage
):
    """M52, against a brute force: for each card in slot order, every spot of its size on a
    0.005 grid that keeps the rules (the cards before it in place). None covers less of her face
    centre than the one the layout chose, and of those that cover as little, none covers less of
    her face."""
    layout = _pop(arrangement, ASPECTS[aspects], stage=stage)
    block = title_block_box(layout.rows, canvas=PORTRAIT).padded(GAP)
    steps = [EDGE_INSET + n * 0.005 for n in range(200)]
    tie = 0.001
    placed: list = []
    for card in layout.cards:
        size = card.box
        keep_out = (block, stage.caption_box, WATERMARK_BOX, *(b.padded(GAP) for b in placed))
        free = []
        for x, y in itertools.product(steps, steps):
            box = mod.NormalizedBox(x, y, x + size.width, y + size.height)
            if box.right > 1 - EDGE_INSET or box.bottom > 1 - EDGE_INSET:
                continue
            if not any(boxes_intersect(box, other) for other in keep_out):
                free.append(_cover(box, stage.faces))
        assert free, card.slot
        chosen = _cover(size, stage.faces)
        assert chosen[0] <= min(c for c, _f in free) + tie, card.slot
        as_little = [f for c, f in free if c <= chosen[0] + tie]  # the grid may miss its spot
        assert not as_little or chosen[1] <= min(as_little) + tie, card.slot
        placed.append(size)


def test_the_face_centre_is_her_eyes_and_mouth_inside_the_saved_face_box():
    centre = props.face_centre(CLOSE_FACE)
    assert CLOSE_FACE.left < centre.left < centre.right < CLOSE_FACE.right
    assert CLOSE_FACE.top < centre.top < centre.bottom < CLOSE_FACE.bottom
    # the eyes sit about half way down a face box, the mouth about three quarters
    for down in (0.5, 0.75):
        assert centre.top < CLOSE_FACE.top + down * CLOSE_FACE.height < centre.bottom


def test_pop_is_the_same_twice():
    first = _pop("grid_2x2", ASPECTS["mixed"], "tr")
    assert _pop("grid_2x2", ASPECTS["mixed"], "tr") == first  # M55


def test_pop_cards_show_one_after_another_and_never_past_the_title():
    pop = props.CARD_MODES["pop"]
    assert pop.windows(4, 0.0, 2.5) == [(0.25, 1.75), (0.5, 2.0), (0.75, 2.25), (1.0, 2.5)]
    assert pop.windows(2, 4.0, 5.6) == [(4.25, 5.6), (4.5, 5.6)]  # cut at the title's end
    assert pop.windows(4, 0.0, 1.2) is None  # the fourth card would flash for 0.2 s
    assert pop.windows(2, 0.0, 1.2) == [(0.25, 1.2), (0.5, 1.2)]
    clean = props.CARD_MODES["clean"]
    assert clean.windows(2, 0.1, 2.6337) == [(0.1, 2.6337), (0.1, 2.6337)]  # exactly the title


def test_a_title_too_brief_for_every_card_declines_the_pop():
    rows, position = _title_rows(TITLE_EN, "en")
    brief = [{**row, "end_s": 1.2} for row in rows]
    layout = props.layout_intro_props(
        "grid_2x2",
        PORTRAIT_PHOTOS,
        brief,
        stage=CLOSE,
        canvas=PORTRAIT,
        position=position,
        mode="pop",
    )
    assert layout == props.PropsDecline("title_too_brief")


def test_a_pop_title_is_one_the_plain_look_would_show():
    """M54: the plain look's preflight declines this loopy stack (it cannot sit clear); with pop
    cards the same title is declined the same way, never squeezed onto her stage instead."""
    title = "how am i / really loving this"
    scene = _scene(
        voices=["solid"] * 3 + ["accent"] * 3, font_pair="grotesk_loop", arrangement="grid_2x2"
    )

    def compile_on(arrangement, card_mode):
        return mod.compile_intro_scene(
            scene.model_copy(update={"arrangement": arrangement}),
            title,
            canvas=PORTRAIT,
            timeline_duration_s=30.0,
            language="en",
            photo_aspects=PORTRAIT_PHOTOS,
            stage=CLOSE,
            card_mode=card_mode,
        )

    plain = compile_on("none", "clean")
    assert (plain.rows, plain.reason) == (None, "placement_unclean")
    assert compile_on("grid_2x2", "pop").reason == "placement_unclean"


def test_pop_never_assumes_the_stand_in_speaker():
    out = _compile("grid_2x2", stage=None, card_mode="pop")
    assert (out.rows, out.reason, out.card_layout) == (None, "props_do_not_fit", "no_stage")


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("arrangement", ["around_speaker", "grid_2x2"])
def test_her_title_compiles_with_pop_cards_and_still_reads_as_her_title(arrangement, language):
    out = _compile(arrangement, language=language, stage=CLOSE, card_mode="pop")
    assert out.reason == "ok", (out.reason, out.card_layout)
    assert out.props is not None and out.props.mode == "pop"
    _assert_pop_drawable(out.props, PORTRAIT_PHOTOS)
    [match] = check.approval_matches(out.rows, TITLES[language])
    assert match["start_s"] == 0.0
    clean = _compile(arrangement, language=language, stage=CLOSE)
    assert (clean.rows, clean.card_layout) == (None, "no_room_for_cards")


def test_the_drawn_ink_of_the_title_never_touches_a_pop_card(monkeypatch):
    real = props.layout_intro_props

    def card_on_the_title(*args, **kwargs):
        layout = real(*args, **kwargs)
        block = title_block_box(layout.rows, canvas=PORTRAIT)
        moved = dataclasses.replace(layout.cards[0], box=block)
        return dataclasses.replace(layout, cards=(moved, *layout.cards[1:]))

    monkeypatch.setattr(props, "layout_intro_props", card_on_the_title)
    out = _compile("grid_2x2", stage=CLOSE, card_mode="pop")
    assert (out.rows, out.reason) == (None, "props_do_not_fit")  # M48


# --- the offer's card looks on a framing with room (stage B, byte for byte) -------------------

_CLEAN_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "kri1615_clean_card_looks.json"

# Text the OS font backend measures: macOS and Linux shape the same font with the same Skia
# a pixel or so apart, and the fixture title's fitted size can differ by 1 px.
_FONT_MEASURED = {("runs", "x"), ("runs", "baseline_y"), ("glyphs", "x"), ("restore", "size_px")}


def _font_measured_masked(value, parent: str = ""):
    if isinstance(value, dict):
        return {
            key: "<measured>" if (parent, key) in _FONT_MEASURED else _font_measured_masked(v, key)
            for key, v in value.items()
        }
    if isinstance(value, list):
        return [_font_measured_masked(v, parent) for v in value]
    return value


def test_card_looks_on_a_framing_with_room_are_stage_bs_byte_for_byte(monkeypatch):
    """The card looks the offer makes where the cards fit off her face (rows, cards, windows,
    previews), EN and TR, equal the golden captured from stage B before cards could pop over
    her, except the font measurements in `_FONT_MEASURED`. `KRI1615_CLEAN_REGENERATE=1`
    rewrites it; only a reviewed look change may."""
    from app.cli import intro_styles_fixtures as fx
    from app.cli import verify_intro_styles as styles
    from app.config import settings

    for key, value in fx.flag_overrides().items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(settings, "intro_scene_model_enabled", False)
    looks = {
        lang: [o for o in styles._card_offer(lang, index)[3]["options"] if o.get("cards")]
        for index, lang in enumerate(styles.CARD_TITLES)
    }
    masked = _font_measured_masked(looks)
    text = json.dumps(masked, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    if os.environ.get("KRI1615_CLEAN_REGENERATE") == "1":
        _CLEAN_GOLDEN.write_text(text)
    assert all(looks.values()), "a framing with room gets card looks in EN and TR"
    assert text == _CLEAN_GOLDEN.read_text()
