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


# ── POST /content-plans/{id}/regenerate (feedback loop, Phase 2) ───────────────


def _plan_mock(user_id: uuid.UUID, *, status: str = "ready") -> MagicMock:
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.plan_status = status
    plan.horizon_days = 30
    plan.events = None
    plan.activation_status = "none"
    plan.seed_clip_paths = []
    plan.items = []
    plan.start_date = None
    plan.generation_started_at = None
    return plan


def test_regenerate_enqueues_and_sets_generating(client: TestClient) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="ready")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=plan)
    with patch("app.tasks.content_plan_build.regenerate_content_plan") as task:
        task.delay = MagicMock()
        resp = client.post(f"/content-plans/{plan.id}/regenerate")
    assert resp.status_code == 200
    assert resp.json()["plan_status"] == "generating"
    task.delay.assert_called_once_with(str(plan.id))


def test_regenerate_409_when_already_generating(client: TestClient) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="generating")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=plan)
    with patch("app.tasks.content_plan_build.regenerate_content_plan") as task:
        task.delay = MagicMock()
        resp = client.post(f"/content-plans/{plan.id}/regenerate")
    assert resp.status_code == 409
    task.delay.assert_not_called()


def test_regenerate_404_for_other_users_plan(client: TestClient) -> None:
    user = _fake_user()
    # _load_owned_plan filters on user_id, so a cross-user plan resolves to None.
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=None)
    resp = client.post(f"/content-plans/{uuid.uuid4()}/regenerate")
    assert resp.status_code == 404


# ── start_date activation (T3) ─────────────────────────────────────────────────


def _task_ctx(plan, persona_row=None) -> MagicMock:
    """Build a sync_session context manager mock for task-level tests."""
    from app.models import ContentPlan, User  # noqa: PLC0415
    from app.models import Persona as PersonaRow  # noqa: PLC0415

    user = MagicMock()
    user.onboarding_status = "plan_ready"

    session = MagicMock()

    def _get(model, _pk):
        if model is ContentPlan:
            return plan
        if model is PersonaRow:
            return persona_row
        if model is User:
            return user
        return None

    session.get = MagicMock(side_effect=_get)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _valid_persona_row() -> MagicMock:
    row = MagicMock()
    row.persona = {
        "summary": "you film calm morning routines",
        "content_pillars": ["mornings"],
        "tone": "warm",
        "audience": "people who want a calmer start",
        "posting_cadence": "3/wk",
        "sample_topics": ["sunrise walk"],
    }
    return row


def test_start_date_set_when_plan_becomes_ready() -> None:
    """generate_content_plan stamps start_date = today when the plan first goes ready.

    Locks the null-guard write so legacy or first-time plans get a real anchor
    date when the task flips plan_status to 'ready'.
    """
    from datetime import date  # noqa: PLC0415

    from app.tasks.content_plan_build import generate_content_plan  # noqa: PLC0415

    plan_id = uuid.uuid4()
    plan = MagicMock()
    plan.id = plan_id
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = []
    plan.start_date = None  # freshly created plan — no date yet

    persona_row = _valid_persona_row()
    ctx = _task_ctx(plan, persona_row)

    from app.agents._schemas.content_plan import PlanItemSpec  # noqa: PLC0415

    output = MagicMock()
    output.items = [PlanItemSpec(day_index=1, theme="morning", idea="film the dawn")]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch("app.tasks.content_plan_build._dedup_and_replace", return_value=output),
    ):
        generate_content_plan.run(str(plan_id))

    assert plan.start_date == date.today()


def test_start_date_fallback_to_generation_started_at() -> None:
    """_plan_response falls back to generation_started_at.date() when start_date is NULL.

    Legacy plans that became ready before T3 have no start_date. The response
    helper must surface a real date rather than None so the frontend has an anchor.
    """
    from datetime import UTC, date, datetime  # noqa: PLC0415

    from app.routes.content_plans import _plan_response  # noqa: PLC0415

    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.plan_status = "ready"
    plan.horizon_days = 30
    plan.events = None
    plan.items = []
    plan.activation_status = "none"
    plan.seed_clip_paths = []
    plan.start_date = None  # legacy — not stamped
    gen_dt = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
    plan.generation_started_at = gen_dt

    resp = _plan_response(plan)

    assert resp.start_date == date(2026, 5, 1)


def test_regenerate_preserves_start_date() -> None:
    """regenerate_content_plan must NOT overwrite an existing start_date.

    The null guard (`if plan.start_date is None`) ensures the original anchor
    date survives re-generation; only NULL plans get stamped.
    """
    from datetime import date  # noqa: PLC0415

    from app.tasks.content_plan_build import regenerate_content_plan  # noqa: PLC0415

    plan_id = uuid.uuid4()
    original_date = date(2026, 4, 15)
    regenerable = MagicMock()
    regenerable.day_index = 1
    regenerable.user_edited = False
    regenerable.current_job_id = None

    plan = MagicMock()
    plan.id = plan_id
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = [regenerable]
    plan.start_date = original_date  # already anchored

    persona_row = _valid_persona_row()
    ctx = _task_ctx(plan, persona_row)

    from app.agents._schemas.content_plan import PlanItemSpec  # noqa: PLC0415

    output = MagicMock()
    output.items = [PlanItemSpec(day_index=1, theme="new theme", idea="new idea")]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.services.feedback_summary.rollup_user_feedback",
            return_value="",
        ),
    ):
        regenerate_content_plan.run(str(plan_id))

    # The original anchor date must not be overwritten.
    assert plan.start_date == original_date
