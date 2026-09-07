from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.routes.memory import _compatibility_projection
from app.services.creator_direction import ProjectDirectionNotFound, StaleRevision

client = TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _override_user_and_db(db: AsyncMock) -> SimpleNamespace:
    user = SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return user


def test_memory_routes_require_authentication() -> None:
    response = client.get("/me/memory")

    assert response.status_code == 401


def test_memory_mutation_disabled_response_is_stable() -> None:
    db = AsyncMock()
    _override_user_and_db(db)
    with patch("app.routes.memory.settings.creator_memory_enabled", False):
        response = client.post(
            "/me/memory/items",
            json={
                "instruction": "Always use Inter font",
                "category": "video_style",
                "enforcement": "constraint",
                "expected_revision": 0,
                "idempotency_key": "disabled-create",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "creator_memory_disabled"
    assert response.json()["detail"]["retryable"] is True


def test_memory_mutation_preserves_stale_revision_error_code() -> None:
    db = AsyncMock()
    _override_user_and_db(db)
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch(
            "app.routes.memory.service.create_item",
            AsyncMock(side_effect=StaleRevision("memory revision is stale")),
        ),
    ):
        response = client.post(
            "/me/memory/items",
            json={
                "instruction": "Always use Inter font",
                "category": "video_style",
                "enforcement": "constraint",
                "expected_revision": 3,
                "idempotency_key": "stale-create",
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["retryable"] is True
    db.rollback.assert_awaited_once()


def test_accept_requires_a_suggested_item_transition() -> None:
    item_id = uuid.uuid4()
    db = AsyncMock()
    _override_user_and_db(db)
    operation = SimpleNamespace(
        id=uuid.uuid4(),
        item_id=item_id,
        resulting_revision=4,
        undo_expires_at=None,
    )
    mutation = AsyncMock(return_value=operation)
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch("app.routes.memory.service.set_item_state", mutation),
    ):
        response = client.post(
            f"/me/memory/items/{item_id}/accept",
            json={"expected_revision": 3, "idempotency_key": "accept-suggestion"},
        )

    assert response.status_code == 200
    assert mutation.await_args.kwargs["required_state"] == "suggested"


def test_missing_or_non_owner_project_override_is_owner_safe_404() -> None:
    thread_id = uuid.uuid4()
    db = AsyncMock()
    _override_user_and_db(db)
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch(
            "app.routes.memory.service.set_override",
            AsyncMock(side_effect=ProjectDirectionNotFound("creation thread not found")),
        ),
    ):
        response = client.post(
            f"/creation-threads/{thread_id}/direction-overrides",
            json={
                "instruction": "Use Inter for this project",
                "normalized_key": "font_family",
                "structured_value": {"font_family": "Inter"},
                "expected_revision": 0,
                "idempotency_key": "missing-thread",
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "project_direction_not_found"


def test_project_override_success_returns_canonical_operation_revision() -> None:
    thread_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=thread_id,
        normalized_key="font_family",
        instruction="Use Inter for this project",
        structured_value={"font_family": "Inter"},
        revision=7,  # ProjectDirectionMutation exposes the canonical revision.
    )
    operation = SimpleNamespace(
        id=uuid.uuid4(),
        resulting_revision=7,
        undo_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        prior_state={"_receipt": {"enabled": True, "applied_count": 1}},
    )
    execute_result = MagicMock()
    execute_result.scalar_one.return_value = operation
    db = AsyncMock()
    db.execute.return_value = execute_result
    _override_user_and_db(db)
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch("app.routes.memory.service.set_override", AsyncMock(return_value=row)),
    ):
        response = client.post(
            f"/creation-threads/{thread_id}/direction-overrides",
            json={
                "instruction": row.instruction,
                "normalized_key": row.normalized_key,
                "structured_value": row.structured_value,
                "expected_revision": 6,
                "idempotency_key": "override-success",
            },
        )

    assert response.status_code == 200
    assert response.json()["revision"] == 7
    assert response.json()["operation_id"] == str(operation.id)
    assert response.json()["undo_expires_at"] is not None


def test_project_override_undo_refreshes_the_effective_project_receipt() -> None:
    thread_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    original = SimpleNamespace(
        operation_kind="set_override",
        prior_state={"_result": {"thread_id": str(thread_id)}},
    )
    undone = SimpleNamespace(
        id=uuid.uuid4(),
        item_id=None,
        resulting_revision=8,
        undo_expires_at=None,
        prior_state={},
    )
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = original
    db.execute.return_value = execute_result
    user = _override_user_and_db(db)
    refresh = AsyncMock(return_value={"enabled": True, "applied_count": 0})
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch("app.routes.memory.service.undo", AsyncMock(return_value=undone)),
        patch("app.routes.memory._refresh_project_direction", refresh),
    ):
        response = client.post(
            f"/me/memory/operations/{operation_id}/undo",
            json={"expected_revision": 7, "idempotency_key": "undo-project-override"},
        )

    assert response.status_code == 200
    assert response.json()["direction_receipt"] == {"enabled": True, "applied_count": 0}
    refresh.assert_awaited_once_with(db, user_id=user.id, thread_id=thread_id)


