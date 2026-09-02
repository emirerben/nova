"""Focused Main Creator route/controller contracts."""

import copy
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import MissingGreenlet
from starlette.requests import Request

from app.agents._schemas.creator_agent import (
    AskUser,
    CreativeStrategy,
    CreatorCraftBundle,
    CreatorMediaRef,
    ProposeStrategy,
    canonical_context_hash,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.auth import get_current_user
from app.config import Settings, settings
from app.database import get_db
from app.limiter import limiter
from app.main import app
from app.models import CreatorAgentExecution, CreatorAgentSession, Job
from app.routes import creator_agent as creator_routes
from app.routes import plan_items as plan_item_routes
from app.routes.creator_agent import (
    AutoIterationBody,
    ConfirmBody,
    StartBody,
    TurnBody,
    _apply_plan_intent,
    _auto_iteration_already_finalized,
    _balanced_integer_duration_s,
    _confirmed_creator_request,
    _creator_speech_cut_source_enabled,
    _fallback_strategy,
    _next_balanced_integer_duration_s,
    _reset_render_target,
    _resolved_cadence_for_turn,
    _seed_guided_specialist_brief,
    _selected_cadence_sources,
    _strict_creator_format,
)
from app.schemas.edit_proposal import (
    EditProposal,
    MixedMediaTimingProfile,
    MontageAudioPlan,
    MontageCadenceConstraint,
    ProposalBrief,
    recognize_cadence_reuse_policy,
    recognize_round_robin_cadence,
    recognize_total_duration_s,
    rejects_round_robin_cadence,
)
from app.services.creator_capabilities import (
    compile_strategy_to_plan,
    resolve_creator_manifest,
)


@pytest.fixture(autouse=True)
def _stub_creator_clip_metadata_dispatch(monkeypatch) -> None:
    from app.tasks.creator_clip_metadata import analyze_creator_clip_metadata

    monkeypatch.setattr(analyze_creator_clip_metadata, "apply_async", MagicMock())


def _craft_bundle(
    *,
    session_id: uuid.UUID,
    job_id: uuid.UUID,
    generation_id: str,
    idempotency_key: str = "craft-1",
) -> CreatorCraftBundle:
    pins = {
        "expected_manifest_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "expected_job_id": str(job_id),
        "expected_variant_id": "variant-1",
        "expected_generation_id": generation_id,
        "expected_revision": 3,
        "expected_ownership_epoch": 4,
    }
    return CreatorCraftBundle(
        session_id=str(session_id),
        idempotency_key=idempotency_key,
        commands=[{**pins, "command": "set_caption_style", "caption_style": "word"}],
        **pins,
    )


def test_creator_clip_metadata_dispatch_uses_analysis_queue() -> None:
    from app.tasks.creator_clip_metadata import (
        CREATOR_CLIP_METADATA_QUEUE,
        analyze_creator_clip_metadata,
    )

    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=5)

    creator_routes._enqueue_creator_clip_metadata(item, plan)

    analyze_creator_clip_metadata.apply_async.assert_called_once_with(
        args=[str(item.id), 5], queue=CREATOR_CLIP_METADATA_QUEUE
    )


@pytest.fixture()
def client() -> TestClient:
    user = SimpleNamespace(id=uuid.uuid4())

    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value
    app.dependency_overrides.clear()
    settings.main_creator_agent_enabled = False
    settings.main_creator_agent_rollout_percent = 0


def _manifest(monkeypatch, *, has_voiceover: bool = False):
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(creator_capabilities.settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(creator_capabilities.settings, "main_creator_agent_rollout_percent", 100)
    return resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=has_voiceover,
        media=[{"media_id": "clip-1", "kind": "video"}],
    )


def test_creator_speech_cut_uses_candidate_specific_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr(settings, "silence_cut_enabled", False)
    monkeypatch.setattr(settings, "retake_cut_enabled", True)
    assert _creator_speech_cut_source_enabled("retake_review") is True
    assert _creator_speech_cut_source_enabled("silence_review") is False

    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(settings, "retake_cut_enabled", False)
    assert _creator_speech_cut_source_enabled("retake_review") is False
    assert _creator_speech_cut_source_enabled("filler_review") is True
    assert _creator_speech_cut_source_enabled("untrusted_source") is False


def test_required_creator_speech_dispatch_keeps_last_good_public(monkeypatch) -> None:
    from app.agents._schemas.creator_agent import ApplySpeechCutCommand
    from app.pipeline.speech_cut_state import cut_revision, make_candidate

    candidate = make_candidate(
        start_s=1.0,
        end_s=1.5,
        reason="filler_acoustic",
        source="retake_review",
        preview="um",
        source_fingerprint="source-a",
        transcript_hash="transcript-a",
    )
    job_id = uuid.uuid4()
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
        "base_video_path": f"generative-jobs/{job_id}/base.mp4",
        "speech_cut_candidates": [candidate],
        "speech_cut_forced_removals": [],
        "speech_cuts_disabled": False,
        "silence_cut": {"removed": []},
    }
    variant["speech_cut_revision"] = cut_revision(variant)
    public_before = copy.deepcopy(variant)
    job = SimpleNamespace(
        id=job_id,
        status="variants_ready",
        assembly_plan={
            "speech_cleanup_contract": "required_v1",
            "silence_cut_disabled": True,
            "variants": [variant],
        },
    )
    command = ApplySpeechCutCommand(
        command="apply_speech_cut",
        candidate_id=candidate["candidate_id"],
        expected_cut_revision=variant["speech_cut_revision"],
        expected_manifest_hash="a" * 64,
        expected_context_hash="b" * 64,
        expected_job_id=str(job_id),
        expected_variant_id="subtitled",
        expected_generation_id=variant["render_generation_id"],
        expected_revision=1,
        expected_ownership_epoch=1,
    )
    monkeypatch.setattr(creator_routes.settings, "retake_cut_enabled", True)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_a, **_k: None)

    request, _operation_id, _prior = creator_routes._stage_creator_speech_cut(
        job,
        variant_id="subtitled",
        command=command,
    )

    assert request["operation"] == "apply_speech_cut_candidate"
    assert job.assembly_plan["variants"] == [public_before]
    assert job.assembly_plan["speech_cut_previous_variants"] == [public_before]
    assert job.assembly_plan["silence_cut_disabled"] is True
    control = job.assembly_plan["speech_cut_control"]
    assert control["render_generation_id"]
    assert control["in_flight"]


@pytest.mark.parametrize(
    ("count", "status", "expected"),
    [
        (0, "running", False),
        (1, "running", True),
        (0, "queued", True),
        (0, "complete", True),
    ],
)
def test_auto_iteration_finalization_is_one_cycle_idempotent(
    count: int, status: str, expected: bool
) -> None:
    session = SimpleNamespace(
        automatic_revision_count=count,
        last_review={"auto_iteration": {"status": status}},
    )

    assert _auto_iteration_already_finalized(session) is expected


@pytest.mark.asyncio
async def test_auto_iteration_keeps_ready_phase_until_craft_succeeds(monkeypatch) -> None:
    """The craft gateway must see the ready phase on first dispatch and retry."""

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_feedback",
        revision=11,
        ownership_epoch=7,
        auto_iteration_opt_in=False,
        max_render_attempts=2,
        render_attempts=1,
        automatic_revision_count=0,
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
        last_review={
            "status": "complete",
            "review_mode": "objective",
            "render_generation_id": "generation-1",
            "confidence": 0.9,
            "quality_score": 3.0,
            "expected_improvement": 0.5,
            "objective_tag": "objective_quality",
            "allowlist_action": "caption_legibility",
            "proposed_revision": {"revision_id": "revision-1", "summary": "Fix captions"},
        },
    )
    item = SimpleNamespace(id=item_id)
    plan = SimpleNamespace(ownership_epoch=7)
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-1",
                    "render_status": "ready",
                }
            ]
        },
    )
    manifest = SimpleNamespace(manifest_hash="a" * 64, context_hash="b" * 64)
    db = AsyncMock()
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = None
    db.execute.return_value = query_result
    db.get.return_value = job
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(side_effect=[session, session]))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )

    async def append_event(*_args, **_kwargs):
        session.revision += 1

    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(
        creator_routes,
        "evaluate_auto_iteration",
        lambda *_args, **_kwargs: SimpleNamespace(decision="eligible"),
    )
    captured: dict = {}

    def build_bundle(**kwargs):
        captured["pin"] = kwargs["pin"]
        return SimpleNamespace(model_dump=lambda mode: {"bounded": True})

    monkeypatch.setattr(creator_routes, "build_auto_bundle", build_bundle)

    async def craft(*_args, **_kwargs):
        captured["status_at_craft"] = session.status
        return SimpleNamespace(generation="generation-2", receipt_id="craft-receipt-1")

    monkeypatch.setattr(creator_routes, "execute_creator_craft", craft)
    monkeypatch.setattr(
        creator_routes,
        "_response",
        AsyncMock(return_value=SimpleNamespace(status="rendering")),
    )
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_quality_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_auto_iteration_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    result = await creator_routes.request_creator_auto_iteration(
        str(item_id),
        AutoIterationBody(
            session_id=session_id,
            expected_revision=11,
            opt_in=True,
            client_event_id="auto-event-1",
        ),
        user,
        db,
    )

    assert result.status == "rendering"
    assert captured["status_at_craft"] == "awaiting_feedback"
    assert captured["pin"]["expected_revision"] == 12


def test_strict_creator_formats_never_use_montage_fallback() -> None:
    assert _strict_creator_format("day_vlog") is True
    assert _strict_creator_format("single_hero") is True
    assert _strict_creator_format("montage") is False


def _craft_route_context(*, user_id: uuid.UUID, job_id: uuid.UUID, session_id: uuid.UUID):
    item_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=session_id,
        creator_id=user_id,
        plan_item_id=item_id,
        status="awaiting_feedback",
        revision=3,
        ownership_epoch=4,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=4,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-2",
                    "render_status": "ready",
                }
            ]
        },
    )
    manifest = SimpleNamespace(
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        capabilities={"caption_style": SimpleNamespace(available=True)},
    )
    return item, plan, session, job, manifest


