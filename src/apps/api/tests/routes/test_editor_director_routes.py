from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.main import app
from app.routes import plan_items
from app.routes._director import DirectorSuggestionsResponse
from app.routes._omni import OmniAssetResponse


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()
    settings.edit_director_enabled = False
    settings.omni_generated_video_enabled = False


def _result(value) -> MagicMock:  # noqa: ANN001
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _owned(user_id: uuid.UUID, *, owner_id: uuid.UUID | None = None):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.status = "variants_ready"
    job.all_candidates = {"clip_paths": ["source.mp4"]}
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "v1",
                "render_status": "ready",
                "ai_timeline": {
                    "slots": [
                        {
                            "clip_index": 0,
                            "duration_s": 5.0,
                            "source_duration_s": 5.0,
                        }
                    ]
                },
            }
        ]
    }
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.current_job = job
    plan = MagicMock()
    plan.user_id = owner_id or user_id
    user = MagicMock()
    user.id = user_id
    return user, item, plan, job


def _install(user, item, plan, *, extra_results=()) -> AsyncMock:  # noqa: ANN001
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[_result(item), *[_result(value) for value in extra_results]]
    )
    db.get = AsyncMock(return_value=plan)
    db.commit = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return db


def _director_body() -> dict:
    return {
        "snapshot": {
            "allowed_op_families": ["text", "timeline", "effect"],
            "total_duration_s": 5,
            "text_bars": [],
            "slots": [],
        },
        "snapshot_revision": "revision-1",
        "dismissed_suggestion_ids": [],
    }


def _director_response() -> DirectorSuggestionsResponse:
    return DirectorSuggestionsResponse(
        suggestions=[],
        snapshot_revision="revision-1",
        requested_model="gemini-3.1-pro-preview",
        model_used="gemini-3.1-pro-preview",
    )


def test_director_routes_require_authentication(client: TestClient) -> None:
    response = client.post(
        f"/plan-items/{uuid.uuid4()}/variants/v1/director/suggestions",
        json=_director_body(),
    )
    assert response.status_code == 401


def test_director_flag_and_ownership_fail_closed(client: TestClient, monkeypatch) -> None:
    user_id = uuid.uuid4()
    user, item, plan, _ = _owned(user_id)
    _install(user, item, plan)
    run = AsyncMock(return_value=_director_response())
    monkeypatch.setattr(plan_items, "run_director", run)

    disabled = client.post(
        f"/plan-items/{item.id}/variants/v1/director/suggestions",
        json=_director_body(),
    )
    assert disabled.status_code == 404
    run.assert_not_awaited()

    settings.edit_director_enabled = True
    user, item, foreign_plan, _ = _owned(user_id, owner_id=uuid.uuid4())
    _install(user, item, foreign_plan)
    foreign = client.post(
        f"/plan-items/{item.id}/variants/v1/director/suggestions",
        json=_director_body(),
    )
    assert foreign.status_code == 404
    run.assert_not_awaited()


def test_director_oversized_snapshot_and_invalid_variant_reject(client: TestClient) -> None:
    settings.edit_director_enabled = True
    user, item, plan, _ = _owned(uuid.uuid4())
    _install(user, item, plan)
    body = _director_body()
    body["snapshot"] = {"text": "x" * (21 * 1024)}
    response = client.post(
        f"/plan-items/{item.id}/variants/v1/director/suggestions",
        json=body,
    )
    assert response.status_code == 422

    user, item, plan, _ = _owned(uuid.uuid4())
    _install(user, item, plan)
    missing = client.post(
        f"/plan-items/{item.id}/variants/missing/director/suggestions",
        json=_director_body(),
    )
    assert missing.status_code == 404


def test_director_suggestion_rate_limit(client: TestClient, monkeypatch) -> None:
    settings.edit_director_enabled = True
    user, item, plan, _ = _owned(uuid.uuid4())
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(item))
    db.get = AsyncMock(return_value=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(
        plan_items,
        "run_director",
        AsyncMock(return_value=_director_response()),
    )
    url = f"/plan-items/{item.id}/variants/v1/director/suggestions"
    headers = {"X-Forwarded-For": f"198.51.100.{uuid.uuid4().int % 200 + 1}"}
    statuses = [
        client.post(url, json=_director_body(), headers=headers).status_code for _ in range(11)
    ]
    assert statuses[-1] == 429


def test_director_feedback_records_acceptance_without_mutating_job(
    client: TestClient,
    monkeypatch,
) -> None:
    settings.edit_director_enabled = True
    user, item, plan, job = _owned(uuid.uuid4())
    _install(user, item, plan)
    baseline = dict(job.assembly_plan)
    record = MagicMock()
    monkeypatch.setattr(plan_items, "record_director_feedback", record)

    response = client.post(
        f"/plan-items/{item.id}/variants/v1/director/feedback",
        json={
            "suggestion_id": "director-1",
            "action": "accepted",
            "category": "text",
            "model_used": "gemini-3.1-pro-preview",
        },
    )

    assert response.status_code == 204
    record.assert_called_once()
    assert job.assembly_plan == baseline


def _omni_body() -> dict:
    return {
        "suggestion_id": "director-1",
        "draft_revision": "v1-test",
        "action": "generate_insert",
        "prompt": "A restrained visual bridge",
        "insert_at_s": 2,
        "duration_s": 4,
    }


def test_omni_start_flag_off_and_missing_asset(client: TestClient) -> None:
    user, item, plan, _ = _owned(uuid.uuid4())
    _install(user, item, plan)
    start = client.post(
        f"/plan-items/{item.id}/variants/v1/omni-assets",
        json=_omni_body(),
    )
    assert start.status_code == 404

    user, item, plan, _ = _owned(uuid.uuid4())
    _install(user, item, plan)
    missing = client.get(
        f"/plan-items/{item.id}/variants/v1/omni-assets/{uuid.uuid4()}",
    )
    assert missing.status_code == 404


def test_omni_start_rate_limit(client: TestClient, monkeypatch) -> None:
    user, item, plan, _ = _owned(uuid.uuid4())
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(item))
    db.get = AsyncMock(return_value=plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    response = OmniAssetResponse(
        asset_id="asset-1",
        status="queued",
        progress=0.02,
        model="gemini-omni-flash-preview",
    )
    monkeypatch.setattr(plan_items, "start_omni_asset", AsyncMock(return_value=response))
    url = f"/plan-items/{item.id}/variants/v1/omni-assets"
    headers = {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 200 + 1}"}
    statuses = [client.post(url, json=_omni_body(), headers=headers).status_code for _ in range(4)]
    assert statuses[-1] == 429
