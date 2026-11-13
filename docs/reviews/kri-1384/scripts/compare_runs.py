"""Compare full live runs of the same fixtures, per fixture.

usage: compare_runs.py OUT.json BASELINE.json CANDIDATE.json CONTROL.json
Each input is a summarize_live_eval.py output. Prints each run's pass rate, the baseline to
candidate transitions, and the candidate against the control fixture by fixture.
"""

import json
import sys
from collections import Counter

out, *paths = sys.argv[1:]
names = ("baseline", "candidate", "control")
runs = {
    name: {case["fixture"]: case for case in json.load(open(path))["cases"]}
    for name, path in zip(names, paths, strict=True)
}
fixtures = sorted(runs["control"])
assert all(sorted(run) == fixtures for run in runs.values()), "runs cover different fixtures"
NOT_RUN = "not run (ledger cap reached)"
assert not any(case["category"] == NOT_RUN for run in runs.values() for case in run.values()), (
    "a run has fixtures the ledger refused"
)


def outcome(name, fixture):
    return "pass" if runs[name][fixture]["passed"] else "fail"


summary = {
    "fixtures": len(fixtures),
    "passed": {name: sum(case["passed"] for case in runs[name].values()) for name in names},
    "baseline_to_candidate": dict(
        Counter(f"{outcome('baseline', f)}->{outcome('candidate', f)}" for f in fixtures)
    ),
    "candidate_vs_control": dict(
        Counter(f"{outcome('candidate', f)}/{outcome('control', f)}" for f in fixtures)
    ),
}
rows = [
    {
        "fixture": f,
        **{name: outcome(name, f) for name in names},
        "candidate_failure": runs["candidate"][f]["failure"],
    }
    for f in fixtures
    if len({outcome(name, f) for name in names}) > 1
]
with open(out, "w") as handle:
    json.dump({**summary, "differing": rows}, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
print(json.dumps(summary, indent=1))
