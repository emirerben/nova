"""Audit template-reanalyze wall-clocks from Langfuse traces.

Purpose: retroactively measure the Phase 1-4 perf wins (PRs #175-#178) using
the agent-trace timing data Langfuse has been collecting since before the
investigation started. Production logs aren't long enough (Fly's ring buffer
is ~7 minutes deep) and the DB only stores `recipe_cached_at` (end-of-run)
without a corresponding `started_at`. Langfuse is the only retroactive source.

Method:
  - Every `Agent.run()` posts one trace tagged with `session_id = ctx.job_id`.
  - Reanalyze tasks set `job_id = f"template:{template_id}:agentic"` (agentic
    path) or `f"template:{template_id}"` (manual path).
  - All per-agent traces for one reanalyze share that session_id — but so do
    traces from EVERY previous reanalyze of the same template, because the
    job_id is template-scoped, not run-scoped. So we cluster within each
    session by timestamp gaps: traces within `_TRACE_GAP_THRESHOLD` (10
    minutes) of each other belong to the same reanalyze run; a gap larger
    than that signals the next run. Without this clustering the wall-clock
    would span the entire history of reanalyzes for that template — useless.
  - Wall-clock per run: max(trace.timestamp + latency) - min(trace.timestamp)
    within the cluster. Captures parallel + serial agent calls correctly;
    does NOT include any non-agent work between calls (typically < 1s of
    DB writes / font baking, dominated by the agent latencies).

Caveat: if a future change adds a > 1s non-agent step between agent calls
(e.g. heavy I/O, sync FFmpeg), this approximation under-counts and should be
replaced by a DB-column-based baseline.

Usage:
  python scripts/audit_reanalyze_timings.py
  python scripts/audit_reanalyze_timings.py --since 2026-05-01 --until 2026-05-17
  python scripts/audit_reanalyze_timings.py --cutoff 2026-05-16 --format json
  python scripts/audit_reanalyze_timings.py --template-id 24ac3408-... --verbose

Output: markdown table grouped by template_id, with N pre/post-cutoff and
mean wall-clock per group. Default cutoff is `2026-05-16` (the day Phase 1's
PR #175 deployed — `be0993e`).

Auth: reads `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` from
environment. Pulls from Fly secrets via `.env` if you've sourced one locally.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Default cutoff: 2026-05-16 (Phase 1 deploy: PR #175, commit be0993e).
_DEFAULT_CUTOFF = "2026-05-16"

# Session-id pattern that identifies a template reanalyze. Matches both:
#   - agentic path: "template:<uuid>:agentic"
#   - manual path:  "template:<uuid>"
_SESSION_PATTERN = re.compile(r"^template:([0-9a-f-]+)(?::agentic)?$", re.IGNORECASE)

# Two traces with a smaller gap between them are treated as the SAME reanalyze
# run. A reanalyze takes ~30s-2min end-to-end; a 10-min gap is safely larger
# than any conceivable mid-run pause but smaller than the spacing between two
# distinct operator-triggered reanalyzes.
_TRACE_GAP_THRESHOLD = timedelta(minutes=10)


@dataclass
class ReanalyzeSession:
    """One reanalyze run, reconstructed from N per-agent traces."""

    session_id: str
    template_id: str
    is_agentic: bool
    wall_clock_s: float
    trace_count: int
    started_at: datetime


def _parse_iso(s: str) -> datetime:
    """Parse Langfuse's ISO timestamps. Accepts both 'Z' and explicit offsets."""
    # Langfuse returns "2026-05-16T09:30:00.123Z" — replace Z with +00:00 for fromisoformat.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _trace_start_end(trace) -> tuple[datetime, datetime] | None:  # noqa: ANN001
    """Pull (start, end) from a v2 Langfuse trace object.

    v2 SDK traces expose `.timestamp` (start) and `.latency` (milliseconds).
    Returns None if either is missing — those traces can't contribute to a
    wall-clock estimate.
    """
    timestamp = getattr(trace, "timestamp", None)
    latency_ms = getattr(trace, "latency", None)
    if timestamp is None or latency_ms is None:
        return None
    start = _parse_iso(timestamp) if isinstance(timestamp, str) else timestamp
    end = start + timedelta(milliseconds=float(latency_ms))
    return start, end


