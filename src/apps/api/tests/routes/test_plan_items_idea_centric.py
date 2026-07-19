"""Route tests for idea-centric PlanItem CRUD (T11–T14).

T11: POST /plan-items creates a PlanItem + seed mirror
T12: DELETE /plan-items/{id} refuses with active job or clips
T13: POST /content-plans/{id}/reorder atomically reorders + rejects foreign ids
T14: POST /plan-items/{id}/expand does NOT write to DB (propose-only)
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agents._runtime import ModelInvocation
from app.agents.idea_expander import FilmingShot
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


class _QueuedModelClient:
    def __init__(self, *responses: dict) -> None:
        self.responses = deque(responses)
        self.invocations: list[dict] = []

    def invoke(self, **kwargs) -> ModelInvocation:  # noqa: ANN003
        import json

        self.invocations.append(kwargs)
        response = self.responses.popleft()
        return ModelInvocation(raw_text=json.dumps(response), tokens_in=10, tokens_out=20)


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
    item.source_idea_seed_text = None
    item.voiceover_gcs_path = None
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None
    item.edit_format = None
    item.landscape_fit = "fit"
    item.content_mode = None
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _valid_expander_payload() -> dict:
    return {
        "theme": "Coffee shop first visit",
        "filming_suggestion": "Film the entrance and your first sip",
        "filming_guide": [
            {
                "what": "Walk up to the shop",
                "how": "Wide shot from across the street",
                "duration_s": 4,
            },
            {"what": "Take the first sip", "how": "Close-up at the table", "duration_s": 3},
        ],
        "rationale": "Creates curiosity",
    }


def _persona() -> MagicMock:
    persona = MagicMock()
    persona.persona = {"summary": "creator", "content_pillars": ["fitness"]}
    persona.style = {}
    return persona


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
    new_item.source_idea_seed_text = None
    new_item.voiceover_gcs_path = None
    new_item.voiceover_bed_level = None
    new_item.voiceover_caption_style = None
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
    db.get = AsyncMock(return_value=plan)
    # Plan lookup → locked persona seed write → reloaded item.
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(
                scalar_one_or_none=MagicMock(return_value=plan),
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[plan]))),
            ),
            MagicMock(scalar_one_or_none=MagicMock(return_value=persona)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=new_item)),
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
    persona_stmt = db.execute.call_args_list[1].args[0]
    assert "FOR UPDATE" in str(persona_stmt).upper()


# ── T12: DELETE refuses with active job or clips ─────────────────────────────


def test_delete_idea_refuses_with_active_job_and_preserves_seed(client: TestClient) -> None:
    """A generating job preserves both its PlanItem and persistent idea seed."""
    user = _user()
    item, plan = _idea_item(user.id, item_status="generating")
    seed_id = uuid.uuid4().hex
    item.source_idea_seed_id = seed_id
    original_seeds = [{"id": seed_id, "text": item.idea, "status": "in_plan"}]
    persona = MagicMock()
    persona.idea_seeds = list(original_seeds)
    # Simulate a live job.
    job = MagicMock()
    job.status = "processing"
    item.current_job = job
    item.current_job_id = uuid.uuid4()

    db = AsyncMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item)])
    db.get = AsyncMock(return_value=plan)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 409
    assert persona.idea_seeds == original_seeds
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.get.assert_awaited_once()


def test_delete_idea_refuses_with_clips_and_preserves_seed(client: TestClient) -> None:
    """Attached clips preserve both their PlanItem and persistent idea seed."""
    user = _user()
    item, plan = _idea_item(user.id, clips=["users/u1/clip.mp4"])
    seed_id = uuid.uuid4().hex
    item.source_idea_seed_id = seed_id
    original_seeds = [{"id": seed_id, "text": item.idea, "status": "in_plan"}]
    persona = MagicMock()
    persona.idea_seeds = list(original_seeds)

    db = AsyncMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item)])
    db.get = AsyncMock(return_value=plan)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 409
    assert persona.idea_seeds == original_seeds
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.get.assert_awaited_once()


def test_delete_unlinked_idea_succeeds_when_clean(client: TestClient) -> None:
    """An unlinked clean idea deletes without requiring a persona seed."""
    user = _user()
    item, plan = _idea_item(user.id)

    db = AsyncMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(item)])
    db.get = AsyncMock(side_effect=[plan, plan])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 204
    db.delete.assert_awaited_with(item)
    db.commit.assert_awaited()
    assert db.get.await_count == 2


def test_delete_linked_idea_removes_seed_and_item_atomically(client: TestClient) -> None:
    """A clean linked idea removes its persona seed in the item delete commit."""
    user = _user()
    item, plan = _idea_item(user.id)
    seed_id = uuid.uuid4().hex
    item.source_idea_seed_id = seed_id
    other_seed = {"id": uuid.uuid4().hex, "text": "keep me", "status": "pending"}
    persona = MagicMock()
    persona.idea_seeds = [
        {"id": seed_id, "text": item.idea, "status": "in_plan"},
        other_seed,
    ]

    db = AsyncMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(persona), _result(item)])
    db.get = AsyncMock(side_effect=[plan, plan])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 204
    assert persona.idea_seeds == [other_seed]
    db.delete.assert_awaited_once_with(item)
    db.commit.assert_awaited_once()
    persona_stmt = db.execute.call_args_list[1].args[0]
    assert "FOR UPDATE" in str(persona_stmt).upper()
    item_stmt = db.execute.call_args_list[2].args[0]
    assert "FOR UPDATE" in str(item_stmt).upper()
    assert item_stmt.get_execution_options()["populate_existing"] is True


@pytest.mark.parametrize("seed_state", ["missing_persona", "missing_entry"])
def test_delete_linked_idea_tolerates_missing_seed_state(
    client: TestClient, seed_state: str
) -> None:
    """Stale provenance never prevents deletion of an otherwise clean item."""
    user = _user()
    item, plan = _idea_item(user.id)
    item.source_idea_seed_id = uuid.uuid4().hex

    persona = None
    if seed_state == "missing_entry":
        persona = MagicMock()
        persona.idea_seeds = [{"id": uuid.uuid4().hex, "text": "keep me", "status": "pending"}]
        original_seeds = list(persona.idea_seeds)

    db = AsyncMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(persona), _result(item)])
    db.get = AsyncMock(side_effect=[plan, plan])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.delete(f"/plan-items/{item.id}")
    assert resp.status_code == 204
    if persona is not None:
        assert persona.idea_seeds == original_seeds
    db.delete.assert_awaited_once_with(item)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_linked_idea_deletes_preserve_both_removals() -> None:
    """Persona row locking prevents concurrent JSONB cleanup from losing a delete."""
    from app.routes.plan_items import delete_idea as delete_idea_endpoint  # noqa: PLC0415

    user = _user()
    first_item, plan = _idea_item(user.id)
    second_item, _ = _idea_item(user.id)
    first_item.content_plan_id = plan.id = uuid.uuid4()
    second_item.content_plan_id = plan.id
    first_item.source_idea_seed_id = uuid.uuid4().hex
    second_item.source_idea_seed_id = uuid.uuid4().hex

    persona = MagicMock()
    persona.idea_seeds = [
        {"id": first_item.source_idea_seed_id, "text": "first", "status": "in_plan"},
        {"id": second_item.source_idea_seed_id, "text": "second", "status": "in_plan"},
    ]

    row_lock = asyncio.Lock()
    both_at_persona = asyncio.Event()
    persona_waiters = 0
    deleted_ids: set[uuid.UUID] = set()

    class LockedDb:
        def __init__(self, item: MagicMock) -> None:
            self.item = item
            self.holds_lock = False

        async def execute(self, stmt):  # noqa: ANN001, ANN202
            nonlocal persona_waiters
            compiled = str(stmt).upper()
            if "FROM PLAN_ITEMS" in compiled:
                return _result(self.item)

            assert "FROM PERSONAS" in compiled
            assert "FOR UPDATE" in compiled
            persona_waiters += 1
            if persona_waiters == 2:
                both_at_persona.set()
            await both_at_persona.wait()
            await row_lock.acquire()
            self.holds_lock = True
            return _result(persona)

        async def get(self, _model, _row_id):  # noqa: ANN001, ANN202
            return plan

        async def delete(self, item: MagicMock) -> None:
            deleted_ids.add(item.id)

        async def commit(self) -> None:
            if self.holds_lock:
                self.holds_lock = False
                row_lock.release()

    await asyncio.gather(
        delete_idea_endpoint(str(first_item.id), user, LockedDb(first_item)),  # type: ignore[arg-type]
        delete_idea_endpoint(str(second_item.id), user, LockedDb(second_item)),  # type: ignore[arg-type]
    )

    assert deleted_ids == {first_item.id, second_item.id}
    assert persona.idea_seeds == []


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
        it.source_idea_seed_text = None
        it.voiceover_gcs_path = None
        it.voiceover_bed_level = None
        it.voiceover_caption_style = None
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
    persona = _persona()

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
    mock_output.filming_guide = [
        FilmingShot(what="Walk up to the shop", how="Wide shot", duration_s=4),
        FilmingShot(what="Take the first sip", how="Close-up", duration_s=3),
    ]
    mock_output.rationale = "Creates curiosity"

    with patch("app.agents.idea_expander.IdeaExpanderAgent.run", return_value=mock_output) as run:
        resp = client.post(f"/plan-items/{item.id}/expand")

    assert resp.status_code == 200
    agent_input = run.call_args.args[0]
    assert agent_input.creator_context == ""
    assert agent_input.video_type == "montage"
    assert agent_input.content_mode == "create_new"
    data = resp.json()
    assert data["theme"] == "Coffee shop first visit"
    assert 2 <= len(data["filming_guide"]) <= 4
    assert all(shot["shot_id"] for shot in data["filming_guide"])
    # No DB writes.
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


def test_expand_trims_context_and_derives_voiceover_type_and_content_mode(
    client: TestClient,
) -> None:
    """Optional request body is sanitized and threaded to the propose-only agent."""
    user = _user()
    item, plan = _idea_item(user.id)
    persona = _persona()
    item.edit_format = "narrated_ready"
    item.content_mode = "existing_footage"

    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(plan)])
    db.get = AsyncMock(side_effect=[plan, plan, persona])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    mock_output = MagicMock()
    mock_output.theme = "Coffee shop first visit"
    mock_output.filming_suggestion = "Find the entrance and first sip in your clips"
    mock_output.filming_guide = [
        FilmingShot(what="Entrance clip", how="Use the widest saved angle", duration_s=4),
        FilmingShot(what="First sip", how="Use the close-up", duration_s=3),
    ]
    mock_output.rationale = "Creates curiosity"

    with patch("app.agents.idea_expander.IdeaExpanderAgent.run", return_value=mock_output) as run:
        resp = client.post(
            f"/plan-items/{item.id}/expand",
            json={"creator_context": "  I want people to save this for Sunday.  "},
        )

    assert resp.status_code == 200
    agent_input = run.call_args.args[0]
    assert agent_input.creator_context == "I want people to save this for Sunday."
    assert agent_input.video_type == "voiceover"
    assert agent_input.content_mode == "existing_footage"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("payload", [{}, {"creator_context": "   "}])
def test_expand_empty_context_becomes_empty_agent_context(
    client: TestClient,
    payload: dict[str, str],
) -> None:
    """Empty request bodies keep the old behavior while accepting explicit JSON."""
    user = _user()
    item, plan = _idea_item(user.id)
    persona = _persona()

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
    mock_output.filming_guide = [
        FilmingShot(what="Walk up to the shop", how="Wide shot", duration_s=4),
        FilmingShot(what="Take the first sip", how="Close-up", duration_s=3),
    ]
    mock_output.rationale = "Creates curiosity"

    with patch("app.agents.idea_expander.IdeaExpanderAgent.run", return_value=mock_output) as run:
        resp = client.post(f"/plan-items/{item.id}/expand", json=payload)

    assert resp.status_code == 200
    assert run.call_args.args[0].creator_context == ""
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


def test_expand_empty_guide_after_retry_returns_502_and_does_not_write(
    client: TestClient,
) -> None:
    """If the agent cannot produce shots after clarification, expand fails friendly."""
    user = _user()
    item, plan = _idea_item(user.id)
    persona = _persona()
    model_client = _QueuedModelClient(
        {**_valid_expander_payload(), "filming_guide": []},
        {**_valid_expander_payload(), "filming_guide": []},
    )

    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(plan)])
    db.get = AsyncMock(side_effect=[plan, plan, persona])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.agents._model_client.default_client", return_value=model_client):
        resp = client.post(f"/plan-items/{item.id}/expand")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Couldn't plan this idea — try again."
    assert len(model_client.invocations) == 2
    assert "montage/voiceover need 2-4" in model_client.invocations[1]["prompt"]
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    assert item.filming_guide == []


def test_expand_empty_once_then_valid_guide_returns_200(client: TestClient) -> None:
    """A sanitized-empty first answer gets one clarification retry."""
    user = _user()
    item, plan = _idea_item(user.id)
    persona = _persona()
    model_client = _QueuedModelClient(
        {**_valid_expander_payload(), "filming_guide": []},
        _valid_expander_payload(),
    )

    db = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(plan)])
    db.get = AsyncMock(side_effect=[plan, plan, persona])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.agents._model_client.default_client", return_value=model_client):
        resp = client.post(f"/plan-items/{item.id}/expand")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["filming_guide"]) == 2
    assert all(shot["shot_id"] for shot in data["filming_guide"])
    assert len(model_client.invocations) == 2
    db.commit.assert_not_awaited()


def test_patch_filming_guide_stamps_missing_shot_ids(client: TestClient) -> None:
    """Accepting an expand proposal without shot_ids never persists null shot_id."""
    user = _user()
    item, plan = _idea_item(user.id)
    persona = _persona()
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(item)])
    db.get = AsyncMock(side_effect=[plan, plan, plan, persona, plan, persona])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={
            "filming_guide": [
                {"what": "Walk up to the shop", "how": "Wide shot", "duration_s": 4},
                {
                    "shot_id": None,
                    "what": "Take the first sip",
                    "how": "Close-up",
                    "duration_s": 3,
                },
            ]
        },
    )

    assert resp.status_code == 200
    assert len(item.filming_guide) == 2
    assert all(shot["shot_id"] for shot in item.filming_guide)
    assert all(len(shot["shot_id"]) == 32 for shot in item.filming_guide)
    db.commit.assert_awaited()


def test_patch_filming_guide_preserves_existing_shot_ids(client: TestClient) -> None:
    """Existing shot_ids round-trip through plan item edits unchanged."""
    user = _user()
    item, plan = _idea_item(user.id)
    persona = _persona()
    existing_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(item), _result(item)])
    db.get = AsyncMock(side_effect=[plan, plan, plan, persona, plan, persona])

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={
            "filming_guide": [
                {
                    "shot_id": existing_ids[0],
                    "what": "Walk up to the shop",
                    "how": "Wide shot",
                    "duration_s": 4,
                },
                {
                    "shot_id": existing_ids[1],
                    "what": "Take the first sip",
                    "how": "Close-up",
                    "duration_s": 3,
                },
            ]
        },
    )

    assert resp.status_code == 200
    assert [shot["shot_id"] for shot in item.filming_guide] == existing_ids
    db.commit.assert_awaited()
