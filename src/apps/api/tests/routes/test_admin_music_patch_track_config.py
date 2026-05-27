"""Integration tests for the PATCH /admin/music-tracks/{id} slot-count guard.

The validator at `app/routes/admin_music.py` rejects (best_start_s, best_end_s,
slot_every_n_beats) combinations that would produce 0 slots when fed to
`generate_music_recipe()`. Before this guard existed an admin could PATCH a
track into a state where every job submission failed inside the worker — the
Marea (Fred Again) prod incident (job e47ba052, 2026-05-25): a 13.4s bridge
window with only 5 beats and slot_every_n_beats=8 silently saved as
analysis_status=ready, then `generate_music_recipe` raised ValueError every
time a job was submitted.

The guard mirrors the same arithmetic the recipe generator uses
(`music_recipe.count_slots`), so PATCH and the worker can never disagree.
"""

from datetime import UTC, datetime
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
    """Mirror the pattern in test_admin_music_lyrics_config_patch.py."""
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
    beats: list[float] | None,
    track_config: dict | None = None,
    analysis_status: str = "ready",
) -> MagicMock:
    """Build a mock track sufficient for `_get_track_or_404` + `_to_response`."""
    t = MagicMock()
    t.id = "track-test"
    t.title = "Test Track"
    t.artist = "Test Artist"
    t.source_url = "https://youtube.com/watch?v=test"
    t.audio_gcs_path = "music/test/audio.m4a"
    t.duration_s = 180.0
    t.beat_timestamps_s = beats
    t.analysis_status = analysis_status
    t.error_detail = None
    t.thumbnail_url = None
    t.published_at = None
    t.archived_at = None
    t.track_config = track_config
    t.best_sections = None
    t.section_version = None
    t.ai_labels = None
    t.label_version = None
    t.lyrics_status = "pending"
    t.lyrics_source = None
    t.lyrics_error_detail = None
    t.lyrics_cached = None
    t.lyrics_whisper_draft = None
    t.lyrics_diagnostic = None
    t.lyrics_extraction_version = 0
    t.lyrics_extracted_at = None
    t.created_at = datetime.now(UTC)
    return t


# ── 422 path: the bug we are fixing ──────────────────────────────────────────


def test_patch_rejects_zero_slot_window(client: TestClient) -> None:
    """The exact Marea config that escaped the analyzer must be rejected at PATCH time."""
    # Beats reproducing prod job e47ba052: 5 beats in [156.6, 170.0]
    marea_beats = [159.474, 165.447, 165.895, 167.026, 169.799]
    track = _make_track(beats=marea_beats, track_config={"slot_every_n_beats": 8})
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"track_config": {"best_start_s": 156.6, "best_end_s": 170.0}},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "0 slots" in detail
    assert "5 beats" in detail
    assert "slot_every_n_beats=8" in detail
    # Track config was NOT mutated (must not write before the guard).
    assert track.track_config == {"slot_every_n_beats": 8}


def test_patch_rejects_zero_slot_window_when_n_lowered(client: TestClient) -> None:
    """If admin lowers n=4 but window is still too narrow, still reject.

    Marea with n=4 actually produces 1 slot (not 0), so it would PASS this
    guard. Use a tighter beat layout where even n=2 stays at 0.
    """
    # 1 beat in the window → range(0, -1, 2) = [] regardless of n
    track = _make_track(beats=[5.0], track_config={"slot_every_n_beats": 4})
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"track_config": {"best_start_s": 0.0, "best_end_s": 10.0}},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 422
    assert "0 slots" in resp.json()["detail"]


# ── 200 paths: guard must not fire when it shouldn't ──────────────────────────


