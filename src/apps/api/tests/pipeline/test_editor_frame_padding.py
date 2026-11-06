"""Legacy phone windows retain the output clock without inventing footage."""

import copy

import pytest

from app.pipeline.editor_render_window import RenderWindowUnavailable, effective_source_start
from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from app.services.editor_empty_drafts import prepare_saved_render_windows
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from tests.pipeline.test_authored_timeline import fixture


def legacy_window(*, start=0.0, rate=1.0, locked=False, duration=58 / 30, span=1.92):
    job, variant, previous = fixture()
    source_duration = start + span * rate
    source = variant[EDITOR_SOURCES_FIELD]["sources"][0]
    source["duration_s"] = source_duration
    source["source_binding"]["original"]["duration_s"] = source_duration
    variant["render_status"] = "awaiting_device"
    variant["user_timeline"]["slots"] = [
        {
            "slot_id": "added",
            "clip_index": 1,
            "in_s": start,
            "duration_s": duration,
            "playback_rate": rate,
            "source_start_locked": locked,
            "source_duration_s": source_duration,
            "source_kind": "video",
        }
    ]
    job.assembly_plan["variants"] = [variant]
    return job, variant, previous


@pytest.mark.parametrize("rate", [0.5, 1, 2, 4])
@pytest.mark.parametrize("start,locked", [(0, False), (0, True), (0.25, True)])
def test_legacy_rounded_phone_window_preserves_clock_rate_and_authored_lanes(rate, start, locked):
    job, variant, previous = legacy_window(start=start, rate=rate, locked=locked)
    before = copy.deepcopy(variant)
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_asset_id == "new"
    assert clip.source_start == start
    assert clip.source_duration == pytest.approx(1.92 * rate)
    assert clip.rate == rate
    assert clip.hold_duration == pytest.approx(58 / 30 - 1.92)
    assert recipe.duration == pytest.approx(58 / 30)
    assert variant == before
    prep = {"render_destination": "device"}
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    assert job.assembly_plan["variants"][0]["user_timeline"] == before["user_timeline"]
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep


def test_successful_unlocked_reanchor_does_not_become_frame_padding():
    job, variant, previous = legacy_window(start=0.25)
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_start == pytest.approx(0.25 + 1.92 - 58 / 30)
    assert clip.source_duration == pytest.approx(58 / 30)
    assert clip.hold_duration is None


@pytest.mark.parametrize("duration", [1.921, 1.93, 59 / 30, 2, 3])
@pytest.mark.parametrize("locked", [False, True])
def test_noncanonical_or_genuine_missing_footage_stays_a_recovery(duration, locked):
    job, variant, previous = legacy_window(duration=duration, locked=locked)
    before = copy.deepcopy(variant)
    with pytest.raises(RenderWindowUnavailable):
        compile_phone_authored_timeline(job, variant, previous)
    assert variant == before
    prep = {"render_destination": "device"}
    prepare_saved_render_windows(job, "original_text", prep)
    assert "footage" in prep["render_recovery"]


@pytest.mark.parametrize("span", [1.916, 1.9])
def test_frame_padding_requires_nearest_frame_not_merely_less_than_one_frame(span):
    job, variant, previous = legacy_window(span=span)
    with pytest.raises(RenderWindowUnavailable):
        compile_phone_authored_timeline(job, variant, previous)


def test_generic_and_cloud_source_window_checks_are_not_relaxed():
    with pytest.raises(RenderWindowUnavailable):
        effective_source_start(0, 58 / 30, 1, 1.92, start_locked=False)
    job, variant, _ = legacy_window()
    variant["render_destination"] = "cloud"
    prep = {}
    prepare_saved_render_windows(job, "original_text", prep)
    assert "footage" in prep["render_recovery"]


@pytest.mark.parametrize("kind", ["missing", "prior_recovery"])
def test_frame_padding_does_not_clear_independent_recovery(kind):
    job, variant, _ = legacy_window()
    prep = {}
    if kind == "missing":
        variant["user_timeline"]["slots"][0]["source_unavailable"] = True
    else:
        prep["render_recovery"] = "Caption timing is unavailable."
    prepare_saved_render_windows(job, "original_text", prep)
    if kind == "missing":
        assert "reconnect" in prep["render_recovery"]
    else:
        assert prep["render_recovery"] == "Caption timing is unavailable."


@pytest.mark.parametrize("change", ["duration", "identity"])
def test_stale_row_and_catalog_cannot_authorize_missing_original_footage(change):
    job, variant, previous = legacy_window()
    binding = variant[EDITOR_SOURCES_FIELD]["sources"][0]["source_binding"]
    if change == "duration":
        binding["original"]["duration_s"] = 1.9
    else:
        binding["generation"] = "another-generation"
    with pytest.raises(ValueError):
        compile_phone_authored_timeline(job, variant, previous)
    prep = {}
    prepare_saved_render_windows(job, "original_text", prep)
    assert prep["render_recovery"]


