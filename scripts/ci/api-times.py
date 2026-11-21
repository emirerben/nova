#!/usr/bin/env python3
"""Report where the API suite's time goes, from the rows tests/_ci_times.py wrote.

    gh run download <run id> -p 'api-times-*' -D /tmp/api-times   # one or more runs
    python3 scripts/ci/api-times.py /tmp/api-times [--json out.json]
    python3 scripts/ci/api-times.py compare /tmp/old /tmp/new   # before a CI switch

Keep the artifact directories when downloading. For each run and shard only the
latest attempt is read. With several runs, a file's or a test's time is the
middle run, so one slow runner does not set it. Outcomes and skips are those of
the newest run. Stdlib only. Guard: scripts/ci/test_api_times.py.

`compare` is the side-by-side check for a CI change (install, shard layout,
runner): OLD and NEW each hold the rows of one or more runs. It fails when a
test stopped running, an outcome changed, a test newly reaches the 60 s
timeout, or more samples per run sit above 45 s than before. Every attempt of
a shard counts as a sample. Tests only in NEW are listed, not failed: the
arms are usually a few commits apart.
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
# The suite's per-test timeout in CI, and the margin the layout bar watches
# (docs/runbooks/change-based-ci.md).
TIMEOUT_SECONDS = 60
NEAR_TIMEOUT_SECONDS = 45


def load(root: Path, attempts_as_runs: bool = False) -> list[dict]:
    """One entry per run and shard: its rows and how many lines were unreadable.

    By default only the latest attempt of a run and shard is kept. With
    `attempts_as_runs` every attempt is its own entry, for `compare`.
    """
    chosen: dict[tuple[str, str, str], tuple[int, Path]] = {}
    for directory in sorted({path.parent for path in root.rglob("*.jsonl")}):
        match = ARTIFACT_NAME.fullmatch(directory.name)
        run, shard, attempt = match.groups() if match else ("local", directory.name, "0")
        key = (run, shard, attempt if attempts_as_runs else "")
        if key not in chosen or int(attempt) > chosen[key][0]:
            chosen[key] = (int(attempt), directory)
    shards = []
    for (run, shard, _), (_, directory) in sorted(chosen.items()):
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


def arm_samples(shards: list[dict]) -> dict:
    """Per test: every outcome and every whole-test duration seen in one arm."""
    outcomes: dict[str, set] = collections.defaultdict(set)
    seconds: dict[str, list[float]] = collections.defaultdict(list)
    for shard in shards:
        for row in shard["rows"]:
            if row.get("kind") != "test":
                continue
            outcomes[row["nodeid"]].add(row["outcome"])
            seconds[row["nodeid"]].append(sum(row.get(phase, 0) for phase in PHASES))
    distinct_shards = len({shard["shard"] for shard in shards}) or 1
    return {
        "outcomes": outcomes,
        "seconds": seconds,
        "samples": len(shards),
        # A re-run of one shard adds samples, not a whole run.
        "runs": len(shards) / distinct_shards,
    }


def compare(old_shards: list[dict], new_shards: list[dict]) -> dict:
    old, new = arm_samples(old_shards), arm_samples(new_shards)
    shared = sorted(set(old["outcomes"]) & set(new["outcomes"]))

    def above(arm, limit):
        return sum(value > limit for values in arm["seconds"].values() for value in values)

    result = {
        "samples": {"old": old["samples"], "new": new["samples"]},
        "tests": {"old": len(old["outcomes"]), "new": len(new["outcomes"])},
        "only_old": sorted(set(old["outcomes"]) - set(new["outcomes"])),
        "only_new": sorted(set(new["outcomes"]) - set(old["outcomes"])),
        "outcome_changes": {
            node: {"old": sorted(old["outcomes"][node]), "new": sorted(new["outcomes"][node])}
            for node in shared
            if old["outcomes"][node] != new["outcomes"][node]
        },
        "newly_at_timeout": sorted(
            node
            for node, values in new["seconds"].items()
            if max(values) >= TIMEOUT_SECONDS
            and max(old["seconds"].get(node, [0])) < TIMEOUT_SECONDS
        ),
        "above_45_per_run": {
            "old": round(above(old, NEAR_TIMEOUT_SECONDS) / old["runs"], 2),
            "new": round(above(new, NEAR_TIMEOUT_SECONDS) / new["runs"], 2),
        },
    }
    result["ok"] = not (
        result["only_old"]
        or result["outcome_changes"]
        or result["newly_at_timeout"]
        or result["above_45_per_run"]["new"] > result["above_45_per_run"]["old"]
    )
    return result


def render_compare(result: dict, top: int = 40) -> str:
    rate = result["above_45_per_run"]
    lines = [
        f"# API suite, old against new: {'PASS' if result['ok'] else 'FAIL'}",
        f"Shard samples: old {result['samples']['old']}, new {result['samples']['new']};"
        f" tests: old {result['tests']['old']}, new {result['tests']['new']}",
        f"Tests that stopped running: {len(result['only_old'])}",
        *(f"- {node}" for node in result["only_old"][:top]),
        f"Tests whose outcome changed: {len(result['outcome_changes'])}",
        *(
            f"- {node}: {', '.join(change['old'])} -> {', '.join(change['new'])}"
            for node, change in list(result["outcome_changes"].items())[:top]
        ),
        f"Tests newly at the {TIMEOUT_SECONDS} s timeout: {len(result['newly_at_timeout'])}",
        *(f"- {node}" for node in result["newly_at_timeout"][:top]),
        f"Samples above {NEAR_TIMEOUT_SECONDS} s per run: old {rate['old']}, new {rate['new']}",
        f"Tests only in the new arm (not a failure): {len(result['only_new'])}",
        *(f"- {node}" for node in result["only_new"][:top]),
    ]
    return "\n".join(lines) + "\n"


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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["compare"]:
        if len(argv) != 3:
            raise SystemExit("usage: api-times.py compare OLD_DIR NEW_DIR")
        arms = [load(Path(path), attempts_as_runs=True) for path in argv[1:]]
        if not all(arms):
            raise SystemExit("error: an arm has no *.jsonl rows")
        result = compare(*arms)
        sys.stdout.write(render_compare(result))
        return 0 if result["ok"] else 1
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
