"""Unit tests for POST /admin/templates/from-music-track endpoint."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MusicTrack

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


def _make_track(
    track_id: str = "track-001",
    analysis_status: str = "ready",
    audio_gcs_path: str = "music/abc/audio.m4a",
    recipe_cached: dict | None = None,
) -> MagicMock:
    t = MagicMock(spec=MusicTrack)
    t.id = track_id
    t.title = "Test Song"
    t.artist = "Test Artist"
    t.analysis_status = analysis_status
    t.audio_gcs_path = audio_gcs_path
    t.beat_timestamps_s = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    t.track_config = {
        "best_start_s": 0.0,
        "best_end_s": 5.0,
        "slot_every_n_beats": 2,
        "required_clips_min": 1,
        "required_clips_max": 2,
    }
    t.duration_s = 120.0
    t.recipe_cached = recipe_cached
    return t


def _make_recipe() -> dict:
    return {
        "shot_count": 2,
        "total_duration_s": 5.0,
        "hook_duration_s": 2.5,
        "slots": [
            {"position": 1, "target_duration_s": 2.5, "slot_type": "hook",
             "transition_in": "whip-pan", "color_hint": "warm", "speed_factor": 1.0,
             "text_overlays": [], "energy": 7.0, "priority": 5},
            {"position": 2, "target_duration_s": 2.5, "slot_type": "broll",
             "transition_in": "dissolve", "color_hint": "cool", "speed_factor": 1.0,
             "text_overlays": [], "energy": 5.0, "priority": 5},
        ],
        "beat_timestamps_s": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
        "sync_style": "cut-on-beat",
        "pacing_style": "fast",
        "color_grade": "warm",
        "transition_style": "whip-pans",
        "copy_tone": "energetic",
        "caption_style": "",
        "creative_direction": "beat-driven",
        "interstitials": [],
        "required_clips_min": 1,
        "required_clips_max": 2,
    }


class TestCreateTemplateFromMusicTrack:
    def test_track_not_found_returns_404(self, client):
        """POST /from-music-track returns 404 when track doesn't exist."""
        mock_db = AsyncMock()
        mock_db.get.return_value = None

        from app.database import get_db
        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.post(
                "/admin/templates/from-music-track",
                json={"music_track_id": "nonexistent"},
                headers=admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404

    def test_track_not_ready_returns_409(self, client):
        """POST /from-music-track returns 409 when track is still analyzing."""
        mock_track = _make_track(analysis_status="analyzing")

        mock_db = AsyncMock()
        mock_db.get.return_value = mock_track

        from app.database import get_db
        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.post(
                "/admin/templates/from-music-track",
                json={"music_track_id": "track-001"},
                headers=admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409
        assert "not ready" in res.json()["detail"]

    def test_track_no_audio_returns_409(self, client):
        """POST /from-music-track returns 409 when track has no audio file."""
        mock_track = _make_track(audio_gcs_path="")

        mock_db = AsyncMock()
        mock_db.get.return_value = mock_track

        from app.database import get_db
        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.post(
                "/admin/templates/from-music-track",
                json={"music_track_id": "track-001"},
                headers=admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409
        assert "audio" in res.json()["detail"].lower()

    def test_happy_path_creates_audio_only_template(self, client):
        """POST /from-music-track creates an audio_only template."""
        mock_track = _make_track(recipe_cached=_make_recipe())

        mock_db = AsyncMock()
        mock_db.get.return_value = mock_track
        # Make add() a no-op (sync method on async session)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        from app.database import get_db
        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.post(
                "/admin/templates/from-music-track",
                json={"music_track_id": "track-001"},
                headers=admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["template_type"] == "audio_only"
        assert data["analysis_status"] == "ready"
        assert data["music_track_id"] == "track-001"
        assert data["name"] == "Test Song"
        # gcs_path should be empty for audio-only
        assert data["gcs_path"] in (None, "")

    def test_recipe_fallback_when_cached_is_none(self, client):
        """POST /from-music-track generates beat-only recipe when recipe_cached is None."""
        mock_track = _make_track(recipe_cached=None)

        mock_db = AsyncMock()
        mock_db.get.return_value = mock_track
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        from app.database import get_db
        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.post(
                "/admin/templates/from-music-track",
                json={"music_track_id": "track-001"},
                headers=admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        data = res.json()
        assert data["template_type"] == "audio_only"
        # Verify that db.add was called (template was created)
        assert mock_db.add.call_count == 2  # template + recipe version

    def test_custom_name_override(self, client):
        """POST /from-music-track uses custom name when provided."""
        mock_track = _make_track(recipe_cached=_make_recipe())

        mock_db = AsyncMock()
        mock_db.get.return_value = mock_track
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        from app.database import get_db
        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.post(
                "/admin/templates/from-music-track",
                json={"music_track_id": "track-001", "name": "Custom Name"},
                headers=admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        assert res.json()["name"] == "Custom Name"