def _required_speech_craft_context(
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    session_id: uuid.UUID,
    with_caption: bool = False,
):
    from app.pipeline.speech_cut_state import cut_revision, make_candidate

    item, plan, session, job, _manifest = _craft_route_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
    )
    candidate = make_candidate(
        start_s=1.0,
        end_s=1.5,
        reason="filler_acoustic",
        source="retake_review",
        preview="um",
        source_fingerprint="source-a",
        transcript_hash="transcript-a",
    )
    variant = {
        "variant_id": "variant-1",
        "resolved_archetype": "subtitled",
        "render_generation_id": "generation-2",
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
        "base_video_path": f"generative-jobs/{job_id}/base.mp4",
        "voiceover_caption_style": "sentence",
        "speech_cut_candidates": [candidate],
        "speech_cut_forced_removals": [],
        "speech_cuts_disabled": False,
        "silence_cut": {"removed": []},
    }
    variant["speech_cut_revision"] = cut_revision(variant)
    job.assembly_plan = {
        "speech_cleanup_contract": "required_v1",
        "silence_cut_disabled": True,
        "variants": [variant],
    }
    manifest = SimpleNamespace(
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        capabilities={
            "automatic_cut": SimpleNamespace(available=True),
            "caption_style": SimpleNamespace(available=True),
        },
    )
    pins = {
        "expected_manifest_hash": manifest.manifest_hash,
        "expected_context_hash": manifest.context_hash,
        "expected_job_id": str(job_id),
        "expected_variant_id": "variant-1",
        "expected_generation_id": "generation-2",
        "expected_revision": 3,
        "expected_ownership_epoch": 4,
    }
    commands = [
        {
            **pins,
            "command": "apply_speech_cut",
            "candidate_id": candidate["candidate_id"],
            "expected_cut_revision": variant["speech_cut_revision"],
        }
    ]
    if with_caption:
        commands.append({**pins, "command": "set_caption_style", "caption_style": "word"})
    body = CreatorCraftBundle(
        session_id=str(session_id),
        idempotency_key=("speech-caption" if with_caption else "speech-only"),
        commands=commands,
        **pins,
    )
    return item, plan, session, job, manifest, body


def _configure_required_speech_craft(
    monkeypatch,
    *,
    item,
    plan,
    session,
    manifest,
) -> None:
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_stable_manifest_fingerprint", lambda _manifest: "stable")
    monkeypatch.setattr(creator_routes.settings, "retake_cut_enabled", True)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args, **_kwargs: None)


def test_creator_speech_cut_rejects_inflight_sibling_before_snapshot(monkeypatch) -> None:
    from app.agents._schemas.creator_agent import ApplySpeechCutCommand

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    _item, _plan, _session, job, _manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=uuid.uuid4(),
    )
    job.assembly_plan["variants"].append(
        {
            "variant_id": "song_text",
            "render_generation_id": uuid.uuid4().hex,
            "render_status": "pending",
            "ok": False,
        }
    )
    before = copy.deepcopy(job.assembly_plan)
    command = next(value for value in body.commands if isinstance(value, ApplySpeechCutCommand))
    monkeypatch.setattr(creator_routes.settings, "retake_cut_enabled", True)

    with pytest.raises(HTTPException) as caught:
        creator_routes._stage_creator_speech_cut(
            job,
            variant_id="variant-1",
            command=command,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "variant_initial_render_in_progress"
    assert job.assembly_plan == before


def test_creator_craft_response_projects_private_state_without_mutating_receipt() -> None:
    import copy

    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        status="succeeded",
        result={
            "generation": "generation-3",
            "preview": {
                "caption_style": "word",
                "_speech_cleanup_internal": {"secret": True},
                "candidate": {
                    "clip_source_instance_ids": ["private-id"],
                    "clip_metadata_identity_index_v2": {"records": []},
                    "clip_paths": ["source.mp4"],
                },
            },
        },
    )
    stored = copy.deepcopy(receipt.result)

    response = creator_routes._craft_response(receipt)

    assert response.preview == {
        "caption_style": "word",
        "candidate": {"clip_paths": ["source.mp4"]},
    }
    assert receipt.result == stored


def test_creator_session_response_projects_nested_private_state(monkeypatch) -> None:
    payload = {
        "id": str(uuid.uuid4()),
        "status": "awaiting_feedback",
        "revision": 3,
        "render_attempts": 1,
        "max_render_attempts": 2,
        "can_render": True,
        "pending_plan": {
            "summary": "Keep this",
            "_speech_cleanup_internal": {"secret": True},
            "candidate": {
                "clip_source_instance_ids": ["private-id"],
                "clip_paths": ["source.mp4"],
            },
        },
        "current_job_id": None,
        "last_review": None,
        "events": [],
        "auto_iteration": None,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }
    monkeypatch.setattr(creator_routes, "serialize_session", lambda _session: payload)

    response = creator_routes._creator_session_response(SimpleNamespace())

    assert response.pending_plan == {
        "summary": "Keep this",
        "candidate": {"clip_paths": ["source.mp4"]},
    }


@pytest.mark.asyncio
async def test_required_creator_speech_only_uses_one_private_generation(monkeypatch) -> None:
    from app.tasks.generative_build import rerender_speech_timing

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
    )
    public_before = copy.deepcopy(job.assembly_plan["variants"])
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    captured: dict[str, CreatorAgentExecution] = {}

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = uuid.uuid4()
            captured["receipt"] = value

    db.add = MagicMock(side_effect=add)
    enqueue = MagicMock()
    monkeypatch.setattr(rerender_speech_timing, "apply_async", enqueue)
    _configure_required_speech_craft(
        monkeypatch,
        item=item,
        plan=plan,
        session=session,
        manifest=manifest,
    )

    response = await creator_routes.execute_creator_craft(
        str(item.id), body, SimpleNamespace(id=user_id), db
    )

    receipt = captured["receipt"]
    control = job.assembly_plan["speech_cut_control"]
    generation = control["render_generation_id"]
    assert response.generation == generation
    assert receipt.result["generation"] == generation
    assert receipt.result["prepared"]["generation"] == generation
    assert receipt.result["speech_cut_operation_id"] == control["operation_id"]
    assert receipt.result["prepared"]["speech_cut_operation_id"] == control["operation_id"]
    assert session.target_generation_id == generation
    assert job.assembly_plan["variants"] == public_before
    assert job.assembly_plan["speech_cut_previous_variants"] == public_before
    staged = job.assembly_plan["speech_cut_previous_variant"]
    assert staged["render_generation_id"] == generation
    assert staged["speech_cut_candidates"][0]["status"] == "applying"
    enqueue.assert_called_once_with(
        args=[str(job_id), control["operation_id"]],
        queue="plan-jobs",
        task_id=f"creator-craft-{receipt.id}-{generation}",
    )


@pytest.mark.asyncio
async def test_required_creator_speech_and_editor_keep_editor_lane_private(monkeypatch) -> None:
    from app.tasks.generative_build import rerender_speech_timing

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
        with_caption=True,
    )
    public_before = copy.deepcopy(job.assembly_plan["variants"])
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    captured: dict[str, CreatorAgentExecution] = {}

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = uuid.uuid4()
            captured["receipt"] = value

    db.add = MagicMock(side_effect=add)

    def prepare_editor(job_value, variant_id, *_args, **_kwargs):
        variants = copy.deepcopy(job_value.assembly_plan["variants"])
        target = next(value for value in variants if value["variant_id"] == variant_id)
        assert target == public_before[0]
        target.update(
            {
                "voiceover_caption_style": "word",
                "render_generation_id": "discarded-editor-generation",
                "render_status": "rendering",
                "ok": False,
            }
        )
        job_value.assembly_plan = {**job_value.assembly_plan, "variants": variants}
        return {
            "generation": "discarded-editor-generation",
            "has_render_section": True,
            "sections": {"caption_meta": True},
        }

    enqueue = MagicMock()
    monkeypatch.setattr(creator_routes, "prepare_editor_commit", prepare_editor)
    monkeypatch.setattr(rerender_speech_timing, "apply_async", enqueue)
    _configure_required_speech_craft(
        monkeypatch,
        item=item,
        plan=plan,
        session=session,
        manifest=manifest,
    )

    response = await creator_routes.execute_creator_craft(
        str(item.id), body, SimpleNamespace(id=user_id), db
    )

    receipt = captured["receipt"]
    control = job.assembly_plan["speech_cut_control"]
    generation = control["render_generation_id"]
    staged = job.assembly_plan["speech_cut_previous_variant"]
    assert response.generation == generation
    assert receipt.result["generation"] == generation
    assert receipt.result["prepared"]["generation"] == generation
    assert session.target_generation_id == generation
    assert staged["render_generation_id"] == generation
    assert staged["voiceover_caption_style"] == "word"
    assert staged["speech_cut_candidates"][0]["status"] == "applying"
    assert job.assembly_plan["variants"] == public_before
    assert job.assembly_plan["speech_cut_previous_variants"] == public_before
    assert "discarded-editor-generation" not in repr(job.assembly_plan)
    enqueue.assert_called_once_with(
        args=[str(job_id), control["operation_id"]],
        queue="plan-jobs",
        task_id=f"creator-craft-{receipt.id}-{generation}",
    )