@pytest.mark.parametrize("duplicate_kind", ["assembly", "cross_registry"])
def test_ambiguous_original_receipts_never_authorize_frame_padding(duplicate_kind):
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    job, variant, previous = legacy_window()
    binding = variant[EDITOR_SOURCES_FIELD]["sources"][0]["source_binding"]
    job.assembly_plan[PHONE_SOURCES_FIELD].append(copy.deepcopy(binding))
    if duplicate_kind == "assembly":
        job.assembly_plan[PHONE_SOURCES_FIELD].append(copy.deepcopy(binding))
    binding["original"]["duration_s"] = 1.90
    # A stale saved bound must not hide the ambiguous real source from the
    # compiler, even when the secondary preparation check would fit it.
    variant["user_timeline"]["slots"][0]["source_duration_s"] = 2.0
    with pytest.raises(ValueError):
        compile_phone_authored_timeline(job, variant, previous)


@pytest.mark.parametrize("span,duration,accepted", [(1.25, 38 / 30, True), (1.15, 35 / 30, False)])
def test_half_frame_ties_follow_ties_to_even_on_both_platforms(span, duration, accepted):
    from app.pipeline.editor_render_window import phone_source_window

    if accepted:
        start, source_span, hold = phone_source_window(0, duration, 1, span, start_locked=True)
        assert start == 0
        assert source_span == span
        assert hold == pytest.approx(1 / 60)
    else:
        with pytest.raises(RenderWindowUnavailable):
            phone_source_window(0, duration, 1, span, start_locked=True)


@pytest.mark.parametrize("send_timeline", [False, True])
def test_actual_prepared_save_retries_legacy_geometry_without_changing_lanes(
    monkeypatch, send_timeline
):
    from app.routes.generative_jobs import EditorCommitRequest, TimelineSlotEdit
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.device_render import device_status
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, _ = _authored_phone_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    source = variant[EDITOR_SOURCES_FIELD]["sources"][0]
    source["duration_s"] = 1.92
    source["source_binding"]["original"]["duration_s"] = 1.92
    row = variant["user_timeline"]["slots"][0]
    row.update(in_s=0, duration_s=58 / 30, source_duration_s=1.92, source_start_locked=True)
    variant["text_elements"][0]["end_s"] = 58 / 30
    before = copy.deepcopy(variant)
    # A recovery Save may resend the timeline or retry with another existing
    # render section. Both must compile the same saved legacy source window.
    sections = (
        {"timeline_slots": [TimelineSlotEdit.model_validate(row)]}
        if send_timeline
        else {"text_elements": copy.deepcopy(variant["text_elements"])}
    )
    prep = prepare_authored_editor_commit(
        job,
        "original_text",
        EditorCommitRequest(base_generation="first", editor_state_version=2, **sections),
        validation_arguments={},
    )
    prepare_saved_render_windows(job, "original_text", prep)
    assert "render_recovery" not in prep
    saved = job.assembly_plan["variants"][0]
    assert saved["render_status"] == "awaiting_device"
    assert len(saved["text_elements"]) == len(before["text_elements"])
    for saved_text, original in zip(saved["text_elements"], before["text_elements"], strict=True):
        assert {key: saved_text[key] for key in original} == original
    for key in ("in_s", "duration_s", "source_start_locked"):
        assert saved["user_timeline"]["slots"][0][key] == row[key]
    assert saved["user_timeline"]["slots"][0].get("source_end_s") is None
    recipe = device_status(job, "original_text").request.recipe
    clip = recipe.tracks[0].clips[0]
    assert (clip.source_asset_id, clip.source_start, clip.source_duration, clip.rate) == (
        "new",
        0,
        1.92,
        1,
    )
    assert clip.hold_duration == pytest.approx(58 / 30 - 1.92)
    assert recipe.duration == pytest.approx(58 / 30)


def test_guided_saved_draft_recovers_through_approval_bound_source_catalog(monkeypatch):
    from app.routes.generative_jobs import (
        EditorCommitRequest,
        TimelineSlotEdit,
        editor_deletion_timeline,
    )
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.device_render import device_status
    from app.services.editor_empty_drafts import stage_initial_authored_baseline
    from app.services.phone_sources import PHONE_SOURCES_FIELD
    from tests.routes._editor_save_matrix import guided_phone_job

    job, variant, _ = guided_phone_job(monkeypatch, count=2, clip_s=3)
    # The immutable original is shorter than the approved proxy/catalog hint.
    # Projection must use this bound while retaining the stable source ID.
    job.assembly_plan[PHONE_SOURCES_FIELD][0]["original"]["duration_s"] = 1.92
    rows = editor_deletion_timeline(job, variant)
    rows[0].update(in_s=0, duration_s=58 / 30, source_start_locked=True)
    rows[0].pop("source_end_s", None)
    staged = stage_initial_authored_baseline(job, "guided_story", rows)
    prep = prepare_authored_editor_commit(
        staged,
        "guided_story",
        EditorCommitRequest(
            base_generation="first",
            editor_state_version=2,
            timeline_slots=[TimelineSlotEdit.model_validate(row) for row in rows],
        ),
        validation_arguments={},
    )
    prepare_saved_render_windows(staged, "guided_story", prep)
    assert "render_recovery" not in prep
    recipe = device_status(staged, "guided_story").request.recipe
    video = next(track for track in recipe.tracks if track.kind == "video")
    first, following = video.clips
    assert (first.source_asset_id, first.source_start, first.source_duration, first.rate) == (
        "m0",
        0,
        1.92,
        1,
    )
    assert first.hold_duration == pytest.approx(58 / 30 - 1.92)
    assert following.source_asset_id == "m1"
    assert following.timeline_start == pytest.approx(58 / 30)
    assert following.source_duration == 3
    assert recipe.duration == pytest.approx(58 / 30 + 3)
