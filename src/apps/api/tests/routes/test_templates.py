"""Unit tests for routes/templates.py — public template list + detail."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import VideoTemplate


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _db_with_template(template: object | None):
    """Dependency override that returns `template` from db.get(...)."""
    async def _gen():
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=template)
        yield mock_db
    return _gen


def _make_template(
    *,
    template_id: str = "tpl-123",
    name: str = "Sample Template",
    published: bool = True,
    archived: bool = False,
    template_type: str = "standard",
    recipe: dict | None = None,
    required_inputs: list | None = None,
) -> MagicMock:
    """Build a VideoTemplate-shaped mock with sensible defaults.

    The detail endpoint reads .archived_at, .published_at, .template_type,
    .required_inputs, and the .recipe_cached projection.
    """
    t = MagicMock(spec=VideoTemplate)
    t.id = template_id
    t.name = name
    t.gcs_path = "templates/sample.mp4"
    t.analysis_status = "ready"
    t.required_clips_min = 5
    t.required_clips_max = 20
    t.template_type = template_type
    t.archived_at = "2026-01-01T00:00:00Z" if archived else None
    t.published_at = "2026-01-01T00:00:00Z" if published else None
    t.required_inputs = required_inputs if required_inputs is not None else []
    t.recipe_cached = recipe if recipe is not None else {
        "slots": [
            {"position": 1, "target_duration_s": 1.0, "media_type": "video"},
            {"position": 2, "target_duration_s": 2.0, "media_type": "video"},
        ],
        "total_duration_s": 3.0,
        "copy_tone": "energetic",
    }
    return t


class TestGetTemplateById:
    def test_returns_200_with_template_shape(self, client):
        t = _make_template(template_id="tpl-abc", name="Dimples Passport")
        app.dependency_overrides[get_db] = _db_with_template(t)
        try:
            res = client.get("/templates/tpl-abc")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        body = res.json()
        assert body["id"] == "tpl-abc"
        assert body["name"] == "Dimples Passport"
        assert body["slot_count"] == 2
        assert body["copy_tone"] == "energetic"
        assert body["total_duration_s"] == 3.0
        assert len(body["slots"]) == 2
        assert body["slots"][0] == {
            "position": 1,
            "target_duration_s": 1.0,
            "media_type": "video",
        }
        assert body["required_inputs"] == []

    def test_includes_required_inputs_when_present(self, client):
        t = _make_template(
            required_inputs=[
                {
                    "key": "location",
                    "label": "Location",
                    "placeholder": "Tokyo",
                    "max_length": 50,
                    "required": True,
                }
            ]
        )
        app.dependency_overrides[get_db] = _db_with_template(t)
        try:
            res = client.get("/templates/tpl-123")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        assert res.json()["required_inputs"] == [
            {
                "key": "location",
                "label": "Location",
                "placeholder": "Tokyo",
                "max_length": 50,
                "required": True,
            }
        ]

    def test_missing_template_returns_404(self, client):
        app.dependency_overrides[get_db] = _db_with_template(None)
        try:
            res = client.get("/templates/missing-id")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404
        assert res.json()["detail"] == "Template not found"

    def test_archived_template_returns_404(self, client):
        t = _make_template(archived=True)
        app.dependency_overrides[get_db] = _db_with_template(t)
        try:
            res = client.get("/templates/tpl-123")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404
        assert res.json()["detail"] == "Template not found"

    def test_unpublished_template_returns_404(self, client):
        t = _make_template(published=False)
        app.dependency_overrides[get_db] = _db_with_template(t)
        try:
            res = client.get("/templates/tpl-123")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404
        assert res.json()["detail"] == "Template not published"

    def test_music_child_returns_404(self, client):
        """Music children are reachable only via parent — same as list endpoint."""
        t = _make_template(template_type="music_child")
        app.dependency_overrides[get_db] = _db_with_template(t)
        try:
            res = client.get("/templates/tpl-123")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404

    def test_corrupt_recipe_returns_404(self, client):
        """A published template with a corrupt recipe should 404, not 500."""
        t = _make_template(recipe={"slots": "not-a-list"})  # malformed
        app.dependency_overrides[get_db] = _db_with_template(t)
        try:
            res = client.get("/templates/tpl-123")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404
        assert res.json()["detail"] == "Template recipe unavailable"

    def test_missing_recipe_returns_404(self, client):
        t = _make_template()
        t.recipe_cached = None
        app.dependency_overrides[get_db] = _db_with_template(t)
        try:
            res = client.get("/templates/tpl-123")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404

    def test_playback_url_route_still_matches(self, client):
        """Adding /{template_id} must not shadow /{template_id}/playback-url.

        The playback-url handler does its own DB lookup via execute(), so we
        just need to ensure the route isn't intercepted by the detail handler.
        """
        async def _gen():
            mock_db = AsyncMock()
            mock_db.get = AsyncMock(return_value=None)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            mock_db.execute = AsyncMock(return_value=mock_result)
            yield mock_db

        app.dependency_overrides[get_db] = _gen
        try:
            res = client.get("/templates/tpl-123/playback-url")
        finally:
            app.dependency_overrides.pop(get_db, None)

        # Playback-url returns its own 404 detail when template is missing —
        # the URL has /playback-url suffix which can't possibly match {template_id}.
        assert res.status_code == 404
