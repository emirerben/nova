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

    # _run_gemini_audio_analysis returns
    # (recipe_cached, ai_labels, sections_dict, sections_error).
    # We control all four directly. recipe_cached starts as a 60s-window
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
            return_value=(mock_recipe_cached, None, mock_sections_dict, None),
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
            return_value=(mock_recipe_cached, None, None, None),  # sections_dict=None
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
    # The (sections_dict=None, sections_error=None) skip branch must NOT
    # write to section_error_detail — "agent skipped" is distinct from
    # "agent failed", and the elif guard at the persistence step exists
    # exactly to prevent the skip from leaving misleading text on the row.
    assert mock_track.section_error_detail is None


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


# ── Forced LRCLIB ID + stale-task protection (NEW: 2026-05-27) ──────────────


def _make_mock_track_for_lyrics(
    *,
    extraction_version: int = 0,
    forced_lrclib_id: int | None = None,
    title: str = "Beauty And A Beat",
    artist: str = "Justin Bieber",
) -> MagicMock:
    """Build a MagicMock that mimics MusicTrack for _run_lyrics_extraction.

    The real model has typed attributes; the orchestrator reads
    `track.lyrics_extraction_version` (int), `track.track_config` (dict|None),
    `track.title`/`track.artist` (str), and writes back several lyrics_* fields.
    """
    mock = MagicMock()
    mock.title = title
    mock.artist = artist
    mock.id = TRACK_ID
    mock.lyrics_extraction_version = extraction_version
    mock.lyrics_status = "extracting"
    if forced_lrclib_id is not None:
        mock.track_config = {"lyrics_config": {"forced_lrclib_id": forced_lrclib_id}}
    else:
        mock.track_config = None
    return mock


def test_run_lyrics_extraction_threads_forced_lrclib_id_to_agent() -> None:
    """Admin pastes a row ID → it ends up in `track_config.lyrics_config.forced_lrclib_id`
    → orchestrator must read it and put it on LyricsInput so the agent
    fetches that exact LRCLIB row instead of doing title/artist search."""
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
            output.lyrics_diagnostic = {}
            output.model_dump = MagicMock(return_value={})
            return output

    track = _make_mock_track_for_lyrics(forced_lrclib_id=8543210)

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = track

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
            duration_s=212.0,
        )

    assert len(captured_inputs) == 1
    assert captured_inputs[0].forced_lrclib_id == 8543210


def test_run_lyrics_extraction_ignores_invalid_forced_id_in_config() -> None:
    """Garbage in track_config.lyrics_config.forced_lrclib_id (e.g. legacy
    string, negative number) must NOT crash the extraction. The agent runs
    the normal title/artist search."""
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
            output.lyrics_diagnostic = {}
            output.model_dump = MagicMock(return_value={})
            return output

    track = MagicMock()
    track.title = "X"
    track.artist = "Y"
    track.id = TRACK_ID
    track.lyrics_extraction_version = 0
    # Pathological config: not an int.
    track.track_config = {"lyrics_config": {"forced_lrclib_id": "not-an-int"}}

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = track

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
            best_end_s=0.0,
            duration_s=0.0,
        )

    # forced_lrclib_id ended up None — fell back to title/artist search.
    assert captured_inputs[0].forced_lrclib_id is None


def test_apply_lyrics_result_routes_publishable_to_cached() -> None:
    """`lrclib_synced+whisper` outputs go to lyrics_cached + status=ready,
    and the Whisper draft column is cleared (any prior draft is now stale)."""
    from app.tasks.music_orchestrate import _apply_lyrics_result

    track = MagicMock()
    track.lyrics_extraction_version = 5
    track.lyrics_whisper_draft = {"prior": "draft"}  # should be cleared

    _apply_lyrics_result(
        track,
        {
            "status": "ready",
            "source": "lrclib_synced+whisper",
            "output": {"source": "lrclib_synced+whisper", "lines": []},
            "diagnostic": {"fallback_path": "ready_synced"},
            "version_snapshot": 5,
        },
    )

    assert track.lyrics_status == "ready"
    assert track.lyrics_source == "lrclib_synced+whisper"
    assert track.lyrics_cached == {"source": "lrclib_synced+whisper", "lines": []}
    assert track.lyrics_whisper_draft is None  # cleared
    assert track.lyrics_diagnostic == {"fallback_path": "ready_synced"}


