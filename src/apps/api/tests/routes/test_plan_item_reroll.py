"""Route tests for POST /plan-items/{id}/reroll.

Mock-DB style, mirroring test_plan_item_variant_edit.py.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app

REROLL_TASK = "app.tasks.content_plan_build.reroll_plan_item"


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _result(value) -> MagicMock:  # noqa: ANN001
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _idea_item(user_id: uuid.UUID, *, item_status: str = "idea", current_job_id=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.day_index = 3
    item.theme = "morning routine"
    item.idea = "film the 5am start"
    item.filming_suggestion = None
    item.rationale = None
    item.filming_guide = []
    item.clip_gcs_paths = []
    item.item_status = item_status
    item.current_job_id = current_job_id
    item.current_job = None
    item.user_edited = False
    item.conformance = None
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    item.clip_assignments = []
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db(execute_results: list, plan) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(v) for v in execute_results])
    db.get = AsyncMock(return_value=plan)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# ── happy path ────────────────────────────────────────────────────────────────


def test_reroll_dispatches_task_and_sets_rerolling(client: TestClient) -> None:
    """POST reroll on an idea item with no job → 200, item_status = 'rerolling',
    task dispatched."""
    user = _user()
    item, plan = _idea_item(user.id)
    # execute order: load item (guard) → reload item (response)
    # After commit, item_status is mutated in-place on the mock.
    db = _db([item, item], plan)
    _override(user, db)

    with patch(REROLL_TASK) as task:
        task.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/reroll")

    assert resp.status_code == 200
    # item_status was set to "rerolling" before the commit
    assert item.item_status == "rerolling"
    task.delay.assert_called_once_with(str(item.id))


# ── 409 guards ────────────────────────────────────────────────────────────────


def test_reroll_409_on_non_idea_item(client: TestClient) -> None:
    """item_status = 'generating' → 409, task never dispatched."""
    user = _user()
    item, plan = _idea_item(user.id, item_status="generating")
    db = _db([item], plan)
    _override(user, db)

    with patch(REROLL_TASK) as task:
        task.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/reroll")

    assert resp.status_code == 409
    task.delay.assert_not_called()


def test_reroll_409_on_item_with_job(client: TestClient) -> None:
    """item_status = 'idea' but current_job_id set → 409."""
    user = _user()
    item, plan = _idea_item(user.id, item_status="idea", current_job_id=uuid.uuid4())
    db = _db([item], plan)
    _override(user, db)

    with patch(REROLL_TASK) as task:
        task.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/reroll")

    assert resp.status_code == 409
    task.delay.assert_not_called()


# ── 404 ownership guard ───────────────────────────────────────────────────────


def test_reroll_404_on_wrong_user(client: TestClient) -> None:
    """Another user's item → 404."""
    user = _user()
    item, plan = _idea_item(user.id)
    plan.user_id = uuid.uuid4()  # different user owns the plan
    db = _db([item], plan)
    _override(user, db)

    with patch(REROLL_TASK) as task:
        task.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/reroll")

    assert resp.status_code == 404
    task.delay.assert_not_called()
