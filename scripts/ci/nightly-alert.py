#!/usr/bin/env python3
"""Open, update or close the one issue that says a nightly run is red (KRI-1549).

The last job of the scheduled `CI` and `iOS` workflows runs this. It reads
every job of this run attempt from the GitHub API. When any job failed, was
cancelled (a timeout is a cancel) or had not finished, it opens one issue
titled "Nightly <workflow> run is red" with the `nightly-red` label, or
comments on the one already open, naming the failed jobs, their failed steps
and the failed tests read from their logs. A green run closes that issue.

Scheduled runs start hours late, so the text names the run's start time and
commit, never "last night". A run of any branch but the default one gets its
own title and no assignee, so a drill on a scratch branch never touches main's
issue. Contract and failure modes: scripts/ci/test_nightly_alert.py.

usage: nightly-alert.py [--dry-run] [--run-id ID] [--attempt N]
env:   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_JOB, GH_TOKEN,
       NIGHTLY_ALERT_ASSIGNEES (comma-separated logins, optional)
"""

import argparse
from datetime import datetime
import json
import os
import re
import subprocess
import sys
import urllib.parse

LABEL = "nightly-red"
GREEN = {"success", "skipped", "neutral"}
# GitHub refuses an issue or comment body over 65,536 characters.
MAX_BODY = 60_000
MAX_TESTS_PER_JOB = 25
MAX_LOGS = 10
RUNBOOK = "docs/runbooks/change-based-ci.md#when-a-nightly-goes-red-kri-1549"

TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", re.M)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# scripts/ios/ui_tests.py verify_results: the UI suite's own verdict.
COVERAGE = re.compile(
    r"UI coverage mismatch: missing=\[(.*?)\], not passed=\[(.*?)\], unexpected=\[(.*?)\]"
)
XCTEST = re.compile(r"^Test Case '-\[([\w.]+)\.(\w+) (\w+)\]' (passed|failed)", re.M)
PYTEST = re.compile(r"^(FAILED|ERROR) (\S+\.py\S*)", re.M)
QUOTED = re.compile(r"'([^']+)'")


def run_gh(args, stdin=None):
    return subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    ).stdout


def api(gh, path):
    return json.loads(gh(["api", path]))


