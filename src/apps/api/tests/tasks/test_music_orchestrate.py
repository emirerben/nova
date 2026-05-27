"""Unit tests for app/tasks/music_orchestrate.py.

DB and GCS are mocked. Celery tasks are called directly (not via .delay()).
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from app.agents._runtime import RefusalError
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
    track.best_sections = None
    track.section_version = None
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


def test_analyze_music_track_retries_when_song_sections_all_invalid() -> None:
    """All-invalid song_sections output is retried before permanent failure.

    LLM constraint misses can be transient. The task should use Celery retry
    first, not immediately mark the track failed on the first refusal.
    """
    mock_track = _make_mock_track(track_config={"slot_every_n_beats": 2})
    mock_track.best_sections = [{"rank": 1, "start_s": 60.0, "end_s": 78.0}]
    mock_track.section_version = "2026-05-15"
    mock_beats = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    mock_gemini_recipe = {
        "shot_count": 2,
        "total_duration_s": 5.0,
        "slots": [
            {"position": 1, "target_duration_s": 2.5, "energy": 7.0},
            {"position": 2, "target_duration_s": 2.5, "energy": 5.0},
        ],
        "beat_timestamps_s": mock_beats,
    }

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track
    mock_file_ref = MagicMock()

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate.download_to_file"),
        patch("app.tasks.music_orchestrate._detect_music_beats", return_value=mock_beats),
        patch("app.tasks.music_orchestrate.auto_best_section", return_value=(0.0, 5.0)),
        patch("app.tasks.music_orchestrate.gemini_upload_and_wait", return_value=mock_file_ref),
        patch(
            "app.tasks.music_orchestrate.analyze_audio_template",
            return_value=mock_gemini_recipe,
        ),
        patch("app.tasks.music_orchestrate._run_song_classifier", return_value=None),
        patch(
            "app.tasks.music_orchestrate._run_song_sections",
            side_effect=RefusalError("song_sections: no valid sections after filter"),
        ),
        patch("app.tasks.music_orchestrate._run_lyrics_extraction", return_value=None),
        patch("app.tasks.music_orchestrate._fail_track") as mock_fail_track,
        patch.object(analyze_music_track_task, "retry", side_effect=Retry("retry")) as mock_retry,
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(Retry):
            analyze_music_track_task(TRACK_ID)

    mock_retry.assert_called_once()
    assert isinstance(mock_retry.call_args.kwargs["exc"], RefusalError)
    mock_fail_track.assert_not_called()
    assert mock_track.analysis_status == "analyzing"
    assert mock_track.best_sections == [{"rank": 1, "start_s": 60.0, "end_s": 78.0}]
    assert mock_track.section_version == "2026-05-15"


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


def test_analyze_music_track_task_gemini_populates_recipe_cached() -> None:
    """When Gemini audio analysis succeeds, recipe_cached is populated on track."""
    mock_track = _make_mock_track(track_config={"slot_every_n_beats": 2})
    mock_beats = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    mock_gemini_recipe = {
        "shot_count": 2,
        "total_duration_s": 5.0,
        "slots": [
            {
                "position": 1,
                "target_duration_s": 2.5,
                "slot_type": "hook",
                "transition_in": "whip-pan",
                "color_hint": "warm",
                "speed_factor": 1.0,
                "text_overlays": [],
                "energy": 7.0,
                "priority": 5,
            },
            {
                "position": 2,
                "target_duration_s": 2.5,
                "slot_type": "broll",
                "transition_in": "dissolve",
                "color_hint": "cool",
                "speed_factor": 1.0,
                "text_overlays": [],
                "energy": 5.0,
                "priority": 5,
            },
        ],
        "copy_tone": "energetic",
        "color_grade": "warm",
        "transition_style": "whip-pans",
        "pacing_style": "fast",
        "creative_direction": "beat-driven",
        "caption_style": "",
        "beat_timestamps_s": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
        "sync_style": "cut-on-beat",
        "interstitials": [],
        "subject_niche": "energetic-pop",
        "has_talking_head": False,
        "has_voiceover": False,
        "has_permanent_letterbox": False,
    }

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    mock_file_ref = MagicMock()

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate.download_to_file"),
        patch("app.tasks.music_orchestrate._detect_music_beats", return_value=mock_beats),
        patch("app.tasks.music_orchestrate.auto_best_section", return_value=(0.0, 5.0)),
        patch("app.tasks.music_orchestrate.gemini_upload_and_wait", return_value=mock_file_ref),
        patch(
            "app.tasks.music_orchestrate.analyze_audio_template",
            return_value=mock_gemini_recipe,
        ),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    assert mock_track.analysis_status == "ready"
    assert mock_track.recipe_cached is not None
    # Merged recipe should have visual properties from Gemini
    assert mock_track.recipe_cached.get("color_grade") == "warm"
    assert mock_track.recipe_cached_at is not None


def test_analyze_music_track_task_promotes_rank_one_to_track_config() -> None:
    """When song_sections returns a valid rank-1, track_config.best_start_s /
    best_end_s reflect the section bounds (NOT the legacy auto_best_section
    45s window) and required_clips_min/max are recomputed from the new
    window. This is the load-bearing fix that retires the 45s default for
    every downstream consumer of track_config.
    """
    from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION

    mock_track = _make_mock_track(track_config={"slot_every_n_beats": 2})
    # 240 beats every 0.5s across 120s — both windows have ample slots.
    mock_beats = [round(0.5 * i, 3) for i in range(1, 241)]

    # _run_gemini_audio_analysis returns (recipe_cached, ai_labels, sections_dict).
    # We control all three directly. recipe_cached starts as a 60s-window
    # merged recipe; the reconcile block should regenerate it for the new
    # rank-1 bounds.
    mock_recipe_cached = {
        "shot_count": 30,
        "total_duration_s": 60.0,
        "slots": [
            {
                "position": i + 1,
                "target_duration_s": 2.0,
                "slot_type": "broll",
                "transition_in": "whip-pan",
                "color_hint": "warm",
                "text_overlays": [],
                "speed_factor": 1.0,
                "energy": 5.0,
                "priority": 5,
            }
            for i in range(30)
        ],
        "color_grade": "warm",
        "transition_style": "whip-pans",
        "creative_direction": "energetic",
        "copy_tone": "playful",
    }
    mock_sections_dict = {
        "sections": [
            {
                "rank": 1,
                "start_s": 30.0,
                "end_s": 50.0,
                "label": "chorus",
                "energy": "high",
                "suggested_use": "hook",
                "rationale": "peak energy chorus section.",
            }
        ],
        "section_version": CURRENT_SECTION_VERSION,
    }

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate.download_to_file"),
        patch("app.tasks.music_orchestrate._detect_music_beats", return_value=mock_beats),
        # Legacy auto_best_section would pick (5.0, 50.0) — a 45s window.
        # The reconcile block must overwrite this with rank-1 (30.0, 50.0).
        patch("app.tasks.music_orchestrate.auto_best_section", return_value=(5.0, 50.0)),
        patch(
            "app.tasks.music_orchestrate._run_gemini_audio_analysis",
            return_value=(mock_recipe_cached, None, mock_sections_dict),
        ),
        patch("app.tasks.music_orchestrate.gemini_upload_and_wait", new=MagicMock()),
        patch("app.tasks.music_orchestrate.analyze_audio_template", new=MagicMock()),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    assert mock_track.analysis_status == "ready"
    cfg = mock_track.track_config
    # Rank-1 bounds win over auto_best_section's 45s window.
    assert cfg["best_start_s"] == 30.0
    assert cfg["best_end_s"] == 50.0
    # required_clips were recomputed for the 20s window (not the 45s default).
    # 41 beats inside [30.0, 50.0] @ 0.5s spacing, slot_every_n_beats=2 →
    # n_slots = ceil((41-2)/2) = 20 (per range(0, len-n, n))
    assert cfg["required_clips_max"] >= 1
    assert cfg["required_clips_max"] < 30  # NOT the original 60s-window count
    assert cfg["required_clips_min"] >= 1
    # recipe_cached was regenerated against the new bounds, preserving
    # visual fields from the previous merged cache.
    assert mock_track.recipe_cached is not None
    assert mock_track.recipe_cached.get("color_grade") == "warm"
    assert mock_track.recipe_cached["total_duration_s"] == 20.0


def test_analyze_music_track_task_keeps_legacy_when_no_sections() -> None:
    """Without sections_dict the auto_best_section 45s window stays as the
    canonical track_config — the same behavior as before the fix.
    Guards against accidentally over-aggressive promotion.
    """
    mock_track = _make_mock_track(track_config={"slot_every_n_beats": 2})
    mock_beats = [round(0.5 * i, 3) for i in range(1, 241)]

    mock_recipe_cached = {
        "shot_count": 30,
        "total_duration_s": 45.0,
        "slots": [],
        "color_grade": "warm",
    }

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.music_orchestrate.download_to_file"),
        patch("app.tasks.music_orchestrate._detect_music_beats", return_value=mock_beats),
        patch("app.tasks.music_orchestrate.auto_best_section", return_value=(5.0, 50.0)),
        patch(
            "app.tasks.music_orchestrate._run_gemini_audio_analysis",
            return_value=(mock_recipe_cached, None, None),  # sections_dict=None
        ),
        patch("app.tasks.music_orchestrate.gemini_upload_and_wait", new=MagicMock()),
        patch("app.tasks.music_orchestrate.analyze_audio_template", new=MagicMock()),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    assert mock_track.analysis_status == "ready"
    cfg = mock_track.track_config
    # Legacy auto_best_section bounds preserved when sections are missing.
    assert cfg["best_start_s"] == 5.0
    assert cfg["best_end_s"] == 50.0


def test_analyze_music_track_task_gemini_failure_falls_back_to_beat_only() -> None:
    """When Gemini fails, track still reaches 'ready' with a beat-only recipe."""
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
        patch(
            "app.tasks.music_orchestrate.gemini_upload_and_wait",
            side_effect=Exception("rate limited"),
        ),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    # Track should still be ready, with a fallback beat-only recipe
    assert mock_track.analysis_status == "ready"
    assert mock_track.recipe_cached is not None
    # Fallback recipe has default color_grade="none" (no Gemini visuals)
    assert mock_track.recipe_cached.get("color_grade") == "none"


# ── Bug A regression lock: audio offset must reach _mix_template_audio ────────


_MUSIC_ORCHESTRATE_SOURCE_PATH = (
    "app/tasks/music_orchestrate.py"  # repo-rooted; pytest cwd is `src/apps/api`
)


def _read_music_orchestrate_source() -> str:
    """Read music_orchestrate.py from disk without importing it.

    The structural-lock tests below need to inspect the source of two
    private functions. We deliberately avoid `inspect.getsource` (which
    requires `import app.tasks.music_orchestrate`) because that module
    has heavy transitive imports (sqlalchemy, celery, fastapi, ...) which
    are not needed for source-level invariant checks.
    """
    import os

    # pytest may be invoked from various cwd's; resolve relative to this file.
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    path = os.path.join(api_dir, _MUSIC_ORCHESTRATE_SOURCE_PATH)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_function_body(source: str, func_name: str) -> str:
    """Return the body of `def func_name(...)` up to the next top-level `def`.

    Top-level meaning at column 0 — we don't care about nested defs, just
    the slice belonging to this function.
    """
    needle = f"\ndef {func_name}("
    start = source.find(needle)
    assert start != -1, f"function {func_name} not found in source"
    next_def = source.find("\ndef ", start + len(needle))
    end = next_def if next_def != -1 else len(source)
    return source[start:end]


def _extract_mix_template_audio_call(func_source: str) -> str:
    """Pull the (possibly multi-line) `_mix_template_audio(...)` call from `func_source`.

    Returns the text from `_mix_template_audio(` through its closing paren.
    Used by the structural-lock tests below.
    """
    call_idx = func_source.index("_mix_template_audio(")
    depth = 0
    for i in range(call_idx, len(func_source)):
        ch = func_source[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return func_source[call_idx : i + 1]
    raise AssertionError("Unbalanced parens in _mix_template_audio call")


def test_run_music_job_passes_audio_start_offset_s_to_mixer() -> None:
    """Lock the Bug A fix: `_run_music_job` MUST pass `audio_start_offset_s`.

    Without this kwarg, `_mix_template_audio` defaults the offset to 0.0 and
    FFmpeg plays the song from t=0 — while the lyric injector positions
    overlays in section-relative time (subtracting `best_start_s` from every
    word). The two timelines drift by `best_start_s` seconds. On the prod
    repro (Travis Scott — HIGHEST IN THE ROOM, best_start_s=68.7s) the lyrics
    were 68.7 seconds out of sync with the audio.

    This is a structural source-inspection test rather than a full mock-based
    integration test. Mocking the entire _run_music_job pipeline (download,
    probe, Gemini upload+analyze, template match, assemble, mix, upload)
    requires ~12 patches and is brittle; the structural test catches the
    regression cleanly and explains *why* it matters in the failure message.
    """
    src = _read_music_orchestrate_source()
    body = _extract_function_body(src, "_run_music_job")
    mix_call = _extract_mix_template_audio_call(body)
    assert "audio_start_offset_s" in mix_call, (
        "_mix_template_audio in _run_music_job is missing the "
        "audio_start_offset_s kwarg. Without it the song plays from t=0 "
        "while lyric overlays are positioned in section-relative time, "
        "causing drift of best_start_s seconds (Bug A — fixed in #257).\n\n"
        f"Current call:\n{mix_call}"
    )
    # The offset must come from track_config best_start_s, not a literal 0
    # or a stale local variable. Document the explicit data dependency.
    assert "best_start_s" in mix_call, (
        "_mix_template_audio must derive audio_start_offset_s from "
        "track_config.best_start_s (the same source used for the lyric "
        f"injector call). Current call:\n{mix_call}"
    )


def test_run_templated_music_job_passes_audio_start_offset_s_to_mixer() -> None:
    """Mirror Bug A lock for the templated-music path.

    Templated tracks (typed-slot recipes like Love From Moon) currently default
    `best_start_s` to 0, so this kwarg is a latent guard rather than an active
    fix on the prod repro. Still required: a future templated track configured
    with a non-zero `best_start_s` would otherwise resurface the same drift
    that bit `_run_music_job`.
    """
    src = _read_music_orchestrate_source()
    body = _extract_function_body(src, "_run_templated_music_job")
    mix_call = _extract_mix_template_audio_call(body)
    assert "audio_start_offset_s" in mix_call, (
        "_mix_template_audio in _run_templated_music_job is missing the "
        "audio_start_offset_s kwarg — see _run_music_job for the original "
        "incident. Latent for templated tracks today (best_start_s==0 by "
        "convention) but required as defense-in-depth.\n\n"
        f"Current call:\n{mix_call}"
    )
    assert "best_start_s" in mix_call


# ── Time-limit parity lock: music vs template orchestrator budgets ───────────


def _read_template_orchestrate_source() -> str:
    """Sibling of _read_music_orchestrate_source() (added in PR #258).

    Reads template_orchestrate.py as a raw string so the parity-check test
    below can compare its Celery decorator kwargs against music's without
    importing the module (which would drag in sqlalchemy/celery/fastapi).
    """
    import os

    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    path = os.path.join(api_dir, "app/tasks/template_orchestrate.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_celery_task_decorator(source: str, task_name_substr: str) -> str:
    """Return the `@celery_app.task(...)` block whose `name=` matches.

    Walks backward from the matched `name="tasks.<substr>"` line to find the
    enclosing `@celery_app.task(` and forward via Python's tokenizer to its
    matching closing paren. Tokenizing (instead of naive char-by-char paren
    counting) is essential because decorator kwarg comments frequently contain
    unbalanced parens in prose (e.g. `# foo (incident bar`), which would fool
    a string-level scan into reporting a missing close paren.
    """
    import io
    import tokenize

    name_idx = source.index(f'name="tasks.{task_name_substr}"')
    decorator_idx = source.rfind("@celery_app.task(", 0, name_idx)
    assert decorator_idx != -1, f"decorator not found for {task_name_substr}"

    # Tokenize the source slice starting at the decorator. The first OP token
    # `(` opens the decorator call; we walk OP tokens, ignoring COMMENT and
    # STRING tokens, until the matching `)` brings depth back to zero.
    slice_src = source[decorator_idx:]
    tokens = tokenize.generate_tokens(io.StringIO(slice_src).readline)
    depth = 0
    saw_open = False
    for tok in tokens:
        if tok.type != tokenize.OP:
            continue
        if tok.string == "(":
            depth += 1
            saw_open = True
        elif tok.string == ")":
            depth -= 1
            if saw_open and depth == 0:
                # tok.end is (row, col) — convert back to a string offset.
                end_row, end_col = tok.end
                lines = slice_src.splitlines(keepends=True)
                offset = sum(len(line) for line in lines[: end_row - 1]) + end_col
                return source[decorator_idx : decorator_idx + offset]
    raise AssertionError(f"unbalanced parens in decorator for {task_name_substr}")


def _extract_limit_kwarg(decorator_src: str, kwarg_name: str) -> int:
    import re

    m = re.search(rf"\b{kwarg_name}\s*=\s*(\d+)", decorator_src)
    assert m, f"{kwarg_name} kwarg not found in decorator:\n{decorator_src}"
    return int(m.group(1))


def test_music_job_time_limit_is_at_least_template_job_time_limit() -> None:
    """The music orchestrator runs the same heavy `_assemble_clips` →
    reframe → ASS-burn → mix pipeline as the template orchestrator. Their
    Celery time budgets must stay in lockstep — otherwise the same 10-clip
    HDR-iPhone input that template renders comfortably will trip the music
    soft limit.

    Incident: job ceaed607 hit SoftTimeLimitExceeded at 1080s with lyric
    burn ~90% complete (audio mix + final encode never ran). The post-#258
    music pipeline now does extra overlay-burn work that the legacy
    1080s/1200s budget did not anticipate. Empirical phase breakdown is in
    the PR description and the plan file
    (plans/we-replaced-genius-with-ancient-shamir.md).

    Source-inspection by file read (NOT inspect.getsource on the imported
    module) to keep this test free of sqlalchemy/celery/fastapi imports.
    Matches the pattern PR #258 established for the Bug A locks above.
    """
    music_src = _read_music_orchestrate_source()
    tmpl_src = _read_template_orchestrate_source()

    music_dec = _extract_celery_task_decorator(music_src, "orchestrate_music_job")
    tmpl_dec = _extract_celery_task_decorator(tmpl_src, "orchestrate_template_job")

    music_soft = _extract_limit_kwarg(music_dec, "soft_time_limit")
    music_hard = _extract_limit_kwarg(music_dec, "time_limit")
    tmpl_soft = _extract_limit_kwarg(tmpl_dec, "soft_time_limit")
    tmpl_hard = _extract_limit_kwarg(tmpl_dec, "time_limit")

    assert music_soft >= tmpl_soft, (
        f"orchestrate_music_job.soft_time_limit ({music_soft}) is below "
        f"orchestrate_template_job.soft_time_limit ({tmpl_soft}). Music jobs "
        "run the same `_assemble_clips` → reframe → ASS-burn → mix pipeline "
        "as templates, so they need at least the same budget. See "
        "plans/we-replaced-genius-with-ancient-shamir.md."
    )
    assert music_hard >= tmpl_hard, (
        f"orchestrate_music_job.time_limit ({music_hard}) is below "
        f"orchestrate_template_job.time_limit ({tmpl_hard}). Hard limits "
        "must move together with soft limits — a tight hard limit will "
        "SIGKILL the worker before the soft-limit grace period elapses."
    )


# ── _coerce_best_start_s (review feedback #2: None-safe parser) ──────────────


def test_coerce_best_start_s_handles_none_invalid_nan() -> None:
    """Review #2: a None / non-numeric / NaN `best_start_s` must NOT crash
    the music orchestrator. The DB column is nullable (admins who never set
    the section leave it unset) and partial config writes may surface
    pydantic-coerced NaN or string values. Crashing on `float(None)` would
    brick the entire job — the helper returns 0.0 instead so the pipeline
    keeps going with the sensible default."""
    from app.tasks.music_orchestrate import _coerce_best_start_s

    # The five cases the helper must absorb without raising:
    assert _coerce_best_start_s(None) == 0.0
    assert _coerce_best_start_s({}) == 0.0
    assert _coerce_best_start_s({"best_start_s": None}) == 0.0
    assert _coerce_best_start_s({"best_start_s": "garbage"}) == 0.0
    assert _coerce_best_start_s({"best_start_s": float("nan")}) == 0.0

    # And the happy path stays exact (no precision loss):
    assert _coerce_best_start_s({"best_start_s": 128.0}) == 128.0
    assert _coerce_best_start_s({"best_start_s": 128}) == 128.0  # int → float
    assert _coerce_best_start_s({"best_start_s": "128.5"}) == 128.5  # string number


# ── _run_lyrics_extraction kwarg wire (Hawai duration-disambiguation) ────────


def test_run_lyrics_extraction_passes_duration_s_to_agent() -> None:
    """The orchestrator must thread `duration_s` through to LyricsInput so
    the agent can pass it to LRCLIB's `/api/get?duration=N` for recording
    disambiguation. If this kwarg ever silently drops (e.g. someone renames
    the helper signature), the version-mismatch defense regresses without
    anything failing loudly — both call sites (`analyze_music_track_task`
    and `extract_track_lyrics_task`) would still type-check and the agent
    would still produce a result, just with LRCLIB's wrong-recording
    syncedLyrics back at the start of the pipeline.
    """
    from app.tasks.music_orchestrate import _run_lyrics_extraction

    captured_inputs: list = []

    class _CapturingAgent:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def run(self, lyrics_input, ctx=None):  # noqa: ARG002
            captured_inputs.append(lyrics_input)
            output = MagicMock()
            output.is_empty = True
            output.source = "lrclib_synced+whisper"
            output.model_dump = MagicMock(return_value={})
            return output

    mock_track = MagicMock()
    mock_track.title = "Hawai"
    mock_track.artist = "Maluma"

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    mock_settings = MagicMock()
    mock_settings.openai_api_key = "sk-test"

    with (
        patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session),
        patch("app.agents.lyrics.LyricsExtractionAgent", _CapturingAgent),
        patch("app.config.settings", mock_settings),
    ):
        _run_lyrics_extraction(
            "/tmp/audio.m4a",
            TRACK_ID,
            best_start_s=0.0,
            best_end_s=180.0,
            duration_s=211.6,
        )

    assert len(captured_inputs) == 1
    assert captured_inputs[0].duration_s == 211.6
    assert captured_inputs[0].best_start_s == 0.0
    assert captured_inputs[0].best_end_s == 180.0
