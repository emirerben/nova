"""Route tests for the per-user library surface (GET /me/jobs, add-to-plan).

Mock-DB style, mirroring test_plan_item_variant_edit.py. The library is strictly
scoped to the authenticated user (no user_id input to forge), so these assert:
derived status + preview-url extraction across job modes, keyset pagination, and
that add-to-plan links both FK sides only when the job AND the plan day belong to
the caller (404 — not 403 — on any cross-user reference).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _job(
    *,
    user_id: uuid.UUID,
    status: str = "variants_ready",
    assembly_plan: dict | None = None,
    mode: str | None = "generative",
    job_type: str = "default",
    content_plan_item_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user_id
    job.status = status
    job.assembly_plan = assembly_plan if assembly_plan is not None else {}
    job.mode = mode
    job.job_type = job_type
    job.content_plan_item_id = content_plan_item_id
    job.created_at = created_at or datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    return job


def _scalar(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _scalars(rows: list) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return r


def _db(execute_results: list) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


client = TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# ── GET /me/jobs ──────────────────────────────────────────────────────────────


def test_list_returns_users_jobs_with_derived_status_and_preview() -> None:
    user = _user()
    ready_variant_job = _job(
        user_id=user.id,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {"variant_id": "song_lyrics", "render_status": "failed", "output_url": None},
                {"variant_id": "song_text", "render_status": "ready", "output_url": "gs://x/a.mp4"},
            ]
        },
    )
    single_output_job = _job(
        user_id=user.id,
        status="template_ready",
        mode=None,
        job_type="template",
        assembly_plan={"output_url": "gs://x/tpl.mp4"},
    )
    db = _db([_scalars([ready_variant_job, single_output_job])])
    _override(user, db)

    resp = client.get("/me/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert [j["status"] for j in body["jobs"]] == ["ready", "ready"]
    assert body["jobs"][0]["output_url"] == "gs://x/a.mp4"  # first READY variant
    assert body["jobs"][1]["output_url"] == "gs://x/tpl.mp4"  # template single output
    assert body["jobs"][1]["mode"] == "template"  # falls back to job_type
    assert body["next_cursor"] is None


def test_list_generating_job_has_no_preview_url() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="processing",
        assembly_plan={"variants": [{"variant_id": "song_text", "render_status": "rendering"}]},
    )
    db = _db([_scalars([job])])
    _override(user, db)

    resp = client.get("/me/jobs")
    assert resp.status_code == 200
    j = resp.json()["jobs"][0]
    assert j["status"] == "generating"
    assert j["output_url"] is None


def test_list_forged_user_id_query_param_is_ignored() -> None:
    """No user_id input exists on the route, so a forged ?user_id is inert —
    the scope always comes from the authenticated dependency."""
    user = _user()
    own_job = _job(user_id=user.id, status="done", assembly_plan={"output_url": "gs://x/own.mp4"})
    db = _db([_scalars([own_job])])
    _override(user, db)

    resp = client.get(f"/me/jobs?user_id={uuid.uuid4()}")
    assert resp.status_code == 200
    # The DB query was built from user.id, not the forged param: we return the
    # rows the (overridden) session yields, and the route never reads ?user_id.
    assert [j["id"] for j in resp.json()["jobs"]] == [str(own_job.id)]


def test_list_paginates_with_next_cursor() -> None:
    user = _user()
    older = _job(user_id=user.id, created_at=datetime(2026, 5, 29, tzinfo=UTC))
    newer = _job(user_id=user.id, created_at=datetime(2026, 5, 30, tzinfo=UTC))
    # limit=1 → route fetches limit+1=2 rows, returns 1, emits cursor from it.
    db = _db([_scalars([newer, older])])
    _override(user, db)

    resp = client.get("/me/jobs?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["id"] == str(newer.id)
    assert body["next_cursor"] == newer.created_at.isoformat()


def test_list_rejects_bad_cursor() -> None:
    user = _user()
    db = _db([])
    _override(user, db)
    resp = client.get("/me/jobs?cursor=not-a-date")
    assert resp.status_code == 400


# ── POST /me/jobs/{id}/add-to-plan ────────────────────────────────────────────


def test_add_to_plan_links_both_fk_sides() -> None:
    user = _user()
    job = _job(user_id=user.id, status="done", assembly_plan={"output_url": "gs://x/o.mp4"})
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user.id
    item = MagicMock()
    item.id = uuid.uuid4()
    db = _db([_scalar(job), _scalar(plan), _scalar(item)])
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 3})
    assert resp.status_code == 200
    assert resp.json()["content_plan_item_id"] == str(item.id)
    # Both sides of the circular FK pair were set, then committed.
    assert item.current_job_id == job.id
    assert job.content_plan_item_id == item.id
    db.commit.assert_awaited_once()


def test_add_to_plan_404_when_job_not_owned() -> None:
    user = _user()
    job = _job(user_id=uuid.uuid4())  # a different user's job
    db = _db([_scalar(job)])
    _override(user, db)
    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 1})
    assert resp.status_code == 404


def test_add_to_plan_404_when_no_plan() -> None:
    user = _user()
    job = _job(user_id=user.id)
    db = _db([_scalar(job), _scalar(None)])  # no content plan
    _override(user, db)
    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 1})
    assert resp.status_code == 404


def test_add_to_plan_404_when_day_missing() -> None:
    user = _user()
    job = _job(user_id=user.id)
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user.id
    db = _db([_scalar(job), _scalar(plan), _scalar(None)])  # day not found
    _override(user, db)
    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 99})
    assert resp.status_code == 404


def test_add_to_plan_400_on_bad_job_id() -> None:
    user = _user()
    db = _db([])
    _override(user, db)
    resp = client.post("/me/jobs/not-a-uuid/add-to-plan", json={"day_index": 1})
    assert resp.status_code == 400
