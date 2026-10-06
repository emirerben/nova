"""KRI-467: the opening title on a phone Talking (subtitled) edit.

`talking_title_element` builds the row (narrated look, confirmed or default
timing), `place_talking_title` keeps it off the speaker's face and clear of
the captions, and `compile_phone_subtitled_plan(text_elements=...)` draws it
under the captions.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE
from app.config import settings
from app.kria.recipes import Canvas
from app.pipeline.narrated_title import narrated_title_placement
from app.pipeline.phone_subtitled_plan import TEXT_LAYER_PREFIX, compile_phone_subtitled_plan
from app.pipeline.phone_subtitled_title import (
    _FALLBACK_FACE_BOX,
    CAPTION_BAND_TOP_FRAC,
    TALKING_TITLE_ELEMENT_ID,
    choose_title_y_frac,
    first_cue_word_end_s,
    place_talking_title,
    source_box_to_canvas,
    talking_title_element,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.silence_cut import CutPlan, Removal
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding

_CANVAS = Canvas(width=1080, height=1920)
_TITLE = "3 sourdough mistakes"


def _row(**changes) -> dict:
    row = talking_title_element(
        _TITLE,
        duration_s=2.0,
        first_word_end_s=None,
        timeline_duration_s=30.0,
        canvas=_CANVAS,
    )
    assert row is not None
    return {**row, **changes}


# --- the row -----------------------------------------------------------------------------


def test_title_holds_for_the_confirmed_seconds_in_the_narrated_look():
    row = _row()

    assert row["id"] == TALKING_TITLE_ELEMENT_ID
    assert row["text"] == _TITLE
    assert (row["start_s"], row["end_s"]) == (0.0, 2.0)
    assert row["role"] == "generative_intro"
    assert row["effect"] == "fade-in"
    # Same look and fit as the voiceover title, spelled out for the editor.
    for key, value in narrated_title_placement(_TITLE, canvas=_CANVAS, explicit=True).items():
        assert row[key] == value
    # Editable: no read-only marker.
    assert row["source_params"] == {"source": "opening_title"}


@pytest.mark.parametrize(
    ("first_word_end_s", "expected"),
    [(0.4, 1.4), (0.0, 1.0), (5.0, 3.0), (None, 3.0)],
    ids=["after-first-word", "floor", "cap", "no-speech"],
)
def test_without_a_confirmed_hold_the_title_fades_after_the_first_word(first_word_end_s, expected):
    row = talking_title_element(
        _TITLE,
        duration_s=None,
        first_word_end_s=first_word_end_s,
        timeline_duration_s=30.0,
        canvas=_CANVAS,
    )

    assert row is not None
    assert row["end_s"] == pytest.approx(max(0.5, expected))


def test_title_is_clamped_to_the_clip_and_skipped_when_empty():
    short = talking_title_element(
        _TITLE, duration_s=4.0, first_word_end_s=None, timeline_duration_s=1.2, canvas=_CANVAS
    )
    assert short is not None and short["end_s"] == 1.2
    assert (
        talking_title_element(
            "   ", duration_s=2.0, first_word_end_s=None, timeline_duration_s=30.0, canvas=_CANVAS
        )
        is None
    )


def test_first_word_end_reads_the_cut_timeline_cues():
    cues = [
        {
            "text": "Mistake one",
            "start_s": 0.3,
            "end_s": 1.4,
            "words": [
                {"text": "Mistake", "start_s": 0.3, "end_s": 0.7},
                {"text": "one", "start_s": 0.8, "end_s": 1.4},
            ],
        },
    ]
    assert first_cue_word_end_s(cues) == 0.7
    assert first_cue_word_end_s([{"text": "Hi", "start_s": 0.0, "end_s": 0.9}]) == 0.9
    assert first_cue_word_end_s([]) is None


# --- source frame -> canvas --------------------------------------------------------------


def test_a_portrait_speaker_maps_one_to_one():
    box = NormalizedBox(0.3, 0.2, 0.7, 0.5)

    assert source_box_to_canvas(
        box, display_width=1080, display_height=1920, canvas=_CANVAS
    ) == pytest.approx(box)


def test_a_cropped_landscape_speaker_spreads_the_face_wider():
    mapped = source_box_to_canvas(
        NormalizedBox(0.45, 0.2, 0.55, 0.5), display_width=1920, display_height=1080, canvas=_CANVAS
    )

    assert mapped is not None
    assert mapped.left < 0.45 and mapped.right > 0.55
    assert (mapped.top, mapped.bottom) == pytest.approx((0.2, 0.5))


def test_a_letterboxed_speaker_squeezes_the_face_toward_the_middle():
    scale = (1080 / 1920) / (1920 / 1080)  # contain / cover for 1920x1080 into 1080x1920
    mapped = source_box_to_canvas(
        NormalizedBox(0.0, 0.0, 1.0, 1.0),
        display_width=1920,
        display_height=1080,
        canvas=_CANVAS,
        scale=scale,
    )

    assert mapped is not None
    assert (mapped.left, mapped.right) == pytest.approx((0.0, 1.0))
    assert mapped.top == pytest.approx(0.5 - 0.5 * 1080 * 1080 / 1920 / 1920)


# --- placement ---------------------------------------------------------------------------


def _probe() -> NormalizedBox:
    return NormalizedBox(0.1, 0.1, 0.9, 0.2)


def test_the_preset_spot_stays_when_no_face_is_under_it():
    y, decision = choose_title_y_frac(_probe(), 0.15, [NormalizedBox(0.3, 0.3, 0.7, 0.5)])

    assert (y, decision["status"]) == (0.15, "preset")


def test_a_face_under_the_preset_moves_the_title_below_it_and_above_the_captions():
    face = NormalizedBox(0.3, 0.08, 0.7, 0.4)

    y, decision = choose_title_y_frac(_probe(), 0.15, [face])

    assert decision["status"] == "moved"
    assert y == 0.5
    assert y + 0.05 <= CAPTION_BAND_TOP_FRAC


def test_a_face_filling_the_frame_takes_the_least_covered_spot():
    y, decision = choose_title_y_frac(_probe(), 0.15, [NormalizedBox(0.0, 0.0, 1.0, 0.9)])

    assert decision["status"] == "best_effort"
    assert y in (0.15, 0.5, 0.44, 0.38)


def _sampler(regions, *, decoded=6, error=None, calls=None):
    def sample(path, anchors, **kwargs):
        if calls is not None:
            calls.append(list(anchors))
        if error is not None:
            raise error
        return regions, {"attempted": len(anchors), "decoded": decoded, "detected": len(regions)}

    return sample


def _place(row, sample, *, keep=((0.0, 30.0),)):
    return place_talking_title(
        row,
        clip_path="/tmp/speaker.mp4",
        keep_segments=list(keep),
        display_width=1080,
        display_height=1920,
        canvas=_CANVAS,
        sample=sample,
    )


def test_a_clear_frame_keeps_the_preset_spot():
    placed, receipt = _place(_row(), _sampler([]))

    assert placed["y_frac"] == _row()["y_frac"]
    assert (receipt["faces"], receipt["status"]) == ("none", "preset")


def test_the_speakers_face_pushes_the_title_down():
    face = ProtectedRegion(0.0, 2.0, NormalizedBox(0.25, 0.05, 0.75, 0.42), "face")

    placed, receipt = _place(_row(), _sampler([face]))

    assert placed["y_frac"] > 0.42
    assert receipt["faces"] == "detected"
    assert receipt["status"] == "moved"
    # Everything else about the row is unchanged.
    assert {k: v for k, v in placed.items() if k != "y_frac"} == {
        k: v for k, v in _row().items() if k != "y_frac"
    }


@pytest.mark.parametrize(
    "sample",
    [_sampler([], error=RuntimeError("no ffmpeg")), _sampler([], decoded=1)],
    ids=["sampler-error", "too-few-frames"],
)
def test_an_unconfirmed_frame_protects_a_talk_to_camera_face(sample):
    """Never assume there's no face (KRI-183)."""
    placed, receipt = _place(_row(), sample)

    assert receipt["faces"] == "fallback"
    assert placed["y_frac"] - 0.05 > _FALLBACK_FACE_BOX.bottom - 0.1
    assert placed["y_frac"] != _row()["y_frac"]


