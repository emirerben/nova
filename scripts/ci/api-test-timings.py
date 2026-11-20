#!/usr/bin/env python3
"""Rewrite api-test-timings.json from the junit reports of API shard jobs.

    gh run download <run id> -p 'api-junit-*' -D /tmp/api-junit   # one or more runs
    python3 scripts/ci/api-test-timings.py /tmp/api-junit "run <id>, <date>"

Keep the artifact directories when downloading. For each run and shard, use
only the latest available attempt. A file's weight is the middle value
of its per-report totals, so one slow runner does not set it. Only files at or
above MIN_LISTED_SECONDS are written; api-tests.py weighs the rest at its
default. Prints what each shard would carry with the new weights.
"""

from __future__ import annotations

import collections
import importlib.util
import json
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("api_tests", HERE / "api-tests.py")
api_tests = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(api_tests)

MIN_LISTED_SECONDS = 5.0
ARTIFACT_NAME = re.compile(r"api-junit-([0-9]+)-([0-9]+)-([0-9]+)")


def latest_reports(reports: Path) -> list[Path]:
    """Prefer the latest downloaded attempt per run/shard; keep legacy reports."""
    chosen: dict[tuple[int, int], tuple[int, Path]] = {}
    legacy = []
    for report in sorted(reports.rglob("*.xml")):
        match = ARTIFACT_NAME.fullmatch(report.parent.name)
        if match is None:
            legacy.append(report)
            continue
        run_id, shard, attempt = map(int, match.groups())
        key = (run_id, shard)
        if key not in chosen or attempt > chosen[key][0]:
            chosen[key] = (attempt, report)
    return sorted([*legacy, *(report for _, report in chosen.values())])


def measure(reports: Path, files: list[Path]) -> tuple[dict[str, float], int]:
    """Seconds per file (middle of its per-report totals) and unowned cases."""
    modules = {
        path.with_suffix("").as_posix().replace("/", "."): path.as_posix()
        for path in files
    }
    totals: dict[str, list[float]] = collections.defaultdict(list)
    unowned = 0
    for report in latest_reports(reports):
        in_report: dict[str, float] = collections.defaultdict(float)
        for case in ET.parse(report).getroot().iter("testcase"):
            # junit gives `tests.pipeline.test_x.TestClass`: the owner is the
            # longest dotted prefix that is a test file.
            parts = (case.get("classname") or "").split(".")
            owner = next(
                (
                    modules[".".join(parts[:end])]
                    for end in range(len(parts), 0, -1)
                    if ".".join(parts[:end]) in modules
                ),
                None,
            )
            if owner is None:
                unowned += 1
                continue
            in_report[owner] += float(case.get("time") or 0)
        for path, seconds in in_report.items():
            totals[path].append(seconds)
    return {path: statistics.median(values) for path, values in totals.items()}, unowned


def payload(seconds: dict[str, float], measured: str) -> dict:
    return {
        "measured": measured,
        "files": {
            path: round(value, 1)
            for path, value in sorted(seconds.items())
            if value >= MIN_LISTED_SECONDS
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit(
            "usage: api-test-timings.py REPORT_DIR 'where and when measured'"
        )
    api_root = HERE.parents[1] / "src/apps/api"
    files = api_tests.test_files(api_root)
    seconds, unowned = measure(Path(argv[0]), files)
    if not seconds:
        raise SystemExit("error: no junit report named a known API test file")
    api_tests.TIMINGS.write_text(json.dumps(payload(seconds, argv[1]), indent=1) + "\n")
    listed = api_tests.file_seconds()
    print(
        f"{len(seconds)} of {len(files)} files measured, {len(listed)} listed, {unowned} cases unowned"
    )
    for index, shard in enumerate(
        api_tests.partition(files, listed, api_tests.SHARD_COUNT)
    ):
        load = sum(seconds.get(path.as_posix(), 0) for path in shard)
        print(f"shard {index}: {len(shard)} files, {load:.0f} measured seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
