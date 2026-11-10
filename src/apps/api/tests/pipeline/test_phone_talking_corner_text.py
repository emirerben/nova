"""KRI-1342 item 7: corner text on a phone Talking edit keeps its corner, never sits on a face.

L2/L4 face avoidance (step inward, shrink, drop), L5 caption avoidance. A fake `measure` stands in
for the skia text measurement: the box is centred on ``y_frac`` and ``size_px / 1000`` tall.
"""

from __future__ import annotations

import pytest

from app.kria.recipes import Canvas
from app.pipeline.phone_talking_corner_text import (
    CORNER_TEXT_SOURCE,
    corner_text_rows,
    place_corner_row,
)
from app.pipeline.render_geometry import NormalizedBox
from app.schemas.edit_proposal import PinnedText

_CANVAS = Canvas(width=1080, height=1920)


def _measure(row: dict) -> NormalizedBox:
    half = float(row["size_px"]) / 2000
    return NormalizedBox(0.05, float(row["y_frac"]) - half, 0.30, float(row["y_frac"]) + half)


def _row(corner="top_left", y_frac=0.05, size_px=50.0) -> dict:
    return {
        "id": "guided-pinned-0",
        "text": "Yorumla",
        "y_frac": y_frac,
        "size_px": size_px,
        "source_params": {"source": CORNER_TEXT_SOURCE, "corner": corner},
    }


def test_l2_a_clear_corner_is_left_exactly_where_it_is():
    placed, receipt = place_corner_row(_row(), faces=[], captions=[], measure=_measure)

    assert placed is not None and placed["y_frac"] == 0.05 and placed["size_px"] == 50.0
    assert receipt["status"] == "placed" and receipt["moved_off"] == []
    assert not receipt["text_overlap"]


def test_l2_a_face_in_the_corner_pushes_the_top_row_down_inside_the_corner():
    face = NormalizedBox(0.0, 0.0, 0.4, 0.12)

    placed, receipt = place_corner_row(_row(), faces=[face], captions=[], measure=_measure)

    assert placed is not None
    assert placed["y_frac"] > 0.12  # off the (padded) face
    assert placed["y_frac"] <= 0.05 + 0.2 + 1e-6  # never leaves the corner zone
    assert placed["size_px"] == 50.0  # moved, not shrunk
    assert receipt["moved_off"] == ["face"]
    assert box_clear(placed, face)


def box_clear(row: dict, face: NormalizedBox) -> bool:
    return _measure(row).coverage_by(face.padded(0.01)) == 0.0


def test_l2_a_bottom_row_steps_up_not_down():
    face = NormalizedBox(0.0, 0.86, 0.4, 1.0)
    row = _row("bottom_left", y_frac=0.9)

    placed, _receipt = place_corner_row(row, faces=[face], captions=[], measure=_measure)

    assert placed is not None and placed["y_frac"] < 0.9
    assert box_clear(placed, face)


def test_l4_when_stepping_cannot_clear_the_face_the_row_shrinks():
    # At full size the lowest inward spot still touches the padded face; a smaller row clears it.
    face = NormalizedBox(0.0, 0.0, 0.4, 0.224)

    placed, receipt = place_corner_row(_row(), faces=[face], captions=[], measure=_measure)

    assert placed is not None
    assert receipt["shrink"] < 1.0
    assert placed["size_px"] == pytest.approx(50.0 * receipt["shrink"], abs=0.1)
    assert receipt["moved_off"] == ["face"]
    assert box_clear(placed, face)


def test_l4_a_face_covering_the_whole_corner_drops_the_row():
    face = NormalizedBox(0.0, 0.0, 0.6, 0.5)

    placed, receipt = place_corner_row(_row(), faces=[face], captions=[], measure=_measure)

    assert placed is None
    assert receipt["status"] == "face_blocked"
    assert receipt["corner"] == "top_left"


def test_l5_prefers_a_spot_clear_of_the_captions():
    captions = [NormalizedBox(0.0, 0.0, 0.5, 0.10)]
    placed, receipt = place_corner_row(_row(), faces=[], captions=captions, measure=_measure)

    assert placed is not None and placed["y_frac"] >= 0.10
    assert receipt["text_overlap"] is False