def test_faces_are_sampled_in_source_time_through_the_cleanup_cut():
    """The title lives in the first 2 s of the CUT video; with [0.5, 1.5) cut
    out, its later anchors read the source a second later."""
    calls: list = []

    _place(_row(), _sampler([], calls=calls), keep=((0.0, 0.5), (1.5, 30.0)))

    [anchors] = calls
    assert anchors[0] < 0.5
    assert anchors[-1] == pytest.approx(1.0 + 2.0 * 11 / 12, abs=1e-3)


# --- the recipe --------------------------------------------------------------------------


def test_the_title_compiles_under_the_captions(monkeypatch):
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "positionedText", "animatedText"],
    )
    recipe = compile_phone_subtitled_plan((_binding(),), caption_cues=_CUES, text_elements=[_row()])

    ids = [layer.id for layer in recipe.text_layers]
    assert ids == [f"{TEXT_LAYER_PREFIX}0", "caption-0", "caption-1"]
    title = recipe.text_layers[0]
    assert (title.start, title.end, title.effect) == (0.0, 2.0, "fade-in")
    assert "font-PlayfairDisplay-Bold.ttf" in {asset.id for asset in recipe.assets}
    validate_phone_pilot_recipe(recipe)


def test_no_text_is_byte_identical_to_before():
    base = compile_phone_subtitled_plan((_binding(),), caption_cues=_CUES)

    assert compile_phone_subtitled_plan((_binding(),), caption_cues=_CUES, text_elements=[]) == base