@pytest.mark.asyncio
async def test_creator_craft_rejects_stale_exact_generation_pin(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id),
            _craft_bundle(
                session_id=session_id,
                job_id=job_id,
                generation_id="generation-1",
            ),
            SimpleNamespace(id=user_id),
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Creator render generation changed"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_craft_rejects_private_initial_generation_before_staging(
    monkeypatch,
) -> None:
    import copy

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    job.assembly_plan["_speech_cleanup_internal"] = {
        "required_speech_generation_locks": {"variant-1": "initial-generation"}
    }
    job.status = "processing"
    stored = copy.deepcopy(job.assembly_plan)
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    resolve_context = AsyncMock(return_value=(manifest, []))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        resolve_context,
    )
    build_commit = MagicMock()
    monkeypatch.setattr(creator_routes, "build_core_craft_editor_commit", build_commit)

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id),
            _craft_bundle(
                session_id=session_id,
                job_id=job_id,
                generation_id="generation-2",
            ),
            SimpleNamespace(id=user_id),
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "variant_initial_render_in_progress"
    assert job.assembly_plan == stored
    resolve_context.assert_not_awaited()
    build_commit.assert_not_called()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_iteration_rejects_private_initial_generation_without_controller_write(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_feedback",
        revision=11,
        ownership_epoch=7,
        auto_iteration_opt_in=False,
        max_render_attempts=2,
        render_attempts=1,
        automatic_revision_count=0,
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
        last_review={
            "status": "complete",
            "review_mode": "objective",
            "render_generation_id": "generation-1",
            "confidence": 0.9,
            "quality_score": 3.0,
            "expected_improvement": 0.5,
            "objective_tag": "objective_quality",
            "allowlist_action": "caption_legibility",
            "proposed_revision": {"revision_id": "revision-1", "summary": "Fix captions"},
        },
    )
    item = SimpleNamespace(id=item_id)
    plan = SimpleNamespace(ownership_epoch=7)
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-1",
                    "render_status": "pending",
                }
            ],
            "_speech_cleanup_internal": {
                "required_speech_generation_locks": {"variant-1": "initial-generation"}
            },
        },
    )
    db = AsyncMock()
    no_row = MagicMock()
    no_row.scalar_one_or_none.return_value = None
    db.execute.return_value = no_row
    db.get.return_value = job
    append_event = AsyncMock()
    resolve_context = AsyncMock()
    craft = AsyncMock()
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "resolve_item_creator_context", resolve_context)
    monkeypatch.setattr(creator_routes, "execute_creator_craft", craft)
    monkeypatch.setattr(
        creator_routes,
        "evaluate_auto_iteration",
        lambda *_args, **_kwargs: SimpleNamespace(decision="eligible"),
    )
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_quality_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_auto_iteration_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    with pytest.raises(HTTPException) as caught:
        await creator_routes.request_creator_auto_iteration(
            str(item_id),
            AutoIterationBody(
                session_id=session_id,
                expected_revision=11,
                opt_in=True,
                client_event_id="auto-event-locked",
            ),
            user,
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "variant_initial_render_in_progress"
    assert session.auto_iteration_opt_in is False
    append_event.assert_not_awaited()
    resolve_context.assert_not_awaited()
    craft.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_craft_replays_succeeded_receipt_without_reenqueue(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    body = _craft_bundle(
        session_id=session_id,
        job_id=job_id,
        generation_id="generation-2",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash(body.model_dump(mode="json")),
        status="succeeded",
        result={
            "generation": "generation-2",
            "prepared": {"generation": "generation-2", "sections": {"caption_meta": True}},
            "preview": {"caption_style": "word"},
        },
    )
    # The first direct craft commit advanced the controller revision before
    # publishing. An exact idempotent replay still carries the original pin.
    session.revision = body.expected_revision + 1
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = receipt
    db.execute.return_value = receipt_result
    db.get.return_value = job
    enqueue = MagicMock()
    monkeypatch.setattr(creator_routes, "enqueue_editor_commit_render", enqueue)
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )

    response = await creator_routes.execute_creator_craft(
        str(item.id), body, SimpleNamespace(id=user_id), db
    )

    assert response.status == "succeeded"
    assert response.generation == "generation-2"
    assert response.preview == {"caption_style": "word"}
    enqueue.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_craft_enqueue_failure_restores_plan_and_fails_receipt(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    body = _craft_bundle(
        session_id=session_id,
        job_id=job_id,
        generation_id="generation-2",
    )
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "variant-1",
                "render_generation_id": "generation-2",
                "render_status": "ready",
                "caption_meta": {"style": "sentence"},
            },
            {"variant_id": "sibling", "render_generation_id": "sibling-1", "rank": 2},
        ],
        "unrelated_state": "before",
    }
    previous_assembly_plan = job.assembly_plan.copy()
    receipt_id = uuid.uuid4()
    failed_receipt = SimpleNamespace(id=receipt_id, status="running", error=None)
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.side_effect = [job, session, job, failed_receipt]

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = receipt_id

    db.add = MagicMock(side_effect=add)
    editor_commit = SimpleNamespace(
        caption_meta=SimpleNamespace(style="word"),
        timeline_slots=None,
        sound_effects=None,
        media_overlays=None,
    )

    def prepare(*_args, **_kwargs):
        job.assembly_plan = {
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-3",
                    "render_status": "rendering",
                },
                {"variant_id": "sibling", "render_generation_id": "sibling-2", "rank": 3},
            ],
            "unrelated_state": "concurrent-update",
        }
        return {"generation": "generation-3", "sections": {"caption_meta": True}}

    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(
        creator_routes,
        "build_core_craft_editor_commit",
        lambda *_args, **_kwargs: editor_commit,
    )
    monkeypatch.setattr(creator_routes, "prepare_editor_commit", prepare)
    monkeypatch.setattr(creator_routes, "craft_preview", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(creator_routes, "_stable_manifest_fingerprint", lambda _manifest: "stable")
    monkeypatch.setattr(
        creator_routes,
        "enqueue_editor_commit_render",
        MagicMock(side_effect=RuntimeError("broker unavailable")),
    )

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id), body, SimpleNamespace(id=user_id), db
        )

    assert caught.value.status_code == 503
    assert job.assembly_plan["variants"][0] == previous_assembly_plan["variants"][0]
    assert job.assembly_plan["variants"][1]["render_generation_id"] == "sibling-2"
    assert job.assembly_plan["unrelated_state"] == "concurrent-update"
    assert failed_receipt.status == "failed"
    assert failed_receipt.error["code"] == "craft_enqueue_failed"
    assert failed_receipt.error["rolled_back"] is True
    # The route advances the controller revision together with the staged
    # generation; broker publication failure restores that exact session state.
    assert session.revision == 3
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_creator_speech_enqueue_failure_rolls_back_private_owner(
    monkeypatch,
) -> None:
    from app.tasks.generative_build import rerender_speech_timing

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
    )
    plan_before = copy.deepcopy(job.assembly_plan)
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    captured: dict[str, CreatorAgentExecution] = {}

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = uuid.uuid4()
            captured["receipt"] = value

    def get(model, *_args, **_kwargs):
        if model is Job:
            return job
        if model is CreatorAgentSession:
            return session
        if model is CreatorAgentExecution:
            return captured["receipt"]
        raise AssertionError(f"unexpected model lookup: {model}")

    db.add = MagicMock(side_effect=add)
    db.get = AsyncMock(side_effect=get)
    enqueue = MagicMock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr(rerender_speech_timing, "apply_async", enqueue)
    _configure_required_speech_craft(
        monkeypatch,
        item=item,
        plan=plan,
        session=session,
        manifest=manifest,
    )

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id), body, SimpleNamespace(id=user_id), db
        )

    receipt = captured["receipt"]
    assert caught.value.status_code == 503
    assert job.assembly_plan == plan_before
    assert job.status == "variants_ready"
    assert job.started_at is None
    assert session.status == "awaiting_feedback"
    assert session.revision == 3
    assert getattr(session, "target_generation_id", None) is None
    assert receipt.status == "failed"
    assert receipt.error["code"] == "craft_enqueue_failed"
    assert receipt.error["rolled_back"] is True
    assert receipt.error["generation"] in enqueue.call_args.kwargs["task_id"]
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_creator_craft_rollback_restores_speech_owned_state_only() -> None:
    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    session_id = uuid.uuid4()
    started_at = datetime.now(UTC)
    previous_plan = {
        "variants": [
            {"variant_id": "target", "render_generation_id": "old", "render_status": "ready"}
        ],
        "silence_cut_disabled": True,
        "speech_cut_control": {"operation_id": "old-op"},
        "speech_cut_previous_variant": {"variant_id": "target", "render_status": "ready"},
        "speech_cut_previous_variants": [{"variant_id": "target"}],
        "speech_cut_last_error": "old-error",
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "target",
                    "render_generation_id": "new",
                    "render_status": "rendering",
                },
                {"variant_id": "sibling", "render_generation_id": "sibling-new"},
            ],
            "silence_cut_disabled": False,
            "speech_cut_control": {"operation_id": "new-op"},
            "speech_cut_previous_variant": {"variant_id": "target", "render_status": "rendering"},
            "speech_cut_previous_variants": [{"variant_id": "sibling"}],
            "speech_cut_last_error": None,
            "unrelated": "concurrent-update",
        },
    )
    session = SimpleNamespace(
        id=session_id,
        status="rendering",
        target_job_id=job_id,
        target_variant_id="target",
        target_generation_id="new",
        render_attempts=2,
        iteration_count=2,
        revision=4,
    )
    failed_receipt = SimpleNamespace(id=receipt_id, status="running", error=None)
    db = AsyncMock()
    db.get.side_effect = [session, job, failed_receipt]

    await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=session_id,
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation="new",
        error=RuntimeError("broker unavailable"),
        previous_job_state={"status": "variants_ready", "started_at": started_at.isoformat()},
        previous_session_state={
            "status": "awaiting_feedback",
            "target_job_id": str(job_id),
            "target_variant_id": "target",
            "target_generation_id": "old",
            "render_attempts": 1,
            "iteration_count": 1,
            "revision": 3,
        },
    )

    assert job.status == "variants_ready"
    assert job.started_at == started_at
    assert job.assembly_plan["variants"][0] == previous_plan["variants"][0]
    assert job.assembly_plan["variants"][1]["render_generation_id"] == "sibling-new"
    assert job.assembly_plan["unrelated"] == "concurrent-update"
    for key in (
        "silence_cut_disabled",
        "speech_cut_control",
        "speech_cut_previous_variant",
        "speech_cut_previous_variants",
        "speech_cut_last_error",
    ):
        assert job.assembly_plan[key] == previous_plan[key]
    assert failed_receipt.status == "failed"
    assert session.status == "awaiting_feedback"
    assert session.target_generation_id == "old"
    assert session.render_attempts == 1
    assert session.revision == 3
    assert [call.args[0] for call in db.get.await_args_list] == [
        CreatorAgentSession,
        Job,
        CreatorAgentExecution,
    ]


