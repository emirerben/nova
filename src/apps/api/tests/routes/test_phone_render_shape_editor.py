"""KRI-306: the native editor's bars/crop choice (`landscape_fit`) on device variants.

Capability and Save must agree: the editor advertises only what Save accepts,
the choice persists on the variant, and a later cut edit keeps the bars.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.routes.test_editor_commit import _job
from tests.routes.test_phone_voiceover_cut_edits import _recipe, _save, _slots, _track, _variant
from tests.routes.test_phone_voiceover_editor_lanes import _enable, voiceover_job


def _make_landscape(job) -> None:
    """c2 (pool index 0, step s2) is a 1920x1080 source; c0/c1 stay portrait."""
    for row in job.assembly_plan[PHONE_SOURCES_FIELD]:
        if row["media_id"] == "c2":
            row["original"]["width"], row["original"]["height"] = 1920, 1080


def _scale(recipe, clip_suffix: str) -> float:
    clips = _track(recipe, "montage").clips
    return next(clip for clip in clips if clip.id.endswith(clip_suffix)).transform.scale


# --- capabilities ----------------------------------------------------------------------


def test_voiceover_montage_advertises_bars_but_not_orientation(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True)
    job, _ = voiceover_job(archetype="voiceover")

    caps = gj._editor_capabilities(job, _variant(job))

    assert caps["landscape_fit"] == {"editable": True, "value": "fill", "reason": None}
    # Save recompiles lanes/cut only, so a live-gap orientation control is closed.
    assert caps["orientation"]["editable"] is False
    assert caps["orientation"]["reason"] == "orientation_unsupported"


def test_voiceover_montage_fit_closes_with_the_lane_rollout(monkeypatch):
    _enable(monkeypatch, lanes=False)
    job, _ = voiceover_job(archetype="voiceover")
    assert gj._editor_capabilities(job, _variant(job))["landscape_fit"]["editable"] is False


def test_the_advertised_value_is_the_persisted_choice(monkeypatch):
    _enable(monkeypatch)
    job, _ = voiceover_job(archetype="voiceover", persisted={"landscape_fit": "fit"})
    assert gj._editor_capabilities(job, _variant(job))["landscape_fit"]["value"] == "fit"


def test_narrated_closes_both_controls(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True)
    job, _ = voiceover_job(archetype="narrated")
    caps = gj._editor_capabilities(job, _variant(job))
    assert caps["landscape_fit"]["editable"] is False
    assert caps["orientation"]["editable"] is False


def test_cloud_variants_always_advertise_it_closed(monkeypatch):
    _enable(monkeypatch)
    job = _job()
    caps = gj._editor_capabilities(job, job.assembly_plan["variants"][0])
    assert caps["landscape_fit"]["editable"] is False


# --- Save ------------------------------------------------------------------------------


def test_fit_save_letterboxes_only_the_sideways_clip_and_persists(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    _make_landscape(job)

    prep = _save(job, vid, landscape_fit="fit")

    assert prep["render_destination"] == "device"
    assert prep["sections"]["landscape_fit"] is True
    assert device_status(job, vid).request.identity.recipe_revision == 2
    recipe = _recipe(job, vid)
    assert _scale(recipe, "-s2") == pytest.approx(0.31640625)  # c2, 1920x1080
    assert _scale(recipe, "-s0") == 1 and _scale(recipe, "-s1") == 1  # portrait: untouched
    assert _variant(job)["landscape_fit"] == "fit"

    _save(job, vid, landscape_fit="fill")
    assert _scale(_recipe(job, vid), "-s2") == 1
    assert _variant(job)["landscape_fit"] == "fill"


def test_a_failed_fit_save_changes_nothing(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="narrated")
    before = device_status(job, vid).request
    with pytest.raises(HTTPException) as error:
        _save(job, vid, landscape_fit="fit")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "landscape_fit_unsupported"
    assert device_status(job, vid).request == before
    assert "landscape_fit" not in _variant(job)


def test_cloud_variant_rejects_the_section(monkeypatch):
    _enable(monkeypatch)
    job = _job()
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(
            job,
            job.assembly_plan["variants"][0]["variant_id"],
            gj.EditorCommitRequest(landscape_fit="fit", base_generation=""),
            user_id="owner",
            plan_item_id="item",
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "landscape_fit_unsupported"


def test_a_cut_edit_keeps_the_bars_for_a_newly_placed_sideways_clip(monkeypatch):
    """An all-portrait cut compiled with "fit" reads back as "fill" from the recipe
    alone; the persisted value is what keeps the bars for a landscape clip added later."""
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover", persisted={"landscape_fit": "fit"})
    _make_landscape(job)  # the pinned recipe (identity everywhere) predates this
    slots = _slots(job, vid)
    sideways = next(slot for slot in slots if slot["clip_index"] == 0)  # c2
    sideways["duration_s"] = 2.0
    piece = {**sideways, "slot_id": None, "in_s": sideways["in_s"] + 2.0, "duration_s": 1.5}
    slots.append(piece)

    _save(job, vid, timeline_slots=slots)

    saved = _variant(job)["user_timeline"]["slots"]
    new_slot_id = saved[-1]["slot_id"]
    assert new_slot_id and new_slot_id != sideways["slot_id"]
    scales = [clip.transform.scale for clip in _track(_recipe(job, vid), "montage").clips]
    assert scales[-1] == pytest.approx(0.31640625)  # the new clip is letterboxed
    assert _variant(job)["landscape_fit"] == "fit"