def test_l5_falls_back_to_face_only_when_captions_cover_every_spot():
    captions = [NormalizedBox(0.0, 0.0, 1.0, 1.0)]
    face = NormalizedBox(0.0, 0.0, 0.4, 0.12)

    placed, receipt = place_corner_row(_row(), faces=[face], captions=captions, measure=_measure)

    assert placed is not None
    assert receipt["text_overlap"] is True
    assert box_clear(placed, face)  # the face is still never covered


def test_l2_a_face_blocked_row_stays_blocked_even_with_clear_captions():
    face = NormalizedBox(0.0, 0.0, 1.0, 1.0)

    placed, receipt = place_corner_row(_row(), faces=[face], captions=[], measure=_measure)

    assert placed is None and receipt["status"] == "face_blocked"


def test_c8_rows_are_tagged_whole_video_only_when_the_pin_has_no_range():
    pins = [
        PinnedText(text="Yorumla", corner="top_left"),
        PinnedText(text="Part 8", corner="bottom_right", start_s=1.0, end_s=4.0),
    ]

    rows = corner_text_rows(pins, talk_end_s=12.0, canvas=_CANVAS)

    assert [r["source_params"]["source"] for r in rows] == [CORNER_TEXT_SOURCE] * 2
    assert [r["source_params"]["corner"] for r in rows] == ["top_left", "bottom_right"]
    assert [r["source_params"]["whole_video"] for r in rows] == [True, False]
    assert (rows[0]["start_s"], rows[0]["end_s"]) == (0.0, 12.0)
    assert (rows[1]["start_s"], rows[1]["end_s"]) == (1.0, 4.0)


# --- L1: the planner keeps a corner pin as a pin only while the corner lane is open -----------


def _talking_manifest():
    from app.services import creator_capabilities as capabilities

    clip = "analysis-proxy-ios-1.mp4"
    return clip, capabilities.resolve_creator_manifest(
        item_id="item-talking",
        edit_format="subtitled",
        media=[{"media_id": clip, "kind": "video", "duration_s": 40.0}],
        phone_source_media_ids=[clip],
        phone_rendering_allowed=True,
    )


def _pinned_strategy(clip: str):
    from app.agents._schemas.creator_agent import CreativeStrategy

    return CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=[clip],
        pinned_texts=[PinnedText(text="Yorumla", corner="top_left")],
    )


def test_l1_open_lane_leaves_a_whole_video_top_pin_as_a_pin(prod_profile, monkeypatch):
    from app.config import settings
    from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2
    from app.services.creator_capabilities import phone_talking_corner_text_open

    monkeypatch.setattr(settings, "phone_talking_corner_text_enabled", True)
    clip, manifest = _talking_manifest()
    assert phone_talking_corner_text_open(manifest)

    checked = check_strategy_for_runtime_v2(manifest, _pinned_strategy(clip))

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title is None
    assert [pin.text for pin in checked.strategy.pinned_texts or []] == ["Yorumla"]


def test_l1_closed_lane_turns_the_same_pin_into_the_opening_title(prod_profile, monkeypatch):
    from app.config import settings
    from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2
    from app.services.creator_capabilities import phone_talking_corner_text_open

    monkeypatch.setattr(settings, "phone_talking_corner_text_enabled", False)
    clip, manifest = _talking_manifest()
    assert not phone_talking_corner_text_open(manifest)

    checked = check_strategy_for_runtime_v2(manifest, _pinned_strategy(clip))

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title == "Yorumla"
    assert not checked.strategy.pinned_texts


# --- review of #2256: clip-scoped lines and lines on screen together ------------------------


