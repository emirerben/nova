"""Admin HTTP + real PostgreSQL: capture grants cannot bypass owner/split/expiry."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import CreationThread, InternalAccountGrant, User
from app.routes import admin_request_following


@pytest.mark.asyncio
async def test_comparison_read_boundary_revocation_holdout_and_owner(monkeypatch):
    owner, thread, grant = [uuid.uuid4() for _ in range(3)]
    monkeypatch.setattr(settings, "admin_api_key", "comparison-admin")
    monkeypatch.setattr(settings, "creator_comparison_capture_enabled", True)
    monkeypatch.setattr(settings, "creator_comparison_capture_user_ids", [str(owner)])
    monkeypatch.setattr(settings, "request_following_all_users", False)
    monkeypatch.setattr(settings, "request_following_user_ids", [str(owner)])
    monkeypatch.setattr(settings, "request_following_holdout_thread_ids", [])
    async with AsyncSessionLocal() as db:
        db.add(User(id=owner, email=f"{owner}@comparison.test"))
        await db.flush()
        db.add(CreationThread(id=thread, creator_id=owner))
        db.add(
            InternalAccountGrant(
                id=grant, creator_id=owner, granted_by="test", idempotency_key=str(grant)
            )
        )
        await db.commit()
    now = datetime.now(UTC)
    params = {
        "creator_id": str(owner),
        "thread_id": str(thread),
        "grant_key": f"internal_grant:{grant}",
        "captured_at": now.isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
    }
    app = FastAPI()
    app.include_router(admin_request_following.router, prefix="/admin/request-following")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            url = "/admin/request-following/comparison-eligibility"
            assert (
                await client.get(url, params=params, headers={"X-Admin-Token": "wrong"})
            ).status_code == 401
            client.headers["X-Admin-Token"] = "comparison-admin"
            result = await client.get(url, params=params)
            assert result.status_code == 200 and result.json()["eligible"] is True
            assert set(result.json()) == {"eligible", "checked_at"}
            assert not (
                await client.get(url, params={**params, "creator_id": str(uuid.uuid4())})
            ).json()["eligible"]
            assert not (
                await client.get(url, params={**params, "grant_key": "internal_grant:old"})
            ).json()["eligible"]
            monkeypatch.setattr(settings, "request_following_holdout_thread_ids", [str(thread)])
            assert not (await client.get(url, params=params)).json()["eligible"]
            monkeypatch.setattr(settings, "creator_comparison_holdout_token", "sealed-test-token")
            sealed_url = "/admin/request-following/comparison-holdout-eligibility"
            assert (await client.get(sealed_url, params=params)).status_code == 403
            held_response = await client.get(
                sealed_url,
                params=params,
                headers={"X-Comparison-Evaluator-Token": "sealed-test-token"},
            )
            assert held_response.status_code == 200 and held_response.json()["eligible"]
            # The ordinary body and eligibility readers never open the holdout.
            assert not (await client.get(url, params=params)).json()["eligible"]
            monkeypatch.setattr(settings, "request_following_holdout_thread_ids", [])
            async with AsyncSessionLocal() as db:
                row = await db.get(InternalAccountGrant, grant)
                row.status = "revoked"
                await db.commit()
            assert not (await client.get(url, params=params)).json()["eligible"]
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id == owner))
            await db.commit()
