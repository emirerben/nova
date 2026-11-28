"""KRI-1616: stickers and edge bleed in the intro card layout (M60-M66 in
`services/intro_props_layout.py`), on the props tests' stages with the real Skia title."""

from __future__ import annotations

import pytest

from app.pipeline import intro_scene as mod

skia = pytest.importorskip("skia")

from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.phone_subtitled_title import _measure_title_box  # noqa: E402
from app.services import intro_props_layout as props  # noqa: E402
from app.services.phone_card_layout import (  # noqa: E402
    EDGE_INSET,
    GAP,
    WATERMARK_BOX,
    boxes_intersect,
)
from tests.pipeline.test_intro_scene_props import (  # noqa: E402
    CLOSE,
    ROOMY,
    TITLES,
    _scene,
    _title_rows,
)

STICKERS = (1.0, 1.0)


def _stickers(arrangement="stickers_edge", aspects=STICKERS, language="en", **kw):
    rows, position = _title_rows(TITLES[language], language)
    layout = props.layout_intro_props(
        arrangement, aspects, rows, canvas=PORTRAIT, position=position, **kw
    )
    return layout, rows


def _off(box):
    return max(-box.left, box.right - 1.0, 0.0)


def test_stickers_are_issued_from_her_transparent_pictures_only():
    assert "stickers_edge" not in mod.issue_menu(TITLES["en"], photo_count=4).arrangements
    assert mod.issue_menu(TITLES["en"], sticker_count=1).arrangements == ("none", "sticker_edge")
    issued = mod.issue_menu(TITLES["en"], sticker_count=2).arrangements
    assert set(issued) == {"none", "sticker_edge", "stickers_edge"}
    assert "arrangement" in _scene(arrangement="stickers_edge").model_dump(mode="json")


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("bleed", [False, True])
def test_m60_m65_stickers_keep_off_her_and_the_title_hanging_only_where_allowed(language, bleed):
    layout, rows = _stickers(language=language, stage=ROOMY, mode="sticker", bleed=bleed)
    assert isinstance(layout, props.PropsLayout), layout
    assert [c.slot for c in layout.cards] == ["sticker_1", "sticker_2"]
    assert [c.z for c in layout.cards] == [props.STICKER_Z, props.STICKER_Z + 1]  # M65
    lines = [_measure_title_box(dict(row), canvas=PORTRAIT) for row in layout.rows]
    boxes = [card.box for card in layout.cards]
    assert (
        len({round(b.width, 9) for b in boxes}) == 1
        and round(boxes[0].width, 9) in props.STICKER_WIDTHS
    )
    for card in layout.cards:
        box = card.box
        assert not any(boxes_intersect(box, face) for face in ROOMY.faces)  # M63
        assert not any(boxes_intersect(box, line) for line in lines)  # M62
        assert not boxes_intersect(box, ROOMY.caption_box)
        assert not boxes_intersect(box, WATERMARK_BOX)
        assert box.top >= EDGE_INSET - 1e-9 and box.bottom <= 1 - EDGE_INSET + 1e-9  # M61
        if bleed:
            assert _off(box) <= props.BLEED_MAX * box.width + 1e-9
        else:
            assert box.left >= EDGE_INSET - 1e-9 and box.right <= 1 - EDGE_INSET + 1e-9  # M60
        assert (card.start_s, card.end_s) == (layout.cards[0].start_s, layout.cards[0].end_s)
    assert not boxes_intersect(boxes[0].padded(GAP / 2), boxes[1].padded(GAP / 2))
    if bleed:
        assert any(_off(b) > 0.15 * b.width for b in boxes), "a sticker hangs past the frame"


def test_m63_on_a_close_selfie_stickers_only_cover_her_in_the_short_mode():
    calm, _rows = _stickers(stage=CLOSE, mode="sticker", bleed=True)
    pop, _rows = _stickers(stage=CLOSE, mode="sticker_pop", bleed=True)
    if isinstance(calm, props.PropsLayout):
        assert not any(boxes_intersect(c.box, f) for c in calm.cards for f in CLOSE.faces)
    assert isinstance(pop, props.PropsLayout), pop
    for card in pop.cards:
        assert card.end_s - card.start_s <= props.SHORT_CARD_S + 1e-9


def test_m61_a_box_past_the_allowance_or_past_the_top_is_caught():
    width = 0.25
    height = width * PORTRAIT.width / PORTRAIT.height
    too_far = mod.NormalizedBox(-0.4 * width, 0.2, 0.6 * width, 0.2 + height)
    over_top = mod.NormalizedBox(0.3, -0.01, 0.3 + width, -0.01 + height)
    for box in (too_far, over_top):
        assert props.card_problems(
            [box], [1.0], stage=ROOMY, canvas=PORTRAIT, mode="sticker", bleed=True
        )
    allowed = mod.NormalizedBox(-0.3 * width, 0.2, 0.7 * width, 0.2 + height)
    assert not props.card_problems(
        [allowed], [1.0], stage=ROOMY, canvas=PORTRAIT, mode="sticker", bleed=True
    )
    assert props.card_problems([allowed], [1.0], stage=ROOMY, canvas=PORTRAIT, mode="sticker")


def test_m60_photo_pop_cards_hang_past_the_side_only_with_bleed():
    rows, position = _title_rows(TITLES["en"], "en")
    common = dict(stage=CLOSE, canvas=PORTRAIT, position=position, mode="pop")
    without = props.layout_intro_props("around_speaker", (0.75, 0.75), rows, **common)
    with_bleed = props.layout_intro_props(
        "around_speaker", (0.75, 0.75), rows, bleed=True, **common
    )
    assert isinstance(without, props.PropsLayout) and isinstance(with_bleed, props.PropsLayout)
    assert all(
        c.box.left >= EDGE_INSET - 1e-9 and c.box.right <= 1 - EDGE_INSET + 1e-9
        for c in without.cards
    )
    for card in with_bleed.cards:
        assert _off(card.box) <= props.BLEED_MAX * card.box.width + 1e-9


@pytest.mark.parametrize(
    ("arrangement", "mode"), [("stickers_edge", "pop"), ("around_speaker", "sticker")]
)
def test_m66_a_mode_for_the_other_kind_is_declined(arrangement, mode):
    layout, _rows = _stickers(arrangement, (0.75, 0.75), stage=ROOMY, mode=mode)
    assert layout == props.PropsDecline("mode_not_for_arrangement")
