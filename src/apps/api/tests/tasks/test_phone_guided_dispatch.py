import copy
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.device_render import device_status
from app.services.generative_jobs import build_generative_job
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_guided_plan import fixture


def setup(monkeypatch):
    plan, bindings = fixture()
    snapshot = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": "generation",
        "guided_edit": {"approved": True},
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=copy.deepcopy(snapshot),
        status="queued",
        all_candidates={},
        error_detail=None,
        failure_reason=None,
    )
    session = Mock()

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
    planner = Mock(return_value=(plan.model_dump(mode="json"), None))
    monkeypatch.setattr(gb, "_guided_execution_plan", planner)
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )
    cloud = Mock(side_effect=AssertionError("phone job entered the cloud renderer"))
    monkeypatch.setattr(gb, "_run_guided_story_job", cloud)
    return job, snapshot, session, planner, cloud


def test_worker_stops_at_immutable_device_request(monkeypatch):
    job, snapshot, session, planner, cloud = setup(monkeypatch)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    request = device_status(job, "guided_story").request
    assert request.recipe.duration == 3
    assert request.identity.recipe_revision == 1
    assert job.assembly_plan["variants"][0]["render_generation_id"] == "generation"
    assert "analysis-proxy" not in request.model_dump_json()
    cloud.assert_not_called()
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", False)
    gb._run_generative_job(str(job.id))
    assert device_status(job, "guided_story").request == request
    assert planner.call_count == 1


@pytest.mark.parametrize("race", ["cancel", "owner", "generation", "binding", "approval"])
def test_stale_planning_cannot_publish(monkeypatch, race):
    job, snapshot, session, planner, cloud = setup(monkeypatch)
    original = planner.return_value

    def race_during_planning(*args):
        if race == "cancel":
            job.status = "cancelled"
        elif race == "owner":
            monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 4))
        elif race == "generation":
            job.assembly_plan["creator_generation_id"] = "new"
        elif race == "binding":
            job.assembly_plan[PHONE_SOURCES_FIELD][0]["generation"] = "124"
        else:
            job.assembly_plan["guided_edit"] = {"approved": False}
        return original

    planner.side_effect = race_during_planning
    gb._run_phone_guided_job(str(job.id), snapshot, ownership_epoch=3)
    assert "_device_render_v1" not in job.assembly_plan
    session.commit.assert_not_called()
    cloud.assert_not_called()


def test_builder_requires_gate_and_exact_private_sources(monkeypatch):
    _, bindings = fixture()
    binding = bindings[0].model_copy(
        update={"proxy_path": "slot-uploads/analysis-proxy-source.mp4"}
    )
    args = dict(
        user_id=uuid.uuid4(),
        clip_paths=[binding.proxy_path],
        mode="content_plan",
        content_plan_item_id=uuid.uuid4(),
        content_plan_ownership_epoch=3,
    )
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    with pytest.raises(ValueError, match="analysis proxies"):
        build_generative_job(**args)
    job = build_generative_job(**args, phone_sources=(binding,))
    assert job.assembly_plan[PHONE_SOURCES_FIELD][0]["media_id"] == binding.media_id
    with pytest.raises(ValueError, match="exactly"):
        build_generative_job(
            **(args | {"clip_paths": ["slot-uploads/original.mp4"]}), phone_sources=(binding,)
        )
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", False)
    with pytest.raises(ValueError, match="unavailable"):
        build_generative_job(**args, phone_sources=(binding,))


@pytest.mark.parametrize("source", ["receipt", "proxy", "destination"])
@pytest.mark.parametrize(
    "task_name, extra",
    [
        ("regenerate_generative_variant", ["guided_story"]),
        ("rerender_speech_timing", ["operation"]),
        ("reburn_narrated_captions", ["guided_story"]),
        ("rerender_caption_camera_effects", ["guided_story"]),
        ("reburn_narrated_bed_level", ["guided_story", 0.5]),
        ("retranscribe_subtitled_captions", ["guided_story", "en"]),
        ("rebuild_slide_post_variant", []),
    ],
)
def test_phone_jobs_never_enter_cloud_reprocessing_tasks(monkeypatch, source, task_name, extra):
    job, _, session, _, _ = setup(monkeypatch)
    if source == "proxy":
        job.assembly_plan = {}
        job.all_candidates = {"clip_paths": ["slot-uploads/analysis-proxy-source.mp4"]}
    elif source == "destination":
        job.assembly_plan = {"variants": [{"render_destination": "device"}]}
    before = copy.deepcopy(vars(job))
    claim = Mock(side_effect=AssertionError("cloud task entered its render body"))
    trace = Mock(side_effect=AssertionError("cloud task entered its render body"))
    monkeypatch.setattr(gb, "_claim_creator_craft_generation", claim)
    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", trace)
    getattr(gb, task_name).run(str(job.id), *extra)
    claim.assert_not_called()
    trace.assert_not_called()
    session.commit.assert_not_called()
    assert vars(job) == before


def test_cloud_task_fence_keeps_normal_cloud_jobs_accepted(monkeypatch):
    job, _, _, _, _ = setup(monkeypatch)
    job.assembly_plan = {}
    job.all_candidates = {"clip_paths": ["slot-uploads/source.mp4"]}
    with gb._owned_job_task_fence(str(job.id)) as accepted:
        assert accepted


def test_initial_orchestrator_still_enters_phone_planning(monkeypatch):
    from contextlib import nullcontext

    job, _, _, _, cloud = setup(monkeypatch)
    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", lambda _: nullcontext())
    monkeypatch.setattr(gb, "job_heartbeat", lambda _: nullcontext())
    monkeypatch.setattr(gb, "mark_finished", Mock())
    gb.orchestrate_generative_job.run(str(job.id))
    assert device_status(job, "guided_story").phase == "awaiting_device"
    cloud.assert_not_called()
