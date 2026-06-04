"""CLI over app.services.build_gate for the gate tick (scripts/cron/gate_runner.sh).

Lives in app/ (copied into the prod Docker image, like app.cli.verify_overlays)
so it runs where verify-overlays runs. Keeps every DECISION in tested Python; the
bash gate runner is just an orchestrator.

Subcommands:
  render-needed   read a unified diff on stdin; exit 0 if `make verify-overlays`
                  must run for this change, exit 1 if not.
  report          read a gate-results JSON array on stdin
                  ([{name, blocking, passed, detail}, ...]); print the
                  gate_report JSON to stdout and write the PR body to
                  --pr-body-out. Exit 0 always (the report carries pass/fail).
"""

from __future__ import annotations

import argparse
import json
import sys

from app.services.build_gate import (
    GateResult,
    aggregate_gate_results,
    build_pr_body,
    render_paths_touched,
)


def _cmd_render_needed(_args: argparse.Namespace) -> int:
    return 0 if render_paths_touched(sys.stdin.read()) else 1


def _cmd_report(args: argparse.Namespace) -> int:
    raw = json.load(sys.stdin)
    results = [
        GateResult(
            name=r["name"],
            blocking=bool(r["blocking"]),
            passed=bool(r["passed"]),
            detail=r.get("detail", ""),
        )
        for r in raw
    ]
    report = aggregate_gate_results(results)
    body = build_pr_body(
        task_id=args.task_id,
        task_title=args.task_title,
        report=report,
        head_sha=args.head or "",
        base_sha=args.base or "",
    )
    if args.pr_body_out:
        with open(args.pr_body_out, "w", encoding="utf-8") as fh:
            fh.write(body)
    json.dump(report.to_dict(), sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli.gate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render-needed", help="exit 0 if verify-overlays must run")
    rep = sub.add_parser("report", help="build gate_report JSON + PR body")
    rep.add_argument("--task-id", required=True)
    rep.add_argument("--task-title", required=True)
    rep.add_argument("--head", default="")
    rep.add_argument("--base", default="")
    rep.add_argument("--pr-body-out", default="")
    args = parser.parse_args(argv)
    if args.cmd == "render-needed":
        return _cmd_render_needed(args)
    if args.cmd == "report":
        return _cmd_report(args)
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
