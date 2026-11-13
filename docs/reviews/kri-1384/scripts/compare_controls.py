"""Join the Sol `medium` failures with the Gemini and Sol `high` controls, per fixture.

usage: compare_controls.py OUT.json SOL_MEDIUM.json GEMINI_CONTROL.json SOL_HIGH.json
"""

import json
import sys
from collections import Counter

out, sol_path, gemini_path, high_path = sys.argv[1:]
sol = {c["fixture"]: c for c in json.load(open(sol_path))["cases"]}
gemini = {c["fixture"]: c for c in json.load(open(gemini_path))["cases"]}
high = {c["fixture"]: c for c in json.load(open(high_path))["cases"]}
NOT_RUN = "not run (ledger cap reached)"


def verdict(case):
    if case is None:
        return "not in control"
    if case["category"] == NOT_RUN:
        return "not run"
    return "pass" if case["passed"] else "fail"


rows = [
    {
        "fixture": name,
        "sol_medium_category": case["category"],
        "gemini_high": verdict(gemini.get(name)),
        "sol_high": verdict(high.get(name)),
    }
    for name, case in sorted(sol.items())
    if not case["passed"]
]
by_category = {}
for row in rows:
    tally = by_category.setdefault(row["sol_medium_category"], Counter())
    tally["sol_medium_failures"] += 1
    tally[f"gemini_{row['gemini_high']}"] += 1
    tally[f"sol_high_{row['sol_high']}"] += 1
summary = {"by_category": {k: dict(v) for k, v in by_category.items()}, "rows": rows}
with open(out, "w") as handle:
    json.dump(summary, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
print(json.dumps(summary["by_category"], indent=1))
