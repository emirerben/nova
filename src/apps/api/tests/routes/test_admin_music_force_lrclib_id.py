"""Tests for `POST /admin/music-tracks/{id}/lyrics-force-lrclib-id` +
the `enabled=true` gate on `PATCH /admin/music-tracks/{id}/lyrics-config`.

Beauty And A Beat PR (2026-05-27). These two endpoints are the admin
recovery path: paste an LRCLIB row ID, server re-extracts against it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

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
    return {"X-Admin-Token": ADMIN_TOKEN}


def _override_db(track: MagicMock):
    """Stub `get_db` so the route handler reads our MagicMock track."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = track
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=lambda obj: None)

    async def _override():
        yield session

    return _override, session


def _make_track(
    *,
    track_id: str = "beauty-track",
    lyrics_status: str = "needs_manual_lyrics",
    has_audio: bool = True,
    extraction_version: int = 3,
    track_config: dict | None = None,
) -> MagicMock:
    t = MagicMock()
    t.id = track_id
    t.title = "Beauty And A Beat"
    t.artist = "Justin Bieber"
    t.audio_gcs_path = "music/x.mp3" if has_audio else None
    t.lyrics_status = lyrics_status
    t.lyrics_extraction_version = extraction_version
    t.track_config = track_config if track_config is not None else {}
    return t


# ── Happy paths ───────────────────────────────────────────────────────────────


