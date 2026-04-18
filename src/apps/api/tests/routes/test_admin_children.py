"""Integration tests for admin music-variant (children) endpoints.

Tests:
  POST   /admin/templates/{id}/children
  GET    /admin/templates/{id}/children
  POST   /admin/templates/{id}/remerge-children
  PATCH  /admin/templates/{id}  (template_type toggle)
  GET    /admin/templates       (exclude_children filter)

Uses FastAPI's TestClient with dependency overrides for DB.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import MusicTrack, TemplateRecipeVersion, VideoTemplate

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _patch_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_TOKEN)
    from app.config import settings
    settings.admin_api_key = ADMIN_TOKEN


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def admin_headers() -> dict:
    return {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


# ── Fixtures for mock data ─────────────────���─────────────────────────────────


def _make_parent(template_id: str = "parent-001") -> VideoTemplate:
    t = MagicMock(spec=VideoTemplate)
    t.id = template_id
    t.name = "Test Parent"
    t.gcs_path = "templates/test/video.mp4"
    t.template_type = "music_parent"
    t.parent_template_id = None
    t.music_track_id = None
    t.analysis_status = "ready"
    t.recipe_cached = {
        "shot_count": 4,
        "total_duration_s": 12.0,
        "slots": [
            {"position": i + 1, "target_duration_s": 3.0, "slot_type": "broll",
             "transition_in": "cut", "text_overlays": [], "speed_factor": 1.0,
             "color_hint": "warm", "energy": 5.0}
            for i in range(4)
        ],
        "copy_tone": "cinematic",
        "caption_style": "bold",
        "creative_direction": "test",
        "color_grade": "warm",
        "transition_style": "cut",
        "interstitials": [],
        "beat_timestamps_s": [],
        "sync_style": "freeform",
        "pacing_style": "moderate",
    }
    t.recipe_cached_at = datetime.now(UTC)
    t.required_clips_min = 3
    t.required_clips_max = 6
    t.published_at = datetime.now(UTC)
    t.archived_at = None
    t.description = None
    t.source_url = None
    t.thumbnail_gcs_path = None
    t.error_detail = None
    t.audio_gcs_path = None
    t.created_at = datetime.now(UTC)
    return t


def _make_track(track_id: str = "track-001") -> MusicTrack:
    t = MagicMock(spec=MusicTrack)
    t.id = track_id
    t.title = "Test Beat"
    t.artist = "DJ Test"
    t.analysis_status = "ready"
    t.beat_timestamps_s = [float(i) for i in range(40)]
    t.track_config = {"best_start_s": 0.0, "best_end_s": 39.0, "slot_every_n_beats": 4}
    t.duration_s = 60.0
    t.audio_gcs_path = "music/test/audio.m4a"
    t.published_at = datetime.now(UTC)
    return t


# ── POST /admin/templates/{id}/children ─────��────────────────────────────────


@patch("app.routes.admin.get_template_or_404")
def test_create_child_parent_not_music_parent(mock_get: AsyncMock, client: TestClient) -> None:
    """POST /children on a standard template → 422."""
    parent = _make_parent()
    parent.template_type = "standard"
    mock_get.return_value = parent

    res = client.post(
        "/admin/templates/parent-001/children",
        headers=admin_headers(),
        json={"music_track_id": "track-001"},
    )
    assert res.status_code == 422
    assert "music_parent" in res.json()["detail"]


@patch("app.routes.admin.get_template_or_404")
def test_create_child_parent_no_recipe(mock_get: AsyncMock, client: TestClient) -> None:
    """POST /children on a parent with no recipe → 409."""
    parent = _make_parent()
    parent.recipe_cached = None
    mock_get.return_value = parent

    res = client.post(
        "/admin/templates/parent-001/children",
        headers=admin_headers(),
        json={"music_track_id": "track-001"},
    )
    assert res.status_code == 409
    assert "recipe" in res.json()["detail"].lower()


# ── GET /admin/templates (exclude_children) ──────────────────────────────────


def test_list_templates_exclude_children_param(client: TestClient) -> None:
    """GET /admin/templates accepts exclude_children query param without error."""
    # This will fail at DB level (no real DB), but validates that the endpoint
    # parses the query param. We check it doesn't return 422 (validation error).
    res = client.get(
        "/admin/templates?exclude_children=true&limit=1",
        headers=admin_headers(),
    )
    # 500 expected (no DB), but NOT 422 (param accepted)
    assert res.status_code != 422


def test_list_templates_exclude_children_false(client: TestClient) -> None:
    """GET /admin/templates?exclude_children=false is accepted."""
    res = client.get(
        "/admin/templates?exclude_children=false&limit=1",
        headers=admin_headers(),
    )
    assert res.status_code != 422


# ── PATCH /admin/templates/{id} (template_type toggle) ──────────────────────


@patch("app.routes.admin.get_template_or_404")
def test_update_template_type_to_music_parent(mock_get: AsyncMock, client: TestClient) -> None:
    """PATCH with template_type=music_parent on a standard template is accepted."""
    parent = _make_parent()
    parent.template_type = "standard"
    mock_get.return_value = parent

    res = client.patch(
        "/admin/templates/parent-001",
        headers=admin_headers(),
        json={"template_type": "music_parent"},
    )
    # Will fail at commit (no DB) but should not be a validation error
    assert res.status_code != 422 or "template_type" not in str(res.json().get("detail", ""))


@patch("app.routes.admin.get_template_or_404")
def test_update_template_type_child_blocked(mock_get: AsyncMock, client: TestClient) -> None:
    """PATCH template_type on a music_child → 422."""
    child = _make_parent()
    child.template_type = "music_child"
    child.parent_template_id = "parent-001"
    mock_get.return_value = child

    res = client.patch(
        "/admin/templates/child-001",
        headers=admin_headers(),
        json={"template_type": "standard"},
    )
    assert res.status_code == 422
    assert "music_child" in res.json()["detail"]


def test_update_template_type_invalid_value(client: TestClient) -> None:
    """PATCH with template_type=invalid → 422 (Pydantic validation)."""
    res = client.patch(
        "/admin/templates/any-id",
        headers=admin_headers(),
        json={"template_type": "invalid"},
    )
    assert res.status_code == 422


# ── POST /admin/templates/{id}/remerge-children ──────────���──────────────────


@patch("app.routes.admin.get_template_or_404")
def test_remerge_not_music_parent(mock_get: AsyncMock, client: TestClient) -> None:
    """POST /remerge-children on standard template → 422."""
    parent = _make_parent()
    parent.template_type = "standard"
    mock_get.return_value = parent

    res = client.post(
        "/admin/templates/parent-001/remerge-children",
        headers=admin_headers(),
    )
    assert res.status_code == 422
    assert "music_parent" in res.json()["detail"]


@patch("app.routes.admin.get_template_or_404")
def test_remerge_no_recipe(mock_get: AsyncMock, client: TestClient) -> None:
    """POST /remerge-children with no recipe → 409."""
    parent = _make_parent()
    parent.recipe_cached = None
    mock_get.return_value = parent

    res = client.post(
        "/admin/templates/parent-001/remerge-children",
        headers=admin_headers(),
    )
    assert res.status_code == 409
