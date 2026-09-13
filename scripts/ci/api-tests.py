#!/usr/bin/env python3
"""Run one deterministic, complete shard of the API pytest suite."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


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
    """Keep the CI runner flags consistent across both isolated shards."""
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
