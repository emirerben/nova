"""KRI-1136: the editor Save contract of a clip tilt.

Capability `clips.tilt` (flag + verified token + build protocol + device render), the Save of
a row `tilt` through a guided/montage edit and a phone Voiceover cut, the "omitted keeps,
null clears" rule, the 426 an old build gets on a tilted variant and the 422 a tilt gets
while the feature is dark.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.kria.clip_tilt_row import canonical_row_tilt
from app.kria.recipes import ClipBackdrop
from app.services.client_protocol import (
    CLIP_TILT_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from tests.routes._editor_save_matrix import VARIANT, order_required_job
from tests.routes._editor_save_matrix import _slot as guided_slot
from tests.routes.test_phone_voiceover_cut_edits import _recipe, _save, _slots, _track, _variant
from tests.routes.test_phone_voiceover_editor_lanes import _enable, voiceover_job

TILT = {"rotation_deg": 15.0, "backdrop": {"kind": "blur"}}


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _open_tilt(monkeypatch, *, protocol=CLIP_TILT_PROTOCOL):
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "clipTilt"],
    )
    set_client_protocol(protocol)


def _tilt_capability(job, variant):
    return (gj._editor_capabilities(job, variant).get("clips") or {}).get("tilt")


# --- capability -----------------------------------------------------------------------


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_the_capability_needs_flag_token_protocol_and_a_device_render(monkeypatch, archetype):
    _enable(monkeypatch)
    job, _ = voiceover_job(archetype=archetype)
    variant = _variant(job)
    assert _tilt_capability(job, variant) is None

    _open_tilt(monkeypatch)
    assert _tilt_capability(job, variant) == {"editable": True, "reason": None}

    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    assert _tilt_capability(job, variant) is None
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", True)

    set_client_protocol(CLIP_TILT_PROTOCOL - 1)
    assert _tilt_capability(job, variant) is None
    set_client_protocol(CLIP_TILT_PROTOCOL)

    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [f for f in gj.settings.phone_render_verified_features if f != "clipTilt"],
    )
    assert _tilt_capability(job, variant) is None
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "clipTilt"],
    )

    variant["render_destination"] = "cloud"
    assert _tilt_capability(job, variant) is None


def test_a_closed_gate_leaves_the_capability_payload_byte_identical(monkeypatch):
    _enable(monkeypatch)
    job, _ = voiceover_job()
    _open_tilt(monkeypatch)
    opened = gj._editor_capabilities(job, _variant(job))
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    closed = gj._editor_capabilities(job, _variant(job))
    del opened["clips"]["tilt"]
    if not opened["clips"]:
        del opened["clips"]
    assert json.dumps(opened, sort_keys=True) == json.dumps(closed, sort_keys=True)


def test_a_guided_montage_advertises_the_capability_through_the_same_gate(monkeypatch):
    job, variant, _ = order_required_job(monkeypatch, order_required=False)
    assert _tilt_capability(job, variant) is None
    _open_tilt(monkeypatch)
    assert _tilt_capability(job, variant) == {"editable": True, "reason": None}
    set_client_protocol(CLIP_TILT_PROTOCOL - 1)
    assert _tilt_capability(job, variant) is None


# --- voiceover cut: Save, read back, reset --------------------------------------------


@pytest.mark.parametrize(
    ("archetype", "track_id"), [("narrated", "narrated"), ("voiceover", "montage")]
)
def test_a_saved_tilt_reaches_the_recipe_reads_back_and_resets_to_the_untilted_recipe(
    monkeypatch, archetype, track_id
):
    _enable(monkeypatch)
    _open_tilt(monkeypatch)
    job, vid = voiceover_job(archetype=archetype)
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(TILT)

    _save(job, vid, timeline_slots=slots)

    clips = _track(_recipe(job, vid), track_id).clips
    assert clips[0].transform.rotation_degrees == 0 and clips[0].backdrop is None
    assert clips[1].transform.rotation_degrees == 15.0
    assert clips[1].backdrop == ClipBackdrop(kind="blur")
    assert "clipTilt" in _recipe(job, vid).required_capabilities
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")
    assert [slot.get("tilt") for slot in timeline["slots"]] == [
        None,
        canonical_row_tilt(TILT),
        None,
    ]

    omitted = [{k: v for k, v in s.items() if k != "tilt"} for s in _slots(job, vid)]
    _save(job, vid, timeline_slots=omitted)
    assert _track(_recipe(job, vid), track_id).clips[1].transform.rotation_degrees == 15.0

    cleared = _slots(job, vid)
    cleared[1]["tilt"] = None
    _save(job, vid, timeline_slots=cleared)
    reset = _recipe(job, vid)
    assert "clipTilt" not in reset.required_capabilities
    assert all("tilt" not in slot for slot in _variant(job)["user_timeline"]["slots"])
    assert all(
        c.transform.rotation_degrees == 0 and c.backdrop is None
        for c in _track(reset, track_id).clips
    )
    assert "backdrop" not in json.dumps(reset.model_dump(mode="json"))


def test_an_identity_tilt_saved_from_a_blank_upright_dial_clears_the_row(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(TILT)
    _save(job, vid, timeline_slots=slots)

    straight = _slots(job, vid)
    straight[1]["tilt"] = {"rotation_deg": 0, "backdrop": {"kind": "blank"}}
    _save(job, vid, timeline_slots=straight)

    assert all("tilt" not in slot for slot in _variant(job)["user_timeline"]["slots"])
    assert _track(_recipe(job, vid), "montage").clips[1].backdrop is None


def test_a_tilt_is_refused_while_the_feature_is_dark_but_a_stored_one_may_be_cleared(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(TILT)
    _save(job, vid, timeline_slots=slots)
    monkeypatch.setattr(gj.settings, "media_tilt_enabled", False)
    before = device_status(job, vid).request

    changed = _slots(job, vid)
    changed[1]["tilt"] = {"rotation_deg": 30, "backdrop": {"kind": "blur"}}
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=changed)
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "clip_tilt_unavailable"
    assert device_status(job, vid).request == before

    kept = _slots(job, vid)
    kept[1]["tilt"] = canonical_row_tilt(TILT)
    _save(job, vid, timeline_slots=kept)

    cleared = _slots(job, vid)
    cleared[1]["tilt"] = None
    _save(job, vid, timeline_slots=cleared)
    assert _track(_recipe(job, vid), "montage").clips[1].transform.rotation_degrees == 0


def test_an_old_build_cannot_replace_the_timeline_of_a_tilted_edit(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(TILT)
    _save(job, vid, timeline_slots=slots)
    before = device_status(job, vid).request
    stored = json.dumps(_variant(job)["user_timeline"], sort_keys=True)

    set_client_protocol(CLIP_TILT_PROTOCOL - 1)
    old_build_slots = [{k: v for k, v in s.items() if k != "tilt"} for s in _slots(job, vid)]
    with pytest.raises(HTTPException) as refused:
        _save(job, vid, timeline_slots=old_build_slots)

    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "clip_tilt",
        "minimum_client_protocol": CLIP_TILT_PROTOCOL,
    }
    assert device_status(job, vid).request == before
    assert json.dumps(_variant(job)["user_timeline"], sort_keys=True) == stored

    set_client_protocol(CLIP_TILT_PROTOCOL)
    _save(job, vid, timeline_slots=old_build_slots)
    assert _track(_recipe(job, vid), "montage").clips[1].transform.rotation_degrees == 15.0


def test_an_old_build_still_saves_an_edit_that_holds_no_tilt(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch, protocol=CLIP_TILT_PROTOCOL - 1)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0
    _save(job, vid, timeline_slots=slots)


# --- guided montage -------------------------------------------------------------------


def _guided_save(job, variant, revision, rows, **overrides):
    slots = [guided_slot(row, **overrides.get(row["slot_id"], {})) for row in rows]
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=gj._guided_v2_revision(job, variant)["revision_number"],
        timeline_slots=slots,
    )
    return gj.prepare_editor_commit(job, VARIANT, body)


def test_a_guided_montage_save_carries_the_tilt_to_the_revision_row_and_recipe(monkeypatch):
    job, variant, revision = order_required_job(monkeypatch, order_required=False)
    _open_tilt(monkeypatch)
    rows = gj.editor_deletion_timeline(job, variant)
    first = rows[0]["slot_id"]

    _guided_save(job, variant, revision, rows, **{first: {"tilt": dict(TILT)}})

    variant = _variant(job)
    saved = gj.editor_deletion_timeline(job, variant)
    assert saved[0]["tilt"] == canonical_row_tilt(TILT)
    assert all("tilt" not in row for row in saved[1:])
    clip = next(
        c
        for t in device_status(job, VARIANT).request.recipe.tracks
        for c in t.clips
        if c.id == first
    )
    assert clip.transform.rotation_degrees == 15.0
    assert clip.backdrop == ClipBackdrop(kind="blur")

    _guided_save(job, _variant(job), revision, saved)
    assert gj.editor_deletion_timeline(job, _variant(job))[0]["tilt"] == canonical_row_tilt(TILT)

    _guided_save(job, _variant(job), revision, saved, **{first: {"tilt": None}})
    reset = gj.editor_deletion_timeline(job, _variant(job))
    assert all("tilt" not in row for row in reset)
    recipe = device_status(job, VARIANT).request.recipe
    assert "clipTilt" not in recipe.required_capabilities


def test_a_guided_tilt_on_a_clip_with_a_look_is_refused(monkeypatch):
    job, variant, revision = order_required_job(monkeypatch, order_required=False)
    _open_tilt(monkeypatch)
    rows = gj.editor_deletion_timeline(job, variant)
    first = rows[0]["slot_id"]
    with pytest.raises(HTTPException) as refused:
        _guided_save(
            job,
            variant,
            revision,
            rows,
            **{first: {"tilt": dict(TILT), "look_preset": "golden_hour"}},
        )
    assert refused.value.status_code in (422,)


# --- authored (Talking, song, lip-sync after an editor Save) --------------------------


def _authored_session(monkeypatch):
    """A saved phone-authored draft; ``save(slot)`` runs the real authored Save."""
    from app.services.authored_editor import prepare_authored_editor_commit
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, _ = _authored_phone_job(monkeypatch)
    _open_tilt(monkeypatch)

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

    return save


def test_an_authored_save_carries_the_tilt_to_the_recipe_and_the_stored_row(monkeypatch):
    save = _authored_session(monkeypatch)
    row, recipe = save(gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3, tilt=dict(TILT)))
    clip = recipe.tracks[0].clips[0]
    assert clip.transform.rotation_degrees == 15.0
    assert clip.backdrop == ClipBackdrop(kind="blur")
    assert row["tilt"] == canonical_row_tilt(TILT)


def test_an_authored_save_that_omits_the_tilt_keeps_it_and_null_resets_it(monkeypatch):
    save = _authored_session(monkeypatch)
    row, _ = save(gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3, tilt=dict(TILT)))

    def again(**fields):
        return gj.TimelineSlotEdit(
            slot_id=row["slot_id"], clip_index=1, in_s=2, duration_s=3, **fields
        )

    kept_row, kept = save(again())
    assert kept_row["tilt"] == canonical_row_tilt(TILT)
    assert kept.tracks[0].clips[0].transform.rotation_degrees == 15.0

    reset_row, reset = save(again(tilt=None))
    assert "tilt" not in reset_row
    assert "clipTilt" not in reset.required_capabilities
    assert reset.tracks[0].clips[0].backdrop is None


def test_an_authored_save_without_a_tilt_stores_no_tilt_key(monkeypatch):
    row, recipe = _authored_session(monkeypatch)(
        gj.TimelineSlotEdit(clip_index=1, in_s=2, duration_s=3)
    )
    assert "tilt" not in row
    assert "clipTilt" not in recipe.required_capabilities


def test_a_cloud_variant_refuses_a_tilt_even_with_the_feature_on(monkeypatch):
    _enable(monkeypatch)
    _open_tilt(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[1]["tilt"] = dict(TILT)
    _variant(job)["render_destination"] = "cloud"
    with pytest.raises(HTTPException) as refused:
        gj._require_clip_tilt_gates(
            job,
            _variant(job),
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(_variant(job)), timeline_slots=slots
            ),
        )
    assert refused.value.detail == {"code": "clip_tilt_unavailable"}


def test_a_talking_edit_that_is_not_authored_yet_does_not_offer_tilt(monkeypatch):
    from tests.routes.test_phone_subtitled_editor_capabilities import _subtitled_job

    job, variant = _subtitled_job(monkeypatch)
    monkeypatch.setattr(gj, "is_phone_subtitled_editor_variant", lambda v: True)
    monkeypatch.setattr(gj, "phone_subtitled_editor_lanes_supported", lambda: True)
    _open_tilt(monkeypatch)
    device = {**variant, "render_destination": "device"}
    assert _tilt_capability(job, device) is None
    assert _tilt_capability(job, {**device, "editor_timeline_mode": "authored"}) == {
        "editable": True,
        "reason": None,
    }
