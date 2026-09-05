"""Fast contract tests for creation-thread route guardrails."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.agents._schemas.persona import Persona as PersonaSchema
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Persona as PersonaRow
from app.routes.creation_threads import (
    ArchiveBody,
    AttachBody,
    MediaInput,
    MessageBody,
    RenameBody,
    UploadBody,
    UploadFile,
    _available_formats,
    _client_id,
    _creator_agent_projection,
    _enabled,
    _exclude_referenced_project_storage,
    _format_clip_limit,
    _load,
    _media_path,
    _other_project_input_references,
    _project,
    _project_job_storage_paths,
    _record_partial_variant_retry_enqueue_failure,
    _response,
    action_thread,
    archive_thread,
    attach_media,
    capabilities,
    get_thread,
    list_threads,
    message_thread,
    rename_thread,
    upload_urls,
)


@pytest.mark.asyncio
async def test_new_thread_provisions_renderable_minimal_persona() -> None:
    """A chat project can dispatch before onboarding has generated a persona."""

    user = SimpleNamespace(id=uuid.uuid4())
    persona_result = Mock()
    persona_result.scalar_one_or_none.return_value = None
    plan_result = Mock()
    plan_result.scalar_one_or_none.return_value = None
    position_result = Mock()
    position_result.scalar_one.return_value = 0
    db = Mock()
    db.get = AsyncMock(return_value=user)
    db.execute = AsyncMock(side_effect=[persona_result, plan_result, position_result])
    db.add = Mock()
    db.flush = AsyncMock()

    _plan, _item = await _project(db, user)

    persona = next(
        call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], PersonaRow)
    )
    validated = PersonaSchema.model_validate(persona.persona)
    assert validated.content_pillars
    assert persona.persona_status == "edited"


@pytest.mark.asyncio
async def test_existing_empty_persona_is_repaired_without_replacing_nonempty() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    empty = PersonaRow(
        user_id=user.id,
        questionnaire={},
        persona={},
        persona_status="generating",
        idea_seeds=[],
    )
    persona_result = Mock()
    persona_result.scalar_one_or_none.return_value = empty
    plan_result = Mock()
    plan_result.scalar_one_or_none.return_value = None
    position_result = Mock()
    position_result.scalar_one.return_value = 0
    db = Mock()
    db.get = AsyncMock(return_value=user)
    db.execute = AsyncMock(side_effect=[persona_result, plan_result, position_result])
    db.add = Mock()
    db.flush = AsyncMock()

    await _project(db, user)

    PersonaSchema.model_validate(empty.persona)
    assert empty.persona_status == "edited"

    preserved = {"summary": "My own lane"}
    empty.persona = preserved
    empty.persona_status = "edited"
    persona_result.scalar_one_or_none.return_value = empty
    db.execute = AsyncMock(side_effect=[persona_result, plan_result, position_result])
    await _project(db, user)
    assert empty.persona == preserved


def test_paper_capabilities_expose_only_three_live_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "narrated_archetype_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    assert _available_formats() == {
        "montage": "montage",
        "narrated": "narrated_planned",
        "talking_to_camera": "subtitled",
    }


def test_unavailable_format_is_removed_from_capability_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "narrated_archetype_enabled", False)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", False)
    assert _available_formats() == {"montage": "montage"}


def test_chat_format_clip_limits_match_plan_item_setup() -> None:
    assert _format_clip_limit("montage") == 50
    assert _format_clip_limit("narrated_planned") == 50
    assert _format_clip_limit("subtitled") == 1


@pytest.mark.parametrize("value", ["", "../escape", "foo/bar", "foo\\bar"])
def test_media_and_event_ids_are_opaque(value: str) -> None:
    with pytest.raises(ValueError):
        _client_id(value)


def test_media_reservation_path_is_deterministic_and_tenant_scoped() -> None:
    user_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    path = _media_path(user_id, thread_id, "upload-1")
    assert path == f"users/{user_id}/creation-threads/{thread_id}/upload-1"
    assert _media_path(user_id, thread_id, "upload-1") == path


def test_client_event_ids_are_canonicalized_and_action_payloads_are_bounded() -> None:
    import app.routes.creation_threads as routes

    body = routes.MessageBody(
        message="keep it warm", client_event_id="  message-1  ", expected_revision=0
    )
    assert body.client_event_id == "message-1"
    with pytest.raises(ValueError):
        routes.ActionBody(
            action="set_intent",
            payload={"intent": "x" * 9000},
            client_action_id="large-action",
            expected_revision=0,
        )


def test_attach_batch_rejects_duplicate_media_and_multiple_voiceovers() -> None:
    with pytest.raises(ValueError, match="media IDs must be unique"):
        AttachBody(
            media=[
                MediaInput(media_id="clip-1.mp4", kind="video"),
                MediaInput(media_id="clip-1.mp4", kind="video"),
            ],
            client_event_id="attach-duplicates",
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="only one voiceover"):
        AttachBody(
            media=[
                MediaInput(media_id="voice-1.m4a", kind="audio"),
                MediaInput(media_id="voice-2.m4a", kind="audio"),
            ],
            client_event_id="attach-voices",
            expected_revision=0,
        )


def test_chat_first_backend_and_creator_defaults_are_on() -> None:
    assert settings.creation_threads_enabled is True


def test_chat_first_account_allowlist_matches_exact_email_or_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), email="Creator@Example.com")
    monkeypatch.setattr(settings, "creation_threads_enabled", True)

    monkeypatch.setattr(settings, "creation_threads_user_allowlist", "creator@example.com")
    _enabled(user)

    monkeypatch.setattr(settings, "creation_threads_user_allowlist", str(user.id))
    _enabled(user)

    monkeypatch.setattr(settings, "creation_threads_user_allowlist", "other@example.com")
    with pytest.raises(HTTPException) as exc:
        _enabled(user)
    assert exc.value.status_code == 404


def test_chat_first_account_allowlist_never_uses_partial_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), email="creator@example.com")
    monkeypatch.setattr(settings, "creation_threads_enabled", True)
    monkeypatch.setattr(settings, "creation_threads_user_allowlist", "creator@example.com.evil")

    with pytest.raises(HTTPException) as exc:
        _enabled(user)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("cohort", ["", "  ", "*"])
def test_chat_first_empty_or_wildcard_allowlist_preserves_global_rollout(
    monkeypatch: pytest.MonkeyPatch, cohort: str
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), email="creator@example.com")
    monkeypatch.setattr(settings, "creation_threads_enabled", True)
    monkeypatch.setattr(settings, "creation_threads_user_allowlist", cohort)
    _enabled(user)


def test_chat_first_global_kill_switch_wins_over_account_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), email="creator@example.com")
    monkeypatch.setattr(settings, "creation_threads_enabled", False)
    monkeypatch.setattr(settings, "creation_threads_user_allowlist", "*")

    with pytest.raises(HTTPException) as exc:
        _enabled(user)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_capabilities_returns_fallback_404_outside_account_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), email="not-launched@example.com")
    monkeypatch.setattr(settings, "creation_threads_enabled", True)
    monkeypatch.setattr(settings, "creation_threads_user_allowlist", "launched@example.com")

    with pytest.raises(HTTPException) as exc:
        await capabilities(user)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Creation chat unavailable"


def test_account_rollout_gate_runs_before_creation_route_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), email="not-launched@example.com")

    async def current_user_override() -> object:
        return user

    async def forbidden_db_override():
        raise AssertionError("a denied account must not open a route database dependency")
        yield  # pragma: no cover

    monkeypatch.setattr(settings, "creation_threads_enabled", True)
    monkeypatch.setattr(settings, "creation_threads_user_allowlist", "launched@example.com")
    app.dependency_overrides[get_current_user] = current_user_override
    app.dependency_overrides[get_db] = forbidden_db_override
    try:
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/creation-threads/capabilities").status_code == 404
        assert client.post("/creation-threads", json={}).status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def test_creator_agent_projection_omits_executable_commands() -> None:
    session = SimpleNamespace(
        status="awaiting_confirmation",
        revision=4,
        active_plan={
            "summary": "Open on the laugh",
            "plan_hash": "x" * 64,
            "version": 2,
            "commands": [{"command": "set_item_intent", "value": "hidden"}],
        },
    )
    assert _creator_agent_projection(session) == {
        "status": "awaiting_confirmation",
        "revision": 4,
        "summary": "Open on the laugh",
        "plan_hash": "x" * 64,
        "version": 2,
    }


def _db_for_scalar(value: object) -> Mock:
    result = Mock()
    result.scalar_one_or_none.return_value = value
    db = Mock()
    db.execute = AsyncMock(return_value=result)
    return db


def _request() -> Request:
    # Each unit call gets its own limiter key; otherwise the decorated route
    # tests exhaust the production 10/minute DELETE budget as a suite.
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": (f"test-{uuid.uuid4()}", 0),
        }
    )


@pytest.mark.asyncio
async def test_load_is_tenant_scoped_and_hides_other_owner() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id)
    db = _db_for_scalar(thread)
    assert await _load(str(thread.id), user, db) is thread

    db = _db_for_scalar(None)
    with pytest.raises(Exception) as exc:
        await _load(str(thread.id), user, db)
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_load_can_use_preserved_owner_id_after_user_orm_expiry() -> None:
    owner_id = uuid.uuid4()
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=owner_id)
    db = _db_for_scalar(thread)

    class _ExpiredUser:
        @property
        def id(self):
            raise AssertionError("expired ORM user must not be lazy-loaded")

    loaded = await _load(
        str(thread.id),
        _ExpiredUser(),
        db,
        lock=True,
        creator_id=owner_id,
    )

    assert loaded is thread


@pytest.mark.asyncio
async def test_message_revision_conflict_does_not_mutate() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active", revision=8)
    db = Mock()
    db.execute = AsyncMock()
    import app.routes.creation_threads as routes

    original = routes._load
    original_duplicate = routes._duplicate
    routes._load = AsyncMock(return_value=thread)
    routes._duplicate = AsyncMock(return_value=None)
    try:
        with pytest.raises(Exception) as exc:
            await message_thread(
                _request(),
                str(thread.id),
                MessageBody(message="make it warmer", client_event_id="m-1", expected_revision=7),
                user,
                db,
            )
        assert getattr(exc.value, "status_code", None) == 409
        db.commit.assert_not_called()
    finally:
        routes._load = original
        routes._duplicate = original_duplicate


@pytest.mark.asyncio
async def test_rendering_message_is_queued_without_calling_creator_agent() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=2,
        active_job_id=uuid.uuid4(),
        state={"media": [{"media_id": "m1"}]},
    )
    job = SimpleNamespace(id=thread.active_job_id, status="processing")
    db = Mock()
    max_result = Mock()
    max_result.scalar_one.return_value = -1
    db.execute = AsyncMock(return_value=max_result)
    db.get = AsyncMock(return_value=job)
    db.add = Mock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    user_message = Mock()
    agent = Mock()
    import app.routes.creation_threads as routes

    original_load, original_dup, original_response, original_agent = (
        routes._load,
        routes._duplicate,
        routes._response,
        routes._agent_message,
    )
    routes._load = AsyncMock(return_value=thread)
    routes._duplicate = AsyncMock(return_value=None)
    routes._response = AsyncMock(return_value=user_message)
    routes._agent_message = agent
    try:
        result = await message_thread(
            _request(),
            str(thread.id),
            MessageBody(message="make it warmer", client_event_id="m-1", expected_revision=2),
            user,
            db,
        )
        assert result is user_message
        assert thread.state["pending_revision_intent"] == "make it warmer"
        agent.assert_not_called()
        db.commit.assert_awaited_once()
    finally:
        routes._load, routes._duplicate, routes._response, routes._agent_message = (
            original_load,
            original_dup,
            original_response,
            original_agent,
        )


@pytest.mark.asyncio
async def test_creator_turn_409_rolls_back_thread_message_for_idempotent_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=2,
        active_job_id=None,
        active_creator_agent_session_id=uuid.uuid4(),
        state={"edit_format": "montage", "media_count": 1},
    )
    db = Mock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    append = AsyncMock()
    agent = AsyncMock(side_effect=HTTPException(409, "stale session"))
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", append)
    monkeypatch.setattr(routes, "_agent_message", agent)

    with pytest.raises(routes.HTTPException) as exc_info:
        await message_thread(
            _request(),
            str(thread.id),
            MessageBody(message="make it warmer", client_event_id="m-atomic", expected_revision=2),
            user,
            db,
        )

    assert exc_info.value.status_code == 409
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_after_terminal_creator_failure_starts_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    failed_session_id = uuid.uuid4()
    new_session_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=failed_session_id,
    )
    failed_session = SimpleNamespace(id=failed_session_id, status="failed", revision=6)
    refreshed_result = Mock()
    refreshed_result.scalar_one.return_value = thread
    db = Mock()
    db.get = AsyncMock(return_value=failed_session)
    db.execute = AsyncMock(return_value=refreshed_result)
    start = AsyncMock(return_value=SimpleNamespace(id=str(new_session_id)))
    turn = AsyncMock()
    sync_agent = AsyncMock()
    monkeypatch.setattr(routes.creator_agent, "start_creator_session_controller", start)
    monkeypatch.setattr(routes.creator_agent, "creator_session_turn_controller", turn)
    monkeypatch.setattr(routes, "_sync_agent", sync_agent)
    body = MessageBody(
        message="Try the corrected photo sequence",
        client_event_id="recovery-message",
        expected_revision=10,
    )

    result = await routes._agent_message(_request(), thread, body, user, db)

    assert result is thread
    assert thread.active_creator_agent_session_id == new_session_id
    start.assert_awaited_once()
    turn.assert_not_awaited()
    sync_agent.assert_awaited_once_with(db, thread)


@pytest.mark.asyncio
async def test_message_after_render_budget_exhaustion_replenishes_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    exhausted_session_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=exhausted_session_id,
    )
    exhausted_session = SimpleNamespace(
        id=exhausted_session_id,
        status="awaiting_feedback",
        revision=14,
        render_attempts=2,
        max_render_attempts=2,
    )
    refreshed_result = Mock()
    refreshed_result.scalar_one.return_value = thread
    db = Mock()
    db.get = AsyncMock(return_value=exhausted_session)
    db.execute = AsyncMock(return_value=refreshed_result)
    start = AsyncMock()
    turn = AsyncMock(return_value=SimpleNamespace(id=str(exhausted_session_id)))
    sync_agent = AsyncMock()
    monkeypatch.setattr(routes.creator_agent, "start_creator_session_controller", start)
    monkeypatch.setattr(routes.creator_agent, "creator_session_turn_controller", turn)
    monkeypatch.setattr(routes, "_sync_agent", sync_agent)
    body = MessageBody(
        message="Use every source once and crop photos like videos",
        client_event_id="fresh-budget-message",
        expected_revision=10,
    )

    result = await routes._agent_message(_request(), thread, body, user, db)

    assert result is thread
    assert thread.active_creator_agent_session_id == exhausted_session_id
    assert exhausted_session.status == "awaiting_feedback"
    assert exhausted_session.revision == 14
    assert exhausted_session.max_render_attempts == 4
    start.assert_not_awaited()
    turn.assert_awaited_once()
    sync_agent.assert_awaited_once_with(db, thread)


@pytest.mark.asyncio
async def test_source_mutation_uses_authoritative_plan_item_job(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id, job_id = uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_plan_item_id=item_id,
        active_job_id=None,
        state={"edit_format": "montage"},
    )
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    job = SimpleNamespace(id=job_id, status="processing")
    db = Mock()
    db.get = AsyncMock(side_effect=[item, job])
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))

    with pytest.raises(routes.HTTPException) as exc_info:
        await routes.action_thread(
            _request(),
            str(thread.id),
            routes.ActionBody(
                action="select_format",
                payload={"format": "montage"},
                client_action_id="format-authoritative-job",
                expected_revision=0,
            ),
            user,
            db,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_archive_is_revision_fenced_and_idempotent() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active", revision=0)
    db = Mock()
    max_result = Mock()
    max_result.scalar_one.return_value = -1
    db.execute = AsyncMock(return_value=max_result)
    db.add = Mock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    original_load, original_dup, original_response = (
        routes._load,
        routes._duplicate,
        routes._response,
    )
    routes._load = AsyncMock(return_value=thread)
    routes._duplicate = AsyncMock(return_value=None)
    routes._response = AsyncMock(return_value=thread)
    try:
        result = await archive_thread(
            _request(),
            str(thread.id),
            ArchiveBody(client_event_id="archive-1", expected_revision=0),
            user,
            db,
        )
        assert result is thread
        assert thread.status == "archived"
        db.commit.assert_awaited_once()
    finally:
        routes._load, routes._duplicate, routes._response = (
            original_load,
            original_dup,
            original_response,
        )


@pytest.mark.asyncio
async def test_rename_is_revision_fenced_and_idempotent() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(), creator_id=user.id, status="active", revision=2, title="Old"
    )
    db = Mock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    original_load = routes._load
    original_dup = routes._duplicate
    original_response = routes._response
    routes._load = AsyncMock(return_value=thread)
    routes._duplicate = AsyncMock(return_value=None)
    routes._response = AsyncMock(return_value=thread)
    original_append = routes._append
    routes._append = AsyncMock()
    try:
        result = await rename_thread(
            _request(),
            str(thread.id),
            RenameBody(title="  Summer   trip ", expected_revision=2, client_event_id="rename-1"),
            user,
            db,
        )
        assert result is thread
        assert thread.title == "Summer trip"
        routes._append.assert_awaited_once()
        assert routes._append.await_args.kwargs == {
            "event_type": "thread_renamed",
            "payload": {"title": "Summer trip"},
            "client_event_id": "rename-1",
        }
        db.commit.assert_awaited_once()
    finally:
        routes._load = original_load
        routes._duplicate = original_dup
        routes._response = original_response
        routes._append = original_append


@pytest.mark.asyncio
async def test_rename_rejects_stale_revision() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(), creator_id=user.id, status="active", revision=3, title="Old"
    )
    import app.routes.creation_threads as routes

    original_load, original_dup = routes._load, routes._duplicate
    routes._load = AsyncMock(return_value=thread)
    routes._duplicate = AsyncMock(return_value=None)
    try:
        with pytest.raises(routes.HTTPException) as exc_info:
            await rename_thread(
                _request(),
                str(thread.id),
                RenameBody(title="New", expected_revision=2, client_event_id="rename-stale"),
                user,
                Mock(),
            )
        assert exc_info.value.status_code == 409
    finally:
        routes._load, routes._duplicate = original_load, original_dup


@pytest.mark.asyncio
async def test_delete_rejects_active_job_before_mutation() -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        revision=4,
        content_plan_id=None,
        active_plan_item_id=None,
        active_job_id=uuid.uuid4(),
        state={"media": []},
    )
    job = SimpleNamespace(id=thread.active_job_id, user_id=user.id, status="processing")

    def result(*, one=None, rows=None):
        value = Mock()
        value.scalar_one_or_none.return_value = one
        value.scalars.return_value.all.return_value = rows or []
        return value

    db = Mock()
    db.execute = AsyncMock(
        side_effect=[
            result(one=None),
            result(one=thread),
            result(rows=[]),
            result(rows=[job]),
            result(rows=[]),
        ]
    )
    with pytest.raises(HTTPException) as exc_info:
        await routes.delete_thread(
            _request(),
            str(thread.id),
            user,
            db,
            expected_revision=4,
        )
    assert exc_info.value.status_code == 409
    db.commit = AsyncMock()
    db.commit.assert_not_awaited()


def _delete_result(*, one=None, rows=None) -> Mock:
    result = Mock()
    result.scalar_one_or_none.return_value = one
    result.scalars.return_value.all.return_value = rows or []
    return result


def _reference_result(rows=()) -> Mock:
    result = Mock()
    result.all.return_value = list(rows)
    return result


@pytest.mark.asyncio
async def test_project_input_reference_scan_includes_other_jobs_and_plan_items() -> None:
    user_id = uuid.uuid4()
    job_clip = f"users/{user_id}/plan/item-a/shared.mp4"
    direct_voiceover = f"voiceover-uploads/direct/{user_id}/voice.m4a"
    assigned_clip = f"users/{user_id}/plan/item-b/assigned.mp4"
    db = Mock()
    db.execute = AsyncMock(
        side_effect=[
            _reference_result(
                [
                    (
                        "jobs/source.mp4",
                        {
                            "clip_paths": [job_clip],
                            "voiceover_gcs_path": direct_voiceover,
                        },
                    )
                ]
            ),
            _reference_result(
                [
                    (
                        [],
                        [{"gcs_path": assigned_clip}],
                        None,
                    )
                ]
            ),
        ]
    )

    references = await _other_project_input_references(
        db,
        user_id=user_id,
        excluded_job_ids={uuid.uuid4()},
        excluded_item_id=uuid.uuid4(),
    )

    assert {job_clip, direct_voiceover, assigned_clip} <= references


def test_shared_input_suppresses_exact_key_and_enclosing_project_prefix() -> None:
    shared = "users/owner/plan/project/shared.mp4"
    private = "users/owner/plan/project/private.mp4"
    unrelated_prefix = "jobs/job-1/"

    paths, prefixes = _exclude_referenced_project_storage(
        [shared, private],
        ["users/owner/plan/project/", unrelated_prefix],
        {shared},
    )

    assert paths == [private]
    assert prefixes == [unrelated_prefix]


def _delete_thread(*, user_id: uuid.UUID, thread_id: uuid.UUID, **overrides) -> SimpleNamespace:
    values = {
        "id": thread_id,
        "creator_id": user_id,
        "revision": 4,
        "status": "active",
        "content_plan_id": None,
        "active_plan_item_id": None,
        "active_job_id": None,
        "active_creator_agent_session_id": None,
        "state": {"media": []},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_delete_rejects_a_live_signed_upload_reservation() -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id = uuid.uuid4()
    thread = _delete_thread(user_id=user.id, thread_id=thread_id)
    reservation = SimpleNamespace(
        creator_id=user.id,
        object_path=_media_path(user.id, thread_id, "clip-1.mp4"),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db = Mock()
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(rows=[reservation]),
        ]
    )
    db.commit = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await routes.delete_thread(
            _request(), str(thread_id), user, db, expected_revision=thread.revision
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Project has an active upload"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_manifest_keeps_expired_upload_reservation_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routes.creation_threads as routes
    from app.tasks.account_lifecycle import purge_job_storage

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id = uuid.uuid4()
    thread = _delete_thread(user_id=user.id, thread_id=thread_id)
    reservation_path = _media_path(user.id, thread_id, "expired-clip.mp4")
    reservation = SimpleNamespace(
        creator_id=user.id,
        object_path=reservation_path,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    monkeypatch.setattr(purge_job_storage, "apply_async", Mock())
    db = Mock()
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(rows=[reservation]),
            _reference_result(),
            _reference_result(),
            _delete_result(),
        ]
    )
    db.add = Mock()
    db.commit = AsyncMock()

    response = await routes.delete_thread(
        _request(), str(thread_id), user, db, expected_revision=thread.revision
    )

    assert response.status_code == 204
    manifests = [call.args[0] for call in db.add.call_args_list]
    deletion = next(manifest for manifest in manifests if hasattr(manifest, "object_paths"))
    assert reservation_path in deletion.object_paths
    assert f"users/{user.id}/creation-threads/{thread_id}/" in deletion.object_prefixes


@pytest.mark.asyncio
async def test_delete_rejects_active_training_retention() -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id, plan_id, item_id, artifact_id = (uuid.uuid4() for _ in range(4))
    thread = _delete_thread(
        user_id=user.id,
        thread_id=thread_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=user.id)
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=None)
    artifact = SimpleNamespace(id=artifact_id, creator_id=user.id, storage_path=None)
    retention = SimpleNamespace(
        artifact_id=artifact_id,
        creator_id=user.id,
        status="pending",
        storage_path=f"users/{user.id}/edit-feedback/{artifact_id}/training.mp4",
    )
    db = Mock()
    db.get = AsyncMock(side_effect=[plan, persona, item])
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(),
            _delete_result(rows=[]),
            _delete_result(rows=[artifact]),
            _delete_result(rows=[retention]),
        ]
    )
    db.commit = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await routes.delete_thread(
            _request(), str(thread_id), user, db, expected_revision=thread.revision
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Project has active artifact processing"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_manifest_includes_exact_visual_staging_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routes.creation_threads as routes
    from app.tasks.account_lifecycle import purge_job_storage

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id, plan_id, item_id = (uuid.uuid4() for _ in range(3))
    thread = _delete_thread(
        user_id=user.id,
        thread_id=thread_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=user.id)
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        clip_gcs_paths=[],
        voiceover_gcs_path=None,
    )
    staging_path = f"dev-user/{user.id}/plan-pool-reservations/{item_id}/visual.png"
    asset = SimpleNamespace(
        user_id=user.id,
        status="ready",
        upload_expires_at=None,
        gcs_path=staging_path,
        preview_gcs_path=None,
    )
    monkeypatch.setattr(purge_job_storage, "apply_async", Mock())
    db = Mock()
    db.get = AsyncMock(side_effect=[plan, persona, item])
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(rows=[]),
            _delete_result(rows=[asset]),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _reference_result(),
            _reference_result(),
            _delete_result(),
            _delete_result(),
        ]
    )
    db.add = Mock()
    db.commit = AsyncMock()

    response = await routes.delete_thread(
        _request(), str(thread_id), user, db, expected_revision=thread.revision
    )

    assert response.status_code == 204
    manifests = [call.args[0] for call in db.add.call_args_list]
    deletion = next(manifest for manifest in manifests if hasattr(manifest, "object_paths"))
    assert staging_path in deletion.object_paths
    assert f"dev-user/{user.id}/plan-pool-reservations/{item_id}/" in deletion.object_prefixes


@pytest.mark.asyncio
async def test_delete_rejects_active_visual_staging_reservation() -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id, plan_id, item_id = (uuid.uuid4() for _ in range(3))
    thread = _delete_thread(
        user_id=user.id,
        thread_id=thread_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=user.id)
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=None)
    asset = SimpleNamespace(
        user_id=user.id,
        status="preparing",
        upload_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        gcs_path=f"dev-user/{user.id}/plan-pool-reservations/{item_id}/visual.png",
        preview_gcs_path=None,
    )
    db = Mock()
    db.get = AsyncMock(side_effect=[plan, persona, item])
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(),
            _delete_result(rows=[asset]),
        ]
    )
    db.commit = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await routes.delete_thread(
            _request(), str(thread_id), user, db, expected_revision=thread.revision
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Project has an active upload"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_manifest_includes_edit_artifact_retention_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routes.creation_threads as routes
    from app.tasks.account_lifecycle import purge_job_storage

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id, plan_id, item_id, artifact_id = (uuid.uuid4() for _ in range(4))
    thread = _delete_thread(
        user_id=user.id,
        thread_id=thread_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=user.id)
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        clip_gcs_paths=[],
        voiceover_gcs_path=None,
    )
    retention_path = f"users/{user.id}/edit-feedback/{artifact_id}/retained.mp4"
    artifact = SimpleNamespace(id=artifact_id, creator_id=user.id, storage_path=None)
    retention = SimpleNamespace(
        artifact_id=artifact_id,
        creator_id=user.id,
        status="succeeded",
        storage_path=retention_path,
    )
    monkeypatch.setattr(purge_job_storage, "apply_async", Mock())
    db = Mock()
    db.get = AsyncMock(side_effect=[plan, persona, item])
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(),
            _delete_result(rows=[]),
            _delete_result(rows=[artifact]),
            _delete_result(rows=[retention]),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _reference_result(),
            _reference_result(),
            _delete_result(),
            _delete_result(),
        ]
    )
    db.add = Mock()
    db.commit = AsyncMock()

    response = await routes.delete_thread(
        _request(), str(thread_id), user, db, expected_revision=thread.revision
    )

    assert response.status_code == 204
    manifests = [call.args[0] for call in db.add.call_args_list]
    deletion = next(manifest for manifest in manifests if hasattr(manifest, "object_paths"))
    assert retention_path in deletion.object_paths
    assert f"users/{user.id}/edit-feedback/{artifact_id}/" in deletion.object_prefixes


@pytest.mark.asyncio
async def test_delete_filters_exact_keys_and_project_prefixes_shared_by_external_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routes.creation_threads as routes
    from app.tasks.account_lifecycle import purge_job_storage

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id, plan_id, item_id = (uuid.uuid4() for _ in range(3))
    thread = _delete_thread(
        user_id=user.id,
        thread_id=thread_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=user.id)
    shared_path = f"users/{user.id}/plan/{item_id}/shared.mp4"
    unrelated_path = f"users/{user.id}/plan/{item_id}/unrelated.mp4"
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        clip_gcs_paths=[shared_path, unrelated_path],
        clip_assignments=[],
        voiceover_gcs_path=None,
    )
    external_job = (
        shared_path,
        {"clip_paths": [], "voiceover_gcs_path": None},
    )
    monkeypatch.setattr(purge_job_storage, "apply_async", Mock())
    db = Mock()
    db.get = AsyncMock(side_effect=[plan, persona, item])
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _reference_result([external_job]),
            _reference_result(),
            _delete_result(),
            _delete_result(),
        ]
    )
    db.add = Mock()
    db.commit = AsyncMock()

    response = await routes.delete_thread(
        _request(), str(thread_id), user, db, expected_revision=thread.revision
    )

    assert response.status_code == 204
    manifests = [call.args[0] for call in db.add.call_args_list]
    deletion = next(manifest for manifest in manifests if hasattr(manifest, "object_paths"))
    assert shared_path not in deletion.object_paths
    assert unrelated_path in deletion.object_paths
    assert f"users/{user.id}/plan/{item_id}/" not in deletion.object_prefixes
    assert f"users/{user.id}/creation-threads/{thread_id}/" in deletion.object_prefixes


@pytest.mark.asyncio
async def test_delete_filters_exact_keys_and_project_prefixes_shared_by_external_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.routes.creation_threads as routes
    from app.tasks.account_lifecycle import purge_job_storage

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id, plan_id, item_id = (uuid.uuid4() for _ in range(3))
    thread = _delete_thread(
        user_id=user.id,
        thread_id=thread_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=user.id)
    shared_path = f"users/{user.id}/plan/{item_id}/shared.mp4"
    unrelated_path = f"users/{user.id}/plan/{item_id}/unrelated.mp4"
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        clip_gcs_paths=[shared_path, unrelated_path],
        clip_assignments=[],
        voiceover_gcs_path=None,
    )
    external_item = ([shared_path], [], None)
    monkeypatch.setattr(purge_job_storage, "apply_async", Mock())
    db = Mock()
    db.get = AsyncMock(side_effect=[plan, persona, item])
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _reference_result(),
            _reference_result([external_item]),
            _delete_result(),
            _delete_result(),
        ]
    )
    db.add = Mock()
    db.commit = AsyncMock()

    response = await routes.delete_thread(
        _request(), str(thread_id), user, db, expected_revision=thread.revision
    )

    assert response.status_code == 204
    manifests = [call.args[0] for call in db.add.call_args_list]
    deletion = next(manifest for manifest in manifests if hasattr(manifest, "object_paths"))
    assert shared_path not in deletion.object_paths
    assert unrelated_path in deletion.object_paths
    assert f"users/{user.id}/plan/{item_id}/" not in deletion.object_prefixes
    assert f"dev-user/{user.id}/plan-pool-reservations/{item_id}/" in deletion.object_prefixes


@pytest.mark.asyncio
async def test_delete_fails_closed_for_corrupted_current_job_linkage() -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id, plan_id, item_id, job_id, other_item_id = (uuid.uuid4() for _ in range(5))
    thread = _delete_thread(
        user_id=user.id,
        thread_id=thread_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=user.id)
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    corrupted_job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=other_item_id,
        status="succeeded",
    )
    db = Mock()
    db.get = AsyncMock(side_effect=[plan, persona, item])
    db.execute = AsyncMock(
        side_effect=[
            _delete_result(),
            _delete_result(one=thread),
            _delete_result(),
            _delete_result(rows=[]),
            _delete_result(rows=[]),
            _delete_result(rows=[corrupted_job]),
        ]
    )
    db.commit = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await routes.delete_thread(
            _request(), str(thread_id), user, db, expected_revision=thread.revision
        )

    assert exc_info.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_tombstone_is_idempotent_for_same_owner() -> None:
    import app.routes.creation_threads as routes

    user = SimpleNamespace(id=uuid.uuid4())
    thread_id = uuid.uuid4()
    tombstone = SimpleNamespace(thread_id=thread_id, creator_id=user.id)
    result = Mock()
    result.scalar_one_or_none.return_value = tombstone
    db = Mock()
    db.execute = AsyncMock(return_value=result)

    response = await routes.delete_thread(
        _request(),
        str(thread_id),
        user,
        db,
        expected_revision=999,
    )

    assert response.status_code == 204
    db.execute.assert_awaited_once()


def test_project_delete_manifest_preserves_plan_shared_input_keys() -> None:
    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    shared = f"users/{user_id}/plan/{item_id}/clip.mp4"
    output = f"jobs/{job_id}/render.mp4"
    job = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        raw_storage_path=shared,
        assembly_plan={"output_path": output},
        all_candidates={"clip_paths": [shared, output]},
    )

    paths = _project_job_storage_paths(
        job,
        [],
        [],
        user_id=user_id,
        thread_id=uuid.uuid4(),
        item_id=item_id,
    )

    assert shared not in paths
    assert output in paths


@pytest.mark.asyncio
async def test_upload_reservation_is_signed_for_opaque_media_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active")
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage, "signed_put_url", lambda path, content_type, size: "signed:" + path
    )
    reservations = Mock()
    reservations.scalars.return_value.all.return_value = []
    db = Mock()
    db.execute = AsyncMock(return_value=reservations)
    db.commit = AsyncMock()
    result = await upload_urls(
        _request(),
        str(thread.id),
        UploadBody(
            files=[
                UploadFile(
                    filename="clip.mp4",
                    content_type="video/mp4",
                    file_size_bytes=10,
                    client_upload_id="clip-1",
                )
            ]
        ),
        user,
        db,
    )
    assert result[0].media_id == "clip-1.mp4"
    assert result[0].gcs_path.endswith("/clip-1.mp4")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type", "expected_suffix"),
    [
        ("voice.m4a", "audio/x-m4a", ".m4a"),
        ("clip.MOV", "video/quicktime", ".mov"),
    ],
)
async def test_upload_reservation_preserves_canonical_render_suffix(
    filename: str,
    content_type: str,
    expected_suffix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active")
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage, "signed_put_url", lambda path, media_type, size: "signed:" + path
    )
    reservations = Mock()
    reservations.scalars.return_value.all.return_value = []
    db = Mock()
    db.execute = AsyncMock(return_value=reservations)
    db.commit = AsyncMock()
    result = await upload_urls(
        _request(),
        str(thread.id),
        UploadBody(
            files=[
                UploadFile(
                    filename=filename,
                    content_type=content_type,
                    file_size_bytes=10,
                    client_upload_id="media-1",
                )
            ]
        ),
        user,
        db,
    )
    assert result[0].media_id.endswith(expected_suffix)
    assert result[0].gcs_path.endswith(expected_suffix)


@pytest.mark.asyncio
async def test_subtitled_upload_reservation_enforces_one_clip_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        active_plan_item_id=uuid.uuid4(),
    )
    item = SimpleNamespace(edit_format="subtitled", clip_gcs_paths=["users/u/existing.mp4"])
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    db = Mock()
    db.get = AsyncMock(return_value=item)

    with pytest.raises(HTTPException, match="capped at 1 clips") as exc:
        await upload_urls(
            _request(),
            str(thread.id),
            UploadBody(
                files=[
                    UploadFile(
                        filename="clip-2.mp4",
                        content_type="video/mp4",
                        file_size_bytes=10,
                        client_upload_id="clip-2",
                    )
                ]
            ),
            user,
            db,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_attach_rejects_tampered_reserved_path(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active", revision=0)
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    with pytest.raises(Exception) as exc:
        await attach_media(
            _request(),
            str(thread.id),
            AttachBody(
                media=[MediaInput(media_id="clip-1", gcs_path="users/other/path", kind="video")],
                client_event_id="attach-1",
                expected_revision=0,
            ),
            user,
            Mock(),
        )
    assert getattr(exc.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_attach_persists_stable_mixed_media_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_job_id=None,
        active_plan_item_id=uuid.uuid4(),
        state={"media": [], "media_count": 0},
    )
    item = SimpleNamespace(
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path=None,
        audio_mode="kria",
    )
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda path: SimpleNamespace(
            size=100,
            content_type="image/jpeg" if path.endswith(".jpg") else "video/mp4",
        ),
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    media = [MediaInput(media_id="clip-1.mp4", kind="video", filename="clip.mp4")]
    await attach_media(
        _request(),
        str(thread.id),
        AttachBody(media=media, client_event_id="attach-mixed", expected_revision=0),
        user,
        db,
    )
    assert [(entry["media_id"], entry["kind"]) for entry in item.clip_assignments] == [
        ("clip-1.mp4", "video"),
    ]
    assert item.clip_gcs_paths == [_media_path(user.id, thread.id, "clip-1.mp4")]


@pytest.mark.asyncio
async def test_attach_consumes_the_matching_upload_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attaching a verified object must remove its durable PUT reservation."""

    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_job_id=None,
        active_plan_item_id=uuid.uuid4(),
        state={"media": [], "media_count": 0},
    )
    item = SimpleNamespace(
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path=None,
        audio_mode="kria",
    )
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(size=100, content_type="video/mp4"),
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    await routes.attach_media(
        _request(),
        str(thread.id),
        AttachBody(
            media=[MediaInput(media_id="clip-1.mp4", kind="video")],
            client_event_id="attach-reservation",
            expected_revision=0,
        ),
        user,
        db,
    )

    delete_statement = db.execute.await_args_list[-1].args[0]
    assert "creation_thread_upload_reservations" in str(delete_statement)
    assert "thread_id" in str(delete_statement)
    assert "media_id" in str(delete_statement)


