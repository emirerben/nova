import copy
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.kria.render_assets import VisualRenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.services import phone_visuals
from app.services.device_render import device_status
from app.services.generative_jobs import build_generative_job
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from app.services.pool_asset_refs import job_references_pool_asset
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_guided_plan import fixture, narration_bed, narration_track
from tests.services import test_phone_visuals as photos


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


@pytest.mark.parametrize(
    "race", ["cancel", "owner", "generation", "binding", "approval", "visuals"]
)
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
        elif race == "visuals":
            # Another run pinned photo receipts this run did not compute.
            job.assembly_plan[PHONE_VISUALS_FIELD] = [photos.photo_visual().model_dump(mode="json")]
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


def test_dispatcher_rejects_phone_snapshot_without_a_registered_renderer(monkeypatch):
    # KRI-114 P0-5/P1-2: the dispatcher fork is archetype-agnostic — a snapshot
    # that is neither a guided-story plan nor a format with a registered phone
    # compiler (PHONE_RENDER_SUPPORTED_FORMATS) must fail loudly instead of
    # silently entering a renderer or a cloud fallback. "subtitled" has no
    # phone compiler yet, so it stays a deliberate negative fixture here.
    from contextlib import nullcontext

    job, _, session, planner, cloud = setup(monkeypatch)
    del job.assembly_plan["guided_edit"]
    job.all_candidates = {"edit_format": "subtitled"}
    phone_runner = Mock()
    montage_runner = Mock()
    monkeypatch.setattr(gb, "_run_phone_guided_job", phone_runner)
    monkeypatch.setattr(gb, "_run_phone_montage_job", montage_runner)
    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", lambda _: nullcontext())
    monkeypatch.setattr(gb, "job_heartbeat", lambda _: nullcontext())
    monkeypatch.setattr(gb, "mark_finished", Mock())
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    fail_job = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail_job)
    # The P0-1 failure-reason wrapper terminalizes the job instead of letting
    # the ValueError escape the task.
    gb.orchestrate_generative_job.run(str(job.id))
    fail_job.assert_called_once()
    args, kwargs = fail_job.call_args
    assert "No phone renderer is registered" in args[1]
    assert kwargs.get("failure_reason") == "phone_plan_unsupported"
    montage_runner.assert_not_called()
    phone_runner.assert_not_called()
    montage_runner.assert_not_called()
    planner.assert_not_called()


@pytest.mark.parametrize(
    "raised, expected_reason",
    [
        pytest.param(
            lambda: UnsupportedPhonePlan("phone looks require exact-canvas unrotated sources"),
            "phone_plan_unsupported",
            id="unsupported_phone_plan",
        ),
        pytest.param(
            lambda: ValueError("Phone rendering requires original source bindings"),
            "phone_plan_unsupported",
            id="pilot_validation_value_error",
        ),
        pytest.param(lambda: RuntimeError("boom"), "phone_plan_failed", id="unexpected_error"),
    ],
)
def test_phone_plan_failure_persists_failure_reason(monkeypatch, raised, expected_reason):
    from contextlib import nullcontext

    job, _, _, planner, cloud = setup(monkeypatch)
    planner.side_effect = raised()
    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", lambda _: nullcontext())
    monkeypatch.setattr(gb, "job_heartbeat", lambda _: nullcontext())
    monkeypatch.setattr(gb, "mark_finished", Mock())
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    fail_job = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail_job)
    gb.orchestrate_generative_job.run(str(job.id))
    fail_job.assert_called_once()
    _args, kwargs = fail_job.call_args
    assert kwargs.get("failure_reason") == expected_reason
    cloud.assert_not_called()


def pool_setup(monkeypatch, *, moments, rows, features=()):
    """A guided phone job whose approved story shows Visuals-pool media."""
    job, snapshot, session, planner, cloud = setup(monkeypatch)
    plan, _ = photos.pool_plan(*moments)
    planner.return_value = (plan.model_dump(mode="json"), None)
    job.user_id = photos.USER_ID
    job.content_plan_item_id = photos.ITEM_ID
    session.get.return_value = job
    session.execute.return_value.scalars.return_value.all.return_value = rows
    downloads = photos.fake_storage(
        monkeypatch,
        {
            (photos.PHOTO_PATH, photos.PHOTO_GENERATION): photos.PHOTO_BYTES,
            (photos.VIDEO_PATH, photos.VIDEO_GENERATION): photos.VIDEO_BYTES,
        },
    )
    photos.fake_probe(monkeypatch)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, *features],
    )
    return job, snapshot, cloud, downloads


