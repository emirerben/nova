"""Per-fixture results of the live Main Creator eval plus the ledger rows of each run.

usage: summarize_live_eval.py OUT.json DB_NAME RUN_PREFIX JUNIT.xml [JUNIT.xml ...]
"""

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter

out, db, prefix, *reports = sys.argv[1:]


def category(message):
    if message is None:
        return None
    if "AiBudgetExceeded" in message or "ai_budget_exhausted" in message:
        return "not run (ledger cap reached)"
    if "brief_updates" in message or "no update records" in message:
        return "requirement not extracted (brief_updates)"
    if "'ask_user'" in message:
        return "asked instead of proposing"
    if "'muted'" in message:
        return "reply wording (muted)"
    if "text update" in message:
        return "text update missing"
    return "other"


# A later report's result replaces a fixture the ledger refused in an earlier one.
by_fixture = {}
for report in reports:
    for case in ET.parse(report).iter("testcase"):
        name = re.search(r"\[(.*)\]", case.get("name")).group(1)
        failure = case.find("failure")
        error = case.find("error")
        problem = failure if failure is not None else error
        message = (
            " ".join((problem.get("message") or "").split())[:240] if problem is not None else None
        )
        row = {"fixture": name, "passed": problem is None, "failure": message}
        row["category"] = category(message)
        earlier = by_fixture.get(name)
        if earlier is None or earlier["category"] == "not run (ledger cap reached)":
            by_fixture[name] = row
cases = list(by_fixture.values())
query = f"""
SELECT test_run_id, provider, requested_model, status,
       coalesce(usage_json->>'_budget_capability', '') AS capability, count(*),
       round(sum(coalesce(settled_cost_usd, 0))::numeric, 4),
       round(sum(CASE WHEN status IN ('unknown', 'provider_started') THEN estimated_cost_usd
                      ELSE 0 END)::numeric, 4)
FROM ai_cost_reservations WHERE test_run_id LIKE '{prefix}%'
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4"""
rows = subprocess.run(
    ["psql", "-h", "localhost", "-d", db, "-At", "-F", "\t", "-c", query],
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()
ledger = [
    dict(
        zip(
            (
                "test_run_id",
                "provider",
                "model",
                "status",
                "capability",
                "calls",
                "settled_usd",
                "held_usd",
            ),
            row.split("\t"),
        )
    )
    for row in rows
]
summary = {
    "fixtures": len(cases),
    "ran": sum(case["category"] != "not run (ledger cap reached)" for case in cases),
    "passed": sum(case["passed"] for case in cases),
    "failure_categories": dict(Counter(c["category"] for c in cases if not c["passed"])),
    "settled_usd": round(sum(float(r["settled_usd"]) for r in ledger), 4),
    "ledger": ledger,
    "cases": cases,
}
with open(out, "w") as handle:
    json.dump(summary, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
print(json.dumps({k: v for k, v in summary.items() if k not in ("cases", "ledger")}, indent=2))
