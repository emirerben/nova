"""Tests for the shot-slot uploader: attach validation matrix, set_item_clips,
read-time reconciliation, conformance nulling, and shot_id stamping.

Uses the same mock-DB pattern as test_plan_item_generation.py.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.services.plan_clips import ClipAssignment, ClipAssignmentError, set_item_clips

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _shot(shot_id: str | None = None) -> dict:
    return {
        "shot_id": shot_id or uuid.uuid4().hex,
        "what": "creator to camera",
        "how": "eye level",
        "duration_s": 8,
    }


def _owned_item(user_id: uuid.UUID, *, clips=None, filming_guide=None, assignments=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = clips or []
    item.clip_assignments = assignments if assignments is not None else []
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
    item.conformance = None
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    item.edit_format = "montage"
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _db_for(item, plan) -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=item)
    db.execute = AsyncMock(return_value=result)
    persona_mock = MagicMock()
    persona_mock.style = None
    from app.models import Persona as PersonaRow  # noqa: PLC0415

    async def _get_side_effect(cls, pk):
        if cls is PersonaRow:
            return persona_mock
        return plan

    db.get = AsyncMock(side_effect=_get_side_effect)
    return db


# ── set_item_clips unit tests ─────────────────────────────────────────────────


def test_set_item_clips_derives_paths_shots_first() -> None:
    """Shot-assigned clips come before pool clips in clip_gcs_paths."""
    item = MagicMock()
    shot_a = ClipAssignment(gcs_path="users/u/plan/p/a.mp4", shot_id="sid-a")
    pool = ClipAssignment(gcs_path="users/u/plan/p/pool.mp4", shot_id=None)
    shot_b = ClipAssignment(gcs_path="users/u/plan/p/b.mp4", shot_id="sid-b")

    set_item_clips(item, [shot_a, pool, shot_b])

    assert item.clip_gcs_paths == [
        "users/u/plan/p/a.mp4",
        "users/u/plan/p/b.mp4",
        "users/u/plan/p/pool.mp4",
    ]
    assert item.clip_assignments == [
        {"gcs_path": "users/u/plan/p/a.mp4", "shot_id": "sid-a", "user_note": "", "machine_matched": False},  # noqa: E501
        {"gcs_path": "users/u/plan/p/pool.mp4", "shot_id": None, "user_note": "", "machine_matched": False},  # noqa: E501
        {"gcs_path": "users/u/plan/p/b.mp4", "shot_id": "sid-b", "user_note": "", "machine_matched": False},  # noqa: E501
    ]


def test_set_item_clips_raises_on_cap() -> None:
    # Cap was raised from 20 → 30 (multi-clip per shot: ~4 shots × 7 clips).
    item = MagicMock()
    with pytest.raises(ClipAssignmentError, match="Too many clips"):
        set_item_clips(item, [ClipAssignment(gcs_path=f"p/{i}.mp4") for i in range(31)])


def test_set_item_clips_raises_on_dup_path() -> None:
    item = MagicMock()
    with pytest.raises(ClipAssignmentError, match="Duplicate gcs_path"):
        set_item_clips(
            item,
            [
                ClipAssignment(gcs_path="p/a.mp4"),
                ClipAssignment(gcs_path="p/a.mp4"),
            ],
        )


def test_set_item_clips_allows_multiple_clips_per_shot_id() -> None:
    """Multiple clips with the same shot_id are now allowed (multi-clip per shot,
    e.g. 'film 5+ clips from the run'). The old dup-shot_id check was removed."""
    item = MagicMock()
    # Should NOT raise — two clips for the same shot_id is the multi-clip feature.
    set_item_clips(
        item,
        [
            ClipAssignment(gcs_path="p/a.mp4", shot_id="sid-1"),
            ClipAssignment(gcs_path="p/b.mp4", shot_id="sid-1"),
        ],
    )
    # Both clips for shot-1 should appear in clip_gcs_paths (shot-slot clips first).
    assert item.clip_gcs_paths == ["p/a.mp4", "p/b.mp4"]


def test_set_item_clips_allows_multiple_pool_clips() -> None:
    """Multiple pool clips (shot_id=None) are allowed; dupe check is by path only."""
    item = MagicMock()
    set_item_clips(
        item,
        [
            ClipAssignment(gcs_path="p/a.mp4", shot_id=None),
            ClipAssignment(gcs_path="p/b.mp4", shot_id=None),
        ],
    )
    assert item.clip_gcs_paths == ["p/a.mp4", "p/b.mp4"]


# ── Attach route 422 matrix ───────────────────────────────────────────────────


def test_attach_clips_rejects_foreign_prefix(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": ["users/evil/plan/x/clip.mp4"]},
    )
    assert resp.status_code == 422


def test_attach_clips_rejects_unknown_shot_id(client: TestClient) -> None:
    user = _user()
    sid = uuid.uuid4().hex
    guide = [_shot(shot_id=sid)]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    clip_path = f"users/{user.id}/plan/{item.id}/a.mp4"
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={
                "clip_gcs_paths": [clip_path],
                "assignments": [{"gcs_path": clip_path, "shot_id": "does-not-exist"}],
            },
        )
    assert resp.status_code == 422
    assert "shot_id" in resp.json()["detail"]


def test_attach_clips_allows_multiple_clips_per_shot_id(client: TestClient) -> None:
    """Multiple clips per shot_id are now accepted (multi-clip-per-shot feature).
    The route should return 200 and store both assignments with the same shot_id."""
    user = _user()
    sid = uuid.uuid4().hex
    guide = [_shot(shot_id=sid)]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    p1 = f"users/{user.id}/plan/{item.id}/a.mp4"
    p2 = f"users/{user.id}/plan/{item.id}/b.mp4"
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={
                "clip_gcs_paths": [p1, p2],
                "assignments": [
                    {"gcs_path": p1, "shot_id": sid},
                    {"gcs_path": p2, "shot_id": sid},
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    # Both clips should appear in the response
    assert len(data["clip_assignments"]) == 2
    assert all(a["shot_id"] == sid for a in data["clip_assignments"])


def test_attach_clips_rejects_dup_gcs_path(client: TestClient) -> None:
    user = _user()
    sid1 = uuid.uuid4().hex
    sid2 = uuid.uuid4().hex
    guide = [_shot(shot_id=sid1), _shot(shot_id=sid2)]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    p = f"users/{user.id}/plan/{item.id}/a.mp4"
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={
                "clip_gcs_paths": [p, p],
                "assignments": [
                    {"gcs_path": p, "shot_id": sid1},
                    {"gcs_path": p, "shot_id": sid2},
                ],
            },
        )
    assert resp.status_code == 422


def test_attach_clips_rejects_over_cap(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, filming_guide=[])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    paths = [f"users/{user.id}/plan/{item.id}/{i}.mp4" for i in range(31)]
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={
                "clip_gcs_paths": paths,
                "assignments": [{"gcs_path": p, "shot_id": None} for p in paths],
            },
        )
    assert resp.status_code == 422


def test_attach_clips_happy_path_with_assignments(client: TestClient) -> None:
    """Happy path: shot assignment accepted, conformance nulled, task fired."""
    user = _user()
    sid = uuid.uuid4().hex
    guide = [_shot(shot_id=sid)]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    clip_path = f"users/{user.id}/plan/{item.id}/a.mp4"
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={
                "clip_gcs_paths": [clip_path],
                "assignments": [{"gcs_path": clip_path, "shot_id": sid}],
            },
        )
    assert resp.status_code == 200
    mock_task.delay.assert_called_once_with(str(item.id))
    # D7: conformance must be nulled on attach.
    assert item.conformance is None


def test_attach_clips_legacy_no_assignments(client: TestClient) -> None:
    """Legacy body without assignments routes all clips to pool."""
    user = _user()
    item, plan = _owned_item(user.id, filming_guide=[])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    clip_path = f"users/{user.id}/plan/{item.id}/a.mp4"
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={"clip_gcs_paths": [clip_path]},
        )
    assert resp.status_code == 200
    # All clips should be in pool (shot_id=None).
    expected_assignments = [
        {"gcs_path": clip_path, "shot_id": None, "user_note": "", "machine_matched": False}
    ]
    assert item.clip_assignments == expected_assignments


def test_attach_clips_nulls_conformance(client: TestClient) -> None:
    """D7: conformance is always reset to None on attach so panel can't be stale."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.conformance = {"verdict": "good"}  # pre-existing verdict
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    clip_path = f"users/{user.id}/plan/{item.id}/a.mp4"
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        client.post(
            f"/plan-items/{item.id}/clips",
            json={"clip_gcs_paths": [clip_path]},
        )
    assert item.conformance is None