def test_patch_accepts_window_that_produces_slots(client: TestClient) -> None:
    """Drop window (47.8-64.14, dense beats) — the recommended Marea fix — must pass."""
    # 48 beats spaced every 0.33s across [47.8, 64.14] (~3 beats/s = the drop)
    dense_beats = [47.8 + (i * 0.33) for i in range(48)]
    track = _make_track(beats=dense_beats, track_config={"slot_every_n_beats": 8})
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"track_config": {"best_start_s": 47.8, "best_end_s": 64.14}},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    assert track.track_config["best_start_s"] == 47.8
    assert track.track_config["best_end_s"] == 64.14


def test_patch_title_only_skips_guard_even_on_broken_track(client: TestClient) -> None:
    """A title-only edit on a track that already has a 0-slot config must succeed.

    Otherwise admins can't edit metadata on broken tracks without first fixing
    the window — surprise 422 on an unrelated field would be terrible UX.
    """
    marea_beats = [159.474, 165.447, 165.895, 167.026, 169.799]
    track = _make_track(
        beats=marea_beats,
        track_config={
            "best_start_s": 156.6,
            "best_end_s": 170.0,
            "slot_every_n_beats": 8,
        },
    )
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"title": "Renamed"},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    assert track.title == "Renamed"


def test_patch_skips_guard_when_track_has_no_beats(client: TestClient) -> None:
    """Track still analyzing — beat_timestamps_s is empty.

    Admins sometimes pre-populate best_start_s/best_end_s while analysis is
    still running so the analyzer keeps their window (see
    music_orchestrate.py:247-253 — only auto-picks when end <= start). The
    guard must not block this workflow; the analyzer's own n_slots check
    will catch a truly broken config once beats land.
    """
    track = _make_track(beats=None, analysis_status="analyzing")
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={"track_config": {"best_start_s": 156.6, "best_end_s": 170.0}},
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    assert track.track_config["best_start_s"] == 156.6


def test_patch_track_config_preserves_existing_keys(client: TestClient) -> None:
    """The admin music page's Save handler sends only the three fields the form
    edits (`best_start_s`, `best_end_s`, `slot_every_n_beats`) — but track_config
    in production also carries `lyrics_config`, `required_clips_min`,
    `required_clips_max`, and others. The PATCH handler MUST deep-merge so a
    partial Save does not drop those fields.

    Without this lock, a future refactor swapping the merge for
    `track.track_config = req.track_config` would silently wipe the entire
    track's lyrics styling and clip-count config every time an admin saves a
    new section window. The bug would only surface the next time a music job
    runs (LyricsConfig defaults swap in) — too late to catch in code review.
    """
    # Dense beats so the slot-count guard passes on the new window
    dense_beats = [47.8 + (i * 0.33) for i in range(48)]
    existing_lyrics_config = {
        "enabled": True,
        "style": "line",
        "pre_roll_s": 0.15,
        "post_dwell_s": 1.25,
        "next_line_gap_s": 0.05,
        "fade_in_ms": 200,
        "fade_out_ms": 300,
        "hold_to_next_threshold_ms": 600,
    }
    track = _make_track(
        beats=dense_beats,
        track_config={
            "best_start_s": 30.0,
            "best_end_s": 50.0,
            "slot_every_n_beats": 8,
            "lyrics_config": existing_lyrics_config,
            "required_clips_min": 3,
            "required_clips_max": 12,
        },
    )
    override, _session = _override_db(track)
    app.dependency_overrides[get_db] = override
    try:
        # Mirror the frontend's actual Save payload — only the three form fields.
        resp = client.patch(
            f"/admin/music-tracks/{track.id}",
            json={
                "track_config": {
                    "best_start_s": 47.8,
                    "best_end_s": 64.14,
                    "slot_every_n_beats": 8,
                }
            },
            headers=_admin_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    # Patched fields took effect.
    assert track.track_config["best_start_s"] == 47.8
    assert track.track_config["best_end_s"] == 64.14
    # Existing keys SURVIVED the merge.
    assert track.track_config["lyrics_config"] == existing_lyrics_config
    assert track.track_config["required_clips_min"] == 3
    assert track.track_config["required_clips_max"] == 12
