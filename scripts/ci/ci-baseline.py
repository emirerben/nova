#!/usr/bin/env python3
"""Measure CI feedback time and Mac-minutes, so a CI change has a before and an after.

Usage:
  python3 scripts/ci/ci-baseline.py                      # last 14 days, this repo
  python3 scripts/ci/ci-baseline.py --days 7 --until 2026-11-19T00:00:00Z
  python3 scripts/ci/ci-baseline.py --json .dev/ci-baseline.json
  python3 scripts/ci/ci-baseline.py --phases 150         # waiting/setup/running per job

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
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_CHECKS = ("lint", "test-api", "build-and-test")
MAC_CHECK_PREFIX = "iPhone validation"
# GitHub returns at most this many results for one filtered run query.
RUN_QUERY_CAP = 1000

# How `--phases` files a step. Anything not listed here is running time, so a
# new step can never make setup look cheaper than it is.
RUNNER_STEPS = {"Set up job", "Complete job", "Initialize containers", "Stop containers"}
# An action step without its own name: "Run actions/checkout@v4".
ACTION_STEP = re.compile(r"^Run \.?[\w.-]*/[\w./-]+(@\S+)?$")
SETUP_PREFIXES = (
    "Post ",
    "Install ",
    "Set up ",
    "Restore ",
    "Save ",
    "Cache ",
    "Disable unused Chrome apt source",
    "Identify ",
    "Hash ",
    "Configure Xcode",
    "Start simulator boot early",
    "Prepare ",
    "Download ",
    "Mint ",
    "Authenticate ",
    "Migrate disposable",
    "Select affected suites",
    "Keep per-test times",
    "Transfer fixture inputs",
    "Upload ",
)
# Steps a setup prefix would catch that are the job's actual work.
WORK_STEPS = ("Upload and distribute",)
WAIT_PREFIXES = ("Wait for ",)
PHASES = ("wait", "setup", "run")


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
    latest attempt. None when a required check had not finished by the merge:
    nobody waited for it.
    """
    merged = parse(merged_at)
    started = before_merge(checks, merged_at)
    if not started:
        return None
    finished = []
    for name in REQUIRED_CHECKS:
        attempts = [c for c in started if c["name"] == name]
        if not attempts:
            return None
        latest = max(attempts, key=lambda c: parse(c["started_at"]))
        if not latest.get("completed_at") or parse(latest["completed_at"]) > merged:
            return None
        finished.append(parse(latest["completed_at"]))
    first = min(parse(c["started_at"]) for c in started)
    return round((max(finished) - first).total_seconds() / 60, 1)


def main_ios_before(pushes, stamp):
    """Result of the last `iOS` run on `main` started before `stamp` that finished.

    PR runs test the merge with `main`, so while `main`'s iPhone build or tests
    are red every PR's Mac leg fails for a reason the PR did not cause.
    """
    earlier = [
        run
        for run in pushes
        if run["created_at"] < stamp and run["conclusion"] in ("success", "failure")
    ]
    if not earlier:
        return "unknown"
    return max(earlier, key=lambda run: run["created_at"])["conclusion"]


def pr_mac_minutes_by_main_state(run_jobs, pushes):
    """Failed and re-run PR Mac-minutes, by `main`'s iOS state when the run began."""
    split = {}
    for run, jobs in run_jobs:
        summary = mac_minutes(jobs)
        bucket = split.setdefault(
            main_ios_before(pushes, run["created_at"]), {"failure": 0, "rerun": 0}
        )
        bucket["failure"] += summary["minutes"].get("failure", 0)
        bucket["rerun"] += summary["rerun_minutes"]
    return split


def pr_used_mac(checks):
    return any(
        c["name"].startswith(MAC_CHECK_PREFIX)
        and c.get("conclusion") not in (None, "skipped")
        for c in checks
    )


def step_phase(name):
    """Which bucket a step's time belongs to: wait, setup or run."""
    if name.startswith(WAIT_PREFIXES):
        return "wait"
    if name.startswith(WORK_STEPS):
        return "run"
    if name in RUNNER_STEPS or ACTION_STEP.match(name) or name.startswith(SETUP_PREFIXES):
        return "setup"
    return "run"


def seconds_between(start, end):
    return round((parse(end) - parse(start)).total_seconds())


def timed_steps(job):
    """(name, phase, seconds) for each step that ran; skipped steps have no times."""
    for item in job.get("steps") or ():
        if item.get("started_at") and item.get("completed_at"):
            yield (
                item["name"],
                step_phase(item["name"]),
                max(0, seconds_between(item["started_at"], item["completed_at"])),
            )