# ── Read-time reconciliation ──────────────────────────────────────────────────


def test_get_plan_item_reconciles_dangling_shot_id(client: TestClient) -> None:
    """Assignments with shot_id not in filming_guide are presented as pool."""
    user = _user()
    live_sid = uuid.uuid4().hex
    stale_sid = uuid.uuid4().hex  # no longer in filming_guide
    guide = [_shot(shot_id=live_sid)]
    assignments = [
        {"gcs_path": f"users/{user.id}/plan/p/a.mp4", "shot_id": live_sid},
        {"gcs_path": f"users/{user.id}/plan/p/b.mp4", "shot_id": stale_sid},  # dangling
    ]
    clips = [a["gcs_path"] for a in assignments]
    item, plan = _owned_item(user.id, clips=clips, filming_guide=guide, assignments=assignments)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    resp_assignments = body["clip_assignments"]
    # The live one keeps its shot_id.
    live = next((a for a in resp_assignments if a["gcs_path"].endswith("a.mp4")), None)
    assert live is not None
    assert live["shot_id"] == live_sid
    # The stale one is demoted to pool (shot_id=null).
    stale = next((a for a in resp_assignments if a["gcs_path"].endswith("b.mp4")), None)
    assert stale is not None
    assert stale["shot_id"] is None


# ── FilmingShot.shot_id in response ──────────────────────────────────────────


