"""Holdout exclusion history includes expired/superseded development, no bodies."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import CreationThread, RequestFollowingCase, User
from app.routes import admin_request_following
from app.services.request_following_cases import digest


@pytest.mark.asyncio
async def test_inventory_is_admin_only_metadata_and_keeps_old_exposure(monkeypatch):
    owner, sealed_owner = uuid.uuid4(), uuid.uuid4()
    threads = [uuid.uuid4() for _ in range(3)]
    monkeypatch.setattr(settings, "admin_api_key", "inventory-test")
    async with AsyncSessionLocal() as db:
        db.add_all([User(id=u, email=f"{u}@inventory.test") for u in [owner, sealed_owner]])
        await db.flush()
        db.add_all(
            [
                CreationThread(id=t, creator_id=owner if i < 2 else sealed_owner)
                for i, t in enumerate(threads)
            ]
        )
        await db.flush()
        for i, t in enumerate(threads):
            db.add(
                RequestFollowingCase(
                    creator_id=owner if i < 2 else sealed_owner,
                    thread_id=t,
                    source_sha256=str(i) * 64,
                    eligibility_key="expired-grant",
                    split="development" if i < 2 else "holdout",
                    superseded=i == 0,
                    capture={"private": "DO NOT LEAK"} if i < 2 else None,
                    expires_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        await db.commit()
    app = FastAPI()
    app.include_router(admin_request_following.router, prefix="/admin/request-following")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/admin/request-following/comparison-development-groups"
            assert (await client.get(path, headers={"X-Admin-Token": "wrong"})).status_code == 401
            response = await client.get(path, headers={"X-Admin-Token": "inventory-test"})
            assert response.status_code == 200
            value = response.json()
            assert f"creator:{owner}" in value["groups"]
            assert all(f"event:{t}" in value["groups"] for t in threads[:2])
            assert f"creator:{sealed_owner}" not in value["groups"]
            assert str(threads[2]) not in response.text and "DO NOT LEAK" not in response.text
            assert value["groups_sha256"] == digest(value["groups"])
            assert value["owner_map_complete"] is True
            assert value["media_map_complete"] is False
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id.in_([owner, sealed_owner])))
            await db.commit()
