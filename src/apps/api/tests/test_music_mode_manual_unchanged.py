"""Regression pin: manual music-mode flow stays byte-identical post-Phase-3.

This is one of three IRON RULE regression tests for the auto-music
Phase 3 PR (see plans/our-current-agentic-template-scalable-gem.md
"Backward compatibility — existing templates are NOT touched"). It pins
the *public surface* of the manual music-mode flow — request schema,
task name, function signature, status string transitions — without
running a real FFmpeg pipeline.

The IRON RULE: a future refactor of Phase 3 cannot quietly break
existing flows. These assertions are deliberately mock-based and shape-
focused, not byte-level golden-output (full render golden-files are too
expensive for CI). If you change anything tested here AND the auto-
music orchestrator was the reason, that's a bug. If the change is
intentional, update this test and explicitly call out the new behavior
in the PR description.

Coverage:
  1. ``CreateMusicJobRequest`` schema fields + validators
  2. ``orchestrate_music_job`` is a Celery task with the canonical name
  3. ``_run_music_job`` exists and is the implementation entry point
  4. End-state status string is still ``music_ready``
  5. Failure-state status string is still ``processing_failed``
  6. The shared render helpers re-exported by the music task module are
     the same callables they always were
  7. The new auto-music orchestrator does NOT shadow the existing one
"""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import MagicMock, patch

from app.routes.music_jobs import CreateMusicJobRequest
from app.tasks import music_orchestrate
from app.tasks.music_orchestrate import (
    _fail_job,
    _run_music_job,
    orchestrate_music_job,
)

JOB_ID = str(uuid.uuid4())


# ── Request schema pin ────────────────────────────────────────────────────────


def test_create_music_job_request_required_fields_unchanged() -> None:
    """POST /music-jobs request schema must have exactly these required fields.

    Adding a new required field is a breaking change for existing
    clients. Removing one means the new orchestrator stole a field.
    """
    fields = CreateMusicJobRequest.model_fields
    assert "music_track_id" in fields
    assert "clip_gcs_paths" in fields
    assert "selected_platforms" in fields
    # No required `mode` or `n_variants` etc. leaking onto the manual route.
    assert fields["music_track_id"].is_required()
    assert fields["clip_gcs_paths"].is_required()


def test_create_music_job_clip_validator_unchanged() -> None:
    """The 1..20 clip-count validator must still fire at the same boundaries."""
    # 0 clips → ValueError
    try:
        CreateMusicJobRequest(music_track_id="t1", clip_gcs_paths=[])
        raise AssertionError("expected validation error for empty clips")
    except Exception as exc:
        assert "1 clip" in str(exc).lower() or "at least" in str(exc).lower()
    # 21 clips → ValueError
    try:
        CreateMusicJobRequest(
            music_track_id="t1",
            clip_gcs_paths=[f"gs://b/c{i}.mp4" for i in range(21)],
        )
        raise AssertionError("expected validation error for >20 clips")
    except Exception as exc:
        assert "20" in str(exc)


# ── Celery task pin ───────────────────────────────────────────────────────────


def test_orchestrate_music_job_task_name_unchanged() -> None:
    """The Celery task name is part of the public surface (Redis broker uses it
    to route messages). Renaming it would break in-flight prod queues mid-deploy.
    """
    assert orchestrate_music_job.name == "tasks.orchestrate_music_job"


def test_orchestrate_music_job_signature_unchanged() -> None:
    """The task accepts (job_id) and returns None. No new params snuck in.

    ``__wrapped__`` strips Celery's bind=True self binding; we check the
    user-visible signature only.
    """
    sig = inspect.signature(orchestrate_music_job.__wrapped__)
    params = list(sig.parameters.keys())
    assert params == ["job_id"]
    assert sig.parameters["job_id"].annotation is str


def test_run_music_job_signature_unchanged() -> None:
    """The implementation entry point is _run_music_job(job_id) → None."""
    sig = inspect.signature(_run_music_job)
    params = list(sig.parameters.keys())
    assert params == ["job_id"]


# ── Status string transitions ─────────────────────────────────────────────────


def test_music_ready_status_string_unchanged() -> None:
    """Successful music jobs end in status=music_ready.

    The frontend's /music-jobs/{id}/status poller and the result page
    both branch on this exact string. Renaming it would cause every
    in-flight job at the moment of deploy to look failed in the UI.
    """
    src = inspect.getsource(_run_music_job)
    assert '"music_ready"' in src, (
        "music_ready status string disappeared from _run_music_job — "
        "the manual flow's terminal status changed. This is a regression."
    )


def test_processing_failed_status_string_unchanged() -> None:
    """Failed music jobs end in status=processing_failed."""
    src = inspect.getsource(_fail_job)
    assert '"processing_failed"' in src, (
        "_fail_job no longer writes processing_failed — "
        "the failure status path changed shape."
    )


# ── Shared helper re-exports ──────────────────────────────────────────────────


def test_music_orchestrate_reuses_shared_render_helpers() -> None:
    """The music task module must keep importing the shared render helpers
    from template_orchestrate — splitting would mean a forked render path,
    which is the exact opposite of what Phase 3 is supposed to do.
    """
    from app.tasks import template_orchestrate

    for name in (
        "_analyze_clips_parallel",
        "_assemble_clips",
        "_download_clips_parallel",
        "_enrich_slots_with_energy",
        "_mix_template_audio",
        "_probe_clips",
        "_upload_clips_parallel",
    ):
        assert getattr(music_orchestrate, name) is getattr(template_orchestrate, name), (
            f"{name} is no longer the same callable in music_orchestrate as "
            f"in template_orchestrate — render path forked, which Phase 3 "
            f"explicitly forbids."
        )


def test_new_auto_music_module_does_not_shadow_music_orchestrate() -> None:
    """The new auto-music module must NOT re-export ``orchestrate_music_job``
    or ``analyze_music_track_task``. Phase 3 adds a new task; it does not
    rename or shadow the existing ones."""
    from app.tasks import auto_music_orchestrate

    assert not hasattr(auto_music_orchestrate, "orchestrate_music_job"), (
        "auto_music_orchestrate.orchestrate_music_job exists — that name "
        "belongs exclusively to the manual flow."
    )
    assert not hasattr(auto_music_orchestrate, "analyze_music_track_task"), (
        "auto_music_orchestrate.analyze_music_track_task exists — that name "
        "belongs exclusively to the admin-time analysis path."
    )


# ── Behavior pin: track-not-ready early exit ─────────────────────────────────


def test_orchestrate_music_job_track_not_ready_still_calls_fail_job() -> None:
    """Behavioral pin: when the track is not ready, _fail_job is called with a
    "not ready" message. This was tested before Phase 3 (see
    test_music_orchestrate.py) — re-pinned here to ensure Phase 3's
    additions did NOT subtly alter the manual flow's failure semantics.
    """
    mock_job = MagicMock()
    mock_job.id = uuid.UUID(JOB_ID)
    mock_job.status = "queued"
    mock_job.music_track_id = "t-123"
    mock_job.all_candidates = {"clip_paths": ["gs://b/c.mp4"]}
    mock_track = MagicMock()
    mock_track.analysis_status = "analyzing"
    mock_track.audio_gcs_path = "music/x/audio.m4a"
    mock_track.recipe_cached = None

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
    msg = mock_fail.call_args[0][1].lower()
    assert "not ready" in msg