@pytest.mark.asyncio
async def test_required_creator_speech_rollback_refuses_superseding_operation() -> None:
    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    session_id = uuid.uuid4()
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {"variant_id": "target", "render_generation_id": "old", "render_status": "ready"}
        ],
    }
    current_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": copy.deepcopy(previous_plan["variants"]),
        "speech_cut_control": {
            "variant_id": "target",
            "operation_id": "superseding-operation",
            # Reusing the token makes this specifically prove that operation id,
            # not generation alone, is part of the private ownership CAS.
            "render_generation_id": "speech-generation",
        },
        "speech_cut_previous_variant": {"variant_id": "target", "private": "new"},
        "speech_cut_previous_variants": copy.deepcopy(previous_plan["variants"]),
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(current_plan),
    )
    session = SimpleNamespace(
        id=session_id,
        status="rendering",
        target_job_id=job_id,
        target_variant_id="target",
        target_generation_id="speech-generation",
        render_attempts=2,
        iteration_count=2,
        revision=4,
    )
    receipt = SimpleNamespace(id=receipt_id, status="running", error=None)
    db = AsyncMock()
    db.get.side_effect = [session, job, receipt]
    stored_plan = copy.deepcopy(job.assembly_plan)
    stored_started_at = job.started_at

    await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=session_id,
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation="speech-generation",
        speech_cut_operation_id="original-operation",
        error=RuntimeError("broker unavailable"),
        previous_job_state={"status": "variants_ready", "started_at": None},
        previous_session_state={
            "status": "awaiting_feedback",
            "target_job_id": str(job_id),
            "target_variant_id": "target",
            "target_generation_id": "old",
            "render_attempts": 1,
            "iteration_count": 1,
            "revision": 3,
        },
    )

    assert job.assembly_plan == stored_plan
    assert job.status == "processing"
    assert job.started_at == stored_started_at
    assert session.status == "rendering"
    assert session.target_generation_id == "speech-generation"
    assert session.revision == 4
    assert receipt.status == "failed"
    assert receipt.error["rolled_back"] is False


@pytest.mark.asyncio
async def test_required_creator_enqueue_response_loss_preserves_adopted_private_owner() -> None:
    """A published task may claim/reserve before ``apply_async`` reports failure."""

    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    session_id = uuid.uuid4()
    operation_id = "speech-operation"
    generation = "speech-generation"
    last_good = {
        "variant_id": "target",
        "render_generation_id": "last-good",
        "render_status": "ready",
        "ok": True,
    }
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [copy.deepcopy(last_good)],
    }
    current_plan = {
        **copy.deepcopy(previous_plan),
        "speech_cut_control": {
            "variant_id": "target",
            "operation_id": operation_id,
            "render_generation_id": generation,
            "finalizer_claim": {
                "operation_id": operation_id,
                "attempt_id": "task-1:0:attempt",
                "render_generation_id": generation,
            },
        },
        "speech_cut_previous_variant": {
            **copy.deepcopy(last_good),
            "render_generation_id": generation,
            "render_status": "rendering",
            "ok": False,
        },
        "speech_cut_previous_variants": [copy.deepcopy(last_good)],
        "_speech_cleanup_internal": {
            "required_speech_generation_locks": {"target": generation},
            "working_render_variants": {
                f"target:{generation}": {
                    "variant_id": "target",
                    "render_generation_id": generation,
                    "render_status": "rendering",
                    "ok": False,
                }
            },
            "render_generation_cleanup_pending": [
                {
                    "generation": generation,
                    "prefix": f"generative-jobs/{job_id}/render-generations/{generation}/",
                    "upload_state": "writing",
                    "lease_expires_at": "2026-09-02T12:30:00+00:00",
                }
            ],
        },
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(current_plan),
    )
    session = SimpleNamespace(
        id=session_id,
        status="rendering",
        target_job_id=job_id,
        target_variant_id="target",
        target_generation_id=generation,
        render_attempts=2,
        iteration_count=2,
        revision=4,
    )
    receipt = SimpleNamespace(
        id=receipt_id,
        status="running",
        result={"prepared": {"generation": generation}},
        error=None,
        completed_at=None,
    )
    db = AsyncMock()
    db.get.side_effect = [session, job, receipt]
    stored_job_plan = copy.deepcopy(job.assembly_plan)
    stored_job_state = (job.status, job.started_at)
    stored_session_state = copy.deepcopy(session.__dict__)

    disposition = await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=session_id,
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation=generation,
        speech_cut_operation_id=operation_id,
        error=RuntimeError("broker response lost"),
        previous_job_state={"status": "variants_ready", "started_at": None},
        previous_session_state={
            "status": "awaiting_feedback",
            "target_job_id": str(job_id),
            "target_variant_id": "target",
            "target_generation_id": "last-good",
            "render_attempts": 1,
            "iteration_count": 1,
            "revision": 3,
        },
    )

    assert disposition == "enqueue_uncertain"
    assert job.assembly_plan == stored_job_plan
    assert (job.status, job.started_at) == stored_job_state
    assert session.__dict__ == stored_session_state
    assert receipt.status == "running"
    assert receipt.result == {"prepared": {"generation": generation}}
    assert receipt.error["code"] == "craft_enqueue_uncertain"
    assert receipt.error["rolled_back"] is False
    assert receipt.completed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim",
    [
        {},
        "malformed-claim",
        {"operation_id": "speech-operation"},
    ],
    ids=["empty", "non-object", "incomplete"],
)
async def test_required_creator_rollback_preserves_malformed_claim(claim) -> None:
    """Only ``finalizer_claim=None`` is route-owned pre-reservation state."""

    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    operation_id = "speech-operation"
    generation = "speech-generation"
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {"variant_id": "target", "render_generation_id": "old", "render_status": "ready"}
        ],
    }
    current_plan = {
        **copy.deepcopy(previous_plan),
        "speech_cut_control": {
            "variant_id": "target",
            "operation_id": operation_id,
            "render_generation_id": generation,
            "finalizer_claim": copy.deepcopy(claim),
        },
        "speech_cut_previous_variant": {"variant_id": "target"},
        "speech_cut_previous_variants": copy.deepcopy(previous_plan["variants"]),
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(current_plan),
    )
    receipt = SimpleNamespace(
        id=receipt_id,
        status="running",
        result={"prepared": {"generation": generation}},
        error=None,
        completed_at=None,
    )
    db = AsyncMock()
    db.get.side_effect = [job, receipt]

    disposition = await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=uuid.uuid4(),
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation=generation,
        speech_cut_operation_id=operation_id,
        error=RuntimeError("broker response lost"),
    )

    assert disposition == "enqueue_uncertain"
    assert job.assembly_plan == current_plan
    assert job.status == "processing"
    assert receipt.status == "running"
    assert receipt.error["code"] == "craft_enqueue_uncertain"


@pytest.mark.asyncio
async def test_required_creator_rollback_preserves_already_published_generation() -> None:
    """A lost broker response can arrive after the worker's final transaction."""

    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    generation = uuid.uuid4().hex
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {
                "variant_id": "target",
                "render_generation_id": uuid.uuid4().hex,
                "render_status": "ready",
            }
        ],
    }
    published_plan = {
        "speech_cleanup_contract": "required_v1",
        "speech_cut_control": None,
        "speech_cut_previous_variant": None,
        "speech_cut_previous_variants": None,
        "variants": [
            {
                "variant_id": "target",
                "render_generation_id": generation,
                "render_status": "ready",
                "video_path": (
                    f"generative-jobs/{job_id}/render-generations/{generation}/final.mp4"
                ),
            }
        ],
    }
    job = SimpleNamespace(
        id=job_id,
        status="variants_ready",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(published_plan),
    )
    receipt = SimpleNamespace(
        id=receipt_id,
        status="running",
        result={"prepared": {"generation": generation}},
        error=None,
        completed_at=None,
    )
    db = AsyncMock()
    db.get.side_effect = [job, receipt]

    disposition = await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=uuid.uuid4(),
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation=generation,
        speech_cut_operation_id=uuid.uuid4().hex,
        error=RuntimeError("broker response lost"),
    )

    assert disposition == "enqueue_uncertain"
    assert job.assembly_plan == published_plan
    assert job.status == "variants_ready"
    assert receipt.status == "running"
    assert receipt.error["code"] == "craft_enqueue_uncertain"
    assert receipt.completed_at is None


class _ExpiringNamespace:
    """Small ORM-like double that rejects attribute reads after rollback."""

    def __init__(self, **values):
        self.__dict__.update(values)
        self.__dict__["_expired"] = False

    def expire(self) -> None:
        self.__dict__["_expired"] = True

    def __getattribute__(self, name):
        if name not in {"__dict__", "expire", "_expired"}:
            if object.__getattribute__(self, "__dict__").get("_expired", False):
                raise MissingGreenlet("attribute access after AsyncSession.rollback()")
        return object.__getattribute__(self, name)


def test_apply_plan_intent_never_activates_missing_voiceover(monkeypatch) -> None:
    manifest = _manifest(monkeypatch, has_voiceover=True)
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="narrated",
            audio_strategy="voiceover",
            render_program="guided",
            selected_media_ids=["clip-1"],
        ),
    )
    item = SimpleNamespace(
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )
    with pytest.raises(HTTPException, match="Record a voiceover"):
        _apply_plan_intent(item, edit_plan)


def test_apply_plan_intent_maps_creative_caption_style_to_renderer_contract(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            caption_style="kinetic",
            render_program="native",
            selected_media_ids=["clip-1"],
        ),
    )
    item = SimpleNamespace(
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )

    _apply_plan_intent(item, edit_plan)

    assert item.voiceover_caption_style == "word"


