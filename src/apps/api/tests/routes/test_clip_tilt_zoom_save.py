"""KRI-1169: the editor Save contract of a clip's tilt zoom.

Capability `clips.tilt_zoom` (the tilt gate plus protocol 32), the Save of `tilt.zoom` into the
recipe's `transform.scale` and back, the 426 a build that cannot keep a stored zoom gets, and
the 422 a zoom gets while its gate is closed.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.kria.clip_tilt_row import canonical_row_tilt
from app.services.client_protocol import (
    CLIP_BACKDROP_MEDIA_PROTOCOL,
    TILT_ZOOM_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from tests.routes._editor_save_matrix import VARIANT, order_required_job
from tests.routes.test_clip_tilt_editor_save import (
    _authored_session,
    _guided_save,
    _open_tilt,
)
from tests.routes.test_phone_voiceover_cut_edits import _recipe, _save, _slots, _track, _variant
from tests.routes.test_phone_voiceover_editor_lanes import _enable, voiceover_job

ZOOMED = {"rotation_deg": 15.0, "zoom": 1.5, "backdrop": {"kind": "blur"}}


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _clips(job, vid, archetype="voiceover"):
    return _track(_recipe(job, vid), "narrated" if archetype == "narrated" else "montage").clips


def _leaf(job, key="tilt_zoom"):
    return (gj._editor_capabilities(job, _variant(job)).get("clips") or {}).get(key)


def test_the_zoom_capability_follows_the_tilt_gate_and_protocol_32(monkeypatch):
    _enable(monkeypatch)
    job, _ = voiceover_job(archetype="voiceover")
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL)
    assert _leaf(job) == {"editable": True, "reason": None}
    assert _leaf(job, "tilt") == {"editable": True, "reason": None}

    set_client_protocol(TILT_ZOOM_PROTOCOL - 1)
    assert _leaf(job) is None
    assert _leaf(job, "tilt") == {"editable": True, "reason": None}

    set_client_protocol(TILT_ZOOM_PROTOCOL)
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    assert _leaf(job) is None

    monkeypatch.setattr(gj.settings, "media_tilt_enabled", True)
    monkeypatch.setattr(gj.settings, "media_tilt_zoom_min_client_protocol", 40)
    assert _leaf(job) is None


def test_a_closed_zoom_gate_leaves_the_capability_payload_byte_identical(monkeypatch):
    _enable(monkeypatch)
    job, _ = voiceover_job()
    _open_tilt(monkeypatch, protocol=CLIP_BACKDROP_MEDIA_PROTOCOL)
    before = json.dumps(gj._editor_capabilities(job, _variant(job)), sort_keys=True)
    set_client_protocol(TILT_ZOOM_PROTOCOL)
    opened = gj._editor_capabilities(job, _variant(job))
    del opened["clips"]["tilt_zoom"]
    assert json.dumps(opened, sort_keys=True) == before


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_a_saved_zoom_reaches_the_recipe_scale_reads_back_and_resets(monkeypatch, archetype):
    _enable(monkeypatch)
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL)
    job, vid = voiceover_job(archetype=archetype)
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(ZOOMED)

    _save(job, vid, timeline_slots=slots)

    clips = _clips(job, vid, archetype)
    assert clips[0].transform.scale == 1
    assert clips[1].transform.scale == pytest.approx(1.5)
    assert clips[1].transform.rotation_degrees == 15.0
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")
    assert [slot.get("tilt") for slot in timeline["slots"]] == [
        None,
        canonical_row_tilt(ZOOMED),
        None,
    ]

    omitted = [{k: v for k, v in s.items() if k != "tilt"} for s in _slots(job, vid)]
    _save(job, vid, timeline_slots=omitted)
    assert _clips(job, vid, archetype)[1].transform.scale == pytest.approx(1.5)

    unzoomed = _slots(job, vid)
    unzoomed[1]["tilt"] = {**ZOOMED, "zoom": 1.0}
    _save(job, vid, timeline_slots=unzoomed)
    assert _clips(job, vid, archetype)[1].transform.scale == 1
    assert _clips(job, vid, archetype)[1].transform.rotation_degrees == 15.0

    cleared = _slots(job, vid)
    cleared[1]["tilt"] = None
    _save(job, vid, timeline_slots=cleared)
    assert "clipTilt" not in _recipe(job, vid).required_capabilities
    assert _clips(job, vid, archetype)[1].transform.scale == 1


def test_a_zoom_alone_needs_no_new_recipe_capability(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = {"zoom": 2.0}
    _save(job, vid, timeline_slots=slots)
    assert _clips(job, vid)[1].transform.scale == pytest.approx(2.0)
    assert "clipTilt" not in _recipe(job, vid).required_capabilities
    assert _variant(job)["user_timeline"]["slots"][1]["tilt"] == {"rotation_deg": 0.0, "zoom": 2.0}


def test_a_build_that_cannot_keep_a_stored_zoom_is_told_to_update(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(ZOOMED)
    _save(job, vid, timeline_slots=slots)
    before = device_status(job, vid).request
    stored = json.dumps(_variant(job)["user_timeline"], sort_keys=True)

    set_client_protocol(TILT_ZOOM_PROTOCOL - 1)
    with pytest.raises(HTTPException) as refused:
        _save(
            job,
            vid,
            timeline_slots=[{k: v for k, v in s.items() if k != "tilt"} for s in _slots(job, vid)],
        )

    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "tilt_zoom",
        "minimum_client_protocol": TILT_ZOOM_PROTOCOL,
    }
    assert device_status(job, vid).request == before
    assert json.dumps(_variant(job)["user_timeline"], sort_keys=True) == stored


def test_a_build_without_zoom_still_saves_a_variant_with_no_zoom(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL - 1)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = {"rotation_deg": 15.0}
    _save(job, vid, timeline_slots=slots)
    assert _clips(job, vid)[1].transform.rotation_degrees == 15.0
    assert _clips(job, vid)[1].transform.scale == 1


def test_a_new_zoom_from_a_build_without_the_gate_is_refused(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL - 1)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(ZOOMED)
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=slots)
    assert refused.value.status_code == 422
    assert refused.value.detail == {"code": "clip_tilt_zoom_unavailable"}
    assert "user_timeline" not in _variant(job)


def test_a_zoom_while_the_tilt_feature_is_dark_is_refused_as_a_tilt(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL)
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = {"zoom": 1.5}
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=slots)
    assert refused.value.detail == {"code": "clip_tilt_unavailable"}


def test_a_guided_montage_save_carries_the_zoom_to_the_revision_row_and_recipe(monkeypatch):
    job, variant, revision = order_required_job(monkeypatch, order_required=False)
    _open_tilt(monkeypatch, protocol=TILT_ZOOM_PROTOCOL)
    rows = gj.editor_deletion_timeline(job, variant)
    first = rows[0]["slot_id"]

    _guided_save(job, variant, revision, rows, **{first: {"tilt": dict(ZOOMED)}})

    saved = gj.editor_deletion_timeline(job, _variant(job))
    assert saved[0]["tilt"] == canonical_row_tilt(ZOOMED)
    clip = next(
        c
        for t in device_status(job, VARIANT).request.recipe.tracks
        for c in t.clips
        if c.id == first
    )
    assert clip.transform.scale == pytest.approx(1.5)
    assert clip.transform.rotation_degrees == 15.0

    _guided_save(job, _variant(job), revision, saved, **{first: {"tilt": None}})
    reset = device_status(job, VARIANT).request.recipe
    assert "clipTilt" not in reset.required_capabilities
    assert all(c.transform.scale == 1 for t in reset.tracks for c in t.clips)


def test_an_authored_save_carries_the_zoom_to_the_recipe_and_the_stored_row(monkeypatch):
    save = _authored_session(monkeypatch)
    set_client_protocol(TILT_ZOOM_PROTOCOL)
    row, recipe = save(gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3, tilt={"zoom": 2.0}))
    assert row["tilt"] == {"rotation_deg": 0.0, "zoom": 2.0}
    clip = next(c for t in recipe.tracks if t.kind == "video" for c in t.clips)
    assert clip.transform.scale == pytest.approx(2.0)
    assert "clipTilt" not in recipe.required_capabilities, "a zoom alone is plain transform.scale"


@pytest.mark.parametrize("zoom", [0.49, 3.01, 0, -1, "1.5", None, True])
def test_a_zoom_outside_the_range_is_rejected_at_the_request(zoom):
    with pytest.raises(ValueError):
        gj.TimelineSlotEdit(clip_index=0, in_s=0, duration_s=1, tilt={"zoom": zoom})
