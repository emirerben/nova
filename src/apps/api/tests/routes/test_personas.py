"""Route tests for /personas (content-plan Phase 3).

Mock-DB (UUID/JSONB models don't map to SQLite). Cover the auth gate, the
create→enqueue flow, GET-when-absent, and PATCH ownership isolation.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _fake_user(uid: uuid.UUID | None = None) -> MagicMock:
    u = MagicMock()
    u.id = uid or uuid.uuid4()
    u.onboarding_status = "pending"
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


def test_create_persona_requires_auth(client: TestClient) -> None:
    """No X-User-Id / Authorization → 401 (plan routes are strict)."""
    app.dependency_overrides[get_db] = lambda: _async_db()
    resp = client.post("/personas", json={"work": "barista"})
    assert resp.status_code == 401


def test_create_persona_enqueues_generation(client: TestClient) -> None:
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=None)

    with patch("app.tasks.persona_build.generate_persona") as task:
        task.delay = MagicMock()
        resp = client.post("/personas", json={"work": "barista", "hobbies": "lifting"})

    assert resp.status_code == 201
    assert resp.json()["persona_status"] == "generating"
    task.delay.assert_called_once()


def test_get_persona_404_when_absent(client: TestClient) -> None:
    user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _async_db(scalar_result=None)

    resp = client.get("/personas")
    assert resp.status_code == 404


def test_patch_persona_404_for_other_users_persona(client: TestClient) -> None:
    """A persona owned by a different user must not be editable (no leak)."""
    user = _fake_user()
    other_persona = MagicMock()
    other_persona.id = uuid.uuid4()
    other_persona.user_id = uuid.uuid4()  # different owner

    db = _async_db()
    db.get = AsyncMock(return_value=other_persona)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/personas/{other_persona.id}", json={"tone": "edgy"})
    assert resp.status_code == 404


# ── POST /personas/{id}/retune-from-feedback (feedback loop, Phase 2) ──────────


def _persona_row(user_id: uuid.UUID, *, status: str) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = user_id
    row.persona_status = status
    row.persona = {"summary": "you", "content_pillars": ["a"], "tone": "warm"}
    row.questionnaire = {"work": "barista"}
    row.error_detail = None
    row.tiktok_profile = None
    return row


def test_retune_blocks_when_persona_hand_edited(client: TestClient) -> None:
    """The 'their say' invariant: a hand-edited persona is authoritative — 409,
    never silently overwritten by inferred feedback."""
    user = _fake_user()
    row = _persona_row(user.id, status="edited")
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.persona_build.retune_persona_from_feedback") as task:
        task.delay = MagicMock()
        resp = client.post(f"/personas/{row.id}/retune-from-feedback")
    assert resp.status_code == 409
    task.delay.assert_not_called()
    # The persona JSON is untouched.
    assert row.persona == {"summary": "you", "content_pillars": ["a"], "tone": "warm"}


def test_retune_enqueues_when_persona_ready(client: TestClient) -> None:
    user = _fake_user()
    row = _persona_row(user.id, status="ready")
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.persona_build.retune_persona_from_feedback") as task:
        task.delay = MagicMock()
        resp = client.post(f"/personas/{row.id}/retune-from-feedback")
    assert resp.status_code == 200
    assert resp.json()["persona_status"] == "generating"
    task.delay.assert_called_once_with(str(row.id))


def test_retune_404_for_other_users_persona(client: TestClient) -> None:
    user = _fake_user()
    row = _persona_row(uuid.uuid4(), status="ready")  # different owner
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.post(f"/personas/{row.id}/retune-from-feedback")
    assert resp.status_code == 404


# ── PATCH posts_per_week ──────────────────────────────────────────────────────


def _editable_row(user_id: uuid.UUID) -> MagicMock:
    """A persona row the caller owns, in a state that accepts edits."""
    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = user_id
    row.persona_status = "ready"
    row.questionnaire = None  # must be a dict or None — MagicMock fails PersonaResponse validation
    row.persona = {
        "summary": "you",
        "content_pillars": ["a"],
        "tone": "warm",
        "audience": "anyone",
        "posting_cadence": "4/week",
    }
    row.error_detail = None
    row.tiktok_profile = None
    return row


def test_patch_persona_accepts_posts_per_week(client: TestClient) -> None:
    """PATCH {posts_per_week: 4} must store the integer, not a stringified '4'."""
    user = _fake_user()
    row = _editable_row(user.id)
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/personas/{row.id}", json={"posts_per_week": 4})

    assert resp.status_code == 200
    assert row.persona["posts_per_week"] == 4
    assert isinstance(row.persona["posts_per_week"], int)


def test_patch_persona_rejects_out_of_range_posts_per_week(client: TestClient) -> None:
    """posts_per_week=9 violates ge=1,le=7 → 422 Unprocessable Entity."""
    user = _fake_user()
    row = _editable_row(user.id)
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/personas/{row.id}", json={"posts_per_week": 9})
    assert resp.status_code == 422


def test_patch_persona_rejects_zero_posts_per_week(client: TestClient) -> None:
    """posts_per_week=0 violates ge=1 → 422."""
    user = _fake_user()
    row = _editable_row(user.id)
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/personas/{row.id}", json={"posts_per_week": 0})
    assert resp.status_code == 422


# ── PATCH idea_seeds (M1 Bring-Your-Own-Ideas) ───────────────────────────────


def test_patch_persona_stamps_missing_idea_seed_ids(client: TestClient) -> None:
    """Seed submitted without an id must receive a server-stamped hex id."""
    user = _fake_user()
    row = _editable_row(user.id)
    row.idea_seeds = []  # start empty, explicit list so or-fallback is predictable
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/personas/{row.id}",
        json={"idea_seeds": [{"text": "morning coffee walk", "status": "pending"}]},
    )

    assert resp.status_code == 200
    # The handler mutates row.idea_seeds in-place.
    assert isinstance(row.idea_seeds, list)
    assert len(row.idea_seeds) == 1
    seed = row.idea_seeds[0]
    assert seed["text"] == "morning coffee walk"
    assert seed["status"] == "pending"
    # Server must have stamped a non-empty id (uuid4 hex, 32 chars).
    assert isinstance(seed["id"], str)
    assert len(seed["id"]) == 32


def test_patch_persona_drops_blank_idea_seeds(client: TestClient) -> None:
    """Seeds with blank text must be filtered out — never persisted."""
    user = _fake_user()
    row = _editable_row(user.id)
    row.idea_seeds = []
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/personas/{row.id}",
        json={
            "idea_seeds": [
                {"text": "a real idea", "status": "pending"},
                {"text": "   ", "status": "pending"},  # blank — must be dropped
                {"text": "", "status": "pending"},  # empty — must be dropped
            ]
        },
    )

    assert resp.status_code == 200
    assert len(row.idea_seeds) == 1
    assert row.idea_seeds[0]["text"] == "a real idea"


def test_patch_persona_invalid_status_coerced_to_pending(client: TestClient) -> None:
    """Unknown seed status must fall back to 'pending' — never stored verbatim."""
    user = _fake_user()
    row = _editable_row(user.id)
    row.idea_seeds = []
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/personas/{row.id}",
        json={"idea_seeds": [{"text": "my idea", "status": "hacked_status"}]},
    )

    assert resp.status_code == 200
    assert row.idea_seeds[0]["status"] == "pending"


def test_patch_idea_seeds_leaves_persona_dict_untouched(client: TestClient) -> None:
    """Patching idea_seeds must NOT modify the persona JSONB (separate column)."""
    user = _fake_user()
    row = _editable_row(user.id)
    row.idea_seeds = []
    original_persona = dict(row.persona)
    db = _async_db()
    db.get = AsyncMock(return_value=row)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/personas/{row.id}",
        json={"idea_seeds": [{"text": "great idea", "status": "pending"}]},
    )

    assert resp.status_code == 200
    # persona JSONB must not have an 'idea_seeds' key injected into it
    assert "idea_seeds" not in row.persona
    # original persona fields must be preserved
    assert row.persona["tone"] == original_persona["tone"]


# ── POST /personas/reset ─────────────────────────────────────────────────────


def test_reset_requires_auth(client: TestClient) -> None:
    """No auth → 401; route is behind CurrentUser (strict)."""
    app.dependency_overrides[get_db] = lambda: _async_db()
    resp = client.post("/personas/reset")
    assert resp.status_code == 401


def test_reset_happy_path(client: TestClient) -> None:
    """Happy path: 200 {reset: true}.

    All three deletes go through db.execute (raw SQL), not db.delete — using the
    ORM db.delete(persona) triggers SQLAlchemy's cascade which tries to SET
    persona_id=NULL on content_plans (NOT NULL constraint violation). Raw DELETE
    lets Postgres's ondelete=CASCADE handle child rows at the DB level.

    Execute order: (1) UPDATE jobs, (2) DELETE feedback, (3) DELETE persona.
    """
    user = _fake_user()
    db_user = MagicMock()
    db_user.onboarding_status = "complete"

    delete_result = MagicMock()
    delete_result.rowcount = 1  # persona existed

    db = AsyncMock()
    db.commit = AsyncMock()
    # execute side_effects: (1) UPDATE jobs, (2) DELETE feedback, (3) DELETE persona
    execute_results = [
        MagicMock(),  # UPDATE jobs result
        MagicMock(),  # DELETE feedback result
        delete_result,  # DELETE persona result (rowcount=1)
    ]
    db.execute = AsyncMock(side_effect=execute_results)
    db.delete = AsyncMock()
    db.get = AsyncMock(return_value=db_user)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.post("/personas/reset")

    assert resp.status_code == 200
    assert resp.json() == {"reset": True}
    # Three execute calls: jobs UPDATE, feedback DELETE, persona DELETE — raw SQL.
    assert db.execute.call_count == 3
    # ORM db.delete must NOT be called (causes the NOT NULL cascade bug).
    db.delete.assert_not_awaited()
    # Onboarding status reset.
    assert db_user.onboarding_status == "pending"
    # Single commit.
    db.commit.assert_awaited_once()


def test_reset_no_persona_is_idempotent_200(client: TestClient) -> None:
    """No-persona case returns 200 — idempotent; 'start over with nothing' is valid.

    The DELETE personas WHERE user_id=:uid simply deletes 0 rows (rowcount=0).
    Jobs UPDATE + feedback DELETE + onboarding reset still all happen.
    """
    user = _fake_user()
    db_user = MagicMock()
    db_user.onboarding_status = "pending"

    no_persona_result = MagicMock()
    no_persona_result.rowcount = 0

    db = AsyncMock()
    db.commit = AsyncMock()
    execute_results = [
        MagicMock(),  # UPDATE jobs result
        MagicMock(),  # DELETE feedback result
        no_persona_result,  # DELETE persona (0 rows — nothing existed)
    ]
    db.execute = AsyncMock(side_effect=execute_results)
    db.delete = AsyncMock()
    db.get = AsyncMock(return_value=db_user)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.post("/personas/reset")

    assert resp.status_code == 200
    assert resp.json() == {"reset": True}
    assert db.execute.call_count == 3
    db.delete.assert_not_awaited()
    assert db_user.onboarding_status == "pending"
    db.commit.assert_awaited_once()


def test_reset_nulls_job_fk_before_persona_delete(client: TestClient) -> None:
    """The FK fix: the first execute must target 'jobs' with content_plan_item_id.

    If a future refactor reorders the operations, this test fails loudly — which
    prevents the Postgres NO ACTION violation on plan_items.id (models.py:383,
    no ondelete on Job.content_plan_item_id).
    """
    user = _fake_user()

    delete_result = MagicMock()
    delete_result.rowcount = 1

    db = AsyncMock()
    db.commit = AsyncMock()
    execute_results = [
        MagicMock(),  # UPDATE jobs
        MagicMock(),  # DELETE feedback
        delete_result,  # DELETE persona
    ]
    db.execute = AsyncMock(side_effect=execute_results)
    db.delete = AsyncMock()
    db.get = AsyncMock(return_value=_fake_user())

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    client.post("/personas/reset")

    # First call must be the jobs UPDATE (the FK-nulling step).
    first_stmt = str(db.execute.call_args_list[0].args[0])
    assert "jobs" in first_stmt.lower(), (
        "First execute must target 'jobs' to NULL content_plan_item_id "
        "before plan_items are cascade-deleted (models.py:383 no-ondelete FK)"
    )
    assert "content_plan_item_id" in first_stmt.lower()
