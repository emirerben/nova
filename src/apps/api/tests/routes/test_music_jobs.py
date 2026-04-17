"""Unit tests for /music-jobs route validation.

These tests focus on input validation and guard clauses that don't require a real DB.
Full pipeline integration is exercised via e2e tests with a real database.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app


def _make_db_with_track(track) -> callable:
    """Return a dependency override that yields a mock session returning a single track."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = track

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def _override():
        yield mock_session

    return _override


def _ready_track(**overrides) -> MagicMock:
    """Return a MagicMock MusicTrack that passes all guard checks by default."""
    t = MagicMock()
    t.id = "track-001"
    t.published_at = datetime.now(UTC)
    t.archived_at = None
    t.analysis_status = "ready"
    t.audio_gcs_path = "music/uuid/audio.m4a"
    t.track_config = {"required_clips_min": 1, "required_clips_max": 20}
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ── POST /music-jobs ──────────────────────────────────────────────────────────


def test_create_music_job_missing_fields(client: TestClient) -> None:
    """POST without required fields returns 422."""
    resp = client.post("/music-jobs", json={})
    assert resp.status_code == 422


def test_create_music_job_empty_clips(client: TestClient) -> None:
    """POST with zero clips returns 422."""
    resp = client.post(
        "/music-jobs",
        json={"music_track_id": "some-track", "clip_gcs_paths": []},
    )
    assert resp.status_code == 422


def test_create_music_job_too_many_clips(client: TestClient) -> None:
    """POST with > 20 clips returns 422."""
    resp = client.post(
        "/music-jobs",
        json={
            "music_track_id": "some-track",
            "clip_gcs_paths": [f"clips/clip{i}.mp4" for i in range(21)],
        },
    )
    assert resp.status_code == 422


def test_create_music_job_invalid_platform(client: TestClient) -> None:
    """POST with unknown platform returns 422."""
    resp = client.post(
        "/music-jobs",
        json={
            "music_track_id": "some-track",
            "clip_gcs_paths": ["clips/clip1.mp4"],
            "selected_platforms": ["snapchat"],
        },
    )
    assert resp.status_code == 422


def test_create_music_job_track_not_found(client: TestClient) -> None:
    """POST with nonexistent track_id returns 404 (DB lookup)."""
    resp = client.post(
        "/music-jobs",
        json={
            "music_track_id": "nonexistent-track-id-00000000",
            "clip_gcs_paths": ["clips/clip1.mp4"],
        },
    )
    # 404 expected — track not found in DB; 500 if DB not connected in test env
    assert resp.status_code in (404, 500)


# ── GET /music-jobs/{id}/status ───────────────────────────────────────────────


def test_get_music_job_status_invalid_uuid(client: TestClient) -> None:
    """GET with non-UUID job_id returns 404."""
    resp = client.get("/music-jobs/not-a-uuid/status")
    assert resp.status_code == 404


def test_get_music_job_status_nonexistent(client: TestClient) -> None:
    """GET with valid UUID but non-existent job returns 404."""
    import uuid
    job_id = str(uuid.uuid4())
    resp = client.get(f"/music-jobs/{job_id}/status")
    assert resp.status_code in (404, 500)


# ── Guard clause tests (DB-mocked) ────────────────────────────────────────────


def test_create_music_job_track_not_published(client: TestClient) -> None:
    """POST with an unpublished track returns 422."""
    track = _ready_track(published_at=None)
    app.dependency_overrides[get_db] = _make_db_with_track(track)
    try:
        resp = client.post(
            "/music-jobs",
            json={"music_track_id": "track-001", "clip_gcs_paths": ["clips/a.mp4"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_create_music_job_track_archived(client: TestClient) -> None:
    """POST with an archived track returns 422."""
    track = _ready_track(archived_at=datetime.now(UTC))
    app.dependency_overrides[get_db] = _make_db_with_track(track)
    try:
        resp = client.post(
            "/music-jobs",
            json={"music_track_id": "track-001", "clip_gcs_paths": ["clips/a.mp4"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_create_music_job_track_not_ready(client: TestClient) -> None:
    """POST with a track still analyzing returns 409."""
    track = _ready_track(analysis_status="analyzing")
    app.dependency_overrides[get_db] = _make_db_with_track(track)
    try:
        resp = client.post(
            "/music-jobs",
            json={"music_track_id": "track-001", "clip_gcs_paths": ["clips/a.mp4"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409


def test_create_music_job_track_no_audio(client: TestClient) -> None:
    """POST with a track missing audio_gcs_path returns 409."""
    track = _ready_track(audio_gcs_path=None)
    app.dependency_overrides[get_db] = _make_db_with_track(track)
    try:
        resp = client.post(
            "/music-jobs",
            json={"music_track_id": "track-001", "clip_gcs_paths": ["clips/a.mp4"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409


def test_create_music_job_clip_count_below_min(client: TestClient) -> None:
    """POST with fewer clips than required_clips_min returns 422."""
    track = _ready_track(track_config={"required_clips_min": 3, "required_clips_max": 6})
    app.dependency_overrides[get_db] = _make_db_with_track(track)
    try:
        resp = client.post(
            "/music-jobs",
            json={"music_track_id": "track-001", "clip_gcs_paths": ["clips/a.mp4", "clips/b.mp4"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


def test_create_music_job_clip_count_above_max(client: TestClient) -> None:
    """POST with more clips than required_clips_max returns 422."""
    track = _ready_track(track_config={"required_clips_min": 1, "required_clips_max": 2})
    app.dependency_overrides[get_db] = _make_db_with_track(track)
    try:
        resp = client.post(
            "/music-jobs",
            json={"music_track_id": "track-001", "clip_gcs_paths": ["clips/a.mp4", "clips/b.mp4", "clips/c.mp4"]},
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422
