"""Route tests for /personas (content-plan Phase 3).

Mock-DB (UUID/JSONB models don't map to SQLite). Cover the auth gate, the
create→enqueue flow, GET-when-absent, and PATCH ownership isolation.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _fake_user(uid: uuid.UUID | None = None) -> MagicMock:
    u = MagicMock()
    u.id = uid or uuid.uuid4()
    u.onboarding_status = "pending"
    return u


def _async_db(scalar_result=None) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=scalar_result))
    )
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_create_persona_requires_auth(client: TestClient) -> None:
    """No X-User-Id / Authorization → 401 (plan routes are strict)."""
    app.dependency_overrides[get_db] = lambda: _async_db()
    resp = client.post("/personas", json={"work": "barista"})
    assert resp.status_code == 401


def test_create_persona_enqueues_generation(client: TestClient) -> None:
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=None)

    with patch("app.tasks.persona_build.generate_persona") as task:
        task.delay = MagicMock()
        resp = client.post("/personas", json={"work": "barista", "hobbies": "lifting"})

    assert resp.status_code == 201
    assert resp.json()["persona_status"] == "generating"
    task.delay.assert_called_once()


def test_get_persona_404_when_absent(client: TestClient) -> None:
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=None)

    resp = client.get("/personas")
    assert resp.status_code == 404


def test_patch_persona_404_for_other_users_persona(client: TestClient) -> None:
    """A persona owned by a different user must not be editable (no leak)."""
    user = _fake_user()
    other_persona = MagicMock()
    other_persona.id = uuid.uuid4()
    other_persona.user_id = uuid.uuid4()  # different owner

    db = _async_db()
    db.get = AsyncMock(return_value=other_persona)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/personas/{other_persona.id}", json={"tone": "edgy"})
    assert resp.status_code == 404
