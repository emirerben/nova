"""Tests for the activation phase fields in GET /content-plans/{plan_id}/activation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _owned_plan(
    user_id: uuid.UUID,
    *,
    activation="activating",
    activation_phase: str | None = None,
    activation_started_at: datetime | None = None,
    seed=None,
):
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.plan_status = "ready"
    plan.activation_status = activation
    plan.activation_phase = activation_phase
    plan.activation_started_at = activation_started_at
    plan.seed_clip_paths = seed or []
    plan.horizon_days = 30
    plan.events = None
    plan.items = []
    return plan


def _db_for(plan) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=plan)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


def test_activation_response_includes_new_fields_nullable(client: TestClient) -> None:
    """Verify response is valid when phase fields are None (fresh rows / deploy-skew)."""
    user = _user()
    plan = _owned_plan(
        user.id,
        activation="activating",
        activation_phase=None,
        activation_started_at=None,
    )
    _override(user, _db_for(plan))

    resp = client.get(f"/content-plans/{plan.id}/activation")
    assert resp.status_code == 200
    body = resp.json()
    assert "activation_status" in body
    assert "activation_phase" in body
    assert body["activation_phase"] is None
    assert "activation_started_at" in body
    assert body["activation_started_at"] is None


def test_activation_response_includes_phase_when_set(client: TestClient) -> None:
    """Verify phase and started_at are returned when the task has stamped them."""
    user = _user()
    now = datetime(2026, 6, 6, 10, 0, 0, tzinfo=UTC)
    plan = _owned_plan(
        user.id,
        activation="activating",
        activation_phase="matching_clips",
        activation_started_at=now,
    )
    _override(user, _db_for(plan))

    resp = client.get(f"/content-plans/{plan.id}/activation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["activation_phase"] == "matching_clips"
    assert body["activation_started_at"] is not None


def test_activation_response_includes_expected_phase_durations(client: TestClient) -> None:
    """Verify expected_phase_durations is populated from baselines."""
    user = _user()
    plan = _owned_plan(user.id, activation="activating")
    _override(user, _db_for(plan))

    resp = client.get(f"/content-plans/{plan.id}/activation")
    assert resp.status_code == 200
    body = resp.json()
    assert "expected_phase_durations" in body
    durations = body["expected_phase_durations"]
    assert durations is not None
    assert "matching_clips" in durations
    assert "picking_days" in durations
    assert "starting_renders" in durations
    assert durations["matching_clips"] == 75_000