def test_review_clip_one_on_a_one_take_edit_runs_over_that_take():
    from app.pipeline.phone_talking_corner_text import unplaced_pins
    from app.tasks.generative_build import _talking_clip_windows

    pins = [PinnedText(text="Yorumla", corner="top_left", clip=1)]
    windows = _talking_clip_windows([(0.0, 10.0), (12.0, 40.0)], [])

    rows = corner_text_rows(pins, talk_end_s=38.0, canvas=_CANVAS, clip_windows=windows)

    assert [(r["start_s"], r["end_s"]) for r in rows] == [(0.0, 38.0)]
    assert unplaced_pins(pins, talk_end_s=38.0, clip_windows=windows) == []


def test_review_clip_two_runs_over_the_second_speaker_for_the_seconds_it_plays():
    from app.tasks.generative_build import _talking_clip_windows

    windows = _talking_clip_windows([(0.0, 20.0)], [15.0])
    pins = [PinnedText(text="Bölüm 8", corner="bottom_right", clip=2)]

    rows = corner_text_rows(pins, talk_end_s=35.0, canvas=_CANVAS, clip_windows=windows)

    assert [(r["start_s"], r["end_s"]) for r in rows] == [(20.0, 35.0)]
    assert rows[0]["source_params"]["whole_video"] is False


def test_review_a_clip_the_edit_does_not_have_is_said_not_dropped_silently():
    from app.pipeline.phone_talking_corner_text import unplaced_pins

    pins = [PinnedText(text="Yorumla", corner="top_left", clip=3)]
    windows = [(0.0, 20.0), (20.0, 35.0)]

    assert corner_text_rows(pins, talk_end_s=35.0, canvas=_CANVAS, clip_windows=windows) == []
    assert unplaced_pins(pins, talk_end_s=35.0, clip_windows=windows) == [
        {"text": "Yorumla", "corner": "top_left", "status": "no_window", "clip": 3}
    ]


def test_review_two_lines_on_screen_together_never_land_on_each_other(monkeypatch):
    """Emir's probe: two 52 px top-left lines at y .12 / .158 and a face ending at y .20 both
    slid to ~.24 and overlapped 92.6 %. Now the second keeps clear of the first, under it."""
    from app.pipeline import phone_subtitled_title as pst
    from app.pipeline.phone_talking_corner_text import place_corner_text

    face = NormalizedBox(0.0, 0.0, 0.6, 0.20)
    monkeypatch.setattr(
        pst, "caption_window_faces", lambda *a, **k: ([face], [], {"faces": "detected"})
    )
    monkeypatch.setattr(pst, "_measure_title_box", lambda row, canvas: _measure(row))
    rows = [
        {**_row(y_frac=0.12, size_px=52.0), "id": "a", "start_s": 0.0, "end_s": 10.0},
        {**_row(y_frac=0.158, size_px=52.0), "id": "b", "start_s": 0.0, "end_s": 10.0},
    ]

    placed, receipt = place_corner_text(
        rows,
        clip_path="take.mp4",
        keep_segments=[(0.0, 10.0)],
        display_width=1080,
        display_height=1920,
        canvas=_CANVAS,
        scale=1.0,
    )

    assert [entry["status"] for entry in receipt["rows"]] == ["placed", "placed"]
    first, second = (_measure(row) for row in placed)
    assert first.intersection_area(second) == 0
    assert first.top >= face.bottom and second.top >= first.bottom  # the stack keeps its order


def test_review_lines_at_different_times_may_share_a_spot(monkeypatch):
    from app.pipeline import phone_subtitled_title as pst
    from app.pipeline.phone_talking_corner_text import place_corner_text

    monkeypatch.setattr(pst, "caption_window_faces", lambda *a, **k: ([], [], {}))
    monkeypatch.setattr(pst, "_measure_title_box", lambda row, canvas: _measure(row))
    rows = [
        {**_row(y_frac=0.12), "id": "a", "start_s": 0.0, "end_s": 4.0},
        {**_row(y_frac=0.12), "id": "b", "start_s": 4.0, "end_s": 8.0},
    ]

    placed, _ = place_corner_text(
        rows,
        clip_path=None,
        keep_segments=[(0.0, 8.0)],
        display_width=1080,
        display_height=1920,
        canvas=_CANVAS,
        scale=1.0,
    )

    assert [row["y_frac"] for row in placed] == [0.12, 0.12]
