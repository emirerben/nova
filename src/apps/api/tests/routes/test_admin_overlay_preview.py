"""Tests for POST /admin/overlay-preview — WYSIWYG editor preview endpoint."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes.admin import _substitute_subject

VALID_TOKEN = "test-admin-token"


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _patch_settings():
    """Helper: settings patch that lets the admin token check pass."""
    return patch("app.routes.admin.settings", admin_api_key=VALID_TOKEN)


def _basic_overlay(text: str = "Hello", start_s: float = 0.0, end_s: float = 4.0) -> dict:
    return {
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "position": "center",
        "effect": "none",
    }


class TestAuth:
    def test_missing_token_rejected(self, client):
        with _patch_settings():
            res = client.post("/admin/overlay-preview", json={
                "overlays": [_basic_overlay()],
                "slot_duration_s": 5.0,
                "time_in_slot_s": 1.0,
            })
        assert res.status_code in (401, 422)

    def test_wrong_token_returns_401(self, client):
        with _patch_settings():
            res = client.post(
                "/admin/overlay-preview",
                json={
                    "overlays": [_basic_overlay()],
                    "slot_duration_s": 5.0,
                    "time_in_slot_s": 1.0,
                },
                headers={"X-Admin-Token": "wrong"},
            )
        assert res.status_code == 401


class TestPayloadValidation:
    def test_too_many_overlays_returns_422(self, client):
        many = [_basic_overlay(text=f"o{i}") for i in range(25)]
        with _patch_settings():
            res = client.post(
                "/admin/overlay-preview",
                json={
                    "overlays": many,
                    "slot_duration_s": 5.0,
                    "time_in_slot_s": 1.0,
                },
                headers={"X-Admin-Token": VALID_TOKEN},
            )
        assert res.status_code == 422

    def test_negative_slot_duration_rejected(self, client):
        with _patch_settings():
            res = client.post(
                "/admin/overlay-preview",
                json={
                    "overlays": [_basic_overlay()],
                    "slot_duration_s": -1.0,
                    "time_in_slot_s": 1.0,
                },
                headers={"X-Admin-Token": VALID_TOKEN},
            )
        assert res.status_code == 422

    def test_huge_time_rejected(self, client):
        with _patch_settings():
            res = client.post(
                "/admin/overlay-preview",
                json={
                    "overlays": [_basic_overlay()],
                    "slot_duration_s": 5.0,
                    "time_in_slot_s": 999.0,
                },
                headers={"X-Admin-Token": VALID_TOKEN},
            )
        assert res.status_code == 422


class TestHappyPath:
    def test_returns_png(self, client):
        with _patch_settings():
            res = client.post(
                "/admin/overlay-preview",
                json={
                    "overlays": [_basic_overlay()],
                    "slot_duration_s": 5.0,
                    "time_in_slot_s": 1.0,
                },
                headers={"X-Admin-Token": VALID_TOKEN},
            )
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        # PNG magic bytes
        assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
        # Cache-Control set
        assert "max-age=60" in res.headers.get("cache-control", "")

    def test_empty_overlays_returns_transparent_png(self, client):
        with _patch_settings():
            res = client.post(
                "/admin/overlay-preview",
                json={
                    "overlays": [],
                    "slot_duration_s": 5.0,
                    "time_in_slot_s": 1.0,
                },
                headers={"X-Admin-Token": VALID_TOKEN},
            )
        assert res.status_code == 200
        assert res.content[:8] == b"\x89PNG\r\n\x1a\n"


class TestSubjectSubstitution:
    def test_substitutes_top_level_text(self):
        out = _substitute_subject(
            [{"text": "Welcome to {{subject}}", "spans": None}],
            "PERU",
        )
        assert out[0]["text"] == "Welcome to PERU"

    def test_substitutes_per_span(self):
        out = _substitute_subject(
            [{
                "text": "static",
                "spans": [
                    {"text": "Hello "},
                    {"text": "{{subject}}!"},
                ],
            }],
            "WORLD",
        )
        assert out[0]["spans"][1]["text"] == "WORLD!"

    def test_no_subject_leaves_overlay_untouched(self):
        original = [{"text": "Welcome to {{subject}}"}]
        out = _substitute_subject(original, None)
        assert out[0]["text"] == "Welcome to {{subject}}"

    def test_does_not_mutate_input(self):
        original = [{"text": "Hi {{subject}}"}]
        _substitute_subject(original, "EARTH")
        assert original[0]["text"] == "Hi {{subject}}"
