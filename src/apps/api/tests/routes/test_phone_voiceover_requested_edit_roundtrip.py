"""End-to-end persistence checks for requested Narrated text and source audio."""

from __future__ import annotations

import pytest

import app.routes.generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status
from tests.routes.test_phone_narrated_title_preview import _title_layers, titled_job
from tests.routes.test_phone_voiceover_cut_edits import _slots
from tests.routes.test_phone_voiceover_editor_lanes import _enable


def _variant(job: object) -> dict:
    return job.assembly_plan["variants"][0]


@pytest.fixture(autouse=True)
def _reset_client_protocol() -> None:
    """Keep explicit old/new-client cases isolated from neighboring tests."""
    clear_request_context()
    yield
    clear_request_context()


def _save(job: object, variant_id: str, **sections: object) -> dict:
    return gj.prepare_editor_commit(
        job,
        variant_id,
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(_variant(job)), **sections
        ),
        user_id="owner",
        plan_item_id="item",
    )


def _recipe(job: object, variant_id: str):
    return device_status(job, variant_id).request.recipe


def _manual_title(job: object) -> dict:
    [title] = _variant(job)["narrated_title_text_elements"]
    return {**title, "text": "Keep this exact timing", "start_s": 8.0, "end_s": 9.0}


def test_spoken_text_keeps_its_manual_narration_clock_across_cut_reopen_and_reorder(monkeypatch):
    job, variant_id = titled_job(monkeypatch)
    variant = _variant(job)
    manual = _manual_title(job)
    variant["narrated_title_text_elements"] = [manual]
    # This receipt selects the cut-active canonical-row path rather than the
    # old pinned title layer. Its detailed contents are not read by that path.
    variant["spoken_text_placement"] = {"version": 1}

    shortened = _slots(job, variant_id)
    shortened[1]["duration_s"] = 0.1
    shortened[2]["duration_s"] = 0.1
    _save(job, variant_id, timeline_slots=shortened)

    # The visual cut now ends before the manual row starts, so its recipe layer
    # drops. Its canonical narration-clock row is never overwritten.
    assert _variant(job)["narrated_title_text_elements"] == [manual]
    assert not _title_layers(job, variant_id)

    reopened = _slots(job, variant_id)
    reopened[1]["duration_s"] = 4.0
    reopened[2]["duration_s"] = 4.0
    # Reorder while restoring the full visual clock. The manual text does not
    # follow a picture slot: it stays at its confirmed narration timestamps.
    _save(job, variant_id, timeline_slots=list(reversed(reopened)))

    [restored] = _title_layers(job, variant_id)
    assert (restored.start, restored.end) == pytest.approx((8.0, 9.0))
    assert " ".join(run.text for run in restored.runs) == "Keep this exact timing"
    assert _variant(job)["narrated_title_text_elements"] == [manual]


