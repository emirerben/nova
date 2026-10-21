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