@pytest.mark.asyncio
async def test_attach_persists_one_valid_voiceover(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_plan_item_id=uuid.uuid4(),
        state={"media": [], "media_count": 0},
    )
    item = SimpleNamespace(
        edit_format="narrated_planned",
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path=None,
        audio_mode="kria",
    )
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda path: SimpleNamespace(size=100, content_type="audio/webm"),
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    await attach_media(
        _request(),
        str(thread.id),
        AttachBody(
            media=[MediaInput(media_id="voice-1.webm", kind="audio", filename="voice.webm")],
            client_event_id="attach-voice",
            expected_revision=0,
        ),
        user,
        db,
    )

    assert item.voiceover_gcs_path == _media_path(user.id, thread.id, "voice-1.webm")
    assert item.audio_mode == "voiceover"


@pytest.mark.asyncio
async def test_attach_rejects_media_already_present_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_plan_item_id=uuid.uuid4(),
        state={"media": [{"media_id": "clip-1.mp4", "kind": "video"}], "media_count": 1},
    )
    item = SimpleNamespace(current_job_id=None)
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    metadata = Mock()
    monkeypatch.setattr(routes.storage, "object_metadata", metadata)
    db = Mock()
    db.get = AsyncMock(return_value=item)

    with pytest.raises(HTTPException, match="already attached") as exc:
        await attach_media(
            _request(),
            str(thread.id),
            AttachBody(
                media=[MediaInput(media_id="clip-1.mp4", kind="video")],
                client_event_id="attach-again",
                expected_revision=0,
            ),
            user,
            db,
        )
    assert exc.value.status_code == 409
    metadata.assert_not_called()


