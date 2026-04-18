"""Unit tests for GET /music-tracks (public gallery endpoint).

Uses FastAPI dependency overrides to avoid requiring a live Postgres connection.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _make_track(
    track_id: str = "track-001",
    title: str = "Test Song",
    artist: str = "Test Artist",
    thumbnail_url: str | None = "https://img.youtube.com/vi/abc/default.jpg",
    track_config: dict | None = None,
    published_at: datetime | None = None,
    archived_at: datetime | None = None,
    analysis_status: str = "ready",
) -> MagicMock:
    """Return a MagicMock that looks like a MusicTrack ORM row."""
    t = MagicMock()
    t.id = track_id
    t.title = title
    t.artist = artist
    t.thumbnail_url = thumbnail_url
    t.track_config = track_config if track_config is not None else {
        "best_start_s": 10.0,
        "best_end_s": 55.0,
        "required_clips_min": 3,
        "required_clips_max": 6,
    }
    t.published_at = published_at or datetime.now(UTC)
    t.archived_at = archived_at
    t.analysis_status = analysis_status
    return t


def _override_get_db(tracks: list):
    """Return an async generator override for get_db that yields a mock session."""
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = tracks
    mock_result.scalars.return_value = mock_scalars

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def _get_db_override():
        yield mock_session

    return _get_db_override


# ── GET /music-tracks ─────────────────────────────────────────────────────────


def test_list_music_tracks_empty(client: TestClient) -> None:
    """Returns an empty list when no tracks are ready."""
    app.dependency_overrides[get_db] = _override_get_db([])
    try:
        resp = client.get("/music-tracks")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data == {"tracks": []}


def test_list_music_tracks_returns_published_tracks(client: TestClient) -> None:
    """Returns tracks with correct field mapping from DB row."""
    track = _make_track(
        track_id="t-abc",
        title="Beat Drop",
        artist="DJ Nova",
        thumbnail_url="https://img.youtube.com/vi/abc/default.jpg",
        track_config={
            "best_start_s": 20.0,
            "best_end_s": 50.0,
            "required_clips_min": 2,
            "required_clips_max": 5,
        },
    )
    app.dependency_overrides[get_db] = _override_get_db([track])
    try:
        resp = client.get("/music-tracks")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tracks"]) == 1
    t = data["tracks"][0]
    assert t["id"] == "t-abc"
    assert t["title"] == "Beat Drop"
    assert t["artist"] == "DJ Nova"
    assert t["thumbnail_url"] == "https://img.youtube.com/vi/abc/default.jpg"
    assert t["section_duration_s"] == 30.0  # 50 - 20
    assert t["required_clips_min"] == 2
    assert t["required_clips_max"] == 5


def test_list_music_tracks_section_duration_calculation(client: TestClient) -> None:
    """section_duration_s = round(end_s - start_s, 1) and is never negative."""
    track = _make_track(
        track_config={
            "best_start_s": 15.3,
            "best_end_s": 45.8,
            "required_clips_min": 1,
            "required_clips_max": 4,
        }
    )
    app.dependency_overrides[get_db] = _override_get_db([track])
    try:
        resp = client.get("/music-tracks")
    finally:
        app.dependency_overrides.clear()

    section = resp.json()["tracks"][0]["section_duration_s"]
    assert section == round(45.8 - 15.3, 1)
    assert section > 0


def test_list_music_tracks_missing_config_defaults(client: TestClient) -> None:
    """Tracks without track_config fields fall back to sensible defaults."""
    track = _make_track(track_config={})
    app.dependency_overrides[get_db] = _override_get_db([track])
    try:
        resp = client.get("/music-tracks")
    finally:
        app.dependency_overrides.clear()

    t = resp.json()["tracks"][0]
    assert t["section_duration_s"] == 0.0   # 0 - 0 = 0, clamped to 0
    assert t["required_clips_min"] == 1     # default
    assert t["required_clips_max"] == 10    # default


def test_list_music_tracks_multiple_tracks(client: TestClient) -> None:
    """Returns all tracks in the list."""
    tracks = [_make_track(track_id=f"t-{i}", title=f"Song {i}") for i in range(3)]
    app.dependency_overrides[get_db] = _override_get_db(tracks)
    try:
        resp = client.get("/music-tracks")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert len(resp.json()["tracks"]) == 3


def test_list_music_tracks_null_thumbnail(client: TestClient) -> None:
    """Tracks without a thumbnail return thumbnail_url=null, not an error."""
    track = _make_track(thumbnail_url=None)
    app.dependency_overrides[get_db] = _override_get_db([track])
    try:
        resp = client.get("/music-tracks")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["tracks"][0]["thumbnail_url"] is None
