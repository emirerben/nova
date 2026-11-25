#!/usr/bin/env python3
"""Run one deterministic, complete shard of the API pytest suite."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Six shards, packed by measured file time (KRI-1544). The count has a history:
# - Four shards split by file position (#2389, 2026-11-19) were reverted the
#   same day: position put test_phone_text_fit_never_fails.py beside the
#   carousel renders, and its 40-cue case went from 24-41 s to 53-60 s against
#   this runner's 60 s timeout, failing an unrelated PR.
# - Three shards packed by time: slowest shard job a median 987 s.
# - Six shards packed by time: 589 s, measured side by side with three on one
#   commit, five repeats each (2026-11-21).
# Before changing the count, run old and new side by side for five repeats and
# compare them with `api-times.py compare` (change-based-ci.md).
# test_api_tests.py keeps ci.yml's matrix equal to this count.
SHARD_COUNT = 6

# Seconds per test file, measured in CI (scripts/ci/api-test-timings.py writes
# it). Only the heavy files are listed; any other file weighs DEFAULT_SECONDS.
TIMINGS = Path(__file__).resolve().with_name("api-test-timings.json")
DEFAULT_SECONDS = 0.5
# A weight above this is a broken measurement, not a test file.
MAX_SECONDS = 3600
# Relative to src/apps/api, where pytest runs.
JUNIT = "test-results/api-junit.xml"
# Where tests/_ci_times.py writes one row per test; ci.yml uploads it.
TIMES = "test-results/ci-times"
# Every skip CI is allowed to report: a file, a pattern its reason must match,
# and why. A skip that matches no entry fails the shard, so a check cannot
# stop running without someone writing down the reason.
EXPECTED_SKIPS = Path(__file__).resolve().with_name("api-expected-skips.json")
# "1" gives every xdist worker its own copy of the migrated database
# (src/apps/api/tests/_worker_database.py). ci.yml sets it from the repository
# variable CI_API_DB_PER_WORKER, which is also the way back.
WORKER_DB_SWITCH = "NOVA_TEST_DB_PER_WORKER"
# Set for pytest once the copies exist: the URL they were copied from. conftest
# moves a worker only while its DATABASE_URL is still this one.
WORKER_DB_COPIED_FROM = "NOVA_TEST_DB_WORKER_COPIES_OF"


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
    # The finite range rejects NaN/infinity and bounds integers before conversion.
    return {
        path: float(seconds)
        for path, seconds in files.items()
        if isinstance(path, str)
        and isinstance(seconds, (int, float))
        and not isinstance(seconds, bool)
        and 0 < seconds <= MAX_SECONDS
    }


def partition(
    files: list[Path], seconds: dict[str, float], shard_count: int
) -> list[list[Path]]:
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

    selected = partition(test_files(api_root), file_seconds(timings), shard_count)[
        shard
    ]
    if not selected:
        raise ValueError(f"API test shard {shard} selected no test files")
    return selected


def worker_databases(environ: dict[str, str], cpu_count=None) -> int | None:
    """How many per-worker databases this run needs; None while the switch is off."""
    if environ.get(WORKER_DB_SWITCH) != "1":
        return None
    return (cpu_count or os.cpu_count)() or 1


def worker_database_command(workers: int) -> list[str]:
    return [sys.executable, "-m", "tests._worker_database", str(workers)]


def worker_database_names(api_root: Path, url: str, workers: int) -> dict[str, str]:
    """The database each xdist worker must have run on, from the module that makes the copies."""
    spec = importlib.util.spec_from_file_location(
        "_worker_database", api_root / "tests/_worker_database.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        f"gw{index}": module.worker_url(url, f"gw{index}").rsplit("/", 1)[1]
        for index in range(workers)
    }


def worker_database_problems(api_root: Path, expected: dict[str, str]) -> list[str]:
    """Workers that did not run on their own copy, from the recorder's startup rows.

    A worker's row names the database its loaded app was on once collection
    had finished (tests/_ci_times.py asks the app, not the environment). No row
    is a problem too: an unproven worker is not a proven one.
    """
    seen: dict[str, str | None] = {}
    for path in sorted((api_root / TIMES).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "startup":
                seen[row.get("worker")] = row.get("database")
    problems = []
    for worker, database in sorted(expected.items()):
        if worker not in seen:
            problems.append(f"{worker} left no startup row: nothing shows which database it used")
        elif seen[worker] != database:
            problems.append(f"{worker} ran on {seen[worker]!r}, not on its own copy {database!r}")
    return problems


def check_worker_databases(api_root: Path, url: str, workers: int) -> int:
    problems = worker_database_problems(api_root, worker_database_names(api_root, url, workers))
    if not problems:
        print(f"Worker databases: all {workers} workers ran on their own copy.")
        return 0
    print("::error::per-worker databases are switched on, but not every worker used its own.")
    for problem in problems:
        print(f"  {problem}")
    return 1


def pytest_command(selected: list[Path], workers: int | None = None) -> list[str]:
    """Keep the CI runner flags consistent across the isolated shards.

    With per-worker databases the worker count is the number of databases made;
    a worker beyond it would find none and stop.
    """
    return [
        "pytest",
        *(str(path) for path in selected),
        "-v",
        "--ignore=tests/quality",
        "-n",
        "auto" if workers is None else str(workers),
        "--timeout=60",
        "--durations=20",
        # Per-test times for scripts/ci/api-test-timings.py; ci.yml uploads it.
        f"--junitxml={JUNIT}",
    ]


def pytest_env(environ: dict[str, str]) -> dict[str, str]:
    """The shard's environment, with the per-test recorder switched on."""
    return {**environ, "NOVA_CI_TIMES_DIR": TIMES}


