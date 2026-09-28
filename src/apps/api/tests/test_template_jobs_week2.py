"""Tests for Week 2 template job endpoints: reroll + list."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app


@pytest_asyncio.fixture
async def client():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    async def override_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c
    app.dependency_overrides.clear()


# ── POST /template-jobs/:id/reroll ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_reroll_invalid_uuid(client):
    """Reroll with invalid UUID → 404."""
    res = await client.post("/template-jobs/not-a-uuid/reroll")
    assert res.status_code in (404, 422)


# ── GET /template-jobs ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_template_jobs_limit_bounds(client):
    """GET /template-jobs?limit=200 → 422 (max 100)."""
    res = await client.get("/template-jobs?limit=200")
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_list_template_jobs_negative_offset(client):
    """GET /template-jobs?offset=-1 → 422."""
    res = await client.get("/template-jobs?offset=-1")
    assert res.status_code == 422


# ── Reroll response schema ──────────────────────────────────────────────────
