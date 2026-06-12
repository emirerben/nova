"""Job ownership enforcement tests.

Guard: anonymous (synthetic-owned) jobs remain reachable by UUID;
real-user-owned jobs enforce that the caller is the owner.

ANATOMY OF THE DB MOCK
- No-header path: CurrentUserOrSynthetic resolves to the synthetic user
  WITHOUT a DB hit — mock only needs to satisfy the job lookup.
- Authenticated path: CurrentUserOrSynthetic resolves via get_current_user,
  which does a DB user lookup first, then the endpoint does the job lookup.
  side_effect=[user_result, job_result] on db.execute covers both calls in order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

SYNTHETIC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEST_KEY = "test-internal-key"
_BEARER = f"Bearer {_TEST_KEY}"


def _client(db: AsyncMock) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app, raise_server_exceptions=False)


def _anon_db(job) -> AsyncMock:
    """DB mock for no-header requests: synthetic path skips user lookup."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=job)))
    return db


def _auth_db(user_id: uuid.UUID, job) -> AsyncMock:
    """DB mock for authenticated requests: user lookup first, then job lookup."""
    user = MagicMock()
    user.id = user_id
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=user)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=job)),
        ]
    )
    return db


def _make_job(user_id: uuid.UUID, mode: str = "generative", job_type: str = "default") -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user_id
    job.mode = mode
    job.job_type = job_type
    _now = datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC)
    job.status = "done"
    job.assembly_plan = {"variants": []}
    job.error_detail = None
    job.created_at = _now
    job.updated_at = _now
    job.edit_format = None
    job.current_phase = None
    job.phase_log = None
    job.started_at = None
    job.finished_at = None
    job.music_track_id = None
    job.template_id = None
    job.failure_reason = None
    job.all_candidates = {}
    return job


# ── ensure_job_owner unit tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_job_owner_none_user_id_allows():
    from app.auth import ensure_job_owner

    caller = MagicMock()
    caller.id = uuid.uuid4()
    ensure_job_owner(None, caller)  # must not raise


@pytest.mark.asyncio
async def test_ensure_job_owner_synthetic_user_id_allows():
    from app.auth import SYNTHETIC_USER_ID as SYN
    from app.auth import ensure_job_owner

    caller = MagicMock()
    caller.id = uuid.uuid4()
    ensure_job_owner(SYN, caller)  # must not raise


@pytest.mark.asyncio
async def test_ensure_job_owner_matching_user_allows():
    from app.auth import ensure_job_owner

    caller_id = uuid.uuid4()
    caller = MagicMock()
    caller.id = caller_id
    ensure_job_owner(caller_id, caller)  # must not raise


@pytest.mark.asyncio
async def test_ensure_job_owner_wrong_user_raises_404():
    from fastapi import HTTPException

    from app.auth import ensure_job_owner

    owner_id = uuid.uuid4()
    caller = MagicMock()
    caller.id = uuid.uuid4()  # different user
    with pytest.raises(HTTPException) as exc_info:
        ensure_job_owner(owner_id, caller)
    assert exc_info.value.status_code == 404


# ── generative status ─────────────────────────────────────────────────────────


def test_generative_status_anonymous_synthetic_job_allowed():
    """No-header poll of a synthetic-owned job → 200 (anonymous public flow preserved)."""
    job = _make_job(SYNTHETIC_USER_ID)
    db = _anon_db(job)

    with patch("app.services.phase_baselines.get_baselines", return_value=None):
        client = _client(db)
        resp = client.get(f"/generative-jobs/{job.id}/status")

    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text


