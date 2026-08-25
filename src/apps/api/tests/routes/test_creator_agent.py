"""Focused Main Creator route/controller contracts."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import MissingGreenlet

from app.agents._schemas.creator_agent import CreativeStrategy, canonical_context_hash
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.auth import get_current_user
from app.config import Settings, settings
from app.database import get_db
from app.main import app
from app.models import CreatorAgentExecution, Job
from app.routes import creator_agent as creator_routes
from app.routes import plan_items as plan_item_routes
from app.routes.creator_agent import (
    ConfirmBody,
    StartBody,
    TurnBody,
    _apply_plan_intent,
    _creator_speech_cut_source_enabled,
    _reset_render_target,
    _seed_guided_specialist_brief,
)
from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest


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
        ),
    )
    item = SimpleNamespace(edit_proposal=None)
    _seed_guided_specialist_brief(item, edit_plan, summary="A sharp three-beat story")

    assert item.edit_proposal["status"] == "briefing"
    assert item.edit_proposal["brief_ready"] is True
    assert item.edit_proposal["brief"] == {
        "direction": "guided_story",
        "goal": "Cold open; Build; Payoff",
        "pace": "fast",
        "duration_s": 24,
    }


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
