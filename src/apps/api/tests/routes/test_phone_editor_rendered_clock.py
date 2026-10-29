"""A published native source window must survive editor read and text-only Save."""

import copy

import pytest

from app.kria.device_render import make_device_request
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor import PHONE_EDITOR_PLAN_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from tests.routes.test_phone_editor_commit import phone_job


def rendered_clock_job(monkeypatch):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    binding = PhoneSourceBinding.model_validate(job.assembly_plan[PHONE_SOURCES_FIELD][0])
    binding.original.duration_s = 1.92
    job.assembly_plan[PHONE_SOURCES_FIELD] = [binding.model_dump(mode="json")]
    media = [
        MediaRef(
            lane="clip",
            media_id="source",
            gcs_path=binding.proxy_path,
            generation="123",
            kind="video",
            duration_s=1.933333,
        )
    ]
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        duration_s=3.433333,
        title="A scene",
        media=media,
        story_beats=[
            StoryBeat(beat_id="story", topic="Scene", media_ids=["source"], duration_s=3.433333)
        ],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                k: getattr(media[0], k)
                for k in ("lane", "media_id", "gcs_path", "generation", "kind")
            }
        ],
    }
    # These exact numbers are from the retained successful recipe, independent
    # of the proxy's 1.933333-second capacity and the editor's 30fps projection.
    from tests.pipeline.test_phone_guided_plan import fixture

    plan, _ = fixture()
    plan.approved_duration_s = plan.resolved_duration_s = 3.42
    moment = plan.story_timeline[0]
    moment.source_start_s = moment.output_start_s = 0
    moment.source_end_s = moment.output_end_s = moment.duration_s = 1.92
    following = moment.model_copy(
        update={
            "moment_id": "following",
            "source_end_s": 1.5,
            "duration_s": 1.5,
            "output_start_s": 1.92,
            "output_end_s": 3.42,
        }
    )
    plan.story_timeline.append(following)
    plan.beat_windows[0].approved_duration_s = 3.42
    plan.beat_windows[0].resolved_duration_s = plan.beat_windows[0].end_s = 3.42
    plan.text_elements = []
    raw = plan.model_dump(mode="json")
    job.assembly_plan.update(guided_edit=guided, guided_story_execution_plan=copy.deepcopy(raw))
    variant = job.assembly_plan["variants"][0]
    variant.update(duration_s=3.42, text_elements=[], **{PHONE_EDITOR_PLAN_FIELD: raw})
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="guided_story",
            revision=2,
            recipe=compile_phone_guided_plan(plan, (binding,)),
        ),
        base_generation="first",
    )
    return job


def test_native_source_and_output_clock_survive_initial_editor_projection(monkeypatch):
    job = rendered_clock_job(monkeypatch)
    before = copy.deepcopy(job.assembly_plan)
    recipe = device_status(job, "guided_story").request.recipe
    timeline = gj.dispatch_get_timeline(
        job, "guided_story", sign_url=lambda path, ttl: "https://inert.invalid/asset"
    )
    slot = timeline["slots"][0]
    clip = recipe.tracks[0].clips[0]
    assert slot["in_s"] == clip.source_start == 0
    assert slot["duration_s"] == clip.source_duration == 1.92
    assert slot["output_start_s"] == clip.timeline_start == 0
    assert slot["output_end_s"] == 1.92
    assert slot["source_duration_s"] == 1.92
    assert job.assembly_plan == before


def test_text_only_save_and_reopen_keep_the_native_clock(monkeypatch):
    job = rendered_clock_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    before = gj._guided_v2_revision(job, variant)
    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant),
            guided_revision_number=before["revision_number"],
            text_elements=[],
        ),
    )
    after = gj.dispatch_get_timeline(
        job, "guided_story", sign_url=lambda path, ttl: "https://inert.invalid/asset"
    )
    assert after["slots"][0]["duration_s"] == 1.92
    assert after["slots"][0]["output_end_s"] == 1.92


def test_client_cannot_remove_rendered_clock_on_save(monkeypatch):
    job = rendered_clock_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    current = gj._guided_v2_revision(job, variant)
    supplied = {**current, "timing_mode": None}
    from fastapi import HTTPException

    before = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException) as error:
        gj.prepare_editor_commit(
            job,
            "guided_story",
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(variant),
                guided_revision_number=current["revision_number"],
                guided_revision=supplied,
            ),
        )
    assert error.value.detail == "GUIDED_REVISION_WIRE_UNSUPPORTED"
    assert job.assembly_plan == before


