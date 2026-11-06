"""Regression before implementation: authored intent survives effective rendering."""

import copy

import pytest

from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from tests.pipeline.test_authored_timeline import fixture


@pytest.mark.parametrize(
    "start,duration,source,locked,expected",
    [
        (0.9, 3, 3.36, False, 0.36),
        (0.2, 3, 3.36, False, 0.2),
        (0.9, 3, 3.36, True, None),
        (0, 4, 3.36, False, None),
        (float("nan"), 3, 3.36, False, None),
    ],
)
def test_effective_window_preserves_duration_and_respects_anchor(
    start, duration, source, locked, expected
):
    from app.pipeline.editor_render_window import effective_source_start

    if expected is None:
        with pytest.raises(ValueError):
            effective_source_start(start, duration, 1, source, start_locked=locked)
    else:
        assert effective_source_start(
            start, duration, 1, source, start_locked=locked
        ) == pytest.approx(expected)


def test_render_shifts_feasible_window_without_rewriting_authored_timeline():
    job, variant, previous = fixture()
    # Same geometry as Troya, using the fixture's immutable 12-second source:
    # a 3-second request overshoots by 0.54 seconds.
    variant["user_timeline"]["slots"][0].update(in_s=9.54, duration_s=3)
    before = copy.deepcopy(variant)
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_start == pytest.approx(9)
    assert clip.source_duration == pytest.approx(3)
    assert clip.rate == 1
    assert recipe.duration == pytest.approx(3)
    assert variant == before


def test_render_does_not_shift_an_explicit_source_start():
    job, variant, previous = fixture()
    variant["user_timeline"]["slots"][0].update(in_s=9.54, duration_s=3, source_start_locked=True)
    before = copy.deepcopy(variant)
    with pytest.raises(ValueError, match="pinned source moment"):
        compile_phone_authored_timeline(job, variant, previous)
    assert variant == before


def test_mapping_preserves_camera_audio_choice():
    job, variant, previous = fixture()
    variant["user_timeline"]["slots"][0].update(in_s=9.54, duration_s=3, muted=True)
    recipe = compile_phone_authored_timeline(job, variant, previous)
    assert recipe.tracks[0].clips[0].volume == 0


def test_explicit_chat_source_start_survives_wire_roundtrip():
    from app.routes.generative_jobs import EditorCommitRequest
    from app.services.kria_editor_ops import compile_editor_ops
    from tests.services.test_kria_editor_ops import _job, _variant

    variant = _variant()
    compiled = compile_editor_ops(
        _job(variant), variant, [{"op": "set_clip_in", "slot_index": 0, "in_s": 0.4}]
    )
    reopened = EditorCommitRequest.model_validate_json(compiled.payload.model_dump_json())
    assert reopened.timeline_slots[0].source_start_locked is True


@pytest.mark.parametrize(
    "supplied", [{}, {"source_start_locked": None}, {"source_start_locked": False}]
)
def test_old_client_cannot_erase_saved_source_start_intent(supplied):
    from app.routes.generative_jobs import TimelineSlotEdit
    from app.services.authored_editor import resolve_authored_phone_slots

    job, variant, _ = fixture()
    variant["user_timeline"]["slots"][0]["source_start_locked"] = True
    rows = resolve_authored_phone_slots(
        job,
        variant,
        [TimelineSlotEdit(slot_id="added", clip_index=1, in_s=2, duration_s=3, **supplied)],
    )
    assert rows[0]["source_start_locked"] is True


def test_phone_compile_failure_returns_validated_draft_for_transaction(monkeypatch):
    from app.routes.generative_jobs import (
        EditorCommitRequest,
        TimelineSlotEdit,
        editor_deletion_timeline,
    )
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.editor_empty_drafts import stage_initial_authored_baseline
    from tests.routes._editor_save_matrix import guided_phone_job

    job, variant, _ = guided_phone_job(monkeypatch)
    rows = editor_deletion_timeline(job, variant)
    staged = stage_initial_authored_baseline(job, "guided_story", rows)
    rows[0]["duration_s"] = 11  # immutable source is only 10 seconds
    request = EditorCommitRequest(
        base_generation="first",
        editor_state_version=2,
        timeline_slots=[TimelineSlotEdit.model_validate(row) for row in rows],
    )
    prep = prepare_authored_editor_commit(staged, "guided_story", request, validation_arguments={})
    assert "footage" in prep["render_recovery"]
    assert staged.assembly_plan["variants"][0]["user_timeline"]["slots"][0]["duration_s"] == 11


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("rate", [0.5, 1.0, 2.0, 4.0])
def test_saved_endpoint_roundoff_matches_native_preview(locked, rate):
    from app.pipeline.editor_render_window import effective_source_start

    # Real saved-draft end-minus-start arithmetic, as in the native saved-range fixture.
    output_duration = 6.2 - 5
    source_duration = 1.2 * rate
    assert output_duration * rate > source_duration
    assert (
        effective_source_start(0, output_duration, rate, source_duration, start_locked=locked) == 0
    )


