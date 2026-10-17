#!/usr/bin/env python3
"""Run existing free regression suites and publish an honest, actionable report.

Run with the API venv Python. No candidate-supplied commands are executed.
The report contains committed fixture/test identities, never automatic captures.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "src/apps/api"
SUITES = (
    "tests/incidents/",
    "tests/evals/request_following/",
    "tests/integration/test_request_following_queue.py",
)
CLAIM = (
    "Deterministic regression execution and recorded-evidence checks only. "
    "No live model calls, new-session semantic verdicts, or rendered-video verification."
)


def summarize(junit: Path, returncode: int):
    counts = dict(passed=0, failed=0, errors=0, skipped=0)
    failures = []
    if not junit.exists():
        return {
            "status": "failed",
            "runner_exit": returncode,
            "counts": counts,
            "failures": [{"test": "runner", "kind": "missing_junit"}],
        }
    for case in ET.parse(junit).getroot().iter("testcase"):
        kind = "passed"
        for tag, status in (
            ("failure", "failed"),
            ("error", "errors"),
            ("skipped", "skipped"),
        ):
            if case.find(tag) is not None:
                kind = status
                break
        counts[kind] += 1
        if kind in ("failed", "errors"):
            failures.append(
                {"test": f"{case.get('classname')}::{case.get('name')}", "kind": kind}
            )
    return {
        "status": "passed"
        if returncode == 0 and not failures and counts["passed"]
        else "failed",
        "runner_exit": returncode,
        "counts": counts,
        "failures": failures,
    }


def run(output):
    if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
        raise SystemExit(
            "Set DATABASE_URL to a migrated disposable database ending in _test"
        )
    output.mkdir(parents=True, exist_ok=True)
    junit = output / "junit.xml"
    # Prevent an inherited live-mode flag or real provider key from changing a
    # scheduled/free run. Explicit replay flags also reach the existing harness.
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "PATH",
            "HOME",
            "TMPDIR",
            "SYSTEMROOT",
            "DATABASE_URL",
            "REDIS_URL",
            "CI",
        }
    }
    env.update(
        NOVA_EVAL_MODE="replay",
        GEMINI_API_KEY="test",
        OPENAI_API_KEY="test",
        ANTHROPIC_API_KEY="test",
        STORAGE_BUCKET="nova-test",
        STORAGE_PROVIDER="gcs",
        TOKEN_ENCRYPTION_KEY="test-key-not-used-in-unit-tests",
        WHISPER_BACKEND="local",
        ALLOWED_ORIGINS='["http://localhost:3000"]',
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        *SUITES,
        "--eval-mode=replay",
        "-q",
        f"--junitxml={junit}",
    ]
    with (output / "pytest.log").open("w") as log:
        result = subprocess.run(
            command, cwd=API, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    report = {
        "schema_version": 1,
        "ran_at": datetime.now(UTC).isoformat(),
        "claim": CLAIM,
        "suites": list(SUITES),
        **summarize(junit, result.returncode),
    }
    # Reuse the foundation's inventory and existing request-following KPI report.
    with tempfile.TemporaryDirectory(prefix="request-following-inventory-") as store:
        inventory = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/request-following-loop.py"),
                "--store",
                store,
                "report",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if inventory.returncode == 0:
            data = json.loads(inventory.stdout)
            (output / "coverage.json").write_text(json.dumps(data, indent=2) + "\n")
            report["coverage"] = {
                "incident_records": data["existing_incidents"],
                "reliability_gates": data["existing_gates"],
            }
        else:
            report["status"] = "failed"
            report["failures"].append(
                {"test": "coverage_inventory", "kind": "report_failed"}
            )
    kpi = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.evals.request_following.report",
            "--eval-mode",
            "replay",
            "--phase",
            "Scheduled replay",
            "--out",
            str(output / "recorded-kpi.md"),
        ],
        cwd=API,
        env=env,
        capture_output=True,
        text=True,
    )
    if kpi.returncode:
        report["status"] = "failed"
        report["failures"].append({"test": "recorded_kpi", "kind": "report_failed"})
    lines = [
        "# Request-following regressions",
        "",
        CLAIM,
        "",
        f"Result: **{report['status']}**",
        "",
        " | ".join(f"{name}: {count}" for name, count in report["counts"].items()),
        "",
        "Skipped includes expected failures; inspect junit.xml for their exact reasons.",
        "",
        "## Failures needing a fixing PR",
        "",
    ]
    lines += [
        f"- `{failure['test']}` ({failure['kind']})" for failure in report["failures"]
    ] or ["None in the executed checks."]
    lines += [
        "",
        "## Coverage and next actions",
        "",
        "- `coverage.json`: incident, gate and fixture inventory; references are not measured proof.",
        "- `recorded-kpi.md`: historical replay KPI with its sample and authored-fixture limits.",
        "- Local review app: consented captured sessions awaiting human intent review.",
        "- For a failed test, inspect `pytest.log` and its linked fixture; propose a fixing PR.",
        "- For an uncovered request, review and export its packet, add a runnable regression, then review promotion.",
        "- Production prompt promotion and rendered-output verification remain separate approvals.",
        "",
    ]
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "summary.md").write_text("\n".join(lines))
    print(
        json.dumps(
            {
                "status": report["status"],
                "counts": report["counts"],
                "report": str(output / "summary.md"),
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve()))
