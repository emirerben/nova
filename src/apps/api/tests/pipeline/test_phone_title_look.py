"""Qendresa item 20: her stacked title look (failure modes F1-F4, F8, F17).

Written before the module. Each test is a way the look could go wrong:

* F1/F2 (split) lives in test_phone_title_look_lines.py.
* F3: a per-line ask is mis-read (style on the wrong line, a colour leaking onto every line).
* F4: the lines overlap each other, run off the frame, or land on her face.
* F8: the logo sits over the title, off the top of the frame, or on her face.
* F17: the approval verifier accepts a stack that is not her title (changed word,
  wrong order, lines shown at different times).
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import TextElement
from app.kria.recipes import Canvas
from app.pipeline import phone_subtitled_title as title_mod
from app.pipeline.phone_title_look import (
    TitleLookAsk,
    is_title_look_row,
    logo_aspect,
    place_title_logo,
    place_title_look,
    stacked_title_matches,
    title_block_box,
    title_look_ask,
    title_look_rows,
    title_look_text,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion

CANVAS = Canvas(width=1080, height=1920)
HER_TITLE = "5 PROJECTS / the AI builder community / can't stop talking about!!!!"
HER_ASK = (
    'Start with me full screen and the title "5 PROJECTS / the AI builder community / '
    "can't stop talking about!!!!\" in three styles: handwritten, bold caps, serif yellow."
)

V5_FACE = (0.44, 0.37, 0.93, 0.61)
SELFIE_FACE = (0.18, 0.06, 0.92, 0.66)
LOW_FACE = (0.25, 0.45, 0.80, 0.80)


def _sampler(*faces, decoded=6):
    regions = [ProtectedRegion(0.0, 30.0, NormalizedBox(*face), kind="face") for face in faces]

    def sample(path, anchors, **_kwargs):
        return list(regions), {
            "attempted": len(anchors),
            "decoded": decoded,
            "detected": len(regions),
        }

    return sample


def _rows(title=HER_TITLE, ask=None):
    rows = title_look_rows(
        title,
        duration_s=None,
        first_word_end_s=0.6,
        timeline_duration_s=30.0,
        canvas=CANVAS,
        ask=ask or TitleLookAsk(),
    )
    assert rows is not None
    return rows


def _place(rows, *faces, position=None, **kwargs):
    return place_title_look(
        rows,
        clip_path="/tmp/speaker.mp4",
        keep_segments=[(0.0, 30.0)],
        display_width=1080,
        display_height=1920,
        canvas=CANVAS,
        sample=_sampler(*faces),
        position=position,
        **kwargs,
    )


def _boxes(rows):
    return [title_mod._measure_title_box(row, canvas=CANVAS) for row in rows]


# --- per-line ask (F3) -------------------------------------------------------------------


def test_her_per_line_styles_map_in_order_and_the_colour_stays_on_its_line():
    ask = title_look_ask(HER_ASK, line_count=3)

    assert ask.line_kinds == ("handwritten", "bold", "serif")
    assert ask.line_colors == (None, None, "#FFFF00")
    assert ask.color is None


def test_a_whole_title_colour_applies_to_every_line_when_no_per_line_styles():
    ask = title_look_ask("make the title yellow", line_count=3, title_rule={"color": "#FFFF00"})

    assert ask.line_kinds == ()
    assert ask.color == "#FFFF00"
    rows = _rows(ask=ask)
    assert {row["color"] for row in rows} == {"#FFFF00"}


def test_per_line_styles_drop_the_whole_title_colour_that_came_from_the_same_words():
    ask = title_look_ask(HER_ASK, line_count=3, title_rule={"color": "#FFFF00"})

    assert ask.color is None
    rows = _rows(ask=ask)
    assert [row.get("color") for row in rows][:2] != ["#FFFF00", "#FFFF00"]
    assert rows[2]["color"] == "#FFFF00"


def test_more_style_words_than_lines_is_not_a_per_line_ask():
    ask = title_look_ask("bold, serif, handwritten", line_count=2)

    assert ask.line_kinds == ()


def test_style_words_about_other_text_never_reach_the_title_lines():
    """Her Q2-style prompt also styles captions and labels; only the title's own sentence counts."""
    request = (
        "Start with the title \"5 PROJECTS / the AI builder community / can't stop talking "
        'about!!!!". Captions in bold black, labels in script. End with "comment REPO".'
    )
    ask = title_look_ask(request, line_count=3)

    assert ask.line_kinds == ()
    assert ask.line_colors == ()


