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
