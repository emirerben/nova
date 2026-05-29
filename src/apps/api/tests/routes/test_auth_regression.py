"""Regression tests for Phase 1 auth changes.

IRON RULE (plan-eng-review): after switching legacy routes to
get_current_user_or_synthetic, unauthenticated requests must still
create jobs attributed to the synthetic user.  Tests here prevent
that regression from shipping silently.

These are unit-level: they mock the DB session to avoid needing a
real database, and exercise the fallback path of get_current_user_or_synthetic.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_client(db_session):
    """Return a TestClient with the DB dependency overridden."""
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app, raise_server_exceptions=False)


# ── generative jobs ──────────────────────────────────────────────────────────


def test_create_generative_job_unauthenticated_uses_synthetic_user():
    """Unauthenticated POST /generative-jobs → job attributed to synthetic user."""
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    created_jobs: list[SimpleNamespace] = []

    def capture_add(obj):
        created_jobs.append(obj)

    db.add = capture_add

    _passthrough = "app.routes.generative_jobs._validate_clip_path_prefixes"
    with patch("app.tasks.generative_build.orchestrate_generative_job") as mock_task:
        mock_task.apply_async = MagicMock()
        with patch(_passthrough, side_effect=lambda v: v):
            client = _make_client(db)
            # No Authorization or X-User-Id headers — exercises synthetic fallback.
            client.post(
                "/generative-jobs",
                json={"clip_gcs_paths": ["dev-user/batch-abc/clip_000.mp4"]},
            )

    app.dependency_overrides.clear()
    # The mock DB doesn't fully simulate the ORM, so the response status may
    # vary.  The critical assertion is that the Job was created with the
    # synthetic user_id — captured in `created_jobs` before commit/refresh.
    assert created_jobs, "No Job row was created — the auth fallback may not be wiring in"
    assert created_jobs[0].user_id == SYNTHETIC_USER_ID, (
        f"Expected synthetic user {SYNTHETIC_USER_ID}, got {created_jobs[0].user_id}"
    )


def test_create_template_job_unauthenticated_uses_synthetic_user():
    """Unauthenticated POST /template-jobs → job attributed to synthetic user."""
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    created_jobs: list[SimpleNamespace] = []
    db.add = lambda obj: created_jobs.append(obj)

    # Patch the template-validation helpers so we don't need a real DB
    with (
        patch("app.routes.template_jobs.get_template_or_404") as mock_tmpl,
        patch("app.routes.template_jobs.require_ready"),
        patch("app.routes.template_jobs.validate_clip_count"),
        patch("app.routes.template_jobs.validate_clip_total_duration"),
        patch("app.routes.template_jobs.validate_clips_processable", new_callable=AsyncMock),
        patch("app.tasks.template_orchestrate.orchestrate_template_job") as mock_task,
    ):
        mock_tmpl.return_value = MagicMock(
            id="template-uuid",
            required_inputs=[],
            agentic=False,
            agentic_pct_timing=False,
        )
        mock_task.apply_async = MagicMock()

        client = _make_client(db)
        client.post(
            "/template-jobs",
            json={
                "template_id": "template-uuid",
                "clip_gcs_paths": ["dev-user/batch/clip_000.mp4"],
                "clip_durations": [5.0],
                "selected_platforms": ["tiktok"],
            },
        )

    app.dependency_overrides.clear()
    assert created_jobs, "No Job row was created — the auth fallback may not be wiring in"
    assert created_jobs[0].user_id == SYNTHETIC_USER_ID


# ── auth module unit tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_current_user_missing_header_returns_synthetic():
    """get_current_user_or_synthetic with no X-User-Id returns the synthetic stub."""
    from app.auth import SYNTHETIC_USER_ID as SYN_ID
    from app.auth import get_current_user_or_synthetic

    db = AsyncMock()
    result = await get_current_user_or_synthetic(x_user_id=None, authorization=None, db=db)
    assert result.id == SYN_ID
    db.execute.assert_not_called()  # no DB hit for synthetic path


@pytest.mark.asyncio
async def test_get_current_user_invalid_uuid_raises_401():
    """get_current_user with a non-UUID X-User-Id raises HTTP 401."""
    from fastapi import HTTPException

    from app.auth import get_current_user

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(x_user_id="not-a-uuid", authorization=None, db=db)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_missing_header_raises_401():
    """get_current_user (strict) with no X-User-Id raises HTTP 401."""
    from fastapi import HTTPException

    from app.auth import get_current_user

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(x_user_id=None, authorization=None, db=db)
    assert exc_info.value.status_code == 401


# ── google upsert ─────────────────────────────────────────────────────────────


def test_google_upsert_creates_new_user():
    """POST /auth/google-upsert creates a new user when email is not in DB."""
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # No existing user
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    added: list = []
    db.add = lambda obj: added.append(obj)

    with patch("app.config.settings") as mock_settings:
        mock_settings.internal_api_key = ""  # unconfigured = open
        client = _make_client(db)
        resp = client.post(
            "/auth/google-upsert",
            json={"email": "new@example.com", "name": "New User"},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["created"] is True
    assert len(added) == 1
    assert added[0].email == "new@example.com"


def test_google_upsert_returns_existing_user():
    """POST /auth/google-upsert returns existing user_id without creating a new row."""
    existing_id = uuid.uuid4()
    existing = MagicMock()
    existing.id = existing_id
    existing.email = "existing@example.com"

    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing))
    )
    added: list = []
    db.add = lambda obj: added.append(obj)

    with patch("app.config.settings") as mock_settings:
        mock_settings.internal_api_key = ""
        client = _make_client(db)
        resp = client.post(
            "/auth/google-upsert",
            json={"email": "existing@example.com"},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["created"] is False
    assert data["user_id"] == str(existing_id)
    assert len(added) == 0  # no new row created


def test_google_upsert_wrong_internal_key_returns_401():
    """POST /auth/google-upsert with a bad INTERNAL_API_KEY returns 401."""
    db = AsyncMock()

    # Must patch the settings object that routes/auth.py already imported.
    with patch("app.routes.auth.settings") as mock_settings:
        mock_settings.internal_api_key = "correct-key"
        client = _make_client(db)
        resp = client.post(
            "/auth/google-upsert",
            json={"email": "attacker@example.com"},
            headers={"Authorization": "Bearer wrong-key"},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 401


# ── get_current_user (strict) additional paths ────────────────────────────────


@pytest.mark.asyncio
async def test_get_current_user_user_not_found_returns_401():
    """get_current_user with a valid UUID that doesn't exist in DB returns 401."""
    from fastapi import HTTPException

    from app.auth import get_current_user

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(
            x_user_id=str(uuid.uuid4()),
            authorization=None,
            db=db,
        )
    assert exc_info.value.status_code == 401
    db.execute.assert_called_once()  # DB was queried


@pytest.mark.asyncio
async def test_get_current_user_or_synthetic_with_header_delegates_to_strict():
    """get_current_user_or_synthetic with an X-User-Id header hits the DB (not synthetic)."""
    from fastapi import HTTPException

    from app.auth import get_current_user_or_synthetic

    user_id = str(uuid.uuid4())
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    # With a header present it delegates to get_current_user, which 401s if no user
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_synthetic(
            x_user_id=user_id,
            authorization=None,
            db=db,
        )
    assert exc_info.value.status_code == 401
    db.execute.assert_called_once()  # DB was hit — not the synthetic bypass


# Note: music_jobs / presigned / uploads use the identical CurrentUserOrSynthetic
# dependency proven end-to-end by the generative + template tests above. They add
# no new auth logic, so endpoint-level tests for each are intentionally omitted —
# the auth-module unit tests above cover every branch of the shared dependency.