def test_her_line_styles_in_a_later_sentence_about_the_title_still_count():
    request = (
        'Start with "5 PROJECTS / the AI builder community / can\'t stop talking about!!!!". '
        "Make the title lines bold caps, serif and yellow handwriting."
    )
    ask = title_look_ask(request, line_count=3)

    assert ask.line_kinds == ("bold", "serif", "handwritten")
    assert ask.line_colors == (None, None, "#FFFF00")


def test_one_style_word_is_not_a_per_line_ask():
    assert title_look_ask("use a bold title", line_count=3).line_kinds == ()


def test_title_top_left_is_read_from_her_words():
    ask = title_look_ask("put the title top left", line_count=3)

    assert ask.position == "top_left"
    rows = _rows(ask=ask)
    assert {row["alignment"] for row in rows} == {"left"}
    assert {row["x_frac"] for row in rows} == {0.06}


def test_a_named_font_from_the_title_rule_applies_to_every_line():
    rows = _rows(ask=TitleLookAsk(font_family="Pacifico"))

    assert {row["font_family"] for row in rows} == {"Pacifico"}
    assert {row["source_params"]["title_look"]["style"] for row in rows} == {"asked"}


# --- rows --------------------------------------------------------------------------------


def test_default_look_is_bold_serif_handwriting_in_her_exact_words():
    rows = _rows()

    assert [row["text"] for row in rows] == [
        "5 PROJECTS",
        "the AI builder community",
        "can't stop talking about!!!!",
    ]
    assert [row["id"] for row in rows] == [
        "opening-title",
        "opening-title-line-2",
        "opening-title-line-3",
    ]
    assert [row["source_params"]["title_look"]["style"] for row in rows] == [
        "bold",
        "serif",
        "handwritten",
    ]
    assert [row["font_family"] for row in rows] == [
        "Bricolage Grotesque",
        "Instrument Serif",
        "Patrick Hand",
    ]
    for row in rows:
        TextElement.model_validate(row)
        assert is_title_look_row(row)
        assert row["source_params"]["source"] == "opening_title"
        assert (row["start_s"], row["end_s"]) == (0.0, 3.0)
    assert title_look_text(rows) == HER_TITLE


def test_a_single_line_title_is_not_a_title_look():
    assert (
        title_look_rows(
            "3 sourdough mistakes",
            duration_s=None,
            first_word_end_s=0.6,
            timeline_duration_s=30.0,
            canvas=CANVAS,
            ask=TitleLookAsk(),
        )
        is None
    )
    assert title_look_text([{"id": "x", "text": "hi"}]) is None


def test_her_confirmed_hold_wins_and_is_clamped_to_the_timeline():
    rows = title_look_rows(
        HER_TITLE,
        duration_s=2.0,
        first_word_end_s=0.6,
        timeline_duration_s=1.5,
        canvas=CANVAS,
        ask=TitleLookAsk(),
    )
    assert {row["end_s"] for row in rows} == {1.5}


# --- stacked placement (F4) --------------------------------------------------------------


def _assert_stacked_and_inside(placed):
    boxes = _boxes(placed)
    for upper, lower in zip(boxes, boxes[1:], strict=False):
        assert upper.bottom <= lower.top + 1e-3, (upper, lower)
    for box in boxes:
        assert box.left >= 0.03 - 1e-3 and box.right <= 0.97 + 1e-3
        assert box.top >= 0.06 - 1e-3 and box.bottom <= 0.95 + 1e-3
    return boxes


def test_no_face_centres_the_block_at_the_top_and_keeps_line_order():
    placed, receipt = _place(_rows())

    boxes = _assert_stacked_and_inside(placed)
    assert receipt["status"] == "preset"
    assert receipt["face_collision"] is False
    assert receipt["lines"] == 3
    assert abs(boxes[0].top - 0.10) < 0.01
    assert [row["text"] for row in placed] == [row["text"] for row in _rows()]


