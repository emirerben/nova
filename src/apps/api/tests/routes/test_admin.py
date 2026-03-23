"""Unit tests for routes/admin.py — admin template endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"


def _override_db():
    """Yield a mock async DB session."""
    mock_db = AsyncMock()
    yield mock_db


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestAdminAuth:
    def test_missing_token_returns_422_or_401(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.post("/admin/templates", json={
                "name": "test", "gcs_path": "templates/test.mp4"
            })
        # Missing required header → 422 (header validation) or 401 (auth check)
        assert res.status_code in (401, 422)

    def test_wrong_token_returns_401(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.post(
                "/admin/templates",
                json={"name": "test", "gcs_path": "templates/test.mp4"},
                headers={"X-Admin-Token": "wrong-token"},
            )
        assert res.status_code == 401

    def test_no_admin_key_configured_returns_503(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = ""
            res = client.post(
                "/admin/templates",
                json={"name": "test", "gcs_path": "templates/test.mp4"},
                headers={"X-Admin-Token": "any-token"},
            )
        assert res.status_code == 503


class TestAdminTemplateValidation:
    def test_gcs_path_must_start_with_templates(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN
            res = client.post(
                "/admin/templates",
                json={"name": "test", "gcs_path": "uploads/test.mp4"},  # wrong prefix
                headers={"X-Admin-Token": VALID_TOKEN},
            )
        assert res.status_code == 422

    def test_gcs_object_not_found_returns_422(self, client):
        with patch("app.routes.admin.settings") as mock_settings, \
             patch("app.storage.object_exists", return_value=False):
            mock_settings.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = _override_db
            try:
                res = client.post(
                    "/admin/templates",
                    json={"name": "test", "gcs_path": "templates/missing.mp4"},
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 422


class TestGetTemplate:
    def test_not_found_returns_404(self, client):
        with patch("app.routes.admin.settings") as mock_settings:
            mock_settings.admin_api_key = VALID_TOKEN

            async def _mock_db_gen():
                mock_db = AsyncMock()
                mock_result = MagicMock()
                mock_result.scalar_one_or_none.return_value = None
                mock_db.execute = AsyncMock(return_value=mock_result)
                yield mock_db

            app.dependency_overrides[get_db] = _mock_db_gen
            try:
                res = client.get(
                    "/admin/templates/nonexistent-id",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404
