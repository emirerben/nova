#!/usr/bin/env python3
"""PreToolUse hook: an agent may run `gh pr merge` only when every check on the
PR's current head has passed or was skipped. Failed, cancelled and still-running
checks block it; so does anything this script cannot read. `--auto` passes,
because GitHub then waits for the required checks itself.

A person can still merge by running the command themselves. Exit 2 blocks the
tool call and shows stderr to the agent.
"""

import json
import re
import shlex
import subprocess
import sys

OK = {"pass", "skipping"}


def merges(command):
    """Yield the argument list of every `gh pr merge` in a shell command."""
    for part in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        for i in range(len(words) - 2):
            if words[i : i + 3] == ["gh", "pr", "merge"]:
                yield words[i + 3 :]


def target(args):
    """The PR number, URL or branch the merge names, and its --repo if any."""
    repo, pr, skip = None, None, False
    for i, word in enumerate(args):
        if skip:
            skip = False
        elif word in ("-R", "--repo"):
            repo, skip = (args[i + 1] if i + 1 < len(args) else None), True
        elif word.startswith("--repo="):
            repo = word.split("=", 1)[1]
        elif word in (
            "-b",
            "--body",
            "-F",
            "--body-file",
            "-t",
            "--subject",
            "-A",
            "--author-email",
            "--match-head-commit",
        ):
            skip = True
        elif not word.startswith("-") and pr is None:
            pr = word
    return pr, repo


def blockers(pr, repo, cwd):
    cmd = ["gh", "pr", "checks", pr, "--json", "name,bucket"]
    if repo:
        cmd += ["--repo", repo]
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    try:
        checks = json.loads(done.stdout)
    except ValueError:
        return [
            f"could not read the checks: {(done.stderr or done.stdout).strip()[:200]}"
        ]
    if not checks:
        return ["the PR has no checks on its current head yet"]
    return sorted(
        f"{row['name']}: {row['bucket']}" for row in checks if row["bucket"] not in OK
    )


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    for args in merges(command):
        if "--auto" in args or "--disable-auto" in args:
            continue
        pr, repo = target(args)
        if not pr:
            print(
                "merge-guard: name the PR number in `gh pr merge <number>` so its checks can be read.",
                file=sys.stderr,
            )
            return 2
        try:
            bad = blockers(pr, repo, event.get("cwd") or None)
        except (OSError, subprocess.SubprocessError) as error:
            bad = [f"could not run gh: {error}"]
        if bad:
            print(
                f"merge-guard: PR {pr} is not green on its current head, so it was not merged.\n  "
                + "\n  ".join(bad)
                + "\nRun the ci-triage skill now (no need to ask): it separates flakes from real failures and gives the local repro."
                "\nWait for running checks. Fix a failure, or rerun the whole run if it was cancelled."
                " If main is red, merge the repair first. Do not retry this merge until every check passes;"
                " tell the user what is red. Only the user can merge past this, by running the command themselves.",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