@pytest.mark.parametrize("face", [V5_FACE, SELFIE_FACE, LOW_FACE], ids=["v5", "selfie", "low"])
def test_the_block_never_covers_her_face(face):
    placed, receipt = _place(_rows(), face)

    boxes = _assert_stacked_and_inside(placed)
    face_box = NormalizedBox(*face)
    for box in boxes:
        assert box.coverage_by(face_box) <= 0.05
    assert receipt["face_collision"] is False


def test_a_close_selfie_moves_the_block_under_the_face():
    placed, receipt = _place(_rows(), SELFIE_FACE)

    assert receipt["status"] in {"moved_below_face", "shrunk"}
    assert _boxes(placed)[0].top >= SELFIE_FACE[3]


def test_a_face_filling_the_frame_shrinks_the_title_and_says_so():
    big = (0.05, 0.06, 0.95, 0.80)
    placed, receipt = _place(_rows(), big)

    assert receipt["status"] in {"shrunk", "best_effort"}
    if receipt["status"] == "shrunk":
        assert receipt["scale"] < 1.0
        assert placed[0]["size_px"] < _rows()[0]["size_px"]
    else:
        assert receipt["face_collision"] is True


def test_top_left_block_is_left_aligned_inside_the_frame():
    ask = TitleLookAsk(position="top_left")
    placed, _ = _place(_rows(ask=ask), position="top_left")

    boxes = _assert_stacked_and_inside(placed)
    assert all(box.left < 0.2 for box in boxes)


def test_middle_position_centres_the_block_vertically():
    placed, _ = _place(_rows(), position="middle")

    block = title_block_box(placed, canvas=CANVAS)
    assert abs((block.top + block.bottom) / 2 - 0.5) < 0.02


def test_captions_in_the_bottom_band_do_not_count_as_overlap():
    caption = NormalizedBox(0.0, 0.70, 1.0, 0.90)
    _, receipt = _place(_rows(), avoid=[caption])

    assert receipt["text_overlap"] is False


def test_text_the_block_cannot_avoid_is_reported_not_hidden():
    caption = NormalizedBox(0.0, 0.08, 1.0, 0.30)
    _, receipt = _place(_rows(), avoid=[caption])

    assert receipt["text_overlap"] is True
    assert receipt["face_collision"] is False


def test_a_sampler_failure_still_protects_the_fallback_face():
    def boom(*_args, **_kwargs):
        raise RuntimeError("no decoder")

    placed, receipt = place_title_look(
        _rows(),
        clip_path="/tmp/speaker.mp4",
        keep_segments=[(0.0, 30.0)],
        display_width=1080,
        display_height=1920,
        canvas=CANVAS,
        sample=boom,
    )
    assert receipt["faces"] == "fallback"
    for box in _boxes(placed):
        assert box.coverage_by(title_mod._FALLBACK_FACE_BOX) <= 0.05


def test_the_module_level_sampler_is_looked_up_at_call_time(monkeypatch):
    monkeypatch.setattr(title_mod, "sample_face_regions", _sampler(V5_FACE))
    placed, receipt = place_title_look(
        _rows(),
        clip_path="/tmp/speaker.mp4",
        keep_segments=[(0.0, 30.0)],
        display_width=1080,
        display_height=1920,
        canvas=CANVAS,
    )
    assert receipt["faces"] == "detected"
    assert receipt["face_boxes"]


def test_a_placement_error_returns_the_rows_unchanged():
    rows = _rows()
    rows[0]["end_s"] = "not a number"
    placed, receipt = _place(rows)

    assert receipt["status"] == "error"
    assert placed == rows


# --- logo (F8) ---------------------------------------------------------------------------


def _block(top=0.20, bottom=0.42, left=0.1, right=0.9):
    return NormalizedBox(left, top, right, bottom)


def test_the_logo_sits_right_above_the_block():
    geometry, reason = place_title_logo(_block(), aspect=1.0, faces=[], canvas=CANVAS)

    assert reason is None
    w = geometry["scale"]
    h = w * CANVAS.width / CANVAS.height
    bottom = geometry["y_frac"] + h / 2
    assert abs(bottom - (0.20 - 0.01)) < 1e-3
    assert geometry["y_frac"] - h / 2 >= 0.06 - 1e-6
    assert abs(geometry["x_frac"] - 0.5) < 1e-6