def test_confirmed_guided_strategy_becomes_specialist_brief(monkeypatch) -> None:
    creator_request = "Photos should have a very fast transition, videos can be a bit longer"
    manifest = _manifest(monkeypatch)
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="licensed_music",
            story_structure=["Cold open", "Build", "Payoff"],
            pacing="fast",
            render_program="guided",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            ),
        ),
    )
    item = SimpleNamespace(edit_proposal=None)
    _seed_guided_specialist_brief(
        item,
        edit_plan,
        summary="A sharp three-beat story",
        creator_request=creator_request,
    )

    assert item.edit_proposal["status"] == "briefing"
    assert item.edit_proposal["brief_ready"] is True
    assert item.edit_proposal["brief"] == {
        "direction": "guided_story",
        "goal": "Cold open; Build; Payoff",
        "pace": "fast",
        "duration_s": 24,
        "creator_request": creator_request,
        "mixed_media_timing": {
            "image_hold": "very_fast",
            "video_hold": "longer",
            "boundary_style": "cut",
        },
        "output_orientation": "portrait",
    }


def test_specialist_brief_normalizes_pool_asset_audio_and_cadence_ids(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "asset-video-a", "kind": "video", "duration_s": 10},
            {"media_id": "asset-video-b", "kind": "video", "duration_s": 10},
        ],
    )
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            audio_strategy="original_audio",
            selected_media_ids=["asset-video-a", "asset-video-b"],
            montage_audio=MontageAudioPlan(
                preserve_source_audio=True,
                source_media_ids=["asset-video-a", "asset-video-b"],
            ),
            montage_cadence=MontageCadenceConstraint(
                source_media_ids=["asset-video-a", "asset-video-b"],
                cut_duration_s=1,
            ),
        ),
    )
    item = SimpleNamespace(edit_proposal=None)

    _seed_guided_specialist_brief(
        item,
        edit_plan,
        summary="Alternate both pool videos.",
        creator_request="Alternate every one second and keep the original audio.",
    )

    assert item.edit_proposal["brief"]["montage_audio"]["source_media_ids"] == [
        "video-a",
        "video-b",
    ]
    assert item.edit_proposal["brief"]["montage_cadence"]["source_media_ids"] == [
        "video-a",
        "video-b",
    ]


def test_native_mixed_timing_replaces_stale_approved_proposal_with_fresh_brief(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            audio_strategy="licensed_music",
            render_program="native",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast",
                video_hold="longer",
                boundary_style="cut",
            ),
        ),
    )
    item = SimpleNamespace(
        edit_proposal={
            "proposal_version": 7,
            "generation_attempt_id": "old-approved-attempt",
            "status": "approved",
        }
    )

    _seed_guided_specialist_brief(
        item,
        edit_plan,
        summary="Fast photos and longer videos.",
        creator_request="Photos should have a very fast transition, videos can be a bit longer",
    )

    assert edit_plan.strategy.render_program == "guided"
    assert item.edit_proposal["proposal_version"] == 9
    assert item.edit_proposal["status"] == "briefing"
    assert item.edit_proposal["brief_ready"] is True
    assert item.edit_proposal["brief"]["mixed_media_timing"] == {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }


def test_truncated_main_creator_fallback_preserves_exact_mixed_media_request(
    monkeypatch,
) -> None:
    strategy = _fallback_strategy(
        _manifest(monkeypatch),
        user_message="Photos should have a very fast transition, videos can be a bit longer",
    )

    assert strategy.direction == "fast_montage"
    assert strategy.mixed_media_timing is not None
    assert strategy.mixed_media_timing.model_dump() == {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }


@pytest.mark.asyncio
async def test_planning_fails_closed_when_mixed_media_specialist_is_unavailable(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", False)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=1,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast",
                video_hold="longer",
                boundary_style="cut",
            ),
        ),
        summary="Fast photos and longer videos.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="failed")

    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Photos should have a very fast transition, videos can be a bit longer",
    )

    assert result is response
    assert session.status == "failed"
    assert session.active_plan is None
    assert session.last_error["code"] == "mixed_media_timing_unavailable"
    assert append_event.await_args.kwargs["event_type"] == "assistant_error"
    assert append_event.await_args.kwargs["payload"] == {
        "message": (
            "Mixed photo and video timing is temporarily unavailable. "
            "No fallback edit was rendered."
        ),
        "code": "mixed_media_timing_unavailable",
    }


@pytest.mark.asyncio
async def test_planning_fails_closed_when_cadence_conflicts_with_voiceover(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 20},
            {"media_id": "match-b", "kind": "video", "duration_s": 20},
        ],
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage", audio_strategy="voiceover", target_duration_s=10
        ),
        summary="Alternate the matches under the voiceover.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="failed")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Alternate every 1 second for 10 seconds.",
    )

    assert result is response
    assert session.status == "failed"
    assert session.last_error["code"] == "montage_cadence_unavailable"
    assert append_event.await_args.kwargs["payload"]["code"] == ("montage_cadence_unavailable")


@pytest.mark.asyncio
async def test_required_cadence_question_fails_closed_at_question_budget(monkeypatch) -> None:
    session = SimpleNamespace(
        status="planning",
        question_count=2,
        question_budget=2,
        last_error=None,
    )
    append_event = AsyncMock()
    monkeypatch.setattr(creator_routes, "append_event", append_event)

    await creator_routes._record_required_cadence_question(
        AsyncMock(),
        session,
        payload={"message": "Choose two videos."},
    )

    assert session.status == "failed"
    assert session.last_error == {"code": "question_budget_exhausted"}
    assert append_event.await_args.kwargs["event_type"] == "assistant_error"


@pytest.mark.asyncio
async def test_alternation_prompt_asks_for_balanced_twelve_second_capacity(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 6.633},
            {"media_id": "match-b", "kind": "video", "duration_s": 26.433},
        ],
    )
    message = "Show one second from one, switch to the other one, and back and forth."
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=0,
                role="user",
                event_type="user_message",
                payload={"message": message},
            )
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage",
            render_program="guided",
            target_duration_s=24,
        ),
        summary="Alternate the two matches.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=message,
    )

    assert result is response
    assert session.status == "briefing"
    payload = append_event.await_args.kwargs["payload"]
    assert payload["message"] == (
        "I can make a balanced 12-second edit without repeating footage. "
        "I recommend using the strongest moments. What would you prefer?"
    )
    assert payload["options"][0] == "Use the best 12 seconds"
    assert payload["cadence_context"]["recommended_duration_s"] == 12


@pytest.mark.asyncio
async def test_unrecognized_source_selection_is_reasked_before_agent_fallback(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 10},
            {"media_id": "match-b", "kind": "video", "duration_s": 10},
            {"media_id": "match-c", "kind": "video", "duration_s": 10},
        ],
    )
    context = {
        "kind": "source_selection",
        "cut_duration_s": 1,
        "reuse_policy": "no_repeat",
        "selections": {"Alternate match-a and match-b": ["match-a", "match-b"]},
    }
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="briefing",
        events=[
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload={"cadence_context": context},
            )
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    to_thread = AsyncMock()
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Whatever you think",
    )

    assert result is response
    to_thread.assert_not_awaited()
    assert append_event.await_args.kwargs["payload"]["reason_code"] == ("cadence_source_selection")
    assert session.status == "briefing"


@pytest.mark.asyncio
async def test_pending_source_selection_honors_latest_cadence_cancellation(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 10},
            {"media_id": "match-b", "kind": "video", "duration_s": 10},
            {"media_id": "match-c", "kind": "video", "duration_s": 10},
        ],
    )
    context = {
        "kind": "source_selection",
        "cut_duration_s": 1,
        "reuse_policy": "no_repeat",
        "selections": {"Alternate match-a and match-b": ["match-a", "match-b"]},
    }
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload={"cadence_context": context},
            )
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    to_thread = AsyncMock(
        return_value=SimpleNamespace(
            action=AskUser(
                kind="ask_user",
                question="What story should this become instead?",
                reason_code="story_direction",
            )
        )
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Don't alternate; make it a guided story",
    )

    assert result is response
    to_thread.assert_awaited_once()
    assert append_event.await_args.kwargs["payload"]["reason_code"] == "story_direction"


@pytest.mark.asyncio
async def test_selected_pair_with_pending_duration_fails_closed(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 10},
            {"media_id": "match-b", "kind": "video", "duration_s": 10},
            {"media_id": "match-c", "kind": "video", "duration_s": None},
        ],
    )
    context = {
        "kind": "source_selection",
        "cut_duration_s": 1,
        "reuse_policy": "no_repeat",
        "selections": {"Alternate match-a and match-c": ["match-a", "match-c"]},
    }
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=0,
                role="user",
                event_type="user_message",
                payload={"message": "Alternate every 1 second"},
            ),
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload={"cadence_context": context},
            ),
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(edit_format="montage", render_program="guided"),
        summary="Alternate the selected matches.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Alternate match-a and match-c",
    )

    assert result is response
    assert append_event.await_args.kwargs["payload"]["reason_code"] == (
        "cadence_duration_unavailable"
    )
    assert append_event.await_args.kwargs["payload"]["cadence_context"]["source_media_ids"] == [
        "match-a",
        "match-c",
    ]


@pytest.mark.asyncio
async def test_model_target_duration_survives_non_regex_cadence_wording(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 20},
            {"media_id": "match-b", "kind": "video", "duration_s": 20},
        ],
    )
    message = "Alternate each match every one second; keep the finished piece ten seconds long."
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage", render_program="guided", target_duration_s=10
        ),
        summary="Alternate the matches for ten seconds.",
    )
    compile_plan = MagicMock(
        return_value={
            "summary": "Alternate the matches for ten seconds.",
            "plan_hash": "a" * 64,
            "target_duration_s": 10,
            "montage_cadence": {},
        }
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "compile_active_plan", compile_plan)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="awaiting_confirmation")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=message,
    )

    assert result is response
    assert compile_plan.call_args.kwargs["strategy"].target_duration_s == 10
    assert compile_plan.call_args.kwargs["strategy"].montage_cadence is not None


