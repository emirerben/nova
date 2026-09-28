#!/usr/bin/env python3
"""Rewrite ui-test-durations.json from recent GitHub Actions iOS runs.

Run from the repo root with an authenticated `gh`:

    python3 scripts/ios/refresh-ui-durations.py [--runs 120]

Each value is the median of the passing attempts of that test across the native
legs of the last N completed `iOS` workflow runs (main shards and PR subsets).
Failed attempts are ignored: retries are flakes, not the test's cost. Tests
without a sample keep their previous value; the file covers exactly the current
inventory. Durations only size and balance shards; they never change coverage.
"""

import argparse
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_tests

PASSED = re.compile(
    r"Test Case '-\[KriaUITests\.(\w+) (test\w+)\]' passed \(([\d.]+) seconds\)"
)


def passed_durations(log):
    """(test id, seconds) for every passing UI test attempt in an xcodebuild log."""
    return [
        (f"KriaUITests/{cls}/{name}", float(seconds))
        for cls, name, seconds in PASSED.findall(log)
    ]


def medians(samples, inventory, previous):
    """Median per inventory test; unsampled tests keep their previous value."""
    result = {}
    for test in sorted(inventory):
        if samples.get(test):
            result[test] = round(statistics.median(samples[test]), 1)
        elif test in previous:
            result[test] = previous[test]
    return result


def gh_json(*args):
    return json.loads(subprocess.check_output(["gh", *args]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=120)
    options = parser.parse_args()
    runs = gh_json(
        "run", "list", "--workflow", "ios.yml", "--limit", str(options.runs),
        "--json", "databaseId,conclusion",
    )
    samples, legs = {}, 0
    for run in runs:
        if run["conclusion"] not in ("success", "failure"):
            continue
        jobs = gh_json(
            "api", f"repos/{{owner}}/{{repo}}/actions/runs/{run['databaseId']}/jobs"
        )["jobs"]
        for job in jobs:
            if "native" not in job["name"] or job["conclusion"] not in ("success", "failure"):
                continue
            log = subprocess.run(
                ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job['id']}/logs"],
                capture_output=True,
            )
            if log.returncode:
                continue  # Expired logs are simply skipped.
            legs += 1
            for test, seconds in passed_durations(log.stdout.decode(errors="replace")):
                samples.setdefault(test, []).append(seconds)
    inventory = ui_tests.discovered()
    previous = json.loads(ui_tests.DURATIONS.read_text()) if ui_tests.DURATIONS.exists() else {}
    durations = medians(samples, inventory, previous)
    ui_tests.DURATIONS.write_text(json.dumps(durations, indent=2) + "\n")
    unsampled = sorted(inventory - samples.keys())
    print(f"Parsed {legs} native legs; {len(durations)} of {len(inventory)} tests recorded.")
    if unsampled:
        print("No samples (kept previous or median weight):\n  " + "\n  ".join(unsampled))


if __name__ == "__main__":
    main()
