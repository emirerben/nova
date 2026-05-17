"""Tests for app/agents/_persistence.py — best-effort agent_run capture.

Persistence MUST be invisible to the caller: every error path swallows
into a log line. These tests verify:
  - non-UUID job_id (e.g. "track:foo") → no DB write attempted
  - UUID job_id → INSERT executed with the expected row shape
  - DB failure → swallowed; agent.run() still returns successfully
  - eval-harness opt-out flag (ctx.extra["skip_agent_run_persist"]) honored
  - raw_text > 100KB → truncated with marker
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.agents._persistence import (
    _RAW_TEXT_MAX,
    _parse_owner,
    _truncate_raw_text,
    persist_agent_run,
)
from app.agents._runtime import RunContext, TerminalError
from tests.agents.conftest import (
    MockModelClient,
    SampleAgent,
    SampleInput,
    safety_response,
)

# ── Unit tests for helpers ────────────────────────────────────────────────────


def test_parse_owner_accepts_bare_uuid_as_job():
    j = uuid.uuid4()
    assert _parse_owner(str(j)) == (j, None, None)


def test_parse_owner_routes_template_prefix():
    j = uuid.uuid4()
    assert _parse_owner(f"template:{j}") == (None, j, None)


def test_parse_owner_routes_track_prefix():
    j = uuid.uuid4()
    assert _parse_owner(f"track:{j}") == (None, None, j)


def test_parse_owner_rejects_malformed_prefix():
    assert _parse_owner("template:not-a-uuid") == (None, None, None)
    assert _parse_owner("track:abc-123") == (None, None, None)


def test_parse_owner_rejects_none_and_empty():
    assert _parse_owner(None) == (None, None, None)
    assert _parse_owner("") == (None, None, None)


def test_truncate_raw_text_passthrough_under_cap():
    s = "x" * 100
    assert _truncate_raw_text(s) == s


def test_truncate_raw_text_appends_marker_over_cap():
    s = "x" * (_RAW_TEXT_MAX + 50)
    out = _truncate_raw_text(s)
    assert out is not None
    assert out.startswith("x" * _RAW_TEXT_MAX)
    assert "[truncated 50 chars]" in out


def test_truncate_raw_text_none_passthrough():
    assert _truncate_raw_text(None) is None


# ── persist_agent_run direct ─────────────────────────────────────────────────


def _fake_engine_capturing():
    """Returns (engine_mock, captured_calls). Each `conn.execute(stmt, params)`
    call adds `params` to `captured_calls`."""
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


def test_persist_skips_unparseable_job_id():
    """A non-UUID, non-prefixed string (eval harness, garbage) is still
    dropped silently — there is nothing sensible to FK on."""
    engine, captured = _fake_engine_capturing()
    with patch("app.database.sync_engine", engine):
        persist_agent_run(
            job_id="random-string-not-a-uuid",
            segment_idx=None,
            agent_name="t",
            prompt_version="1",
            model="m",
            outcome="ok",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=10,
            input_dict={"a": 1},
            output_dict={"b": 2},
            raw_text="hi",
        )
    assert captured == []
    engine.begin.assert_not_called()


def test_persist_routes_template_prefix_to_template_id():
    engine, captured = _fake_engine_capturing()
    tpl = uuid.uuid4()
    with patch("app.database.sync_engine", engine):
        persist_agent_run(
            job_id=f"template:{tpl}",
            segment_idx=None,
            agent_name="nova.compose.template_recipe",
            prompt_version="1",
            model="m",
            outcome="ok",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=10,
            input_dict={"a": 1},
            output_dict={"b": 2},
            raw_text="hi",
        )
    assert len(captured) == 1
    assert captured[0]["job_id"] is None
    assert captured[0]["template_id"] == str(tpl)
    assert captured[0]["music_track_id"] is None


def test_persist_routes_track_prefix_to_music_track_id():
    engine, captured = _fake_engine_capturing()
    tr = uuid.uuid4()
    with patch("app.database.sync_engine", engine):
        persist_agent_run(
            job_id=f"track:{tr}",
            segment_idx=None,
            agent_name="nova.audio.song_classifier",
            prompt_version="1",
            model="m",
            outcome="ok",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=10,
            input_dict={"a": 1},
            output_dict={"b": 2},
            raw_text="hi",
        )
    assert len(captured) == 1
    assert captured[0]["job_id"] is None
    assert captured[0]["template_id"] is None
    assert captured[0]["music_track_id"] == str(tr)


def test_persist_writes_row_for_uuid_job():
    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()
    with patch("app.database.sync_engine", engine):
        persist_agent_run(
            job_id=str(j),
            segment_idx=3,
            agent_name="nova.x",
            prompt_version="2026-05-01",
            model="gemini-2.5-flash",
            outcome="ok",
            attempts=1,
            tokens_in=42,
            tokens_out=99,
            cost_usd=0.000123,
            latency_ms=250,
            input_dict={"topic": "x"},
            output_dict={"answer": "y", "score": 80},
            raw_text='{"answer":"y","score":80}',
        )
    assert len(captured) == 1
    row = captured[0]
    assert row["job_id"] == str(j)
    assert row["template_id"] is None
    assert row["music_track_id"] is None
    assert row["segment_idx"] == 3
    assert row["agent_name"] == "nova.x"
    assert row["outcome"] == "ok"
    assert row["tokens_in"] == 42
    # JSONB params are serialized to strings before binding
    assert '"answer"' in row["output_json"]
    assert row["raw_text"] == '{"answer":"y","score":80}'


def test_persist_swallows_db_failure(caplog):
    engine = MagicMock()
    engine.begin.side_effect = RuntimeError("connection refused")
    j = uuid.uuid4()
    with patch("app.database.sync_engine", engine):
        # Must not raise
        persist_agent_run(
            job_id=str(j),
            segment_idx=None,
            agent_name="t",
            prompt_version="1",
            model="m",
            outcome="ok",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0,
            input_dict=None,
            output_dict=None,
            raw_text=None,
        )


def test_persist_handles_unserializable_input():
    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()

    class Unpickleable:
        def __repr__(self):
            return "<weird>"

    with patch("app.database.sync_engine", engine):
        persist_agent_run(
            job_id=str(j),
            segment_idx=None,
            agent_name="t",
            prompt_version="1",
            model="m",
            outcome="ok",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0,
            # Pass a value that json.dumps(default=str) can still handle
            input_dict={"obj": Unpickleable()},
            output_dict=None,
            raw_text=None,
        )
    assert len(captured) == 1
    # `default=str` falls back to repr-ish; serialization succeeds.
    assert "<weird>" in captured[0]["input_json"]


# ── End-to-end: Agent.run() through _log_outcome ─────────────────────────────


def test_agent_run_triggers_persist_for_uuid_job(
    sample_agent: SampleAgent, mock_client: MockModelClient
):
    """Drive a real Agent.run() with a UUID job_id and assert one row was
    persisted by the outcome logger. Verifies the wiring inside
    `_log_outcome` actually calls `persist_agent_run`."""
    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()
    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 50})

    with patch("app.database.sync_engine", engine):
        out = sample_agent.run(
            SampleInput(topic="x"),
            ctx=RunContext(job_id=str(j), segment_idx=2),
        )

    assert out.answer == "ok"
    assert len(captured) == 1
    assert captured[0]["job_id"] == str(j)
    assert captured[0]["segment_idx"] == 2
    assert captured[0]["agent_name"] == "test.sample"
    assert captured[0]["outcome"] == "ok"


def test_agent_run_persists_track_context_to_music_track_id(
    sample_agent: SampleAgent, mock_client: MockModelClient
):
    """Track-level analysis uses ctx.job_id='track:<uuid>' and now lands in
    agent_run.music_track_id so the per-job admin view can join it back."""
    engine, captured = _fake_engine_capturing()
    track_uuid = uuid.uuid4()
    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 50})
    with patch("app.database.sync_engine", engine):
        sample_agent.run(
            SampleInput(topic="x"),
            ctx=RunContext(job_id=f"track:{track_uuid}"),
        )
    assert len(captured) == 1
    assert captured[0]["job_id"] is None
    assert captured[0]["music_track_id"] == str(track_uuid)


def test_agent_run_skips_persist_for_malformed_track_context(
    sample_agent: SampleAgent, mock_client: MockModelClient
):
    """A track-prefixed but non-UUID suffix (legacy/eval) is still dropped."""
    engine, captured = _fake_engine_capturing()
    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 50})
    with patch("app.database.sync_engine", engine):
        sample_agent.run(
            SampleInput(topic="x"),
            ctx=RunContext(job_id="track:song-1"),
        )
    assert captured == []


def test_agent_run_skips_persist_when_opted_out(
    sample_agent: SampleAgent, mock_client: MockModelClient
):
    """Eval harness opts out via ctx.extra['skip_agent_run_persist']."""
    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()
    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 50})
    with patch("app.database.sync_engine", engine):
        sample_agent.run(
            SampleInput(topic="x"),
            ctx=RunContext(
                job_id=str(j),
                extra={"skip_agent_run_persist": True},
            ),
        )
    assert captured == []


def test_agent_run_persist_failure_does_not_break_job(
    sample_agent: SampleAgent, mock_client: MockModelClient
):
    """DB failure during persistence MUST NOT propagate — the job continues."""
    engine = MagicMock()
    engine.begin.side_effect = RuntimeError("db down")
    j = uuid.uuid4()
    mock_client.queue("gemini-2.5-flash", {"answer": "still-ok", "score": 100})
    with patch("app.database.sync_engine", engine):
        out = sample_agent.run(
            SampleInput(topic="x"),
            ctx=RunContext(job_id=str(j)),
        )
    assert out.answer == "still-ok"


# ── Failure-outcome persistence ──────────────────────────────────────────────
#
# The admin job-debug view's primary use case is diagnosing bad outputs. The
# rows that matter most are the FAILING ones — schema errors, refusals,
# transient exhaustion. Below tests assert each failure path writes a row
# with the right ``outcome`` field, raw_text captured, and error_message
# populated. Without these, a regression that breaks failure-path persistence
# would silently leave the admin view blind to the exact runs admins need.


def test_agent_run_schema_error_persisted_with_raw_text(
    sample_agent: SampleAgent, mock_client: MockModelClient
):
    """Pydantic-invalid output (parsable JSON, out-of-range value) → terminal_schema."""
    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()
    # SampleOutput requires score 0-100. score=9999 passes _check_refusal
    # (required keys present, JSON valid) but fails Pydantic validation in
    # parse() → SchemaError. Two attempts (initial + schema-clarification
    # retry) exhausts the schema_retries budget → terminal_schema.
    mock_client.queue(
        "gemini-2.5-flash",
        {"answer": "yes", "score": 9999},
        {"answer": "yes", "score": 9999},
    )
    with patch("app.database.sync_engine", engine):
        with pytest.raises(TerminalError):
            sample_agent.run(SampleInput(topic="x"), ctx=RunContext(job_id=str(j)))
    schema_rows = [r for r in captured if r["outcome"] == "terminal_schema"]
    actual = [r["outcome"] for r in captured]
    assert schema_rows, f"expected terminal_schema row, got: {actual}"
    row = schema_rows[0]
    assert row["raw_text"] == '{"answer": "yes", "score": 9999}'
    assert row["error_message"]
    assert row["output_json"] is None  # failure path → no parsed output


def test_agent_run_refusal_persisted(sample_agent: SampleAgent, mock_client: MockModelClient):
    """Safety refusal → terminal_refusal row with raw_text captured."""
    from app.agents._runtime import ModelInvocation  # noqa: PLC0415

    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()
    # SAFETY finish_reason raises RefusalError. The runtime tries one
    # clarification retry (refusal_retries 0→1), so queue TWO safety
    # responses; the second one exhausts the retry budget → terminal_refusal.
    def _safety_inv() -> ModelInvocation:
        return ModelInvocation(
            raw_text='{"partial":"blocked"}',
            tokens_in=10,
            tokens_out=5,
            raw_response=safety_response(),
        )

    mock_client.queue("gemini-2.5-flash", _safety_inv(), _safety_inv())
    with patch("app.database.sync_engine", engine):
        with pytest.raises(TerminalError):
            sample_agent.run(SampleInput(topic="x"), ctx=RunContext(job_id=str(j)))
    refusal_rows = [r for r in captured if r["outcome"] == "terminal_refusal"]
    actual = [r["outcome"] for r in captured]
    assert refusal_rows, f"expected terminal_refusal row, got: {actual}"
    row = refusal_rows[0]
    assert row["raw_text"] == '{"partial":"blocked"}'
    assert row["error_message"] and "refusal" in row["error_message"].lower()


def test_agent_run_transient_exhausted_persisted(
    sample_agent: SampleAgent, mock_client: MockModelClient
):
    """All models exhausted on transient errors → terminal_transient row."""
    from app.agents._runtime import TransientError  # noqa: PLC0415

    engine, captured = _fake_engine_capturing()
    j = uuid.uuid4()
    # SampleAgent has max_attempts=3 + fallback to pro, so 3+3 transients
    # exhausts the chain.
    for _ in range(3):
        mock_client.queue("gemini-2.5-flash", TransientError("503"))
    for _ in range(3):
        mock_client.queue("gemini-2.5-pro", TransientError("503"))
    with patch("app.database.sync_engine", engine):
        with pytest.raises(TerminalError):
            sample_agent.run(SampleInput(topic="x"), ctx=RunContext(job_id=str(j)))
    transient_rows = [r for r in captured if r["outcome"] == "terminal_transient"]
    actual = [r["outcome"] for r in captured]
    assert transient_rows, f"expected terminal_transient row, got: {actual}"
    row = transient_rows[0]
    assert row["error_message"]  # captures the last transient exception