def job_phases(job):
    """Seconds a job spent waiting, setting up and running. None if it did no work.

    Waiting starts at the job's own creation, not the run's start: a job behind
    `needs` is created when its dependencies finish. Steps that wait on another
    job count as waiting too.
    """
    if job.get("conclusion") not in ("success", "failure"):
        return None
    if not job.get("started_at") or not job.get("completed_at"):
        return None
    created = job.get("created_at") or job["started_at"]
    phases = {
        "wait": max(0, seconds_between(created, job["started_at"])),
        "setup": 0,
        "run": 0,
    }
    for _, phase, seconds in timed_steps(job):
        phases[phase] += seconds
    phases["total"] = max(0, seconds_between(job["started_at"], job["completed_at"]))
    return phases


def tails(values):
    return {"p50": percentile(values, 50), "p95": percentile(values, 95)}


def phase_table(jobs):
    """Per job name: how many ran, p50/p95 of each phase, and every step's phase."""
    phases, steps = defaultdict(list), defaultdict(lambda: defaultdict(list))
    for job in jobs:
        measured = job_phases(job)
        if measured is None:
            continue
        phases[job["name"]].append(measured)
        for name, phase, seconds in timed_steps(job):
            steps[job["name"]][(name, phase)].append(seconds)
    table = {}
    for name, rows in phases.items():
        table[name] = {"n": len(rows)}
        for phase in (*PHASES, "total"):
            table[name][phase] = tails([row[phase] for row in rows])
        table[name]["steps"] = sorted(
            (
                {"name": step, "phase": phase, "n": len(values)} | tails(values)
                for (step, phase), values in steps[name].items()
            ),
            key=lambda row: (-row["p50"], row["name"]),
        )
    return table


def runner_os(job):
    labels = [str(label) for label in job.get("labels") or ()]
    if any(label.startswith("macos") for label in labels):
        return "macos"
    if any(label.startswith("ubuntu") for label in labels):
        return "linux"
    return "other"


def runner_minutes(jobs):
    """Minutes jobs held a runner, by OS, part minutes rounded up."""
    minutes = Counter()
    for job in jobs:
        if job.get("conclusion") == "skipped":
            continue
        if not job.get("started_at") or not job.get("completed_at"):
            continue
        seconds = seconds_between(job["started_at"], job["completed_at"])
        if seconds >= 0:
            minutes[runner_os(job)] += max(1, math.ceil(seconds / 60))
    return dict(minutes)


def run_required_minutes(run, jobs):
    """Run start to the last required check finishing. None unless all finished."""
    finished = []
    for name in REQUIRED_CHECKS:
        done = [
            job
            for job in jobs
            if job["name"] == name
            and job.get("conclusion") in ("success", "failure")
            and job.get("completed_at")
        ]
        if not done:
            return None
        finished.append(max(parse(job["completed_at"]) for job in done))
    return round((max(finished) - parse(run["run_started_at"])).total_seconds() / 60, 1)


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