def test_selected_source_audio_survives_reorder_split_and_new_occurrence(monkeypatch):
    _enable(monkeypatch, protocol=14)
    monkeypatch.setattr(gj.settings, "phone_voiceover_source_audio_enabled", True)
    set_client_protocol(14)
    job, variant_id = titled_job(monkeypatch, protocol=14)
    variant = _variant(job)
    variant["source_audio_media_ids"] = ["c1"]
    variant["source_audio_bed_gain"] = 0.3

    response = gj._variants_for_response(job)[0]
    assert response["source_audio_media_ids"] == ["c1"]
    assert response["source_audio_bed_gain"] == pytest.approx(0.3)

    # The GET endpoint returns this through TimelineResponse, not the raw
    # projection dict. Keep the native marker explicit in that wire schema.
    timeline = gj.TimelineResponse(
        **gj.dispatch_get_timeline(job, variant_id, sign_url=lambda path, ttl: f"https://s/{path}")
    ).model_dump(mode="json")
    assert [slot["source_audio_volume"] for slot in timeline["slots"]] == [0.0, 1.0, 0.0]

    slots = _slots(job, variant_id)
    # Pool order is c2, c0, c1, so this is the selected source occurrence.
    selected = slots[1]
    selected["duration_s"] = 2.0
    split = {**selected, "slot_id": None, "in_s": 1.0, "duration_s": 2.0}
    # A new c0 occurrence must remain muted even though it did not exist in
    # the starting timeline.
    new_unselected = {
        "slot_id": None,
        "clip_index": 1,
        "in_s": 0.0,
        "duration_s": 2.0,
        "duration_beats": None,
        "removed": False,
        "transition_after": "cut",
        "transition_duration_s": None,
    }
    _save(job, variant_id, timeline_slots=[selected, split, slots[2], slots[0], new_unselected])

    saved = _variant(job)["user_timeline"]["slots"]
    assert [slot["source_audio_volume"] for slot in saved] == [1.0, 1.0, 0.0, 0.0, 0.0]
    video = next(track for track in _recipe(job, variant_id).tracks if track.id == "narrated")
    assert [clip.volume for clip in video.clips] == [1.0, 1.0, 0.0, 0.0, 0.0]
    # The marker is unit-only: the existing global bed gain remains the one
    # AudioMixRecipe applies, rather than being multiplied into each marker.
    assert _recipe(job, variant_id).audio.original_volume == pytest.approx(0.3)


def test_old_client_cannot_reopen_or_save_persisted_source_audio_selection(monkeypatch):
    """The protocol gate applies even while creation rollout is switched off."""
    job, variant_id = titled_job(monkeypatch, protocol=13)
    _variant(job)["source_audio_media_ids"] = []

    with pytest.raises(gj.HTTPException) as capabilities_refused:
        gj._editor_capabilities(job, _variant(job))
    assert capabilities_refused.value.status_code == 426
    assert capabilities_refused.value.detail == {
        "code": "client_update_required",
        "feature": "voiceover_source_audio",
        "minimum_client_protocol": 14,
    }

    # The timeline endpoint is independently guarded: callers do not have to
    # fetch editor capabilities before they ask for its source-audio markers.
    with pytest.raises(gj.HTTPException) as timeline_refused:
        gj.dispatch_get_timeline(job, variant_id, sign_url=lambda path, ttl: path)
    assert timeline_refused.value.status_code == 426

    # Save reaches the service entry point used by native editor commits.
    with pytest.raises(gj.HTTPException) as save_refused:
        _save(job, variant_id)
    assert save_refused.value.status_code == 426


def test_new_client_can_reopen_and_save_persisted_source_audio_selection(monkeypatch):
    job, variant_id = titled_job(monkeypatch, protocol=14)
    _variant(job)["source_audio_media_ids"] = []

    assert gj._editor_capabilities(job, _variant(job))["text_elements"] is True
    timeline = gj.dispatch_get_timeline(job, variant_id, sign_url=lambda path, ttl: path)
    assert timeline["slots"]
    saved = _save(job, variant_id, timeline_slots=_slots(job, variant_id))
    assert saved["sections"]["timeline"] is True


def test_legacy_variant_remains_open_to_old_client(monkeypatch):
    job, variant_id = titled_job(monkeypatch, protocol=13)
    assert "source_audio_media_ids" not in _variant(job)

    assert gj._editor_capabilities(job, _variant(job))["text_elements"] is True
    assert gj.dispatch_get_timeline(job, variant_id, sign_url=lambda path, ttl: path)["slots"]
    saved = _save(job, variant_id, timeline_slots=_slots(job, variant_id))
    assert saved["sections"]["timeline"] is True


def test_source_audio_protocol_guard_is_inert_outside_http_request(monkeypatch):
    job, variant_id = titled_job(monkeypatch, protocol=13)
    _variant(job)["source_audio_media_ids"] = ["c1"]
    clear_request_context()

    # Worker/chat paths carry no client protocol and must retain the persisted
    # selection instead of being rejected by an app-update requirement.
    assert gj.dispatch_get_timeline(job, variant_id, sign_url=lambda path, ttl: path)["slots"]
