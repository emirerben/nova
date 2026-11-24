"""The editor Save contract of a clip dim, end to end through the real Save and phone writers.

Failure modes, one test each:
- the editor is offered Dim while the flag, the verified token, the build or the device render
  is missing, or a closed gate changes the capability payload;
- a saved dim never reaches the pinned recipe or does not read back to the editor;
- a Save that omits the dim drops it, or a removed dim survives in the row or the recipe;
- a new dim is accepted while the feature is dark, or a stored one cannot be removed then;
- an old build replaces the timeline of a dimmed edit (and silently undims it), or polls a
  dimmed recipe it cannot decode;
- a guided montage or an authored edit loses the dim between the revision, the row and the recipe.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.routes.device_render import _client_update_detail
from app.services.client_protocol import (
    CLIP_DIM_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from tests.routes._editor_save_matrix import VARIANT, order_required_job
from tests.routes._editor_save_matrix import _slot as guided_slot
from tests.routes.test_phone_voiceover_cut_edits import _recipe, _save, _slots, _track, _variant
from tests.routes.test_phone_voiceover_editor_lanes import _enable, voiceover_job


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _open_dim(monkeypatch, *, protocol=CLIP_DIM_PROTOCOL):
    monkeypatch.setattr(gj.settings, "clip_dim_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "clipDim"],
    )
    set_client_protocol(protocol)


def _dim_capability(job, variant):
    return (gj._editor_capabilities(job, variant).get("clips") or {}).get("dim")


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_the_capability_needs_flag_token_protocol_and_a_device_render(monkeypatch, archetype):
    _enable(monkeypatch)
    job, _ = voiceover_job(archetype=archetype)
    variant = _variant(job)
    assert _dim_capability(job, variant) is None

    _open_dim(monkeypatch)
    assert _dim_capability(job, variant) == {"editable": True, "reason": None}

    monkeypatch.setattr(gj.settings, "clip_dim_enabled", False)
    assert _dim_capability(job, variant) is None
    monkeypatch.setattr(gj.settings, "clip_dim_enabled", True)

    set_client_protocol(CLIP_DIM_PROTOCOL - 1)
    assert _dim_capability(job, variant) is None
    set_client_protocol(CLIP_DIM_PROTOCOL)

    verified = gj.settings.phone_render_verified_features
    monkeypatch.setattr(
        gj.settings, "phone_render_verified_features", [f for f in verified if f != "clipDim"]
    )
    assert _dim_capability(job, variant) is None
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", verified)

    variant["render_destination"] = "cloud"
    assert _dim_capability(job, variant) is None


def test_clip_dim_flag_off_is_byte_identical(monkeypatch):
    _enable(monkeypatch)
    job, _ = voiceover_job()
    _open_dim(monkeypatch)
    opened = gj._editor_capabilities(job, _variant(job))
    monkeypatch.setattr(gj.settings, "clip_dim_enabled", False)
    closed = gj._editor_capabilities(job, _variant(job))
    del opened["clips"]["dim"]
    if not opened["clips"]:
        del opened["clips"]
    assert json.dumps(opened, sort_keys=True) == json.dumps(closed, sort_keys=True)


@pytest.mark.parametrize(
    ("archetype", "track_id"), [("narrated", "narrated"), ("voiceover", "montage")]
)
def test_a_saved_dim_reaches_the_recipe_reads_back_and_removes_cleanly(
    monkeypatch, archetype, track_id
):
    _enable(monkeypatch)
    _open_dim(monkeypatch)
    job, vid = voiceover_job(archetype=archetype)
    undimmed = _recipe(job, vid).model_dump(mode="json")
    slots = _slots(job, vid)
    slots[1]["dim"] = 0.5

    _save(job, vid, timeline_slots=slots)

    clips = _track(_recipe(job, vid), track_id).clips
    assert [c.dim for c in clips] == [None, 0.5, None]
    assert "clipDim" in _recipe(job, vid).required_capabilities
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")
    assert [slot.get("dim") for slot in timeline["slots"]] == [None, 0.5, None]

    omitted = [{k: v for k, v in s.items() if k != "dim"} for s in _slots(job, vid)]
    _save(job, vid, timeline_slots=omitted)
    assert _track(_recipe(job, vid), track_id).clips[1].dim == 0.5

    removed = _slots(job, vid)
    removed[1]["dim"] = None
    _save(job, vid, timeline_slots=removed)
    reset = _recipe(job, vid)
    assert "clipDim" not in reset.required_capabilities
    assert all("dim" not in slot for slot in _variant(job)["user_timeline"]["slots"])
    assert "dim" not in json.dumps(reset.model_dump(mode="json")["tracks"])
    assert reset.model_dump(mode="json")["tracks"] == undimmed["tracks"]


def test_a_dim_is_refused_while_the_feature_is_dark_but_a_stored_one_may_be_removed(
    monkeypatch,
):
    _enable(monkeypatch)
    _open_dim(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["dim"] = 0.4
    _save(job, vid, timeline_slots=slots)
    monkeypatch.setattr(gj.settings, "clip_dim_enabled", False)
    before = device_status(job, vid).request

    changed = _slots(job, vid)
    changed[1]["dim"] = 0.8
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=changed)
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "clip_dim_unavailable"
    assert device_status(job, vid).request == before

    removed = _slots(job, vid)
    removed[1]["dim"] = None
    _save(job, vid, timeline_slots=removed)
    assert _track(_recipe(job, vid), "montage").clips[1].dim is None


def test_an_old_build_cannot_replace_the_timeline_of_a_dimmed_edit_or_poll_its_recipe(
    monkeypatch,
):
    _enable(monkeypatch)
    _open_dim(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["dim"] = 0.5
    _save(job, vid, timeline_slots=slots)
    before = device_status(job, vid).request
    stored = json.dumps(_variant(job)["user_timeline"], sort_keys=True)
    update = {
        "code": "client_update_required",
        "feature": "clip_dim",
        "minimum_client_protocol": CLIP_DIM_PROTOCOL,
    }

    set_client_protocol(CLIP_DIM_PROTOCOL - 1)
    old_build_slots = [{k: v for k, v in s.items() if k != "dim"} for s in _slots(job, vid)]
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=old_build_slots)
    assert (refused.value.status_code, refused.value.detail) == (426, update)
    assert device_status(job, vid).request == before
    assert json.dumps(_variant(job)["user_timeline"], sort_keys=True) == stored
    assert _client_update_detail(job, {}, device_status(job, vid)) == update

    set_client_protocol(CLIP_DIM_PROTOCOL)
    assert _client_update_detail(job, {}, device_status(job, vid)) is None
    _save(job, vid, timeline_slots=old_build_slots)
    assert _track(_recipe(job, vid), "montage").clips[1].dim == 0.5


def test_an_old_build_still_saves_an_edit_that_holds_no_dim(monkeypatch):
    _enable(monkeypatch)
    _open_dim(monkeypatch, protocol=CLIP_DIM_PROTOCOL - 1)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0
    _save(job, vid, timeline_slots=slots)


def _guided_save(job, variant, rows, **overrides):
    slots = [guided_slot(row, **overrides.get(row["slot_id"], {})) for row in rows]
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=gj._guided_v2_revision(job, variant)["revision_number"],
        timeline_slots=slots,
    )
    return gj.prepare_editor_commit(job, VARIANT, body)


def _guided_clip(job, clip_id):
    recipe = device_status(job, VARIANT).request.recipe
    return next(c for t in recipe.tracks for c in t.clips if c.id == clip_id)


def test_a_guided_montage_save_carries_the_dim_to_the_revision_row_and_recipe(monkeypatch):
    job, variant, _ = order_required_job(monkeypatch, order_required=False)
    _open_dim(monkeypatch)
    rows = gj.editor_deletion_timeline(job, variant)
    first = rows[0]["slot_id"]

    _guided_save(job, variant, rows, **{first: {"dim": 0.4}})

    saved = gj.editor_deletion_timeline(job, _variant(job))
    assert saved[0]["dim"] == 0.4
    assert all("dim" not in row for row in saved[1:])
    assert _guided_clip(job, first).dim == 0.4

    _guided_save(job, _variant(job), saved)
    assert gj.editor_deletion_timeline(job, _variant(job))[0]["dim"] == 0.4

    _guided_save(job, _variant(job), saved, **{first: {"dim": None}})
    assert all("dim" not in row for row in gj.editor_deletion_timeline(job, _variant(job)))
    assert "clipDim" not in device_status(job, VARIANT).request.recipe.required_capabilities


def _authored_session(monkeypatch):
    from app.services.authored_editor import prepare_authored_editor_commit
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, _ = _authored_phone_job(monkeypatch)
    _open_dim(monkeypatch)

    def save(slot):
        prepare_authored_editor_commit(
            job,
            "original_text",
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(job.assembly_plan["variants"][0]),
                editor_state_version=1,
                timeline_slots=[slot],
            ),
            validation_arguments={},
        )
        variant = job.assembly_plan["variants"][0]
        return variant["user_timeline"]["slots"][0], device_status(
            job, "original_text"
        ).request.recipe

    return save, job


def test_an_authored_save_keeps_an_omitted_dim_and_removes_a_null_one(monkeypatch):
    save, job = _authored_session(monkeypatch)
    row, recipe = save(gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3, dim=0.6))
    assert row["dim"] == 0.6
    assert recipe.tracks[0].clips[0].dim == 0.6
    reopened = gj.dispatch_get_timeline(
        job, "original_text", sign_url=lambda path, ttl: f"https://s/{path}"
    )
    assert [slot.get("dim") for slot in reopened["slots"]] == [0.6]

    def again(**fields):
        return gj.TimelineSlotEdit(
            slot_id=row["slot_id"], clip_index=1, in_s=2, duration_s=3, **fields
        )

    kept_row, kept = save(again())
    assert kept_row["dim"] == 0.6 and kept.tracks[0].clips[0].dim == 0.6

    reset_row, reset = save(again(dim=None))
    assert "dim" not in reset_row
    assert reset.tracks[0].clips[0].dim is None
    assert "clipDim" not in reset.required_capabilities


def test_a_save_slot_rounds_its_dim_and_reads_zero_as_none():
    assert gj.TimelineSlotEdit(clip_index=0, in_s=0, dim=0.404).dim == 0.4
    assert gj.TimelineSlotEdit(clip_index=0, in_s=0, dim=0).dim is None
    assert "dim" not in gj.TimelineSlotEdit(clip_index=0, in_s=0).model_dump()
    assert "dim" not in gj.TimelineSlotEdit(clip_index=0, in_s=0, dim=None).model_dump()
    with pytest.raises(ValueError):
        gj.TimelineSlotEdit(clip_index=0, in_s=0, dim=40)