def origin_repo(root=ROOT):
    """owner/name of the checkout's `origin`, without spending an API call."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, check=True, cwd=root,
    ).stdout.strip()
    return repo_from_remote(url)


def repo_from_remote(url):
    path = url.removesuffix(".git").rstrip("/")
    for marker in ("github.com/", "github.com:"):
        if marker in path:
            name = path.split(marker, 1)[1]
            if name.count("/") == 1:
                return name
    raise RuntimeError(f"cannot read a GitHub owner/name from origin {url!r}; pass --repo")


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

    pushes = [r for r in runs if r["path"].endswith("/ios.yml") and r["event"] == "push"]
    by_main_state = pr_mac_minutes_by_main_state(
        [(run, jobs) for run, jobs in run_jobs if run["event"] == "pull_request"], pushes
    )

    waits = {"all": [], "mac": [], "no_mac": []}
    with_mac = unmeasured = 0
    rows = []
    for pr, checks in pr_checks:
        used = pr_used_mac(before_merge(checks, pr["merged_at"]))
        with_mac += used
        wait = feedback_minutes(checks, pr["merged_at"])
        rows.append({"number": pr["number"], "mac": used, "minutes": wait})
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
            "pr_minutes_by_main_ios_state": by_main_state,
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
            "rows": rows,
        },
    }


def measure_phases(repo, start, end, limit, workflow="ci.yml", event="pull_request", workers=8):
    """Waiting, setup and running per job over the newest finished runs in a window."""
    runs, page = [], 1
    while len(runs) < limit:
        batch = gh(
            f"repos/{repo}/actions/workflows/{workflow}/runs?event={event}"
            f"&status=completed&created={start}..{end}&per_page=100&page={page}",
            "[.workflow_runs[]|{id,conclusion,run_attempt,created_at,run_started_at}]",
            paginate=False,
        )[0]
        runs += [run for run in batch if run["conclusion"] in ("success", "failure")]
        if len(batch) < 100:
            break
        page += 1
    runs = runs[:limit]
    require_budget(int(len(runs) * 1.1) + 20)

    def jobs_for(run):
        return run, gh(
            f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=latest",
            ".jobs[]|{name,labels,conclusion,created_at,started_at,completed_at,"
            "steps:[.steps[]?|{name,conclusion,started_at,completed_at}]}",
        )

    with ThreadPoolExecutor(workers) as pool:
        run_jobs = list(pool.map(jobs_for, runs))

    waits = [run_required_minutes(run, jobs) for run, jobs in run_jobs]
    minutes = Counter()
    for _, jobs in run_jobs:
        minutes.update(runner_minutes(jobs))
    return {
        "repo": repo,
        "workflow": workflow,
        "event": event,
        "window": {"start": start, "end": end},
        "runs": {
            "measured": len(runs),
            "failed": sum(run["conclusion"] == "failure" for run in runs),
            "rerun": sum((run["run_attempt"] or 1) > 1 for run in runs),
            "oldest": min((run["created_at"] for run in runs), default=None),
            "newest": max((run["created_at"] for run in runs), default=None),
        },
        "minutes_to_last_required_check": tails([w for w in waits if w is not None])
        | {"n": sum(w is not None for w in waits)},
        "runner_minutes": dict(minutes),
        "jobs": phase_table([job for _, jobs in run_jobs for job in jobs]),
    }


def render_phases(report, step_floor=8):
    def pair(tail):
        return f"{tail['p50']} / {tail['p95']}"

    runs, wait = report["runs"], report["minutes_to_last_required_check"]
    measured = max(runs["measured"], 1)
    lines = [
        f"# CI job phases: {report['repo']} {report['workflow']} on {report['event']}",
        f"Runs: {runs['measured']} finished ({runs['failed']} failed, {runs['rerun']} re-run),"
        f" {runs['oldest']} to {runs['newest']}",
        f"Minutes from run start to last required check: median {wait['p50']},"
        f" p95 {wait['p95']} ({wait['n']} runs where all of them finished)",
        "Runner-minutes per run: "
        + ", ".join(
            f"{name} {total / measured:.0f}"
            for name, total in sorted(report["runner_minutes"].items())
        ),
        "",
        "Seconds, p50 / p95. Waiting is job creation to runner pickup plus steps that"
        " wait on another job.",
        "",
        "| Job | Runs | Waiting | Setup | Running | Whole job |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    ordered = sorted(report["jobs"].items(), key=lambda item: -item[1]["total"]["p50"])
    for name, row in ordered:
        lines.append(
            f"| {name} | {row['n']} | {pair(row['wait'])} | {pair(row['setup'])}"
            f" | {pair(row['run'])} | {pair(row['total'])} |"
        )
    lines += ["", f"## Steps with a median of {step_floor} s or more"]
    for name, row in ordered:
        slow = [item for item in row["steps"] if item["p50"] >= step_floor]
        if not slow:
            continue
        lines += ["", f"### {name}", "| Step | Filed as | Runs | p50 / p95 (s) |",
                  "| --- | --- | --- | --- |"]
        for item in slow:
            lines.append(
                f"| {item['name']} | {item['phase']} | {item['n']} | {pair(item)} |"
            )
    return "\n".join(lines) + "\n"


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
    for state, label in (("failure", "red"), ("success", "green"), ("unknown", "unknown")):
        split = mac["pr_minutes_by_main_ios_state"].get(state)
        if split:
            lines.append(
                f"- PR Mac-minutes while main's own iOS run was {label}:"
                f" {split['failure']} failed, {split['rerun']} on re-runs"
            )
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
    parser.add_argument(
        "--phases",
        type=int,
        metavar="RUNS",
        help="report waiting, setup and running per job over the newest RUNS"
        " finished runs in the window, instead of the baseline",
    )
    parser.add_argument("--workflow", default="ci.yml", help="workflow file for --phases")
    parser.add_argument("--event", default="pull_request", help="trigger for --phases")
    args = parser.parse_args(argv)
    repo = args.repo or origin_repo()
    end = parse(args.until) if args.until else datetime.now(timezone.utc)
    start = iso(end - timedelta(days=args.days))
    if args.phases:
        report = measure_phases(repo, start, iso(end), args.phases, args.workflow, args.event)
        text = render_phases(report)
    else:
        report = measure(repo, start, iso(end))
        text = render(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"error: {error}") from error
