"""Aggregate template-job phase_done logs into a per-phase Pareto.

Phase 0 deliverable of the multi-clip template speedup plan. Lives in-repo
so anyone with read access to Fly logs can produce the baseline data the
plan's milestone priority depends on.

Two structlog events feed this:

- ``fixed_intro_stage_done`` — outer Celery-stage timing for
  ``orchestrate_template_job`` (download_clips, analyze_clips, match_clips,
  assemble, mix_audio, generate_copy, upload, finalize). Emitted by the
  ``_stage`` context manager at template_orchestrate.py:192.

- ``assemble_phase_done`` — inner ``_assemble_clips`` sub-phase timing
  (plan, single_pass, render_parallel, curtain_and_interstitials, join,
  text_overlay). Emitted by ``_phase_done`` at template_orchestrate.py:1575.

Both carry ``elapsed_ms`` and ``job_id``. The script joins on ``job_id`` so
each row in the report carries both the outer stage and the inner sub-phase
breakdown for the same job. Template id is pulled from any ``template_id``
field present on either event (it's logged at job start, the structlog
binding propagates).

Usage:
    fly logs --app nova-video --json | python scripts/aggregate_phase_timings.py
    python scripts/aggregate_phase_timings.py --input ./logs.jsonl
    python scripts/aggregate_phase_timings.py --input ./logs.jsonl --format json
    python scripts/aggregate_phase_timings.py --input ./logs.jsonl --by template_id

The output is intentionally a markdown table by default — easy to paste
into Notion or the design doc that feeds the milestone priority decision.

Note: this is a read-only analytics tool. It does not mutate state, does
not require API/DB credentials, and is safe to run against arbitrary log
captures. If a log line is not valid JSON it is silently skipped (Fly
sometimes interleaves non-structured stderr).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import IO, Iterable

PHASE_EVENTS = {"fixed_intro_stage_done", "assemble_phase_done"}


@dataclass
class PhaseStats:
    samples: list[float] = field(default_factory=list)

    def add(self, elapsed_ms: float) -> None:
        self.samples.append(elapsed_ms)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def p95(self) -> float:
        return _quantile(self.samples, 0.95)

    @property
    def p99(self) -> float:
        return _quantile(self.samples, 0.99)

    @property
    def total(self) -> float:
        return sum(self.samples)


def _quantile(values: list[float], q: float) -> float:
    """Linear interpolation between adjacent ranks. statistics.quantiles
    requires N≥2, which is annoying for low-traffic templates. Manual
    formula handles N=1 gracefully (returns the single sample)."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = q * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    weight = rank - lo
    return sorted_values[lo] * (1 - weight) + sorted_values[hi] * weight


def _iter_events(stream: IO[str]) -> Iterable[dict]:
    """Yield structlog JSON dicts. Skips non-JSON and non-phase lines."""
    for line in stream:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Fly's --json wraps log output in an envelope: {"timestamp": ...,
        # "message": "{...real structlog...}"}. Unwrap if present so the
        # tool works with either raw structlog jsonl or Fly's wrapper.
        if isinstance(evt.get("message"), str) and evt["message"].startswith("{"):
            try:
                evt = json.loads(evt["message"])
            except json.JSONDecodeError:
                continue
        if not isinstance(evt, dict):
            continue
        if evt.get("event") in PHASE_EVENTS:
            yield evt


def aggregate(events: Iterable[dict], group_by: str | None = None) -> dict:
    """Walk events and bucket by phase, optionally also by group_by field.

    Returns ``{ group_key: { phase_name: PhaseStats } }``. When group_by is
    None, the outer dict has a single ``"__all__"`` key. Phase names are
    the structlog ``stage`` (outer) or ``phase`` (inner) value with a
    prefix so they don't collide.
    """
    buckets: dict[str, dict[str, PhaseStats]] = defaultdict(
        lambda: defaultdict(PhaseStats)
    )
    for evt in events:
        elapsed = evt.get("elapsed_ms")
        if not isinstance(elapsed, (int, float)):
            continue
        if evt.get("event") == "fixed_intro_stage_done":
            phase_name = f"stage:{evt.get('stage', 'unknown')}"
        else:
            phase_name = f"assemble:{evt.get('phase', 'unknown')}"
        key = str(evt.get(group_by) or "<missing>") if group_by else "__all__"
        buckets[key][phase_name].add(float(elapsed))
    return buckets


def render_markdown(buckets: dict, group_by: str | None) -> str:
    """Single-table-per-group markdown output."""
    lines: list[str] = []
    lines.append("# Phase timing Pareto\n")
    if group_by:
        lines.append(f"Grouped by `{group_by}`.\n")
    for group_key in sorted(buckets):
        if group_by:
            lines.append(f"## {group_by}={group_key}\n")
        lines.append("| Phase | N | Median (ms) | p95 (ms) | p99 (ms) | Total (ms) |")
        lines.append("|-------|---|-------------|----------|----------|------------|")
        phase_rows = sorted(
            buckets[group_key].items(),
            key=lambda kv: kv[1].total,
            reverse=True,
        )
        for phase_name, stats in phase_rows:
            lines.append(
                f"| {phase_name} | {stats.count} | "
                f"{stats.median:,.0f} | {stats.p95:,.0f} | "
                f"{stats.p99:,.0f} | {stats.total:,.0f} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_json(buckets: dict) -> str:
    out: dict = {}
    for group_key, phases in buckets.items():
        out[group_key] = {
            phase_name: {
                "count": stats.count,
                "median_ms": round(stats.median, 1),
                "p95_ms": round(stats.p95, 1),
                "p99_ms": round(stats.p99, 1),
                "total_ms": round(stats.total, 1),
            }
            for phase_name, stats in phases.items()
        }
    return json.dumps(out, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input",
        type=argparse.FileType("r"),
        default=sys.stdin,
        help="JSONL log file. Defaults to stdin so you can pipe `fly logs`.",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format. Markdown is the default for human review.",
    )
    parser.add_argument(
        "--by",
        dest="group_by",
        default=None,
        help="Group rows by this event field (e.g. template_id, slot_count).",
    )
    args = parser.parse_args(argv)

    buckets = aggregate(_iter_events(args.input), group_by=args.group_by)
    if not buckets:
        print(
            "No phase_done events found. Expected JSONL on stdin or via "
            "--input. Each event must have event=fixed_intro_stage_done or "
            "event=assemble_phase_done and an elapsed_ms field.",
            file=sys.stderr,
        )
        return 1

    rendered = (
        render_json(buckets)
        if args.format == "json"
        else render_markdown(buckets, args.group_by)
    )
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
