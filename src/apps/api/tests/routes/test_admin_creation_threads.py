"""Admin creation-thread integrity report: read-only, no signed URLs, admin-gated."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import ContentPlan, CreatorAgentSession, Job, PlanItem
from app.routes.admin_creation_threads import creation_thread_integrity

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _execute_results(*, thread, tombstone, event_row) -> AsyncMock:
    thread_result = Mock()
    thread_result.scalar_one_or_none.return_value = thread
    tombstone_result = Mock()
    tombstone_result.scalar_one_or_none.return_value = tombstone
    events_result = Mock()
    events_result.one.return_value = event_row
    return AsyncMock(side_effect=[thread_result, tombstone_result, events_result])


def _db_get(*, item=None, plan=None, session=None, job=None) -> AsyncMock:
    async def _get(model, _row_id=None, *_a, **_kw):
        if model is PlanItem:
            return item
        if model is ContentPlan:
            return plan
        if model is CreatorAgentSession:
            return session
        if model is Job:
            return job
        return None

    return AsyncMock(side_effect=_get)


@pytest.mark.asyncio
async def test_integrity_reports_missing_thread_and_deletion_tombstone() -> None:
    thread_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    tombstone = SimpleNamespace(creator_id=creator_id, created_at=datetime.now(UTC))
    db = Mock()
    db.execute = _execute_results(thread=None, tombstone=tombstone, event_row=None)

    out = await creation_thread_integrity(str(thread_id), db)

    assert out.thread is None
    assert out.tombstone.exists is True
    assert out.tombstone.creator_id == str(creator_id)
    assert out.video is None
    assert out.edges == {}


@pytest.mark.asyncio
async def test_integrity_reports_thread_with_no_deletion_and_no_edges() -> None:
    thread_id = uuid.uuid4()
    db = Mock()
    db.execute = _execute_results(thread=None, tombstone=None, event_row=None)

    out = await creation_thread_integrity(str(thread_id), db)

    assert out.thread is None
    assert out.tombstone.exists is False


@pytest.mark.asyncio
async def test_integrity_names_the_incoherent_job_edge_and_omits_signed_urls() -> None:
    creator_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    thread_id, item_id, plan_id, session_id, job_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    thread = SimpleNamespace(
        id=thread_id,
        creator_id=creator_id,
        runtime_version=1,
        status="active",
        revision=2,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=session_id,
        active_job_id=job_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)
    plan = SimpleNamespace(id=plan_id, user_id=creator_id, ownership_epoch=0)
    session = SimpleNamespace(
        id=session_id,
        creator_id=creator_id,
        plan_item_id=item_id,
        target_job_id=job_id,
        ownership_epoch=0,
    )
    # Cross-owned: user_id does not match the thread's creator_id.
    job = SimpleNamespace(
        id=job_id,
        user_id=other_user_id,
        content_plan_item_id=item_id,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "video_path": "users/x/output.mp4",
                    "output_url": "https://signed.example/should-never-appear",
                }
            ]
        },
    )
    db = Mock()
    db.execute = _execute_results(thread=thread, tombstone=None, event_row=(3, 10, 12))
    db.get = _db_get(item=item, plan=plan, session=session, job=job)

    out = await creation_thread_integrity(str(thread_id), db)

    assert out.thread is not None
    assert out.thread.id == str(thread_id)
    assert out.events.count == 3
    assert out.events.min_sequence == 10
    assert out.events.max_sequence == 12

    assert out.edges["plan_item"].coherent is True
    assert out.edges["creator_agent_session"].coherent is True
    assert out.edges["job"].coherent is False
    assert out.edges["job"].reason == "job_incoherent"

    # The video is still reported (for recovery triage) even though its
    # ownership edge is incoherent -- but never with a signed URL.
    assert out.video is not None
    assert out.video.job_status == "variants_ready"
    assert out.video.variant_count == 1
    variant = out.video.variants[0]
    assert variant.variant_id == "original_text"
    assert variant.render_status == "ready"
    assert variant.has_video_path is True
    assert not hasattr(variant, "output_url")
    dumped = variant.model_dump()
    assert "output_url" not in dumped
    assert "signed" not in str(dumped)


def test_integrity_requires_admin_auth(client: TestClient) -> None:
    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _db
    try:
        with patch("app.routes.admin.settings") as settings:
            settings.admin_api_key = ADMIN_TOKEN
            response = client.get(
                f"/admin/creation-threads/{uuid.uuid4()}/integrity",
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code in {401, 422}


def test_integrity_rejects_malformed_thread_id(client: TestClient) -> None:
    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _db
    try:
        with patch("app.routes.admin.settings") as settings:
            settings.admin_api_key = ADMIN_TOKEN
            response = client.get(
                "/admin/creation-threads/not-a-uuid/integrity",
                headers={"X-Admin-Token": ADMIN_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 422
