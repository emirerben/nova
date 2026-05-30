"""Route tests for the feedback-loop write surface (POST/DELETE /me/feedback).

Mock-DB style (mirrors test_me_jobs.py). Feedback is strictly user-scoped — the
row's user_id is always the authed user, never a body field — so these assert:
ownership 404s (no existence leak), the closed signal enum + body validation, the
one-thumb-per-video rule (a flip deletes the prior thumb), and delete ownership.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _scalar(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _db(execute_results: list) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=execute_results)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


client = TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# ── POST /me/feedback ─────────────────────────────────────────────────────────


def test_thumb_on_owned_job_writes_user_scoped_row() -> None:
    user = _user()
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user.id
    # execute: [job lookup, one-thumb delete].
    db = _db([_scalar(job), MagicMock()])
    _override(user, db)

    resp = client.post("/me/feedback", json={"signal": "up", "job_id": str(job.id)})
    assert resp.status_code == 201
    assert resp.json()["signal"] == "up"
    # The persisted row is scoped to the AUTHED user (never a body field) and to the job.
    row = db.add.call_args.args[0]
    assert row.user_id == user.id
    assert row.job_id == job.id
    assert row.signal == "up"
    # A thumb runs the one-thumb delete first, so execute is awaited twice.
    assert db.execute.await_count == 2


def test_note_on_owned_job_does_not_run_one_thumb_delete() -> None:
    user = _user()
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user.id
    db = _db([_scalar(job)])  # only the job lookup — no delete for a note
    _override(user, db)

    resp = client.post(
        "/me/feedback",
        json={"signal": "note", "job_id": str(job.id), "note": "more sunsets"},
    )
    assert resp.status_code == 201
    assert db.execute.await_count == 1
    assert db.add.call_args.args[0].note == "more sunsets"


def test_plan_level_note_on_owned_plan() -> None:
    user = _user()
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user.id
    db = _db([_scalar(plan)])
    _override(user, db)

    resp = client.post(
        "/me/feedback",
        json={"signal": "note", "content_plan_id": str(plan.id), "note": "punchier hooks"},
    )
    assert resp.status_code == 201
    row = db.add.call_args.args[0]
    assert row.content_plan_id == plan.id
    assert row.job_id is None


def test_feedback_on_other_users_job_is_404() -> None:
    user = _user()
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = uuid.uuid4()  # different owner
    db = _db([_scalar(job)])
    _override(user, db)

    resp = client.post("/me/feedback", json={"signal": "up", "job_id": str(job.id)})
    assert resp.status_code == 404  # not 403 — never leak that the id exists


def test_feedback_on_other_users_plan_is_404() -> None:
    user = _user()
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = uuid.uuid4()
    db = _db([_scalar(plan)])
    _override(user, db)

    resp = client.post(
        "/me/feedback",
        json={"signal": "note", "content_plan_id": str(plan.id), "note": "x"},
    )
    assert resp.status_code == 404


def test_bad_signal_is_422() -> None:
    user = _user()
    _override(user, _db([]))
    resp = client.post("/me/feedback", json={"signal": "love", "job_id": str(uuid.uuid4())})
    assert resp.status_code == 422


def test_note_signal_without_note_is_422() -> None:
    user = _user()
    _override(user, _db([]))
    resp = client.post("/me/feedback", json={"signal": "note", "job_id": str(uuid.uuid4())})
    assert resp.status_code == 422


def test_both_targets_is_422() -> None:
    user = _user()
    _override(user, _db([]))
    resp = client.post(
        "/me/feedback",
        json={"signal": "up", "job_id": str(uuid.uuid4()), "content_plan_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


def test_no_target_is_422() -> None:
    user = _user()
    _override(user, _db([]))
    resp = client.post("/me/feedback", json={"signal": "up"})
    assert resp.status_code == 422


# ── DELETE /me/feedback/{id} ──────────────────────────────────────────────────


def test_delete_owned_feedback_is_204() -> None:
    user = _user()
    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = user.id
    db = _db([_scalar(row)])
    _override(user, db)

    resp = client.delete(f"/me/feedback/{row.id}")
    assert resp.status_code == 204
    db.delete.assert_awaited_once_with(row)


def test_delete_other_users_feedback_is_404() -> None:
    user = _user()
    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = uuid.uuid4()  # different owner
    db = _db([_scalar(row)])
    _override(user, db)

    resp = client.delete(f"/me/feedback/{row.id}")
    assert resp.status_code == 404
    db.delete.assert_not_awaited()
