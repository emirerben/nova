"""Tests for scripts/audit_reanalyze_timings.py pure functions.

The Langfuse SDK call is the only IO boundary and is exercised indirectly
through `_trace_start_end` (the trace-object shape adapter). Everything else
is pure transformation: session-id parsing, time arithmetic, bucketing,
markdown rendering.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Load the script as a module without making it part of the regular package
# tree (scripts/ has no __init__.py and isn't intended to be imported by app
# code). This pattern matches how tests for other scripts/ files would work.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit_reanalyze_timings.py"
spec = importlib.util.spec_from_file_location("audit_reanalyze_timings", _SCRIPT_PATH)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
sys.modules["audit_reanalyze_timings"] = audit
spec.loader.exec_module(audit)


# ── session-id pattern ──────────────────────────────────────────────────────


def test_session_pattern_matches_agentic_path():
    m = audit._SESSION_PATTERN.match("template:24ac3408-993e-4341-a6bb-cfcc4f5ba41c:agentic")
    assert m is not None
    assert m.group(1) == "24ac3408-993e-4341-a6bb-cfcc4f5ba41c"


def test_session_pattern_matches_manual_path():
    """Manual analyze_template_task sets job_id = `template:<id>` without
    the :agentic suffix. Audit must capture both paths."""
    m = audit._SESSION_PATTERN.match("template:24ac3408-993e-4341-a6bb-cfcc4f5ba41c")
    assert m is not None
    assert m.group(1) == "24ac3408-993e-4341-a6bb-cfcc4f5ba41c"


def test_session_pattern_rejects_unrelated_session_ids():
    """Other job types (auto-music, single-video, copy generation) have
    different session_id prefixes. Audit must not count them as reanalyzes."""
    assert audit._SESSION_PATTERN.match("job:abc123") is None
    assert audit._SESSION_PATTERN.match("music:abc") is None
    # Trailing junk after the UUID should not match either.
    assert audit._SESSION_PATTERN.match("template:abc:something-else") is None


# ── trace start/end ─────────────────────────────────────────────────────────


@dataclass
class _FakeTrace:
    """Minimal trace shape — mirrors the v2 SDK fields the audit reads."""

    timestamp: str | datetime | None
    latency: float | None
    session_id: str = "template:abc:agentic"


def test_trace_start_end_with_iso_string_timestamp():
    t = _FakeTrace(timestamp="2026-05-16T10:00:00.000Z", latency=5000.0)
    pair = audit._trace_start_end(t)
    assert pair is not None
    start, end = pair
    assert start == datetime(2026, 5, 16, 10, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 5, 16, 10, 0, 5, tzinfo=UTC)


def test_trace_start_end_with_datetime_timestamp():
    """Langfuse SDK may return parsed datetime objects on some versions —
    the adapter must handle both."""
    ts = datetime(2026, 5, 16, 10, 0, 0, tzinfo=UTC)
    t = _FakeTrace(timestamp=ts, latency=2500.0)
    pair = audit._trace_start_end(t)
    assert pair is not None
    start, end = pair
    assert start == ts
    assert (end - start).total_seconds() == 2.5


def test_trace_start_end_returns_none_when_timestamp_missing():
    t = _FakeTrace(timestamp=None, latency=5000.0)
    assert audit._trace_start_end(t) is None


def test_trace_start_end_returns_none_when_latency_missing():
    t = _FakeTrace(timestamp="2026-05-16T10:00:00Z", latency=None)
    assert audit._trace_start_end(t) is None


# ── _summarize bucketing ────────────────────────────────────────────────────


def _session(
    template_id: str,
    started_at: datetime,
    wall_clock_s: float,
    is_agentic: bool = True,
) -> audit.ReanalyzeSession:
    return audit.ReanalyzeSession(
        session_id=f"template:{template_id}" + (":agentic" if is_agentic else ""),
        template_id=template_id,
        is_agentic=is_agentic,
        wall_clock_s=wall_clock_s,
        trace_count=5,
        started_at=started_at,
    )


def test_summarize_buckets_by_cutoff():
    cutoff = datetime(2026, 5, 16, tzinfo=UTC)
    sessions = [
        # Two pre-cutoff agentic runs on template A
        _session("aaa", cutoff - timedelta(days=2), 60.0),
        _session("aaa", cutoff - timedelta(days=1), 80.0),
        # One post-cutoff agentic run on template A
        _session("aaa", cutoff + timedelta(hours=1), 10.0),
    ]
    summary = audit._summarize(sessions, cutoff)
    rows = [r for r in summary["rows"] if r["template_id"] == "aaa" and r["path"] == "agentic"]
    assert len(rows) == 1
    row = rows[0]
    assert row["n_pre"] == 2
    assert row["mean_pre_s"] == 70.0
    assert row["n_post"] == 1
    assert row["mean_post_s"] == 10.0
    # Δ% = (10 - 70) / 70 * 100 = -85.7
    assert row["delta_pct"] == -85.7


def test_summarize_separates_agentic_and_manual_paths():
    """Same template_id but different paths must report as separate rows —
    Phase 2 affected both paths and the operator wants to see them apart."""
    cutoff = datetime(2026, 5, 16, tzinfo=UTC)
    sessions = [
        _session("aaa", cutoff - timedelta(days=1), 60.0, is_agentic=True),
        _session("aaa", cutoff - timedelta(days=1), 30.0, is_agentic=False),
    ]
    summary = audit._summarize(sessions, cutoff)
    paths = {r["path"] for r in summary["rows"]}
    assert paths == {"agentic", "manual"}


def test_summarize_delta_pct_is_none_when_one_bucket_empty():
    """If a template only has post-cutoff samples (e.g. a new template added
    after Phase 1 deployed), Δ% is undefined — must not crash."""
    cutoff = datetime(2026, 5, 16, tzinfo=UTC)
    sessions = [
        _session("bbb", cutoff + timedelta(hours=1), 12.0),
    ]
    summary = audit._summarize(sessions, cutoff)
    row = summary["rows"][0]
    assert row["n_pre"] == 0
    assert row["mean_pre_s"] is None
    assert row["delta_pct"] is None


# ── markdown rendering ──────────────────────────────────────────────────────


def test_render_markdown_includes_cutoff_in_header():
    cutoff = datetime(2026, 5, 16, tzinfo=UTC)
    sessions = [_session("aaa", cutoff - timedelta(days=1), 60.0)]
    summary = audit._summarize(sessions, cutoff)
    md = audit._render_markdown(summary)
    assert "2026-05-16" in md
    assert "| Template |" in md
    assert "aaa" in md


def test_render_markdown_handles_one_sided_data():
    """A row with only post-cutoff data must render '—' for pre fields,
    not crash. The first prior implementation tripped here."""
    cutoff = datetime(2026, 5, 16, tzinfo=UTC)
    sessions = [_session("bbb", cutoff + timedelta(hours=1), 12.0)]
    summary = audit._summarize(sessions, cutoff)
    md = audit._render_markdown(summary)
    # Must contain a "—" somewhere in the row for the missing mean_pre_s.
    assert "—" in md


# ── trace clustering (the bug E2E smoke caught) ──────────────────────────────


def test_cluster_separates_runs_by_gap():
    """The original implementation grouped all traces by session_id alone.
    But production reuses session_id across reanalyzes for the same template,
    so two reanalyzes 60 minutes apart got collapsed into one "session" with
    a wall-clock spanning the gap. Clustering by timestamp gap fixes this.

    Setup: two distinct reanalyze runs, 60 min apart (well above the 10-min
    gap threshold). Each run has 3 agent traces. Cluster must produce 2."""
    run1_start = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    run2_start = datetime(2026, 5, 15, 11, 0, 0, tzinfo=UTC)
    traces = [
        _FakeTrace(timestamp=run1_start.isoformat().replace("+00:00", "Z"), latency=20000.0),
        _FakeTrace(
            timestamp=(run1_start + timedelta(seconds=25)).isoformat().replace("+00:00", "Z"),
            latency=15000.0,
        ),
        _FakeTrace(
            timestamp=(run1_start + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
            latency=10000.0,
        ),
        _FakeTrace(timestamp=run2_start.isoformat().replace("+00:00", "Z"), latency=2000.0),
        _FakeTrace(
            timestamp=(run2_start + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
            latency=1500.0,
        ),
        _FakeTrace(
            timestamp=(run2_start + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            latency=1000.0,
        ),
    ]
    clusters = audit._cluster_traces_by_gap(traces)
    assert len(clusters) == 2
    # Run 1 wall-clock: 60s from start to last trace start + 10s last latency = 70s
    assert clusters[0].trace_count == 3
    assert 65 <= (clusters[0].max_end - clusters[0].min_start).total_seconds() <= 75
    # Run 2 wall-clock: 5s + 1s = 6s
    assert clusters[1].trace_count == 3
    assert 5 <= (clusters[1].max_end - clusters[1].min_start).total_seconds() <= 7


def test_cluster_groups_close_traces_together():
    """Multiple agent calls within one reanalyze must end up in the SAME
    cluster — the gap inside one run is seconds, not minutes."""
    base = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    traces = [
        _FakeTrace(timestamp=base.isoformat().replace("+00:00", "Z"), latency=5000.0),
        _FakeTrace(
            timestamp=(base + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            latency=3000.0,
        ),
        _FakeTrace(
            timestamp=(base + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            latency=2000.0,
        ),
    ]
    clusters = audit._cluster_traces_by_gap(traces)
    assert len(clusters) == 1
    assert clusters[0].trace_count == 3


def test_cluster_handles_unsorted_input():
    """Langfuse may return traces in any order. Cluster must sort first."""
    run1_start = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    run2_start = datetime(2026, 5, 15, 11, 0, 0, tzinfo=UTC)
    traces = [
        _FakeTrace(timestamp=run2_start.isoformat().replace("+00:00", "Z"), latency=2000.0),
        _FakeTrace(timestamp=run1_start.isoformat().replace("+00:00", "Z"), latency=20000.0),
        _FakeTrace(
            timestamp=(run1_start + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            latency=5000.0,
        ),
    ]
    clusters = audit._cluster_traces_by_gap(traces)
    assert len(clusters) == 2
    assert clusters[0].min_start == run1_start
    assert clusters[1].min_start == run2_start


def test_cluster_empty_input_returns_empty():
    assert audit._cluster_traces_by_gap([]) == []


def test_cluster_skips_traces_with_missing_timestamps():
    """If a trace has no timestamp/latency, exclude it from clustering rather
    than crash — same fail-open philosophy as _trace_start_end."""
    base = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    traces = [
        _FakeTrace(timestamp=base.isoformat().replace("+00:00", "Z"), latency=5000.0),
        _FakeTrace(timestamp=None, latency=3000.0),
        _FakeTrace(timestamp="not-a-real-iso-string", latency=None),
    ]
    clusters = audit._cluster_traces_by_gap(traces)
    assert len(clusters) == 1
    assert clusters[0].trace_count == 1


def test_cluster_uses_max_end_not_max_start_for_gap_detection():
    """Use end-to-start gap, not start-to-start. A long-running trace that
    almost butts up against the next one should NOT trigger a split."""
    base = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    traces = [
        _FakeTrace(
            timestamp=base.isoformat().replace("+00:00", "Z"),
            latency=300000.0,  # 5 min latency
        ),
        _FakeTrace(
            # 9 min after first START, only 4 min after it ENDED.
            timestamp=(base + timedelta(minutes=9)).isoformat().replace("+00:00", "Z"),
            latency=5000.0,
        ),
    ]
    clusters = audit._cluster_traces_by_gap(traces)
    assert len(clusters) == 1


# ── credential / SDK gating ─────────────────────────────────────────────────


def test_fetch_raises_when_credentials_missing(monkeypatch):
    """The fetch function must SystemExit with a helpful message when
    LANGFUSE_*_KEY env vars are missing. Surfaces the auth problem cleanly
    instead of failing inside the SDK."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    import pytest

    with pytest.raises(SystemExit) as excinfo:
        audit._fetch_reanalyze_sessions(
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 6, 1, tzinfo=UTC),
        )
    msg = str(excinfo.value)
    assert "LANGFUSE_PUBLIC_KEY" in msg or "LANGFUSE_SECRET_KEY" in msg


def test_parse_iso_handles_z_suffix():
    """Langfuse returns timestamps as `...Z` (UTC) — Python's fromisoformat
    on 3.11 doesn't accept Z natively."""
    t = audit._parse_iso("2026-05-16T10:00:00.000Z")
    assert t.tzinfo is not None
    assert t.year == 2026


def test_parse_iso_handles_explicit_offset():
    t = audit._parse_iso("2026-05-16T10:00:00+00:00")
    assert t.tzinfo is not None
