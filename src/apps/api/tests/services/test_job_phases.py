"""Unit tests for app/services/job_phases.py.

The phase service is best-effort — every helper swallows exceptions. The
contract we lock in here:

  1. `mark_started` stamps started_at + current_phase exactly once
     (re-invocation never moves started_at backwards).
  2. `record_phase` appends a NEW list to phase_log (so SQLAlchemy detects
     the mutation) and updates current_phase to `next_phase` when given.
  3. Bad job_ids (None, non-UUID string, missing job) are no-ops.
  4. DB errors are swallowed — never raised to the caller.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.services import job_phases


def _mock_job() -> MagicMock:
    """Stand-in Job ORM object — only the attributes the service reads/writes."""
    job = MagicMock()
    job.started_at = None
    job.finished_at = None
    job.current_phase = None
    job.phase_log = []
    return job


def _patched_session(job: MagicMock | None) -> MagicMock:
    """Construct a context-manager mock that yields a session whose `.get(...)`
    returns the given job (or None)."""
    session = MagicMock()
    session.get.return_value = job
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_mark_started_stamps_started_at_and_phase() -> None:
    job = _mock_job()
    with patch.object(job_phases, "sync_session", return_value=_patched_session(job)):
        job_phases.mark_started(uuid.uuid4())
    assert job.started_at is not None
    assert job.current_phase == job_phases.PHASE_DOWNLOAD_CLIPS


def test_mark_started_idempotent_does_not_reset_started_at() -> None:
    """Calling mark_started a second time must keep the earlier started_at.
    The worker may legitimately call this twice (rare retry), and started_at
    is the user-facing wall-time anchor — must not move backwards."""
    job = _mock_job()
    first_stamp = MagicMock()
    job.started_at = first_stamp
    with patch.object(job_phases, "sync_session", return_value=_patched_session(job)):
        job_phases.mark_started(uuid.uuid4())
    assert job.started_at is first_stamp


def test_record_phase_appends_entry_with_new_list() -> None:
    """phase_log must be REASSIGNED (not .append()d in place) so SQLAlchemy
    detects the JSONB mutation. This test fails if someone refactors to
    in-place .append()."""
    job = _mock_job()
    original_list = job.phase_log
    with patch.object(job_phases, "sync_session", return_value=_patched_session(job)):
        job_phases.record_phase(
            uuid.uuid4(),
            job_phases.PHASE_DOWNLOAD_CLIPS,
            elapsed_ms=1234,
            next_phase=job_phases.PHASE_ANALYZE_CLIPS,
        )
    assert job.phase_log is not original_list
    assert len(job.phase_log) == 1
    entry = job.phase_log[0]
    assert entry["name"] == "download_clips"
    assert entry["elapsed_ms"] == 1234
    assert job.current_phase == "analyze_clips"


def test_record_phase_preserves_existing_entries() -> None:
    job = _mock_job()
    job.phase_log = [{"name": "download_clips", "elapsed_ms": 100}]
    with patch.object(job_phases, "sync_session", return_value=_patched_session(job)):
        job_phases.record_phase(
            uuid.uuid4(),
            job_phases.PHASE_ANALYZE_CLIPS,
            elapsed_ms=200,
        )
    assert len(job.phase_log) == 2
    assert [e["name"] for e in job.phase_log] == ["download_clips", "analyze_clips"]


def test_record_phase_without_next_phase_leaves_current_phase_unchanged() -> None:
    """The final phase (finalize) has no `next_phase` — mark_finished clears
    current_phase, not record_phase."""
    job = _mock_job()
    job.current_phase = "upload"
    with patch.object(job_phases, "sync_session", return_value=_patched_session(job)):
        job_phases.record_phase(uuid.uuid4(), job_phases.PHASE_FINALIZE)
    # current_phase stays at "upload"; mark_finished is the one that clears it.
    assert job.current_phase == "upload"


def test_mark_finished_clears_current_phase_and_stamps_finished_at() -> None:
    job = _mock_job()
    job.current_phase = "upload"
    with patch.object(job_phases, "sync_session", return_value=_patched_session(job)):
        job_phases.mark_finished(uuid.uuid4())
    assert job.current_phase is None
    assert job.finished_at is not None


def test_invalid_job_id_is_silent_noop() -> None:
    """Stale Celery enqueue with job_id=None or a non-UUID string must not
    crash — defensive guard mirrors the orchestrator's own uuid validation."""
    session_mock = MagicMock()
    with patch.object(job_phases, "sync_session", return_value=session_mock):
        job_phases.mark_started(None)  # type: ignore[arg-type]
        job_phases.record_phase("not-a-uuid", "download_clips")
        job_phases.mark_finished(None)  # type: ignore[arg-type]
    # Session must never even be opened for invalid inputs.
    session_mock.__enter__.assert_not_called()


def test_missing_job_is_silent_noop() -> None:
    """If the row was deleted between job-create and worker pickup, the
    helpers must not blow up."""
    with patch.object(job_phases, "sync_session", return_value=_patched_session(None)):
        job_phases.mark_started(uuid.uuid4())
        job_phases.record_phase(uuid.uuid4(), "download_clips")
        job_phases.mark_finished(uuid.uuid4())
    # No assertion needed — the test passes if nothing raised.


def test_db_error_is_swallowed_not_raised() -> None:
    """A transient DB blip must not fail the user's pipeline run."""
    boom = MagicMock(side_effect=RuntimeError("connection refused"))
    with patch.object(job_phases, "sync_session", boom):
        # Should not raise.
        job_phases.record_phase(uuid.uuid4(), "assemble", elapsed_ms=500)


def test_phase_timer_records_on_clean_exit() -> None:
    job_id = uuid.uuid4()
    with patch.object(job_phases, "record_phase") as mock_record:
        with job_phases.PhaseTimer(job_id, "assemble", next_phase="upload"):
            pass
    mock_record.assert_called_once()
    kwargs = mock_record.call_args.kwargs
    assert kwargs["next_phase"] == "upload"
    assert kwargs["elapsed_ms"] >= 0


def test_phase_timer_skips_record_on_exception() -> None:
    """If the wrapped block raises, the phase did NOT complete — don't lie
    in phase_log. The outer failure handler will clear current_phase."""
    with patch.object(job_phases, "record_phase") as mock_record:
        try:
            with job_phases.PhaseTimer(uuid.uuid4(), "assemble"):
                raise ValueError("boom")
        except ValueError:
            pass
    mock_record.assert_not_called()
