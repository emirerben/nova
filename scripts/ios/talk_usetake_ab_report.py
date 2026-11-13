#!/usr/bin/env python3
"""Reduce the cells of scripts/ios/talk-usetake-ab.sh to medians per (build, dwell). Usage: talk_usetake_ab_report.py <out-dir>"""
import json
import re
import statistics
import sys
from pathlib import Path

INTERVALS = ["talk.use.spinner", "talk.use.intake", "talk.use.stageCopy", "talk.use.import", "talk.use.proxyGateWait",
             "talk.use.proxyTranscode", "talk.use.prewarmHashWait", "talk.use.prewarmProxyWait",
             "talk.prewarm.hash", "talk.prewarm.proxy"]
COUNTERS = ["talk.use.prewarmProxyUsed", "talk.use.prewarm.claimed", "talk.use.prewarm.miss"]


def load(cell: Path):
    path = cell / "summary.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def first(summary, name):
    """Duration of the first occurrence. A failed Use take puts the take back in Review, which prepares it again for the retry, so
    later occurrences of talk.prewarm.* belong to the retry, not to the dwell."""
    rows = summary["intervals"].get(name)
    return rows[0][1] if rows else None


def main(out: Path):
    cells = {}
    for cell in sorted(out.iterdir()):
        match = re.fullmatch(r"(pr|base)-d(\d+)-r(\d+)", cell.name)
        if not match or not cell.is_dir():
            continue
        summary = load(cell)
        if summary is None:
            continue
        cells.setdefault((match[1], int(match[2])), []).append((cell.name, summary))
    print("| build | dwell s | n | " + " | ".join(n.removeprefix("talk.") for n in INTERVALS) + " | " + " | ".join(c.removeprefix("talk.use.") for c in COUNTERS) + " |")
    print("|" + "---|" * (3 + len(INTERVALS) + len(COUNTERS)))
    for (build, dwell), runs in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        row = [build, str(dwell), str(len(runs))]
        for name in INTERVALS:
            values = [v for v in (first(s, name) for _, s in runs) if v is not None]
            row.append("-" if not values else f"{statistics.median(values):.0f} ({min(values):.0f}-{max(values):.0f})")
        for name in COUNTERS:
            row.append(str(sum(len(s["counters"].get(name, [])) for _, s in runs)))
        print("| " + " | ".join(row) + " |")
    print("\nCells are median ms (min-max) over runs, first occurrence per run. '-' = the build has no such signpost or it never fired.")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
