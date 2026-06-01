"""Route tests for Phase 5 plan-item upload + generation endpoints (mock-DB)."""

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


def _async_db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _owned_item(user_id: uuid.UUID, *, clips=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = clips or []
    item.day_index = 1
    item.theme = "t"
    item.idea = "i"
    item.filming_suggestion = None
    item.rationale = None
    item.current_job_id = None
    item.current_job = None
    item.item_status = "idea"
    item.user_edited = False
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db_for(item, plan) -> AsyncMock:
    """DB mock matching _load_owned_item: the item is loaded via
    execute().scalar_one_or_none() (eager-loads current_job), the plan
    ownership check via get()."""
    db = _async_db()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=item)
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=plan)
    return db


def test_generate_requires_clips(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, clips=[])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 409


def test_generate_enqueues_when_clips_present(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{0}/plan/0/a.mp4"])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.tasks.content_plan_build.generate_plan_item_videos") as task:
        task.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 200
    task.delay.assert_called_once_with(str(item.id))


def test_attach_clips_rejects_foreign_prefix(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": ["users/someone-else/plan/x/clip.mp4"]},
    )
    assert resp.status_code == 422


def test_upload_urls_returns_signed_puts(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(
        "app.storage.presigned_put_url_for_plan_item",
        return_value=("https://signed.example/put", f"users/{user.id}/plan/{item.id}/x.mp4"),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/upload-urls",
            json={
                "files": [
                    {"filename": "x.mp4", "content_type": "video/mp4", "file_size_bytes": 1000}
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["urls"]) == 1
    assert body["urls"][0]["gcs_path"].startswith(f"users/{user.id}/plan/")
