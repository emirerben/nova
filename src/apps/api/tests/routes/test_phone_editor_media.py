"""Device Save/reopen contract for server-admitted editor media."""

import copy

import pytest
from fastapi import HTTPException

from app.pipeline.guided_story import (
    GuidedStoryError,
    compile_execution_plan,
    compile_guided_runtime_plan,
)
from app.routes import generative_jobs as gj
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.device_render import device_status
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.services.public_assembly_plan import _strip_private_state
from tests.routes.test_phone_editor_commit import phone_job
from tests.services import test_phone_visuals as photos


def media_job(monkeypatch):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(gj.settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [
            *gj.settings.phone_render_verified_features,
            "stillImages",
            "visualVideos",
            "visualBlocks",
            "authoredText",
        ],
    )
    binding = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    media = [
        MediaRef(
            lane="clip",
            media_id="source",
            gcs_path=binding["proxy_path"],
            generation="123",
            kind="video",
            duration_s=10,
        )
    ]
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        duration_s=3,
        title="A short scene",
        media=media,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id="source",
                source_start_s=2 + index,
                source_end_s=3 + index,
                output_duration_s=1,
                role="hook",
            )
            for index in range(3)
        ],
        story_beats=[StoryBeat(beat_id="story", topic="Scene", media_ids=["source"], duration_s=3)],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                key: getattr(media[0], key)
                for key in ("lane", "media_id", "gcs_path", "generation", "kind")
            }
        ],
    }
    plan = compile_execution_plan(guided, track=None)
    job.assembly_plan.update(guided_edit=guided, guided_story_execution_plan=plan)
    variant = job.assembly_plan["variants"][0]
    variant["text_elements"] = plan["text_elements"]
    return job, variant


def admit_photo(variant):
    photo = photos.photo_visual()
    source = {
        "media_id": photo.media_id,
        "lane": "asset",
        "kind": "image",
        "gcs_path": photo.gcs_path,
        "generation": photo.generation,
        "duration_s": None,
    }
    variant[EDITOR_SOURCES_FIELD] = {
        "sources": [
            {
                **source,
                "status": "ready",
                "source_index": 1,
                "visual_binding": photo.model_dump(mode="json"),
            }
        ],
        "imports": {},
    }
    return source


def test_source_admission_rehashes_without_editing_original_or_bumping_revision(monkeypatch):
    job, variant = media_job(monkeypatch)
    original = copy.deepcopy(job.assembly_plan)
    before = gj._guided_v2_revision(job, variant)
    source = admit_photo(variant)
    after = gj._guided_v2_revision(job, variant)
    assert after["sources"] == [*before["sources"], source]
    assert after["state_hash"] != before["state_hash"]
    assert after["revision_number"] == before["revision_number"]
    assert job.assembly_plan["guided_edit"] == original["guided_edit"]
    assert (
        job.assembly_plan["guided_story_execution_plan"] == original["guided_story_execution_plan"]
    )
    # Persisted revisions also acquire the union on read; reopening is stable.
    variant["guided_edit_revision"] = before
    assert gj._guided_v2_revision(job, variant) == after


def test_untrusted_revision_cannot_admit_sources_but_server_receipt_can(monkeypatch):
    job, variant = media_job(monkeypatch)
    source = admit_photo(variant)
    revision = gj._guided_v2_revision(job, variant)
    args = (
        job.assembly_plan["guided_story_execution_plan"],
        job.assembly_plan["guided_edit"],
        revision,
    )
    with pytest.raises(GuidedStoryError):
        compile_guided_runtime_plan(*args)
    assert (
        compile_guided_runtime_plan(*args, admitted_sources=[source])["editor_source_pool"][-1]
        == source
    )
    with pytest.raises(GuidedStoryError):
        compile_guided_runtime_plan(*args, admitted_sources=[{**source, "generation": "replaced"}])


def test_timeline_photo_save_then_text_save_keeps_added_media_after_rollback(monkeypatch):
    job, variant = media_job(monkeypatch)
    admit_photo(variant)
    original = copy.deepcopy(job.assembly_plan["guided_edit"])
    revision = gj._guided_v2_revision(job, variant)
    prep = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=[
                gj.TimelineSlotEdit(
                    slot_id=revision["segments"][0]["segment_id"],
                    clip_index=0,
                    in_s=2,
                    duration_s=1,
                ),
                gj.TimelineSlotEdit(clip_index=1, in_s=0, duration_s=1),
            ],
        ),
    )
    saved = device_status(job, "guided_story").request.recipe
    assert saved.tracks[0].clips[-1].source_asset_id == f"visual-{photos.PHOTO_ID}"
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", False)
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation=prep["generation"],
            guided_revision_number=prep["revision_number"],
            text_elements=[
                {**row, "text": "Edited"}
                for row in gj._guided_text_state_for_response(job, variant)[0]
            ],
        ),
    )
    resaved = device_status(job, "guided_story").request.recipe
    assert resaved.tracks == saved.tracks
    assert resaved.audio == saved.audio
    assert resaved.duration == saved.duration
    assert job.assembly_plan["guided_edit"] == original
    assert (
        gj._guided_v2_revision(job, job.assembly_plan["variants"][0])["sources"][-1]["media_id"]
        == photos.PHOTO_ID
    )


