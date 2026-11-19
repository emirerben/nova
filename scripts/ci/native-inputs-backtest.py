#!/usr/bin/env python3
"""Replay the PR Mac rule over real PR runs and list what it would have missed.

The rule (select-tests.py `native_input`): a PR needs a Mac only when it changes
an iPhone build or test input. For every pull-request `CI` run in the window this
recomputes the rule from the PR's diff and checks what the Mac leg really did on
the runs the rule would have skipped. A Mac failure there that no re-run turned
green is either an inherited break on `main` or a path the rule does not know:
read each one before the rule is allowed to skip anything (KRI-1534).

Usage:
  python3 scripts/ci/native-inputs-backtest.py [--days 14] [--until ISO] [--json PATH]

Stdlib only; needs an authenticated `gh`. Read-only: every call is a GET.
Guards: scripts/ci/test_native_inputs_backtest.py.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
MAC_JOB_PREFIX = "iPhone validation"
# GitHub's compare API returns at most this many files.
COMPARE_FILE_CAP = 300


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


selector = load("select-tests")
baseline = load("ci-baseline")


def changed_paths(files):
    """Both sides of every change; the selector also disables rename detection."""
    paths = []
    for entry in files:
        paths.append(entry["filename"])
        if entry.get("previous_filename"):
            paths.append(entry["previous_filename"])
    return paths


def would_skip(paths, truncated, manifest_edit=None):
    """Whether the rule would start no Mac. Unknown diffs never skip.

    `manifest_edit` is select-tests.py `selectable_manifest_edit` for a diff
    that touches the journey manifest, or None when it could not be read or is
    not a plain row change: the manifest then selects a Mac like any policy path.
    """
    if truncated or not paths:
        return False
    for path in paths:
        if path == selector.JOURNEY_MANIFEST and manifest_edit is not None:
            if manifest_edit[1]:
                return False
            continue
        if selector.native_input(path):
            return False
    return True


def decode_manifest(content):
    """A manifest from the contents API's base64 body, or None."""
    if not content.strip():
        return None
    try:
        return json.loads(base64.b64decode(content, validate=False))
    except (binascii.Error, ValueError):
        return None


def fetch_manifest(repo, ref):
    """The journey manifest at one commit, or None when it cannot be read."""
    try:
        # A JSON object, not the bare string: baseline.gh parses each line.
        body = baseline.gh(
            f"repos/{repo}/contents/{selector.JOURNEY_MANIFEST}?ref={ref}",
            "{content}",
            paginate=False,
        )
    except RuntimeError:
        return None
    return decode_manifest(body[0].get("content") or "") if body else None


def mac_outcome(jobs):
    """What the Mac legs of one run proved, across all attempts."""
    legs = {}
    for job in jobs:
        if job["name"].startswith(MAC_JOB_PREFIX) and job["conclusion"] != "skipped":
            legs.setdefault(job["name"], []).append(job)
    if not legs:
        return {"verdict": "not_run", "failed_steps": []}
    verdicts, failed_steps = set(), []
    for attempts in legs.values():
        attempts.sort(key=lambda job: job.get("run_attempt") or 1)
        results = [job["conclusion"] for job in attempts]
        if "failure" in results:
            later = results[results.index("failure") + 1 :]
            if "success" in later:
                verdicts.add("flake")
            else:
                verdicts.add("failed")
                for job in attempts:
                    failed_steps += job.get("failed_steps") or []
        elif "success" in results:
            verdicts.add("passed")
        else:
            verdicts.add("cancelled")
    for verdict in ("failed", "flake", "cancelled", "passed"):
        if verdict in verdicts:
            return {"verdict": verdict, "failed_steps": sorted(set(failed_steps))}
    raise AssertionError("unreachable")