def photo_setup(monkeypatch, *, still_images, row=None):
    return pool_setup(
        monkeypatch,
        moments=[photos.photo_moment()],
        rows=[row or photos.photo_row()],
        features=["stillImages"] if still_images else [],
    )


def spy_on_binding(monkeypatch):
    """Record the kinds the worker asks the binder for."""
    real = phone_visuals.bind_phone_visuals
    asked = []

    def bind(open_session, **kwargs):
        asked.append(kwargs["kinds"])
        return real(open_session, **kwargs)

    monkeypatch.setattr(phone_visuals, "bind_phone_visuals", bind)
    return asked


def run_until_failure(monkeypatch, job):
    from contextlib import nullcontext

    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", lambda _: nullcontext())
    monkeypatch.setattr(gb, "job_heartbeat", lambda _: nullcontext())
    monkeypatch.setattr(gb, "mark_finished", Mock())
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    fail_job = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail_job)
    gb.orchestrate_generative_job.run(str(job.id))
    fail_job.assert_called_once()
    return fail_job.call_args


def test_photo_without_verified_still_images_fails_exactly_as_before(monkeypatch):
    job, _, cloud, downloads = photo_setup(monkeypatch, still_images=False)
    args, kwargs = run_until_failure(monkeypatch, job)
    assert kwargs.get("failure_reason") == "phone_plan_unsupported"
    assert "unsupported phone photo" in args[1]
    assert downloads == []
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
    assert "_device_render_v1" not in job.assembly_plan
    cloud.assert_not_called()


def test_verified_still_images_pin_the_photo_bytes_into_the_device_recipe(monkeypatch):
    job, _, cloud, downloads = photo_setup(monkeypatch, still_images=True)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    request = device_status(job, "guided_story").request
    visual = photos.photo_visual()
    [asset] = [a for a in request.recipe.asset_manifest.assets if a.kind == "visual"]
    assert asset == VisualRenderAsset(
        id=f"visual-{photos.PHOTO_ID}",
        visual_id=photos.PHOTO_ID,
        generation=photos.PHOTO_GENERATION,
        fingerprint={"sha256": visual.sha256, "byte_count": visual.byte_count},
    )
    assert "stillImages" in request.recipe.required_capabilities
    still = request.recipe.tracks[0].clips[-1]
    assert (still.source_asset_id, still.source_start, still.timeline_start) == (asset.id, 0, 3)
    # Recipes carry identities only; the storage path stays in private job state.
    assert photos.PHOTO_PATH not in request.model_dump_json()
    rows = job.assembly_plan[PHONE_VISUALS_FIELD]
    assert rows == [visual.model_dump(mode="json")]
    assert job_references_pool_asset(
        SimpleNamespace(raw_storage_path=None, assembly_plan={PHONE_VISUALS_FIELD: rows}),
        asset_id="unrelated",
        gcs_path=photos.PHOTO_PATH,
    )
    assert downloads == [(photos.PHOTO_PATH, photos.PHOTO_GENERATION)]
    cloud.assert_not_called()


def test_verified_still_images_leave_video_only_stories_unchanged(monkeypatch):
    job, _, _, _, cloud = setup(monkeypatch)
    downloads = photos.fake_storage(monkeypatch, {})
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "stillImages"],
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
    assert downloads == []
    cloud.assert_not_called()


@pytest.mark.parametrize("change", [{"status": "failed"}, {"gcs_generation": "78"}])
def test_stale_pool_photo_fails_closed_as_unsupported(monkeypatch, change):
    job, _, cloud, downloads = photo_setup(
        monkeypatch, still_images=True, row=photos.photo_row(**change)
    )
    _, kwargs = run_until_failure(monkeypatch, job)
    assert kwargs.get("failure_reason") == "phone_plan_unsupported"
    assert downloads == []
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
    cloud.assert_not_called()


PHOTO_PIN = (photos.PHOTO_PATH, photos.PHOTO_GENERATION)
VIDEO_PIN = (photos.VIDEO_PATH, photos.VIDEO_GENERATION)


def mixed_setup(monkeypatch, features):
    """The bound clip, then a pool photo, then a pool video."""
    return pool_setup(
        monkeypatch,
        moments=[photos.photo_moment(), photos.video_moment()],
        rows=[photos.photo_row(), photos.video_row()],
        features=features,
    )