@pytest.mark.asyncio
async def test_subtitled_attachment_enforces_one_clip_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_plan_item_id=uuid.uuid4(),
        state={"media": [{"media_id": "clip-1.mp4", "kind": "video"}], "media_count": 1},
    )
    item = SimpleNamespace(
        edit_format="subtitled",
        current_job_id=None,
        clip_gcs_paths=["users/u/clip-1.mp4"],
        clip_assignments=[],
        voiceover_gcs_path=None,
    )
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda path: SimpleNamespace(size=100, content_type="video/mp4"),
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)

    with pytest.raises(HTTPException, match="capped at 1 clips") as exc:
        await attach_media(
            _request(),
            str(thread.id),
            AttachBody(
                media=[MediaInput(media_id="clip-2.mp4", kind="video")],
                client_event_id="attach-second-subtitled",
                expected_revision=0,
            ),
            user,
            db,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_attach_rejects_images_from_primary_creation_media() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_job_id=None,
        active_plan_item_id=uuid.uuid4(),
        state={"media": [], "media_count": 0},
    )
    import app.routes.creation_threads as routes

    original_load = routes._load
    original_duplicate = routes._duplicate
    routes._load = AsyncMock(return_value=thread)
    routes._duplicate = AsyncMock(return_value=None)
    try:
        with pytest.raises(HTTPException, match="Visuals pool"):
            await routes.attach_media(
                _request(),
                str(thread.id),
                AttachBody(
                    media=[MediaInput(media_id="still-1.jpg", kind="image", filename="still.jpg")],
                    client_event_id="attach-image",
                    expected_revision=0,
                ),
                user,
                Mock(),
            )
    finally:
        # Keep this isolated test from mutating the imported route globally.
        routes._load = original_load
        routes._duplicate = original_duplicate


