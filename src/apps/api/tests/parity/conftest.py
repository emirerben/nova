"""Write ``test-results/parity-decisions.json`` after a run that collected the parity suite.

Results are computed by re-evaluating every case through ``evaluate()`` (the same
helper the tests use), never by reading the xfail markers, so the report says what
the code does today.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

_API_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = _API_ROOT / "test-results" / "parity-decisions.json"


def _jsonable(value: Any) -> Any:
    if isinstance(value, frozenset | set):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, tuple | list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _pct(passed: int, total: int) -> float:
    return round(100.0 * passed / total, 1) if total else 100.0


def build_report() -> dict[str, Any]:
    from tests.parity.test_decision_parity import CASES, evaluate  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    sanity_failures: list[str] = []
    for case in CASES:
        sanity = evaluate(case, "en")
        if not sanity["passed"]:
            sanity_failures.append(case.id)
        for variant in case.variants:
            result = evaluate(case, variant)
            if result["passed"]:
                status = "fixed_remove_xfail" if result["known_gap"] else "parity"
            else:
                status = "baseline_gap" if result["known_gap"] else "regression"
            rows.append(
                {
                    **{k: _jsonable(v) for k, v in result.items()},
                    "result": "pass" if result["passed"] else "fail",
                    "status": status,
                    "english_sanity": "pass" if sanity["passed"] else "fail",
                }
            )

    by_stage: dict[str, dict[str, int]] = defaultdict(lambda: {"variants": 0, "pass": 0})
    by_variant: dict[str, dict[str, int]] = defaultdict(lambda: {"variants": 0, "pass": 0})
    for row in rows:
        for bucket in (by_stage[row["stage"]], by_variant[row["variant"]]):
            bucket["variants"] += 1
            bucket["pass"] += row["result"] == "pass"
    passed = sum(row["result"] == "pass" for row in rows)
    return {
        "suite": "tests/parity/test_decision_parity.py",
        "ticket": "KRI-773",
        "totals": {
            "cases": len(CASES),
            "variants": len(rows),
            "pass": passed,
            "fail": len(rows) - passed,
            "parity_pct": _pct(passed, len(rows)),
            "english_sanity_failures": sanity_failures,
        },
        "by_stage": {
            stage: {
                **v,
                "fail": v["variants"] - v["pass"],
                "parity_pct": _pct(v["pass"], v["variants"]),
            }
            for stage, v in by_stage.items()
        },
        "by_variant": {
            name: {
                **v,
                "fail": v["variants"] - v["pass"],
                "parity_pct": _pct(v["pass"], v["variants"]),
            }
            for name, v in by_variant.items()
        },
        "results": rows,
    }


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(build_report(), indent=2, ensure_ascii=False) + "\n")
