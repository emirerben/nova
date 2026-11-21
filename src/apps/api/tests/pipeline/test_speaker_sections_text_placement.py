"""KRI-1558 (row 24): text keeps off the speaker's face WHERE EACH SECTION DRAWS HER.

A ``small`` section maps her face into its own corner, ``own`` / ``full`` keep it full-frame and a
``picture`` section has no face on screen. The title / label placer, the CTA ending's cut-out
boxes, the caption styler's face map and the worker's closing kwargs all read the same layout.
Without layout windows every one of them is exactly what it was.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.recipes import Canvas
from app.pipeline import phone_cta_ending
from app.pipeline import phone_subtitled_title as pst
from app.pipeline.phone_subtitled_lanes import (
    SubtitledBackground,
    SubtitledLayoutWindow,
    SubtitledSpeakerFrame,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services import phone_text_styling as styling
from app.tasks import generative_build as gb

_CANVAS = Canvas(width=1080, height=1920)
CORNER = (0.42, 0.76, 0.8)
LEFT = (0.42, 0.24, 0.8)
# Her full-frame face sits under the title's preset spot (y 0.15).
FACE = NormalizedBox(0.25, 0.05, 0.75, 0.42)
LAYOUTS = (
    (0.0, 3.0, "own", None),
    (3.0, 6.0, "small", CORNER),
    (6.0, 8.0, "picture", None),
    (8.0, 10.0, "small", LEFT),
)


def _sampler(regions, *, decoded=6):
    def sample(path, anchors, **kwargs):  # noqa: ARG001
        return regions, {"attempted": len(anchors), "decoded": decoded, "detected": len(regions)}

    return sample


def _row(start_s: float, end_s: float) -> dict:
    row = pst.talking_title_element(
        "3 sourdough mistakes",
        duration_s=end_s - start_s,
        first_word_end_s=None,
        timeline_duration_s=30.0,
        canvas=_CANVAS,
    )
    assert row is not None
    return {**row, "start_s": start_s, "end_s": end_s}


def _place(row, **kwargs):
    face = ProtectedRegion(0.0, 30.0, FACE, "face")
    return pst.place_talking_title(
        row,
        clip_path="/tmp/speaker.mp4",
        keep_segments=[(0.0, 30.0)],
        display_width=1080,
        display_height=1920,
        canvas=_CANVAS,
        sample=_sampler([face]),
        **kwargs,
    )


# --- how she is drawn, per part --------------------------------------------------------------


def test_the_parts_follow_the_layouts_with_the_phones_precedence():
    parts = pst.speaker_drawn_parts((1.0, 9.0), None, [(0.0, 2.0)], LAYOUTS)
    assert parts == [
        (1.0, 3.0, "full", None),  # the full-screen span, then `own`: drawn alike, merged
        (3.0, 6.0, "frame", CORNER),
        (6.0, 8.0, "hidden", None),
        (8.0, 9.0, "frame", LEFT),
    ]
    # A layer frame applies only where no layout covers the time.
    assert pst.speaker_drawn_at(12.0, CORNER, [], LAYOUTS) == ("frame", CORNER)
    assert pst.speaker_drawn_at(12.0, None, [], LAYOUTS) == ("full", None)
    assert pst.speaker_drawn_at(1.0, CORNER, [], LAYOUTS) == ("full", None)


def test_as_drawn_maps_each_part_by_its_own_layout():
    def sample(part):
        return [FACE], {"part": part}

    boxes, receipts = pst._as_drawn(sample, (0.0, 10.0), None, [], LAYOUTS)
    assert [r["part"] for r in receipts] == [(0.0, 3.0), (3.0, 6.0), (6.0, 8.0), (8.0, 10.0)]
    assert boxes == [
        FACE,  # own: full-frame
        pst.frame_face_box(FACE, CORNER),
        pst.frame_face_box(FACE, LEFT),  # the picture section adds no face
    ]


def test_without_layouts_as_drawn_is_exactly_what_it_was():
    def sample(part):
        return [FACE], {"part": part}

    for frame, spans in ((None, []), (CORNER, []), (CORNER, [(0.0, 3.0)])):
        assert pst._as_drawn(sample, (0.0, 10.0), frame, spans) == pst._as_drawn(
            sample, (0.0, 10.0), frame, spans, ()
        )


# --- the title / label placer ----------------------------------------------------------------


def test_a_row_over_her_own_take_moves_off_her_face_like_today():
    today, _ = _place(_row(0.5, 2.5))
    placed, _ = _place(_row(0.5, 2.5), layout_windows=LAYOUTS)
    assert placed["y_frac"] == today["y_frac"] > FACE.bottom


def test_a_row_over_a_corner_section_keeps_the_preset_spot():
    placed, receipt = _place(_row(3.5, 5.5), layout_windows=LAYOUTS)
    assert placed["y_frac"] == _row(3.5, 5.5)["y_frac"]
    assert receipt["status"] == "preset"


def test_a_row_over_a_picture_section_has_no_face_to_avoid():
    placed, receipt = _place(_row(6.2, 7.8), layout_windows=LAYOUTS)
    assert placed["y_frac"] == _row(6.2, 7.8)["y_frac"]
    assert receipt["status"] == "preset"


def test_a_row_across_a_switch_keeps_off_the_face_of_either_part():
    # 2-4 s: her own take (face at the top) then the corner: the full-frame face still counts.
    placed, receipt = _place(_row(2.0, 4.0), layout_windows=LAYOUTS)
    assert placed["y_frac"] > FACE.bottom
    assert receipt["full_screen_parts"] == 2


# --- the CTA ending's cut-out ----------------------------------------------------------------


def test_the_cutout_follows_the_section_drawing_her_small():
    sampled = [((0.5, 2.5), [FACE]), ((3.5, 5.5), [FACE]), ((6.5, 7.5), [FACE]), ((8.5, 9.5), [])]
    boxes = phone_cta_ending._cutout_boxes(sampled, None, [], LAYOUTS)
    assert len(boxes) == 2  # the corner sections only
    right, left = boxes
    assert right.left > 0.5 > left.right  # bottom-right, then bottom-left
    # Without layouts, a frame and a full-screen span: exactly today's boxes.
    legacy = phone_cta_ending._cutout_boxes(sampled, CORNER, [(0.0, 3.0)])
    assert legacy == phone_cta_ending._cutout_boxes(sampled, CORNER, [(0.0, 3.0)], ())
    assert len(legacy) == 3


# --- the caption styler's face map ------------------------------------------------------------


def _layout_background(*, with_layouts: bool = True) -> SubtitledBackground:
    frame = SubtitledSpeakerFrame(width_fraction=0.42, x_fraction=0.76, y_fraction=0.8)
    layouts = (
        SubtitledLayoutWindow(start_s=0.0, end_s=3.0, layout="own"),
        SubtitledLayoutWindow(start_s=3.0, end_s=6.0, layout="small", speaker_frame=frame),
        SubtitledLayoutWindow(start_s=6.0, end_s=8.0, layout="picture"),
    )
    return SubtitledBackground(
        kind="color", color="#111111", layout_windows=layouts if with_layouts else ()
    )


def test_the_styler_maps_faces_per_section(monkeypatch):
    monkeypatch.setattr(settings, "speaker_frame_caption_faces_enabled", True)
    policy: dict = {}
    styling._record_speaker_layout(policy, _layout_background())
    assert [w["layout"] for w in policy["_speaker_layout"]["layouts"]] == [
        "own",
        "small",
        "picture",
    ]
    face = {"start_s": 1.0, "end_s": 7.0, "box": (0.25, 0.05, 0.75, 0.42)}
    mapped = styling._faces_where_drawn([face], policy)
    assert [(m["start_s"], m["end_s"]) for m in mapped] == [(1.0, 3.0), (3.0, 6.0)]
    assert mapped[0]["box"] == face["box"]
    assert mapped[1]["box"][0] > 0.5  # drawn in the bottom-right corner
    # Where she is not in a corner, a caption keeps its x (the full-screen rule of item 16).
    assert styling._not_in_corner(policy["_speaker_layout"])[:2] == [
        {"start_s": 0.0, "end_s": 3.0},
        {"start_s": 6.0, "end_s": 1800.0},
    ]


def test_the_styler_records_no_layout_key_without_layouts(monkeypatch):
    monkeypatch.setattr(settings, "speaker_frame_caption_faces_enabled", True)
    policy: dict = {}
    styling._record_speaker_layout(policy, _layout_background(with_layouts=False))
    assert policy["_speaker_layout"] == {"frame": None, "full_screen": []}
    face = {"start_s": 1.0, "end_s": 7.0, "box": (0.25, 0.05, 0.75, 0.42)}
    assert styling._faces_where_drawn([face], policy) == [face]


# --- the worker's closing kwargs and list-label backdrop --------------------------------------


@pytest.mark.parametrize("with_layouts", [True, False])
def test_the_closing_kwargs_carry_the_layouts_only_when_there_are_some(monkeypatch, with_layouts):
    monkeypatch.setattr(settings, "speaker_frame_caption_faces_enabled", True)
    monkeypatch.setattr(gb, "_phone_list_label_card_boxes", lambda *_a: [])
    background = _layout_background() if with_layouts else _layout_background(with_layouts=False)
    kwargs = gb._closing_speaker_frame_kwargs(
        SimpleNamespace(background=background), (), [], _CANVAS
    )
    if not with_layouts:
        assert kwargs == {}  # no frame, no layouts: the ending is placed exactly as before
        return
    assert kwargs["speaker_frame"] is None
    assert [span[2] for span in kwargs["layout_windows"]] == ["own", "small", "picture"]
    assert kwargs["layout_windows"][1][3] == CORNER


def test_the_list_label_backdrop_is_where_a_section_shows_a_picture():
    lanes = SimpleNamespace(background=_layout_background())
    assert gb._list_label_backdrop_spans(lanes, 10.0) == ((3.0, 6.0), (6.0, 8.0))
