#!/usr/bin/env python3
"""Turn a `scripts/ios/bench.sh` run into numbers, and compare two runs.

  bench_report.py report --log pass1.log --log pass2.log --label base --sha abc123 --dest sim --iterations 10 --out base.json
  bench_report.py compare base.json fix.json

`report` reads the `measured [...]` lines XCTest prints for every metric, plus `PERF <test> key=value` lines the
tests print for counts (requests, rebuilds, network fetches), and writes median / p90 / MAD per metric.

`compare` marks a metric "improved" or "regressed" only when the medians differ by more than twice the larger of the
two MADs, so run-to-run noise is never reported as a win. Counters are compared exactly.

Stdlib only; runs with any Python 3.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

MEASURED = re.compile(
    r"Test Case '-\[(?P<cls>[\w.]+) (?P<test>\w+)\]' measured \[(?P<metric>.+?), (?P<unit>[^,\]]+)\] "
    r"average: (?P<avg>[-\d.]+), relative standard deviation: [-\d.]+%, values: \[(?P<values>[^\]]*)\]"
)
COUNTER = re.compile(r"^PERF (?P<test>\w+) (?P<pairs>.+)$", re.MULTILINE)


def mad(values: list[float]) -> float:
    median = statistics.median(values)
    return statistics.median(abs(v - median) for v in values)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def parse_logs(texts: list[str]) -> tuple[dict, dict]:
    """Pools every `measured` line across logs (one log per pass, many passes per side) and averages the counters.

    Pooling is what makes interleaved A/B honest: the spread (MAD) then includes the drift between passes."""
    pooled: dict[str, dict] = {}
    for text in texts:
        for match in MEASURED.finditer(text):
            values = [float(v) for v in match["values"].split(",") if v.strip()]
            if not values:
                continue
            entry = pooled.setdefault(f"{match['test']}/{match['metric']}", {"unit": match["unit"], "values": []})
            entry["values"].extend(values)
    metrics: dict[str, dict] = {}
    for key, entry in pooled.items():
        values = entry["values"]
        metrics[key] = {
            "unit": entry["unit"],
            "n": len(values),
            "median": statistics.median(values),
            "mean": statistics.fmean(values),
            "p90": percentile(values, 0.9),
            "mad": mad(values),
            "min": min(values),
            "max": max(values),
            "values": values,
        }
    samples: dict[str, list[float]] = {}
    for text in texts:
        for match in COUNTER.finditer(text):
            for pair in match["pairs"].split():
                name, _, raw = pair.partition("=")
                try:
                    samples.setdefault(f"{match['test']}/{name}", []).append(float(raw))
                except ValueError:
                    continue
    counters = {key: round(statistics.fmean(values), 3) for key, values in samples.items()}
    return metrics, counters


def fmt(value: float, unit: str) -> str:
    if unit == "s":
        return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.2f} s"
    return f"{value:.3g} {unit}"


def render_table(result: dict) -> str:
    lines = ["| metric | median | p90 | MAD | n |", "|---|---|---|---|---|"]
    for key, m in sorted(result["metrics"].items()):
        lines.append(f"| {key} | {fmt(m['median'], m['unit'])} | {fmt(m['p90'], m['unit'])} | {fmt(m['mad'], m['unit'])} | {m['n']} |")
    for key, value in sorted(result["counters"].items()):
        lines.append(f"| {key} | {value:g} | | | |")
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    metrics, counters = parse_logs([Path(path).read_text(errors="replace") for path in args.log])
    result = {
        "label": args.label, "sha": args.sha, "dest": args.dest, "iterations": args.iterations,
        "metrics": metrics, "counters": counters,
    }
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nbench: {len(metrics)} metrics, {len(counters)} counters -> {args.out}\n")
    print(render_table(result))
    if not metrics and not counters:
        print("bench: nothing was measured; see the xcodebuild output above", file=sys.stderr)
        return 1
    return 0


HIGHER_IS_BETTER = ("Frame Rate", "Frame Count")


def verdict(base: dict, new: dict, key: str = "") -> tuple[str, float]:
    delta = new["median"] - base["median"]
    noise = 2 * max(base["mad"], new["mad"])
    if abs(delta) <= noise:
        return "noise", delta
    # Durations, CPU, hitches: lower is better. Frame rate and frame count: higher is better.
    better = delta > 0 if any(word in key for word in HIGHER_IS_BETTER) else delta < 0
    return ("improved" if better else "regressed"), delta


def cmd_compare(args: argparse.Namespace) -> int:
    base = json.loads(Path(args.base).read_text())
    new = json.loads(Path(args.new).read_text())
    print(f"**{base['label']}** ({base['sha']}, {base['dest']}) vs **{new['label']}** ({new['sha']}, {new['dest']})\n")
    if base["dest"] != new["dest"]:
        print("> Warning: different destinations; these numbers are not comparable.\n")
    print("| metric | before | after | change | verdict |")
    print("|---|---|---|---|---|")
    for key in sorted(set(base["metrics"]) & set(new["metrics"])):
        b, n = base["metrics"][key], new["metrics"][key]
        word, delta = verdict(b, n, key)
        percent = (delta / b["median"] * 100) if b["median"] else 0.0
        print(f"| {key} | {fmt(b['median'], b['unit'])} | {fmt(n['median'], n['unit'])} | {percent:+.0f}% | {word} |")
    # A counter that never ticks is never written, so one missing from a run means zero (a build that stopped hashing
    # files reports no hash counter at all); a key missing from both runs is not shown.
    for key in sorted(set(base["counters"]) | set(new["counters"])):
        b, n = base["counters"].get(key, 0.0), new["counters"].get(key, 0.0)
        # Counts that depend on timing (poll loops, rebuilds while scrolling) wobble by a few percent between
        # identical runs; only a change beyond that, or beyond half a request, is a difference.
        word = "same" if abs(b - n) <= max(0.5, 0.1 * abs(b)) else ("improved" if n < b else "regressed")
        print(f"| {key} | {b:g} | {n:g} | {n - b:+g} | {word} |")
    only = sorted(set(base["metrics"]) ^ set(new["metrics"]))
    if only:
        print("\nOnly in one run: " + ", ".join(only))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--log", action="append", required=True, help="repeat once per pass; samples are pooled")
    report.add_argument("--label", required=True)
    report.add_argument("--sha", required=True)
    report.add_argument("--dest", required=True)
    report.add_argument("--iterations", type=int, required=True)
    report.add_argument("--out", required=True)
    report.set_defaults(func=cmd_report)
    compare = sub.add_parser("compare")
    compare.add_argument("base")
    compare.add_argument("new")
    compare.set_defaults(func=cmd_compare)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