@pytest.mark.parametrize(
    "features, asked_kinds, bound, message",
    [
        ([], [], [], "unsupported phone photo"),
        (["stillImages"], [{"image"}], [PHOTO_PIN], "unsupported phone visual video"),
        (["visualVideos"], [{"video"}], [VIDEO_PIN], "unsupported phone photo"),
    ],
    ids=["neither", "still_images_only", "visual_videos_only"],
)
def test_each_pool_kind_binds_only_while_its_feature_is_verified(
    monkeypatch, features, asked_kinds, bound, message
):
    job, _, cloud, downloads = mixed_setup(monkeypatch, features)
    asked = spy_on_binding(monkeypatch)
    args, kwargs = run_until_failure(monkeypatch, job)
    # The unverified kind stays unbound, so the compiler fails closed on it;
    # with neither verified the binder is never even called.
    assert asked == [frozenset(kinds) for kinds in asked_kinds]
    assert downloads == bound
    assert kwargs.get("failure_reason") == "phone_plan_unsupported"
    assert message in args[1]
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
    assert "_device_render_v1" not in job.assembly_plan
    cloud.assert_not_called()


def test_both_verified_features_pin_photos_and_videos_in_one_recipe(monkeypatch):
    job, _, cloud, downloads = mixed_setup(monkeypatch, ["stillImages", "visualVideos"])
    asked = spy_on_binding(monkeypatch)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    assert asked == [frozenset({"image", "video"})]
    assert downloads == [PHOTO_PIN, VIDEO_PIN]
    request = device_status(job, "guided_story").request
    kinds = [a.media_kind for a in request.recipe.asset_manifest.assets if a.kind == "visual"]
    assert sorted(kinds) == ["image", "video"]
    assert {"stillImages", "visualVideos"} <= set(request.recipe.required_capabilities)
    assert job.assembly_plan[PHONE_VISUALS_FIELD] == [
        photos.photo_visual().model_dump(mode="json"),
        photos.video_visual().model_dump(mode="json"),
    ]
    cloud.assert_not_called()


def test_verified_visual_videos_pin_the_pool_video_into_the_device_recipe(monkeypatch):
    job, _, cloud, downloads = pool_setup(
        monkeypatch,
        moments=[photos.video_moment()],
        rows=[photos.video_row()],
        features=["visualVideos"],
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    request = device_status(job, "guided_story").request
    visual = photos.video_visual()
    [asset] = [a for a in request.recipe.asset_manifest.assets if a.kind == "visual"]
    assert asset == VisualRenderAsset(
        id=f"visual-{photos.VIDEO_ID}",
        visual_id=photos.VIDEO_ID,
        generation=photos.VIDEO_GENERATION,
        media_kind="video",
        fingerprint={"sha256": visual.sha256, "byte_count": visual.byte_count},
    )
    assert '"media_kind":"video"' in request.model_dump_json()
    assert "visualVideos" in request.recipe.required_capabilities
    assert "stillImages" not in request.recipe.required_capabilities
    clip = request.recipe.tracks[0].clips[-1]
    assert (clip.source_asset_id, clip.source_start, clip.source_duration, clip.timeline_start) == (
        asset.id,
        1,
        2,
        3,
    )
    # Recipes carry identities only; the storage path stays in private job state.
    assert photos.VIDEO_PATH not in request.model_dump_json()
    rows = job.assembly_plan[PHONE_VISUALS_FIELD]
    assert rows == [visual.model_dump(mode="json")]
    assert rows[0]["kind"] == "video"
    assert job_references_pool_asset(
        SimpleNamespace(raw_storage_path=None, assembly_plan={PHONE_VISUALS_FIELD: rows}),
        asset_id="unrelated",
        gcs_path=photos.VIDEO_PATH,
    )
    assert downloads == [VIDEO_PIN]
    cloud.assert_not_called()


@pytest.mark.parametrize(
    "change", [{"status": "failed"}, {"kind": "image"}], ids=["status", "kind"]
)
def test_stale_pool_video_fails_closed_as_unsupported(monkeypatch, change):
    job, _, cloud, downloads = pool_setup(
        monkeypatch,
        moments=[photos.video_moment()],
        rows=[photos.video_row(**change)],
        features=["visualVideos"],
    )
    _, kwargs = run_until_failure(monkeypatch, job)
    assert kwargs.get("failure_reason") == "phone_plan_unsupported"
    assert downloads == []
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
    cloud.assert_not_called()


# --- KRI-132 phone-voiceover-gate follow-up: guided-story narration lane ----
#
# The compiler's own stills+narration shape (2 video + 2 Visuals-photo
# moments tiling the canonical narration duration) is covered exhaustively in
# tests/pipeline/test_phone_guided_plan.py; these tests stay on the plain
# `fixture()` base (no Visuals pool moments, so `bind_phone_visuals` never
# even touches the session) and focus on the WORKER's own bed-resolution
# plumbing: does it call `_resolve_phone_voiceover_bed` with the right
# arguments, does a stale/missing bed fail closed, and does the rollout flag
# gate it before a bed is ever resolved.


def narration_setup(monkeypatch, *, flag: bool = True):
    """A guided phone job whose approved plan carries a recorded voiceover."""
    job, snapshot, session, planner, cloud = setup(monkeypatch)
    plan, _bindings = fixture()
    plan.narration = narration_track(duration_s=3.0)
    planner.return_value = (plan.model_dump(mode="json"), None)
    monkeypatch.setattr(gb.settings, "phone_guided_narration_rendering_enabled", flag)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix", "narrationAudio"],
    )
    return job, snapshot, session, planner, cloud, plan