def backtest(repo, start, end, workers=8):
    every = baseline.list_runs(repo, start, end)
    pushes = [
        run
        for run in every
        if run["path"] == ".github/workflows/ios.yml" and run["event"] == "push"
    ]
    runs = [
        run
        for run in every
        if run["path"] == ".github/workflows/ci.yml"
        and run["event"] == "pull_request"
        and run["status"] == "completed"
    ]

    # One compare query per run, two manifest reads for each run that edits
    # it (about a third), and a jobs query for each run the rule skips.
    baseline.require_budget(int(len(runs) * 2.2) + 20)

    def inspect(run):
        compared = baseline.gh(
            f"repos/{repo}/compare/main...{run['head_sha']}?per_page={COMPARE_FILE_CAP}",
            "{base:.merge_base_commit.sha,files:[.files[]|{filename,previous_filename}]}",
            paginate=False,
        )
        compared = compared[0] if compared else {"base": None, "files": []}
        files = compared["files"]
        paths = changed_paths(files)
        edit = None
        if selector.JOURNEY_MANIFEST in paths and compared["base"]:
            sides = [
                fetch_manifest(repo, ref) for ref in (compared["base"], run["head_sha"])
            ]
            if None not in sides:
                edit = selector.selectable_manifest_edit(*sides)
        skip = would_skip(paths, len(files) >= COMPARE_FILE_CAP, edit)
        # Only a run the rule would skip needs its Mac outcome read.
        jobs = (
            baseline.gh(
                f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=all",
                ".jobs[]|{name,conclusion,run_attempt,"
                'failed_steps:[.steps[]?|select(.conclusion=="failure")|.name]}',
            )
            if skip
            else []
        )
        return {
            "run": run["id"],
            "sha": run["head_sha"],
            "created_at": run["created_at"],
            "would_skip": skip,
            "manifest": (
                None
                if selector.JOURNEY_MANIFEST not in paths
                else "everything"
                if edit is None
                else "native rows"
                if edit[1]
                else "linux rows"
            ),
            "api_file_swift_reads": any(
                path.startswith("src/apps/api/") and selector.declared_native_input(path)
                for path in paths
            ),
            "main_ios": baseline.main_ios_before(pushes, run["created_at"]),
            **mac_outcome(jobs),
        }

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(inspect, runs))
    skipped = [r for r in results if r["would_skip"]]
    return {
        "repo": repo,
        "window": {"start": start, "end": end},
        "pr_runs": len(results),
        "changed_api_file_swift_reads": sum(r["api_file_swift_reads"] for r in results),
        "manifest_edits": dict(Counter(r["manifest"] for r in results if r["manifest"])),
        "would_skip": len(skipped),
        "would_skip_mac_ran": sum(r["verdict"] != "not_run" for r in skipped),
        "would_skip_verdicts": dict(Counter(r["verdict"] for r in skipped)),
        "to_read": [r for r in skipped if r["verdict"] == "failed"],
        "rows": results,
    }


def render(report):
    lines = [
        f"# Mac rule backtest: {report['repo']}",
        f"Window: {report['window']['start']} to {report['window']['end']}",
        "",
        f"- Pull-request CI runs: {report['pr_runs']}",
        f"- Runs that changed an API font or fixture Swift tests open:"
        f" {report['changed_api_file_swift_reads']}",
        "- Runs that edit the journey manifest, by what the edit selects: "
        + (
            ", ".join(f"{v} {k}" for k, v in sorted(report["manifest_edits"].items()))
            or "none"
        ),
        f"- Runs the rule would give no Mac: {report['would_skip']}"
        f" (a Mac really ran on {report['would_skip_mac_ran']} of them)",
    ]
    for verdict, count in sorted(report["would_skip_verdicts"].items()):
        lines.append(f"  - Mac {verdict}: {count}")
    red_main = sum(entry["main_ios"] == "failure" for entry in report["to_read"])
    lines += [
        "",
        f"## Failures to read before the rule may skip: {len(report['to_read'])}"
        f" ({red_main} while main's own iOS run was already red)",
    ]
    for entry in report["to_read"]:
        lines.append(
            f"- https://github.com/{report['repo']}/actions/runs/{entry['run']}"
            f" ({entry['sha'][:9]}, main iOS {entry['main_ios']} just before):"
            f" {'; '.join(entry['failed_steps']) or 'no failed step recorded'}"
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="owner/name; defaults to the current repository")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--until", help="window end, ISO-8601 UTC; defaults to now")
    parser.add_argument("--json", help="also write the raw result to this path")
    args = parser.parse_args(argv)
    repo = args.repo or baseline.origin_repo()
    end = baseline.parse(args.until) if args.until else datetime.now(timezone.utc)
    report = backtest(repo, baseline.iso(end - timedelta(days=args.days)), baseline.iso(end))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"error: {error}") from error