def test_apply_lyrics_result_routes_whisper_only_to_draft_with_needs_manual_status() -> None:
    """The Beauty And A Beat policy: whisper_only output is NEVER written to
    lyrics_cached. Status flips to needs_manual_lyrics, draft column gets the
    Whisper output for admin reference, error message hints at the recovery
    path (paste a row ID)."""
    from app.tasks.music_orchestrate import _apply_lyrics_result

    track = MagicMock()
    track.lyrics_extraction_version = 3
    track.lyrics_cached = {"stale": "publishable"}  # should be cleared

    _apply_lyrics_result(
        track,
        {
            "status": "needs_manual_lyrics",
            "source": "whisper_only",
            "whisper_draft": {"source": "whisper_only", "lines": [{"text": "hello"}]},
            "diagnostic": {"fallback_path": "needs_manual_lyrics"},
            "version_snapshot": 3,
        },
    )

    assert track.lyrics_status == "needs_manual_lyrics"
    assert track.lyrics_cached is None  # CRITICAL — never publish Whisper-only
    assert track.lyrics_whisper_draft == {
        "source": "whisper_only",
        "lines": [{"text": "hello"}],
    }
    assert track.lyrics_source == "whisper_only"
    assert track.lyrics_diagnostic == {"fallback_path": "needs_manual_lyrics"}
    assert "paste" in (track.lyrics_error_detail or "").lower()


def test_apply_lyrics_result_stale_task_discards_mutation() -> None:
    """The Beauty And A Beat sprint scenario: admin pastes wrong ID, then
    pastes the right ID before the first task finishes. The newer task bumped
    `lyrics_extraction_version` from 5→6 at dispatch. When the older task
    arrives at `_apply_lyrics_result` with `version_snapshot=5` against a
    row at version=6, the mutation must be discarded — applying it would
    overwrite the newer task's output with the older task's wrong result."""
    from app.tasks.music_orchestrate import _apply_lyrics_result

    track = MagicMock()
    track.id = TRACK_ID
    track.lyrics_extraction_version = 6  # newer task already bumped
    prior_status = "ready"
    prior_cached = {"correct": "lyrics"}
    track.lyrics_status = prior_status
    track.lyrics_cached = prior_cached

    _apply_lyrics_result(
        track,
        {
            "status": "needs_manual_lyrics",  # the OLDER task's bad result
            "source": "whisper_only",
            "whisper_draft": {"wrong": "garbage"},
            "diagnostic": {"fallback_path": "needs_manual_lyrics"},
            "version_snapshot": 5,  # doesn't match current 6 → discard
        },
    )

    # Track state must be UNCHANGED — no mutation applied.
    assert track.lyrics_status == prior_status, "stale task overwrote status"
    assert track.lyrics_cached == prior_cached, "stale task overwrote cached lyrics"


def test_apply_lyrics_result_matching_version_applies_mutation() -> None:
    """Sanity-check the other side of the gate: when version_snapshot matches
    current, the mutation applies normally."""
    from app.tasks.music_orchestrate import _apply_lyrics_result

    track = MagicMock()
    track.id = TRACK_ID
    track.lyrics_extraction_version = 4

    _apply_lyrics_result(
        track,
        {
            "status": "ready",
            "source": "lrclib_synced+whisper",
            "output": {"lines": [{"text": "ok"}]},
            "diagnostic": {"fallback_path": "ready_synced"},
            "version_snapshot": 4,  # matches → apply
        },
    )

    assert track.lyrics_status == "ready"
    assert track.lyrics_cached == {"lines": [{"text": "ok"}]}


