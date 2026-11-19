#!/usr/bin/env python3
"""Measure CI feedback time and Mac-minutes, so a CI change has a before and an after.

Usage:
  python3 scripts/ci/ci-baseline.py                      # last 14 days, this repo
  python3 scripts/ci/ci-baseline.py --days 7 --until 2026-11-19T00:00:00Z
  python3 scripts/ci/ci-baseline.py --json .dev/ci-baseline.json

Stdlib only; needs an authenticated `gh`. Read-only: every call is a GET.
The repository is public, so hosted minutes cost nothing. What is scarce is the
five concurrent macOS runners and the time people wait for required checks, so
those are what this reports. Method notes live in
docs/runbooks/change-based-ci.md#baseline. Guards: scripts/ci/test_ci_baseline.py.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_CHECKS = ("lint", "test-api", "build-and-test")
MAC_CHECK_PREFIX = "iPhone validation"
# GitHub returns at most this many results for one filtered run query.
RUN_QUERY_CAP = 1000


def parse(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentile(values, rank):
    """Nearest-rank percentile: always one of the observed values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(rank / 100 * len(ordered)) - 1)]


def uncapped_windows(start, end, count):
    """Split [start, end] until each window is under GitHub's result cap."""
    if count(start, end) < RUN_QUERY_CAP:
        return [(start, end)]
    first, last = parse(start), parse(end)
    middle = first + timedelta(seconds=int((last - first).total_seconds() // 2))
    if middle <= first or middle >= last:
        raise RuntimeError(f"More than {RUN_QUERY_CAP} runs between {start} and {end}")
    return uncapped_windows(start, iso(middle), count) + uncapped_windows(
        iso(middle), end, count
    )


def is_mac(job):
    return any(str(label).startswith("macos") for label in job.get("labels") or ())


def mac_minutes(jobs):
    """Minutes macOS jobs held a runner, by how the job ended (all attempts)."""
    minutes, cancelled_after = Counter(), []
    unfinished = reruns = count = 0
    for job in jobs:
        if not is_mac(job) or job.get("conclusion") == "skipped":
            continue
        if not job.get("started_at") or not job.get("completed_at"):
            unfinished += 1
            continue
        seconds = (parse(job["completed_at"]) - parse(job["started_at"])).total_seconds()
        if seconds < 0:
            continue
        held = max(1, math.ceil(seconds / 60))
        count += 1
        minutes[job["conclusion"] or "unknown"] += held
        if (job.get("run_attempt") or 1) > 1:
            reruns += held
        if job["conclusion"] == "cancelled":
            cancelled_after.append(round(seconds / 60, 1))
    return {
        "jobs": count,
        "minutes": dict(minutes),
        "rerun_minutes": reruns,
        "unfinished": unfinished,
        "cancelled_after": cancelled_after,
    }


def before_merge(checks, merged_at):
    merged = parse(merged_at)
    return [c for c in checks if c.get("started_at") and parse(c["started_at"]) <= merged]


def feedback_minutes(checks, merged_at):
    """First check start to the last required check finishing, on the merged head.

    Earlier attempts count toward the start; each required check is judged by its
    latest attempt. None when a required check never finished before the merge.
    """
    started = before_merge(checks, merged_at)
    if not started:
        return None
    finished = []
    for name in REQUIRED_CHECKS:
        attempts = [c for c in started if c["name"] == name]
        if not attempts:
            return None
        latest = max(attempts, key=lambda c: parse(c["started_at"]))
        if not latest.get("completed_at"):
            return None
        finished.append(parse(latest["completed_at"]))
    first = min(parse(c["started_at"]) for c in started)
    return round((max(finished) - first).total_seconds() / 60, 1)


def pr_used_mac(checks):
    return any(
        c["name"].startswith(MAC_CHECK_PREFIX)
        and c.get("conclusion") not in (None, "skipped")
        for c in checks
    )


def gh(path, jq, paginate=True):
    """One read-only GitHub API query, one JSON value per output line.

    `paginate=False` reads a single page: a `total_count` lookup that pages
    through its results one at a time costs a request per run.
    """
    command = ["gh", "api", "--method", "GET", path, "--jq", jq]
    if paginate:
        command.insert(4, "--paginate")
    for attempt in range(4):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return [json.loads(line) for line in result.stdout.splitlines() if line]
        time.sleep(2**attempt)
    raise RuntimeError(f"gh api {path} failed: {result.stderr.strip()[:300]}")


def require_budget(calls):
    """Refuse to start per-run queries the hourly API allowance cannot finish."""
    core = gh("rate_limit", ".resources.core", paginate=False)[0]
    if core["remaining"] < calls:
        reset = datetime.fromtimestamp(core["reset"], timezone.utc)
        raise RuntimeError(
            f"needs about {calls} GitHub API calls, {core['remaining']} left"
            f" until {iso(reset)}; retry after that or use a shorter --days"
        )


def mac_workflow_paths(root=ROOT):
    """Workflows that can start a macOS job, read from the checkout."""
    return sorted(
        f".github/workflows/{path.name}"
        for path in (root / ".github/workflows").glob("*.yml")
        if "macos" in path.read_text(encoding="utf-8")
    )


def list_runs(repo, start, end):
    base = f"repos/{repo}/actions/runs?per_page=100&created="

    def count(a, b):
        path = base.replace("per_page=100", "per_page=1") + f"{a}..{b}"
        return gh(path, ".total_count", paginate=False)[0]

    fields = (
        ".workflow_runs[]|{id,name,path,event,status,conclusion,run_attempt,"
        "head_sha,created_at,run_started_at,updated_at}"
    )
    runs = {}
    for a, b in uncapped_windows(start, end, count):
        for run in gh(base + f"{a}..{b}", fields):
            runs[run["id"]] = run
    return list(runs.values())


def list_merged_prs(repo, start, end):
    merged, page = [], 1
    while True:
        batch = gh(
            f"repos/{repo}/pulls?state=closed&base=main&sort=updated&direction=desc"
            f"&per_page=100&page={page}",
            "[.[]|{number,merged_at,updated_at,sha:.head.sha}]",
        )[0]
        merged += [
            pr for pr in batch if pr["merged_at"] and start <= pr["merged_at"] <= end
        ]
        if len(batch) < 100 or batch[-1]["updated_at"] < start:
            return merged
        page += 1


def spread(values):
    return {"n": len(values), "median": percentile(values, 50), "p90": percentile(values, 90)}


def measure(repo, start, end, workers=8):
    runs = list_runs(repo, start, end)
    mac_paths = set(mac_workflow_paths())
    mac_runs = [r for r in runs if r["path"] in mac_paths and r["status"] == "completed"]

    def jobs_for(run):
        jobs = gh(
            f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=all",
            ".jobs[]|{name,labels,conclusion,started_at,completed_at,run_attempt}",
        )
        return run, jobs

    prs = list_merged_prs(repo, start, end)
    # One jobs query per Mac-workflow run and one check-runs query per PR; a
    # few of each span two pages.
    require_budget(int((len(mac_runs) + len(prs)) * 1.1) + 20)

    def checks_for(pr):
        return pr, gh(
            f"repos/{repo}/commits/{pr['sha']}/check-runs?per_page=100&filter=all",
            ".check_runs[]|{name,conclusion,started_at,completed_at}",
        )

    with ThreadPoolExecutor(workers) as pool:
        run_jobs = list(pool.map(jobs_for, mac_runs))
        pr_checks = list(pool.map(checks_for, prs))

    workflows = {}
    by_workflow = defaultdict(list)
    for run in runs:
        by_workflow[run["name"]].append(run)
    for name, group in sorted(by_workflow.items()):
        walls = [
            (parse(r["updated_at"]) - parse(r["run_started_at"])).total_seconds() / 60
            for r in group
            if r["conclusion"] in ("success", "failure")
        ]
        workflows[name] = {
            "runs": len(group),
            "cancelled": sum(r["conclusion"] == "cancelled" for r in group),
            "rerun": sum((r["run_attempt"] or 1) > 1 for r in group),
            "wall_minutes": spread([round(w, 1) for w in walls]),
        }

    mac, nightly = {}, {}
    grouped = defaultdict(list)
    for run, jobs in run_jobs:
        grouped[(run["name"], run["event"])] += jobs
        if run["event"] == "schedule":
            nightly.setdefault(run["name"], Counter())[run["conclusion"]] += 1
    all_cancelled = []
    for (name, event), jobs in sorted(grouped.items()):
        summary = mac_minutes(jobs)
        if summary["jobs"] or summary["unfinished"]:
            all_cancelled += summary.pop("cancelled_after")
            mac[f"{name} / {event}"] = summary
    total = Counter()
    for summary in mac.values():
        total.update(summary["minutes"])

    waits = {"all": [], "mac": [], "no_mac": []}
    with_mac = unmeasured = 0
    for pr, checks in pr_checks:
        used = pr_used_mac(before_merge(checks, pr["merged_at"]))
        with_mac += used
        wait = feedback_minutes(checks, pr["merged_at"])
        if wait is None:
            unmeasured += 1
            continue
        waits["all"].append(wait)
        waits["mac" if used else "no_mac"].append(wait)

    return {
        "repo": repo,
        "window": {"start": start, "end": end},
        "workflows": workflows,
        "mac": {
            "by_workflow_event": mac,
            "minutes_by_outcome": dict(total),
            "total_minutes": sum(total.values()),
            "rerun_minutes": sum(s["rerun_minutes"] for s in mac.values()),
            "cancelled_after_minutes": spread(all_cancelled)
            | {
                "within_5": sum(v <= 5 for v in all_cancelled),
                "within_10": sum(v <= 10 for v in all_cancelled),
            },
        },
        "nightly": {name: dict(counts) for name, counts in sorted(nightly.items())},
        "prs": {
            "merged": len(prs),
            "with_mac": with_mac,
            "without_finished_required_checks": unmeasured,
            "minutes_to_last_required_check": {k: spread(v) for k, v in waits.items()},
        },
    }


def render(report):
    def cell(value):
        return "-" if value is None else str(value)

    mac, prs = report["mac"], report["prs"]
    total = mac["total_minutes"] or 1
    lines = [
        f"# CI baseline: {report['repo']}",
        f"Window: {report['window']['start']} to {report['window']['end']}",
        "",
        "## Pull requests merged to main",
        f"- Merged: {prs['merged']}; a Mac ran on {prs['with_mac']}"
        f" ({prs['with_mac'] / max(prs['merged'], 1):.0%})",
        f"- Not measured (a required check never finished before merge):"
        f" {prs['without_finished_required_checks']}",
        "",
        "| Minutes, first check to last required check | PRs | Median | p90 |",
        "| --- | --- | --- | --- |",
    ]
    for label, key in (("All", "all"), ("Mac ran", "mac"), ("No Mac", "no_mac")):
        s = prs["minutes_to_last_required_check"][key]
        lines.append(f"| {label} | {s['n']} | {cell(s['median'])} | {cell(s['p90'])} |")
    lines += [
        "",
        "## Mac-minutes (all attempts, part minutes rounded up)",
        f"- Total: {mac['total_minutes']}",
    ]
    for outcome, minutes in sorted(mac["minutes_by_outcome"].items(), key=lambda i: -i[1]):
        lines.append(f"- Ended {outcome}: {minutes} ({minutes / total:.0%})")
    lines.append(f"- Spent on re-run attempts: {mac['rerun_minutes']}")
    cancelled = mac["cancelled_after_minutes"]
    lines += [
        f"- Cancelled Mac jobs: {cancelled['n']}; ran a median {cell(cancelled['median'])} min"
        f" (p90 {cell(cancelled['p90'])}) before the cancel;"
        f" {cancelled['within_5']} within 5 min, {cancelled['within_10']} within 10",
        "",
        "| Workflow / event | Mac jobs | Minutes | Cancelled min | Failed min | Re-run min |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, s in mac["by_workflow_event"].items():
        m = s["minutes"]
        lines.append(
            f"| {name} | {s['jobs']} | {sum(m.values())} | {m.get('cancelled', 0)}"
            f" | {m.get('failure', 0)} | {s['rerun_minutes']} |"
        )
    lines += ["", "## Nightly runs of Mac workflows"]
    for name, counts in report["nightly"].items():
        lines.append(f"- {name}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    lines += [
        "",
        "## Workflow runs",
        "| Workflow | Runs | Cancelled | Re-run | Wall median (min) | Wall p90 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, s in sorted(report["workflows"].items(), key=lambda i: -i[1]["runs"]):
        w = s["wall_minutes"]
        lines.append(
            f"| {name} | {s['runs']} | {s['cancelled']} | {s['rerun']}"
            f" | {cell(w['median'])} | {cell(w['p90'])} |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="owner/name; defaults to the current repository")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--until", help="window end, ISO-8601 UTC; defaults to now")
    parser.add_argument("--json", help="also write the raw summary to this path")
    args = parser.parse_args(argv)
    repo = args.repo or subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout.strip()
    end = parse(args.until) if args.until else datetime.now(timezone.utc)
    report = measure(repo, iso(end - timedelta(days=args.days)), iso(end))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"error: {error}") from error
