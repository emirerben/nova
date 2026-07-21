"""Route tests for /content-plans and /plan-items + the derive_item_status helper.

derive_item_status is a pure function (no DB) — exercised directly. Routes use
the mock-DB + dependency-override pattern (UUID/JSONB don't map to SQLite).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
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
    """Kill switch OFF → old auto-generate behavior: plan starts 'generating'."""
    user = _fake_user()
    persona = MagicMock()
    persona.persona_status = "ready"
    persona.id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=persona)
    with (
        patch("app.tasks.content_plan_build.generate_content_plan") as task,
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.idea_centric_plan_enabled = False
        task.delay = MagicMock()
        resp = client.post("/content-plans", json={"events": "spring break", "horizon_days": 30})
    assert resp.status_code == 201
    assert resp.json()["plan_status"] == "generating"
    task.delay.assert_called_once()


def test_create_plan_no_auto_generate(client: TestClient) -> None:
    """Kill switch ON (default) → idea-centric: plan starts 'ready', no generation task."""
    user = _fake_user()
    persona = MagicMock()
    persona.persona_status = "ready"
    persona.id = uuid.uuid4()
    persona.idea_seeds = []
    persona.questionnaire = {}
    app.dependency_overrides[get_current_user] = lambda: user
    db = _async_db(scalar_result=persona)
    # db.execute for plan items returns empty list.
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=persona)),  # persona lookup
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ]
    )
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.config.settings") as mock_settings:
        mock_settings.idea_centric_plan_enabled = True
        resp = client.post("/content-plans", json={"events": "", "horizon_days": 30})
    assert resp.status_code == 201
    assert resp.json()["plan_status"] == "ready"


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
    plan.updated_at = datetime.now(UTC)
    plan.pool = {}
    return plan


def _plan_item_mock() -> MagicMock:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.day_index = None
    item.theme = None
    item.idea = "stale generated idea"
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.filming_suggestion = None
    item.rationale = None
    item.filming_guide = []
    item.clip_gcs_paths = []
    item.clip_assignments = []
    item.item_status = "idea"
    item.current_job_id = None
    item.current_job = None
    item.user_edited = False
    item.conformance = None
    item.content_mode = None
    item.edit_format = None
    item.montage_preset = "classic"
    item.voiceover_gcs_path = None
    item.landscape_fit = "fit"
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    return item


def _persona_mock() -> MagicMock:
    persona = MagicMock()
    persona.idea_seeds = []
    persona.persona = {}
    return persona


def test_get_plan_repairs_stale_generating_plan_with_items_to_ready(
    client: TestClient,
) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="generating")
    plan.generation_started_at = datetime.now(UTC) - timedelta(minutes=11)
    plan.items = [_plan_item_mock()]
    db = _async_db(scalar_result=plan)
    db.get = AsyncMock(return_value=_persona_mock())
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.get("/content-plans")

    assert resp.status_code == 200
    assert resp.json()["plan_status"] == "ready"
    assert plan.plan_status == "ready"
    db.commit.assert_awaited_once()


def test_get_plan_repairs_stale_generating_empty_plan_to_failed(
    client: TestClient,
) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="generating")
    plan.generation_started_at = datetime.now(UTC) - timedelta(minutes=11)
    plan.items = []
    db = _async_db(scalar_result=plan)
    db.get = AsyncMock(return_value=_persona_mock())
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.get("/content-plans")

    assert resp.status_code == 200
    assert resp.json()["plan_status"] == "failed"
    assert plan.plan_status == "failed"
    db.commit.assert_awaited_once()


def test_get_plan_keeps_fresh_generating_plan_in_flight(client: TestClient) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="generating")
    plan.generation_started_at = datetime.now(UTC) - timedelta(minutes=2)
    plan.items = []
    db = _async_db(scalar_result=plan)
    db.get = AsyncMock(return_value=_persona_mock())
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.get("/content-plans")

    assert resp.status_code == 200
    assert resp.json()["plan_status"] == "generating"
    assert plan.plan_status == "generating"
    db.commit.assert_not_awaited()


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


# ── POST /content-plans/{id}/generate-ideas ──────────────────────────────────


def test_generate_ideas_locks_plan_and_enqueues(client: TestClient) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="ready")
    db = _async_db(scalar_result=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.content_plan_build.generate_ideas_into_plan") as task:
        task.delay = MagicMock()
        resp = client.post(f"/content-plans/{plan.id}/generate-ideas")

    assert resp.status_code == 200
    assert resp.json()["plan_status"] == "generating"
    task.delay.assert_called_once_with(str(plan.id), plan.generation_started_at.isoformat())
    first_stmt = db.execute.call_args_list[0].args[0]
    assert "FOR UPDATE" in str(first_stmt).upper()


def test_generate_ideas_409_when_already_generating(client: TestClient) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="generating")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=plan)

    with patch("app.tasks.content_plan_build.generate_ideas_into_plan") as task:
        task.delay = MagicMock()
        resp = client.post(f"/content-plans/{plan.id}/generate-ideas")

    assert resp.status_code == 409
    task.delay.assert_not_called()


def test_generate_ideas_publish_failure_terminalizes_plan(client: TestClient) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="ready")
    db = _async_db(scalar_result=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.content_plan_build.generate_ideas_into_plan") as task:
        task.delay = MagicMock(side_effect=ConnectionError("redis unavailable"))
        resp = client.post(f"/content-plans/{plan.id}/generate-ideas")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Couldn't start idea generation. Try again."
    assert plan.plan_status == "failed"
    assert db.commit.await_count == 2


def test_generate_ideas_ambiguous_publish_failure_preserves_completed_plan(
    client: TestClient,
) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="ready")
    db = _async_db(scalar_result=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    def accepted_then_raised(_plan_id: str, _generation_token: str) -> None:
        plan.plan_status = "ready"
        raise ConnectionError("publish confirmation lost")

    with patch("app.tasks.content_plan_build.generate_ideas_into_plan") as task:
        task.delay = MagicMock(side_effect=accepted_then_raised)
        resp = client.post(f"/content-plans/{plan.id}/generate-ideas")

    assert resp.status_code == 503
    assert plan.plan_status == "ready"
    assert db.commit.await_count == 2
    failure_stmt = db.execute.call_args_list[1].args[0]
    assert "FOR UPDATE" in str(failure_stmt).upper()
    assert failure_stmt.get_execution_options()["populate_existing"] is True


def test_generate_ideas_publish_failure_preserves_newer_generating_attempt(
    client: TestClient,
) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="ready")
    db = _async_db(scalar_result=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    def newer_attempt_started(_plan_id: str, _generation_token: str) -> None:
        plan.plan_status = "generating"
        plan.generation_started_at = datetime.now(UTC) + timedelta(minutes=1)
        raise ConnectionError("publish confirmation lost")

    with patch("app.tasks.content_plan_build.generate_ideas_into_plan") as task:
        task.delay = MagicMock(side_effect=newer_attempt_started)
        resp = client.post(f"/content-plans/{plan.id}/generate-ideas")

    assert resp.status_code == 503
    assert plan.plan_status == "generating"
    assert plan.generation_started_at > datetime.now(UTC)


def test_generate_ideas_publish_failure_matches_equivalent_timezone_offset(
    client: TestClient,
) -> None:
    user = _fake_user()
    plan = _plan_mock(user.id, status="ready")
    db = _async_db(scalar_result=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    def same_instant_different_offset(_plan_id: str, generation_token: str) -> None:
        started_at = datetime.fromisoformat(generation_token)
        plan.generation_started_at = started_at.astimezone(timezone(timedelta(hours=1)))
        raise ConnectionError("publish confirmation lost")

    with patch("app.tasks.content_plan_build.generate_ideas_into_plan") as task:
        task.delay = MagicMock(side_effect=same_instant_different_offset)
        resp = client.post(f"/content-plans/{plan.id}/generate-ideas")

    assert resp.status_code == 503
    assert plan.plan_status == "failed"


@pytest.mark.asyncio
async def test_generate_ideas_concurrent_requests_enqueue_once() -> None:
    """Two sessions serialized by the emitted row lock enqueue exactly once."""
    from fastapi import HTTPException  # noqa: PLC0415

    from app.routes.content_plans import (  # noqa: PLC0415
        ContentPlanResponse,
    )
    from app.routes.content_plans import (
        generate_ideas_into_plan as generate_ideas_endpoint,
    )

    user = _fake_user()
    plan = _plan_mock(user.id, status="ready")
    row_lock = asyncio.Lock()

    class LockedDb:
        def __init__(self) -> None:
            self.holds_lock = False

        async def execute(self, stmt):
            if "FOR UPDATE" in str(stmt).upper():
                await row_lock.acquire()
                self.holds_lock = True
            return MagicMock(scalar_one_or_none=MagicMock(return_value=plan))

        async def commit(self) -> None:
            if self.holds_lock:
                self.holds_lock = False
                row_lock.release()

        async def rollback(self) -> None:
            if self.holds_lock:
                self.holds_lock = False
                row_lock.release()

    async def submit(db: LockedDb):
        try:
            return await generate_ideas_endpoint(str(plan.id), user, db)  # type: ignore[arg-type]
        except HTTPException as exc:
            return exc
        finally:
            await db.rollback()

    first_db = LockedDb()
    second_db = LockedDb()
    with patch("app.tasks.content_plan_build.generate_ideas_into_plan") as task:
        task.delay = MagicMock()
        results = await asyncio.gather(submit(first_db), submit(second_db))

    successes = [result for result in results if isinstance(result, ContentPlanResponse)]
    conflicts = [
        result
        for result in results
        if isinstance(result, HTTPException) and result.status_code == 409
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    task.delay.assert_called_once_with(str(plan.id), plan.generation_started_at.isoformat())


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


# ── pool matched_item_id reconciliation ─────────────────────────────────────


def _pool_plan(user_id: uuid.UUID, pool: dict, items: list) -> MagicMock:
    """Build a plan mock with a pool and a list of item mocks."""
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.plan_status = "ready"
    plan.horizon_days = 30
    plan.events = None
    plan.activation_status = "none"
    plan.seed_clip_paths = []
    plan.start_date = None
    plan.generation_started_at = None
    plan.pool = pool
    plan.items = items
    return plan


def _item_mock(item_id: uuid.UUID, clip_gcs_paths: list[str]) -> MagicMock:
    it = MagicMock()
    it.id = item_id
    it.clip_gcs_paths = clip_gcs_paths
    it.clip_assignments = [{"gcs_path": p, "shot_id": None} for p in clip_gcs_paths]
    it.item_status = "idea"
    it.current_job = None
    it.content_plan_id = uuid.uuid4()
    it.day_index = 1
    it.theme = "Test"
    it.idea = "Test idea"
    it.filming_suggestion = None
    it.rationale = None
    it.edit_format = "montage"
    it.user_edited = False
    it.current_job_id = None
    it.filming_guide = []
    it.conformance = None
    it.sequence_quote = None
    it.sequence_mode = None
    it.position = 1
    it.scheduled_date = None
    it.notes = None
    it.scenes = []
    it.source_idea_seed_id = None
    it.source_idea_seed_text = None
    it.edit_format = None
    it.voiceover_gcs_path = None
    it.voiceover_bed_level = None
    it.voiceover_caption_style = None
    it.user_edited = False
    it.current_job_id = None
    return it


def test_get_plan_exposes_current_job_finished_at(client: TestClient) -> None:
    user = _fake_user()
    item = _item_mock(uuid.uuid4(), [])
    finished_at = datetime(2026, 7, 19, 12, 30, tzinfo=UTC)
    item.current_job_id = uuid.uuid4()
    item.current_job = SimpleNamespace(status="variants_ready", finished_at=finished_at)
    plan = _pool_plan(user.id, {}, [item])
    plan.persona_id = uuid.uuid4()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=plan)

    resp = client.get("/content-plans")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["finished_at"] == "2026-07-19T12:30:00Z"

    item.current_job.finished_at = None
    assert client.get("/content-plans").json()["items"][0]["finished_at"] is None

    item.current_job_id = None
    item.current_job = None
    assert client.get("/content-plans").json()["items"][0]["finished_at"] is None


def test_pool_matched_count_resets_stale_match() -> None:
    """Pool clip whose matched_item_id no longer exists in the item's clips
    must be treated as unmatched in the response (matched_item_id -> null).

    Scenario: user uploaded clip A to the pool; the matcher assigned it to
    item #1. User later replaced clip A on item #1 with a different clip.
    Now pool clip A's matched_item_id still points at item #1, but clip A
    is no longer in item #1's clip_gcs_paths. The reconciler must reset it.
    """
    from app.routes.content_plans import _plan_response  # noqa: PLC0415

    item_id = uuid.uuid4()
    stale_clip_path = "users/u/plan-pool/p/clip_a.mp4"
    live_clip_path = "users/u/plan-pool/p/clip_b.mp4"  # replacement clip

    # Item now has only the replacement clip -- the pool clip was removed.
    item = _item_mock(item_id, [live_clip_path])

    pool = {
        "status": "matched",
        "clips": [
            # clip_a is stale: still in pool but no longer on the item
            {"gcs_path": stale_clip_path, "matched_item_id": str(item_id)},
            # clip_b is genuinely matched (it IS on the item now)
            {"gcs_path": live_clip_path, "matched_item_id": str(item_id)},
        ],
    }
    plan = _pool_plan(uuid.uuid4(), pool, [item])

    resp = _plan_response(plan)

    # The stale match must not count.
    assert resp.pool_matched_count == 1
    # The live match is preserved.
    assert resp.pool_clip_count == 2


def test_pool_matched_count_resets_deleted_item() -> None:
    """Pool clip whose matched_item_id points at a non-existent item is reset."""
    from app.routes.content_plans import _plan_response  # noqa: PLC0415

    deleted_item_id = uuid.uuid4()
    clip_path = "users/u/plan-pool/p/clip_gone.mp4"

    pool = {
        "status": "matched",
        "clips": [
            {"gcs_path": clip_path, "matched_item_id": str(deleted_item_id)},
        ],
    }
    # Plan has no items -- the item was deleted.
    plan = _pool_plan(uuid.uuid4(), pool, [])

    resp = _plan_response(plan)

    assert resp.pool_matched_count == 0
    assert resp.pool_clip_count == 1


def test_pool_matched_count_preserves_valid_match() -> None:
    """Pool clips whose matched item still holds the clip are not reset."""
    from app.routes.content_plans import _plan_response  # noqa: PLC0415

    item_id = uuid.uuid4()
    clip_path = "users/u/plan-pool/p/clip_keep.mp4"

    item = _item_mock(item_id, [clip_path])

    pool = {
        "status": "matched",
        "clips": [
            {"gcs_path": clip_path, "matched_item_id": str(item_id)},
        ],
    }
    plan = _pool_plan(uuid.uuid4(), pool, [item])

    resp = _plan_response(plan)

    assert resp.pool_matched_count == 1
    assert resp.pool_clip_count == 1


# ── T3: seed-pool carry (onboarding-fork → create_plan) ──────────────────────


def _ready_persona(questionnaire: dict | None = None) -> MagicMock:
    """A persona in 'ready' state with a real dict questionnaire."""
    persona = MagicMock()
    persona.persona_status = "ready"
    persona.id = uuid.uuid4()
    # Use a real dict so .get() works correctly (MagicMock.get() returns MagicMock).
    persona.questionnaire = questionnaire if questionnaire is not None else {}
    return persona


def test_create_plan_carries_durable_onboarding_paths(client: TestClient) -> None:
    """Durable generative-jobs/*/sources/ paths in onboarding_clip_paths → seed_clip_paths."""
    user = _fake_user()
    durable_paths = [
        "generative-jobs/abc123/sources/clip1.mp4",
        "generative-jobs/abc123/sources/clip2.mp4",
    ]
    persona = _ready_persona(questionnaire={"onboarding_clip_paths": durable_paths})
    db = _async_db(scalar_result=persona)

    # Capture the ContentPlan instance added to the session.
    added_plans: list = []

    def _capture_add(obj):  # noqa: E301
        from app.models import ContentPlan  # noqa: PLC0415

        if isinstance(obj, ContentPlan):
            added_plans.append(obj)

    db.add = MagicMock(side_effect=_capture_add)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.content_plan_build.generate_content_plan") as task:
        task.delay = MagicMock()
        resp = client.post("/content-plans", json={"events": "", "horizon_days": 30})

    assert resp.status_code == 201
    assert added_plans, "ContentPlan must have been added to the session"
    plan = added_plans[0]
    assert plan.seed_clip_paths == durable_paths


def test_create_plan_does_not_carry_ephemeral_paths(client: TestClient) -> None:
    """Ephemeral music-uploads/ paths must NOT be carried to seed_clip_paths."""
    user = _fake_user()
    persona = _ready_persona(
        questionnaire={"onboarding_clip_paths": ["music-uploads/tmp/clip.mp4"]}
    )
    db = _async_db(scalar_result=persona)

    added_plans: list = []

    def _capture_add(obj):  # noqa: E301
        from app.models import ContentPlan  # noqa: PLC0415

        if isinstance(obj, ContentPlan):
            added_plans.append(obj)

    db.add = MagicMock(side_effect=_capture_add)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.content_plan_build.generate_content_plan") as task:
        task.delay = MagicMock()
        resp = client.post("/content-plans", json={"events": "", "horizon_days": 30})

    assert resp.status_code == 201
    # Plan must exist and seed_clip_paths must NOT have been set to the ephemeral path.
    assert added_plans, "ContentPlan must have been added to the session"
    plan = added_plans[0]
    # seed_clip_paths is either None or [] — never an ephemeral path.
    assert not plan.seed_clip_paths


def test_attach_seed_clips_still_rejects_wrong_prefix(client: TestClient) -> None:
    """attach_seed_clips (public endpoint) must still 422 a generative-jobs path.

    The refactor extracted _validate_seed_clip_prefix into a helper but must NOT
    have accidentally loosened the public endpoint's prefix guard.
    """
    user = _fake_user()
    plan_id = uuid.uuid4()
    plan = MagicMock()
    plan.id = plan_id
    plan.user_id = user.id
    plan.items = []
    plan.pool = {}
    plan.activation_status = "none"
    plan.seed_clip_paths = []
    plan.plan_status = "generating"
    plan.horizon_days = 30
    plan.events = None
    plan.generation_started_at = None
    plan.start_date = None

    db = _async_db(scalar_result=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    # A durable generative-jobs path posted via request body must be rejected
    # (only server-derived carry in create_plan bypasses the validator).
    resp = client.post(
        f"/content-plans/{plan_id}/seed-clips",
        json={"clip_gcs_paths": ["generative-jobs/abc123/sources/clip1.mp4"]},
    )
    assert resp.status_code == 422


# ── content_mode resolution parity (list GET vs item GET) ─────────────────────


def _bare_plan_item(content_mode=None):
    it = MagicMock()
    it.id = uuid.uuid4()
    it.day_index = 1
    it.theme = "t"
    it.idea = "i"
    it.position = 1
    it.scheduled_date = None
    it.notes = None
    it.scenes = []
    it.filming_suggestion = None
    it.rationale = None
    it.filming_guide = []
    it.clip_gcs_paths = []
    it.clip_assignments = []
    it.item_status = "idea"
    it.current_job = None
    it.current_job_id = None
    it.user_edited = False
    it.conformance = None
    it.content_mode = content_mode
    it.edit_format = None
    it.voiceover_gcs_path = None
    it.landscape_fit = "fit"
    it.voiceover_bed_level = None
    it.voiceover_caption_style = None
    it.source_idea_seed_id = None
    return it


def test_resolve_item_content_mode_priority() -> None:
    """Item override beats persona; junk collapses to create_new."""
    from app.routes.content_plans import _resolve_item_content_mode

    assert _resolve_item_content_mode(_bare_plan_item("existing_footage"), "mixed") == (
        "existing_footage"
    )
    assert _resolve_item_content_mode(_bare_plan_item(None), "mixed") == "mixed"
    assert _resolve_item_content_mode(_bare_plan_item(None), "bogus") == "create_new"
    assert _resolve_item_content_mode(_bare_plan_item(None), None) == "create_new"


def test_plan_response_threads_persona_content_mode() -> None:
    """The plan (list) payload must resolve content_mode like the item GET does —
    it previously hardcoded create_new, diverging from GET /plan-items/{id}."""
    from app.routes.content_plans import _plan_response

    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.plan_status = "ready"
    plan.horizon_days = 30
    plan.events = None
    plan.items = [_bare_plan_item(None), _bare_plan_item("create_new")]
    plan.pool = {}
    plan.activation_status = "none"
    plan.seed_clip_paths = []
    plan.generation_started_at = None
    plan.start_date = None

    resp = _plan_response(plan, persona_content_mode="mixed")
    assert resp.items[0].content_mode == "mixed"  # persona fallback
    assert resp.items[1].content_mode == "create_new"  # item override wins