@pytest.mark.parametrize("locked", [False, True])
def test_saved_endpoint_tolerance_does_not_admit_a_true_millisecond_overrun(locked):
    from app.pipeline.editor_render_window import RenderWindowUnavailable, effective_source_start

    with pytest.raises(RenderWindowUnavailable, match="more footage"):
        effective_source_start(0, 1.201, 1, 1.2, start_locked=locked)


def test_saved_endpoint_roundoff_never_reanchors_to_a_negative_start():
    from app.pipeline.editor_render_window import effective_source_start

    assert effective_source_start(0.1, 6.2 - 5, 1, 1.2, start_locked=False) == 0


@pytest.mark.parametrize("locked", [False, True])
def test_exact_source_edge_with_rounded_output_survives_saved_render_preparation(locked):
    from app.routes.generative_jobs import TimelineSlotEdit
    from app.services.authored_editor import resolve_authored_phone_slots
    from app.services.editor_empty_drafts import prepare_saved_render_windows

    job, variant, previous = fixture()
    rate = 3.5
    output_duration = round(12 / rate * 30) / 30
    rows = resolve_authored_phone_slots(
        job,
        variant,
        [
            TimelineSlotEdit(
                slot_id="added",
                clip_index=1,
                in_s=0,
                source_end_s=12,
                duration_s=output_duration,
                playback_rate=rate,
                source_start_locked=locked,
            )
        ],
        preserve_intent=True,
    )
    variant["user_timeline"]["slots"] = rows
    variant["render_status"] = "awaiting_device"
    job.assembly_plan["variants"] = [variant]
    before = copy.deepcopy(rows)
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_asset_id == "new"
    assert clip.source_start == 0
    assert clip.source_duration == 12
    assert clip.rate == rate
    assert clip.hold_duration == pytest.approx(output_duration - 12 / rate)
    assert recipe.duration == pytest.approx(output_duration)
    prep = {"render_destination": "device"}
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep
    saved = job.assembly_plan["variants"][0]
    assert saved["render_status"] == "awaiting_device"
    assert saved["render_window_mappings"] == []
    assert saved["user_timeline"]["slots"] == before
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep
    assert job.assembly_plan["variants"][0]["user_timeline"]["slots"] == before


@pytest.mark.parametrize("source_end", [12.001, 13])
def test_invalid_exact_edge_is_rejected_before_saved_render_preparation(source_end):
    from fastapi import HTTPException

    from app.routes.generative_jobs import TimelineSlotEdit
    from app.services.authored_editor import resolve_authored_phone_slots

    job, variant, _ = fixture()
    before = copy.deepcopy(variant)
    with pytest.raises(HTTPException, match="TIMELINE_INVALID_WINDOW"):
        resolve_authored_phone_slots(
            job,
            variant,
            [
                TimelineSlotEdit(
                    clip_index=1, in_s=0, source_end_s=source_end, duration_s=12, playback_rate=1
                )
            ],
            preserve_intent=True,
        )
    assert variant == before


def test_missing_explicit_endpoint_source_still_requires_recovery():
    from app.services.editor_empty_drafts import prepare_saved_render_windows

    job, variant, _ = fixture()
    variant["user_timeline"]["slots"][0].update(source_end_s=5, source_unavailable=True)
    job.assembly_plan["variants"] = [variant]
    prep = {}
    prepare_saved_render_windows(job, "original_text", prep)
    assert "reconnect" in prep["render_recovery"]


def test_cloud_endpoint_metadata_does_not_bypass_its_legacy_window_check():
    from app.services.editor_empty_drafts import prepare_saved_render_windows

    job, variant, _ = fixture()
    variant["render_destination"] = "cloud"
    variant["user_timeline"]["slots"][0].update(
        in_s=0,
        duration_s=13,
        source_end_s=12,
        source_duration_s=12,
    )
    job.assembly_plan["variants"] = [variant]
    prep = {}
    prepare_saved_render_windows(job, "original_text", prep)
    assert "more footage" in prep["render_recovery"]


@pytest.mark.parametrize("source_end", [float("nan"), float("inf"), 0])
def test_malformed_exact_edge_is_rejected_by_the_wire_schema(source_end):
    from pydantic import ValidationError

    from app.routes.generative_jobs import TimelineSlotEdit

    with pytest.raises(ValidationError):
        TimelineSlotEdit(
            clip_index=1, in_s=0, source_end_s=source_end, duration_s=12, playback_rate=1
        )


