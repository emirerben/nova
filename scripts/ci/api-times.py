#!/usr/bin/env python3
"""Report where the API suite's time goes, from the rows tests/_ci_times.py wrote.

    gh run download <run id> -p 'api-times-*' -D /tmp/api-times   # one or more runs
    python3 scripts/ci/api-times.py /tmp/api-times [--json out.json]

Keep the artifact directories when downloading. For each run and shard only the
latest attempt is read. With several runs, a file's or a test's time is the
middle run, so one slow runner does not set it. Outcomes and skips are those of
the newest run. Stdlib only. Guard: scripts/ci/test_api_times.py.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

ARTIFACT_NAME = re.compile(r"api-times-([0-9]+)-([0-9]+)-([0-9]+)")
PHASES = ("setup", "call", "teardown")
SLOW_FILE_SECONDS = 20
SLOW_TEST_SECONDS = 10


def load(root: Path) -> list[dict]:
    """One entry per run and shard: its rows and how many lines were unreadable."""
    chosen: dict[tuple[str, str], tuple[int, Path]] = {}
    for directory in sorted({path.parent for path in root.rglob("*.jsonl")}):
        match = ARTIFACT_NAME.fullmatch(directory.name)
        run, shard, attempt = match.groups() if match else ("local", directory.name, "0")
        key = (run, shard)
        if key not in chosen or int(attempt) > chosen[key][0]:
            chosen[key] = (int(attempt), directory)
    shards = []
    for (run, shard), (_, directory) in sorted(chosen.items()):
        rows, unreadable = [], 0
        for path in sorted(directory.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    unreadable += 1
        shards.append({"run": run, "shard": shard, "rows": rows, "unreadable": unreadable})
    return shards


def middle(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def summarize(shards: list[dict]) -> dict:
    runs = sorted({shard["run"] for shard in shards})
    files: dict[str, dict[str, float]] = collections.defaultdict(lambda: collections.Counter())
    tests: dict[str, dict[str, float]] = collections.defaultdict(dict)
    cpu: dict[str, dict[str, float]] = collections.defaultdict(dict)
    phases = {run: collections.Counter() for run in runs}
    seen: dict[str, collections.Counter] = {run: collections.Counter() for run in runs}
    startup: dict[str, dict] = {}
    newest = runs[-1] if runs else None
    outcomes: collections.Counter = collections.Counter()
    skipped, xfailed = {}, {}
    for shard in shards:
        run = shard["run"]
        for row in shard["rows"]:
            if row.get("kind") == "startup":
                entry = startup.setdefault(f"{run}/{shard['shard']}", {})
                entry[row["worker"]] = row["seconds"]
                entry["items"] = row["items"]
                continue
            if row.get("kind") != "test":
                continue
            node = row["nodeid"]
            seconds = sum(row.get(phase, 0) for phase in PHASES)
            seen[run][node] += 1
            files[row["file"]][run] += seconds
            tests[node][run] = tests[node].get(run, 0) + seconds
            cpu[node][run] = cpu[node].get(run, 0) + row.get("cpu", 0)
            for phase in PHASES:
                phases[run][phase] += row.get(phase, 0)
            if run == newest:
                outcomes[row["outcome"]] += 1
                if row["outcome"] == "skipped":
                    skipped[node] = row.get("reason", "")
                elif row["outcome"] == "xfailed":
                    xfailed[node] = row.get("reason", "")
    return {
        "runs": len(runs),
        "unreadable_lines": sum(shard["unreadable"] for shard in shards),
        "ran_twice": sorted({n for run in runs for n, count in seen[run].items() if count > 1}),
        "files": {path: middle(list(by_run.values())) for path, by_run in files.items()},
        "tests": {node: middle(list(by_run.values())) for node, by_run in tests.items()},
        "cpu": {node: middle(list(by_run.values())) for node, by_run in cpu.items()},
        "phase_seconds": {
            phase: middle([phases[run][phase] for run in runs]) if runs else 0
            for phase in PHASES
        },
        "startup": startup,
        "outcomes": dict(sorted(outcomes.items())),
        "skipped": dict(sorted(skipped.items())),
        "xfailed": dict(sorted(xfailed.items())),
    }


def render(report: dict, top: int = 25) -> str:
    files, tests, cpu = report["files"], report["tests"], report["cpu"]
    phase = report["phase_seconds"]
    wall = sum(tests.values())
    slow_files = sorted((p for p in files if files[p] > SLOW_FILE_SECONDS), key=lambda p: -files[p])
    slow_tests = sorted((n for n in tests if tests[n] > SLOW_TEST_SECONDS), key=lambda n: -tests[n])
    lines = [
        f"# API suite times: {report['runs']} run(s), {len(files)} files, {len(tests)} tests",
        f"Outcomes (newest run): {report['outcomes']}",
        f"Unreadable lines: {report['unreadable_lines']}; tests that ran twice in one run:"
        f" {len(report['ran_twice'])}",
        f"Test seconds: setup {phase['setup']:.0f}, call {phase['call']:.0f},"
        f" teardown {phase['teardown']:.0f}; CPU {sum(cpu.values()):.0f}"
        f" ({sum(cpu.values()) / wall:.0%} of wall)" if wall else "No test rows.",
        "",
        "## Seconds before the first test, per worker",
    ]
    for name, entry in sorted(report["startup"].items()):
        workers = ", ".join(f"{w} {s:.0f}" for w, s in sorted(entry.items()) if w != "items")
        lines.append(f"- run/shard {name}: {workers} ({entry['items']} tests collected)")
    lines += ["", f"## Files over {SLOW_FILE_SECONDS} s: {len(slow_files)}"]
    lines += [f"- {files[path]:7.1f}  {path}" for path in slow_files[:top]]
    lines += ["", f"## Tests over {SLOW_TEST_SECONDS} s: {len(slow_tests)}"]
    for node in slow_tests[:top]:
        share = cpu[node] / tests[node] if tests[node] else 0
        lines.append(f"- {tests[node]:7.1f}  CPU {share:.0%}  {node}")
    lines += ["", f"## Skipped (newest run): {len(report['skipped'])}"]
    reasons = collections.Counter(report["skipped"].values())
    lines += [f"- {count} x {reason or '(no reason)'}" for reason, count in reasons.most_common()]
    lines += ["", f"## Expected failures (newest run): {len(report['xfailed'])}"]
    lines += [f"- {node}: {reason}" for node, reason in report["xfailed"].items()]
    if report["ran_twice"]:
        lines += ["", "## Ran twice in one run"] + [f"- {n}" for n in report["ran_twice"][:top]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--json", type=Path, help="also write the full summary here")
    args = parser.parse_args(argv)
    shards = load(args.directory)
    if not shards:
        raise SystemExit(f"error: no *.jsonl rows under {args.directory}")
    report = summarize(shards)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