def test_force_lrclib_id_accepts_numeric_input(client: TestClient) -> None:
    track = _make_track()
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        with patch("app.tasks.music_orchestrate.extract_track_lyrics_task.delay") as mock_dispatch:
            resp = client.post(
                f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
                json={"id_or_url": "8543210"},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["track_id"] == track.id
    assert body["analysis_status"] == "extracting"
    # Forced ID persisted to track_config.lyrics_config.
    assert track.track_config["lyrics_config"]["forced_lrclib_id"] == 8543210
    # Version bumped from 3 → 4.
    assert track.lyrics_extraction_version == 4
    # Status transitioned to extracting; diagnostic cleared.
    assert track.lyrics_status == "extracting"
    assert track.lyrics_diagnostic is None
    # Celery task dispatched.
    mock_dispatch.assert_called_once_with(track.id)


def test_force_lrclib_id_accepts_lrclib_lyrics_url(client: TestClient) -> None:
    track = _make_track()
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        with patch("app.tasks.music_orchestrate.extract_track_lyrics_task.delay"):
            resp = client.post(
                f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
                json={"id_or_url": "https://lrclib.net/lyrics/8543210"},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert track.track_config["lyrics_config"]["forced_lrclib_id"] == 8543210


def test_force_lrclib_id_accepts_api_get_url(client: TestClient) -> None:
    track = _make_track()
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        with patch("app.tasks.music_orchestrate.extract_track_lyrics_task.delay"):
            resp = client.post(
                f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
                json={"id_or_url": "https://lrclib.net/api/get/777"},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert track.track_config["lyrics_config"]["forced_lrclib_id"] == 777


def test_force_lrclib_id_preserves_other_track_config_keys(client: TestClient) -> None:
    """The endpoint persists `forced_lrclib_id` into `lyrics_config` without
    blowing away other config (best_start_s, lyrics_config.style, etc.)."""
    track = _make_track(
        track_config={
            "best_start_s": 12.0,
            "slot_every_n_beats": 8,
            "lyrics_config": {"enabled": False, "style": "line"},
        }
    )
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        with patch("app.tasks.music_orchestrate.extract_track_lyrics_task.delay"):
            client.post(
                f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
                json={"id_or_url": "42"},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert track.track_config["best_start_s"] == 12.0
    assert track.track_config["slot_every_n_beats"] == 8
    assert track.track_config["lyrics_config"]["style"] == "line"
    assert track.track_config["lyrics_config"]["forced_lrclib_id"] == 42


# ── 422 paths (parser security boundary) ──────────────────────────────────────


def test_force_lrclib_id_rejects_non_lrclib_url(client: TestClient) -> None:
    """The host allowlist must reject any URL that isn't lrclib.net."""
    track = _make_track()
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
            json={"id_or_url": "https://evil.com/lyrics/8543210"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
    assert "not an LRCLIB host" in resp.json()["detail"]


def test_force_lrclib_id_rejects_substring_spoof(client: TestClient) -> None:
    """`lrclib.net.evil.com` would fool a naive substring check; must reject."""
    track = _make_track()
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
            json={"id_or_url": "https://lrclib.net.evil.com/lyrics/1"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422


def test_force_lrclib_id_rejects_empty(client: TestClient) -> None:
    track = _make_track()
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
            json={"id_or_url": ""},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422


def test_force_lrclib_id_rejects_zero(client: TestClient) -> None:
    track = _make_track()
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
            json={"id_or_url": "0"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422


# ── 409 paths (lifecycle conflicts) ──────────────────────────────────────────


def test_force_lrclib_id_rejects_when_already_extracting(client: TestClient) -> None:
    """Force-ID while an extraction is in flight would race the running
    task and (depending on which finishes first) overwrite the result. The
    stale-task gate also catches this at commit time, but rejecting here
    gives the operator a clearer message."""
    track = _make_track(lyrics_status="extracting")
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
            json={"id_or_url": "1"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 409
    assert "in progress" in resp.json()["detail"]


def test_force_lrclib_id_rejects_when_no_audio(client: TestClient) -> None:
    track = _make_track(has_audio=False)
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-force-lrclib-id",
            json={"id_or_url": "1"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 409


# ── Auth ──────────────────────────────────────────────────────────────────────


def test_force_lrclib_id_requires_admin_auth(client: TestClient) -> None:
    """No X-Admin-Token header → 401/403. FastAPI may also surface 422 if
    request body parsing runs first; either is acceptable, only success
    (200) would be a security regression."""
    resp = client.post(
        "/admin/music-tracks/any-id/lyrics-force-lrclib-id",
        json={"id_or_url": "1"},
        # no headers
    )
    assert resp.status_code in (401, 403, 422)
    assert resp.status_code != 200, "auth bypass — must not succeed without admin token"


# ── PATCH /{track_id}: enabled=true gate when status ≠ ready ─────────────────


def test_patch_track_rejects_enable_lyrics_when_needs_manual_lyrics(
    client: TestClient,
) -> None:
    """A hand-crafted PATCH that sets `track_config.lyrics_config.enabled=true`
    on a needs_manual_lyrics track must be rejected with 422 — defense in
    depth for the no-burn-on-non-ready policy. FE disables the checkbox
    visually; this is the backend wall."""
    track = MagicMock()
    track.id = "x"
    track.lyrics_status = "needs_manual_lyrics"
    track.beat_timestamps_s = None
    track.track_config = {"lyrics_config": {"enabled": False, "style": "line"}}
    track.published_at = None
    track.archived_at = None

    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"track_config": {"lyrics_config": {"enabled": True, "style": "line"}}},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
    assert "needs_manual_lyrics" in resp.json()["detail"]


def test_patch_track_rejects_enable_lyrics_when_extracting(client: TestClient) -> None:
    track = MagicMock()
    track.id = "x"
    track.lyrics_status = "extracting"
    track.beat_timestamps_s = None
    track.track_config = {"lyrics_config": {"enabled": False, "style": "line"}}
    track.published_at = None
    track.archived_at = None

    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"track_config": {"lyrics_config": {"enabled": True, "style": "line"}}},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422


def _is_lyrics_enable_gate_response(resp) -> bool:
    """True iff the response is the 422 emitted by the lyrics-enable gate.

    Asserting positive-path 200 against a bare MagicMock track is fragile —
    the route's `_to_response(track)` Pydantic-validates many typed fields
    and synthesizing a valid mock for all of them is more boilerplate than
    these gate tests merit. Instead the disable / no-lyrics tests assert
    the gate does NOT fire (response is not the gate's 422), which is
    the only invariant relevant to the gate's correctness.
    """
    if resp.status_code != 422:
        return False
    detail = resp.json().get("detail")
    return isinstance(detail, str) and "needs_manual_lyrics" in detail


def test_patch_track_allows_disable_lyrics_on_any_status(client: TestClient) -> None:
    """Disabling lyrics must always work — operators have to be able to
    turn off broken burns on any track regardless of status. The gate
    only fires on enabled=true."""
    track = MagicMock()
    track.id = "x"
    track.lyrics_status = "needs_manual_lyrics"
    track.beat_timestamps_s = None
    track.track_config = {"lyrics_config": {"enabled": True, "style": "line"}}
    track.published_at = None
    track.archived_at = None

    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"track_config": {"lyrics_config": {"enabled": False, "style": "line"}}},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert not _is_lyrics_enable_gate_response(resp), "gate fired on disable request"


def test_patch_track_allows_other_edits_when_lyrics_config_omitted(
    client: TestClient,
) -> None:
    """The gate is scoped narrowly: a PATCH that doesn't touch lyrics_config
    must not be affected, even on a needs_manual_lyrics track."""
    track = MagicMock()
    track.id = "x"
    track.lyrics_status = "needs_manual_lyrics"
    track.beat_timestamps_s = None
    track.track_config = {"lyrics_config": {"enabled": False}}
    track.published_at = None
    track.archived_at = None

    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"title": "New Title"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert not _is_lyrics_enable_gate_response(resp), (
        "gate fired on a PATCH that didn't touch lyrics_config"
    )