def test_roundoff_window_compiles_and_prepares_without_changing_the_saved_cut():
    from app.routes.generative_jobs import TimelineSlotEdit
    from app.services.authored_editor import resolve_authored_phone_slots
    from app.services.editor_empty_drafts import prepare_saved_render_windows
    from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD

    job, variant, previous = fixture()
    source = variant[EDITOR_SOURCES_FIELD]["sources"][0]
    source["duration_s"] = 1.2
    source["source_binding"]["original"]["duration_s"] = 1.2
    rows = resolve_authored_phone_slots(
        job,
        variant,
        [
            TimelineSlotEdit(
                slot_id="added", clip_index=1, in_s=0, duration_s=6.2 - 5, source_start_locked=True
            )
        ],
        preserve_intent=True,
    )
    variant["user_timeline"]["slots"] = rows
    variant["render_status"] = "awaiting_device"
    job.assembly_plan["variants"] = [variant]
    before = copy.deepcopy(rows)
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_asset_id == "new"
    assert clip.source_start == 0
    assert clip.source_duration == 6.2 - 5
    assert clip.rate == 1
    assert recipe.duration == 6.2 - 5
    prep = {"render_destination": "device"}
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep
    assert job.assembly_plan["variants"][0]["user_timeline"]["slots"] == before


@pytest.mark.parametrize("rate", [0.5, 1, 1.3, 3.5, 4])
def test_exact_endpoint_survives_the_actual_prepared_phone_save(monkeypatch, rate):
    from app.routes.generative_jobs import EditorCommitRequest, TimelineSlotEdit
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.device_render import device_status
    from app.services.editor_empty_drafts import prepare_saved_render_windows
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, _variant = _authored_phone_job(monkeypatch)
    duration = round(12 / rate * 30) / 30
    prep = prepare_authored_editor_commit(
        job,
        "original_text",
        EditorCommitRequest(
            base_generation="first",
            editor_state_version=2,
            timeline_slots=[
                TimelineSlotEdit(
                    clip_index=1,
                    in_s=0,
                    source_end_s=12,
                    duration_s=duration,
                    playback_rate=rate,
                    source_start_locked=True,
                )
            ],
        ),
        validation_arguments={},
    )
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep
    assert prep["render_destination"] == "device"
    request = device_status(job, "original_text").request
    clip = request.recipe.tracks[0].clips[0]
    assert clip.source_asset_id == "new"
    assert clip.source_start == 0
    assert clip.source_duration == 12
    assert clip.rate == rate
    hold = duration - 12 / rate
    if hold > 1e-6:
        assert clip.hold_duration == pytest.approx(hold)
    else:
        assert clip.hold_duration is None
    assert request.recipe.duration == pytest.approx(duration)


def _pilot_hold_recipe(*, hold, frame_rate=30, track_kind="video"):
    from app.kria.recipes_v2 import EditRecipeV2

    _, _, previous = fixture()
    raw = previous.model_dump(mode="json")
    raw["frame_rate"] = frame_rate
    raw["tracks"][0]["kind"] = track_kind
    raw["tracks"][0]["clips"][0]["hold_duration"] = hold
    return EditRecipeV2.model_validate(raw)


@pytest.mark.parametrize("frame_rate", [24, 30, 60])
@pytest.mark.parametrize("fraction", [0, 0.1, 0.5, 1])
def test_pilot_accepts_main_video_frame_padding_without_freeze_capability(
    monkeypatch, frame_rate, fraction
):
    from app.config import settings
    from app.services.phone_rollout import recipe_has_freeze_frame, validate_phone_pilot_recipe

    recipe = _pilot_hold_recipe(hold=fraction / frame_rate, frame_rate=frame_rate)
    assert "freezeFrame" not in recipe.required_capabilities
    assert not recipe_has_freeze_frame(recipe)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


@pytest.mark.parametrize("frame_rate", [24, 30, 60])
def test_pilot_keeps_the_real_freeze_capability_gate(monkeypatch, frame_rate):
    from app.config import settings
    from app.services.phone_rollout import (
        PhoneCapabilityUnavailable,
        recipe_has_freeze_frame,
        validate_phone_pilot_recipe,
    )

    recipe = _pilot_hold_recipe(hold=1 / frame_rate + 0.001, frame_rate=frame_rate)
    assert "freezeFrame" in recipe.required_capabilities
    assert recipe_has_freeze_frame(recipe)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(recipe.required_capabilities - {"freezeFrame"}),
    )
    with pytest.raises(PhoneCapabilityUnavailable) as rejected:
        validate_phone_pilot_recipe(recipe)
    assert rejected.value.capability == "freezeFrame"
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_pilot_frame_padding_does_not_qualify_overlay_holds(monkeypatch):
    from app.config import settings
    from app.services.phone_rollout import validate_phone_pilot_recipe

    recipe = _pilot_hold_recipe(hold=0.005, track_kind="overlay")
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    with pytest.raises(ValueError, match="Editor media await"):
        validate_phone_pilot_recipe(recipe)
