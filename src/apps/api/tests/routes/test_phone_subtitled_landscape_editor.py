"""Landscape output for phone-rendered Talking (`subtitled`): capability + editor Save.

Real boundaries: the capability map is built by `_editor_capabilities`, Save runs
`prepare_editor_commit` -> `prepare_phone_editor_commit` -> the real
`compile_phone_subtitled_plan`, and the assertions read the pinned device recipe.
Failure-mode list: the session scratchpad's `backend-failure-modes.md` (C*/S* rows).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.kria.protected_targets import PROTECTED_TEXT_FIELD
from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from tests.routes.test_editor_commit import _arm_every_editor_lane, _job
from tests.routes.test_phone_subtitled_editor_commit import (
    _FACE_SHIFT,
    _face_filled_phone_job,
    _letterboxed_phone_job,
    _main_track_scales,
    _main_track_shifts,
    _resave,
    talking_head_job,
)


@pytest.fixture(autouse=True)
def _landscape_on(monkeypatch):
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "true")


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


def _canvas(job) -> tuple[int, int]:
    canvas = device_status(job, "subtitled").request.recipe.canvas
    return canvas.width, canvas.height


def _caps(job) -> dict:
    return gj._editor_capabilities(job, _variant(job))


# --- capability (C*) ----------------------------------------------------------------


def test_device_talking_advertises_orientation_when_the_lane_is_open(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    assert _caps(job)["orientation"] == {"editable": True, "value": "portrait", "reason": None}


def test_the_multi_clip_talking_head_is_open_too(monkeypatch):
    job, _recipe = talking_head_job(monkeypatch)
    assert _variant(job)["resolved_archetype"] == "subtitled"
    assert _caps(job)["orientation"]["editable"] is True


def test_orientation_closes_with_its_own_reason_when_the_output_flag_is_off(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", False)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "false")
    orientation = _caps(job)["orientation"]
    assert (orientation["editable"], orientation["reason"]) == (False, "disabled")


def test_orientation_closes_when_the_talking_save_lane_is_off(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", False)
    orientation = _caps(job)["orientation"]
    assert (orientation["editable"], orientation["reason"]) == (False, "phone_edit_unsupported")
    # ... and Save agrees (never advertise what Save refuses, or the reverse).
    with pytest.raises(HTTPException) as error:
        _resave(job, orientation="landscape")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "phone_edit_unsupported"


def test_cloud_subtitled_stays_closed_and_save_refuses_landscape(monkeypatch):
    _arm_every_editor_lane(monkeypatch)
    job = _job(resolved_archetype="subtitled")
    variant = job.assembly_plan["variants"][0]
    assert variant.get("render_destination") != "device"
    assert gj._editor_capabilities(job, variant)["orientation"] == {
        "editable": False,
        "value": "portrait",
        "reason": "orientation_unsupported",
    }
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(
            job,
            variant["variant_id"],
            gj.EditorCommitRequest(orientation="landscape", base_generation=""),
            user_id="owner",
            plan_item_id="item",
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "orientation_unsupported"


@pytest.mark.parametrize("archetype", ["narrated", "talking_head"])
def test_other_archetypes_stay_orientation_unsupported(monkeypatch, archetype):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    variant = {**_variant(job), "resolved_archetype": archetype}
    assert gj._orientation_unsupported_reason(variant) == "orientation_unsupported"
    assert gj._editor_capabilities(job, variant)["orientation"]["editable"] is False


def test_a_talking_variant_with_a_collage_or_visual_blocks_keeps_the_generic_closure(
    monkeypatch,
):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    variant = {**_variant(job), "visual_blocks": [{"id": "b"}]}
    assert gj._orientation_unsupported_reason(variant) == "orientation_unsupported"


def test_a_landscape_talking_variant_shows_its_canvas_and_no_bars(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    _resave(job, orientation="landscape")
    caps = _caps(job)
    assert caps["orientation"] == {"editable": True, "value": "landscape", "reason": None}
    assert caps["landscape_fit"] == {
        "editable": False,
        "value": "fill",
        "reason": "landscape_output",
    }


# --- Save (S*) ----------------------------------------------------------------------


def test_save_landscape_recompiles_onto_the_wide_canvas_without_bars(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    assert _canvas(job) == (1080, 1920)
    assert all(scale < 1 for scale in _main_track_scales(device_status(job, "subtitled").request))

    prep = _resave(job, orientation="landscape")

    assert prep["render_destination"] == "device"
    assert prep["render_task_id"] is None
    assert _canvas(job) == (1920, 1080)
    # A 1920x1080 take on a 1920x1080 canvas: identity, never the letterbox scale.
    assert _main_track_scales(device_status(job, "subtitled").request) == [1] * len(
        _main_track_scales(device_status(job, "subtitled").request)
    )
    variant = _variant(job)
    assert (variant["orientation"], variant["landscape_fit"]) == ("landscape", "fill")
    assert variant["render_status"] == "awaiting_device"


def test_a_stale_bars_value_in_the_same_save_is_ignored_on_landscape(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    _resave(job, orientation="landscape", landscape_fit="fit")
    assert _canvas(job) == (1920, 1080)
    assert _variant(job)["landscape_fit"] == "fill"
    assert all(s == 1 for s in _main_track_scales(device_status(job, "subtitled").request))


def test_landscape_drops_the_face_fill_shift_and_portrait_brings_it_back(monkeypatch):
    job = _face_filled_phone_job(monkeypatch, cut=True)
    receipt_before = dict(_variant(job)["speaker_framing"])

    _resave(job, orientation="landscape")
    shifts = _main_track_shifts(device_status(job, "subtitled").request)
    assert shifts and all(shift == (1, 0) for shift in shifts)
    assert _canvas(job) == (1920, 1080)
    # The receipt says why nothing was face-filled, and keeps its window and prior verdict
    # so switching back can use them.
    framing = _variant(job)["speaker_framing"]
    assert (framing["mode"], framing["reason"]) == ("centre_fill", "landscape_canvas")
    assert framing["window"] == receipt_before["window"]
    assert framing["before_landscape"] == {"mode": "face_fill", "reason": "face_in_window"}

    _resave(job, orientation="portrait")
    assert _canvas(job) == (1080, 1920)
    shifts = _main_track_shifts(device_status(job, "subtitled").request)
    assert shifts and all(shift == (1, _FACE_SHIFT) for shift in shifts)
    assert _variant(job)["orientation"] == "portrait"
    framing = _variant(job)["speaker_framing"]
    assert (framing["mode"], framing["reason"]) == ("face_fill", "face_in_window")
    assert "before_landscape" not in framing


def test_switching_back_to_portrait_reopens_bars_and_crop(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    _resave(job, orientation="landscape")
    _resave(job, orientation="portrait")
    assert _canvas(job) == (1080, 1920)
    assert _caps(job)["landscape_fit"]["editable"] is True
    _resave(job, landscape_fit="fit")
    assert all(s == 0.31640625 for s in _main_track_scales(device_status(job, "subtitled").request))


def test_a_save_without_an_orientation_section_keeps_the_canvas(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=True)
    _resave(job, orientation="landscape")
    _resave(job, sound_effects=[])
    assert _canvas(job) == (1920, 1080)
    assert _variant(job)["orientation"] == "landscape"
    # A portrait variant is untouched by a section-less Save.
    portrait = _letterboxed_phone_job(monkeypatch, cut=False)
    _resave(portrait, sound_effects=[])
    assert _canvas(portrait) == (1080, 1920)
    assert _variant(portrait).get("orientation") in (None, "portrait")


def test_the_canvas_is_read_from_the_pinned_recipe_when_the_variant_has_no_orientation(
    monkeypatch,
):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    _resave(job, orientation="landscape")
    _variant(job).pop("orientation")  # e.g. a row written before the key existed
    _resave(job, sound_effects=[])
    assert _canvas(job) == (1920, 1080)


def test_the_multi_clip_talking_head_keeps_its_cutaways_on_the_wide_canvas(monkeypatch):
    from app.pipeline.phone_subtitled_plan import CUTAWAY_TRACK_ID

    job, old_recipe = talking_head_job(monkeypatch)
    old_cutaways = next(t for t in old_recipe.tracks if t.id == CUTAWAY_TRACK_ID)

    _resave(job, orientation="landscape")

    new = device_status(job, "subtitled").request.recipe
    assert (new.canvas.width, new.canvas.height) == (1920, 1080)
    tracks = {track.id: track for track in new.tracks}
    assert [c.source_asset_id for c in tracks["subtitled"].clips] == ["speaker"]
    assert [c.source_asset_id for c in tracks[CUTAWAY_TRACK_ID].clips] == [
        c.source_asset_id for c in old_cutaways.clips
    ]
    assert new.duration == pytest.approx(20.0)


def test_a_canvas_change_marks_the_protected_text_receipt_stale(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    receipt = {
        "version": 1,
        "verdict": "clear",
        "targets": [{"status": "clear"}],
        "keep_segments": [[0.0, 10.0]],
        "speaker_position_x": None,
        "landscape_fit": "fit",
    }
    _variant(job)[PROTECTED_TEXT_FIELD] = dict(receipt)

    # A section-less Save changes nothing it was measured with: still clear.
    _resave(job, sound_effects=[])
    assert _variant(job)[PROTECTED_TEXT_FIELD]["verdict"] == "clear"

    _resave(job, orientation="landscape")
    stale = _variant(job)[PROTECTED_TEXT_FIELD]
    assert stale["verdict"] == "stale"
    assert stale["stale_reason"] == "edited_geometry"


def test_a_save_refuses_landscape_for_the_other_phone_archetypes(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    _variant(job)["resolved_archetype"] = "narrated"
    with pytest.raises(HTTPException) as error:
        _resave(job, orientation="landscape")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "orientation_unsupported"


def test_a_failed_landscape_save_changes_nothing(monkeypatch):
    job = _letterboxed_phone_job(monkeypatch, cut=False)
    before = device_status(job, "subtitled").request
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", False)
    with pytest.raises(HTTPException):
        _resave(job, orientation="landscape")
    assert device_status(job, "subtitled").request == before
    assert _variant(job).get("orientation") in (None, "portrait")
