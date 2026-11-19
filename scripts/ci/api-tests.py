#!/usr/bin/env python3
"""Run one deterministic, complete shard of the API pytest suite."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


# Two shards. Four were tried on 2026-11-19 (#2389) and reverted the same day:
# round-robin put test_phone_text_fit_never_fails.py in a shard with the
# carousel renders and other CPU-heavy tests, and its 40-cue case went from
# 24-41 s to 53-60 s against this runner's 60 s timeout, failing an unrelated
# PR. The slowest job only got 3-4 minutes faster. Raise the count again only
# with shards balanced on measured per-file time, and check the slowest tests
# against the timeout over several runs first. test_api_tests.py keeps
# ci.yml's matrix equal to this count, so a shard can never be left out.
SHARD_COUNT = 2


def test_files(api_root: Path) -> list[Path]:
    """Return every normal API test file, excluding the separate quality suite."""
    tests_root = api_root / "tests"
    return sorted(
        path.relative_to(api_root)
        for path in tests_root.rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and path.relative_to(tests_root).parts[0] != "quality"
    )


def shard_files(api_root: Path, shard: int, shard_count: int = SHARD_COUNT) -> list[Path]:
    """Partition sorted test files by round-robin position, failing closed on bad input."""
    if shard_count != SHARD_COUNT:
        raise ValueError(f"shard count must be {SHARD_COUNT}, got {shard_count}")
    if not 0 <= shard < shard_count:
        raise ValueError(f"shard must be in [0, {shard_count - 1}], got {shard}")

    selected = test_files(api_root)[shard::shard_count]
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