def test_get_plan_item_returns_shot_id_in_filming_guide(client: TestClient) -> None:
    """GET /plan-items/{id} includes shot_id on each filming_guide entry."""
    user = _user()
    sid = uuid.uuid4().hex
    guide = [_shot(shot_id=sid)]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["filming_guide"][0]["shot_id"] == sid


# ── PATCH /{item_id}/shots/{shot_id} ─────────────────────────────────────────


def test_edit_shot_happy_path(client: TestClient) -> None:
    """PATCH /shots/{shot_id} mutates the matching shot and sets user_edited=True."""
    user = _user()
    sid = uuid.uuid4().hex
    guide = [{"shot_id": sid, "what": "original", "how": "eye level", "duration_s": 5}]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}/shots/{sid}",
        json={"what": "updated text", "duration_s": 10},
    )

    assert resp.status_code == 200
    # The item's guide was mutated in-place.
    assert item.filming_guide[0]["what"] == "updated text"
    assert item.filming_guide[0]["duration_s"] == 10
    assert item.user_edited is True
    db.commit.assert_awaited()


def test_edit_shot_unknown_shot_id_returns_422(client: TestClient) -> None:
    """PATCH /shots/{shot_id} returns 422 when shot_id is not in the guide."""
    user = _user()
    sid = uuid.uuid4().hex
    guide = [{"shot_id": "other-id", "what": "x", "duration_s": 5}]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/plan-items/{item.id}/shots/{sid}", json={"what": "new"})

    assert resp.status_code == 422


def test_edit_shot_wrong_user_returns_404(client: TestClient) -> None:
    """PATCH /shots/{shot_id} returns 404 when the plan belongs to someone else."""
    user = _user()
    other_user = _user()
    sid = uuid.uuid4().hex
    guide = [{"shot_id": sid, "what": "x", "duration_s": 5}]
    item, plan = _owned_item(other_user.id, filming_guide=guide)  # owned by other_user
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user  # logged in as user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/plan-items/{item.id}/shots/{sid}", json={"what": "x"})

    assert resp.status_code == 404


def test_edit_shot_clip_count_is_clamped(client: TestClient) -> None:
    """clip_count is clamped to [1, 10] — out-of-range values are silently adjusted."""
    user = _user()
    sid = uuid.uuid4().hex
    guide = [{"shot_id": sid, "what": "x", "duration_s": 5, "clip_count": 3}]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    # 0 → clamps to 1; 11 → clamps to 10.
    resp = client.patch(f"/plan-items/{item.id}/shots/{sid}", json={"clip_count": 0})
    assert resp.status_code == 200
    assert item.filming_guide[0]["clip_count"] == 1

    item.filming_guide[0]["clip_count"] = 3  # reset
    resp = client.patch(f"/plan-items/{item.id}/shots/{sid}", json={"clip_count": 11})
    assert resp.status_code == 200
    assert item.filming_guide[0]["clip_count"] == 10


# ── POST /{item_id}/generate-guide ───────────────────────────────────────────


def test_generate_guide_happy_path(client: TestClient) -> None:
    """POST /generate-guide calls run_shot_list_writer and persists the guide."""
    user = _user()
    item, plan = _owned_item(user.id, filming_guide=[])  # empty guide → AI generates
    item.theme = "morning run"
    item.idea = "5 AM training session"
    item.edit_format = "montage"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    mock_shot = MagicMock()
    mock_shot.model_dump.return_value = {
        "what": "lace up shoes", "how": "close-up", "duration_s": 4
    }
    mock_result = MagicMock()
    mock_result.shots = [mock_shot]

    with patch("app.agents.shot_list_writer.run_shot_list_writer", return_value=mock_result):
        resp = client.post(f"/plan-items/{item.id}/generate-guide")

    assert resp.status_code == 200
    # Guide was written with a minted shot_id on each shot.
    assert len(item.filming_guide) == 1
    assert "shot_id" in item.filming_guide[0]
    assert item.filming_guide[0]["what"] == "lace up shoes"
    assert item.user_edited is True
    db.commit.assert_awaited()


def test_generate_guide_returns_409_when_guide_exists(client: TestClient) -> None:
    """POST /generate-guide returns 409 when the item already has a filming guide."""
    user = _user()
    sid = uuid.uuid4().hex
    guide = [{"shot_id": sid, "what": "existing", "duration_s": 5}]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.post(f"/plan-items/{item.id}/generate-guide")

    assert resp.status_code == 409


def test_generate_guide_returns_500_when_agent_produces_no_shots(client: TestClient) -> None:
    """POST /generate-guide returns 500 when the LLM produces an empty shots list."""
    user = _user()
    item, plan = _owned_item(user.id, filming_guide=[])
    item.theme = "test"
    item.idea = "test"
    item.edit_format = "montage"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    mock_result = MagicMock()
    mock_result.shots = []  # agent returned nothing

    with patch("app.agents.shot_list_writer.run_shot_list_writer", return_value=mock_result):
        resp = client.post(f"/plan-items/{item.id}/generate-guide")

    assert resp.status_code == 500