def _fetch_reanalyze_sessions(
    since: datetime,
    until: datetime,
    template_filter: str | None = None,
    verbose: bool = False,
) -> list[ReanalyzeSession]:
    """Pull all reanalyze sessions from Langfuse in [since, until]."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        raise SystemExit(
            "ERROR: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set in environment.\n"
            "Either source `.env` locally or pull the keys from Fly secrets:\n"
            "  fly secrets list -a nova-video"
        )

    try:
        from langfuse import Langfuse  # noqa: PLC0415
    except ImportError as exc:
        raise SystemExit(
            "ERROR: langfuse SDK not installed. "
            "Run `pip install 'langfuse>=2,<3'` or `pip install -e '.[observability]'`."
        ) from exc

    client = Langfuse()
    if verbose:
        print(
            f"Querying Langfuse ({os.environ.get('LANGFUSE_HOST', 'cloud.langfuse.com')}) "
            f"from {since.isoformat()} to {until.isoformat()}",
            file=sys.stderr,
        )

    # Group traces by session_id. We pull all traces in window and filter
    # client-side because the v2 API doesn't support session_id glob/regex
    # filters and we want both `template:*` and `template:*:agentic` patterns.
    traces_by_session: dict[str, list] = defaultdict(list)
    page = 1
    total = 0
    while True:
        response = client.api.trace.list(
            from_timestamp=since,
            to_timestamp=until,
            page=page,
            limit=100,
        )
        traces = getattr(response, "data", None) or []
        if not traces:
            break
        for trace in traces:
            session_id = getattr(trace, "session_id", None) or getattr(trace, "sessionId", None)
            if not session_id:
                continue
            match = _SESSION_PATTERN.match(session_id)
            if not match:
                continue
            if template_filter and match.group(1) != template_filter:
                continue
            traces_by_session[session_id].append(trace)
            total += 1
        if verbose:
            print(f"  page {page}: {len(traces)} traces, {total} reanalyze so far", file=sys.stderr)
        page += 1

    if verbose:
        print(
            f"Found {total} reanalyze traces across {len(traces_by_session)} sessions",
            file=sys.stderr,
        )

    sessions: list[ReanalyzeSession] = []
    for session_id, traces in traces_by_session.items():
        match = _SESSION_PATTERN.match(session_id)
        if match is None:
            continue
        clusters = _cluster_traces_by_gap(traces)
        for cluster in clusters:
            sessions.append(
                ReanalyzeSession(
                    session_id=session_id,
                    template_id=match.group(1),
                    is_agentic=session_id.endswith(":agentic"),
                    wall_clock_s=(cluster.max_end - cluster.min_start).total_seconds(),
                    trace_count=cluster.trace_count,
                    started_at=cluster.min_start,
                )
            )

    return sessions


@dataclass
class _TraceCluster:
    """One reanalyze run's worth of traces: their min start, max end, count."""

    min_start: datetime
    max_end: datetime
    trace_count: int


def _cluster_traces_by_gap(traces) -> list[_TraceCluster]:  # noqa: ANN001
    """Group traces into reanalyze runs by timestamp-gap detection.

    Production code reuses `job_id` (which becomes `session_id` in Langfuse)
    across multiple reanalyzes of the same template — so a naive
    group-by-session_id collapses N reanalyzes into one mega-cluster with a
    wall-clock that spans days. Cluster by `_TRACE_GAP_THRESHOLD` instead:
    sort traces by start time, then split whenever the gap from the previous
    trace's end to the next trace's start exceeds the threshold.
    """
    endpoints: list[tuple[datetime, datetime]] = []
    for t in traces:
        pair = _trace_start_end(t)
        if pair is not None:
            endpoints.append(pair)
    if not endpoints:
        return []

    endpoints.sort(key=lambda p: p[0])
    clusters: list[_TraceCluster] = []
    current_start = endpoints[0][0]
    current_end = endpoints[0][1]
    current_count = 1

    for start, end in endpoints[1:]:
        # Gap measured from the cluster's max-end so far to this trace's start.
        # If this trace started > threshold after everything before it ended,
        # close the current cluster and open a new one.
        if start - current_end > _TRACE_GAP_THRESHOLD:
            clusters.append(
                _TraceCluster(
                    min_start=current_start,
                    max_end=current_end,
                    trace_count=current_count,
                )
            )
            current_start = start
            current_end = end
            current_count = 1
        else:
            if end > current_end:
                current_end = end
            current_count += 1

    clusters.append(
        _TraceCluster(
            min_start=current_start,
            max_end=current_end,
            trace_count=current_count,
        )
    )
    return clusters


