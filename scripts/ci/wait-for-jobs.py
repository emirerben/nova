#!/usr/bin/env python3
"""Wait inside a job for sibling jobs of the same workflow run (stdlib + gh).

The iPhone validation legs used to `needs:` the portable journey jobs, so every
Mac sat idle until journey-offline finished (~11 min, 2026-10-27..30) before it
even queued for a runner. The legs now start with the selector and compile
while the Linux journeys run; this script blocks only at the step that consumes
their fixture bundle. It fails closed: a sibling that ends in anything other
than success, never appears, or outlasts the deadline fails the leg.

usage: wait-for-jobs.py JOB [JOB ...]   (env: GITHUB_REPOSITORY, GITHUB_RUN_ID)
"""

import json
import os
import subprocess
import sys
import time

POLL_SECONDS = 15
# journey-offline's own timeout is 30 min and journey-library's 80; a leg that
# waits longer than this has a stuck sibling and should say so, not spin.
DEADLINE_SECONDS = 45 * 60


def fetch_jobs(repo, run_id):
    """Latest attempt of every job in this run, keyed by job name."""
    raw = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
        ]
    )
    return {job["name"]: job for job in json.loads(raw)["jobs"]}


def check(jobs, names):
    """Return (done, failure message). Raises nothing; missing jobs are pending."""
    pending = []
    for name in names:
        job = jobs.get(name)
        if job is None or job.get("status") != "completed":
            pending.append(name)
            continue
        if job.get("conclusion") != "success":
            return True, f"{name} finished {job.get('conclusion')}; this leg cannot use its output"
    return not pending, ""


def wait(names, fetch, sleep=time.sleep, clock=time.monotonic, deadline=DEADLINE_SECONDS):
    started = clock()
    while True:
        try:
            done, failure = check(fetch(), names)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
            # A transient API error is not a verdict; the deadline still bounds it.
            print(f"Job status unavailable ({error}); retrying", file=sys.stderr)
            done, failure = False, ""
        if failure:
            raise SystemExit(failure)
        if done:
            print(f"Ready after {int(clock() - started)}s: {', '.join(names)}")
            return
        if clock() - started > deadline:
            raise SystemExit(f"Gave up after {deadline}s waiting for {', '.join(names)}")
        sleep(POLL_SECONDS)


def main(argv):
    names = [name for name in argv if name]
    if not names:
        raise SystemExit(__doc__)
    repo, run_id = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"]
    wait(names, lambda: fetch_jobs(repo, run_id))


if __name__ == "__main__":
    main(sys.argv[1:])
