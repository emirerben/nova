#!/usr/bin/env python3
"""Run one deterministic, complete shard of the API pytest suite."""

from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys


# Three shards, packed by measured file time (KRI-1544). Four shards split by
# file position were tried on 2026-11-19 (#2389) and reverted the same day:
# position put test_phone_text_fit_never_fails.py beside the carousel renders
# and other CPU-heavy tests, and its 40-cue case went from 24-41 s to 53-60 s
# against this runner's 60 s timeout, failing an unrelated PR. Before raising
# the count, run the old and the new layout side by side for several repeats
# and compare every slow test against the timeout (change-based-ci.md).
# test_api_tests.py keeps ci.yml's matrix equal to this count.
SHARD_COUNT = 3

# Seconds per test file, measured in CI (scripts/ci/api-test-timings.py writes
# it). Only the heavy files are listed; any other file weighs DEFAULT_SECONDS.
TIMINGS = Path(__file__).resolve().with_name("api-test-timings.json")
DEFAULT_SECONDS = 0.5
# A weight above this is a broken measurement, not a test file.
MAX_SECONDS = 3600
# Relative to src/apps/api, where pytest runs.
JUNIT = "test-results/api-junit.xml"


def test_files(api_root: Path) -> list[Path]:
    """Return every normal API test file, excluding the separate quality suite."""
    tests_root = api_root / "tests"
    return sorted(
        path.relative_to(api_root)
        for path in tests_root.rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and path.relative_to(tests_root).parts[0] != "quality"
    )


def file_seconds(timings: Path = TIMINGS) -> dict[str, float]:
    """Measured seconds by posix path. Anything unusable is dropped, so a bad
    timings file can only make the shards uneven, never lose a test file."""
    try:
        files = json.loads(timings.read_text())["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(files, dict):
        return {}
    return {
        path: float(seconds)
        for path, seconds in files.items()
        if isinstance(path, str)
        and isinstance(seconds, (int, float))
        and not isinstance(seconds, bool)
        and math.isfinite(seconds)
        and 0 < seconds <= MAX_SECONDS
    }


def partition(files: list[Path], seconds: dict[str, float], shard_count: int) -> list[list[Path]]:
    """Pack files longest first into the lightest shard.

    Every shard job computes this on its own runner, so it must depend on
    nothing but its arguments: weights are whole milliseconds and every tie
    is broken by path or shard number. Each shard is returned in path order,
    because pytest collects in argument order and xdist hands tests out in
    collection order; heaviest-first would start every worker on the heaviest
    files together.
    """
    weights = {
        path: round(seconds.get(path.as_posix(), DEFAULT_SECONDS) * 1000)
        for path in files
    }
    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    for path in sorted(files, key=lambda path: (-weights[path], path.as_posix())):
        lightest = min(range(shard_count), key=lambda index: (loads[index], index))
        shards[lightest].append(path)
        loads[lightest] += weights[path]
    return [sorted(shard) for shard in shards]


def shard_files(
    api_root: Path,
    shard: int,
    shard_count: int = SHARD_COUNT,
    timings: Path = TIMINGS,
) -> list[Path]:
    """One shard of the suite, failing closed on bad input."""
    if shard_count != SHARD_COUNT:
        raise ValueError(f"shard count must be {SHARD_COUNT}, got {shard_count}")
    if not 0 <= shard < shard_count:
        raise ValueError(f"shard must be in [0, {shard_count - 1}], got {shard}")

    selected = partition(test_files(api_root), file_seconds(timings), shard_count)[shard]
    if not selected:
        raise ValueError(f"API test shard {shard} selected no test files")
    return selected


def pytest_command(selected: list[Path]) -> list[str]:
    """Keep the CI runner flags consistent across the isolated shards."""
    return [
        "pytest",
        *(str(path) for path in selected),
        "-v",
        "--ignore=tests/quality",
        "-n",
        "auto",
        "--timeout=60",
        "--durations=20",
        # Per-test times for scripts/ci/api-test-timings.py; ci.yml uploads it.
        f"--junitxml={JUNIT}",
    ]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise ValueError("usage: api-tests.py SHARD")
    try:
        shard = int(argv[0])
    except ValueError as error:
        raise ValueError(f"shard must be an integer, got {argv[0]!r}") from error

    api_root = Path(__file__).resolve().parents[2] / "src/apps/api"
    selected = shard_files(api_root, shard)
    print(f"API test shard {shard}/{SHARD_COUNT}: {len(selected)} files")
    return subprocess.run(pytest_command(selected), cwd=api_root, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        raise SystemExit(f"error: {error}") from error
