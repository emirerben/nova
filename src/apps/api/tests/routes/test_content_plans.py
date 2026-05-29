"""Route tests for /content-plans and /plan-items + the derive_item_status helper.

derive_item_status is a pure function (no DB) — exercised directly. Routes use
the mock-DB + dependency-override pattern (UUID/JSONB don't map to SQLite).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.routes.plan_items import derive_item_status

# ── derive_item_status (pure, plan T2) ────────────────────────────────────────


def _item(item_status="idea", job_status=None):
    it = MagicMock()
    it.item_status = item_status
    if job_status is None:
        it.current_job = None
    else:
        job = MagicMock()
        job.status = job_status
        it.current_job = job
    return it


def test_status_without_job_uses_row_state() -> None:
    assert derive_item_status(_item("idea")) == "idea"
    assert derive_item_status(_item("awaiting_clips")) == "awaiting_clips"


def test_status_ready_when_job_ready() -> None:
    assert derive_item_status(_item("awaiting_clips", "variants_ready")) == "ready"
    assert derive_item_status(_item("idea", "variants_ready_partial")) == "ready"


def test_status_failed_when_job_failed() -> None:
    assert derive_item_status(_item("idea", "variants_failed")) == "failed"
    assert derive_item_status(_item("idea", "cancelled")) == "failed"


def test_status_generating_while_job_in_flight() -> None:
    assert derive_item_status(_item("idea", "processing")) == "generating"
    assert derive_item_status(_item("idea", "queued")) == "generating"


# ── routes ─────────────────────────────────────────────────────────────────


def _fake_user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
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


def test_create_plan_requires_ready_persona(client: TestClient) -> None:
    user = _fake_user()
    persona = MagicMock()
    persona.persona_status = "generating"  # not ready
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=persona)
    resp = client.post("/content-plans", json={"events": "", "horizon_days": 30})
    assert resp.status_code == 409


def test_create_plan_enqueues_when_persona_ready(client: TestClient) -> None:
    user = _fake_user()
    persona = MagicMock()
    persona.persona_status = "ready"
    persona.id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=persona)
    with patch("app.tasks.content_plan_build.generate_content_plan") as task:
        task.delay = MagicMock()
        resp = client.post("/content-plans", json={"events": "spring break", "horizon_days": 30})
    assert resp.status_code == 201
    assert resp.json()["plan_status"] == "generating"
    task.delay.assert_called_once()


def test_get_plan_404_when_absent(client: TestClient) -> None:
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=None)
    resp = client.get("/content-plans")
    assert resp.status_code == 404


def test_patch_item_404_for_other_users_plan(client: TestClient) -> None:
    user = _fake_user()
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    other_plan = MagicMock()
    other_plan.user_id = uuid.uuid4()  # different owner

    db = _async_db()
    db.get = AsyncMock(side_effect=[item, other_plan])
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.patch(f"/plan-items/{item.id}", json={"idea": "hijack"})
    assert resp.status_code == 404
