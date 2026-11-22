#!/usr/bin/env python3
"""Paths whose change makes a `main` commit a new TestFlight build (stdlib only).

Defined once and read by both sides (KRI-1547):
- testflight.yml diffs these between the last uploaded commit and the tested
  one, and skips the upload when none changed. It reads this file from the
  workflow's policy revision and runs it to print one path per line.
- the iOS main-push selector (scripts/ci/select-tests.py `main_push_input`)
  starts the Mac lane, which is what dispatches TestFlight, when one of these
  or an iPhone build or test input changed since the last green push run.

Entries are git pathspecs: a directory covers everything under it.
"""

TESTFLIGHT_RELEASE_INPUTS = (
    "src/apps/ios",
    "scripts/ios",
    ".github/workflows/ios.yml",
    ".github/workflows/testflight.yml",
    ".claude/worktrees",
    "src/apps/api/app/config.py",
    "src/apps/api/app/services/mobile_auth.py",
)


def release_input(path):
    """Whether a changed repo path is a TestFlight release input."""
    return any(
        path == spec or path.startswith(spec + "/")
        for spec in TESTFLIGHT_RELEASE_INPUTS
    )


if __name__ == "__main__":
    print("\n".join(TESTFLIGHT_RELEASE_INPUTS))
