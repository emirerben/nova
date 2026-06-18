"""Route tests for Phase 5 plan-item upload + generation endpoints (mock-DB)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _async_db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _owned_item(user_id: uuid.UUID, *, clips=None, filming_guide=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = clips or []
    item.day_index = 1
    item.theme = "t"
    item.idea = "i"
    item.filming_suggestion = None
    item.rationale = None
    item.filming_guide = filming_guide if filming_guide is not None else []
    item.current_job_id = None
    item.current_job = None
    item.item_status = "idea"
    item.user_edited = False
    # M4: explicit None so plan_item_response does not mistake MagicMock for a dict.
    item.conformance = None
    # 0055: new nullable/added fields.
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db_for(item, plan) -> AsyncMock:
    """DB mock matching _load_owned_item: the item is loaded via
    execute().scalar_one_or_none() (eager-loads current_job), the plan
    ownership check via get().

    M4: also handles _get_instruction_level's Persona get() call —
    returns a mock persona with style=None so instruction_level defaults to 'full'.
    """
    db = _async_db()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=item)
    db.execute = AsyncMock(return_value=result)

    persona_mock = MagicMock()
    persona_mock.style = None  # → instruction_level defaults to "full"

    # get() is called with different classes: ContentPlan (returns plan),
    # Persona (returns persona_mock). Use side_effect to differentiate.
    from app.models import Persona as PersonaRow  # noqa: PLC0415

    async def _get_side_effect(cls, pk):
        if cls is PersonaRow:
            return persona_mock
        return plan

    db.get = AsyncMock(side_effect=_get_side_effect)
    return db


def test_generate_requires_clips(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, clips=[])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 409


def test_generate_enqueues_when_clips_present(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{0}/plan/0/a.mp4"])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.tasks.content_plan_build.generate_plan_item_videos") as task:
        task.delay = MagicMock()
        resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 200
    task.delay.assert_called_once_with(str(item.id))


def test_attach_clips_rejects_foreign_prefix(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": ["users/someone-else/plan/x/clip.mp4"]},
    )
    assert resp.status_code == 422


def test_upload_urls_returns_signed_puts(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(
        "app.storage.presigned_put_url_for_plan_item",
        return_value=("https://signed.example/put", f"users/{user.id}/plan/{item.id}/x.mp4"),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/upload-urls",
            json={
                "files": [
                    {"filename": "x.mp4", "content_type": "video/mp4", "file_size_bytes": 1000}
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["urls"]) == 1
    assert body["urls"][0]["gcs_path"].startswith(f"users/{user.id}/plan/")


# ── filming_guide serialization ───────────────────────────────────────────────


def test_get_plan_item_returns_filming_guide(client: TestClient) -> None:
    """GET /plan-items/{id} returns filming_guide list from the item row."""
    user = _user()
    guide = [{"what": "creator to camera", "how": "eye level", "duration_s": 8}]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    # filming_guide now includes shot_id (null for pre-0052 rows without a stamped id).
    assert body["filming_guide"] == [
        {"shot_id": None, "what": "creator to camera", "how": "eye level", "duration_s": 8}
    ]


# ── M4: attach_clips fire-and-forget + instruction_level ─────────────────────


def test_attach_clips_returns_200_immediately(client: TestClient) -> None:
    """POST /plan-items/{id}/clips returns 200 without blocking on conformance analysis.

    The conformance task is fire-and-forget: analyze_item_conformance.delay()
    is called but the response must not wait for it to complete.
    """
    user = _user()
    item, plan = _owned_item(
        user.id,
        clips=[],
        filming_guide=[{"what": "creator at desk", "how": "eye level", "duration_s": 5}],
    )
    db = _db_for(item, plan)
    clip_path = f"users/{user.id}/plan/{item.id}/clip.mp4"
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={"clip_gcs_paths": [clip_path]},
        )

    assert resp.status_code == 200
    # Delay was called once with the item id (fire-and-forget).
    mock_task.delay.assert_called_once_with(str(item.id))


def test_get_plan_item_returns_instruction_level(client: TestClient) -> None:
    """GET /plan-items/{id} includes instruction_level (default 'full' when style absent)."""
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["instruction_level"] == "full"


def test_get_plan_item_returns_conformance_null_when_absent(client: TestClient) -> None:
    """GET /plan-items/{id} returns conformance=null when no verdict has run yet."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.conformance = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conformance"] is None


def test_get_plan_item_filming_guide_empty_for_legacy_items(client: TestClient) -> None:
    """Legacy items (filming_guide=[]) return filming_guide:[] in the response."""
    user = _user()
    item, plan = _owned_item(user.id, filming_guide=[])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    assert resp.json()["filming_guide"] == []


def test_plan_item_response_tolerates_malformed_guide() -> None:
    """Malformed JSONB shots (non-dict, missing keys) are skipped or defaulted."""
    from app.models import PlanItem  # noqa: PLC0415
    from app.routes.plan_items import plan_item_response  # noqa: PLC0415

    item = MagicMock(spec=PlanItem)
    item.id = uuid.uuid4()
    item.day_index = 1
    item.theme = "test theme"
    item.idea = "test idea"
    item.filming_guide = [
        "not a dict",  # skipped
        {"what": "creator plating dish"},  # kept: missing 'how' and 'duration_s' → defaults
        {"how": "wide", "duration_s": 4},  # kept: missing 'what' → "" default
    ]
    item.filming_suggestion = None
    item.rationale = None
    item.clip_gcs_paths = []
    item.current_job_id = None
    item.current_job = None
    item.item_status = "idea"
    item.user_edited = False
    item.conformance = None  # M4: must be None/dict, not a MagicMock attribute
    item.clip_assignments = []
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None

    resp = plan_item_response(item)
    # non-dict skipped; 2 dicts kept with defaults
    assert len(resp.filming_guide) == 2
    assert resp.filming_guide[0].what == "creator plating dish"
    assert resp.filming_guide[0].how == ""
    assert resp.filming_guide[0].duration_s == 1  # FilmingShotResponse default is 1 (not 0)
    assert resp.filming_guide[1].what == ""
    assert resp.filming_guide[1].how == "wide"