@pytest.mark.asyncio
async def test_detail_poll_is_read_only_and_revision_stable() -> None:
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        status="active",
        revision=4,
        state={"media_count": 1},
        content_plan_id=None,
        active_plan_item_id=None,
        active_creator_agent_session_id=None,
        active_job_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    event_result = Mock()
    event_result.scalars.return_value.all.return_value = []
    db = Mock()
    db.get = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=event_result)
    db.commit = AsyncMock()
    first = await _response(db, thread)
    second = await _response(db, thread)
    assert first.revision == second.revision == 4
    assert first.events == second.events == []
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_retry_enqueue_failure_preserves_newer_thread_projection() -> None:
    user_id = uuid.uuid4()
    thread_id, job_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    render_generation_id = "generation-old"
    stale_thread = SimpleNamespace(
        id=thread_id,
        creator_id=user_id,
        active_job_id=job_id,
        state={
            "generation": {
                "status": "rendering",
                "job_id": str(job_id),
                "variant_id": "song_text",
                "render_generation_id": render_generation_id,
            }
        },
    )
    newer_state = {
        "selected_variant_id": "original_text",
        "generation": {
            "status": "rendering",
            "job_id": str(job_id),
            "variant_id": "song_text",
            "render_generation_id": "generation-new",
        },
    }
    live_thread = SimpleNamespace(
        id=thread_id,
        creator_id=user_id,
        active_job_id=job_id,
        state=newer_state,
    )
    query_result = Mock()
    query_result.scalar_one_or_none.return_value = live_thread
    session = SimpleNamespace(
        target_job_id=job_id,
        target_variant_id="song_text",
        target_generation_id="generation-new",
        status="rendering",
    )
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_generation_id": "generation-new",
                    "render_status": "rendering",
                }
            ]
        }
    )
    db = Mock()
    db.execute = AsyncMock(return_value=query_result)
    db.get = AsyncMock(side_effect=[session, job])
    db.rollback = AsyncMock()
    db.commit = AsyncMock()

    await _record_partial_variant_retry_enqueue_failure(
        db,
        stale_thread,
        session_id,
        job_id,
        "song_text",
        render_generation_id,
        RuntimeError("broker unavailable"),
    )

    assert live_thread.state == newer_state
    assert job.assembly_plan["variants"][0]["render_status"] == "rendering"
    assert session.status == "rendering"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_response_fails_closed_for_cross_user_linked_job() -> None:
    user_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    item_id, plan_id, session_id, job_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    plan = SimpleNamespace(id=plan_id, user_id=user_id)
    session = SimpleNamespace(
        id=session_id,
        creator_id=user_id,
        plan_item_id=item_id,
        target_job_id=job_id,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=other_user_id,
        content_plan_item_id=item_id,
    )
    db = Mock()
    db.get = AsyncMock(side_effect=[item, plan, session, job])

    with pytest.raises(HTTPException) as exc_info:
        await _response(db, thread)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_detail_repairs_missing_job_projection_from_exact_creator_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id, session_id, job_id, plan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=9,
        state={"edit_format": "montage"},
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=None,
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        target_job_id=job_id,
        target_variant_id=None,
        target_generation_id=None,
        ownership_epoch=0,
        status="rendering",
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        mode="content_plan",
        status="variants_ready",
        assembly_plan={"variants": [{"variant_id": "original_text", "render_status": "ready"}]},
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, ownership_epoch=0)
    db = Mock()
    db.get = AsyncMock(side_effect=[session, item, job, plan, session, session, job])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    output = await get_thread(str(thread.id), user, db)

    assert output is thread
    assert thread.active_job_id == job_id
    assert thread.state["generation"] == {"status": "ready", "job_id": str(job_id)}
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_projection_repair_locks_authoritative_graph_before_session() -> None:
    """Projection repair must match Creator Agent's PlanItem -> Job -> session order."""

    user = SimpleNamespace(id=uuid.uuid4())
    item_id, session_id, job_id, plan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        active_job_id=None,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        state={},
    )
    session = SimpleNamespace(
        creator_id=user.id,
        plan_item_id=item_id,
        target_job_id=job_id,
        target_variant_id=None,
        target_generation_id=None,
        ownership_epoch=0,
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        mode="content_plan",
        status="processing",
        assembly_plan={"variants": []},
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, ownership_epoch=0)
    db = Mock()
    db.get = AsyncMock(side_effect=[session, item, job, plan, session])

    import app.routes.creation_threads as routes

    assert await routes._repair_missing_thread_job_projection(db, thread, user) is True

    calls = db.get.await_args_list
    assert [call.args[0] for call in calls] == [
        routes.CreatorAgentSession,
        routes.PlanItem,
        routes.Job,
        routes.ContentPlan,
        routes.CreatorAgentSession,
    ]
    assert calls[0].kwargs == {}
    assert calls[1].kwargs["with_for_update"] is True
    assert calls[2].kwargs["with_for_update"] is True
    assert calls[4].kwargs["with_for_update"] is True
    assert thread.active_job_id == job_id


