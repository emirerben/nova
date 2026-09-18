"""Tests for POST /admin/reviewer-account/seed (KRI-111).

Mock-DB style (mirrors test_admin_generative.py / test_mobile_auth.py):
`X-Admin-Token` gate via `patch("app.routes.admin.settings")`, everything
else (feature config, source/reviewer lookups) through the real `settings`
singleton via `monkeypatch`, and an ordered `db.execute` side_effect list.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.services.reviewer_login import hash_password

ADMIN_TOKEN = "test-admin-token"
REVIEWER_EMAIL = "reviewer@usekria.com"


@pytest.fixture()
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_db, None)


def _configure_reviewer_login(monkeypatch) -> None:
    monkeypatch.setattr(settings, "reviewer_login_enabled", True)
    monkeypatch.setattr(settings, "reviewer_login_email", REVIEWER_EMAIL)
    monkeypatch.setattr(settings, "reviewer_login_password_hash", hash_password("irrelevant-here"))


def _user(**overrides) -> SimpleNamespace:
    base = dict(id=uuid.uuid4(), email="creator@example.com", onboarding_status="complete")
    base.update(overrides)
    return SimpleNamespace(**base)


def _job(**overrides) -> SimpleNamespace:
    job_id = overrides.pop("id", uuid.uuid4())
    user_id = overrides.pop("user_id", uuid.uuid4())
    base = dict(
        id=job_id,
        user_id=user_id,
        status="variants_ready",
        job_type="default",
        mode="generative",
        template_id=None,
        music_track_id=None,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "video_path": f"generative-jobs/{job_id}/out.mp4",
                    "poster_path": f"generative-jobs/{job_id}/out.jpg",
                }
            ]
        },
        raw_storage_path="users/source/raw.mp4",
        selected_platforms=None,
        probe_metadata=None,
        transcript=None,
        scene_cuts=None,
        all_candidates=None,
        error_detail=None,
        failure_reason=None,
        current_phase=None,
        phase_log=[],
        pipeline_trace=None,
        started_at=None,
        finished_at=None,
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _scalar(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalars(values: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def _rows(rows: list) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


def _db(*results: object) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    remaining = iter(results)

    async def execute(statement: object) -> object:
        return next(remaining)

    db.execute = AsyncMock(side_effect=execute)
    return db


def _post(client: TestClient, db, source_email: str, limit: int = 6):
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.routes.admin.settings") as admin_settings:
        admin_settings.admin_api_key = ADMIN_TOKEN
        return client.post(
            "/admin/reviewer-account/seed",
            json={"source_user_email": source_email, "limit": limit},
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )


def test_unconfigured_returns_404(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reviewer_login_enabled", False)
    response = _post(client, _db(), "creator@example.com")
    assert response.status_code == 404


def test_clone_creates_new_rows_with_reviewer_ownership_and_marker(client, monkeypatch) -> None:
    _configure_reviewer_login(monkeypatch)
    source = _user()
    source_job = _job(user_id=source.id)

    db = _db(
        _scalar(source),  # source user lookup
        _scalar(None),  # reviewer user lookup (not found -> create)
        _rows([]),  # already-seeded markers (none)
        _scalars([source_job]),  # source's ready jobs
    )
    with patch("app.routes.admin_reviewer.storage") as storage_mock:
        storage_mock.copy_object = MagicMock()
        response = _post(client, db, source.email)

    assert response.status_code == 200
    body = response.json()
    assert body["created_user"] is True
    assert body["skipped"] == []
    assert len(body["cloned"]) == 1
    cloned_id = body["cloned"][0]
    assert cloned_id != str(source_job.id)

    added = [call.args[0] for call in db.add.call_args_list]
    new_user = next(row for row in added if type(row).__name__ == "User")
    new_job = next(row for row in added if type(row).__name__ == "Job")

    assert new_user.email == REVIEWER_EMAIL
    assert new_user.auth_provider == "reviewer"
    assert new_user.onboarding_status == source.onboarding_status

    assert str(new_job.id) == cloned_id
    assert new_job.id != source_job.id
    assert new_job.user_id == new_user.id
    assert new_job.assembly_plan["reviewer_seed_source_job_id"] == str(source_job.id)
    # The video/poster paths must be rewritten onto the CLONE's own job-id
    # prefix (see admin_reviewer._clone_owned_output_path) so `routes/me.py`'s
    # ownership check succeeds for the new row.
    new_variant = new_job.assembly_plan["variants"][0]
    assert new_variant["video_path"] == f"generative-jobs/{new_job.id}/out.mp4"
    assert new_variant["poster_path"] == f"generative-jobs/{new_job.id}/out.jpg"
    # Cross-user references are dropped, not copied.
    assert new_job.content_plan_item_id is None
    assert new_job.celery_task_id is None
    storage_mock.copy_object.assert_any_call(
        f"generative-jobs/{source_job.id}/out.mp4",
        f"generative-jobs/{new_job.id}/out.mp4",
    )
    db.commit.assert_awaited()


def test_second_call_skips_already_seeded_job(client, monkeypatch) -> None:
    _configure_reviewer_login(monkeypatch)
    source = _user()
    reviewer = _user(email=REVIEWER_EMAIL, id=uuid.uuid4())
    source_job = _job(user_id=source.id)

    db = _db(
        _scalar(source),  # source user lookup
        _scalar(reviewer),  # reviewer user lookup (already exists)
        _rows([(str(source_job.id),)]),  # already-seeded marker present
        _scalars([source_job]),  # source's ready jobs (same job again)
    )
    with patch("app.routes.admin_reviewer.storage") as storage_mock:
        storage_mock.copy_object = MagicMock()
        response = _post(client, db, source.email)

    assert response.status_code == 200
    body = response.json()
    assert body["created_user"] is False
    assert body["cloned"] == []
    assert body["skipped"] == [str(source_job.id)]
    storage_mock.copy_object.assert_not_called()
    # No new Job row is added on a skip.
    added_types = [type(call.args[0]).__name__ for call in db.add.call_args_list]
    assert "Job" not in added_types
