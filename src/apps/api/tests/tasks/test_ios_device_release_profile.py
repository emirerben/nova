"""Plan 025 A5: production-shaped discovery → planning → dispatch → worker.

Failure model: a generic picker can offer a format while a particular media
shape is refused; a planner can accept that shape but dispatch may mint a Job
that its worker cannot compile. Keep those two contracts separate and exercise
real gates under the SAME flags. External analysis/storage is replaced by the
existing worker fixtures; admission, dispatch and recipe compilers stay real.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.config import settings
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.routes import generative_jobs
from app.routes.creation_threads import capabilities as picker_capabilities
from app.services import creator_capabilities
from app.services.device_render import device_status
from app.services.phone_rollout import phone_subtitled_editor_lanes_supported
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile
from tests.pipeline.test_phone_guided_plan import narration_bed
from tests.tasks.test_content_plan_build import (
    _dispatch_guided_voiceover_cleanup,
    _run_phone_dispatch,
)
from tests.tasks.test_phone_guided_dispatch import narration_setup as guided_narration_setup
from tests.tasks.test_phone_guided_dispatch import setup as guided_setup
from tests.tasks.test_phone_guided_narration_prod_replay import (
    _guided_voiceover_strategy,
    _phone_manifest,
)
from tests.tasks.test_phone_montage_dispatch import setup as voiceover_setup
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _setup_narrated,
    _setup_subtitled,
    _setup_talking_head,
)
from tests.tasks.test_unified_montage_dispatch import (
    _brief as unified_brief,
)
from tests.tasks.test_unified_montage_dispatch import (
    _fixture as unified_fixture,
)
from tests.tasks.test_unified_montage_dispatch import (
    harness as unified_harness,
)


def _release_profile(monkeypatch, *, editor: bool, talking: bool) -> None:
    apply_prod_profile(monkeypatch)
    # Effective API settings read on 2026-10-02: all phone format gates ON,
    # protocol floor 2; admission still hybrid and cloud execution still ON.
    # Qualification deliberately tests the target admission/execution boundary.
    for name in (
        "ios_device_only_mode",
        "phone_subtitled_rendering_enabled",
        "phone_narrated_rendering_enabled",
        "phone_narration_rendering_enabled",
        "phone_guided_narration_rendering_enabled",
        "narrated_archetype_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "cloud_render_execution_enabled", False)
    monkeypatch.setattr(settings, "kria_minimum_client_protocol", 2)
    monkeypatch.setattr(settings, "phone_subtitled_editor_lanes_enabled", editor)
    monkeypatch.setattr(settings, "phone_talking_head_rendering_enabled", talking)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_user_ids", [])


# format, shape, clips, voiceover. Aliases share a picker entry but each must
# independently survive the planner/dispatch/worker path.
CASES = [
    ("montage", "guided", 2, False),
    ("day_vlog", "guided", 2, False),
    ("single_hero", "guided", 2, False),
    ("montage", "voiceover", 2, True),
    ("subtitled", "subtitled", 1, False),
    ("narrated", "recorded", 3, True),
    ("narrated_planned", "recorded", 3, True),
    ("narrated_ready", "recorded", 3, True),
    ("narrated_ready", "self_single", 1, False),
    ("narrated_ready", "self_multi", 3, False),
    ("subtitled", "wrong_clip_count", 2, False),
    ("subtitled", "wrong_audio", 1, True),
    ("talking_head", "unsupported", 3, False),
    ("slides", "unsupported", 1, False),
]


@pytest.mark.parametrize("editor", [False, True], ids=["editor-off", "editor-on"])
@pytest.mark.parametrize("talking", [False, True], ids=["talking-off", "talking-on"])
@pytest.mark.parametrize("fmt,shape,count,voiceover", CASES)
@pytest.mark.asyncio
async def test_release_profile_agrees_through_device_recipe(
    monkeypatch, editor, talking, fmt, shape, count, voiceover
):
    _release_profile(monkeypatch, editor=editor, talking=talking)
    supported = shape not in {"unsupported", "wrong_clip_count", "wrong_audio"} and (
        shape != "self_multi" or talking
    )
    picker = await picker_capabilities(SimpleNamespace(id=uuid.uuid4()), native_client=True)
    offered = {entry["edit_format"] for entry in picker["formats"]}
    assert offered == {"montage", "subtitled", "narrated_planned"}
    public_format = (
        "montage"
        if fmt in {"day_vlog", "single_hero"}
        else "narrated_planned"
        if fmt.startswith("narrated")
        else fmt
    )
    assert (public_format in offered) is (shape != "unsupported")
    # Discovery is intentionally independent of a future clip count/voiceover.
    media = [{"media_id": f"c{i}", "kind": "video"} for i in range(count)]
    manifest = creator_capabilities.resolve_creator_manifest(
        item_id="release-qualification",
        edit_format=fmt,
        media=media,
        phone_source_media_ids=[m["media_id"] for m in media],
        phone_rendering_allowed=True,
        has_voiceover=voiceover,
    )
    available = manifest.capabilities[f"phone_format:{fmt}"]
    assert available.available is supported
    if supported:
        plan = creator_capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(edit_format=fmt, selected_media_ids=[m["media_id"] for m in media]),
        )
        assert plan.strategy.render_program in {"native", "guided"}
    else:
        assert available.reason_code
        assert available.reason

    with patch("app.tasks.content_plan_build.log") as log:
        result, _, build, bind = _run_phone_dispatch(
            monkeypatch,
            edit_format=fmt,
            approved=shape == "guided",
            voiceover=voiceover,
            clip_count=count,
            phone_narration_rendering_enabled=True,
            phone_render_verified_features=list(PROD_VERIFIED_FEATURES),
            phone_subtitled_media_lanes_enabled=True,
        )
    assert result.outcome == ("dispatched" if supported else "invalid_clips")
    if not supported:
        build.assert_not_called()
        bind.assert_not_called()
        assert log.warning.call_args.kwargs["phone_gate"]
        return
    build.assert_called_once()
    bind.assert_called_once()

    # These fixture builders replace external analysis and database ownership,
    # and some install minimal flags. Restore the audited profile AFTER setup.
    if shape == "guided":
        job, _, _, _, cloud = guided_setup(monkeypatch)
        variant = "guided_story"
    elif shape == "voiceover":
        job, _, _, _, cloud = voiceover_setup(monkeypatch, edit_format=fmt)
        variant = "voiceover_only"
    elif shape == "recorded":
        job, _, _, _ = _setup_narrated(monkeypatch, edit_format=fmt)
        cloud = None
        variant = "narrated"
    elif shape == "self_multi":
        job, _, _, _ = _setup_talking_head(monkeypatch)
        cloud = None
        variant = "subtitled"
    else:
        job, _, _, _ = _setup_subtitled(monkeypatch, edit_format=fmt)
        if shape == "self_single":
            # Resolve known speech metadata without probing synthetic /tmp media.
            monkeypatch.setattr(gb, "_resolve_archetype", lambda *a, **k: ("subtitled", "c0", None))
        cloud = None
        variant = "subtitled"
    _release_profile(monkeypatch, editor=editor, talking=talking)
    assert phone_subtitled_editor_lanes_supported() is editor
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    request = device_status(job, variant).request
    assert set(request.recipe.required_capabilities) <= set(PROD_VERIFIED_FEATURES)
    assert all(v["render_destination"] == "device" for v in job.assembly_plan["variants"])
    if cloud:
        cloud.assert_not_called()


@pytest.mark.parametrize("editor", [False, True], ids=["editor-off", "editor-on"])
def test_release_profile_clamps_real_phone_caption_capabilities(monkeypatch, editor):
    """The release switch must alter the actual response clamp, not just its helper.

    The capability map mirrors the three caption fields `_editor_capabilities`
    receives from the real base map. Keeping the clamp call here makes this a
    direct assertion of the route contract returned to the iOS editor.
    """
    _release_profile(monkeypatch, editor=editor, talking=True)
    capabilities = {
        "caption_cues": {"editable": True, "reason": None},
        "caption_meta": {"editable": True, "reason": None},
        "caption_editor_style": True,
    }

    clamped = generative_jobs._clamp_phone_editor_capabilities(
        capabilities, subtitled_lanes=phone_subtitled_editor_lanes_supported()
    )

    if editor:
        assert clamped == capabilities
    else:
        assert clamped["caption_cues"] == {
            "editable": False,
            "reason": "phone_edit_unsupported",
        }
        assert clamped["caption_meta"] == {
            "editable": False,
            "reason": "phone_edit_unsupported",
        }
        assert clamped["caption_editor_style"] is False


def test_release_profile_guided_recorded_voiceover_reaches_phone_worker(monkeypatch):
    """Use the production-shaped guided voiceover fixtures at all four gates.

    The deterministic dispatch/worker fixtures stub persistence, analysis and
    storage only; capability selection, dispatch gating, recipe compilation,
    and device handoff stay production code.
    """
    _release_profile(monkeypatch, editor=True, talking=True)

    manifest = _phone_manifest()
    guided = manifest.capabilities[creator_capabilities.CAPABILITY_GUIDED_VOICEOVER]
    assert guided.available is True
    strategy = _guided_voiceover_strategy()
    planned = creator_capabilities.compile_strategy_to_plan(manifest, strategy)
    assert planned.strategy.render_program == "guided"
    assert planned.strategy.execution_contract == strategy.execution_contract

    dispatched = _dispatch_guided_voiceover_cleanup(
        monkeypatch,
        cleanup_choice="keep_original",
        narration_for=lambda *_args: {},
        before_dispatch=lambda: _release_profile(monkeypatch, editor=True, talking=True),
    )
    assert dispatched.result.outcome == "dispatched"
    dispatched.bind_mock.assert_called_once()
    dispatched.mock_build.assert_called_once()
    dispatched_job = dispatched.session.add.call_args.args[0]
    assert (
        dispatched_job.assembly_plan["guided_edit"]["execution_contract"]
        == strategy.execution_contract
    )

    # `narration_setup` installs a deliberately minimal phone-feature list.
    # Reapply the audited profile before exercising the production worker.
    job, _snapshot, _session, _planner, cloud, plan = guided_narration_setup(monkeypatch)
    _release_profile(monkeypatch, editor=True, talking=True)
    monkeypatch.setattr(
        gb,
        "_resolve_phone_voiceover_bed",
        lambda *_args, **_kwargs: narration_bed(duration_s=3.0),
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    request = device_status(job, "guided_story").request
    assert request.recipe.audio.narration_asset_id is not None
    assert "narrationAudio" in request.recipe.required_capabilities
    assert set(request.recipe.required_capabilities) <= set(PROD_VERIFIED_FEATURES)
    cloud.assert_not_called()


def test_release_profile_runtime_v2_unified_montage_reaches_phone_worker(monkeypatch):
    """The normal runtime-v2 montage admission path must reach its real worker.

    The unified harness replaces clip analysis and persistence, while its
    production worker still plans the brief, compiles the device recipe, and
    records the immutable device request.
    """
    _release_profile(monkeypatch, editor=True, talking=True)
    bindings, _assignments = unified_fixture()
    media = [{"media_id": binding.media_id, "kind": "video"} for binding in bindings]
    manifest = creator_capabilities.resolve_creator_manifest(
        item_id="runtime-v2-unified-montage",
        edit_format="montage",
        media=media,
        phone_source_media_ids=[binding.media_id for binding in bindings],
        phone_rendering_allowed=True,
        has_voiceover=False,
    )
    planned = creator_capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            selected_media_ids=[binding.media_id for binding in bindings],
        ),
    )
    assert planned.strategy.render_program in {"native", "guided"}

    result, dispatched_job, build, bind = _run_phone_dispatch(
        monkeypatch,
        edit_format="montage",
        approved=False,
        phone_render_verified_features=list(PROD_VERIFIED_FEATURES),
        dispatch_kwargs={
            "bypass_guided_edit_gate": True,
            "allow_phone_unapproved_montage": True,
        },
    )
    assert result.outcome == "dispatched"
    bind.assert_called_once()
    build.assert_called_once()
    assert "guided_edit" not in dispatched_job.assembly_plan

    # The worker fixture deliberately narrows verified features; restore the
    # audited release profile before executing its real unified montage path.
    build_worker = unified_harness.__wrapped__(monkeypatch)
    job, _snapshot, _session, _bindings, plain = build_worker(brief=unified_brief())
    _release_profile(monkeypatch, editor=True, talking=True)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert "unified_montage" in job.assembly_plan
    assert device_status(job, "guided_story").request.identity.variant_id == "guided_story"
    assert all(v["render_destination"] == "device" for v in job.assembly_plan["variants"])
    plain.assert_not_called()


def test_release_profile_guided_voiceover_worker_refuses_before_cloud_fallback(monkeypatch):
    """A stale rollout setting must fail the phone worker without cloud work."""
    job, snapshot, session, _planner, cloud, _plan = guided_narration_setup(monkeypatch)
    _release_profile(monkeypatch, editor=True, talking=True)
    monkeypatch.setattr(settings, "phone_guided_narration_rendering_enabled", False)

    with pytest.raises(UnsupportedPhonePlan, match="guided-story narration"):
        gb._run_phone_guided_job(str(job.id), snapshot, ownership_epoch=3)

    assert job.status == "queued"
    session.commit.assert_not_called()
    cloud.assert_not_called()