@pytest.mark.asyncio
async def test_detail_reconciles_failed_guided_planning_before_a_job_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed guided proposal cannot leave chat stuck before a Job exists."""

    user = SimpleNamespace(id=uuid.uuid4())
    session_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=session_id,
        active_job_id=None,
    )
    session = SimpleNamespace(id=session_id, status="executing", target_job_id=None)
    db = Mock()
    db.get = AsyncMock(return_value=session)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes,
        "_repair_missing_thread_job_projection",
        AsyncMock(return_value=False),
    )
    reconcile = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "reconcile_render_state", reconcile)
    sync_agent = AsyncMock()
    monkeypatch.setattr(routes, "_sync_agent", sync_agent)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    output = await get_thread(str(thread.id), user, db)

    assert output is thread
    reconcile.assert_awaited_once_with(db, session)
    sync_agent.assert_awaited_once_with(db, thread)
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(thread)


@pytest.mark.asyncio
async def test_detail_projects_job_discovered_during_creator_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One detail read both discovers and projects an async guided Job."""

    user = SimpleNamespace(id=uuid.uuid4())
    session_id, job_id = uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=session_id,
        active_job_id=None,
    )
    session = SimpleNamespace(id=session_id, status="executing", target_job_id=None)
    db = Mock()
    db.get = AsyncMock(return_value=session)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))

    async def repair(_db, target_thread, _user):
        if session.target_job_id is None:
            return False
        target_thread.active_job_id = session.target_job_id
        return True

    repair_projection = AsyncMock(side_effect=repair)
    monkeypatch.setattr(routes, "_repair_missing_thread_job_projection", repair_projection)

    async def reconcile(_db, _session):
        session.target_job_id = job_id
        return True

    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock(side_effect=reconcile))
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    output = await get_thread(str(thread.id), user, db)

    assert output.active_job_id == job_id
    assert repair_projection.await_count == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_repairs_missing_job_projection_before_reopening_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed Creator turn can be retried after its thread projection lags."""

    user = SimpleNamespace(id=uuid.uuid4())
    item_id, session_id, job_id, plan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=9,
        state={"edit_format": "montage", "media_count": 1},
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=None,
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        target_job_id=job_id,
        target_variant_id=None,
        target_generation_id=None,
        ownership_epoch=0,
        status="rendering",
        revision=4,
        render_attempts=1,
        max_render_attempts=2,
        active_plan={"version": 1, "plan_hash": "x" * 64, "edit_format": "montage"},
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        mode="content_plan",
        status="variants_ready",
        assembly_plan={"variants": [{"variant_id": "original_text", "render_status": "ready"}]},
    )
    plan = SimpleNamespace(id=plan_id, user_id=user.id, ownership_epoch=0)
    result = SimpleNamespace(id=str(session_id), current_job_id=str(job_id))
    db = Mock()
    # Repair reads session/item/job/plan, then retry reads the repaired session/job.
    db.get = AsyncMock(side_effect=[session, item, job, plan, session, session, job])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    async def settle_session(_db, target_session):
        target_session.status = "awaiting_confirmation"

    monkeypatch.setattr(routes, "reconcile_render_state", settle_session)
    confirm = AsyncMock(return_value=result)
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", confirm)

    output = await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="retry",
            payload={},
            client_action_id="retry-after-projection-gap",
            expected_revision=9,
        ),
        user,
        db,
    )

    assert output is thread
    assert thread.active_job_id == job_id
    confirm.assert_awaited_once()
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_projection_repair_rejects_target_from_wrong_plan_owner() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id, session_id, job_id, plan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        active_job_id=None,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
    )
    session = SimpleNamespace(
        creator_id=user.id,
        plan_item_id=item_id,
        target_job_id=job_id,
        ownership_epoch=0,
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        mode="content_plan",
    )
    plan = SimpleNamespace(id=plan_id, user_id=uuid.uuid4(), ownership_epoch=0)
    db = Mock()
    db.get = AsyncMock(side_effect=[session, item, job, plan, session])
    import app.routes.creation_threads as routes

    repaired = await routes._repair_missing_thread_job_projection(db, thread, user)

    assert repaired is False
    assert thread.active_job_id is None


@pytest.mark.asyncio
async def test_list_returns_lightweight_project_summaries() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=3,
        state={"media_count": 2},
        content_plan_id=None,
        active_plan_item_id=None,
        active_creator_agent_session_id=None,
        active_job_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [thread]
    db = Mock()
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    output = await list_threads(user, db, include_archived=False, limit=20)
    assert len(output) == 1
    assert output[0].events == []
    assert output[0].job is None


@pytest.mark.asyncio
async def test_list_summaries_batch_active_job_and_agent_status_for_safe_actions() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    job_id, session_id = uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        title="Rendering harbor",
        status="active",
        revision=3,
        state={},
        content_plan_id=None,
        active_plan_item_id=None,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        current_phase="assemble",
        failure_reason=None,
        assembly_plan={"variants": [{"variant_id": "one", "render_status": "rendering"}]},
    )
    thread_result = Mock()
    thread_result.scalars.return_value.all.return_value = [thread]
    job_result = Mock()
    job_result.scalars.return_value.all.return_value = [job]
    session_result = Mock()
    session_result.all.return_value = [(session_id, "rendering")]
    db = Mock()
    db.execute = AsyncMock(side_effect=[thread_result, job_result, session_result])

    output = await list_threads(user, db, include_archived=False, limit=20)

    assert output[0].job == {
        "id": str(job_id),
        "status": "processing",
        "current_phase": "assemble",
        "failure_reason": None,
        "variants": [{"variant_id": "one", "render_status": "rendering"}],
    }
    assert output[0].creator_agent == {"status": "rendering"}


@pytest.mark.asyncio
async def test_action_idempotency_rejects_changed_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active", revision=2)
    duplicate = SimpleNamespace(
        event_type="action_set_intent",
        payload={"action": "set_intent", "intent": "old"},
    )
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=duplicate))
    with pytest.raises(Exception) as exc:
        await routes.action_thread(
            _request(),
            str(thread.id),
            routes.ActionBody(
                action="set_intent",
                payload={"intent": "new"},
                client_action_id="a-1",
                expected_revision=2,
            ),
            user,
            Mock(),
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_message_idempotency_rejects_non_message_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active", revision=2)
    duplicate = SimpleNamespace(event_type="revision_queued", content="same text")
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=duplicate))
    with pytest.raises(Exception) as exc:
        await routes.message_thread(
            _request(),
            str(thread.id),
            routes.MessageBody(message="same text", client_event_id="shared", expected_revision=2),
            user,
            Mock(),
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_select_variant_requires_authoritative_ready_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=1,
        active_job_id=uuid.uuid4(),
        state={},
    )
    job = SimpleNamespace(
        id=thread.active_job_id,
        assembly_plan={"variants": [{"variant_id": "ready-1", "render_status": "ready"}]},
    )
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    db = Mock()
    db.get = AsyncMock(return_value=job)
    with pytest.raises(Exception) as exc:
        await routes.action_thread(
            _request(),
            str(thread.id),
            routes.ActionBody(
                action="select_variant",
                payload={"variant_id": "not-a-real-variant"},
                client_action_id="variant-1",
                expected_revision=1,
            ),
            user,
            db,
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_message_without_prerequisites_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_job_id=None,
        active_creator_agent_session_id=None,
        state={},
    )
    db = Mock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    agent = AsyncMock()
    monkeypatch.setattr(routes, "_agent_message", agent)

    output = await routes.message_thread(
        _request(),
        str(thread.id),
        routes.MessageBody(message="make it bold", client_event_id="m-inert", expected_revision=0),
        user,
        db,
    )
    assert output is thread
    agent.assert_not_awaited()
    assert append.await_count == 2
    assert append.await_args_list[1].kwargs["event_type"] == "format_prompt"


@pytest.mark.asyncio
async def test_revision_action_replay_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active", revision=3)
    duplicate = SimpleNamespace(
        event_type="revision_requested",
        payload={"action": "revise", "intent": "tighter opening"},
    )
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=duplicate))
    response = AsyncMock(return_value=thread)
    monkeypatch.setattr(routes, "_response", response)
    db = Mock()
    db.rollback = AsyncMock()
    result = await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="revise",
            payload={"intent": "tighter opening"},
            client_action_id="revision-1",
            expected_revision=3,
        ),
        user,
        db,
    )
    assert result is thread
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_archive_rejects_reuse_of_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=user.id, status="active", revision=3)
    duplicate = SimpleNamespace(event_type="user_message", payload={})
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=duplicate))
    with pytest.raises(Exception) as exc:
        await routes.archive_thread(
            _request(),
            str(thread.id),
            routes.ArchiveBody(client_event_id="shared-id", expected_revision=3),
            user,
            Mock(),
        )
    assert getattr(exc.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_message_binds_existing_creator_session(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    session_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_job_id=None,
        active_creator_agent_session_id=session_id,
        state={"edit_format": "montage", "media_count": 1},
    )
    db = Mock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=session_id, revision=7))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    agent = AsyncMock(return_value=thread)
    monkeypatch.setattr(routes, "_agent_message", agent)
    result = await routes.message_thread(
        _request(),
        str(thread.id),
        routes.MessageBody(message="make it calmer", client_event_id="m-1", expected_revision=0),
        user,
        db,
    )
    assert result is thread
    agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_dispatch_links_authoritative_job(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=session_id,
        active_job_id=None,
        state={"edit_format": "montage"},
    )
    session = SimpleNamespace(
        id=session_id,
        revision=4,
        active_plan={"version": 1, "plan_hash": "x" * 64, "edit_format": "montage"},
    )
    result = SimpleNamespace(id=str(session_id), current_job_id=str(job_id))
    db = Mock()
    db.get = AsyncMock(return_value=session)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    confirm = AsyncMock(return_value=result)
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", confirm)
    output = await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="confirm_generation",
            payload={},
            client_action_id="confirm-1",
            expected_revision=0,
        ),
        user,
        db,
    )
    assert output is thread
    assert thread.active_job_id == job_id
    confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_reopens_terminal_render_with_existing_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    session_id = uuid.uuid4()
    old_job_id = uuid.uuid4()
    new_job_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=2,
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=session_id,
        active_job_id=old_job_id,
        state={"edit_format": "montage", "media_count": 1},
    )
    session = SimpleNamespace(
        id=session_id,
        status="failed",
        revision=4,
        render_attempts=1,
        max_render_attempts=2,
        active_plan={"version": 1, "plan_hash": "x" * 64, "edit_format": "montage"},
    )
    old_job = SimpleNamespace(id=old_job_id, status="processing_failed")
    result = SimpleNamespace(id=str(session_id), current_job_id=str(new_job_id))
    db = Mock()
    db.get = AsyncMock(side_effect=[session, old_job])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock())
    monkeypatch.setattr(routes, "_sync_agent", AsyncMock())
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    confirm = AsyncMock(return_value=result)
    monkeypatch.setattr(routes.creator_agent, "confirm_creator_plan_controller", confirm)

    output = await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="retry",
            payload={},
            client_action_id="retry-1",
            expected_revision=2,
        ),
        user,
        db,
    )
    assert output is thread
    assert session.status == "awaiting_confirmation"
    confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_partial_render_dispatches_only_failed_variant(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    thread_id = uuid.uuid4()
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=thread_id,
        creator_id=user.id,
        status="active",
        revision=2,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        state={"edit_format": "montage", "media_count": 2},
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        target_job_id=job_id,
        target_variant_id="song_text",
        target_generation_id="old-generation",
        ownership_epoch=0,
        status="awaiting_feedback",
    )
    failed = {
        "variant_id": "song_text",
        "render_status": "failed",
        "render_generation_id": "old-generation",
    }
    ready = {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "sibling-generation",
    }
    job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        mode="content_plan",
        status="variants_ready_partial",
        current_phase=None,
        started_at=None,
        assembly_plan={"variants": [failed, ready]},
    )
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    db = Mock()
    db.get = AsyncMock(side_effect=[session, job, item])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes
    from app.tasks.generative_build import regenerate_generative_variant

    delay = Mock()
    monkeypatch.setattr(regenerate_generative_variant, "delay", delay)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    output = await action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="retry",
            payload={"variant_id": "song_text"},
            client_action_id="retry-variant-1",
            expected_revision=2,
        ),
        user,
        db,
    )

    assert output is thread
    assert failed["render_status"] == "rendering"
    assert failed["render_generation_id"] != "old-generation"
    assert ready["render_status"] == "ready"
    assert session.status == "rendering"
    assert session.target_variant_id == "song_text"
    assert session.target_generation_id == failed["render_generation_id"]
    delay.assert_called_once_with(
        str(job_id), "song_text", render_gen_id=failed["render_generation_id"]
    )
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_retry_partial_render_rejects_sibling_in_flight(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id, session_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        state={"edit_format": "montage"},
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        target_job_id=job_id,
        target_variant_id="failed",
        target_generation_id="old",
        ownership_epoch=0,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        mode="content_plan",
        status="variants_ready_partial",
        current_phase=None,
        started_at=None,
        assembly_plan={
            "variants": [
                {"variant_id": "failed", "render_status": "failed"},
                {"variant_id": "sibling", "render_status": "rendering"},
            ]
        },
    )
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    db = Mock()
    db.get = AsyncMock(side_effect=[session, job, item])
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))

    with pytest.raises(routes.HTTPException) as exc_info:
        await action_thread(
            _request(),
            str(thread.id),
            routes.ActionBody(
                action="retry",
                payload={"variant_id": "failed"},
                client_action_id="retry-variant-2",
                expected_revision=0,
            ),
            user,
            db,
        )
    assert exc_info.value.status_code == 409
    assert job.assembly_plan["variants"][0]["render_status"] == "failed"


@pytest.mark.asyncio
async def test_retry_partial_render_broker_failure_is_retryable(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id, session_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=0,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        state={"edit_format": "montage"},
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        target_job_id=job_id,
        target_variant_id="failed",
        target_generation_id="old",
        ownership_epoch=0,
        status="awaiting_feedback",
    )
    target = {
        "variant_id": "failed",
        "render_status": "failed",
        "render_generation_id": "old",
    }
    job = SimpleNamespace(
        id=job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        mode="content_plan",
        status="variants_ready_partial",
        current_phase=None,
        started_at=None,
        assembly_plan={"variants": [target]},
    )
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    db = Mock()
    db.get = AsyncMock(side_effect=[session, job, item, session, job])
    thread_result = Mock()
    thread_result.scalar_one_or_none.return_value = thread
    db.execute = AsyncMock(return_value=thread_result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes
    from app.tasks.generative_build import regenerate_generative_variant

    monkeypatch.setattr(
        regenerate_generative_variant,
        "delay",
        Mock(side_effect=RuntimeError("redis unavailable")),
    )
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    with pytest.raises(routes.HTTPException) as exc_info:
        await routes.action_thread(
            _request(),
            str(thread.id),
            routes.ActionBody(
                action="retry",
                payload={"variant_id": "failed"},
                client_action_id="retry-variant-broker-failure",
                expected_revision=0,
            ),
            user,
            db,
        )

    assert exc_info.value.status_code == 503
    assert target["render_status"] == "failed"
    assert target["error_class"] == "retry_enqueue_failed"
    assert session.status == "failed"
    assert db.rollback.await_count == 1
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_ready_revision_preparation_clears_pending_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        revision=2,
        active_plan_item_id=uuid.uuid4(),
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        state={"edit_format": "montage", "media_count": 1},
    )
    session = SimpleNamespace(
        id=session_id,
        status="awaiting_feedback",
        revision=4,
        active_plan={"version": 1, "plan_hash": "x" * 64},
    )
    job = SimpleNamespace(id=job_id, status="variants_ready")
    db = Mock()
    db.get = AsyncMock(side_effect=[session, job])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "reconcile_render_state", AsyncMock())
    monkeypatch.setattr(routes, "_agent_message", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    await routes.action_thread(
        _request(),
        str(thread.id),
        routes.ActionBody(
            action="revise",
            payload={"intent": "tighten the hook"},
            client_action_id="revision-prepare-1",
            expected_revision=2,
        ),
        user,
        db,
    )
    assert "pending_revision_intent" not in thread.state
    assert thread.state["prepared_revision_job_id"] == str(job_id)