@pytest.mark.asyncio
async def test_unrelated_revision_keeps_accepted_sources_with_three_videos(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 20},
            {"media_id": "match-b", "kind": "video", "duration_s": 20},
            {"media_id": "match-c", "kind": "video", "duration_s": 20},
        ],
    )
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=2,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=1,
                role="user",
                event_type="user_message",
                payload={"message": "Alternate every 1 second."},
            ),
            SimpleNamespace(
                sequence=2,
                role="assistant",
                event_type="assistant_strategy",
                payload={
                    "target_duration_s": 12,
                    "montage_cadence": cadence.model_dump(mode="json"),
                },
            ),
        ],
        agent_call_count=0,
        agent_call_budget=3,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(edit_format="montage", render_program="guided"),
        summary="Keep the cadence and refine the captions.",
    )
    compile_plan = MagicMock(
        return_value={
            "summary": "Keep the cadence and refine the captions.",
            "plan_hash": "a" * 64,
            "target_duration_s": 12,
            "montage_cadence": cadence.model_dump(mode="json"),
        }
    )
    append_event = AsyncMock()
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "compile_active_plan", compile_plan)
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    response = SimpleNamespace(status="awaiting_confirmation")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=2,
        user_message="Make the captions clean.",
    )

    assert result is response
    planned = compile_plan.call_args.kwargs["strategy"]
    assert planned.target_duration_s == 12
    assert planned.montage_cadence == cadence
    assert append_event.await_args.kwargs["event_type"] == "assistant_strategy"


def test_confirmed_creator_request_preserves_instruction_across_clarification() -> None:
    initial = "Photos should have a very fast transition, videos can be a bit longer"
    events = [
        SimpleNamespace(sequence=0, role="user", payload={"message": initial}),
        SimpleNamespace(sequence=1, role="assistant", payload={"message": "Use music?"}),
        SimpleNamespace(sequence=2, role="user", payload={"message": "Yes"}),
    ]

    assert _confirmed_creator_request(events, "Yes") == f"{initial}\nYes"


def test_capacity_recommendation_preserves_cadence_context(monkeypatch) -> None:
    manifest = _manifest(monkeypatch).model_copy(
        update={
            "media": [
                {"media_id": "match-a", "kind": "video", "duration_s": 6.633},
                {"media_id": "match-b", "kind": "video", "duration_s": 26.433},
            ]
        }
    )
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "capacity",
                    "cadence": cadence.model_dump(mode="json"),
                    "requested_duration_s": 24,
                    "recommended_duration_s": 12,
                    "recommendation": "Use the best 12 seconds",
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Use the best 12 seconds"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="original request\nUse the best 12 seconds",
        user_message="Use the best 12 seconds",
    )

    assert resolved == cadence
    assert target_s == 12


def test_capacity_answer_can_choose_a_shorter_custom_length(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "capacity",
                    "cadence": cadence.model_dump(mode="json"),
                    "requested_duration_s": 24,
                    "recommended_duration_s": 12,
                    "recommendation": "Use the best 12 seconds",
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Make it 10 seconds"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="original request\nMake it 10 seconds",
        user_message="Make it 10 seconds",
    )

    assert resolved == cadence
    assert target_s == 10


def test_insufficient_capacity_recommendation_explicitly_enables_reuse(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    recommendation = "Allow the strongest moments to repeat"
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "capacity",
                    "cadence": cadence.model_dump(mode="json"),
                    "requested_duration_s": 24,
                    "recommended_duration_s": 2,
                    "recommendation": recommendation,
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": recommendation},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request=f"original request\n{recommendation}",
        user_message=recommendation,
    )

    assert resolved == cadence.model_copy(update={"reuse_policy": "allow_repeat"})
    assert target_s == 24


def test_source_selection_accepts_free_form_video_labels(monkeypatch) -> None:
    base_manifest = _manifest(monkeypatch)
    manifest = type(base_manifest).model_validate(
        {
            **base_manifest.model_dump(mode="json"),
            "media": [
                {"media_id": "match-a", "kind": "video", "duration_s": 10, "label": "Final"},
                {
                    "media_id": "match-b",
                    "kind": "video",
                    "duration_s": 10,
                    "label": "Semi-final",
                },
                {
                    "media_id": "match-c",
                    "kind": "video",
                    "duration_s": 10,
                    "label": "Quarter-final",
                },
            ],
        }
    )
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "source_selection",
                    "cut_duration_s": 1,
                    "reuse_policy": "no_repeat",
                    "selections": {"Alternate Final and Semi-final": ["match-a", "match-b"]},
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Use Semi-final and Quarter-final"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every second\nUse Semi-final and Quarter-final",
        user_message="Use Semi-final and Quarter-final",
    )

    assert resolved == MontageCadenceConstraint(
        source_media_ids=["match-b", "match-c"], cut_duration_s=1
    )
    assert target_s is None


def test_reuse_policy_does_not_treat_a_prohibition_as_permission() -> None:
    assert recognize_cadence_reuse_policy("Do not repeat any footage") == "no_repeat"
    assert recognize_cadence_reuse_policy("Can you not repeat footage?") == "no_repeat"
    assert recognize_cadence_reuse_policy("Repeat the best moments if needed") == "allow_repeat"


def test_cadence_recognizers_separate_cut_timing_total_length_and_cancellation() -> None:
    request = "Make a 3-second edit and alternate every 1 second"

    assert recognize_round_robin_cadence(request) == 1
    assert recognize_total_duration_s(request) == 3
    assert recognize_total_duration_s("I want a 10-second video") == 10
    assert _balanced_integer_duration_s(limit_s=24, cycle_s=1.4) == 21
    assert _next_balanced_integer_duration_s(minimum_s=3, limit_s=12, cycle_s=2) == 4
    assert rejects_round_robin_cadence("Don't alternate; make it a guided story") is True


def test_duration_revision_does_not_resurrect_cadence_after_cancellation() -> None:
    events = [
        SimpleNamespace(
            sequence=0,
            role="user",
            event_type="user_message",
            payload={"message": "Alternate every 1 second."},
        ),
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_strategy",
            payload={
                "target_duration_s": 12,
                "montage_cadence": {
                    "mode": "round_robin",
                    "source_media_ids": ["match-a", "match-b"],
                    "cut_duration_s": 1,
                    "reuse_policy": "no_repeat",
                },
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Don't alternate; make it a guided story."},
        ),
        SimpleNamespace(
            sequence=3,
            role="user",
            event_type="user_message",
            payload={"message": "Make it 10 seconds."},
        ),
    ]
    manifest = SimpleNamespace(
        media=[
            CreatorMediaRef(media_id="match-a", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-b", kind="video", duration_s=10),
        ]
    )

    cadence, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request=(
            "Alternate every 1 second.\n"
            "Don't alternate; make it a guided story.\n"
            "Make it 10 seconds."
        ),
        user_message="Make it 10 seconds.",
    )

    assert cadence is None
    assert target_s is None


def test_unrecognized_source_selection_does_not_resolve() -> None:
    manifest = SimpleNamespace(
        media=[
            CreatorMediaRef(media_id="match-a", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-b", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-c", kind="video", duration_s=10),
        ]
    )
    context = {
        "kind": "source_selection",
        "selections": {"Alternate match-a and match-b": ["match-a", "match-b"]},
    }

    assert _selected_cadence_sources(context, manifest, "Whatever you think") is None


def test_latest_planned_cadence_survives_unrelated_revision(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_strategy",
            payload={
                "target_duration_s": 12,
                "montage_cadence": cadence.model_dump(mode="json"),
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Make the captions clean"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every 1 second\nMake the captions clean",
        user_message="Make the captions clean",
    )

    assert resolved == cadence
    assert target_s == 12


def test_duration_retry_preserves_selected_sources_with_three_videos() -> None:
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-c"], cut_duration_s=1)
    manifest = SimpleNamespace(
        media=[
            CreatorMediaRef(media_id="match-a", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-b", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-c", kind="video", duration_s=10),
        ]
    )
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "duration_unavailable",
                    "cadence": cadence.model_dump(mode="json"),
                    "cut_duration_s": 1,
                    "source_media_ids": ["match-a", "match-c"],
                }
            },
        )
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every 1 second\nUse match-a and match-c\nTry again",
        user_message="Try again",
    )

    assert resolved == cadence
    assert target_s is None


def test_latest_turn_can_cancel_planned_cadence(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_strategy",
            payload={
                "target_duration_s": 12,
                "montage_cadence": cadence.model_dump(mode="json"),
            },
        )
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every 1 second\nDon't alternate anymore",
        user_message="Don't alternate anymore; make it a guided story",
    )

    assert resolved is None
    assert target_s is None


def test_latest_turn_overrides_prior_cadence_timing_and_reuse(monkeypatch) -> None:
    manifest = _manifest(monkeypatch).model_copy(
        update={
            "media": [
                CreatorMediaRef(media_id="match-a", kind="video", duration_s=20),
                CreatorMediaRef(media_id="match-b", kind="video", duration_s=20),
            ]
        }
    )

    resolved, target_s = _resolved_cadence_for_turn(
        events=[],
        manifest=manifest,
        creator_request=(
            "Alternate every 1 second without repeating.\n"
            "Actually, alternate every 2 seconds and allow the moments to repeat."
        ),
        user_message="Actually, alternate every 2 seconds and allow the moments to repeat.",
    )

    assert resolved is not None
    assert resolved.cut_duration_s == 2
    assert resolved.reuse_policy == "allow_repeat"
    assert target_s is None


