"""Unit tests for PATCH /admin/templates/{id}/lyrics-config.

Mock strategy: `get_template_or_404` uses `db.execute(select(...))` so we
mock `db.execute` to return a result whose `.scalar_one_or_none()` returns
the template (or None). `db.get(MusicTrack, ...)` for the linked-track
lookup is mocked separately.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MusicTrack, VideoTemplate

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _patch_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_TOKEN)
    from app.config import settings

    settings.admin_api_key = ADMIN_TOKEN


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _admin_headers() -> dict:
    return {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


def _make_template(
    *,
    music_track_id: str | None = "track-001",
    lyrics_config: dict | None = None,
) -> MagicMock:
    t = MagicMock(spec=VideoTemplate)
    t.id = "tmpl-001"
    t.name = "Test Template"
    t.gcs_path = None
    t.analysis_status = "ready"
    t.required_clips_min = 1
    t.required_clips_max = 2
    t.required_inputs = []
    t.published_at = None
    t.archived_at = None
    t.description = None
    t.source_url = None
    t.thumbnail_gcs_path = None
    t.error_detail = None
    t.template_type = "audio_only"
    t.parent_template_id = None
    t.music_track_id = music_track_id
    t.is_agentic = False
    t.use_layer2_default = None
    t.recipe_cached = None
    t.recipe_cached_versions = {}
    t.lyrics_config = lyrics_config
    t.created_at = datetime(2026, 5, 20, tzinfo=UTC)
    return t


def _make_track(lyrics_cfg: dict | None) -> MagicMock:
    track = MagicMock(spec=MusicTrack)
    track.id = "track-001"
    track.track_config = {"lyrics_config": lyrics_cfg} if lyrics_cfg is not None else {}
    return track


def _setup_db(template: MagicMock | None, track: MagicMock | None = None) -> AsyncMock:
    """Mock db.execute → template lookup, db.get → track lookup."""
    mock_db = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = template
    mock_db.execute = AsyncMock(return_value=exec_result)
    mock_db.get = AsyncMock(return_value=track)
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    return mock_db


class TestSetTemplateLyricsConfig:
    def test_set_override_persists_dict(self, client):
        """PATCH with a valid dict stores it on the template."""
        template = _make_template()
        track = _make_track(lyrics_cfg={"enabled": True, "style": "karaoke"})
        mock_db = _setup_db(template, track)

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.patch(
                "/admin/templates/tmpl-001/lyrics-config",
                json={
                    "lyrics_config": {
                        "enabled": True,
                        "style": "per-word-pop",
                        "position": "center",
                        "text_color": "#FF00FF",
                    }
                },
                headers=_admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200, res.json()
        assert template.lyrics_config == {
            "enabled": True,
            "style": "per-word-pop",
            "position": "center",
            "text_color": "#FF00FF",
        }
        body = res.json()
        assert body["lyrics_config"]["style"] == "per-word-pop"
        assert body["linked_track_lyrics_config"]["style"] == "karaoke"

    def test_clear_override_with_null(self, client):
        """PATCH with lyrics_config=null clears the override → back to inherit."""
        template = _make_template(lyrics_config={"enabled": True, "style": "karaoke"})
        track = _make_track(lyrics_cfg={"enabled": False, "style": "karaoke"})
        mock_db = _setup_db(template, track)

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.patch(
                "/admin/templates/tmpl-001/lyrics-config",
                json={"lyrics_config": None},
                headers=_admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200, res.json()
        assert template.lyrics_config is None
        body = res.json()
        assert body["lyrics_config"] is None
        assert body["linked_track_lyrics_config"]["enabled"] is False

    def test_empty_dict_is_persisted_not_dropped(self, client):
        """The empty dict {} must round-trip — it's a legit "lyrics off"
        state distinct from NULL (which means inherit).
        """
        template = _make_template()
        track = _make_track(lyrics_cfg={"enabled": True, "style": "karaoke"})
        mock_db = _setup_db(template, track)

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.patch(
                "/admin/templates/tmpl-001/lyrics-config",
                json={"lyrics_config": {}},
                headers=_admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200, res.json()
        # CRITICAL: {} is persisted as-is. If this becomes None/null we've
        # regressed the "explicit off" semantic the orchestrator depends on.
        assert template.lyrics_config == {}
        assert res.json()["lyrics_config"] == {}

    def test_invalid_style_rejected_422(self, client):
        template = _make_template()
        mock_db = _setup_db(template)

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.patch(
                "/admin/templates/tmpl-001/lyrics-config",
                json={"lyrics_config": {"enabled": True, "style": "bouncing-pickles"}},
                headers=_admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 422

    def test_invalid_hex_color_rejected_422(self, client):
        template = _make_template()
        mock_db = _setup_db(template)

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.patch(
                "/admin/templates/tmpl-001/lyrics-config",
                json={"lyrics_config": {"enabled": True, "text_color": "not-a-color"}},
                headers=_admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 422

    def test_template_without_music_track_409(self, client):
        """Lyrics config on a non-music template makes no sense."""
        template = _make_template(music_track_id=None)
        mock_db = _setup_db(template)

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.patch(
                "/admin/templates/tmpl-001/lyrics-config",
                json={"lyrics_config": {"enabled": True}},
                headers=_admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 409
        assert "music track" in res.json()["detail"].lower()

    def test_404_when_template_missing(self, client):
        mock_db = _setup_db(template=None)

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: mock_db
        try:
            res = client.patch(
                "/admin/templates/missing/lyrics-config",
                json={"lyrics_config": None},
                headers=_admin_headers(),
            )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 404
