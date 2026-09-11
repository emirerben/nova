import copy
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.agents._schemas.text_element import TextElement
from app.kria.device_render import make_device_request
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.pipeline.test_phone_guided_plan import fixture


def phone_job(monkeypatch):
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "positionedText", "animatedText", "audioMix"],
    )
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", False)
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="title",
            text="Before",
            start_s=0,
            end_s=2,
            effect="none",
            font_family="Inter-Bold",
            size_px=64,
        )
    ]
    raw = plan.model_dump(mode="json")
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            "guided_story_execution_plan": raw,
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [
                {
                    "variant_id": "guided_story",
                    "resolved_archetype": "guided_story",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": 3,
                    "text_mode": "agent_text",
                    "intro_mode": "linear",
                    "intro_layout": "linear",
                    "text_elements": copy.deepcopy(raw["text_elements"]),
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="guided_story",
            revision=1,
            recipe=compile_phone_guided_plan(plan, bindings),
        ),
        base_generation="first",
    )
    return job


def save(job, *, generation="first", effect="fade-in", motion=None):
    element = {
        **job.assembly_plan["variants"][0]["text_elements"][0],
        "text": "After",
        "effect": effect,
        "motion": motion,
    }
    return gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(base_generation=generation, text_elements=[element]),
    )


def test_phone_save_atomically_pins_revision_without_cloud_dispatch(monkeypatch):
    job = phone_job(monkeypatch)
    old = device_status(job, "guided_story").request
    prep = save(job)
    new = device_status(job, "guided_story").request
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1
    assert new.identity.recipe_digest != old.identity.recipe_digest
    assert new.recipe.text_layers[0].effect == "fade-in"
    assert "After" in new.model_dump_json()
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    assert prep["render_destination"] == "device"
    with patch("app.tasks.generative_build.regenerate_generative_variant.apply_async") as cloud:
        gj.enqueue_editor_commit_render(str(job.id), "guided_story", prep)
    cloud.assert_not_called()
    before = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException) as stale:
        save(job)
    assert stale.value.status_code == 409
    assert job.assembly_plan == before
    save(job, generation=prep["generation"])
    assert device_status(job, "guided_story").request.identity.recipe_revision == 3


@pytest.mark.parametrize("failure", ["capability", "cohort", "rollback", "binding"])
def test_failed_native_compile_leaves_entire_baseline_untouched(monkeypatch, failure):
    job = phone_job(monkeypatch)
    if failure == "capability":
        monkeypatch.setattr(
            gj.settings,
            "phone_render_verified_features",
            ["basicComposition", "local1080Export", "positionedText", "audioMix"],
        )
    elif failure == "cohort":
        monkeypatch.setattr(gj.settings, "phone_render_user_ids", [uuid.uuid4()])
    elif failure == "rollback":
        monkeypatch.setattr(gj.settings, "phone_rendering_enabled", False)
    elif failure == "binding":
        job.assembly_plan[PHONE_SOURCES_FIELD][0]["generation"] = "changed"
    before = copy.deepcopy(vars(job))
    with pytest.raises(HTTPException) as error:
        save(job, effect="fade-in")
    assert error.value.status_code == 422
    assert vars(job) == before


@pytest.mark.parametrize("effect", ["typewriter", "stream-in", "smooth-type", "staggered-slice"])
def test_reveal_save_stays_on_device(monkeypatch, effect):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "text_motion_v2_enabled", True)
    prep = save(job, effect=effect, motion={"version": 2} if effect == "smooth-type" else None)
    request = device_status(job, "guided_story").request
    layer = request.recipe.text_layers[0]
    assert layer.effect == effect
    content = layer.staggered or layer.smooth_reveal or layer.discrete_reveal
    assert content.text == "After"
    assert request.identity.recipe_revision == 2
    with patch("app.tasks.generative_build.regenerate_generative_variant.apply_async") as cloud:
        gj.enqueue_editor_commit_render(str(job.id), "guided_story", prep)
    cloud.assert_not_called()


def test_phone_guided_revision_compiles_trimmed_source_window(monkeypatch):
    from app.pipeline.guided_story import compile_execution_plan
    from app.schemas.edit_proposal import (
        EditProposalSnapshot,
        FastMontageCut,
        MediaRef,
        StoryBeat,
        canonical_media_digest,
    )

    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
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
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    segment = revision["segments"][0]
    prep = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=[
                gj.TimelineSlotEdit(
                    slot_id=segment["segment_id"], clip_index=0, in_s=2.2, duration_s=0.8
                )
            ],
        ),
    )
    request = device_status(job, "guided_story").request
    clip = request.recipe.tracks[0].clips[0]
    assert (clip.source_start, clip.source_duration) == pytest.approx((2.2, 0.8))
    assert request.recipe.duration == pytest.approx(0.8)
    assert prep["revision_number"] == 2
    assert request.identity.recipe_revision == 2
    assert job.assembly_plan["variants"][0]["duration_s"] == pytest.approx(0.8)