def test_media_visual_save_is_atomic_and_survives_text_resave(monkeypatch):
    job, variant = media_job(monkeypatch)
    admit_photo(variant)
    revision = gj._guided_v2_revision(job, variant)
    block = {
        "id": "photo-layer",
        "kind": "media",
        "asset_id": photos.PHOTO_ID,
        "src_gcs_path": photos.PHOTO_PATH,
        "media_kind": "image",
        "start_s": 0.5,
        "end_s": 1.5,
        "display_mode": "overlay",
        "scale": 0.4,
    }
    assets = {photos.PHOTO_ID: {"status": "ready", "gcs_path": photos.PHOTO_PATH, "kind": "image"}}
    prep = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            visual_blocks=[block],
        ),
        visual_assets=assets,
    )
    saved = device_status(job, "guided_story").request.recipe
    overlay = next(track for track in saved.tracks if track.kind == "overlay")
    assert overlay.clips[0].visual_placement.width_fraction == 0.4
    assert saved.text_layers
    variant = job.assembly_plan["variants"][0]
    before = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException):
        gj.prepare_editor_commit(
            job,
            "guided_story",
            gj.EditorCommitRequest(
                base_generation=prep["generation"],
                guided_revision_number=prep["revision_number"],
                visual_blocks=[
                    {**block, "source_crop": {"x": 0, "y": 0, "width": 0.5, "height": 0.5}}
                ],
            ),
            visual_assets=assets,
        )
    assert job.assembly_plan == before
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", False)
    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation=prep["generation"],
            guided_revision_number=prep["revision_number"],
            text_elements=[],
        ),
    )
    resaved = device_status(job, "guided_story").request.recipe
    assert resaved.tracks == saved.tracks
    assert resaved.audio == saved.audio


def test_capabilities_are_qualified_and_only_open_media(monkeypatch):
    job, variant = media_job(monkeypatch)
    capabilities = gj._editor_capabilities(job, variant)
    assert capabilities["phone_editor_media"]["source_registration"] is True
    assert capabilities["clips"]["add"]["editable"] is True
    assert capabilities["lanes"]["visual_blocks"]["editable"] is True
    assert capabilities["visual_block_kinds"] == ["media"]
    assert capabilities["visual_editor_style"] is False
    assert capabilities["camera_effects"] is False
    monkeypatch.setattr(
        gj.settings, "phone_render_verified_features", ["stillImages", "visualVideos"]
    )
    assert "phone_editor_media" not in gj._editor_capabilities(job, variant)
    assert gj._editor_capabilities(job, variant)["clips"]["add"]["editable"] is False


def test_private_admission_and_effective_plan_are_removed_recursively():
    assert _strip_private_state(
        {
            "variants": [
                {
                    "variant_id": "guided_story",
                    EDITOR_SOURCES_FIELD: {"imports": {"private": "secret"}},
                    "nested": {"_phone_editor_plan_v1": {"path": "private"}},
                }
            ]
        }
    ) == {"variants": [{"variant_id": "guided_story", "nested": {}}]}


@pytest.mark.parametrize("lane", ["timeline", "visual"])
def test_rollback_rejects_new_placements_without_changing_saved_state(monkeypatch, lane):
    job, variant = media_job(monkeypatch)
    admit_photo(variant)
    revision = gj._guided_v2_revision(job, variant)
    monkeypatch.setattr(gj.settings, "phone_editor_media_enabled", False)
    sections = (
        {"timeline_slots": [gj.TimelineSlotEdit(clip_index=1, in_s=0, duration_s=1)]}
        if lane == "timeline"
        else {
            "visual_blocks": [
                {
                    "id": "new-layer",
                    "kind": "media",
                    "asset_id": photos.PHOTO_ID,
                    "src_gcs_path": photos.PHOTO_PATH,
                    "media_kind": "image",
                    "start_s": 0,
                    "end_s": 1,
                }
            ]
        }
    )
    before = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(
            job,
            "guided_story",
            gj.EditorCommitRequest(
                base_generation="first",
                guided_revision_number=revision["revision_number"],
                **sections,
            ),
        )
    assert error.value.detail == {"code": "phone_editor_media_unavailable"}
    assert job.assembly_plan == before


def test_single_save_can_place_visual_on_newly_appended_timeline(monkeypatch):
    job, variant = media_job(monkeypatch)
    admit_photo(variant)
    revision = gj._guided_v2_revision(job, variant)
    slots = [
        gj.TimelineSlotEdit(
            slot_id=row["segment_id"],
            clip_index=0,
            in_s=row["source_start_s"],
            duration_s=row["duration_s"],
        )
        for row in revision["segments"]
    ]
    slots.append(gj.TimelineSlotEdit(clip_index=1, in_s=0, duration_s=3))
    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=slots,
            visual_blocks=[
                {
                    "id": "appended-layer",
                    "kind": "media",
                    "asset_id": photos.PHOTO_ID,
                    "src_gcs_path": photos.PHOTO_PATH,
                    "media_kind": "image",
                    "start_s": 3.5,
                    "end_s": 4.5,
                }
            ],
        ),
        visual_assets={
            photos.PHOTO_ID: {
                "status": "ready",
                "gcs_path": photos.PHOTO_PATH,
                "kind": "image",
            }
        },
    )
    recipe = device_status(job, "guided_story").request.recipe
    assert recipe.duration == 6
    overlay = next(track for track in recipe.tracks if track.kind == "overlay")
    assert overlay.clips[0].timeline_start == 3.5
    assert overlay.clips[0].source_duration == 1
