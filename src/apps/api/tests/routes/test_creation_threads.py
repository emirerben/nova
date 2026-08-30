"""Fast contract tests for creation-thread route guardrails."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.agents._schemas.persona import Persona as PersonaSchema
from app.config import settings
from app.models import Persona as PersonaRow
from app.routes.creation_threads import (
    ArchiveBody,
    AttachBody,
    MediaInput,
    MessageBody,
    UploadBody,
    UploadFile,
    _available_formats,
    _client_id,
    _creator_agent_projection,
    _load,
    _media_path,
    _project,
    _response,
    action_thread,
    archive_thread,
    attach_media,
    get_thread,
    list_threads,
    message_thread,
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


def test_chat_first_backend_and_creator_defaults_are_on() -> None:
    assert settings.creation_threads_enabled is True


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
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


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
        Mock(),
    )
    assert result[0].media_id == "clip-1.mp4"
    assert result[0].gcs_path.endswith("/clip-1.mp4")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type", "expected_suffix"),
    [
        ("photo.HEIC", "image/heic", ".heic"),
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
        Mock(),
    )
    assert result[0].media_id.endswith(expected_suffix)
    assert result[0].gcs_path.endswith(expected_suffix)


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
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    media = [
        MediaInput(media_id="clip-1.mp4", kind="video", filename="clip.mp4"),
        MediaInput(media_id="still-1.jpg", kind="image", filename="still.jpg"),
    ]
    await attach_media(
        _request(),
        str(thread.id),
        AttachBody(media=media, client_event_id="attach-mixed", expected_revision=0),
        user,
        db,
    )
    assert [(entry["media_id"], entry["kind"]) for entry in item.clip_assignments] == [
        ("clip-1.mp4", "video"),
        ("still-1.jpg", "image"),
    ]


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
    db.get = AsyncMock(side_effect=[session, item, job, plan, session, job])
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
    db.get = AsyncMock(side_effect=[session, item, job, plan, session, job])
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
    db.get = AsyncMock(side_effect=[session, item, job, plan])
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