def test_stale_cadence_question_is_not_reused(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={"cadence_context": {"kind": "capacity"}},
        ),
        SimpleNamespace(
            sequence=2,
            role="assistant",
            event_type="assistant_strategy",
            payload={"message": "A newer plan"},
        ),
        SimpleNamespace(
            sequence=3,
            role="user",
            event_type="user_message",
            payload={"message": "Use the best 12 seconds"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Use the best 12 seconds",
        user_message="Use the best 12 seconds",
    )

    assert resolved is None
    assert target_s is None


def test_main_creator_prompt_contains_no_storage_capabilities(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    prompt = MainCreatorAgent(SimpleNamespace()).render_prompt(
        MainCreatorInput(
            user_message="Make it quick",
            capability_manifest=manifest,
        )
    )
    assert "clip-1" in prompt
    assert "gs://" not in prompt
    assert "s3://" not in prompt
    assert "FFmpeg commands" in prompt


def test_rollout_flags_fail_closed_when_dependencies_are_missing() -> None:
    base = {"storage_bucket": "test", "database_url": "postgresql://test/test"}
    with pytest.raises(ValidationError, match="execution requires"):
        Settings(**base, main_creator_agent_execution_enabled=True)
    with pytest.raises(ValidationError, match="auto iteration requires review"):
        Settings(
            **base,
            main_creator_agent_enabled=True,
            main_creator_agent_auto_iteration_enabled=True,
        )
    with pytest.raises(ValidationError, match="auto iteration requires quality review"):
        Settings(
            **base,
            main_creator_agent_enabled=True,
            main_creator_agent_execution_enabled=True,
            main_creator_agent_review_enabled=True,
            main_creator_agent_auto_iteration_enabled=True,
        )
    with pytest.raises(ValidationError, match="auto iteration requires execution"):
        Settings(
            **base,
            main_creator_agent_enabled=True,
            main_creator_agent_review_enabled=True,
            main_creator_agent_quality_review_enabled=True,
            main_creator_agent_auto_iteration_enabled=True,
        )


def test_replan_clears_every_prior_render_identity() -> None:
    session = SimpleNamespace(
        active_plan={"plan_hash": "old"},
        target_job_id=uuid.uuid4(),
        target_variant_id="variant-old",
        target_generation_id="generation-old",
        last_review={"decision": "approve"},
        last_good={"job_id": "kept-for-rollback"},
    )

    _reset_render_target(session)

    assert session.active_plan is None
    assert session.target_job_id is None
    assert session.target_variant_id is None
    assert session.target_generation_id is None
    assert session.last_review is None
    assert session.last_good == {"job_id": "kept-for-rollback"}


@pytest.mark.parametrize("body_type", [StartBody, TurnBody])
def test_creator_messages_reject_whitespace_at_the_api_boundary(body_type) -> None:
    values = {"message": "   ", "client_event_id": "event-1"}
    if body_type is TurnBody:
        values.update(session_id="11111111-1111-1111-1111-111111111111", expected_revision=0)
    with pytest.raises(ValidationError, match="must not be blank"):
        body_type.model_validate(values)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/session",
            {"message": "   ", "client_event_id": "event-1"},
        ),
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/turn",
            {
                "session_id": "22222222-2222-2222-2222-222222222222",
                "expected_revision": 0,
                "message": "   ",
                "client_event_id": "event-1",
            },
        ),
    ],
)
def test_creator_routes_return_422_for_blank_messages(
    client: TestClient, path: str, payload: dict
) -> None:
    assert client.post(path, json=payload).status_code == 422


def test_creator_route_rollout_gate_is_hidden_as_404(client: TestClient) -> None:
    response = client.post(
        "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/session",
        json={"message": "Make it fast", "client_event_id": "event-1"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Creator agent unavailable"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/session",
            {"message": "Make it fast", "client_event_id": "rate-start"},
        ),
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/turn",
            {
                "session_id": "22222222-2222-2222-2222-222222222222",
                "expected_revision": 0,
                "message": "Make it fast",
                "client_event_id": "rate-turn",
            },
        ),
    ],
)
def test_creator_mutations_are_rate_limited_before_model_call(
    client: TestClient, monkeypatch, path: str, payload: dict
) -> None:
    limiter._storage.reset()
    model_call = MagicMock()
    monkeypatch.setattr(MainCreatorAgent, "run", model_call)
    try:
        responses = [client.post(path, json=payload) for _ in range(13)]
    finally:
        limiter._storage.reset()

    assert [response.status_code for response in responses[:12]] == [404] * 12
    assert responses[-1].status_code == 429
    model_call.assert_not_called()


@pytest.mark.asyncio
async def test_start_replays_terminal_session_for_persisted_start_event(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="completed",
        revision=2,
        events=[
            SimpleNamespace(
                client_event_id="terminal-event",
                sequence=0,
                payload={"message": "Make it personal"},
            )
        ],
    )
    db = AsyncMock()
    prior_result = MagicMock()
    prior_result.scalar_one_or_none.return_value = session
    db.execute.return_value = prior_result
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("test", 1),
            "scheme": "http",
            "server": ("test", 80),
            "query_string": b"",
        }
    )

    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    load_session = AsyncMock(return_value=session)
    monkeypatch.setattr(creator_routes, "_load_session", load_session)
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)
    planning = AsyncMock()
    monkeypatch.setattr(creator_routes, "_run_planning_turn", planning)
    response = SimpleNamespace(status="completed")
    response_for = AsyncMock(return_value=response)
    monkeypatch.setattr(creator_routes, "_response", response_for)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    result = await creator_routes.start_creator_session(
        request,
        str(item.id),
        StartBody(message="Make it personal", client_event_id="terminal-event"),
        user,
        db,
    )

    assert result is response
    latest_session.assert_not_awaited()
    planning.assert_not_awaited()
    load_session.assert_awaited_once_with(db, session.id, user.id, item.id, for_update=True)
    response_for.assert_awaited_once_with(db, session)