def _summarize(sessions: list[ReanalyzeSession], cutoff: datetime) -> dict:
    """Group sessions by (template_id, path) and bucket by pre/post cutoff."""
    grouped: dict[tuple[str, bool], dict] = defaultdict(lambda: {"pre": [], "post": []})
    for s in sessions:
        bucket = "pre" if s.started_at < cutoff else "post"
        grouped[(s.template_id, s.is_agentic)][bucket].append(s.wall_clock_s)

    rows = []
    for (template_id, is_agentic), buckets in grouped.items():
        pre = buckets["pre"]
        post = buckets["post"]
        row = {
            "template_id": template_id,
            "path": "agentic" if is_agentic else "manual",
            "n_pre": len(pre),
            "mean_pre_s": statistics.mean(pre) if pre else None,
            "n_post": len(post),
            "mean_post_s": statistics.mean(post) if post else None,
        }
        if row["mean_pre_s"] and row["mean_post_s"]:
            row["delta_pct"] = round(
                100 * (row["mean_post_s"] - row["mean_pre_s"]) / row["mean_pre_s"], 1
            )
        else:
            row["delta_pct"] = None
        rows.append(row)
    return {"cutoff": cutoff.isoformat(), "rows": rows}


def _render_markdown(summary: dict) -> str:
    """Markdown table — paste into the plan file or a PR comment."""
    lines = [
        f"# Reanalyze wall-clock audit (cutoff: {summary['cutoff']})",
        "",
        "| Template | Path | N pre | Mean pre (s) | N post | Mean post (s) | Δ% |",
        "|----------|------|------:|-------------:|-------:|--------------:|----:|",
    ]
    for r in sorted(summary["rows"], key=lambda x: (x["template_id"], x["path"])):
        lines.append(
            f"| `{r['template_id'][:8]}…` | {r['path']} | {r['n_pre']} | {r['mean_pre_s']:.1f}"
            if r["mean_pre_s"] is not None
            else f"| `{r['template_id'][:8]}…` | {r['path']} | {r['n_pre']} | —"
        )
    # The single-line generator above gets the formatting wrong on None — use a
    # straightforward loop instead.
    lines = lines[:4]
    for r in sorted(summary["rows"], key=lambda x: (x["template_id"], x["path"])):
        mean_pre = f"{r['mean_pre_s']:.1f}" if r["mean_pre_s"] is not None else "—"
        mean_post = f"{r['mean_post_s']:.1f}" if r["mean_post_s"] is not None else "—"
        delta = f"{r['delta_pct']:+.1f}%" if r["delta_pct"] is not None else "—"
        lines.append(
            f"| `{r['template_id'][:8]}…` | {r['path']} | {r['n_pre']} | "
            f"{mean_pre} | {r['n_post']} | {mean_post} | {delta} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit reanalyze wall-clocks from Langfuse traces."
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO date inclusive (default: 30d before cutoff).",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="ISO date inclusive (default: now).",
    )
    parser.add_argument(
        "--cutoff",
        type=str,
        default=_DEFAULT_CUTOFF,
        help=f"ISO date splitting pre/post buckets (default: {_DEFAULT_CUTOFF}, Phase 1 deploy).",
    )
    parser.add_argument(
        "--template-id",
        type=str,
        default=None,
        help="Restrict to one template UUID.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log pagination progress to stderr.",
    )
    args = parser.parse_args()

    cutoff = _parse_iso(args.cutoff + "T00:00:00+00:00")
    until = _parse_iso(args.until + "T23:59:59+00:00") if args.until else datetime.now(UTC)
    since = (
        _parse_iso(args.since + "T00:00:00+00:00") if args.since else cutoff - timedelta(days=30)
    )

    sessions = _fetch_reanalyze_sessions(
        since=since,
        until=until,
        template_filter=args.template_id,
        verbose=args.verbose,
    )

    if not sessions:
        print(
            "No reanalyze sessions found in window. Check --since/--until or confirm "
            "credentials by running `fly secrets list -a nova-video | grep LANGFUSE`.",
            file=sys.stderr,
        )
        return 1

    summary = _summarize(sessions, cutoff)

    if args.format == "json":
        import json

        print(json.dumps(summary, indent=2, default=str))
    else:
        print(_render_markdown(summary))

    return 0


if __name__ == "__main__":
    sys.exit(main())
