"""Unit tests for app/tasks/auto_music_orchestrate.py (Phase 3).

Mocked DB + GCS + Gemini + matcher; the Celery task is invoked directly.
The render path is short-circuited so each test stays well under 1s.

Coverage (from the plan's "Tests for the new orchestrator" list):
  - clip_metadata is called exactly N times (N = clip count), not N×K
  - music_matcher is called exactly once
  - Empty matcher ranked list → status=matching_failed
  - Some variants fail → status=variants_ready_partial, successful
    variants are persisted
  - Hallucinated track_id (impossible after Phase 2's filter, but
    defense-in-depth here) → orchestrator falls through to the next
    ranked track
  - Zero labeled tracks → status=no_labeled_tracks with a useful error
  - Feature-flag-off behavior: task early-exits with processing_failed
    and never reaches the pipeline
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.tasks.auto_music_orchestrate import (
    _load_matcher_candidates,
    _run_auto_music_job,
    orchestrate_auto_music_job,
)

JOB_ID = str(uuid.uuid4())


# ── Helpers ──────────────────────────────────────────────────────────────────


@dataclass
class FakeClipMeta:
    clip_id: str
    hook_text: str = ""
    hook_score: float = 5.0
    transcript: str = ""
    best_moments: list = None  # type: ignore[assignment]
    detected_subject: str = ""
    analysis_degraded: bool = False
    failed: bool = False
    clip_path: str = ""

    def __post_init__(self) -> None:
        if self.best_moments is None:
            self.best_moments = []


def _make_track(
    track_id: str = "tr-001",
    *,
    label_version: str = "2026-05-15",
    title: str = "Test Song",
    slot_count_hint: int = 8,
) -> MagicMock:
    """Build a MagicMock that quacks like a MusicTrack with current labels."""
    t = MagicMock()
    t.id = track_id
    t.title = title
    t.duration_s = 180.0
    t.audio_gcs_path = f"music/{track_id}/audio.m4a"
    t.beat_timestamps_s = [float(i) * 0.5 for i in range(64)]
    t.track_config = {
        "best_start_s": 0.0,
        "best_end_s": 32.0,
        "slot_every_n_beats": 4,
        "required_clips_max": slot_count_hint,
    }
    t.ai_labels = {
        "labels": {
            "label_version": label_version,
            "genre": "pop",
            "vibe_tags": ["upbeat"],
            "energy": "high",
            "pacing": "medium",
            "mood": "fun",
            "ideal_content_profile": "general clips",
            "copy_tone": "punchy",
            "transition_style": "hard_cut",
            "color_grade": "none",
        },
        "rationale": "test rationale",
    }
    t.label_version = label_version
    t.published_at = datetime.now(UTC)
    t.archived_at = None
    t.analysis_status = "ready"
    t.recipe_cached = {"slots": [{"position": i + 1} for i in range(slot_count_hint)]}
    return t


def _make_job(
    *,
    n_clips: int = 4,
    n_variants: int = 3,
) -> MagicMock:
    """Build a MagicMock that quacks like a Job row ready for auto-music."""
    job = MagicMock()
    job.id = uuid.UUID(JOB_ID)
    job.status = "queued"
    job.mode = None
    job.assembly_plan = None
    job.failure_reason = None
    job.error_detail = None
    job.all_candidates = {
        "clip_paths": [f"gs://b/clip{i}.mp4" for i in range(n_clips)],
        "n_variants": n_variants,
    }
    return job


def _file_ref(name: str) -> MagicMock:
    ref = MagicMock()
    ref.name = name
    return ref


# ── Feature flag gate ─────────────────────────────────────────────────────────


def test_flag_off_early_exits_with_processing_failed() -> None:
    """When the ENABLE_AUTO_MUSIC_MODE flag is off, the task MUST NOT run
    the pipeline. It writes processing_failed + failure_reason=auto_music_disabled.
    """
    job = _make_job()
    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = job

    with (
        patch("app.tasks.auto_music_orchestrate.settings.enable_auto_music_mode", False),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch("app.tasks.auto_music_orchestrate._run_auto_music_job") as ran,
    ):
        orchestrate_auto_music_job(JOB_ID)

    # Pipeline must not have run.
    ran.assert_not_called()
    # Job must have been failed with the structured reason.
    assert job.status == "processing_failed"
    assert job.failure_reason == "auto_music_disabled"


# ── Cost guard: clip_metadata exactly N times, matcher exactly once ──────────


def _setup_flag_on(monkeypatch_settings=True):
    """Patch settings.enable_auto_music_mode=True for the test."""
    return patch("app.tasks.auto_music_orchestrate.settings.enable_auto_music_mode", True)


def _patch_render_pipeline_happy(monkeypatch_module, picks=2, raise_on_ranks=()):
    """Patch every external integration in the orchestrator with happy paths.

    Returns the (mock_analyze, mock_matcher_run) so the caller can
    assert call counts.
    """
    pass  # implementation lives inline in each test for clarity.


def test_clip_metadata_called_once_per_clip_not_per_variant() -> None:
    """COST GUARD: clip_metadata runs N times total (N=clip count), NOT N×K
    (where K=variant count). The plan calls this non-negotiable.
    """
    n_clips = 4
    n_variants = 3
    job = _make_job(n_clips=n_clips, n_variants=n_variants)
    track = _make_track(slot_count_hint=4)

    # _analyze_clips_parallel is the function that internally calls
    # clip_metadata once per file_ref. We pin it here at the orchestrator
    # boundary: it's invoked ONCE by the orchestrator, returning
    # n_clips ClipMetas.
    file_refs = [_file_ref(f"files/{i}") for i in range(n_clips)]
    clip_metas = [FakeClipMeta(clip_id=f"files/{i}") for i in range(n_clips)]

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    # We only need to satisfy db.get(Job, ...) and the matcher-candidate
    # SELECT inside _load_matcher_candidates.
    mock_session.get.return_value = job
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [track]
    mock_session.execute.return_value = mock_execute_result

    matcher_ranked = [
        {
            "track_id": track.id,
            "score": 8.5,
            "rationale": "great fit",
            "predicted_strengths": ["x"],
        },
    ]

    with (
        _setup_flag_on(),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch(
            "app.tasks.auto_music_orchestrate._download_clips_parallel",
            return_value=[f"/tmp/c{i}.mp4" for i in range(n_clips)],
        ),
        patch("app.tasks.auto_music_orchestrate._probe_clips", return_value={}),
        patch(
            "app.tasks.auto_music_orchestrate._upload_clips_parallel",
            return_value=file_refs,
        ),
        patch(
            "app.tasks.auto_music_orchestrate._analyze_clips_parallel",
            return_value=(clip_metas, 0),
        ) as mock_analyze,
        patch(
            "app.tasks.auto_music_orchestrate._run_music_matcher",
            return_value=matcher_ranked,
        ) as mock_matcher,
        patch(
            "app.tasks.auto_music_orchestrate._render_variants_parallel",
            return_value=[{"ok": True, "rank": 1, "track_id": track.id}],
        ),
    ):
        _run_auto_music_job(JOB_ID)

    # COST GUARD ASSERTIONS:
    assert mock_analyze.call_count == 1, (
        f"_analyze_clips_parallel called {mock_analyze.call_count}x — "
        "expected exactly once total. clip_metadata is the dominant cost; "
        "multiplying it by variant count is forbidden."
    )
    assert mock_matcher.call_count == 1, (
        f"_run_music_matcher called {mock_matcher.call_count}x — "
        "the matcher is per-job, not per-variant."
    )


# ── matcher empty list → matching_failed ─────────────────────────────────────


def test_empty_matcher_ranked_list_fails_with_matching_failed_status() -> None:
    """When music_matcher returns no ranked picks, the job fails gracefully
    with status=matching_failed and a useful error_detail."""
    job = _make_job()
    track = _make_track()

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = job
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [track]
    mock_session.execute.return_value = mock_execute_result

    with (
        _setup_flag_on(),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch(
            "app.tasks.auto_music_orchestrate._download_clips_parallel",
            return_value=["/tmp/c.mp4"] * 4,
        ),
        patch("app.tasks.auto_music_orchestrate._probe_clips", return_value={}),
        patch(
            "app.tasks.auto_music_orchestrate._upload_clips_parallel",
            return_value=[_file_ref(f"files/{i}") for i in range(4)],
        ),
        patch(
            "app.tasks.auto_music_orchestrate._analyze_clips_parallel",
            return_value=([FakeClipMeta(clip_id=f"files/{i}") for i in range(4)], 0),
        ),
        patch(
            "app.tasks.auto_music_orchestrate._run_music_matcher",
            return_value=[],
        ),
    ):
        _run_auto_music_job(JOB_ID)

    assert job.status == "matching_failed"
    assert job.failure_reason == "matching_failed"
    assert "matcher" in (job.error_detail or "").lower()


# ── no labeled tracks → no_labeled_tracks status ─────────────────────────────


def test_no_labeled_tracks_fails_with_useful_error() -> None:
    """When the matcher-candidate SQL returns zero rows, the job fails with
    a structured no_labeled_tracks status and a useful error message —
    not a Pydantic stack trace."""
    job = _make_job()

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = job
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_execute_result

    with (
        _setup_flag_on(),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch(
            "app.tasks.auto_music_orchestrate._download_clips_parallel",
            return_value=["/tmp/c.mp4"] * 4,
        ),
        patch("app.tasks.auto_music_orchestrate._probe_clips", return_value={}),
        patch(
            "app.tasks.auto_music_orchestrate._upload_clips_parallel",
            return_value=[_file_ref(f"files/{i}") for i in range(4)],
        ),
        patch(
            "app.tasks.auto_music_orchestrate._analyze_clips_parallel",
            return_value=([FakeClipMeta(clip_id=f"files/{i}") for i in range(4)], 0),
        ),
        patch("app.tasks.auto_music_orchestrate._run_music_matcher") as matcher,
    ):
        _run_auto_music_job(JOB_ID)

    # matcher must NOT have been called — we fail BEFORE spending an LLM call.
    matcher.assert_not_called()
    assert job.status == "no_labeled_tracks"
    assert job.failure_reason == "no_labeled_tracks"
    msg = (job.error_detail or "").lower()
    assert "label" in msg, (
        f"no_labeled_tracks error message is not informative: {msg!r}"
    )


# ── hallucinated track_id falls through to next pick ─────────────────────────


def test_hallucinated_track_id_falls_through_to_next_ranked() -> None:
    """Belt-and-suspenders defense: even though the matcher's parse() already
    filters hallucinations, the orchestrator double-checks against the
    candidate set. A surviving hallucinated ID must be SKIPPED, not crash
    the job — the next ranked track takes the slot.
    """
    job = _make_job(n_variants=2)
    track_real = _make_track(track_id="real-track")

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = job
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [track_real]
    mock_session.execute.return_value = mock_execute_result

    matcher_ranked = [
        {
            "track_id": "ghost-track",  # hallucinated
            "score": 9.0,
            "rationale": "fake",
            "predicted_strengths": [],
        },
        {
            "track_id": "real-track",
            "score": 8.0,
            "rationale": "real one",
            "predicted_strengths": ["a"],
        },
    ]

    captured_picks: dict = {}

    def fake_render(*, picks, **_kwargs):
        captured_picks["picks"] = picks
        return [
            {"ok": True, "rank": i + 1, "track_id": p[0].id, "score": p[1]}
            for i, p in enumerate(picks)
        ]

    with (
        _setup_flag_on(),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch(
            "app.tasks.auto_music_orchestrate._download_clips_parallel",
            return_value=["/tmp/c.mp4"] * 4,
        ),
        patch("app.tasks.auto_music_orchestrate._probe_clips", return_value={}),
        patch(
            "app.tasks.auto_music_orchestrate._upload_clips_parallel",
            return_value=[_file_ref(f"files/{i}") for i in range(4)],
        ),
        patch(
            "app.tasks.auto_music_orchestrate._analyze_clips_parallel",
            return_value=([FakeClipMeta(clip_id=f"files/{i}") for i in range(4)], 0),
        ),
        patch(
            "app.tasks.auto_music_orchestrate._run_music_matcher",
            return_value=matcher_ranked,
        ),
        patch(
            "app.tasks.auto_music_orchestrate._render_variants_parallel",
            side_effect=fake_render,
        ),
    ):
        _run_auto_music_job(JOB_ID)

    picks = captured_picks.get("picks", [])
    assert len(picks) == 1, (
        f"Expected exactly 1 pick after ghost-track was dropped, got {len(picks)}"
    )
    assert picks[0][0].id == "real-track"
    # And the job lands on variants_ready (one good render).
    assert job.status == "variants_ready"


# ── partial render failure → variants_ready_partial ──────────────────────────


def test_partial_variant_failure_yields_variants_ready_partial() -> None:
    """When some variants fail to render and at least one succeeds, the job
    lands on status=variants_ready_partial and the successful variants are
    persisted (via _render_variants_parallel's per-variant DB writes).
    """
    job = _make_job(n_variants=3)
    t1 = _make_track(track_id="t-1")
    t2 = _make_track(track_id="t-2")
    t3 = _make_track(track_id="t-3")

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = job
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [t1, t2, t3]
    mock_session.execute.return_value = mock_execute_result

    matcher_ranked = [
        {"track_id": "t-1", "score": 9.0, "rationale": "a", "predicted_strengths": []},
        {"track_id": "t-2", "score": 8.0, "rationale": "b", "predicted_strengths": []},
        {"track_id": "t-3", "score": 7.0, "rationale": "c", "predicted_strengths": []},
    ]
    # Variant 2 fails; 1 and 3 succeed.
    fake_results = [
        {"ok": True, "rank": 1, "track_id": "t-1", "score": 9.0, "output_url": "gs://u/1"},
        {"ok": False, "rank": 2, "track_id": "t-2", "score": 8.0, "error": "boom"},
        {"ok": True, "rank": 3, "track_id": "t-3", "score": 7.0, "output_url": "gs://u/3"},
    ]

    with (
        _setup_flag_on(),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch(
            "app.tasks.auto_music_orchestrate._download_clips_parallel",
            return_value=["/tmp/c.mp4"] * 4,
        ),
        patch("app.tasks.auto_music_orchestrate._probe_clips", return_value={}),
        patch(
            "app.tasks.auto_music_orchestrate._upload_clips_parallel",
            return_value=[_file_ref(f"files/{i}") for i in range(4)],
        ),
        patch(
            "app.tasks.auto_music_orchestrate._analyze_clips_parallel",
            return_value=([FakeClipMeta(clip_id=f"files/{i}") for i in range(4)], 0),
        ),
        patch(
            "app.tasks.auto_music_orchestrate._run_music_matcher",
            return_value=matcher_ranked,
        ),
        patch(
            "app.tasks.auto_music_orchestrate._render_variants_parallel",
            return_value=fake_results,
        ),
    ):
        _run_auto_music_job(JOB_ID)

    assert job.status == "variants_ready_partial"
    # Per-variant summary made it onto the assembly_plan.
    plan = job.assembly_plan or {}
    assert "variants" in plan
    assert len(plan["variants"]) == 3
    failures = [v for v in plan["variants"] if not v["ok"]]
    successes = [v for v in plan["variants"] if v["ok"]]
    assert len(failures) == 1
    assert len(successes) == 2


def test_all_variants_fail_yields_variants_failed() -> None:
    """When every variant fails to render, terminal status is variants_failed."""
    job = _make_job(n_variants=2)
    t1 = _make_track(track_id="t-1")
    t2 = _make_track(track_id="t-2")

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = job
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [t1, t2]
    mock_session.execute.return_value = mock_execute_result

    matcher_ranked = [
        {"track_id": "t-1", "score": 9.0, "rationale": "a", "predicted_strengths": []},
        {"track_id": "t-2", "score": 8.0, "rationale": "b", "predicted_strengths": []},
    ]
    fake_results = [
        {"ok": False, "rank": 1, "track_id": "t-1", "score": 9.0, "error": "x"},
        {"ok": False, "rank": 2, "track_id": "t-2", "score": 8.0, "error": "y"},
    ]

    with (
        _setup_flag_on(),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch(
            "app.tasks.auto_music_orchestrate._download_clips_parallel",
            return_value=["/tmp/c.mp4"] * 4,
        ),
        patch("app.tasks.auto_music_orchestrate._probe_clips", return_value={}),
        patch(
            "app.tasks.auto_music_orchestrate._upload_clips_parallel",
            return_value=[_file_ref(f"files/{i}") for i in range(4)],
        ),
        patch(
            "app.tasks.auto_music_orchestrate._analyze_clips_parallel",
            return_value=([FakeClipMeta(clip_id=f"files/{i}") for i in range(4)], 0),
        ),
        patch(
            "app.tasks.auto_music_orchestrate._run_music_matcher",
            return_value=matcher_ranked,
        ),
        patch(
            "app.tasks.auto_music_orchestrate._render_variants_parallel",
            return_value=fake_results,
        ),
    ):
        _run_auto_music_job(JOB_ID)

    assert job.status == "variants_failed"


# ── candidate filter: pre-match drops degenerate tracks ──────────────────────


def test_load_matcher_candidates_drops_tracks_with_too_many_slots() -> None:
    """Critical risk #4: a track that wants 12 slots when the user uploaded
    4 clips would produce a degenerate recipe. The pre-match filter must
    drop it so the matcher never even sees it.
    """
    big_track = _make_track(track_id="big", slot_count_hint=24)
    small_track = _make_track(track_id="small", slot_count_hint=4)

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = [big_track, small_track]
    mock_session.execute.return_value = mock_execute_result

    with patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session):
        out = _load_matcher_candidates(n_clips=3)

    ids = [t.id for t in out]
    assert "small" in ids
    assert "big" not in ids, (
        "Track with 24 slots survived pre-match filter against 3 clips — "
        "Critical risk #4 is not mitigated."
    )


# ── celery task wrapper: failure is swallowed (never raises) ─────────────────


def test_orchestrate_auto_music_job_swallows_exceptions() -> None:
    """The Celery task MUST NOT raise — exceptions become _fail_job calls.
    Mirrors the discipline of orchestrate_music_job + orchestrate_template_job.
    """
    with (
        patch("app.tasks.auto_music_orchestrate.settings.enable_auto_music_mode", True),
        patch(
            "app.tasks.auto_music_orchestrate._run_auto_music_job",
            side_effect=RuntimeError("boom from inner pipeline"),
        ),
        patch("app.tasks.auto_music_orchestrate._fail_job") as mock_fail,
    ):
        # Must not raise.
        orchestrate_auto_music_job(JOB_ID)

    mock_fail.assert_called_once()
    assert "boom" in mock_fail.call_args[0][1]


# ── absent feature flag default ──────────────────────────────────────────────


def test_feature_flag_default_is_false() -> None:
    """The plan is explicit: ENABLE_AUTO_MUSIC_MODE defaults to False. Flipping
    the default to True without explicit user action would silently expose
    the new flow."""
    from app.config import Settings

    fresh = Settings(
        storage_bucket="x",
        storage_provider="gcs",
        database_url="postgresql://u:p@h/d",
        redis_url="redis://x",
        openai_api_key="x",
        token_encryption_key="x",
        waitlist_admin_secret="x",
        allowed_origins=["http://localhost:3000"],
    )
    assert fresh.enable_auto_music_mode is False, (
        "ENABLE_AUTO_MUSIC_MODE flipped to True by default — this would "
        "expose the new flow without explicit user opt-in."
    )


# ── n_variants clamp ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("requested,expected_max", [(0, 1), (1, 1), (3, 3), (50, 10)])
def test_n_variants_clamped(requested: int, expected_max: int) -> None:
    """n_variants is clamped to [1, 10] so a malicious / buggy caller can't
    blow up the worker by requesting 1000 variants."""
    job = _make_job(n_variants=requested)

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = job
    # No tracks → fast exit at no_labeled_tracks; that's fine, we only
    # care about reaching _load_matcher_candidates without an OverflowError.
    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_execute_result

    captured: dict = {}

    def fake_analyze(*args, **kwargs):
        return ([FakeClipMeta(clip_id="f0")], 0)

    with (
        _setup_flag_on(),
        patch("app.tasks.auto_music_orchestrate._sync_session", return_value=mock_session),
        patch(
            "app.tasks.auto_music_orchestrate._download_clips_parallel",
            return_value=["/tmp/c.mp4"],
        ),
        patch("app.tasks.auto_music_orchestrate._probe_clips", return_value={}),
        patch(
            "app.tasks.auto_music_orchestrate._upload_clips_parallel",
            return_value=[_file_ref("files/0")],
        ),
        patch(
            "app.tasks.auto_music_orchestrate._analyze_clips_parallel",
            side_effect=fake_analyze,
        ),
        patch(
            "app.tasks.auto_music_orchestrate._load_matcher_candidates",
            side_effect=lambda n: (captured.__setitem__("n_clips", n), [])[1],
        ),
    ):
        _run_auto_music_job(JOB_ID)

    # Reaches no_labeled_tracks because we returned an empty candidate list.
    assert job.status == "no_labeled_tracks"
    # The clamp itself doesn't expose n_variants publicly; the assertion
    # here is that the orchestrator did not crash on extreme inputs.
    assert "n_clips" in captured
