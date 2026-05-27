from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

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


def _track(**overrides) -> MagicMock:
    t = MagicMock()
    t.id = "track-preview"
    t.analysis_status = "ready"
    t.audio_gcs_path = "music/track/audio.m4a"
    t.duration_s = 5.0
    t.track_config = {"lyrics_config": {"enabled": True, "style": "line"}}
    t.lyrics_cached = {
        "lines": [
            {
                "text": "hello",
                "start_s": 1.0,
                "end_s": 2.0,
                "words": [{"text": "hello", "start_s": 1.0, "end_s": 2.0}],
            }
        ]
    }
    for key, value in overrides.items():
        setattr(t, key, value)
    return t


def _override_db(*results: MagicMock):
    session = AsyncMock()
    execute_results = []
    for item in results:
        result = MagicMock()
        result.scalar_one_or_none.return_value = item
        execute_results.append(result)
    session.execute = AsyncMock(side_effect=execute_results)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(side_effect=lambda obj: None)

    async def _override():
        yield session

    return _override, session


def test_post_lyrics_preview_returns_202_job_id(client: TestClient) -> None:
    track = _track()
    override, _session = _override_db(track)
    new_job = MagicMock()
    new_job.id = uuid4()
    app.dependency_overrides[get_db] = override
    try:
        with (
            patch("app.routes.admin_music.Job", return_value=new_job) as mock_job,
            patch(
                "app.services.job_dispatch.enqueue_orchestrator",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_enqueue.return_value = str(new_job.id)
            resp = client.post(
                f"/admin/music-tracks/{track.id}/lyrics-preview",
                json={"lyrics_config_override": {"post_dwell_s": 0.3}},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["job_id"] == str(new_job.id)
    # Default style is "line" when the request omits it — backwards-compat with
    # callers that pre-dated the multi-style dashboard.
    assert body["style"] == "line"
    effective = mock_job.call_args.kwargs["assembly_plan"]["lyrics_config_effective"]
    assert effective["post_dwell_s"] == 0.3
    assert effective["style"] == "line"


@pytest.mark.parametrize("style", ["karaoke", "per-word-pop"])
def test_post_lyrics_preview_routes_alt_style(client: TestClient, style: str) -> None:
    """Top-level ``style`` flows through to the persisted effective config and
    the response echoes it back. Pre-fix the route hardcoded ``"line"`` and
    Pop-up / Karaoke were unreachable from admin.
    """
    track = _track()
    override, _session = _override_db(track)
    new_job = MagicMock()
    new_job.id = uuid4()
    app.dependency_overrides[get_db] = override
    try:
        with (
            patch("app.routes.admin_music.Job", return_value=new_job) as mock_job,
            patch(
                "app.services.job_dispatch.enqueue_orchestrator",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_enqueue.return_value = str(new_job.id)
            resp = client.post(
                f"/admin/music-tracks/{track.id}/lyrics-preview",
                json={"style": style},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["style"] == style
    effective = mock_job.call_args.kwargs["assembly_plan"]["lyrics_config_effective"]
    assert effective["style"] == style
    assert mock_job.call_args.kwargs["assembly_plan"]["lyric_style"] == style


def test_post_lyrics_preview_strips_line_only_keys_for_alt_styles(
    client: TestClient,
) -> None:
    """The validator rejects line-only knobs (pre_roll_s, fade_in_ms, …) on
    non-Line configs. When the track's saved config carries those (a typical
    Line-tuned track) AND the admin picks Karaoke for preview, the route
    must drop them before validation — otherwise a 422 would shadow the
    real bug: that the user just wanted to see Karaoke render, not edit
    the persisted config. The persisted track config is untouched.
    """
    track = _track(
        track_config={
            "lyrics_config": {
                "enabled": True,
                "style": "line",
                "pre_roll_s": 0.4,
                "fade_in_ms": 50,
                "post_dwell_s": 1.0,
            }
        }
    )
    override, _session = _override_db(track)
    new_job = MagicMock()
    new_job.id = uuid4()
    app.dependency_overrides[get_db] = override
    try:
        with (
            patch("app.routes.admin_music.Job", return_value=new_job) as mock_job,
            patch(
                "app.services.job_dispatch.enqueue_orchestrator",
                new_callable=AsyncMock,
            ) as mock_enqueue,
        ):
            mock_enqueue.return_value = str(new_job.id)
            resp = client.post(
                f"/admin/music-tracks/{track.id}/lyrics-preview",
                json={"style": "karaoke"},
                headers=_admin_headers(),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 202, resp.text
    effective = mock_job.call_args.kwargs["assembly_plan"]["lyrics_config_effective"]
    for stripped in ("pre_roll_s", "fade_in_ms", "post_dwell_s"):
        assert stripped not in effective, (
            f"line-only key {stripped!r} leaked into karaoke preview config"
        )
    assert effective["style"] == "karaoke"


def test_post_lyrics_preview_rejects_unknown_style(client: TestClient) -> None:
    """Top-level ``style`` is validated against the literal union. An unknown
    style returns 422, never reaches the renderer.
    """
    resp = client.post(
        "/admin/music-tracks/track-preview/lyrics-preview",
        json={"style": "starwipe"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 422


def test_post_lyrics_preview_rejects_missing_lyrics(client: TestClient) -> None:
    track = _track(lyrics_cached=None)
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-preview",
            json={},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422


def test_post_lyrics_preview_rejects_empty_lines_array(client: TestClient) -> None:
    """`lyrics_cached` carrying metadata (source, lrclib_id, …) but an empty
    `lines` array used to slip past the `if not track.lyrics_cached` guard and
    burn a worker on a job that always failed with "no renderable lyric
    overlays". The route now rejects upfront with a useful message.
    """
    track = _track(lyrics_cached={"source": "lrclib_synced+whisper", "lines": []})
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-preview",
            json={},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
    assert "no lyric lines" in resp.json()["detail"]


def test_post_lyrics_preview_rejects_missing_audio(client: TestClient) -> None:
    track = _track(audio_gcs_path=None)
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.post(
            f"/admin/music-tracks/{track.id}/lyrics-preview",
            json={},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
    assert "audio" in resp.json()["detail"]


def test_post_lyrics_preview_rejects_invalid_override(client: TestClient) -> None:
    resp = client.post(
        "/admin/music-tracks/track-preview/lyrics-preview",
        json={"lyrics_config_override": {"post_dwell_s": 10.0}},
        headers=_admin_headers(),
    )

    assert resp.status_code == 422


def test_get_lyrics_preview_status_shape(client: TestClient) -> None:
    track = _track()
    job = MagicMock()
    job.id = uuid4()
    job.job_type = "lyrics_preview"
    job.music_track_id = track.id
    job.status = "music_ready"
    job.error_detail = None
    job.created_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    job.assembly_plan = {
        "output_url": "https://example.com/out.mp4",
        "lyrics_config_effective": {"post_dwell_s": 0.3},
        "preview_start_s": 28.80,
        "preview_duration_s": 20.0,
    }
    override, _session = _override_db(track, job)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/lyrics-preview-jobs/{job.id}/status",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == str(job.id)
    assert body["output_url"] == "https://example.com/out.mp4"
    assert body["lyrics_config_effective"] == {"post_dwell_s": 0.3}
    # Window must flow through to the response so the frontend can render the
    # "Previewing m:ss – m:ss" caption. Without it, the auto-anchor change is
    # silent and admins watching a song with a 30s instrumental intro would
    # think the wrong song was loaded.
    assert body["preview_start_s"] == 28.80
    assert body["preview_duration_s"] == 20.0


def test_get_lyrics_preview_status_omits_window_when_plan_lacks_it(
    client: TestClient,
) -> None:
    """Legacy lyrics_preview rows (rendered before this PR) don't have
    `preview_start_s` / `preview_duration_s` in their assembly_plan. The
    response must omit those fields rather than 500ing on a type coercion.
    """
    track = _track()
    job = MagicMock()
    job.id = uuid4()
    job.job_type = "lyrics_preview"
    job.music_track_id = track.id
    job.status = "music_ready"
    job.error_detail = None
    job.created_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    job.assembly_plan = {
        "output_url": "https://example.com/out.mp4",
        "lyrics_config_effective": {"post_dwell_s": 0.3},
    }
    override, _session = _override_db(track, job)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/lyrics-preview-jobs/{job.id}/status",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["preview_start_s"] is None
    assert body["preview_duration_s"] is None


def test_get_lyrics_preview_status_surfaces_style(client: TestClient) -> None:
    """The status response carries the rendered style so the frontend can
    route the result back to the correct preview slot (line/popup/karaoke).
    Reads from the explicit ``lyric_style`` field in assembly_plan; falls
    back to ``lyrics_config_effective.style`` for legacy rows.
    """
    track = _track()
    job = MagicMock()
    job.id = uuid4()
    job.job_type = "lyrics_preview"
    job.music_track_id = track.id
    job.status = "music_ready"
    job.error_detail = None
    job.created_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    job.assembly_plan = {
        "output_url": "https://example.com/out.mp4",
        "lyrics_config_effective": {"style": "karaoke"},
        "lyric_style": "karaoke",
    }
    override, _session = _override_db(track, job)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/lyrics-preview-jobs/{job.id}/status",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["style"] == "karaoke"


def test_get_lyrics_preview_status_legacy_row_falls_back_to_cfg_style(
    client: TestClient,
) -> None:
    """Legacy rows (rendered before this PR) have no ``lyric_style`` field.
    The status endpoint must fall back to ``lyrics_config_effective.style``
    so the frontend can still group historical previews by style.
    """
    track = _track()
    job = MagicMock()
    job.id = uuid4()
    job.job_type = "lyrics_preview"
    job.music_track_id = track.id
    job.status = "music_ready"
    job.error_detail = None
    job.created_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    job.assembly_plan = {
        "output_url": "https://example.com/out.mp4",
        "lyrics_config_effective": {"style": "line"},
        # NOTE: no `lyric_style` key — simulates a pre-multi-style-dashboard row
    }
    override, _session = _override_db(track, job)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/lyrics-preview-jobs/{job.id}/status",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["style"] == "line"


def test_get_lyrics_preview_status_mismatched_track_returns_404(
    client: TestClient,
) -> None:
    track = _track()
    job = MagicMock()
    job.id = uuid4()
    job.job_type = "lyrics_preview"
    job.music_track_id = "other-track"
    override, _session = _override_db(track, job)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.get(
            f"/admin/music-tracks/{track.id}/lyrics-preview-jobs/{job.id}/status",
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404