def test_apply_lyrics_result_skipped_bypasses_version_gate() -> None:
    """Skipped runs (no OPENAI_API_KEY) don't read a version snapshot, so
    they bypass the stale-task gate and can land their no-op mutation on
    any row state. Existing pre-2026-05-27 behavior; pinned here so a
    future refactor doesn't break it."""
    from app.tasks.music_orchestrate import _apply_lyrics_result

    track = MagicMock()
    track.lyrics_status = "pending"
    track.lyrics_extraction_version = 99

    _apply_lyrics_result(track, {"status": "skipped", "reason": "openai_api_key_missing"})

    # `skipped` left a fresh row at `pending` because there was no prior
    # successful extraction to preserve.
    assert track.lyrics_status == "pending"


# ── _run_song_sections return-shape contract ──────────────────────────────────
#
# These pin the (dict | None, str | None) tuple contract that
# analyze_music_track_task relies on to populate vs clear
# MusicTrack.section_error_detail. Drift here silently degrades the
# admin observability: a returned-None on a real failure with no error
# string puts the row back into the "no agent sections, no reason" state
# the fix exists to eliminate.


def test_run_song_sections_returns_dict_and_none_error_on_success() -> None:
    from app.tasks.music_orchestrate import _run_song_sections

    file_ref = MagicMock()
    file_ref.uri = "gs://gemini/uploaded/abc"
    file_ref.mime_type = "audio/mp4"

    fake_output = MagicMock()
    fake_output.sections = [MagicMock(rank=1)]
    fake_output.to_dict.return_value = {
        "sections": [{"rank": 1, "start_s": 30.0, "end_s": 48.0}],
        "section_version": "2026-05-22",
    }

    with (
        patch("app.agents.song_sections.SongSectionsAgent") as agent_cls,
        patch("app.agents._model_client.default_client"),
    ):
        agent_cls.return_value.run.return_value = fake_output
        sections, error = _run_song_sections(
            file_ref=file_ref,
            audio_template_output={},
            beats=[0.5, 1.0, 1.5],
            duration_s=180.0,
            track_id=TRACK_ID,
        )

    assert error is None
    assert sections is not None
    assert sections["section_version"] == "2026-05-22"


def test_run_song_sections_captures_exception_message_on_silent_fail() -> None:
    """Non-Refusal Exception must NOT propagate — it must return
    (None, str(exc)) so the caller can persist the reason to
    MusicTrack.section_error_detail. This is the bug class the fix exists
    to surface: without the error string, the admin UI shows
    "no agent sections" with no clue why."""
    from app.tasks.music_orchestrate import _run_song_sections

    file_ref = MagicMock()
    file_ref.uri = "gs://gemini/uploaded/abc"
    file_ref.mime_type = "audio/mp4"

    with (
        patch("app.agents.song_sections.SongSectionsAgent") as agent_cls,
        patch("app.agents._model_client.default_client"),
    ):
        agent_cls.return_value.run.side_effect = RuntimeError(
            "song_sections: invalid JSON — Expecting value: line 1 column 1 (char 0)"
        )
        sections, error = _run_song_sections(
            file_ref=file_ref,
            audio_template_output={},
            beats=[0.5, 1.0, 1.5],
            duration_s=180.0,
            track_id=TRACK_ID,
        )

    assert sections is None
    assert error is not None
    assert "invalid JSON" in error


