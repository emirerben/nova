"""Replay stored phone-export reports under the current late-window rule. Read-only.

Run before a verifier bump ships, to see which `checked` reports the new rule rejects:

    python -m app.cli.replay_export_verdicts reports/
    python -m app.cli.replay_export_verdicts --fetch --prod --since 2026-11-01 reports/

`reports/` holds `GET /admin/jobs/{id}/export-verification` responses, one file per job.
`--fetch` fills it through `scripts/admin.py` (GET only) before replaying.

The rule reads stored per-sample evidence, so no media is needed. A report from before
v6 did not store whether a window was on the source frame grid; the rule applies only
there, so such a flip is an upper bound (`grid unknown`). Every picture window that
passed is replayed, also inside a report rejected for its text or audio. Exit 1 when a
passed window flips.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]


def _reports(node) -> Iterator[dict]:
    if isinstance(node, dict):
        if isinstance(node.get("checks"), list) and "verifier_version" in node:
            yield node
        for value in node.values():
            yield from _reports(value)
    elif isinstance(node, list):
        for value in node:
            yield from _reports(value)


def replay(job: dict) -> list[dict]:
    """One row per stored picture check of a job's export-verification response."""
    from app.services.actual_export_verification import _late_window  # noqa: PLC0415

    rows = []
    for report in _reports(job):
        for check in report["checks"]:
            if check.get("kind") != "picture":
                continue
            row = {
                "job_id": job.get("job_id"),
                "clip_id": check.get("clip_id"),
                "verifier_version": report["verifier_version"],
                "report_status": report.get("status"),
                "grid": {True: "on", False: "off"}.get(check.get("on_source_grid"), "unknown"),
                "reason": None,
            }
            samples = check.get("sample_evidence")
            if check.get("status") != "passed":
                row["verdict"] = "already_failed"
            elif not samples or check.get("lead_next") is None:
                row["verdict"] = "not_replayable"
            else:
                if row["grid"] != "off":
                    row["reason"] = _late_window(
                        samples, check.get("neighbour_samples") or 0, check["lead_next"]
                    )
                row["verdict"] = "flips" if row["reason"] else "holds"
            rows.append(row)
    return rows


def fetch(get: Callable[[str], dict], *, since: str, out: Path, page: int = 200) -> int:
    """Save every phone-rendered job created on or after `since`; a failed GET raises."""
    out.mkdir(parents=True, exist_ok=True)
    kept = offset = 0
    while True:
        items = get(f"jobs?limit={page}&offset={offset}")["items"]
        for item in items:
            if item["created_at"][:10] < since:
                return kept
            evidence = get(f"jobs/{item['job_id']}/export-verification")
            if evidence.get("variants"):
                (out / f"{item['job_id']}.json").write_text(json.dumps(evidence))
                kept += 1
        if len(items) < page:
            return kept
        offset += page


def _admin(script: Path, prod: bool) -> Callable[[str], dict]:
    def get(path: str) -> dict:
        done = subprocess.run(
            [sys.executable, str(script), *(["--prod"] if prod else []), "GET", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode:
            raise RuntimeError(f"admin request failed: {path}: {done.stderr.strip()}")
        return json.loads(done.stdout)

    return get


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", type=Path, help="directory of export-verification responses")
    parser.add_argument("--fetch", action="store_true", help="fill the directory first")
    parser.add_argument("--prod", action="store_true", help="fetch from production")
    parser.add_argument("--since", default="2026-10-01", help="oldest job date to fetch")
    parser.add_argument("--admin", type=Path, default=REPO_ROOT / "scripts" / "admin.py")
    args = parser.parse_args(argv)
    # The verifier module imports server settings but this command never connects to
    # storage or a database; mirror verify_device_export's inert defaults.
    os.environ.setdefault("STORAGE_BUCKET", "offline-export-replay")
    os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")
    if args.fetch:
        kept = fetch(_admin(args.admin, args.prod), since=args.since, out=args.reports)
        print(f"fetched {kept} phone-rendered jobs since {args.since}")
    rows = [
        row
        for path in sorted(args.reports.glob("*.json"))
        for row in replay(json.loads(path.read_text()))
    ]
    counts = Counter(row["verdict"] for row in rows)
    print(f"picture checks: {len(rows)}")
    for verdict in ("holds", "flips", "already_failed", "not_replayable"):
        print(f"{verdict}: {counts[verdict]}")
    for row in rows:
        if row["verdict"] == "flips":
            print(
                f"  {row['job_id']} {row['clip_id']} {row['reason']} "
                f"(report {row['report_status']}, {row['verifier_version']}, grid {row['grid']})"
            )
    return 1 if counts["flips"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
