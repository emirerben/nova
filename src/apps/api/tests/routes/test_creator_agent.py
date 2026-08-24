"""Focused Main Creator route/controller contracts."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.auth import get_current_user
from app.config import Settings, settings
from app.database import get_db
from app.main import app
from app.routes import creator_agent as creator_routes
from app.routes.creator_agent import (
    ConfirmBody,
    StartBody,
    TurnBody,
    _apply_plan_intent,
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