def test_rendered_revision_rejects_real_source_overrun(monkeypatch):
    from app.schemas.guided_edit_revision import normalize_guided_editor_revision

    job = rendered_clock_job(monkeypatch)
    revision = gj._guided_v2_revision(job, job.assembly_plan["variants"][0])
    revision["segments"][0]["source_end_s"] = 1.933333
    revision["state_hash"] = ""
    with pytest.raises(ValueError, match="exceeds original duration"):
        normalize_guided_editor_revision(revision)


@pytest.mark.parametrize("protocol", [None, 24, 25])
def test_rendered_clock_requires_compatible_http_reader(monkeypatch, protocol):
    from fastapi import HTTPException

    from app.services.client_protocol import clear_request_context, set_client_protocol

    job = rendered_clock_job(monkeypatch)
    before = copy.deepcopy(job.assembly_plan)
    set_client_protocol(protocol)
    try:
        if protocol == 25:
            assert (
                gj.dispatch_get_timeline(
                    job, "guided_story", sign_url=lambda *_: "https://inert.invalid"
                )["slots"][0]["duration_s"]
                == 1.92
            )
        else:
            with pytest.raises(HTTPException) as error:
                gj.dispatch_get_timeline(
                    job, "guided_story", sign_url=lambda *_: "https://inert.invalid"
                )
            assert error.value.status_code == 426
            assert error.value.detail["feature"] == "rendered_timeline_clock"
            with pytest.raises(HTTPException) as save_error:
                gj.prepare_editor_commit(
                    job,
                    "guided_story",
                    gj.EditorCommitRequest(
                        base_generation=gj.variant_render_baseline(
                            job.assembly_plan["variants"][0]
                        ),
                        guided_revision_number=1,
                        text_elements=[],
                    ),
                )
            assert save_error.value.status_code == 426
        assert job.assembly_plan == before
    finally:
        clear_request_context()


def test_rendered_rate_edit_preserves_following_start_and_transition(monkeypatch):
    from app.schemas.guided_edit_revision import normalize_guided_editor_revision
    from app.services.guided_timeline import build_guided_segments

    job = rendered_clock_job(monkeypatch)
    current = gj._guided_v2_revision(job, job.assembly_plan["variants"][0])
    slots = [
        gj.TimelineSlotEdit(
            slot_id="moment",
            clip_index=0,
            in_s=0,
            duration_s=2.56,
            source_end_s=1.92,
            playback_rate=0.75,
        ),
        gj.TimelineSlotEdit(slot_id="following", clip_index=0, in_s=0, duration_s=1.5),
    ]
    segments = build_guided_segments(
        current, slots, transitions_enabled=True, max_slots=120, max_total_s=120
    )
    revision = normalize_guided_editor_revision({**current, "segments": segments, "state_hash": ""})
    assert revision["segments"][0]["duration_s"] == 2.56
    assert revision["segments"][1]["output_start_s"] == 2.56
    from app.services.guided_timeline import effective_transitions

    assert (
        effective_transitions(
            [0.373, 0.373], [("crossfade", 0.3), ("cut", 0)], rendered_clock=True
        )[0][1]
        == 0.1119
    )


def test_rate_crossfade_uses_resolved_original_duration(monkeypatch):
    from app.services.guided_timeline import build_guided_segments

    job = rendered_clock_job(monkeypatch)
    current = gj._guided_v2_revision(job, job.assembly_plan["variants"][0])
    slots = [
        gj.TimelineSlotEdit(
            slot_id="moment",
            clip_index=0,
            in_s=0,
            duration_s=11 / 30,
            source_end_s=1.12,
            playback_rate=3,
            transition_after="crossfade",
            transition_duration_s=0.3,
        ),
        gj.TimelineSlotEdit(slot_id="following", clip_index=0, in_s=0, duration_s=1.5),
    ]
    segments = build_guided_segments(
        current, slots, transitions_enabled=True, max_slots=120, max_total_s=120
    )
    assert segments[0]["duration_s"] == 1.12 / 3
    assert segments[0]["transition_duration_s"] == (1.12 / 3) * 0.3
    assert segments[1]["output_start_s"] == pytest.approx((1.12 / 3) * 0.7, abs=1e-12)