def test_project_override_undo_replay_returns_the_original_receipt_without_refresh() -> None:
    operation_id = uuid.uuid4()
    original = SimpleNamespace(operation_kind="set_override", prior_state={})
    stored_receipt = {"enabled": True, "applied_count": 0, "memory_revision": 8}
    undone = SimpleNamespace(
        id=uuid.uuid4(),
        item_id=None,
        resulting_revision=8,
        undo_expires_at=None,
        prior_state={"operation_id": str(operation_id), "_receipt": stored_receipt},
    )
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = original
    db.execute.return_value = execute_result
    _override_user_and_db(db)
    refresh = AsyncMock()
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch("app.routes.memory.service.undo", AsyncMock(return_value=undone)),
        patch("app.routes.memory._refresh_project_direction", refresh),
    ):
        response = client.post(
            f"/me/memory/operations/{operation_id}/undo",
            json={"expected_revision": 7, "idempotency_key": "undo-project-override"},
        )

    assert response.status_code == 200
    assert response.json()["direction_receipt"] == stored_receipt
    refresh.assert_not_awaited()


def test_memory_item_update_does_not_forward_compatibility_metadata() -> None:
    item_id = uuid.uuid4()
    operation = SimpleNamespace(
        id=uuid.uuid4(), item_id=item_id, resulting_revision=3, undo_expires_at=None
    )
    db = AsyncMock()
    _override_user_and_db(db)
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch("app.routes.memory.service.update_item", AsyncMock(return_value=operation)) as update,
    ):
        response = client.patch(
            f"/me/memory/items/{item_id}",
            json={
                "instruction": "Use Inter",
                "category": "video_style",
                "enforcement": "default",
                "expected_revision": 2,
                "idempotency_key": "update-without-compatibility-metadata",
            },
        )

    assert response.status_code == 200
    assert "compatibility_key" not in update.await_args.kwargs


def test_compatibility_create_uses_schema_safe_reserved_profile_key() -> None:
    operation = SimpleNamespace(
        id=uuid.uuid4(), item_id=uuid.uuid4(), resulting_revision=3, undo_expires_at=None
    )
    db = AsyncMock()
    _override_user_and_db(db)
    with (
        patch("app.routes.memory.settings.creator_memory_enabled", True),
        patch("app.routes.memory.service.create_item", AsyncMock(return_value=operation)) as create,
    ):
        response = client.post(
            "/me/memory/items",
            json={
                "instruction": "Always make founder stories",
                "category": "content",
                "enforcement": "constraint",
                "compatibility_key": "summary",
                "expected_revision": 2,
                "idempotency_key": "compatibility-summary",
            },
        )

    assert response.status_code == 201
    assert create.await_args.kwargs["normalized_key"] == "creator_profile_summary"
    assert create.await_args.kwargs.get("source_kind", "profile") == "profile"


def test_compatibility_projection_hides_replaced_profile_fields() -> None:
    persona = SimpleNamespace(
        persona={
            "summary": "Founder stories",
            "tone": "Warm",
            "posting_cadence": "Weekly",
            "content_pillars": ["Travel"],
        }
    )

    assert _compatibility_projection(persona, replaced_keys={"summary", "cadence"}) == {
        "tone": "Warm",
        "content_pillars": ["Travel"],
    }


def test_admin_health_returns_only_safe_aggregates() -> None:
    db = AsyncMock()
    status_result = MagicMock()
    status_result.all.return_value = [("pending", 2), ("dead", 1)]
    version_result = MagicMock()
    version_result.all.return_value = [(1, 3)]
    code_result = MagicMock()
    code_result.all.return_value = [("TimeoutError", 1)]
    oldest_result = MagicMock()
    oldest_result.scalar_one_or_none.return_value = datetime.now(UTC) - timedelta(seconds=30)
    db.execute = AsyncMock(side_effect=[status_result, version_result, code_result, oldest_result])
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.routes.admin.settings.admin_api_key", "admin-test"):
        response = client.get(
            "/admin/creator-memory/health",
            headers={"X-Admin-Token": "admin-test"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"dead": 1, "pending": 2}
    assert body["payload_versions"] == {"1": 3}
    assert body["result_codes"] == {"TimeoutError": 1}
    assert "instruction" not in str(body).casefold()
    assert "source_message" not in body


def test_admin_retry_is_idempotent_for_pending_row() -> None:
    row = SimpleNamespace(id=uuid.uuid4(), status="pending", attempts=2)
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute.return_value = result
    app.dependency_overrides[get_db] = lambda: db

    with (
        patch("app.routes.admin.settings.admin_api_key", "admin-test"),
        patch("app.routes.admin_creator_memory.settings.creator_memory_enabled", True),
    ):
        response = client.post(
            f"/admin/creator-memory/outbox/{row.id}/retry",
            headers={"X-Admin-Token": "admin-test"},
        )

    assert response.status_code == 200
    assert response.json() == {"id": str(row.id), "status": "pending", "attempts": 2}
    db.commit.assert_not_awaited()
