"""Tests for app/services/pipeline_trace.py.

The pipeline_trace service is a contextvar + JSONB-array append. These
tests verify:
  - context manager set/clear is leak-proof on exception
  - record_pipeline_event with no bound job_id is a no-op
  - record_pipeline_event with bound job_id issues the right UPDATE
  - DB failure is swallowed; caller doesn't see it
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.pipeline_trace import (
    current_pipeline_job_id,
    pipeline_trace_for,
    record_pipeline_event,
)


def _fake_engine_capturing():
    captured: list[dict] = []
    conn = MagicMock()

    def _execute(_stmt, params):
        captured.append(params)
        return MagicMock()

    conn.execute.side_effect = _execute
    ctx_mgr = MagicMock()
    ctx_mgr.__enter__ = MagicMock(return_value=conn)
    ctx_mgr.__exit__ = MagicMock(return_value=False)
    engine = MagicMock()
    engine.begin.return_value = ctx_mgr
    return engine, captured


def test_context_manager_sets_and_clears():
    assert current_pipeline_job_id() is None
    j = uuid.uuid4()
    with pipeline_trace_for(j):
        assert current_pipeline_job_id() == str(j)
    assert current_pipeline_job_id() is None


def test_context_manager_clears_on_exception():
    """Critical regression guard: contextvar MUST clear on exception so
    the next Celery task on this worker doesn't inherit job_id."""
    j = uuid.uuid4()
    with pytest.raises(RuntimeError):
        with pipeline_trace_for(j):
            assert current_pipeline_job_id() == str(j)
            raise RuntimeError("boom")
    assert current_pipeline_job_id() is None


def test_record_event_no_bound_job_is_noop():
    engine, captured = _fake_engine_capturing()
    with patch("app.database.sync_engine", engine):
        # No `with pipeline_trace_for(...)` wrap
        record_pipeline_event("interstitial", "test_event", {"foo": 1})
    assert captured == []
    engine.begin.assert_not_called()


def test_record_event_with_bound_job_writes_row():
    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()
    with patch("app.database.sync_engine", engine):
        with pipeline_trace_for(j):
            record_pipeline_event(
                stage="beat_snap",
                event="slot_snapped",
                data={"slot_index": 3, "drift_ms": 47},
            )
    assert len(captured) == 1
    assert captured[0]["job_id"] == str(j)
    # Event JSON contains stage + event + data
    event_json = captured[0]["event_json"]
    assert "beat_snap" in event_json
    assert "slot_snapped" in event_json
    assert "drift_ms" in event_json


def test_record_event_swallows_db_failure(caplog):
    engine = MagicMock()
    engine.begin.side_effect = RuntimeError("db down")
    j = uuid.uuid4()
    with patch("app.database.sync_engine", engine):
        with pipeline_trace_for(j):
            # Must NOT raise — pipeline code can't break on a trace write
            record_pipeline_event("transition", "xfade_picked", {"type": "fade"})


def test_record_event_invalid_job_id_in_context_is_noop():
    """If somehow a non-UUID got bound (programming error), drop quietly."""
    engine, captured = _fake_engine_capturing()
    from app.services.pipeline_trace import set_pipeline_job_id  # noqa: PLC0415

    token = set_pipeline_job_id("not-a-uuid")
    try:
        with patch("app.database.sync_engine", engine):
            record_pipeline_event("interstitial", "ev", {})
    finally:
        from app.services.pipeline_trace import reset_pipeline_job_id  # noqa: PLC0415

        reset_pipeline_job_id(token)
    assert captured == []


def test_nested_contexts_restore_outer():
    """Inner with-block must restore the outer job_id on exit."""
    a = uuid.uuid4()
    b = uuid.uuid4()
    with pipeline_trace_for(a):
        assert current_pipeline_job_id() == str(a)
        with pipeline_trace_for(b):
            assert current_pipeline_job_id() == str(b)
        assert current_pipeline_job_id() == str(a)
    assert current_pipeline_job_id() is None