def test_caption_mirrors_and_removed_rows_never_draw():
    mirror = {
        "id": "mirror",
        "text": "Hello everyone",
        "start_s": 0.0,
        "end_s": 1.5,
        "role": "generative_sequence",
        "source_params": {"source": CAPTION_CUE_SOURCE, "key": "0"},
    }
    removed = _row(id="gone", removed=True)

    recipe = compile_phone_subtitled_plan(
        (_binding(),), caption_cues=_CUES, text_elements=[mirror, removed, _row()]
    )

    assert [layer.id for layer in recipe.text_layers] == ["text-0", "caption-0", "caption-1"]


def test_text_past_the_clip_ends_with_it_instead_of_failing_the_recipe():
    """The phone can measure the clip a hair shorter than the proxy the title
    was clamped to; an edited row can also run past the end."""
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=8.0),),
        caption_cues=_CUES,
        text_elements=[_row(end_s=8.04), _row(id="late", start_s=8.5, end_s=9.0)],
    )

    [title] = [layer for layer in recipe.text_layers if layer.id.startswith(TEXT_LAYER_PREFIX)]
    assert (title.start, title.end) == (0.0, 8.0)
    assert recipe.duration == pytest.approx(8.0)


def test_a_cleanup_cut_keeps_the_title_in_the_first_seconds_of_the_cut_video():
    cut = CutPlan(
        keep_segments=[(0.0, 0.5), (1.5, 12.0)],
        removed=[Removal(start_s=0.5, end_s=1.5, reason="test")],
        time_saved_s=1.0,
    )

    recipe = compile_phone_subtitled_plan(
        (_binding(),), caption_cues=_CUES, cut_plan=cut, text_elements=[_row()]
    )

    title = recipe.text_layers[0]
    assert (title.start, title.end) == (0.0, 2.0)
    assert recipe.duration == pytest.approx(11.0)
