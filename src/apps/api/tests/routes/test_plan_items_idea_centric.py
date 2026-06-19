"""Route tests for idea-centric PlanItem CRUD (T11–T14).

T11: POST /plan-items creates a PlanItem + seed mirror
T12: DELETE /plan-items/{id} refuses with active job or clips
T13: POST /content-plans/{id}/reorder atomically reorders + rejects foreign ids
T14: POST /plan-items/{id}/expand does NOT write to DB (propose-only)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app

# ── shared helpers ─────────────────────────────────────────────────────────────


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _result(value) -> MagicMock:  # noqa: ANN001
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _plan(user_id: uuid.UUID, items=None) -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.user_id = user_id
    p.persona_id = uuid.uuid4()
    p.plan_status = "ready"
    p.horizon_days = 30
    p.events = None
    p.items = items or []
    p.activation_status = "none"
    p.seed_clip_paths = []
    p.pool = {}
    p.generation_started_at = None
    p.start_date = None
    return p


def _idea_item(user_id: uuid.UUID, *, item_status: str = "idea", current_job_id=None, clips=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.day_index = None
    item.theme = None
    item.idea = "visit the new coffee shop"
    item.filming_suggestion = None
    item.rationale = None
    item.filming_guide = []
    item.clip_gcs_paths = clips or []
    item.clip_assignments = []
    item.scenes = []
    item.notes = None
    item.scheduled_date = None
    item.position = 1
    item.item_status = item_status
    item.current_job_id = current_job_id
    item.current_job = None
    item.user_edited = True
    item.conformance = None
    item.source_idea_seed_id = None
    item.edit_format = None
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# ── T11: POST /plan-items creates item + seed mirror ─────────────────────────


def test_add_idea_creates_plan_item_and_seed_mirror(client: TestClient) -> None:
    """Posting a bare idea creates a PlanItem and mirrors it to persona.idea_seeds."""
    user = _user()
    plan = _plan(user.id, items=[])

    new_item = MagicMock()
    new_item.id = uuid.uuid4()
    new_item.content_plan_id = plan.id
    new_item.day_index = None
    new_item.theme = None
    new_item.idea = "visit the new coffee shop"
    new_item.filming_suggestion = None
    new_item.rationale = None
    new_item.filming_guide = []
    new_item.clip_gcs_paths = []
    new_item.clip_assignments = []
    new_item.scenes = []
    new_item.notes = None
    new_item.scheduled_date = None
    new_item.position = 1
    new_item.item_status = "idea"
    new_item.current_job_id = None
    new_item.current_job = None
    new_item.user_edited = True
    new_item.conformance = None
    new_item.source_idea_seed_id = None
    new_item.edit_format = None

    persona = MagicMock()
    persona.persona = {"summary": "creator"}
    persona.idea_seeds = []
    persona.style = {}

    db = AsyncMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    db.add = MagicMock()
    db.refresh = AsyncMock()
    db.get = AsyncMock(side_effect=[persona, plan, plan])
    # First execute: plan lookup. Second execute: reloaded item.
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(
                scalar_one_or_none=MagicMock(return_value=plan),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[plan]))),
            ),
            MagicMock(scalar_one_or_none=MagicMock(return_value=new_item)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=plan)),
        ]
    )

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    plan_id = str(plan.id)
    resp = client.post(f"/plan-items?plan_id={plan_id}", json={"idea": "visit the new coffee shop"})
    # DB.add was called (item creation) and commit was called.
    assert resp.status_code == 201
    db.add.assert_called()
    db.commit.assert_awaited()


# ── T12: DELETE refuses with active job or clips ─────────────────────────────


def test_delete_idea_refuses_with_active_job(client: TestClient) -> None:
    """DELETE /plan-items/{id} returns 409 when a job is generating."""
    user = _user()
    item, plan = _idea_item(user.id, item_status="generating")
    # Simulate a live job.
    job = MagicMock()
    job.status = "processing"
    item.current_job = job
    item.current_job_id = uuid.uuid4()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(plan), _result(item)])
    db.get = AsyncMock(return_value=plan)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 409


def test_delete_idea_refuses_with_clips(client: TestClient) -> None:
    """DELETE /plan-items/{id} returns 409 when the item has clips."""
    user = _user()
    item, plan = _idea_item(user.id, clips=["users/u1/clip.mp4"])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(plan)])
    db.get = AsyncMock(return_value=plan)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 409


def test_delete_idea_succeeds_when_clean(client: TestClient) -> None:
    """DELETE /plan-items/{id} returns 204 for an un-started idea without clips."""
    user = _user()
    item, plan = _idea_item(user.id)

    db = AsyncMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(plan)])
    db.get = AsyncMock(return_value=plan)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 204
    db.delete.assert_awaited_with(item)
    db.commit.assert_awaited()


# ── T13: POST /content-plans/{id}/reorder ────────────────────────────────────


def _plan_with_items(user_id: uuid.UUID, n: int = 3):
    items = []
    for i in range(n):
        it = MagicMock()
        it.id = uuid.uuid4()
        it.position = i + 1
        it.day_index = i + 1
        it.theme = None
        it.idea = f"idea {i + 1}"
        it.filming_suggestion = None
        it.rationale = None
        it.filming_guide = []
        it.clip_gcs_paths = []
        it.clip_assignments = []
        it.scenes = []
        it.notes = None
        it.scheduled_date = None
        it.item_status = "idea"
        it.current_job_id = None
        it.current_job = None
        it.user_edited = False
        it.conformance = None
        it.source_idea_seed_id = None
        it.edit_format = None
        items.append(it)
    plan = _plan(user_id, items=items)
    return plan, items


def test_reorder_items_atomic(client: TestClient) -> None:
    """Reorder assigns new position values in order."""
    user = _user()
    plan, items = _plan_with_items(user.id, n=3)
    reversed_ids = [str(it.id) for it in reversed(items)]

    db = AsyncMock()
    db.commit = AsyncMock()
    # First execute: load plan for reorder. Second: reload plan for response.
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=plan)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=plan)),
        ]
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.post(f"/content-plans/{plan.id}/reorder", json={"item_ids": reversed_ids})
    assert resp.status_code == 200
    # Position 1 should now be the last item.
    assert items[-1].position == 1
    assert items[0].position == 3
    db.commit.assert_awaited()


def test_reorder_rejects_foreign_id(client: TestClient) -> None:
    """Reorder returns 400 when a foreign item id is included."""
    user = _user()
    plan, items = _plan_with_items(user.id, n=2)
    foreign_id = str(uuid.uuid4())
    ids = [str(it.id) for it in items] + [foreign_id]  # wrong count too

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=plan)),
        ]
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.post(f"/content-plans/{plan.id}/reorder", json={"item_ids": ids})
    assert resp.status_code == 400


# ── T14: POST /plan-items/{id}/expand (propose-only) ─────────────────────────


def test_expand_does_not_write_db(client: TestClient) -> None:
    """expand returns a proposal without calling db.add or db.commit."""
    user = _user()
    item, plan = _idea_item(user.id)
    persona = MagicMock()
    persona.persona = {"summary": "creator", "content_pillars": ["fitness"]}

    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(plan)])
    db.get = AsyncMock(side_effect=[plan, plan, persona])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    mock_output = MagicMock()
    mock_output.theme = "Coffee shop first visit"
    mock_output.filming_suggestion = "Film the entrance and your first sip"
    mock_output.filming_guide = []
    mock_output.rationale = "Creates curiosity"

    with patch("app.agents.idea_expander.IdeaExpanderAgent.run", return_value=mock_output):
        resp = client.post(f"/plan-items/{item.id}/expand")

    assert resp.status_code == 200
    data = resp.json()
    assert data["theme"] == "Coffee shop first visit"
    # No DB writes.
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