def test_generative_status_no_headers_real_user_job_404():
    """No-header poll of a real-user-owned job → 404 (unauthenticated caller)."""
    real_owner = uuid.uuid4()
    job = _make_job(real_owner)
    db = _anon_db(job)

    with patch("app.services.phase_baselines.get_baselines", return_value=None):
        client = _client(db)
        resp = client.get(f"/generative-jobs/{job.id}/status")

    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_generative_status_wrong_user_real_job_404():
    """Authenticated poll of a real-user job by a different user → 404."""
    real_owner = uuid.uuid4()
    caller_id = uuid.uuid4()  # different from real_owner
    job = _make_job(real_owner)
    db = _auth_db(caller_id, job)

    with patch("app.services.phase_baselines.get_baselines", return_value=None):
        client = _client(db)
        resp = client.get(
            f"/generative-jobs/{job.id}/status",
            headers={"X-User-Id": str(caller_id), "Authorization": _BEARER},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_generative_status_owner_real_job_200():
    """Authenticated poll of a real-user job by the owner → 200."""
    owner_id = uuid.uuid4()
    job = _make_job(owner_id)
    db = _auth_db(owner_id, job)

    with patch("app.services.phase_baselines.get_baselines", return_value=None):
        client = _client(db)
        resp = client.get(
            f"/generative-jobs/{job.id}/status",
            headers={"X-User-Id": str(owner_id), "Authorization": _BEARER},
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text


# ── generative mutation (retext) ──────────────────────────────────────────────


def test_generative_retext_no_headers_real_user_job_404():
    real_owner = uuid.uuid4()
    job = _make_job(real_owner)
    db = _anon_db(job)

    client = _client(db)
    resp = client.post(
        f"/generative-jobs/{job.id}/variants/v1/retext",
        json={"text": "hello", "remove": False},
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_generative_retext_wrong_user_real_job_404():
    real_owner = uuid.uuid4()
    caller_id = uuid.uuid4()
    job = _make_job(real_owner)
    db = _auth_db(caller_id, job)

    client = _client(db)
    resp = client.post(
        f"/generative-jobs/{job.id}/variants/v1/retext",
        json={"text": "hello", "remove": False},
        headers={"X-User-Id": str(caller_id), "Authorization": _BEARER},
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_generative_retext_owner_real_job_enqueues():
    """Owner calling retext on their job gets past the ownership check."""
    owner_id = uuid.uuid4()
    job = _make_job(owner_id)
    db = _auth_db(owner_id, job)

    with patch("app.routes.generative_jobs.dispatch_retext"):
        client = _client(db)
        resp = client.post(
            f"/generative-jobs/{job.id}/variants/v1/retext",
            json={"text": "hello", "remove": False},
            headers={"X-User-Id": str(owner_id), "Authorization": _BEARER},
        )
    app.dependency_overrides.clear()
    # 404 here means ownership blocked — owner should always pass
    assert resp.status_code != 404, resp.text


# ── music status ──────────────────────────────────────────────────────────────


def test_music_status_anonymous_synthetic_job_allowed():
    job = _make_job(SYNTHETIC_USER_ID, mode="music", job_type="music")
    db = _anon_db(job)

    client = _client(db)
    resp = client.get(f"/music-jobs/{job.id}/status")
    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text


def test_music_status_no_headers_real_user_job_404():
    real_owner = uuid.uuid4()
    job = _make_job(real_owner, mode="music", job_type="music")
    db = _anon_db(job)

    client = _client(db)
    resp = client.get(f"/music-jobs/{job.id}/status")
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_music_status_wrong_user_real_job_404():
    real_owner = uuid.uuid4()
    caller_id = uuid.uuid4()
    job = _make_job(real_owner, mode="music", job_type="music")
    db = _auth_db(caller_id, job)

    client = _client(db)
    resp = client.get(
        f"/music-jobs/{job.id}/status",
        headers={"X-User-Id": str(caller_id), "Authorization": _BEARER},
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_music_status_owner_real_job_200():
    owner_id = uuid.uuid4()
    job = _make_job(owner_id, mode="music", job_type="music")
    db = _auth_db(owner_id, job)

    client = _client(db)
    resp = client.get(
        f"/music-jobs/{job.id}/status",
        headers={"X-User-Id": str(owner_id), "Authorization": _BEARER},
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text


# ── template status ───────────────────────────────────────────────────────────


def test_template_status_anonymous_synthetic_job_allowed():
    job = _make_job(SYNTHETIC_USER_ID, mode="template", job_type="template")
    db = _anon_db(job)

    client = _client(db)
    resp = client.get(f"/template-jobs/{job.id}/status")
    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text


def test_template_status_no_headers_real_user_job_404():
    real_owner = uuid.uuid4()
    job = _make_job(real_owner, mode="template", job_type="template")
    db = _anon_db(job)

    client = _client(db)
    resp = client.get(f"/template-jobs/{job.id}/status")
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_template_status_wrong_user_real_job_404():
    real_owner = uuid.uuid4()
    caller_id = uuid.uuid4()
    job = _make_job(real_owner, mode="template", job_type="template")
    db = _auth_db(caller_id, job)

    client = _client(db)
    resp = client.get(
        f"/template-jobs/{job.id}/status",
        headers={"X-User-Id": str(caller_id), "Authorization": _BEARER},
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text


def test_template_status_owner_real_job_200():
    owner_id = uuid.uuid4()
    job = _make_job(owner_id, mode="template", job_type="template")
    db = _auth_db(owner_id, job)

    client = _client(db)
    resp = client.get(
        f"/template-jobs/{job.id}/status",
        headers={"X-User-Id": str(owner_id), "Authorization": _BEARER},
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text


# ── content_plan readability ──────────────────────────────────────────────────


def test_generative_status_content_plan_job_owner_allowed():
    """content_plan-mode job is readable by its owner via the generative status endpoint."""
    owner_id = uuid.uuid4()
    job = _make_job(owner_id, mode="content_plan")
    db = _auth_db(owner_id, job)

    with patch("app.services.phase_baselines.get_baselines", return_value=None):
        client = _client(db)
        resp = client.get(
            f"/generative-jobs/{job.id}/status",
            headers={"X-User-Id": str(owner_id), "Authorization": _BEARER},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text


def test_generative_status_content_plan_job_wrong_user_404():
    """content_plan-mode job returns 404 for a different user."""
    real_owner = uuid.uuid4()
    caller_id = uuid.uuid4()
    job = _make_job(real_owner, mode="content_plan")
    db = _auth_db(caller_id, job)

    with patch("app.services.phase_baselines.get_baselines", return_value=None):
        client = _client(db)
        resp = client.get(
            f"/generative-jobs/{job.id}/status",
            headers={"X-User-Id": str(caller_id), "Authorization": _BEARER},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 404, resp.text
