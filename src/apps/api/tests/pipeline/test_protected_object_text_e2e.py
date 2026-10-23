"""KRI-827 E2E: text stays off the face and the object the creator marked, for the WHOLE
time each text is on screen, through crop and cut changes.

Failure modes (written before the code):

F1  Text clear at the first frame only; the plate moves under it later.
F2  Sampling too sparse misses a fast pass of the plate through the text.
F3  The plate is occluded / absent at some anchors and that is read as "clear".
F4  The tracker drifts onto a different object (a decoy) and reports success.
F5  The crop (cover-fill, letterbox scale, KRI-547 ``position_x``) is not applied to the
    object box, so the box is checked where the plate is not drawn.
F6  A source swap reuses the old source's boxes (media id / generation mismatch).
F7  A speech-cleanup cut (``keep_segments``) shifts output time vs source time and the
    box at the wrong moment is used.
F10 Face and object both block a spot and the old "face wins, other text dropped" fallback
    drops the object constraint.
F11 No safe spot exists: the text lands on the plate and the receipt still says clear.
F13 Captions ignore the object while the title honours it.
F17 The tracker timed out / failed and the receipt says verified anyway.
F18 Object protection runs when the creator never asked (default must be unchanged).

The harness (`app.cli.verify_protected_text`) draws synthetic clips whose plate path is
known per frame, runs the production placement (`place_talking_title`, the spoken-text
rows, the caption look chooser) with the real tracker subprocess, renders every text row's
real Skia ink, and measures ink-on-plate at 10 Hz across each row's visible interval. It
writes ``report.json`` + ``montage.png``.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg"),
    # Ten synthetic clips, a real tracker subprocess each and real Skia ink: ~30 s.
    pytest.mark.timeout(300),
]


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    from app.cli.verify_protected_text import run_scenarios

    out_dir = str(tmp_path_factory.mktemp("protected-verify"))
    result = run_scenarios(out_dir)
    result["_out_dir"] = out_dir
    return result


def _scenario(report: dict, name: str) -> dict:
    return next(s for s in report["scenarios"] if s["name"] == name)


def test_no_text_row_is_called_clear_while_it_covers_the_object(report):
    """The core guard against false success, over every scenario and every row."""
    assert report["false_success"] == []


@pytest.mark.parametrize(
    "name",
    [
        "static_plate_below_face",
        "plate_moves_into_band",
        "fast_pass",
        "cleanup_cut",
        "landscape_crop",
        "decoy_plate",
    ],
)
def test_supported_scenarios_keep_every_text_off_the_plate_the_whole_time(report, name):
    scenario = _scenario(report, name)
    assert scenario["verdict"] == "clear", scenario
    for row in scenario["rows"]:
        assert row["measured"] == "clear", row
        assert row["claimed"] == "clear", row


def test_a_first_frame_only_placement_would_have_covered_the_moving_plate(report):
    """F1: the control can see the failure it guards against."""
    scenario = _scenario(report, "plate_moves_into_band")
    assert scenario["first_frame_only_control"]["measured"] == "collision"


def test_an_occluded_plate_is_never_called_clear(report):
    scenario = _scenario(report, "occluded_plate")
    assert scenario["verdict"] == "unverified"
    # Still kept clear of where the plate was last seen.
    assert all(row["measured"] == "clear" for row in scenario["rows"])


def test_a_plate_filling_the_frame_is_a_truthful_collision(report):
    scenario = _scenario(report, "full_frame_plate")
    assert scenario["verdict"] == "collision"
    assert any(row["claimed"] == "collision" for row in scenario["rows"])


def test_a_tracker_timeout_is_unverified(report):
    assert _scenario(report, "tracker_timeout")["verdict"] == "unverified"


def test_a_swapped_source_never_reuses_the_old_selection(report):
    scenario = _scenario(report, "source_swapped")
    assert scenario["verdict"] == "invalidated"
    assert scenario["tracked"] is False


def test_the_tracker_stays_on_the_marked_plate_past_a_decoy(report):
    scenario = _scenario(report, "decoy_plate")
    assert scenario["track_min_iou"] >= 0.5


def test_captions_move_off_the_plate_too(report):
    scenario = _scenario(report, "static_plate_below_face")
    captions = [row for row in scenario["rows"] if row["kind"] == "captions"]
    assert captions and all(row["measured"] == "clear" for row in captions)


def test_artifacts_are_written(report):
    out_dir = report["_out_dir"]
    with open(os.path.join(out_dir, "report.json")) as handle:
        stored = json.load(handle)
    assert stored["false_success"] == []
    assert os.path.getsize(os.path.join(out_dir, "montage.png")) > 10_000


def test_captions_are_measured_and_moved_in_the_look_the_render_draws() -> None:
    """F13 with the phone text-style policy on: the captions the render draws use the
    policy look (its font, colour, spot), so that look is what is measured against the
    plate, and moving them off it keeps the creator's font and colour (only y changes)."""
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.object_track import ObjectTrack, TrackAnchor
    from app.pipeline.phone_captions import PhoneCaptionLook
    from app.pipeline.phone_protected_text import choose_caption_look
    from app.pipeline.render_geometry import NormalizedBox
    from app.services.phone_text_styling import phone_text_policy_scope

    plate = NormalizedBox(0.05, 0.40, 0.95, 0.62)
    track = ObjectTrack(
        label="plate",
        media_id="clip-1",
        generation="g1",
        seed_t_s=0.0,
        seed_box=plate,
        anchors=tuple(TrackAnchor(i * 0.1, plate, "tracked") for i in range(0, 61)),
    )
    cues = [
        {"text": "this is the dulce de leche", "start_s": 0.5, "end_s": 2.5},
        {"text": "and the oblea goes on top", "start_s": 2.5, "end_s": 5.5},
    ]
    # The policy draws the captions mid-frame, right on the plate, in its own font/colour.
    policy_look = PhoneCaptionLook(
        font_family="TikTok Sans Bold", text_color="#FFD400", position_y_frac=0.5
    )
    common = {
        "caption_style": "sentence",
        "canvas": PORTRAIT,
        "objects": [track],
        "faces": [],
        "keep_segments": [(0.0, 6.0)],
        "display_width": 1080,
        "display_height": 1920,
    }
    with phone_text_policy_scope({"caption_look": policy_look}):
        look, receipt = choose_caption_look(cues, **common)
    assert receipt["look"] == "policy"
    assert receipt["evaluated"][0]["position_y_frac"] == 0.5
    assert receipt["evaluated"][0]["object_lines"] > 0, "the drawn spot covers the plate"
    assert receipt["status"] == "moved" and receipt["object_collision"] is False
    assert look is not None
    assert (look.font_family, look.text_color) == ("TikTok Sans Bold", "#FFD400")
    assert look.position_y_frac != 0.5

    # Without a policy the default bottom band is already clear: nothing moves (F18).
    look, receipt = choose_caption_look(cues, **common)
    assert receipt["look"] == "default" and receipt["status"] == "preset" and look is None
