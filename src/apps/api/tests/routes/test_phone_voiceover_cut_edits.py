"""KRI-290: a phone Voiceover edit's clips can be trimmed, extended, reordered,
split and deleted -- whatever that does to the footage-vs-voiceover length.

Covers the capability map (and its kill switch), the Save path end to end
(`prepare_editor_commit` -> `resolve_phone_voiceover_slots` ->
`replace_voiceover_cut`), clip deletion, the read-back timeline, and that
caption / lane edits keep working on top of a creator cut.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.services.device_render import device_status
from app.services.phone_voiceover_cut_editor import phone_voiceover_cut_editable
from tests.routes.test_editor_commit import _job
from tests.routes.test_phone_voiceover_editor_lanes import (
    _enable,
    _fake_inspect,
    _sfx_payload,
    voiceover_job,
)


def _slots(job, vid) -> list[dict]:
    """What the iOS editor posts back: the slots it loaded, as `encodeSlot` does."""
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")
    return [
        {
            "slot_id": slot["slot_id"],
            "clip_index": slot["clip_index"],
            "in_s": slot["in_s"],
            "duration_s": slot["duration_s"],
            "duration_beats": None,
            "removed": False,
            "transition_after": slot.get("transition_after") or "cut",
            "transition_duration_s": slot.get("transition_duration_s"),
            **({"playback_rate": slot["playback_rate"]} if "playback_rate" in slot else {}),
        }
        for slot in timeline["slots"]
    ]


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


def _save(job, vid, **sections) -> dict:
    return gj.prepare_editor_commit(
        job,
        vid,
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(_variant(job)), **sections
        ),
        user_id="owner",
        plan_item_id="item",
    )


def _recipe(job, vid):
    return device_status(job, vid).request.recipe


def _track(recipe, track_id):
    return next(track for track in recipe.tracks if track.id == track_id)


# --- capabilities ---------------------------------------------------------------------


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_capabilities_open_the_timeline(monkeypatch, archetype):
    _enable(monkeypatch)
    job, _ = voiceover_job(archetype=archetype)

    caps = gj._editor_capabilities(job, _variant(job))

    assert caps["timeline"] is True
    assert caps["split_clips"] is True
    assert phone_voiceover_cut_editable(job, _variant(job))


@pytest.mark.parametrize(
    ("archetype", "reason"),
    [("narrated", "locked_to_voiceover"), ("voiceover", "voiceover_bed_fit")],
)
def test_kill_switch_restores_the_voiceover_lock(monkeypatch, archetype, reason):
    _enable(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_voiceover_timeline_edits_enabled", False)
    job, vid = voiceover_job(archetype=archetype)
    slots = _slots(job, vid)
    before = device_status(job, vid).request

    assert gj._editor_capabilities(job, _variant(job))["timeline"] is False
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")
    assert timeline["editable"] is False and timeline["reason"] == reason
    slots[0]["duration_s"] = 2.0
    with pytest.raises(HTTPException) as error:
        _save(job, vid, timeline_slots=slots)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == reason
    assert device_status(job, vid).request == before


def test_cloud_voiceover_edits_stay_locked(monkeypatch):
    _enable(monkeypatch)
    job = _job(resolved_archetype="narrated")
    assert gj._timeline_ineligibility(job, _variant(job)) == "locked_to_voiceover"


# --- Save -----------------------------------------------------------------------------


def test_shortening_below_the_voiceover_saves_and_cuts_the_voice(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job()
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0  # 12 s of voice over 10 s of footage

    prep = _save(job, vid, timeline_slots=slots)

    assert prep["render_destination"] == "device"
    assert device_status(job, vid).request.identity.recipe_revision == 2
    recipe = _recipe(job, vid)
    assert recipe.duration == pytest.approx(10.0)
    assert _track(recipe, "narration").clips[0].source_duration == pytest.approx(10.0)
    assert all(layer.end <= 10.0 for layer in recipe.text_layers)
    variant = _variant(job)
    assert variant["duration_s"] == pytest.approx(10.0)
    assert [row["duration_s"] for row in variant["user_timeline"]["slots"]] == [4.0, 4.0, 2.0]
    assert variant["narrated_timings"][-1]["end_s"] == pytest.approx(10.0)

    # The editor reads back exactly what the phone renders.
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")
    assert timeline["editable"] is True
    assert timeline["total_duration_s"] == pytest.approx(10.0)
    assert [s["slot_id"] for s in timeline["slots"]] == [s["slot_id"] for s in slots]
    assert timeline["clips"][0]["native_source"]["media_id"] == "c2"


def test_extending_past_the_voiceover_saves_and_plays_on(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[0]["duration_s"] = 6.0  # c0: 8.5 s of footage from its 1.5 s in-point

    _save(job, vid, timeline_slots=slots)

    recipe = _recipe(job, vid)
    assert recipe.duration == pytest.approx(14.0)
    assert _track(recipe, "narration").clips[0].source_duration == pytest.approx(12.0)
    assert [clip.timeline_start for clip in _track(recipe, "montage").clips] == pytest.approx(
        [0.0, 6.0, 10.0]
    )


def test_a_second_cut_edit_builds_on_the_first(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job()
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0
    _save(job, vid, timeline_slots=slots)

    again = _slots(job, vid)
    again[-1]["duration_s"] = 5.0  # extend back past the 12 s voice
    _save(job, vid, timeline_slots=list(reversed(again)))

    recipe = _recipe(job, vid)
    assert recipe.duration == pytest.approx(13.0)
    assert [clip.id.split("-", 2)[2] for clip in _track(recipe, "narrated").clips] == [
        "s2",
        "s1",
        "s0",
    ]
    assert _track(recipe, "narration").clips[0].source_duration == pytest.approx(12.0)


def test_deleting_a_clip_is_a_cut_edit_not_an_authored_restage(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job()
    baseline = gj.editor_deletion_timeline(job, _variant(job))
    assert [row["slot_id"] for row in baseline] == ["s0", "s1", "s2"]

    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(_variant(job)),
        editor_state_version=1,
        deletions=[{"kind": "clip", "id": "s1"}],
    )
    normalized = gj.normalize_editor_deletion_request(job, vid, body)
    gj.prepare_editor_commit(job, vid, normalized, user_id="owner", plan_item_id="item")

    clips = _track(_recipe(job, vid), "narrated").clips
    assert [clip.id for clip in clips] == ["step-0-s0", "step-1-s2"]
    assert _variant(job).get("editor_timeline_mode") != "authored"


def test_a_split_piece_gets_a_server_slot_id(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    first = slots[0]
    first["duration_s"] = 2.0
    piece = {**first, "slot_id": None, "in_s": first["in_s"] + 2.0}

    _save(job, vid, timeline_slots=[first, piece, *slots[1:]])

    saved = _variant(job)["user_timeline"]["slots"]
    assert len(saved) == 4 and saved[1]["slot_id"] and saved[1]["slot_id"] != first["slot_id"]
    assert len(_track(_recipe(job, vid), "montage").clips) == 4


def test_an_unknown_slot_id_is_stale(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job()
    slots = _slots(job, vid)
    slots[0]["slot_id"] = "from-another-tab"
    with pytest.raises(HTTPException) as error:
        _save(job, vid, timeline_slots=slots)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "TIMELINE_STALE"


def test_a_montage_window_past_its_footage_is_out_of_bounds(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    slots = _slots(job, vid)
    slots[0]["duration_s"] = 9.0  # c0 has 8.5 s from 1.5 s
    with pytest.raises(HTTPException) as error:
        _save(job, vid, timeline_slots=slots)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "TIMELINE_OUT_OF_BOUNDS"


def test_speed_and_looks_stay_closed_on_the_phone(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    for change in ({"playback_rate": 2.0}, {"look_preset": "golden_hour"}):
        slots = _slots(job, vid)
        slots[0].update(change)
        with pytest.raises(HTTPException) as error:
            _save(job, vid, timeline_slots=slots)
        assert error.value.detail["code"] == "phone_edit_unsupported"


# --- other edits on top of a creator cut ------------------------------------------------


def test_a_caption_save_keeps_the_creator_cut(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job()
    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0
    _save(job, vid, timeline_slots=slots)
    cut = _track(_recipe(job, vid), "narrated")

    _save(job, vid, caption_cues=[{"text": "Packed", "start_s": 9.0, "end_s": 12.0}])

    recipe = _recipe(job, vid)
    assert _track(recipe, "narrated") == cut
    assert recipe.duration == pytest.approx(10.0)
    assert all(layer.end <= 10.0 for layer in recipe.text_layers)


def test_lanes_follow_the_cut_and_come_back_when_it_grows(monkeypatch):
    _enable(monkeypatch)
    _fake_inspect(monkeypatch)
    job, vid = voiceover_job()
    _save(job, vid, sound_effects=[_sfx_payload(at_s=10.5)])
    assert any(track.id == "sfx" for track in _recipe(job, vid).tracks)

    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 2.0  # the video now ends before the effect
    _save(job, vid, timeline_slots=slots)
    assert not any(track.id == "sfx" for track in _recipe(job, vid).tracks)
    assert _variant(job)["sound_effects"][0]["sound_effect_id"] == "pop"

    slots = _slots(job, vid)
    slots[-1]["duration_s"] = 4.0
    _save(job, vid, timeline_slots=slots)
    assert any(track.id == "sfx" for track in _recipe(job, vid).tracks)


def test_a_narrated_window_slowed_past_the_preview_floor_is_out_of_bounds(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job()
    slots = _slots(job, vid)
    slots[1]["duration_s"] = 9.0  # c1 has 2 s of footage: 1.95 / 9 is under 0.25x
    with pytest.raises(HTTPException) as error:
        _save(job, vid, timeline_slots=slots)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "TIMELINE_OUT_OF_BOUNDS"


def test_an_untouched_slow_clip_never_blocks_editing_another(monkeypatch):
    from app.services import phone_voiceover_cut_editor

    _enable(monkeypatch)
    # Pretend the pinned ~0.49x c1 clip is below the floor (an older render).
    monkeypatch.setattr(phone_voiceover_cut_editor, "MIN_NARRATED_RATE", 0.6)
    job, vid = voiceover_job()
    before = _track(_recipe(job, vid), "narrated").clips[1]
    slots = _slots(job, vid)
    slots[0]["duration_s"] = 3.0

    _save(job, vid, timeline_slots=slots)

    after = _track(_recipe(job, vid), "narrated").clips[1]
    assert (after.source_duration, after.rate) == (before.source_duration, before.rate)


def test_the_chat_copilot_sees_and_trims_the_cut(monkeypatch):
    from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops

    _enable(monkeypatch)
    job, vid = voiceover_job()

    snapshot = build_editor_snapshot(job, _variant(job))
    assert "clip" in snapshot["allowed_op_families"]
    assert len(snapshot["slots"]) == 3

    compiled = compile_editor_ops(
        job, _variant(job), [{"op": "set_clip_duration", "slot_index": 2, "duration_s": 2.0}]
    )
    gj.prepare_editor_commit(job, vid, compiled.payload, user_id="owner", plan_item_id="item")

    assert _recipe(job, vid).duration == pytest.approx(10.0)