def test_narration_plan_resolves_and_pins_the_bed(monkeypatch):
    job, snapshot, session, planner, cloud, plan = narration_setup(monkeypatch)
    bed = narration_bed(duration_s=3.0)  # matches plan.narration's 3s duration
    calls = []

    def fake_bed(job_id, gcs_path):
        calls.append((job_id, gcs_path))
        return bed

    monkeypatch.setattr(gb, "_resolve_phone_voiceover_bed", fake_bed, raising=False)

    gb._run_generative_job(str(job.id))

    assert calls == [(str(job.id), plan.narration.gcs_path)]
    assert job.status == "awaiting_device"
    request = device_status(job, "guided_story").request
    voice_asset_id = f"voiceover-{bed.plan_item_id}"
    assert request.recipe.audio.narration_asset_id == voice_asset_id
    assert request.recipe.audio.original_volume == 0
    [narration_track_recipe] = [t for t in request.recipe.tracks if t.kind == "audio"]
    assert narration_track_recipe.clips[0].source_asset_id == voice_asset_id
    cloud.assert_not_called()


def test_narration_bed_generation_mismatch_fails_closed(monkeypatch):
    job, snapshot, session, planner, cloud, plan = narration_setup(monkeypatch)
    stale_bed = narration_bed(generation="stale-generation")
    monkeypatch.setattr(gb, "_resolve_phone_voiceover_bed", lambda *a: stale_bed, raising=False)

    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval"):
        gb._run_phone_guided_job(str(job.id), snapshot, ownership_epoch=3)
    session.commit.assert_not_called()
    cloud.assert_not_called()


def test_narration_bed_missing_fails_closed(monkeypatch):
    job, snapshot, session, planner, cloud, plan = narration_setup(monkeypatch)
    monkeypatch.setattr(gb, "_resolve_phone_voiceover_bed", lambda *a: None, raising=False)

    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval"):
        gb._run_phone_guided_job(str(job.id), snapshot, ownership_epoch=3)
    session.commit.assert_not_called()
    cloud.assert_not_called()


def test_narration_flag_off_fails_closed_before_resolving_a_bed(monkeypatch):
    job, snapshot, session, planner, cloud, plan = narration_setup(monkeypatch, flag=False)
    bed_calls = []
    monkeypatch.setattr(
        gb,
        "_resolve_phone_voiceover_bed",
        lambda *a: (bed_calls.append(a), narration_bed())[1],
        raising=False,
    )

    with pytest.raises(UnsupportedPhonePlan, match="guided-story narration"):
        gb._run_phone_guided_job(str(job.id), snapshot, ownership_epoch=3)
    assert bed_calls == []  # never even attempted to resolve one
    session.commit.assert_not_called()
    cloud.assert_not_called()


def test_plan_without_narration_never_looks_up_a_bed(monkeypatch):
    """Byte-identical to before this change: a plan with no `.narration` never
    calls `_resolve_phone_voiceover_bed` at all."""
    job, snapshot, session, planner, cloud = setup(monkeypatch)  # base fixture, no narration
    bed_calls = []
    monkeypatch.setattr(
        gb,
        "_resolve_phone_voiceover_bed",
        lambda *a: (bed_calls.append(a), narration_bed())[1],
        raising=False,
    )

    gb._run_generative_job(str(job.id))

    assert bed_calls == []
    assert job.status == "awaiting_device"
    request = device_status(job, "guided_story").request
    assert request.recipe.audio.narration_asset_id is None
    cloud.assert_not_called()
