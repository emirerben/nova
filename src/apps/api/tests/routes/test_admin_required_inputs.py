"""Integration tests for the typed required_inputs editor on the admin PATCH endpoint.

Covers:
  PATCH /admin/templates/{id}  body={"required_inputs": [...]}

The validation paths (422 cases) run end-to-end against the real Pydantic
validator — no DB commit reached. The accept path is a sanity check that the
validator passes the body through.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import VideoTemplate

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _patch_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_TOKEN)
    from app.config import settings

    settings.admin_api_key = ADMIN_TOKEN


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _headers() -> dict:
    return {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


def _make_template(template_id: str = "tpl-001") -> VideoTemplate:
    t = MagicMock(spec=VideoTemplate)
    t.id = template_id
    t.name = "Test"
    t.gcs_path = "templates/test/video.mp4"
    t.template_type = "standard"
    t.parent_template_id = None
    t.music_track_id = None
    t.analysis_status = "ready"
    t.recipe_cached = {}
    t.required_clips_min = 3
    t.required_clips_max = 6
    t.required_inputs = []
    t.published_at = None
    t.archived_at = None
    t.description = None
    t.source_url = None
    t.thumbnail_gcs_path = None
    t.error_detail = None
    t.is_agentic = False
    t.use_layer2_default = None
    t.created_at = datetime.now(UTC)
    return t


def test_patch_with_valid_required_inputs_passes_validation(client: TestClient) -> None:
    """A well-formed list of required_inputs entries doesn't trip the validator.
    The DB commit may fail (no real session) but that's not a 422."""
    with patch(
        "app.routes.admin.get_template_or_404", new=AsyncMock(return_value=_make_template())
    ):
        res = client.patch(
            "/admin/templates/tpl-001",
            headers=_headers(),
            json={
                "required_inputs": [
                    {
                        "key": "location",
                        "label": "Location",
                        "placeholder": "Brazil",
                        "max_length": 40,
                        "required": True,
                    },
                    {
                        "key": "subject",
                        "label": "Subject",
                        "placeholder": "",
                        "max_length": 50,
                        "required": False,
                    },
                ]
            },
        )
    assert res.status_code != 422, res.json()


def test_patch_rejects_duplicate_keys(client: TestClient) -> None:
    """Two entries with the same key → 422 from the field_validator."""
    res = client.patch(
        "/admin/templates/tpl-001",
        headers=_headers(),
        json={
            "required_inputs": [
                {"key": "location", "label": "Location 1"},
                {"key": "location", "label": "Location 2"},
            ]
        },
    )
    assert res.status_code == 422
    assert "duplicate" in str(res.json()).lower()


def test_patch_rejects_empty_key(client: TestClient) -> None:
    """Empty `key` field → 422."""
    res = client.patch(
        "/admin/templates/tpl-001",
        headers=_headers(),
        json={
            "required_inputs": [
                {"key": "  ", "label": "Some Label"},
            ]
        },
    )
    assert res.status_code == 422
    assert "key" in str(res.json()).lower()


def test_patch_rejects_empty_label(client: TestClient) -> None:
    """Empty `label` field → 422."""
    res = client.patch(
        "/admin/templates/tpl-001",
        headers=_headers(),
        json={
            "required_inputs": [
                {"key": "location", "label": ""},
            ]
        },
    )
    assert res.status_code == 422
    assert "label" in str(res.json()).lower()


def test_patch_with_empty_list_passes_validation(client: TestClient) -> None:
    """Empty `required_inputs: []` is valid — admin wants to clear the list."""
    with patch(
        "app.routes.admin.get_template_or_404", new=AsyncMock(return_value=_make_template())
    ):
        res = client.patch(
            "/admin/templates/tpl-001",
            headers=_headers(),
            json={"required_inputs": []},
        )
    assert res.status_code != 422, res.json()


def test_patch_omitting_required_inputs_leaves_field_alone(client: TestClient) -> None:
    """Not sending `required_inputs` shouldn't trigger the validator at all."""
    with patch(
        "app.routes.admin.get_template_or_404", new=AsyncMock(return_value=_make_template())
    ):
        res = client.patch(
            "/admin/templates/tpl-001",
            headers=_headers(),
            json={"name": "Renamed"},
        )
    assert res.status_code != 422
