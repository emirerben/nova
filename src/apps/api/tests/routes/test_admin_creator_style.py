"""Tests for /admin/creator-style-assignments — the Smart Captions rollout API.

The v0.11.0.0 review flagged that assignment rows had no write surface; this
router is it. Mock-DB tests (no Postgres needed) covering the upsert contract,
validation walls, and the admin gate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import CreatorStyleAssignment

ADMIN_TOKEN = "test-admin-token"
BASE = "/admin/creator-style-assignments"


def _user(email: str = "nova.creativevideo@gmail.com") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email=email)


def _db_returning_user(user) -> AsyncMock:
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = user
    db.execute.return_value = execute_result
    db.get.return_value = None

    async def _refresh(obj):  # updated_at is DB-generated; fake it post-commit
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = datetime.now(UTC)

    db.refresh.side_effect = _refresh
    return db


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_TOKEN, raising=False)
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _override_db(db) -> None:
    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db


def test_requires_admin_token(client) -> None:
    payload = {"email": "x@y.z", "preset_id": "cigdem", "preset_version": "v2"}
    missing = client.post(BASE, json=payload)
    assert missing.status_code == 422  # X-Admin-Token header is required
    wrong = client.post(BASE, headers={"X-Admin-Token": "nope"}, json=payload)
    assert wrong.status_code == 401


def test_upsert_creates_enabled_assignment(client) -> None:
    user = _user()
    db = _db_returning_user(user)
    _override_db(db)

    resp = client.post(
        BASE,
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={
            "email": "Nova.CreativeVideo@gmail.com",  # normalized to lowercase
            "preset_id": "cigdem",
            "preset_version": "v2",
            "assigned_by": "yasin",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "nova.creativevideo@gmail.com"
    assert (body["preset_id"], body["preset_version"]) == ("cigdem", "v2")
    assert body["enabled"] is True
    assert body["user_id"] == str(user.id)
    added = db.add.call_args[0][0]
    assert isinstance(added, CreatorStyleAssignment)
    assert added.enabled is True
    db.commit.assert_awaited_once()


def test_upsert_unknown_email_404s(client) -> None:
    db = _db_returning_user(None)
    _override_db(db)

    resp = client.post(
        BASE,
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={"email": "nobody@example.com", "preset_id": "cigdem", "preset_version": "v2"},
    )

    assert resp.status_code == 404
    db.commit.assert_not_awaited()


def test_upsert_rejects_unknown_preset_and_bad_charset(client) -> None:
    db = _db_returning_user(_user())
    _override_db(db)

    unknown = client.post(
        BASE,
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={"email": "a@b.c", "preset_id": "cigdem", "preset_version": "v99"},
    )
    assert unknown.status_code == 422

    traversal = client.post(
        BASE,
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={"email": "a@b.c", "preset_id": "../cigdem", "preset_version": "v2"},
    )
    assert traversal.status_code == 422
    db.commit.assert_not_awaited()


def test_upsert_rejects_half_shadow_pair(client) -> None:
    db = _db_returning_user(_user())
    _override_db(db)

    resp = client.post(
        BASE,
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={
            "email": "a@b.c",
            "preset_id": "cigdem",
            "preset_version": "v2",
            "shadow_preset_id": "cigdem",
        },
    )

    assert resp.status_code == 422
    assert "together" in resp.json()["detail"]


def test_upsert_updates_existing_row_and_can_opt_out(client) -> None:
    user = _user()
    db = _db_returning_user(user)
    existing = CreatorStyleAssignment(
        user_id=user.id,
        preset_id="cigdem",
        preset_version="v1",
        enabled=True,
        assigned_by="system",
    )
    existing.updated_at = datetime.now(UTC)
    db.get.return_value = existing
    _override_db(db)

    resp = client.post(
        BASE,
        headers={"X-Admin-Token": ADMIN_TOKEN},
        json={
            "email": user.email,
            "preset_id": "cigdem",
            "preset_version": "v2",
            "enabled": False,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert existing.preset_version == "v2"
    db.add.assert_not_called()  # updated in place, not duplicated
    db.commit.assert_awaited_once()


def test_delete_removes_assignment(client) -> None:
    user = _user()
    db = _db_returning_user(user)
    existing = CreatorStyleAssignment(
        user_id=user.id, preset_id="cigdem", preset_version="v2", enabled=True
    )
    db.get.return_value = existing
    _override_db(db)

    resp = client.delete(f"{BASE}/{user.email}", headers={"X-Admin-Token": ADMIN_TOKEN})

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    db.delete.assert_awaited_once_with(existing)