def test_run_song_sections_refusal_still_propagates() -> None:
    """RefusalError means every proposed section violated hard constraints —
    the task must visibly retry/fail rather than silently degrade. The
    error-string capture must NOT swallow it."""
    from app.tasks.music_orchestrate import _run_song_sections

    file_ref = MagicMock()
    file_ref.uri = "gs://gemini/uploaded/abc"
    file_ref.mime_type = "audio/mp4"

    with (
        patch("app.agents.song_sections.SongSectionsAgent") as agent_cls,
        patch("app.agents._model_client.default_client"),
    ):
        agent_cls.return_value.run.side_effect = RefusalError(
            "song_sections: no valid sections after filter"
        )
        with pytest.raises(RefusalError):
            _run_song_sections(
                file_ref=file_ref,
                audio_template_output={},
                beats=[0.5, 1.0, 1.5],
                duration_s=180.0,
                track_id=TRACK_ID,
            )


def test_run_song_sections_returns_none_none_on_zero_duration() -> None:
    """A duration ≤ 0 track is not actionable — _run_song_sections must
    return (None, None) so the row carries NO error_detail (it's "skipped",
    not "failed"). Pinned to prevent a regression where the skip branch
    starts writing misleading "duration was zero" text to the row."""
    from app.tasks.music_orchestrate import _run_song_sections

    sections, error = _run_song_sections(
        file_ref=MagicMock(),
        audio_template_output={},
        beats=[],
        duration_s=0.0,
        track_id=TRACK_ID,
    )
    assert sections is None
    assert error is None


def test_fail_track_preserves_section_error_detail() -> None:
    """When a downstream stage marks a track failed (e.g. 0-slot guard at
    music_orchestrate.py:266), _fail_track must NOT null section_error_detail.
    A failed track keeps both signals: error_detail carries the whole-task
    reason, section_error_detail carries the song_sections-step reason.
    Pinned as documentation of intent — the analyze-start clear and the
    persistence elif are the only writers; _fail_track is intentionally
    silent on this field."""
    from app.tasks.music_orchestrate import _fail_track

    mock_track = _make_mock_track(analysis_status="analyzing")
    mock_track.section_error_detail = "song_sections: invalid JSON — prior reason"
    mock_track.best_sections = [{"rank": 1, "start_s": 30.0, "end_s": 50.0}]
    mock_track.section_version = "2026-05-22"

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = mock_track

    with patch("app.tasks.music_orchestrate._sync_session", return_value=mock_session):
        _fail_track(TRACK_ID, "downstream stage produced 0 slots")

    assert mock_track.analysis_status == "failed"
    assert mock_track.error_detail == "downstream stage produced 0 slots"
    # Existing behavior: best_sections + section_version null on hard failure.
    assert mock_track.best_sections is None
    assert mock_track.section_version is None
    # Intentional: section_error_detail is preserved so admin sees both signals
    # (whole-task reason AND song_sections-step reason) as separate context
    # fields on the failed row. Change this assertion deliberately if the
    # product wants a single canonical error field on failed tracks.
    assert mock_track.section_error_detail == "song_sections: invalid JSON — prior reason"


def test_run_gemini_audio_analysis_outer_failure_keeps_sections_error_none() -> None:
    """Pins the documented invariant: when the OUTER Gemini upload fails
    (before _run_song_sections is even attempted), sections_error MUST be
    None — not str(exc). Otherwise every Gemini transport blip would
    falsely attribute itself to the song_sections agent on
    MusicTrack.section_error_detail. The real outer error stays in
    the worker log (`gemini_audio_analysis_failed`).
    """
    from app.tasks.music_orchestrate import _run_gemini_audio_analysis

    with (
        patch(
            "app.tasks.music_orchestrate.gemini_upload_and_wait",
            side_effect=RuntimeError("synthetic upload failure"),
        ),
        patch(
            "app.tasks.music_orchestrate.generate_music_recipe",
            return_value={"slots": [], "total_duration_s": 5.0},
        ),
    ):
        recipe, labels, sections, sections_error = _run_gemini_audio_analysis(
            local_audio="/tmp/audio.m4a",
            beats=[0.5, 1.0, 1.5],
            track_config={"slot_every_n_beats": 2},
            duration_s=60.0,
            track_id=TRACK_ID,
        )

    # Beat-only fallback fired so the track can still reach `ready`.
    assert recipe is not None
    # Labels + sections can't be produced without the file_ref.
    assert labels is None
    assert sections is None
    # The load-bearing assertion: outer failure is NOT attributed to song_sections.
    assert sections_error is None