def fetch_jobs(gh, repo, run_id, attempt):
    """Every job of this attempt, so a re-run is judged by its own results."""
    jobs, page = [], 1
    while True:
        data = api(gh, f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        jobs += data["jobs"]
        if len(data["jobs"]) < 100 or len(jobs) >= data["total_count"]:
            return jobs
        page += 1


def red_jobs(jobs, self_name):
    """(red, unfinished): skipped is green; anything still running is not."""
    red, unfinished = [], []
    for job in jobs:
        if job["name"] == self_name:
            continue
        if job.get("status") != "completed":
            unfinished.append(job)
        elif job.get("conclusion") not in GREEN:
            red.append(job)
    return red, unfinished


def failed_tests(log):
    """Tests a job log shows failing; a test that later passed was only flaky."""
    text = TIMESTAMP.sub("", ANSI.sub("", log.lstrip("\ufeff")))
    names, seen = [], set()

    def add(name, note=""):
        if name not in seen:
            seen.add(name)
            names.append(name + note)

    for missing, not_passed, unexpected in COVERAGE.findall(text):
        for name in QUOTED.findall(not_passed):
            add(name)
        for name in QUOTED.findall(missing):
            add(name, " (did not run)")
        for name in QUOTED.findall(unexpected):
            add(name, " (ran but was not selected)")
    failed, passed = [], set()
    for module, cls, test, outcome in XCTEST.findall(text):
        name = f"{module}/{cls}/{test}"
        if outcome == "passed":
            passed.add(name)
        else:
            failed.append(name)
    for name in failed:
        if name not in passed:
            add(name)
    for kind, name in PYTEST.findall(text):
        add(name, " (error)" if kind == "ERROR" else "")
    return names


def read_log(gh, repo, job_id):
    path = f"repos/{repo}/actions/jobs/{job_id}/logs"
    try:
        return gh(["api", path])
    except subprocess.CalledProcessError as error:
        # Newer gh (the runner's, 2026-11-20 drill) refuses to print a response
        # with terminal escape sequences, and every runner log has colour codes.
        if "--allow-escape-sequences" not in (error.stderr or ""):
            raise
        return gh(["api", "--allow-escape-sequences", path])


def job_tests(gh, repo, job):
    try:
        return failed_tests(read_log(gh, repo, job["id"])), None
    except (subprocess.CalledProcessError, OSError) as error:
        detail = (getattr(error, "stderr", "") or str(error)).strip().splitlines()
        return [], detail[-1] if detail else "unknown error"


def issue_title(workflow, branch, default_branch):
    title = f"Nightly {workflow} run is red"
    return title if branch == default_branch else f"{title} (branch {branch})"


def describe_run(run, repo):
    started = datetime.strptime(run["run_started_at"], "%Y-%m-%dT%H:%M:%SZ")
    sha = run["head_sha"]
    return (
        f"the scheduled **{run['name']}** run that started "
        f"{started:%Y-%m-%d %H:%M} UTC on `{run['head_branch']}` at commit "
        f"[`{sha[:9]}`](https://github.com/{repo}/commit/{sha}) "
        f"([run]({run['html_url']}), attempt {run['run_attempt']})"
    )


def failed_step(job):
    for step in job.get("steps") or ():
        if step.get("conclusion") not in GREEN | {None}:
            return step["name"]
    return None


def report(run, repo, red, unfinished, tests, cap=MAX_TESTS_PER_JOB):
    lines = [f"Red: {describe_run(run, repo)}.", ""]
    for job in red:
        step = failed_step(job)
        at = f' at "{step}"' if step else ""
        lines.append(f"- [`{job['name']}`]({job['html_url']}): {job['conclusion']}{at}")
        names, error = tests.get(job["id"], ([], None))
        if error:
            lines.append(f"  - log could not be read ({error})")
        for name in names[:cap]:
            lines.append(f"  - `{name}`")
        if len(names) > cap:
            lines.append(f"  - … and {len(names) - cap} more in the job log")
    for job in red[MAX_LOGS:]:
        lines.append(f"  - (log of `{job['name']}` not read: only the first {MAX_LOGS} red jobs are read)")
    for job in unfinished:
        lines.append(
            f"- `{job['name']}`: had not finished when this check ran "
            "(add it to the `nightly-alert` job's `needs`)"
        )
    lines += [
        "",
        "Each red scheduled run adds a comment here; the next green one closes "
        f"this issue. What to do: https://github.com/{repo}/blob/main/{RUNBOOK}",
    ]
    body = "\n".join(lines)
    if len(body) > MAX_BODY:
        if cap > 3:
            return report(run, repo, red, unfinished, tests, cap=3)
        body = body[: MAX_BODY - 40] + "\n\n… (cut to fit GitHub's limit)"
    return body


def main(argv, env=os.environ, gh=run_gh):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print, change nothing")
    parser.add_argument("--run-id", help="default: GITHUB_RUN_ID")
    parser.add_argument("--attempt", help="default: GITHUB_RUN_ATTEMPT, else the run's latest")
    args = parser.parse_args(argv)

    repo = env["GITHUB_REPOSITORY"]
    run_id = args.run_id or env["GITHUB_RUN_ID"]
    run = api(gh, f"repos/{repo}/actions/runs/{run_id}")
    # A re-run of an older night must not close or comment on the issue that a
    # newer night already reported: only the newest run of this kind speaks.
    # (--run-id previews any finished run, so it skips this.)
    query = urllib.parse.urlencode({"event": run["event"], "branch": run["head_branch"], "per_page": 1})
    newest = api(gh, f"repos/{repo}/actions/workflows/{run['workflow_id']}/runs?{query}")["workflow_runs"]
    if not args.run_id and newest and newest[0]["id"] != int(run_id):
        print(f"Run {run_id} is not the newest {run['event']} run of {run['name']} "
              f"(that is {newest[0]['id']}); it reports nothing.")
        return
    attempt = args.attempt or (None if args.run_id else env.get("GITHUB_RUN_ATTEMPT")) or run["run_attempt"]
    run["run_attempt"] = int(attempt)
    jobs = fetch_jobs(gh, repo, run_id, attempt)
    default_branch = api(gh, f"repos/{repo}")["default_branch"]
    title = issue_title(run["name"], run["head_branch"], default_branch)
    red, unfinished = red_jobs(jobs, env.get("GITHUB_JOB", ""))
    listed = json.loads(
        gh(["issue", "list", "--repo", repo, "--label", LABEL, "--state", "open",
            "--limit", "100", "--json", "number,title"])
    )
    open_issues = sorted(issue["number"] for issue in listed if issue["title"] == title)

    def write(verb, cmd, stdin=None):
        if args.dry_run:
            print(f"[dry run] would {verb}: gh {' '.join(cmd)}")
            if stdin:
                print(stdin)
            return ""
        return gh(cmd, stdin)

    if not red and not unfinished:
        if not open_issues:
            outcome = f"{run['name']} run is green; no open '{title}' issue."
        for number in open_issues:
            comment = f"Green again: {describe_run(run, repo)}. Closing."
            write("close", ["issue", "close", str(number), "--repo", repo, "--comment", comment])
            outcome = f"{run['name']} run is green; closed #{number}."
    else:
        tests = {job["id"]: job_tests(gh, repo, job) for job in red[:MAX_LOGS]}
        body = report(run, repo, red, unfinished, tests)
        if open_issues:
            number = open_issues[0]
            write("comment", ["issue", "comment", str(number), "--repo", repo, "--body-file", "-"], body)
            outcome = f"{run['name']} run is red; commented on #{number}."
        else:
            write("create the label", ["label", "create", LABEL, "--repo", repo, "--color", "B60205",
                                       "--description", "A scheduled CI or iOS run is red (KRI-1549)", "--force"])
            create = ["issue", "create", "--repo", repo, "--title", title, "--label", LABEL, "--body-file", "-"]
            assignees = env.get("NIGHTLY_ALERT_ASSIGNEES", "").strip(" ,")
            url = None
            if assignees and run["head_branch"] == default_branch:
                try:
                    url = write("open", [*create, "--assignee", assignees], body)
                except subprocess.CalledProcessError as error:
                    print(f"Could not assign {assignees} ({(error.stderr or '').strip()}); opening unassigned.",
                          file=sys.stderr)
            if url is None:
                url = write("open", create, body)
            outcome = f"{run['name']} run is red; opened {url.strip() or title}."
    print(outcome)
    if env.get("GITHUB_STEP_SUMMARY") and not args.dry_run:
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(f"{outcome}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