@pytest.mark.asyncio
async def test_start_rejects_when_raw_generate_already_minted_active_job(monkeypatch) -> None:
    """The inverse start-vs-generate race is fenced by the same item lock."""
    user = SimpleNamespace(id=uuid.uuid4())
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    plan = SimpleNamespace(ownership_epoch=4)
    job = SimpleNamespace(id=job_id, status="queued")
    db = AsyncMock()
    no_prior = MagicMock()
    no_prior.scalar_one_or_none.return_value = None
    db.execute.return_value = no_prior
    db.get.return_value = job

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    owned_context = AsyncMock(return_value=(item, plan, SimpleNamespace()))
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)

    with pytest.raises(HTTPException) as exc:
        await creator_routes.start_creator_session(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/",
                    "headers": [],
                    "client": ("test", 1),
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                }
            ),
            str(item.id),
            StartBody(message="Alternate every second", client_event_id="start-after-generate"),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert "current render" in str(exc.value.detail)
    owned_context.assert_awaited_once_with(db, str(item.id), user.id, for_update=True)
    db.get.assert_awaited_once_with(
        Job,
        job_id,
        with_for_update=True,
        populate_existing=True,
    )
    latest_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_rejects_when_raw_auto_design_reservation_won_race(monkeypatch) -> None:
    """An analyzing auto-design owns the item before its render Job exists."""
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(
        id=uuid.uuid4(),
        current_job_id=None,
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="raw-auto-attempt",
            status="analyzing",
            approval_mode="auto",
            brief=ProposalBrief(),
        ).model_dump(mode="json"),
    )
    plan = SimpleNamespace(ownership_epoch=4)
    db = AsyncMock()
    no_prior = MagicMock()
    no_prior.scalar_one_or_none.return_value = None
    db.execute.return_value = no_prior

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)

    with pytest.raises(HTTPException) as exc:
        await creator_routes.start_creator_session(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/",
                    "headers": [],
                    "client": ("test", 1),
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                }
            ),
            str(item.id),
            StartBody(message="Alternate every second", client_event_id="start-during-auto"),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert "already designing" in str(exc.value.detail)
    latest_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_rejects_fresh_auto_design_even_with_old_terminal_job(monkeypatch) -> None:
    """A stale completed Job cannot satisfy a newer proposal attempt."""
    user = SimpleNamespace(id=uuid.uuid4())
    old_job_id = uuid.uuid4()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        current_job_id=old_job_id,
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="fresh-raw-auto-attempt",
            status="analyzing",
            approval_mode="auto",
            brief=ProposalBrief(),
        ).model_dump(mode="json"),
    )
    plan = SimpleNamespace(ownership_epoch=4)
    old_job = SimpleNamespace(
        id=old_job_id,
        status="variants_ready",
        assembly_plan={"guided_edit": {"generation_attempt_id": "older-attempt"}},
    )
    db = AsyncMock()
    no_prior = MagicMock()
    no_prior.scalar_one_or_none.return_value = None
    db.execute.return_value = no_prior
    db.get.return_value = old_job

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)

    with pytest.raises(HTTPException) as exc:
        await creator_routes.start_creator_session(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/",
                    "headers": [],
                    "client": ("test", 1),
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                }
            ),
            str(item.id),
            StartBody(message="Use a new direction", client_event_id="fresh-after-old-job"),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert "already designing" in str(exc.value.detail)
    latest_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_locks_an_existing_session_before_appending(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="briefing",
        revision=0,
        active_plan=None,
    )
    db = AsyncMock()
    duplicate_result = MagicMock()
    duplicate_result.scalar_one_or_none.return_value = None
    db.execute.return_value = duplicate_result

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_latest_session", AsyncMock(return_value=session))
    load_session = AsyncMock(return_value=session)
    monkeypatch.setattr(creator_routes, "_load_session", load_session)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    planning = AsyncMock(return_value=SimpleNamespace(id="response"))
    monkeypatch.setattr(creator_routes, "_run_planning_turn", planning)

    await creator_routes.start_creator_session(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [],
                "client": ("test", 1),
                "scheme": "http",
                "server": ("test", 80),
                "query_string": b"",
            }
        ),
        str(item.id),
        StartBody(message="Make it personal", client_event_id="event-1"),
        user,
        db,
    )

    load_session.assert_awaited_once_with(db, session.id, user.id, item.id, for_update=True)
    planning.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_without_an_active_plan_returns_conflict(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=1)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="briefing",
        revision=0,
        active_plan=None,
    )
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.return_value = receipt_result
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))

    with pytest.raises(HTTPException) as caught:
        await creator_routes.confirm_creator_plan(
            str(item.id),
            ConfirmBody(
                session_id=session.id,
                expected_revision=0,
                plan_version=1,
                plan_hash="a" * 64,
                client_event_id="confirm-1",
            ),
            user,
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Creator plan changed"


@pytest.mark.asyncio
async def test_guided_confirm_returns_rendering_after_rollback_expires_loaded_rows(
    monkeypatch,
) -> None:
    """The guided confirmation must not read ORM instances after its rollback.

    ``_maybe_auto_design_generate`` commits the proposal/task reservation.  The
    confirmation route then rolls back to clear its old lock state.  In an
    ``AsyncSession`` that expires the original ``session`` and ``item`` rows;
    touching one of them afterwards raises ``MissingGreenlet`` instead of
    returning the durable rendering state.
    """

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    initial_item = _ExpiringNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )
    session = _ExpiringNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=0,
        ownership_epoch=1,
        manifest_hash=manifest.manifest_hash,
        active_plan=None,
        render_attempts=0,
        max_render_attempts=2,
        iteration_count=0,
    )
    live_item = SimpleNamespace(id=item_id, current_job_id=None)
    refreshed_item = SimpleNamespace(
        id=item_id,
        current_job_id=job_id,
        edit_proposal=None,
    )
    completed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        active_plan={},
        target_job_id=None,
        status="executing",
    )
    rehydrated_session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        ownership_epoch=1,
        active_plan={},
        status="executing",
    )
    plan = SimpleNamespace(ownership_epoch=1)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    active = {
        "version": 1,
        "plan_hash": "a" * 64,
        "summary": "A focused guided edit",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    session.active_plan = active
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash(
            {
                "session_id": str(session_id),
                "expected_revision": 0,
                "plan_version": 1,
                "plan_hash": "a" * 64,
                "client_event_id": "guided-confirm-1",
            }
        ),
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    candidate = SimpleNamespace(
        id=job_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=1,
        status="processing",
        assembly_plan={},
    )
    db = AsyncMock()
    db.add = MagicMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result

    async def rollback_and_expire() -> None:
        initial_item.expire()
        session.expire()

    db.rollback.side_effect = rollback_and_expire

    async def db_get(model, object_id, **_kwargs):
        if model is Job:
            return candidate
        if model is CreatorAgentExecution:
            return receipt
        return None

    db.get.side_effect = db_get
    owned_context = AsyncMock(
        side_effect=[
            (initial_item, plan, SimpleNamespace()),
            (live_item, plan, SimpleNamespace()),
            (refreshed_item, plan, SimpleNamespace()),
        ]
    )
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    monkeypatch.setattr(
        creator_routes,
        "_load_session",
        AsyncMock(side_effect=[session, rehydrated_session, completed]),
    )
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", lambda *_args: None)
    monkeypatch.setattr(
        creator_routes, "_seed_guided_specialist_brief", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(
        creator_routes,
        "_response",
        AsyncMock(return_value=SimpleNamespace(status="rendering")),
    )

    async def reserve_guided_proposal(*_args, generation_attempt_id, **_kwargs):
        refreshed_item.edit_proposal = {
            "generation_attempt_id": generation_attempt_id,
            "proposal_version": 7,
        }
        candidate.assembly_plan = {"guided_edit": {"generation_attempt_id": generation_attempt_id}}
        return SimpleNamespace()

    monkeypatch.setattr(plan_item_routes, "_maybe_auto_design_generate", reserve_guided_proposal)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(settings, "guided_auto_design_enabled", True)

    returned = await creator_routes.confirm_creator_plan(
        str(item_id),
        ConfirmBody(
            session_id=session_id,
            expected_revision=0,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="guided-confirm-1",
        ),
        user,
        db,
    )

    assert returned.status == "rendering"
    assert completed.status == "rendering"
    assert completed.target_job_id == job_id
    assert receipt.status == "succeeded"


@pytest.mark.asyncio
async def test_guided_confirm_persists_failure_after_post_rollback_expiry(monkeypatch) -> None:
    """A post-rollback error must become a durable failed receipt, not a 500."""

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    initial_item = _ExpiringNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )
    session = _ExpiringNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=0,
        ownership_epoch=1,
        manifest_hash=manifest.manifest_hash,
        active_plan=None,
        render_attempts=0,
        max_render_attempts=2,
        iteration_count=0,
    )
    failed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        last_error=None,
    )
    plan = SimpleNamespace(ownership_epoch=1)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    session.active_plan = {
        "version": 1,
        "plan_hash": "a" * 64,
        "summary": "A focused guided edit",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    request = {
        "session_id": session_id,
        "expected_revision": 0,
        "plan_version": 1,
        "plan_hash": "a" * 64,
        "client_event_id": "guided-confirm-fail-1",
    }
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash({**request, "session_id": str(session_id)}),
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db = AsyncMock()
    db.add = MagicMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result

    async def rollback_and_expire() -> None:
        initial_item.expire()
        session.expire()

    db.rollback.side_effect = rollback_and_expire
    db.get.return_value = receipt
    owned_context = AsyncMock(
        side_effect=[
            (initial_item, plan, SimpleNamespace()),
            (SimpleNamespace(id=item_id, current_job_id=None), plan, SimpleNamespace()),
            MissingGreenlet("expired ORM row"),
        ]
    )
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    monkeypatch.setattr(
        creator_routes,
        "_load_session",
        AsyncMock(side_effect=[session, failed]),
    )
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", lambda *_args: None)
    monkeypatch.setattr(
        creator_routes, "_seed_guided_specialist_brief", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(
        creator_routes,
        "_response",
        AsyncMock(return_value=SimpleNamespace(status="failed")),
    )
    monkeypatch.setattr(
        plan_item_routes,
        "_maybe_auto_design_generate",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(settings, "guided_auto_design_enabled", True)

    returned = await creator_routes.confirm_creator_plan(
        str(item_id),
        ConfirmBody(
            session_id=session_id,
            expected_revision=0,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="guided-confirm-fail-1",
        ),
        user,
        db,
    )

    assert returned.status == "failed"
    assert failed.status == "failed"
    assert failed.last_error["code"] == "execution_failed"
    assert receipt.status == "failed"
    assert receipt.error["code"] == "execution_failed"


@pytest.mark.asyncio
async def test_guided_confirm_resumes_exact_job_without_auto_design(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = str(uuid.uuid4())
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        revision=0,
        ownership_epoch=1,
        active_plan={
            "version": 1,
            "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
            "guided_generation_attempt_id": attempt_id,
        },
    )
    completed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        active_plan=session.active_plan,
        target_job_id=None,
        status="executing",
    )
    body = ConfirmBody(
        session_id=session_id,
        expected_revision=0,
        plan_version=1,
        plan_hash="a" * 64,
        client_event_id="guided-resume-1",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash(body.model_dump(mode="json")),
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    candidate = SimpleNamespace(
        id=job_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=1,
        all_candidates={},
        assembly_plan={"guided_edit": {"generation_attempt_id": attempt_id}},
    )
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = receipt
    db.execute.return_value = receipt_result

    async def db_get(model, _object_id, **_kwargs):
        if model is Job:
            return candidate
        if model is CreatorAgentExecution:
            return receipt
        return None

    db.get.side_effect = db_get
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=1), SimpleNamespace())),
    )
    monkeypatch.setattr(
        creator_routes, "_load_session", AsyncMock(side_effect=[session, completed])
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="rendering")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    auto_design = AsyncMock()
    monkeypatch.setattr(plan_item_routes, "_maybe_auto_design_generate", auto_design)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    returned = await creator_routes.confirm_creator_plan(str(item_id), body, user, db)

    assert returned is response
    assert completed.status == "rendering"
    assert completed.target_job_id == job_id
    assert receipt.status == "succeeded"
    auto_design.assert_not_awaited()


@pytest.mark.asyncio
async def test_guided_confirm_missing_refreshed_receipt_returns_durable_failure(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=None)
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=0,
        ownership_epoch=1,
        manifest_hash=manifest.manifest_hash,
        active_plan={
            "version": 1,
            "plan_hash": "a" * 64,
            "summary": "A focused guided edit",
            "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
        },
        render_attempts=0,
        max_render_attempts=2,
        iteration_count=0,
    )
    rehydrated = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        ownership_epoch=1,
        active_plan=session.active_plan,
    )
    failed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        last_error=None,
    )
    db = AsyncMock()
    db.add = MagicMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = None
    refreshed_item = SimpleNamespace(id=item_id, current_job_id=None, edit_proposal=None)
    owned_context = AsyncMock(
        side_effect=[
            (item, SimpleNamespace(ownership_epoch=1), SimpleNamespace()),
            (item, SimpleNamespace(ownership_epoch=1), SimpleNamespace()),
            (refreshed_item, SimpleNamespace(ownership_epoch=1), SimpleNamespace()),
        ]
    )
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    monkeypatch.setattr(
        creator_routes,
        "_load_session",
        AsyncMock(side_effect=[session, rehydrated, failed]),
    )
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", lambda *_args: None)
    monkeypatch.setattr(
        creator_routes, "_seed_guided_specialist_brief", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="failed")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    async def reserve_guided_proposal(*_args, generation_attempt_id, **_kwargs):
        refreshed_item.edit_proposal = {
            "generation_attempt_id": generation_attempt_id,
            "proposal_version": 7,
        }
        return SimpleNamespace()

    monkeypatch.setattr(plan_item_routes, "_maybe_auto_design_generate", reserve_guided_proposal)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(settings, "guided_auto_design_enabled", True)

    returned = await creator_routes.confirm_creator_plan(
        str(item_id),
        ConfirmBody(
            session_id=session_id,
            expected_revision=0,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="guided-missing-receipt-1",
        ),
        user,
        db,
    )

    assert returned is response
    assert failed.status == "failed"
    assert failed.last_error["code"] == "execution_failed"
    assert db.rollback.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_render_preserves_direction_and_refunds_attempt(monkeypatch) -> None:
    user_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        plan_item_id=item.id,
        status="rendering",
        render_attempts=1,
        iteration_count=1,
    )
    receipt = SimpleNamespace(id=uuid.uuid4(), status="running", error=None, completed_at=None)
    db = AsyncMock()
    db.get.return_value = receipt
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    append = AsyncMock()
    monkeypatch.setattr(creator_routes, "append_event", append)
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    returned = await creator_routes._concurrent_render_response(
        db,
        session=session,
        item=item,
        user_id=user_id,
        receipt=receipt,
    )

    assert returned is response
    assert session.status == "briefing"
    assert session.render_attempts == 0
    assert receipt.status == "stale"
    assert receipt.error == {"code": "concurrent_render"}
    append.assert_awaited_once()
