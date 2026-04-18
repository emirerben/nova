"""Unit tests for app/tasks/music_orchestrate.py.

DB and GCS are mocked. Celery tasks are called directly (not via .delay()).
"""

import uuid
from unittest.mock import MagicMock, patch

from app.tasks.music_orchestrate import (
    analyze_music_track_task,
    orchestrate_music_job,
)

TRACK_ID = "test-track-id-0001"
JOB_ID = str(uuid.uuid4())


# ── analyze_music_track_task ──────────────────────────────────────────────────


def _make_mock_track(
    analysis_status: str = "queued",
    audio_gcs_path: str = "music/abc/audio.m4a",
    duration_s: float = 180.0,
    track_config: dict | None = None,
) -> MagicMock:
    track = MagicMock()
    track.analysis_status = analysis_status
    track.audio_gcs_path = audio_gcs_path
    track.duration_s = duration_s
    track.track_config = track_config or {}
    return track


def test_analyze_music_track_task_beats_stored_in_db() -> None:
    """analyze_music_track_task stores detected beats and sets status=ready."""
    # Need slot_every_n_beats=2 so 6 beats → n_slots=2 (guard requires > 0)
    mock_track = _make_mock_track(track_config={"slot_every_n_beats": 2})
    mock_beats = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate.download_to_file"),
        patch("app.tasks.music_orchestrate._detect_music_beats", return_value=mock_beats),
        patch("app.tasks.music_orchestrate.auto_best_section", return_value=(0.0, 5.0)),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    # beat_timestamps_s should have been set on the track
    assert mock_track.beat_timestamps_s == mock_beats
    assert mock_track.analysis_status == "ready"


def test_analyze_music_track_task_missing_track() -> None:
    """analyze_music_track_task exits silently when track not found."""
    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = None

    with patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session):
        # Should return without error
        analyze_music_track_task("nonexistent-id")


def test_analyze_music_track_task_no_audio_gcs_path() -> None:
    """analyze_music_track_task marks track as failed when audio_gcs_path is missing."""
    mock_track = _make_mock_track(audio_gcs_path="")

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate._fail_track") as mock_fail,
    ):
        analyze_music_track_task(TRACK_ID)

    mock_fail.assert_called_once()
    assert "audio" in mock_fail.call_args[0][1].lower()


def test_analyze_music_track_task_zero_beats_fails_track() -> None:
    """If _detect_music_beats returns [], the track is failed (0 slots = unsupported audio)."""
    mock_track = _make_mock_track()

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate.download_to_file"),
        patch("app.tasks.music_orchestrate._detect_music_beats", return_value=[]),
        patch("app.tasks.music_orchestrate.auto_best_section", return_value=(0.0, 45.0)),
        patch("app.tasks.music_orchestrate._fail_track") as mock_fail,
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    mock_fail.assert_called_once()
    assert "0 slots" in mock_fail.call_args[0][1]


# ── orchestrate_music_job ─────────────────────────────────────────────────────


def _make_mock_job(
    music_track_id: str = TRACK_ID,
    clip_paths: list[str] | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = uuid.UUID(JOB_ID)
    job.status = "queued"
    job.music_track_id = music_track_id
    job.all_candidates = {"clip_paths": clip_paths or ["gs://bucket/clip1.mp4"]}
    job.assembly_plan = None
    return job


def _make_mock_track_data() -> MagicMock:
    track = MagicMock()
    track.analysis_status = "ready"
    track.audio_gcs_path = "music/abc/audio.m4a"
    track.beat_timestamps_s = [float(i) for i in range(0, 40)]
    track.track_config = {
        "best_start_s": 0.0,
        "best_end_s": 39.0,
        "slot_every_n_beats": 4,
        "required_clips_min": 1,
        "required_clips_max": 10,
    }
    track.duration_s = 180.0
    return track


def test_orchestrate_music_job_track_not_ready_fails_fast() -> None:
    """Job fails immediately when music track is not in 'ready' state."""
    mock_job = _make_mock_job()
    mock_track = MagicMock()
    mock_track.analysis_status = "analyzing"
    mock_track.audio_gcs_path = "music/abc/audio.m4a"

    call_count = [0]

    def mock_get(model, id_val):
        call_count[0] += 1
        if call_count[0] == 1:
            return mock_job
        return mock_track

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.side_effect = mock_get

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate._fail_job") as mock_fail,
    ):
        orchestrate_music_job(JOB_ID)

    mock_fail.assert_called_once()
    assert "not ready" in mock_fail.call_args[0][1].lower()


def test_orchestrate_music_job_no_audio_gcs_path_fails() -> None:
    """Job fails when track has no audio_gcs_path (upload failed silently)."""
    mock_job = _make_mock_job()
    mock_track = MagicMock()
    mock_track.analysis_status = "ready"
    mock_track.audio_gcs_path = None  # Critical failure mode

    call_count = [0]

    def mock_get(model, id_val):
        call_count[0] += 1
        if call_count[0] == 1:
            return mock_job
        return mock_track

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.side_effect = mock_get

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate._fail_job") as mock_fail,
    ):
        orchestrate_music_job(JOB_ID)

    mock_fail.assert_called_once()
    assert "audio_gcs_path" in mock_fail.call_args[0][1]