def test_a_wide_logo_is_capped_at_sixteen_percent_of_the_height():
    geometry, _ = place_title_logo(_block(top=0.4), aspect=0.2, faces=[], canvas=CANVAS)

    h = geometry["scale"] * CANVAS.width / 0.2 / CANVAS.height
    assert h <= 0.16 + 1e-6


def test_left_alignment_puts_the_logo_over_the_block_left_edge():
    geometry, _ = place_title_logo(
        _block(left=0.06), aspect=1.0, faces=[], canvas=CANVAS, alignment="left"
    )
    assert abs(geometry["x_frac"] - (0.06 + geometry["scale"] / 2)) < 1e-3


def test_no_room_above_the_title_returns_the_reason():
    geometry, reason = place_title_logo(_block(top=0.09), aspect=1.0, faces=[], canvas=CANVAS)

    assert geometry is None
    assert reason == "no_room_above_title"


def test_the_logo_slides_or_shrinks_off_a_face():
    face = NormalizedBox(0.30, 0.0, 0.70, 0.40)
    geometry, reason = place_title_logo(
        _block(top=0.30, bottom=0.5, left=0.06, right=0.94),
        aspect=1.0,
        faces=[face],
        canvas=CANVAS,
    )
    if geometry is None:
        assert reason == "no_room_above_title"
        return
    w = geometry["scale"]
    h = w * CANVAS.width / CANVAS.height
    box = NormalizedBox(
        geometry["x_frac"] - w / 2,
        geometry["y_frac"] - h / 2,
        geometry["x_frac"] + w / 2,
        geometry["y_frac"] + h / 2,
    )
    assert box.coverage_by(face) <= 0.05


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1:1", 1.0), ("16:9", 16 / 9), ("0.5625", 0.5625), (None, 1.0), ("junk", 1.0), ("0:5", 1.0)],
)
def test_logo_aspect_parsing(raw, expected):
    assert logo_aspect({"aspect": raw}) == pytest.approx(expected)


# --- approval verifier (F17) -------------------------------------------------------------


def _evidence(changes=None):
    rows = [
        {
            "element_id": "opening-title",
            "text": "5 PROJECTS",
            "start_s": 0.0,
            "end_s": 3.0,
            "anchor_y": 0.14,
        },
        {
            "element_id": "opening-title-line-2",
            "text": "the AI builder community",
            "start_s": 0.0,
            "end_s": 3.0,
            "anchor_y": 0.2,
        },
        {
            "element_id": "opening-title-line-3",
            "text": "can't stop talking about!!!!",
            "start_s": 0.0,
            "end_s": 3.0,
            "anchor_y": 0.26,
        },
    ]
    for index, patch in (changes or {}).items():
        rows[index] = {**rows[index], **patch}
    return rows


def test_a_real_stack_matches_with_its_line_ids():
    [match] = stacked_title_matches(_evidence(), HER_TITLE, tolerance_s=0.05)

    assert match["text"] == HER_TITLE
    assert match["start_s"] == 0.0
    assert match["end_s"] == 3.0
    assert match["role"] is None
    assert match["stacked_lines"] == [
        "opening-title",
        "opening-title-line-2",
        "opening-title-line-3",
    ]


def test_wrapped_whitespace_is_not_a_difference():
    rows = _evidence({1: {"text": "the AI  builder\ncommunity"}})
    assert stacked_title_matches(rows, HER_TITLE, tolerance_s=0.05)


@pytest.mark.parametrize(
    "changes",
    [
        {1: {"text": "the AI builders community"}},
        {2: {"text": "can't stop talking about!!!"}},
        {0: {"anchor_y": 0.5}},
        {1: {"anchor_y": 0.14}},
        {2: {"start_s": 1.0}},
        {1: {"end_s": 1.0}},
        {0: {"anchor_y": None}},
    ],
    ids=["word", "punctuation", "order", "same-y", "start", "end", "no-anchor"],
)
def test_negative_controls_return_nothing(changes):
    assert stacked_title_matches(_evidence(changes), HER_TITLE, tolerance_s=0.05) == []


def test_captions_never_stand_in_for_a_line():
    rows = _evidence({1: {"element_id": "caption-7"}})
    assert stacked_title_matches(rows, HER_TITLE, tolerance_s=0.05) == []


def test_a_single_line_wanted_title_never_matches():
    assert stacked_title_matches(_evidence(), "5 PROJECTS", tolerance_s=0.05) == []
