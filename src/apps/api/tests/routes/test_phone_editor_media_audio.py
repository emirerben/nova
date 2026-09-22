"""Regression coverage for device-only editor media and narrated re-saves."""

from __future__ import annotations

import copy

from app.kria.device_render import make_device_request
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD, PhoneSourceBinding
from tests.pipeline.test_phone_guided_plan import narration_fixture
from tests.routes.test_phone_editor_commit import phone_job
from tests.routes.test_phone_editor_media import media_job


def _admit_footage(variant: dict) -> PhoneSourceBinding:
    binding = PhoneSourceBinding.model_validate(
        {
            "media_id": "admitted-footage",
            "proxy_path": "users/owner/thread/analysis-proxy-admitted-footage.mp4",
            "generation": "admitted-generation",
            "original": {
                "sha256": "f" * 64,
                "byte_count": 1234,
                "duration_s": 4,
                "width": 1920,
                "height": 1080,
                "has_audio": True,
            },
        }
    )
    variant[EDITOR_SOURCES_FIELD] = {
        "imports": {},
        "sources": [
            {
                "status": "ready",
                "source_index": 1,
                "media_id": binding.media_id,
                "lane": "clip",
                "gcs_path": binding.proxy_path,
                "generation": binding.generation,
                "kind": "video",
                "duration_s": binding.original.duration_s,
                "source_binding": binding.model_dump(mode="json"),
            }
        ],
    }
    return binding


def test_admitted_footage_save_projects_the_retained_original_not_proxy(monkeypatch):
    job, variant = media_job(monkeypatch)
    binding = _admit_footage(variant)
    revision = gj._guided_v2_revision(job, variant)

    prep = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=[gj.TimelineSlotEdit(clip_index=1, in_s=0, duration_s=1)],
        ),
    )

    recipe = device_status(job, "guided_story").request.recipe
    admitted_asset = next(
        asset for asset in recipe.asset_manifest.assets if asset.id == binding.media_id
    )
    assert admitted_asset.kind == "original"
    assert admitted_asset.media_id == binding.media_id
    assert (admitted_asset.fingerprint.sha256, admitted_asset.fingerprint.byte_count) == (
        binding.original.sha256,
        binding.original.byte_count,
    )
    assert binding.proxy_path not in recipe.model_dump_json()
    assert recipe.tracks[0].clips[-1].source_asset_id == binding.media_id
    assert prep["render_destination"] == "device"


def _narrated_phone_job(monkeypatch):
    job = phone_job(monkeypatch)
    plan, bindings, visuals, bed = narration_fixture()
    raw = plan.model_dump(mode="json")
    initial_recipe = compile_phone_guided_plan(plan, bindings, visuals=visuals, narration=bed)
    monkeypatch.setattr(
        gj.settings, "phone_render_verified_features", list(initial_recipe.required_capabilities)
    )
    job.assembly_plan = {
        "guided_story_execution_plan": raw,
        PHONE_SOURCES_FIELD: [binding.model_dump(mode="json") for binding in bindings],
        PHONE_VISUALS_FIELD: [visual.model_dump(mode="json") for visual in visuals],
        "variants": [
            {
                "variant_id": "guided_story",
                "resolved_archetype": "guided_story",
                "render_destination": "device",
                "render_status": "awaiting_device",
                "render_generation_id": "first",
                "duration_s": raw["resolved_duration_s"],
                "text_mode": "agent_text",
                "intro_mode": "linear",
                "intro_layout": "linear",
                "text_elements": copy.deepcopy(raw["text_elements"]),
            }
        ],
    }
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id, variant_id="guided_story", revision=1, recipe=initial_recipe
        ),
        base_generation="first",
    )
    return job


def test_narrated_phone_recipe_keeps_pinned_voiceover_through_two_text_saves(monkeypatch):
    job = _narrated_phone_job(monkeypatch)
    initial = device_status(job, "guided_story").request.recipe
    voice_id = next(
        asset.id for asset in initial.asset_manifest.assets if asset.kind == "voiceover"
    )
    initial_voice = next(asset for asset in initial.asset_manifest.assets if asset.id == voice_id)
    initial_audio_track = next(track for track in initial.tracks if track.kind == "audio")

    first = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            text_elements=[
                {**job.assembly_plan["variants"][0]["text_elements"][0], "text": "First"}
            ],
        ),
    )
    second = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation=first["generation"],
            text_elements=[
                {**job.assembly_plan["variants"][0]["text_elements"][0], "text": "Second"}
            ],
        ),
    )

    saved = device_status(job, "guided_story").request.recipe
    assert (
        next(asset for asset in saved.asset_manifest.assets if asset.id == voice_id)
        == initial_voice
    )
    assert next(track for track in saved.tracks if track.kind == "audio") == initial_audio_track
    assert saved.audio.narration_asset_id == voice_id
    assert second["render_destination"] == "device"
    assert device_status(job, "guided_story").request.identity.recipe_revision == 3
