"""Synthetic existing-editor capability probe, not Cabildo semantic fulfillment."""

import pytest

from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from tests.routes.test_phone_editor_commit import _enable_guided_v2, phone_job


def test_editor_can_save_three_disjoint_windows_of_same_asset(monkeypatch, tmp_path):
    job = phone_job(monkeypatch)
    revision = _enable_guided_v2(job, monkeypatch)
    starts = [8.0, 0.0, 4.0]
    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            text_elements=[],
            timeline_slots=[
                gj.TimelineSlotEdit(
                    slot_id=row["segment_id"], clip_index=0, in_s=start, duration_s=1
                )
                for row, start in zip(revision["segments"], starts, strict=True)
            ],
        ),
    )
    recipe = device_status(job, "guided_story").request.recipe
    clips = recipe.tracks[0].clips
    assert len(clips) == 3
    assert len({row.source_asset_id for row in clips}) == 1
    assert [row.source_start for row in clips] == starts
    assert [row.source_duration for row in clips] == [1, 1, 1]
    assert [row.timeline_start for row in clips] == [0, 1, 2]
    assert recipe.duration == 3
    destination = tmp_path
    destination.mkdir(exist_ok=True)
    (destination / "recipe.json").write_text(recipe.model_dump_json(indent=2))


def test_editor_rejects_out_of_bounds_window_without_mutating_saved_plan(monkeypatch):
    import copy

    from fastapi import HTTPException

    job = phone_job(monkeypatch)
    revision = _enable_guided_v2(job, monkeypatch)
    before = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException):
        gj.prepare_editor_commit(
            job,
            "guided_story",
            gj.EditorCommitRequest(
                base_generation="first",
                guided_revision_number=revision["revision_number"],
                text_elements=[],
                timeline_slots=[
                    gj.TimelineSlotEdit(
                        slot_id=revision["segments"][0]["segment_id"],
                        clip_index=0,
                        in_s=9.5,
                        duration_s=1,
                    )
                ],
            ),
        )
    assert job.assembly_plan == before
