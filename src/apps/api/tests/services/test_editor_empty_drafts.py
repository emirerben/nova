import copy
from types import SimpleNamespace

import pytest

from app.services.editor_empty_drafts import editor_sections, public_empty_editor_variant


def test_editor_sections_materializes_generated_intro_for_empty_snapshot() -> None:
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "v",
                    "render_generation_id": "generation",
                    "text_mode": "agent_text",
                    "intro_text": "Saved hook",
                }
            ]
        }
    )

    sections = editor_sections(job, "v")

    assert sections["text_elements"][0]["text"] == "Saved hook"
    assert sections["text_elements"][0]["role"] == "generative_intro"


def test_empty_public_variant_never_exposes_last_render_artifacts() -> None:
    variant = {
        "variant_id": "v",
        "editor_state": "empty",
        "video_path": "private/last.mp4",
        "base_video_path": "private/base.mp4",
        "output_url": "https://signed/last.mp4",
        "poster_url": "https://signed/poster.jpg",
        "duration_s": 12.0,
    }

    projected = public_empty_editor_variant(SimpleNamespace(_saved_editor_drafts={}), variant)

    assert projected["editor_state"] == "empty"
    assert projected["output_url"] is None
    assert projected["video_path"] is None
    assert projected["base_video_path"] is None
    assert projected["poster_url"] is None
    assert projected["duration_s"] == 0.0


@pytest.mark.parametrize("enabled", [False, True])
def test_initial_guided_deletion_materializes_effective_lanes_before_mode_switch(
    monkeypatch, enabled
):
    from app.routes import generative_jobs as gj
    from app.services.editor_empty_drafts import stage_initial_authored_baseline
    from tests.routes.test_editor_commit import _narrated_guided_job

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", enabled)
    job = _narrated_guided_job()
    variant = job.assembly_plan["variants"][0]
    label = {"id": "participant-label", "text": "Alice", "start_s": 0, "end_s": 2}
    job.assembly_plan["guided_story_execution_plan"]["narration_label_text_elements"] = [label]
    revision = gj._guided_v2_revision(job, variant)
    revision.update(
        sound_effects=[{"id": "effect", "sound_effect_id": "pop", "start_s": 1}],
        media_overlays=[{"id": "overlay", "start_s": 2}],
        visual_blocks=[{"id": "visual", "start_s": 3}],
        motion_scenes=[{"id": "motion", "start_s": 4}],
        custom_effects=[{"id": "custom", "start_s": 5}],
        caption_meta={"style": "sentence", "y_frac": 0.65},
        audio={
            "mode": "track",
            "track_id": "track",
            "title": "Bed",
            "audio_gcs_path": "music/bed",
            "generation": "1",
            "start_s": 4,
            "end_s": 15,
            "level": 0.3,
        },
        state_hash="",
    )
    variant["guided_edit_revision"] = revision
    before = copy.deepcopy(job.assembly_plan)
    staged = stage_initial_authored_baseline(job, variant["variant_id"], [{"slot_id": "kept"}])
    effective = staged.assembly_plan["variants"][0]
    assert effective["editor_timeline_mode"] == "authored"
    assert any(row["id"] == "participant-label" for row in effective["text_elements"])
    for lane in (
        "sound_effects",
        "media_overlays",
        "visual_blocks",
        "motion_scenes",
        "custom_effects",
    ):
        assert effective[lane] == revision[lane]
    assert effective["caption_meta"] == revision["caption_meta"]
    assert effective["music_track_id"] == "track"
    assert effective["music_start_s"] == 4
    assert effective["mix"] == 0.3
    gj.require_guided_story_editor_commit(
        staged,
        variant["variant_id"],
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(effective),
            timeline_slots=[gj.TimelineSlotEdit(clip_index=0, in_s=0, duration_s=1)],
        ),
    )
    assert (
        editor_sections(staged, variant["variant_id"])["visual_blocks"] == revision["visual_blocks"]
    )
    assert job.assembly_plan == before


@pytest.mark.asyncio
@pytest.mark.parametrize("from_empty", [False, True])
async def test_first_staged_device_restore_checks_original_source_receipt(monkeypatch, from_empty):
    from fastapi import HTTPException

    from app.routes import editor_sources
    from app.routes import generative_jobs as gj
    from app.services.editor_empty_drafts import (
        stage_initial_authored_baseline,
        stage_saved_draft_baseline,
    )
    from tests.pipeline.test_authored_timeline import fixture

    job, variant, _previous = fixture()
    variant.pop("editor_timeline_mode")
    variant["render_generation_id"] = "generation"
    job.assembly_plan["variants"] = [variant]
    if from_empty:
        variant["editor_state"] = "empty"
        job._saved_editor_drafts = {
            variant["variant_id"]: SimpleNamespace(
                snapshot={
                    "editor_payload": {"sections": {"timeline_slots": [], "text_elements": []}}
                }
            )
        }
        staged = stage_saved_draft_baseline(
            job,
            variant["variant_id"],
            gj.EditorCommitRequest(
                base_generation="generation",
                editor_state_version=1,
            ),
        )
    else:
        staged = stage_initial_authored_baseline(job, variant["variant_id"], [{"clip_index": 0}])
    effective = staged.assembly_plan["variants"][0]
    assert effective["editor_timeline_mode"] == "authored"
    monkeypatch.setattr(
        editor_sources.storage, "object_metadata", lambda path: SimpleNamespace(generation="stale")
    )
    with pytest.raises(HTTPException, match="editor_source_generation_stale"):
        await editor_sources.validate_editor_sources(
            None, job=staged, variant=effective, used_media_ids={"old"}
        )
    assert "editor_timeline_mode" not in variant
