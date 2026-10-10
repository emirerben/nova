"""KRI-547 journey: the face-filled crop the server sends, as the iPhone receives it.

The face-filled vertical crop rests on one device-side fact: the iOS engine
(`KriaMediaEngine` ``Composition.transform``) applies a main-track clip's
``MediaTransform.position_x`` after its cover fill, so the creator sees the
window the server chose. No production recipe shifted a main-track clip before
KRI-547, so the server and the device share one checked-in contract:
``src/apps/ios/Tests/Fixtures/KRI547SpeakerFaceFill.json``.

This module builds that fixture through the production chain -- the worker's
decision (`decide_speaker_framing`, with an injected face sampler instead of
OpenCV), the phone Talking compiler (`compile_phone_subtitled_plan` with
``speaker_position_x``) and the device-render status the phone polls
(`make_device_request` / `DeviceRenderStatus`) -- and fails when the server's
output drifts from the checked-in file. `DeviceSpeakerFaceFillE2ETests` renders
each case through the device exporter over a colour-banded 1920x1080 clip and
measures the window that is actually on screen.

Regenerate after an intended server change, then re-run the native test:
    KRIA_REGENERATE_FIXTURES=1 pytest tests/pipeline/test_kri547_face_fill_device_fixture.py
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_recipe_shared import max_cover_shift_px
from app.pipeline.phone_speaker_framing import decide_speaker_framing
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS, compile_phone_subtitled_plan
from app.pipeline.phone_subtitled_title import source_box_to_canvas
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.phone_sources import PhoneSourceBinding

FIXTURE = Path(__file__).resolve().parents[3] / "ios/Tests/Fixtures/KRI547SpeakerFaceFill.json"
GENERATOR = "src/apps/api/tests/pipeline/test_kri547_face_fill_device_fixture.py"

SOURCE_WIDTH = 1920
SOURCE_HEIGHT = 1080
DURATION_S = 2.0
MEDIA_ID = "kri547-speaker"
JOB_ID = uuid.UUID("00000000-0000-4000-8000-000000000547")
# The native test draws the source as vertical bands, left to right, so every
# band edge on screen names one exact source x. Adjacent colours differ in at
# least one full channel, and none is black (black is a missing-picture edge).
BAND_WIDTH_PX = 240
BAND_RGB = [
    [255, 0, 0],
    [255, 255, 0],
    [0, 255, 0],
    [0, 255, 255],
    [0, 0, 255],
    [255, 0, 255],
    [255, 255, 255],
    [128, 128, 128],
]
# (case, steady face box left/right on the source, normalized). Top/bottom keep
# the face above the caption block (`FACE_BOTTOM_LIMIT_FRAC`).
CASES = [
    # The Kadıköy shape: a speaker in the left third; the picture slides right.
    ("face_left_third", 0.20, 0.34),
    # A face at the right edge: the shift clamps so no black edge shows.
    ("face_right_edge_clamped", 0.86, 0.98),
    # A centred face: zero shift, the engine's own centre crop (identity).
    ("face_centred", 0.43, 0.57),
]
FACE_TOP, FACE_BOTTOM = 0.20, 0.45


def _binding() -> PhoneSourceBinding:
    # The native test generates the clip, so the fingerprint is a placeholder:
    # the exporter is handed the file directly, never through the resolver.
    return PhoneSourceBinding(
        media_id=MEDIA_ID,
        proxy_path=f"users/owner/creation/analysis-proxy-{MEDIA_ID}.mp4",
        generation="547",
        original=OriginalMediaDescriptor(
            sha256="5" * 64,
            byte_count=547,
            duration_s=DURATION_S,
            width=SOURCE_WIDTH,
            height=SOURCE_HEIGHT,
            has_audio=False,
        ),
    )


def _steady_face(left: float, right: float):
    box = NormalizedBox(left, FACE_TOP, right, FACE_BOTTOM)

    def sample(path, anchors, **kwargs):
        regions = [ProtectedRegion(at - 0.1, at + 0.1, box, "face") for at in anchors]
        return regions, {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}

    return sample


def _cover() -> float:
    return max(_STORY_CANVAS.width / SOURCE_WIDTH, _STORY_CANVAS.height / SOURCE_HEIGHT)


def _visible_source_px(position_x: float) -> list[float]:
    """The source x range on screen once the engine cover-fills the clip centred
    and moves it ``position_x`` canvas px right: canvas x = (source x - W/2) *
    cover + canvas W/2 + position_x, solved for canvas x = 0 and canvas W."""
    cover = _cover()
    left = SOURCE_WIDTH / 2 - (_STORY_CANVAS.width / 2 + position_x) / cover
    return [round(left, 3), round(left + _STORY_CANVAS.width / cover, 3)]


def build_fixture() -> dict:
    cases = []
    for name, face_left, face_right in CASES:
        framing = decide_speaker_framing(
            "/tmp/kri547-speaker.mp4",
            keep_segments=[(0.0, DURATION_S)],
            display_width=SOURCE_WIDTH,
            display_height=SOURCE_HEIGHT,
            canvas=_STORY_CANVAS,
            # The phone Talking item's default: without the ask, bars.
            landscape_fit="fit",
            asked_by=["kri547-vertical"],
            sample=_steady_face(face_left, face_right),
        )
        assert framing.mode == "face_fill", (name, framing.receipt())
        recipe = compile_phone_subtitled_plan(
            (_binding(),),
            caption_cues=[],
            landscape_fit="fit",
            speaker_position_x=framing.position_x,
        )
        [speaker] = [
            clip for track in recipe.tracks if track.kind == "video" for clip in track.clips
        ]
        status = DeviceRenderStatus(
            phase="awaiting_device",
            request=make_device_request(
                job_id=JOB_ID, variant_id="subtitled", revision=1, recipe=recipe
            ),
        )
        window = framing.window
        assert window is not None
        payload = status.model_dump(mode="json")
        # A set on the model: dump it in a stable order so the pin never flaps
        # with the interpreter's hash seed.
        recipe_json = payload["request"]["recipe"]
        recipe_json["required_capabilities"] = sorted(recipe_json["required_capabilities"])
        cases.append(
            {
                "name": name,
                "face_box": [face_left, FACE_TOP, face_right, FACE_BOTTOM],
                "framing": {
                    "mode": framing.mode,
                    "reason": framing.reason,
                    "window": window.as_dict(),
                },
                "position_x": speaker.transform.position_x,
                "scale": speaker.transform.scale,
                "visible_source_px": _visible_source_px(speaker.transform.position_x),
                "status": payload,
            }
        )
    return {
        "contract": (
            "KRI-547 face-filled crop: the iOS engine must show visible_source_px of a "
            "1920x1080 speaker clip after the main-track MediaTransform in status"
        ),
        "generator": GENERATOR,
        "canvas": {"width": _STORY_CANVAS.width, "height": _STORY_CANVAS.height},
        "source": {
            "media_id": MEDIA_ID,
            "width": SOURCE_WIDTH,
            "height": SOURCE_HEIGHT,
            "duration_s": DURATION_S,
            "band_width_px": BAND_WIDTH_PX,
            "band_rgb": BAND_RGB,
        },
        "cover_scale": round(_cover(), 6),
        "max_cover_shift_px": round(
            max_cover_shift_px(SOURCE_WIDTH, SOURCE_HEIGHT, _STORY_CANVAS), 3
        ),
        "cases": cases,
    }


def test_checked_in_device_fixture_matches_the_server_compiler():
    built = build_fixture()
    if os.environ.get("KRIA_REGENERATE_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(built, indent=2, ensure_ascii=False) + "\n")
    assert FIXTURE.exists(), f"missing {FIXTURE}; regenerate with KRIA_REGENERATE_FIXTURES=1"
    assert json.loads(FIXTURE.read_text()) == built, (
        "the server's face-filled recipe changed; regenerate the iOS fixture with "
        "KRIA_REGENERATE_FIXTURES=1 and re-run DeviceSpeakerFaceFillE2ETests"
    )


@pytest.mark.parametrize("case", build_fixture()["cases"], ids=lambda case: case["name"])
def test_the_compiled_shift_shows_exactly_the_window_the_framing_chose(case):
    window = case["framing"]["window"]
    left_px, right_px = case["visible_source_px"]
    # The recipe's shift (floored to hundredths) and the framing's window
    # (rounded to 1e-5 of the width) name the same source pixels.
    assert left_px == pytest.approx(window["left"] * SOURCE_WIDTH, abs=0.05)
    assert right_px == pytest.approx(window["right"] * SOURCE_WIDTH, abs=0.05)
    assert 0 <= left_px and right_px <= SOURCE_WIDTH  # no black edge
    assert case["scale"] == 1
    # Titles and cards map the face through `source_box_to_canvas`; the window
    # must land exactly on the canvas there too.
    mapped = source_box_to_canvas(
        NormalizedBox(window["left"], 0.0, window["right"], 1.0),
        display_width=SOURCE_WIDTH,
        display_height=SOURCE_HEIGHT,
        canvas=_STORY_CANVAS,
        position_x=case["position_x"],
    )
    assert mapped is not None
    assert mapped.left == pytest.approx(0.0, abs=1e-4)
    assert mapped.right == pytest.approx(1.0, abs=1e-4)


def test_the_cases_cover_a_right_shift_a_clamped_left_shift_and_no_shift():
    by_name = {case["name"]: case for case in build_fixture()["cases"]}
    limit = max_cover_shift_px(SOURCE_WIDTH, SOURCE_HEIGHT, _STORY_CANVAS)
    assert by_name["face_left_third"]["position_x"] > 0
    assert by_name["face_right_edge_clamped"]["position_x"] == pytest.approx(-limit, abs=0.01)
    assert by_name["face_right_edge_clamped"]["visible_source_px"][1] == pytest.approx(
        SOURCE_WIDTH, abs=0.05
    )
    assert by_name["face_centred"]["position_x"] == 0
    # Every case shows at least two band edges, so the device can measure it.
    for case in by_name.values():
        left_px, right_px = case["visible_source_px"]
        edges = [x for x in range(BAND_WIDTH_PX, SOURCE_WIDTH, BAND_WIDTH_PX)]
        assert sum(left_px < x < right_px for x in edges) >= 2, case["name"]
