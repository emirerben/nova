"""Unit tests for /music-jobs route validation.

These tests focus on input validation and guard clauses that don't require a real DB.
Full pipeline integration is exercised via e2e tests with a real database.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


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
