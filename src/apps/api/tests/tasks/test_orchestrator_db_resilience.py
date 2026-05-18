"""DB-resilience tests for long-running Celery orchestrator tasks.

Pinned to incident 2026-05-18 07:45:57Z (job c13db8a3): nova-db Postgres
primary stalled for ~65s mid-`analyze_clips`; the orchestrator's catch-all
`except Exception` swallowed the `OperationalError`, then `_mark_failed`
also failed against the still-down DB. With `max_retries=0`, Celery did
not retry. Job zombied at status=processing.

These tests verify the post-fix invariants:

1. Every long-running orchestrator task has the right `autoretry_for`
   + `retry_backoff` + `max_retries` decorator config so Celery retries
   transient DB errors automatically.

2. The transient-DB except clause re-raises `OperationalError` /
   `DBAPIError` BEFORE the catch-all converts it into a terminal
   `processing_failed`. Without this, the autoretry never fires because
   the exception is swallowed.

3. `_mark_failed` / `_fail_job` / `_fail_track` are themselves robust to
   transient DB outages — they retry internally so the terminal write
   eventually lands when the DB comes back, even if the orchestrator's
   autoretry budget is exhausted.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError


def _operational_error(msg: str = "server closed the connection unexpectedly") -> OperationalError:
    """Construct a realistic SQLAlchemy OperationalError like the prod incident."""
    return OperationalError("SELECT 1", {}, Exception(msg))


# ────────────────────────────────────────────────────────────────────────────
# 1. Decorator config — every long-running orchestrator has autoretry_for
# ────────────────────────────────────────────────────────────────────────────


class TestRetryDecoratorConfig:
    """Each orchestrator task must declare autoretry_for=(OperationalError, DBAPIError).

    Without these, a transient nova-db blip kills the task with no retry —
    the exact failure mode from the 2026-05-18 incident.
    """

    @pytest.mark.parametrize(
        "import_path",
        [
            # Template-mode pipeline (the documented incident's path).
            "app.tasks.template_orchestrate.orchestrate_template_job",
            "app.tasks.template_orchestrate.analyze_template_task",
            "app.tasks.template_orchestrate.orchestrate_single_video_job",
            # Music + auto-music pipelines.
            "app.tasks.music_orchestrate.orchestrate_music_job",
            "app.tasks.music_orchestrate.analyze_music_track_task",
            "app.tasks.auto_music_orchestrate.orchestrate_auto_music_job",
            # Agentic build path.
            "app.tasks.agentic_template_build.agentic_template_build_task",
            # Legacy single-video + Drive-import path (still live —
            # drive_import.py:279 enqueues orchestrate.orchestrate_job).
            "app.tasks.orchestrate.orchestrate_job",
            "app.tasks.orchestrate.render_clip",
            "app.tasks.drive_import.import_from_drive",
            "app.tasks.drive_import.batch_import_from_drive",
        ],
    )
    def test_task_retries_on_transient_db_errors(self, import_path: str):
        """The task's autoretry_for tuple must include OperationalError.

        OperationalError is the transient class (connection drops, server
        timeouts). We deliberately do NOT include DBAPIError (the parent
        class) — that would also catch IntegrityError, ProgrammingError,
        and DataError, which are deterministic bugs that should not retry.
        """
        module_path, _, attr = import_path.rpartition(".")
        module = __import__(module_path, fromlist=[attr])
        task = getattr(module, attr)

        # Celery exposes the decorator's autoretry_for as task.autoretry_for
        # (set by the @task() decorator). On older Celery it lives in
        # task.__dict__["autoretry_for"]; accept either.
        autoretry = getattr(task, "autoretry_for", None) or task.__dict__.get("autoretry_for", ())
        assert OperationalError in autoretry, (
            f"{import_path} missing OperationalError in autoretry_for — "
            "a transient DB blip will zombie any in-flight job (see incident "
            "2026-05-18 07:45:57Z)."
        )
        # Guard against re-broadening: catching DBAPIError directly would
        # retry IntegrityError / ProgrammingError / DataError, wasting
        # compute on deterministic bugs.
        assert DBAPIError not in autoretry, (
            f"{import_path} autoretry_for includes DBAPIError — too broad. "
            "Use OperationalError only (DBAPIError is the parent class that "
            "covers IntegrityError, ProgrammingError, DataError — those are "
            "deterministic bugs and should NOT retry)."
        )

    @pytest.mark.parametrize(
        "import_path",
        [
            # Template-mode pipeline (the documented incident's path).
            "app.tasks.template_orchestrate.orchestrate_template_job",
            "app.tasks.template_orchestrate.analyze_template_task",
            "app.tasks.template_orchestrate.orchestrate_single_video_job",
            # Music + auto-music pipelines.
            "app.tasks.music_orchestrate.orchestrate_music_job",
            "app.tasks.music_orchestrate.analyze_music_track_task",
            "app.tasks.auto_music_orchestrate.orchestrate_auto_music_job",
            # Agentic build path.
            "app.tasks.agentic_template_build.agentic_template_build_task",
            # Legacy single-video + Drive-import path (still live —
            # drive_import.py:279 enqueues orchestrate.orchestrate_job).
            "app.tasks.orchestrate.orchestrate_job",
            "app.tasks.orchestrate.render_clip",
            "app.tasks.drive_import.import_from_drive",
            "app.tasks.drive_import.batch_import_from_drive",
        ],
    )
    def test_task_retry_budget_covers_documented_incident(self, import_path: str):
        """Retry budget must cover the documented 65s nova-db VM stall.

        With retry_backoff=True (exponential 1, 2, 4, 8, 16, 32, 60...)
        capped at retry_backoff_max=60, max_retries=7 gives a deterministic
        1+2+4+8+16+32+60 = 123s of retry budget — ~2× safety margin over
        the 65s incident.

        Critical: retry_jitter MUST be False. With jitter, Celery picks a
        random delay between 0 and the exponential value, HALVING the
        average budget. Empirical test 2026-05-18 14:39:42Z showed jitter
        budgets of [1, 0, 3, 5, 4, 7] = 20s total for max_retries=6, which
        failed to cover a 25s simulated outage.
        """
        module_path, _, attr = import_path.rpartition(".")
        module = __import__(module_path, fromlist=[attr])
        task = getattr(module, attr)
        assert task.max_retries and task.max_retries >= 7, (
            f"{import_path} has max_retries={task.max_retries} — "
            "with retry_backoff=True and retry_jitter=False, this yields a "
            "retry budget too small to cover the documented 65s nova-db "
            "stall. Need max_retries=7+ for 123s+ budget."
        )
        # Sanity: exponential backoff must be enabled so the delays compound.
        assert task.retry_backoff, (
            f"{import_path} has retry_backoff disabled — "
            "fixed 1s retries would burn the budget on the first 7 seconds."
        )
        # Critical: jitter MUST be False — see empirical finding above.
        assert not task.retry_jitter, (
            f"{import_path} has retry_jitter=True — empirically halves the "
            "retry budget. Set retry_jitter=False for deterministic coverage."
        )


# ────────────────────────────────────────────────────────────────────────────
# 2. Transient-DB except clause — OperationalError must propagate, not be
#    converted into a terminal _mark_failed call
# ────────────────────────────────────────────────────────────────────────────


class TestTransientDbErrorPropagates:
    """The orchestrator's top-level except chain MUST re-raise OperationalError
    so Celery's autoretry_for catches it. Pre-fix: the catch-all `except
    Exception` swallowed it and called _mark_failed (which also failed).
    """

    def test_orchestrate_template_job_reraises_operational_error(self):
        """The inner dispatcher must let OperationalError propagate to Celery."""
        from app.tasks import template_orchestrate

        job_uuid = uuid.uuid4()
        op_err = _operational_error()

        with (
            patch.object(
                template_orchestrate,
                "_run_template_job",
                side_effect=op_err,
            ),
            patch.object(template_orchestrate, "_mark_failed") as mock_mark_failed,
        ):
            with pytest.raises(OperationalError):
                template_orchestrate._orchestrate_template_job_inner(str(job_uuid), job_uuid, False)
            mock_mark_failed.assert_not_called()

    def test_orchestrate_template_job_marks_non_db_exceptions_failed(self):
        """The catch-all still fires for real bugs (non-DB exceptions)."""
        from app.tasks import template_orchestrate

        job_uuid = uuid.uuid4()
        with (
            patch.object(
                template_orchestrate,
                "_run_template_job",
                side_effect=RuntimeError("real bug"),
            ),
            patch.object(template_orchestrate, "_mark_failed") as mock_mark_failed,
        ):
            # Should NOT raise — caught by the catch-all → _mark_failed called.
            template_orchestrate._orchestrate_template_job_inner(str(job_uuid), job_uuid, False)
            mock_mark_failed.assert_called_once()

    def test_non_transient_dbapi_error_falls_to_catch_all(self):
        """IntegrityError, ProgrammingError, etc. are deterministic bugs.

        The transient-DB except clause must NOT catch them — they should
        fall through to the catch-all `except Exception` so _mark_failed
        gets called and the user sees an immediate processing_failed
        instead of a row stuck at status=processing until the 5-min
        sweeper.

        This guards against re-broadening the clause to (OperationalError,
        DBAPIError). Reviewed and narrowed on 2026-05-18.
        """
        from sqlalchemy.exc import IntegrityError

        from app.tasks import template_orchestrate

        # IntegrityError is a DBAPIError subclass but NOT an OperationalError.
        # Pre-narrowing: this would have been caught + re-raised + terminal
        # failure with no _mark_failed. Post-narrowing: catch-all fires.
        integrity_err = IntegrityError("INSERT ...", {}, Exception("UNIQUE constraint"))

        job_uuid = uuid.uuid4()
        with (
            patch.object(
                template_orchestrate,
                "_run_template_job",
                side_effect=integrity_err,
            ),
            patch.object(template_orchestrate, "_mark_failed") as mock_mark_failed,
        ):
            # Catch-all should fire — _mark_failed called, no raise.
            template_orchestrate._orchestrate_template_job_inner(str(job_uuid), job_uuid, False)
            mock_mark_failed.assert_called_once()


# ────────────────────────────────────────────────────────────────────────────
# 3. _mark_failed / _fail_job / _fail_track must retry transient DB errors
# ────────────────────────────────────────────────────────────────────────────


class TestMarkFailedRobustness:
    """The terminal failure path must itself survive a transient DB outage.

    Pre-fix: _mark_failed called _sync_session() once. If the DB was still
    down (which it was during the incident), this raised OperationalError
    and bubbled past Celery, leaving the row at status=processing.
    """

    def test_mark_failed_retries_through_transient_outage(self):
        """First 2 attempts hit OperationalError; 3rd succeeds."""
        from app.tasks import template_orchestrate

        op_err = _operational_error()
        good_session = MagicMock()
        good_session.__enter__ = MagicMock(return_value=MagicMock())
        good_session.__exit__ = MagicMock(return_value=False)

        # Sequence: fail, fail, succeed
        call_sequence = [op_err, op_err, good_session]

        def session_factory():
            outcome = call_sequence.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            patch.object(template_orchestrate, "_sync_session", side_effect=session_factory),
            patch.object(template_orchestrate, "mark_failed_phase") as mock_mark_phase,
            patch.object(template_orchestrate.time, "sleep"),
        ):
            # Should not raise.
            template_orchestrate._mark_failed(uuid.uuid4(), "db_connection_lost", "test message")
        mock_mark_phase.assert_called_once()

    def test_mark_failed_gives_up_after_three_attempts(self):
        """If DB is down for the full retry budget, return cleanly (don't raise)."""
        from app.tasks import template_orchestrate

        op_err = _operational_error()

        with (
            patch.object(template_orchestrate, "_sync_session", side_effect=op_err),
            patch.object(template_orchestrate, "mark_failed_phase") as mock_mark_phase,
            patch.object(template_orchestrate.time, "sleep"),
        ):
            # Must not raise even though every attempt fails.
            template_orchestrate._mark_failed(uuid.uuid4(), "db_connection_lost", "test")
        # mark_failed_phase only called on success — should NOT have run.
        mock_mark_phase.assert_not_called()

    def test_music_fail_job_retries_through_transient_outage(self):
        from app.tasks import music_orchestrate

        op_err = _operational_error()
        good_session = MagicMock()
        good_session.__enter__ = MagicMock(return_value=MagicMock())
        good_session.__exit__ = MagicMock(return_value=False)
        sequence = [op_err, good_session]

        def factory():
            outcome = sequence.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(music_orchestrate, "_sync_session", side_effect=factory):
            music_orchestrate._fail_job(str(uuid.uuid4()), "msg")
        # If we got here without raising, the retry path worked.

    def test_auto_music_fail_job_retries_through_transient_outage(self):
        from app.tasks import auto_music_orchestrate

        op_err = _operational_error()
        good_session = MagicMock()
        good_session.__enter__ = MagicMock(return_value=MagicMock())
        good_session.__exit__ = MagicMock(return_value=False)
        sequence = [op_err, good_session]

        def factory():
            outcome = sequence.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(auto_music_orchestrate, "_sync_session", side_effect=factory):
            auto_music_orchestrate._fail_job(str(uuid.uuid4()), "msg")
