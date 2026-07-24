"""Summarize render instrumentation from admin debug JSON.

Read-only usage:

    python scripts/render_trace_baseline.py --input debug.json
    python scripts/render_trace_baseline.py --job-id <uuid> --prod
    python scripts/render_trace_baseline.py --reference-url <plan-item-url> --input debug.json

The script never mutates Nova state. If a reference plan-item URL is provided
without an exact job/debug payload, it labels the report as comparable rather
than pretending it measured that item.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.render_summary import build_render_summary_from_debug_payload

PROD_BASE_URL = "https://nova-video.fly.dev"
LOCAL_BASE_URL = "http://localhost:8000"


def main() -> int:
    args = _parse_args()
    label = "exact" if args.job_id else "comparable"
    payload: dict[str, Any]
    if args.input:
        payload = json.loads(Path(args.input).read_text())
        if args.reference_url and not args.job_id:
            label = "comparable"
    elif args.job_id:
        payload = _fetch_debug_payload(args)
        label = "exact"
    else:
        print("ERROR: provide --input or --job-id", file=sys.stderr)
        return 2

    summary = build_render_summary_from_debug_payload(payload)
    if args.format == "json":
        print(json.dumps({"baseline": label, "render_summary": summary}, indent=2))
    else:
        print(_render_markdown(summary, label=label, reference_url=args.reference_url))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Path to GET /admin/jobs/{id}/debug JSON.")
    parser.add_argument("--job-id", help="Job UUID to fetch from the admin debug API.")
    parser.add_argument(
        "--reference-url",
        help="Reference plan-item/editor URL for report labeling.",
    )
    parser.add_argument("--prod", action="store_true", help="Fetch from production Fly API.")
    parser.add_argument("--base-url", help="Override API base URL.")
    parser.add_argument(
        "--admin-key",
        help="Admin token; defaults to ADMIN_PROD_API_KEY/ADMIN_API_KEY.",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def _fetch_debug_payload(args: argparse.Namespace) -> dict[str, Any]:
    base_url = (args.base_url or (PROD_BASE_URL if args.prod else LOCAL_BASE_URL)).rstrip("/")
    token = (
        args.admin_key or os.environ.get("ADMIN_PROD_API_KEY") or os.environ.get("ADMIN_API_KEY")
    )
    if not token:
        raise SystemExit("ERROR: admin token missing; pass --admin-key or set ADMIN_*_API_KEY")
    req = urllib.request.Request(
        f"{base_url}/admin/jobs/{args.job_id}/debug",
        headers={"X-Admin-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"ERROR: admin debug fetch failed: HTTP {exc.code}") from exc


def _render_markdown(
    summary: dict[str, Any] | None,
    *,
    label: str,
    reference_url: str | None,
) -> str:
    lines = ["# Render Timing Baseline", ""]
    lines.append(f"Baseline type: **{label}**")
    if reference_url:
        lines.append(f"Reference: `{reference_url}`")
    if summary is None:
        lines.append("")
        lines.append("No render-stage instrumentation was present in this payload.")
        return "\n".join(lines)
    lines.extend(
        [
            "",
            f"- Trace: `{summary.get('trace_id') or 'n/a'}`",
            f"- Queue time: {_fmt_ms(summary.get('total_queue_ms'))}",
            f"- Processing time: {_fmt_ms(summary.get('total_processing_ms'))}",
            f"- Agent work: {_fmt_ms(summary.get('agent_work_ms'))}",
            "",
            "## Slowest Stages",
            "",
            "| Stage | Duration | Status |",
            "|---|---:|---|",
        ]
    )
    for row in summary.get("slowest_stages") or []:
        lines.append(
            f"| `{row.get('stage')}` | {_fmt_ms(row.get('elapsed_ms'))} | {row.get('status', '')} |"
        )
    lines.extend(["", "## Repeated Stages", "", "| Stage | Count |", "|---|---:|"])
    for row in summary.get("repeated_stages") or []:
        lines.append(f"| `{row.get('stage')}` | {row.get('count')} |")
    lines.extend(["", "## Retries", "", "| Stage | Status |", "|---|---|"])
    for row in summary.get("retries") or []:
        lines.append(f"| `{row.get('stage')}` | {row.get('status') or 'retry'} |")
    lines.extend(["", "## Cache", "", "| Cache | Counts |", "|---|---|"])
    for name, counts in (summary.get("cache") or {}).items():
        rendered = ", ".join(f"{status}: {count}" for status, count in counts.items())
        lines.append(f"| `{name}` | {rendered} |")
    return "\n".join(lines)


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "n/a"
    ms = float(value)
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest}s"


if __name__ == "__main__":
    raise SystemExit(main())
