from unittest.mock import AsyncMock, MagicMock

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
    result = MagicMock()
    result.scalar_one_or_none.return_value = track
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=lambda obj: None)

    async def _override():
        yield session

    return _override, session


def test_patch_lyrics_config_deep_merges_and_preserves_track_config(client: TestClient) -> None:
    track = MagicMock()
    track.id = "track-lyrics-config"
    track.track_config = {
        "best_start_s": 12.0,
        "slot_every_n_beats": 8,
        "lyrics_config": {"enabled": True, "style": "line", "post_dwell_s": 1.0},
    }
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}/lyrics-config",
            json={"fade_in_ms": 75},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["lyrics_config"]["post_dwell_s"] == 1.0
    assert resp.json()["lyrics_config"]["fade_in_ms"] == 75
    assert track.track_config["best_start_s"] == 12.0
    assert track.track_config["slot_every_n_beats"] == 8


def test_patch_lyrics_config_rejects_unknown_font(client: TestClient) -> None:
    track = MagicMock()
    track.id = "track-lyrics-config"
    track.track_config = {"lyrics_config": {"enabled": True, "style": "line"}}
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}/lyrics-config",
            json={"font_family": "Not A Real Font"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