def expected_skips(path: Path = EXPECTED_SKIPS) -> list[tuple[str, re.Pattern[str]]]:
    """(file, reason pattern) pairs. Anything malformed raises: a list that
    cannot be read must not excuse a single skip."""
    try:
        entries = json.loads(path.read_text())["skips"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"cannot read expected skips from {path}: {error}") from error
    pairs = []
    for entry in entries:
        try:
            file, reason, why = entry["file"], entry["reason"], entry["why"]
            if not (isinstance(file, str) and file and isinstance(reason, str) and reason):
                raise ValueError("file and reason must be non-empty text")
            if not (isinstance(why, str) and why):
                raise ValueError("why must say why the skip is acceptable")
            pairs.append((file, re.compile(reason)))
        except (KeyError, TypeError, ValueError, re.error) as error:
            raise ValueError(f"bad expected-skip entry {entry!r}: {error}") from error
    return pairs


def clear_rows(api_root: Path) -> None:
    """Drop rows an earlier run left behind, so only this run's are judged."""
    shutil.rmtree(api_root / TIMES, ignore_errors=True)


def unexpected_skips(api_root: Path, expected: Path = EXPECTED_SKIPS) -> list[tuple[str, str]]:
    """Skipped tests of this shard that no expected-skip entry covers."""
    allowed = expected_skips(expected)
    rows = []
    for path in sorted((api_root / TIMES).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "test":
                rows.append(row)
    if not rows:
        raise ValueError(f"no per-test rows under {api_root / TIMES}; cannot check skips")
    return sorted(
        (row["nodeid"], row.get("reason", ""))
        for row in rows
        if row["outcome"] == "skipped"
        and not any(
            row["file"] == file and pattern.search(row.get("reason", ""))
            for file, pattern in allowed
        )
    )


def check_skips(api_root: Path, expected: Path = EXPECTED_SKIPS) -> int:
    try:
        unexpected = unexpected_skips(api_root, expected)
    except ValueError as error:
        print(f"::error::{error}")
        return 1
    if not unexpected:
        print("Skips: every skipped test is on scripts/ci/api-expected-skips.json.")
        return 0
    print(f"::error::{len(unexpected)} test(s) skipped that are not on the expected-skip list.")
    print(
        "A skipped test is a check that did not run. Make it run in CI, or add an entry to"
        " scripts/ci/api-expected-skips.json saying why it cannot."
    )
    for nodeid, reason in unexpected:
        print(f"  {nodeid}: {reason}")
    return 1


HISTORY_MARKER = "needs_git_history"


def history_files(api_root: Path) -> list[Path]:
    """Test files with a test marked as needing git history: those tests skip
    on the shards' one-commit clone, so they must also run where it exists."""
    return [
        path
        for path in test_files(api_root)
        if f"mark.{HISTORY_MARKER}" in (api_root / path).read_text(encoding="utf-8")
    ]


def history_command(api_root: Path, environ: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """pytest over the history files, with a missing commit made a failure."""
    files = history_files(api_root)
    if not files:
        raise ValueError("no test file asks for git history; the history job would check nothing")
    # Only the marked tests: this job lacks libraries other tests in the same
    # files need (skia's libEGL).
    command = ["pytest", *(str(path) for path in files), "-m", HISTORY_MARKER, "-q", "-rs"]
    return command, {**environ, "NOVA_REQUIRE_GIT_HISTORY": "1"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise ValueError("usage: api-tests.py SHARD | history")
    api_root = Path(__file__).resolve().parents[2] / "src/apps/api"
    if argv[0] == "history":
        command, env = history_command(api_root, dict(os.environ))
        print("Tests that need git history:", *command[1 : command.index("-m")])
        return subprocess.run(command, cwd=api_root, env=env, check=False).returncode
    try:
        shard = int(argv[0])
    except ValueError as error:
        raise ValueError(f"shard must be an integer, got {argv[0]!r}") from error

    selected = shard_files(api_root, shard)
    print(f"API test shard {shard}/{SHARD_COUNT}: {len(selected)} files")
    clear_rows(api_root)
    env = pytest_env(dict(os.environ))
    workers = worker_databases(env)
    if workers is not None:
        # Before pytest: Postgres will not copy a database that has open connections.
        made = subprocess.run(
            worker_database_command(workers), cwd=api_root, env=env, check=False
        ).returncode
        if made:
            print("error: the per-worker test databases were not created", file=sys.stderr)
            return made
        env[WORKER_DB_COPIED_FROM] = env["DATABASE_URL"]
    status = subprocess.run(
        pytest_command(selected, workers),
        cwd=api_root,
        env=env,
        check=False,
    ).returncode
    # Both checked even when pytest failed, so one run reports everything.
    skips = check_skips(api_root)
    proof = 0
    if workers is not None:
        proof = check_worker_databases(api_root, env[WORKER_DB_COPIED_FROM], workers)
    return status or skips or proof


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        raise SystemExit(f"error: {error}") from error
