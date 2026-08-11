"""Route tests for the content-plan activation-seed endpoints (mock-DB)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _owned_plan(user_id: uuid.UUID, *, status="ready", activation="none", seed=None):
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.persona_id = uuid.uuid4()
    plan.ownership_epoch = 1
    plan.ownership_quarantined_at = None
    plan.plan_status = status
    plan.activation_status = activation
    plan.seed_clip_paths = seed or []
    plan.horizon_days = 30
    plan.events = None
    plan.items = []
    # Explicit None so Pydantic doesn't try to validate the MagicMock attr as a date
    plan.start_date = None
    plan.generation_started_at = None
    persona = MagicMock()
    persona.id = plan.persona_id
    persona.user_id = plan.user_id
    persona.persona = {}
    persona.idea_seeds = []
    plan._owned_persona_fixture = persona
    return plan


def _db_for(plan) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()

    async def _execute(stmt):  # noqa: ANN001
        value = plan._owned_persona_fixture if "FROM personas" in str(stmt) else plan
        return MagicMock(scalar_one_or_none=MagicMock(return_value=value))

    db.execute = AsyncMock(side_effect=_execute)
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


def test_seed_clips_rejects_foreign_prefix(client: TestClient) -> None:
    user = _user()
    plan = _owned_plan(user.id)
    _override(user, _db_for(plan))
    resp = client.post(
        f"/content-plans/{plan.id}/seed-clips",
        json={"clip_gcs_paths": [f"users/{user.id}/plan/{plan.id}/item-x/clip.mp4"]},
    )
    # Item prefix is NOT the seed prefix — must be rejected.
    assert resp.status_code == 422


def test_seed_clips_accepts_seed_prefix(client: TestClient) -> None:
    user = _user()
    plan = _owned_plan(user.id)
    _override(user, _db_for(plan))
    good = f"users/{user.id}/plan/{plan.id}/seed/abc-clip.mp4"
    resp = client.post(f"/content-plans/{plan.id}/seed-clips", json={"clip_gcs_paths": [good]})
    assert resp.status_code == 200
    assert resp.json()["activation_status"] == "seeding"


def test_seed_upload_urls_returns_seed_prefix(client: TestClient) -> None:
    user = _user()
    plan = _owned_plan(user.id)
    _override(user, _db_for(plan))
    seed_path = f"users/{user.id}/plan/{plan.id}/seed/x.mp4"
    with patch(
        "app.storage.presigned_put_url_for_plan_seed",
        return_value=("https://signed.example/put", seed_path),
    ):
        resp = client.post(
            f"/content-plans/{plan.id}/seed-upload-urls",
            json={
                "files": [
                    {"filename": "x.mp4", "content_type": "video/mp4", "file_size_bytes": 1000}
                ]
            },
        )
    assert resp.status_code == 200
    assert "/seed/" in resp.json()["urls"][0]["gcs_path"]


def test_activate_requires_ready_plan(client: TestClient) -> None:
    user = _user()
    plan = _owned_plan(user.id, status="generating", seed=["users/x/plan/y/seed/a.mp4"])
    _override(user, _db_for(plan))
    resp = client.post(f"/content-plans/{plan.id}/activate")
    assert resp.status_code == 409


def test_activate_requires_seed_clips(client: TestClient) -> None:
    user = _user()
    plan = _owned_plan(user.id, status="ready", seed=[])
    _override(user, _db_for(plan))
    resp = client.post(f"/content-plans/{plan.id}/activate")
    assert resp.status_code == 409


def test_activate_rejects_when_already_activating(client: TestClient) -> None:
    user = _user()
    plan = _owned_plan(
        user.id, status="ready", activation="activating", seed=["users/x/plan/y/seed/a.mp4"]
    )
    _override(user, _db_for(plan))
    resp = client.post(f"/content-plans/{plan.id}/activate")
    assert resp.status_code == 409


def test_activate_enqueues_on_happy_path(client: TestClient) -> None:
    user = _user()
    plan = _owned_plan(user.id, status="ready", seed=["users/x/plan/y/seed/a.mp4"])
    _override(user, _db_for(plan))
    with patch("app.tasks.content_plan_build.activate_content_plan") as task:
        task.delay = MagicMock()
        resp = client.post(f"/content-plans/{plan.id}/activate")
    assert resp.status_code == 200
    task.delay.assert_called_once_with(str(plan.id), plan.ownership_epoch)
    assert plan.activation_status == "activating"
