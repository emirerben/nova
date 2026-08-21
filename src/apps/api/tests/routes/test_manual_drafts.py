"""Route contract for the hidden, resumable manual-editor lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.auth import get_current_user
from app.config import settings
from app.database import AsyncSessionLocal, get_db, sync_session
from app.main import app
from app.models import ContentPlan, Job, Persona, PlanItem, User
from app.routes.generative_jobs import _durable_sources_prefix
from app.routes.manual_drafts import ManualDraftCreateBody, create_manual_draft


def _scalar(value) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    result.scalar_one = MagicMock(return_value=value)
    return result


def _scalars(values: list) -> MagicMock:
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=values)))
    return result


def _db(results: list) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    return user


def _plan(user_id: uuid.UUID) -> MagicMock:
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.ownership_epoch = 4
    return plan


def _item(plan_id: uuid.UUID, *, paths: list[str] | None = None) -> MagicMock:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = plan_id
    item.position = 1
    item.current_job_id = uuid.uuid4()
    item.clip_gcs_paths = paths or []
    return item


def _job(user_id: uuid.UUID, item_id: uuid.UUID) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user_id
    job.mode = "manual_draft"
    job.status = "draft"
    job.content_plan_item_id = item_id
    job.content_plan_ownership_epoch = 4
    job.assembly_plan = {"manual_draft": True, "variants": []}
    job.all_candidates = {"clip_paths": [], "manual_draft": True}
    return job


client = TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


def test_create_manual_draft_links_one_item_and_job(monkeypatch) -> None:
    user = _user()
    plan = _plan(user.id)
    db = _db([_scalar(plan), _scalar(None), _scalar(0)])
    _override(user, db)
    monkeypatch.setattr(
        "app.routes.manual_drafts.load_owned_plan_persona",
        AsyncMock(return_value=MagicMock()),
    )

    response = client.post("/plan-items/manual-drafts", json={"title": "  My first cut  "})

    assert response.status_code == 201
    item, job = [call.args[0] for call in db.add.call_args_list]
    assert item.id == job.content_plan_item_id
    assert item.current_job_id == job.id
    assert item.day_index is None
    assert item.content_mode == "existing_footage"
    assert item.theme == "My first cut"
    assert job.mode == "manual_draft"
    assert job.status == "draft"
    assert job.content_plan_ownership_epoch == 4
    assert response.json() == {
        "plan_item_id": str(item.id),
        "job_id": str(job.id),
        "variant_id": None,
        "status": "draft",
    }


def test_create_manual_draft_resumes_latest_unexported_job(monkeypatch) -> None:
    user = _user()
    plan = _plan(user.id)
    item = _item(plan.id)
    job = _job(user.id, item.id)
    job.id = item.current_job_id
    db = _db([_scalar(plan), _scalar(item), _scalar(job)])
    _override(user, db)
    monkeypatch.setattr(
        "app.routes.manual_drafts.load_owned_plan_persona",
        AsyncMock(return_value=MagicMock()),
    )

    response = client.post("/plan-items/manual-drafts", json={})

    assert response.status_code == 201
    assert response.json()["plan_item_id"] == str(item.id)
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_create_manual_draft_requests_converge_on_one_item_and_job() -> None:
    """Two real sessions serialize on the plan and both return the same draft."""

    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        pytest.skip(f"refusing to write to non-test database {db_name!r}")
    try:
        with sync_session() as probe:
            probe.execute(text("select 1"))
    except OperationalError:
        pytest.skip("nova_test Postgres not reachable")

    user_id = uuid.uuid4()
    with sync_session() as setup:
        setup.add(User(id=user_id, email=f"{user_id}@test.local"))
        setup.flush()
        persona = Persona(
            user_id=user_id,
            persona_status="ready",
            persona={"content_mode": "travel", "tone": "direct"},
        )
        setup.add(persona)
        setup.flush()
        plan = ContentPlan(user_id=user_id, persona_id=persona.id, plan_status="ready")
        setup.add(plan)
        setup.commit()
        plan_id = plan.id

    start = asyncio.Event()

    async def submit() -> object:
        async with AsyncSessionLocal() as session:
            await start.wait()
            return await create_manual_draft(
                ManualDraftCreateBody(title="Concurrent draft"),
                SimpleNamespace(id=user_id),
                session,
            )

    first = asyncio.create_task(submit())
    second = asyncio.create_task(submit())
    start.set()
    responses = await asyncio.gather(first, second)

    assert {response.plan_item_id for response in responses} == {responses[0].plan_item_id}
    assert {response.job_id for response in responses} == {responses[0].job_id}
    with sync_session() as check:
        pairs = check.execute(
            select(PlanItem, Job)
            .join(Job, Job.id == PlanItem.current_job_id)
            .where(PlanItem.content_plan_id == plan_id, Job.mode == "manual_draft")
        ).all()
    assert len(pairs) == 1


def test_initialize_manual_draft_seeds_ordered_video_timeline(monkeypatch) -> None:
    user = _user()
    plan = _plan(user.id)
    paths = [
        f"users/{user.id}/plan/item/one.mp4",
        f"users/{user.id}/plan/item/two.mp4",
    ]
    item = _item(plan.id, paths=paths)
    job = _job(user.id, item.id)
    job.id = item.current_job_id
    db = _db([_scalar(item), _scalar(plan), _scalar(item), _scalar(job)])
    _override(user, db)
    monkeypatch.setattr(
        "app.routes.manual_drafts.load_owned_plan_persona",
        AsyncMock(return_value=MagicMock()),
    )

    response = client.post(
        f"/plan-items/{item.id}/manual-draft/initialize",
        json={
            "media": [
                {"gcs_path": paths[0], "duration_s": 9.5, "kind": "video"},
                {"gcs_path": paths[1], "duration_s": 3, "kind": "video"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["variant_id"] == "original_text"
    variant = job.assembly_plan["variants"][0]
    slots = variant["ai_timeline"]["slots"]
    assert [slot["source_gcs_path"] for slot in slots] == paths
    assert [slot["clip_index"] for slot in slots] == [0, 1]
    assert [slot["duration_s"] for slot in slots] == [5.0, 3.0]
    assert variant["render_status"] == "draft"
    assert variant["manual_draft"] is True
    assert job.all_candidates["clip_paths"] == paths
    db.commit.assert_awaited_once()


def test_repeat_initialize_rejects_changed_attached_media(monkeypatch) -> None:
    user = _user()
    plan = _plan(user.id)
    old_path = f"users/{user.id}/plan/item/old.mp4"
    new_path = f"users/{user.id}/plan/item/new.mp4"
    item = _item(plan.id, paths=[new_path])
    job = _job(user.id, item.id)
    job.id = item.current_job_id
    job.assembly_plan = {
        "manual_draft": True,
        "variants": [
            {
                "variant_id": "original_text",
                "ai_timeline": {"slots": [{"source_gcs_path": old_path}]},
            }
        ],
    }
    db = _db([_scalar(item), _scalar(plan), _scalar(item), _scalar(job)])
    _override(user, db)
    monkeypatch.setattr(
        "app.routes.manual_drafts.load_owned_plan_persona",
        AsyncMock(return_value=MagicMock()),
    )

    response = client.post(
        f"/plan-items/{item.id}/manual-draft/initialize",
        json={"media": [{"gcs_path": new_path, "duration_s": 5, "kind": "video"}]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Draft footage changed. Start a new manual draft to rebuild the timeline."
    )
    db.commit.assert_not_awaited()


def test_initialize_manual_draft_rejects_photos_before_editor(monkeypatch) -> None:
    user = _user()
    plan = _plan(user.id)
    paths = [
        f"users/{user.id}/plan/item/one.mp4",
        f"users/{user.id}/plan/item/two.jpg",
    ]
    item = _item(plan.id, paths=paths)
    job = _job(user.id, item.id)
    job.id = item.current_job_id
    db = _db([_scalar(item), _scalar(plan), _scalar(item), _scalar(job)])
    _override(user, db)
    monkeypatch.setattr(
        "app.routes.manual_drafts.load_owned_plan_persona",
        AsyncMock(return_value=MagicMock()),
    )

    response = client.post(
        f"/plan-items/{item.id}/manual-draft/initialize",
        json={
            "media": [
                {"gcs_path": paths[0], "duration_s": 5, "kind": "video"},
                {"gcs_path": paths[1], "duration_s": 3, "kind": "image"},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Photo timelines are not available in the manual editor yet. "
        "Use Make a video with Kria for photos, or choose videos only."
    )
    assert job.assembly_plan["variants"] == []
    db.commit.assert_not_awaited()


def test_initialize_requires_media_metadata_in_attached_order(monkeypatch) -> None:
    user = _user()
    plan = _plan(user.id)
    paths = [
        f"users/{user.id}/plan/item/one.mp4",
        f"users/{user.id}/plan/item/two.mp4",
    ]
    item = _item(plan.id, paths=paths)
    job = _job(user.id, item.id)
    job.id = item.current_job_id
    db = _db([_scalar(item), _scalar(plan), _scalar(item), _scalar(job)])
    _override(user, db)
    monkeypatch.setattr(
        "app.routes.manual_drafts.load_owned_plan_persona",
        AsyncMock(return_value=MagicMock()),
    )

    response = client.post(
        f"/plan-items/{item.id}/manual-draft/initialize",
        json={
            "media": [
                {"gcs_path": paths[1], "duration_s": 5, "kind": "video"},
                {"gcs_path": paths[0], "duration_s": 5, "kind": "video"},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Media metadata must match the attached footage order."
    db.commit.assert_not_awaited()


def test_initialize_rejects_client_media_kind_that_disagrees_with_path(monkeypatch) -> None:
    user = _user()
    plan = _plan(user.id)
    path = f"users/{user.id}/plan/item/photo.jpg"
    item = _item(plan.id, paths=[path])
    job = _job(user.id, item.id)
    job.id = item.current_job_id
    db = _db([_scalar(item), _scalar(plan), _scalar(item), _scalar(job)])
    _override(user, db)
    monkeypatch.setattr(
        "app.routes.manual_drafts.load_owned_plan_persona",
        AsyncMock(return_value=MagicMock()),
    )

    response = client.post(
        f"/plan-items/{item.id}/manual-draft/initialize",
        json={"media": [{"gcs_path": path, "duration_s": 5, "kind": "video"}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Media kind must match the attached file."
    db.commit.assert_not_awaited()


def test_initialize_cross_user_item_is_hidden() -> None:
    user = _user()
    db = _db([_scalar(None)])
    _override(user, db)

    response = client.post(f"/plan-items/{uuid.uuid4()}/manual-draft/initialize", json={})

    assert response.status_code == 404
    assert response.json()["detail"] == "Plan item not found"


def test_manual_draft_timeline_uses_the_owned_plan_item_source_prefix() -> None:
    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job = MagicMock(
        mode="manual_draft",
        user_id=user_id,
        content_plan_item_id=item_id,
    )

    assert _durable_sources_prefix(job) == f"users/{user_id}/plan/{item_id}/"


def test_exported_manual_draft_keeps_the_owned_source_prefix() -> None:
    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job = MagicMock(
        mode="content_plan",
        user_id=user_id,
        content_plan_item_id=item_id,
        assembly_plan={"manual_draft": True, "variants": []},
        all_candidates={"manual_draft": True},
    )

    assert _durable_sources_prefix(job) == f"users/{user_id}/plan/{item_id}/"