# ── analyze_music_track_task: section_error_detail persistence ────────────────


def test_analyze_music_track_persists_section_error_detail_truncated() -> None:
    """When _run_gemini_audio_analysis returns
    (recipe_cached, ai_labels, None, "some error"), analyze_music_track_task
    must write the truncated error to MusicTrack.section_error_detail.
    This is the observability that closes the "no agent sections, no reason"
    blind spot."""
    from app.tasks.music_orchestrate import MAX_ERROR_DETAIL_LEN, analyze_music_track_task

    mock_track = _make_mock_track(track_config={"slot_every_n_beats": 2})
    mock_track.section_error_detail = None
    mock_beats = [round(0.5 * i, 3) for i in range(1, 241)]
    huge_error = "song_sections: invalid JSON — " + ("x" * (MAX_ERROR_DETAIL_LEN * 2))

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
            return_value=(None, None, None, huge_error),
        ),
        patch("app.tasks.music_orchestrate.gemini_upload_and_wait", new=MagicMock()),
        patch("app.tasks.music_orchestrate.analyze_audio_template", new=MagicMock()),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    assert mock_track.analysis_status == "ready"
    assert mock_track.best_sections is None
    assert mock_track.section_version is None
    assert mock_track.section_error_detail is not None
    # Truncated — defends the row against an unbounded Gemini repr blowing
    # up the DB column. Bound is MAX_ERROR_DETAIL_LEN, mirrored from the
    # lyrics_error_detail precedent.
    assert len(mock_track.section_error_detail) <= MAX_ERROR_DETAIL_LEN
    assert mock_track.section_error_detail.startswith("song_sections: invalid JSON")


def test_analyze_music_track_clears_section_error_detail_on_successful_run() -> None:
    """A subsequent successful re-analyze must NOT leave a stale error
    on the row. Pre-cleared in analyze_music_track_task right before the
    analysis_status='analyzing' commit, AND not re-set on the success branch."""
    from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION
    from app.tasks.music_orchestrate import analyze_music_track_task

    mock_track = _make_mock_track(track_config={"slot_every_n_beats": 2})
    # Row starts with a STALE error from a prior failed run.
    mock_track.section_error_detail = "song_sections: stale prior failure"
    mock_beats = [round(0.5 * i, 3) for i in range(1, 241)]
    mock_sections_dict = {
        "sections": [
            {
                "rank": 1,
                "start_s": 30.0,
                "end_s": 50.0,
                "label": "chorus",
                "energy": "high",
                "suggested_use": "hook",
                "rationale": "peak energy chorus.",
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
        patch("app.tasks.music_orchestrate.auto_best_section", return_value=(5.0, 50.0)),
        patch(
            "app.tasks.music_orchestrate._run_gemini_audio_analysis",
            return_value=({"slots": []}, None, mock_sections_dict, None),
        ),
        patch("app.tasks.music_orchestrate.gemini_upload_and_wait", new=MagicMock()),
        patch("app.tasks.music_orchestrate.analyze_audio_template", new=MagicMock()),
        patch("tempfile.TemporaryDirectory") as mock_td,
    ):
        mock_td.return_value.__enter__ = lambda s: "/tmp/fake"
        mock_td.return_value.__exit__ = MagicMock(return_value=False)

        analyze_music_track_task(TRACK_ID)

    assert mock_track.analysis_status == "ready"
    # Sections wrote, version wrote, AND the stale error is gone — that
    # last assertion is the load-bearing one. Without the analyze-start
    # clear, the row would carry "stale prior failure" forever.
    assert mock_track.section_version == CURRENT_SECTION_VERSION
    assert mock_track.section_error_detail is None
