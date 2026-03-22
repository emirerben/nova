"""Integration tests for POST /api/waitlist and GET /api/admin/waitlist."""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Ensure settings can load before importing app modules
os.environ.setdefault("WAITLIST_ADMIN_SECRET", "test-admin-secret")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")

from app.database import get_db
from app.main import app
from app.models import Base

# ── In-memory SQLite engine for tests ─────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        # Only create the waitlist table — other models use Postgres-specific types
        # (BYTEA, UUID) that SQLite doesn't support.
        await conn.run_sync(WaitlistSignup.__table__.create)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_engine):
    Session = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── Happy path ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_success(client):
    res = await client.post("/api/waitlist", json={"email": "alice@example.com"})
    assert res.status_code == 201
    assert res.json() == {"message": "success"}


@pytest.mark.asyncio
async def test_email_normalization(client, db_engine):
    res = await client.post("/api/waitlist", json={"email": "USER@Gmail.com"})
    assert res.status_code == 201

    from sqlalchemy import text

    async with db_engine.connect() as conn:
        row = (await conn.execute(text("SELECT email FROM waitlist_signups LIMIT 1"))).fetchone()
    assert row is not None
    assert row[0] == "user@gmail.com"


# ── Duplicate handling ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_duplicate(client):
    await client.post("/api/waitlist", json={"email": "bob@example.com"})
    res = await client.post("/api/waitlist", json={"email": "bob@example.com"})
    assert res.status_code == 409
    assert res.json()["detail"] == "already_registered"


@pytest.mark.asyncio
async def test_signup_case_insensitive_dup(client):
    """Normalization + UNIQUE constraint together must reject case variants."""
    await client.post("/api/waitlist", json={"email": "USER@Gmail.com"})
    res = await client.post("/api/waitlist", json={"email": "user@gmail.com"})
    assert res.status_code == 409


# ── Validation ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_invalid_email(client):
    res = await client.post("/api/waitlist", json={"email": "notanemail"})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_signup_empty_email(client):
    res = await client.post("/api/waitlist", json={"email": ""})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_signup_whitespace_email(client):
    res = await client.post("/api/waitlist", json={"email": "   "})
    assert res.status_code == 422


# ── Rate limiting ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_rate_limit(client):
    """6th request from same IP within a minute must return 429."""
    headers = {"X-Forwarded-For": "10.0.0.99"}  # unique IP for this test
    for i in range(5):
        res = await client.post(
            "/api/waitlist",
            json={"email": f"ratelimit{i}@example.com"},
            headers=headers,
        )
        assert res.status_code == 201

    res = await client.post(
        "/api/waitlist",
        json={"email": "ratelimit5@example.com"},
        headers=headers,
    )
    assert res.status_code == 429


# ── Admin endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_no_auth(client):
    res = await client.get("/api/admin/waitlist")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_wrong_secret(client):
    res = await client.get("/api/admin/waitlist", headers={"X-Admin-Secret": "wrong"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_valid_secret(client):
    await client.post("/api/waitlist", json={"email": "carol@example.com"})

    res = await client.get(
        "/api/admin/waitlist", headers={"X-Admin-Secret": "test-admin-secret"}
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["email"] == "carol@example.com"
    assert "id" in data[0]
    assert "created_at" in data[0]
    assert "invited_at" in data[0]
    assert data[0]["invited_at"] is None
