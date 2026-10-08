"""Machine-readable prompt library and honest coverage report for KRI-524.

The corpus is deliberately a library of requests, not a claim that every request
has been sent to a model.  ``executed`` cases must carry a concrete result and
``unexecuted`` cases remain useful regression inputs for future captures.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents._schemas.edit_format import EDIT_FORMATS

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "prompt_coverage" / "corpus.json"

FAMILIES = (
    "text",
    "positioning",
    "timing",
    "preservation",
    "selection",
    "order",
    "captions",
    "audio",
    "pacing",
    "style",
    "compound",
    "correction",
    "ambiguity",
    "capability",
)

SCENARIOS = {
    "library_only": {
        "fixture": None,
        "pytest": None,
        "tier": "unexecuted_library",
    },
}

DESTINATIONS = {"cloud_editor", "native_editor", "worker_render"}
KINDS = {"user_example", "negative", "clarification", "unsupported"}
PROVENANCE_KINDS = {"authored", "adapted", "capture"}
STAGES = {"creation", "editing"}
EVIDENCE_STATUSES = {"unexecuted", "executed"}
FAILURE_CATEGORY_BY_FAMILY = {
    "text": "wrong_text",
    "positioning": "wrong_geometry",
    "timing": "wrong_timing",
    "preservation": "source_loss",
    "selection": "selection_order",
    "order": "selection_order",
    "captions": "audio_caption",
    "audio": "audio_caption",
    "pacing": "pacing_style",
    "style": "pacing_style",
    "compound": "compound_correction",
    "correction": "compound_correction",
    "ambiguity": "ambiguity",
    "capability": "unsupported_false_claim",
}
REGRESSION_SUITES = (
    "tests/evals/request_following",
    "tests/evals/test_edit_copilot_evals.py",
    "tests/evals/test_main_creator_evals.py",
    "tests/evals/test_clip_intent_planner_evals.py",
    "tests/incidents",
)


@dataclass(frozen=True)
class PromptCase:
    case_id: str
    prompt: str
    format: str
    stage: str
    family: str
    kind: str
    destination_applicability: tuple[str, ...]
    prerequisites: tuple[str, ...]
    fixture_paths: tuple[str, ...]
    related_evidence: tuple[str, ...]
    execution_scenario: str
    provenance: dict[str, str]
    expected_behavior: str
    independent_assertions: tuple[str, ...]
    evidence_status: str = "unexecuted"
    execution: dict[str, Any] | None = None


def validate_cases(cases: list[PromptCase]) -> dict[str, Any]:
    errors: list[str] = []
    ids = [c.case_id for c in cases]
    prompts = [c.prompt.casefold() for c in cases]
    if len(cases) < 200:
        errors.append(f"expected at least 200 cases, got {len(cases)}")
    if len(ids) != len(set(ids)):
        errors.append("case IDs are not unique")
    if len(prompts) != len(set(prompts)):
        errors.append("prompt text is not unique")
    if set(c.format for c in cases) != set(EDIT_FORMATS):
        errors.append("format coverage does not match canonical EDIT_FORMATS")
    if set(c.family for c in cases) != set(FAMILIES):
        errors.append("family coverage is incomplete")
    if {c.stage for c in cases} != {"creation", "editing"}:
        errors.append("creation and editing stages are both required")
    if not {c.kind for c in cases} >= KINDS:
        errors.append("user_example, negative, clarification, and unsupported kinds are required")
    for case in cases:
        if case.format not in EDIT_FORMATS:
            errors.append(f"{case.case_id}: invalid format")
        if case.stage not in STAGES:
            errors.append(f"{case.case_id}: invalid stage")
        if case.family not in FAMILIES:
            errors.append(f"{case.case_id}: invalid family")
        if case.evidence_status not in EVIDENCE_STATUSES:
            errors.append(f"{case.case_id}: invalid evidence status")
        if not case.expected_behavior or len(case.independent_assertions) < 2:
            errors.append(f"{case.case_id}: incomplete expected behavior/assertions")
        if case.evidence_status == "executed" and not case.execution:
            errors.append(f"{case.case_id}: executed case has no execution result")
        if case.evidence_status != "executed" and case.execution:
            errors.append(f"{case.case_id}: unexecuted case has execution result")
        if case.kind not in KINDS:
            errors.append(f"{case.case_id}: invalid case kind")
        if not set(case.destination_applicability) <= DESTINATIONS:
            errors.append(f"{case.case_id}: invalid destination")
        if case.provenance.get("kind") not in PROVENANCE_KINDS:
            errors.append(f"{case.case_id}: invalid provenance")
        if case.execution_scenario != "library_only":
            errors.append(f"{case.case_id}: execution mapping requires exact evidence")
        if case.execution_scenario == "library_only" and case.related_evidence:
            errors.append(f"{case.case_id}: library-only case has execution evidence")
        if not case.provenance.get("source"):
            errors.append(f"{case.case_id}: provenance source is required")
        if not case.prerequisites:
            errors.append(f"{case.case_id}: prerequisites are required")
        if case.execution_scenario not in SCENARIOS:
            errors.append(f"{case.case_id}: unknown execution scenario")
    return {"case_count": len(cases), "errors": errors, "valid": not errors}


def load_cases(path: Path = FIXTURE) -> list[PromptCase]:
    raw = json.loads(path.read_text())
    return [PromptCase(**item) for item in raw["cases"]]


def report(cases: list[PromptCase]) -> dict[str, Any]:
    checked = validate_cases(cases)
    by_format = {fmt: sum(c.format == fmt for c in cases) for fmt in EDIT_FORMATS}
    by_family = {family: sum(c.family == family for c in cases) for family in FAMILIES}
    by_status = {
        status: sum(c.evidence_status == status for c in cases)
        for status in sorted({c.evidence_status for c in cases})
    }
    by_kind = {kind: sum(c.kind == kind for c in cases) for kind in sorted({c.kind for c in cases})}
    by_destination = {
        destination: sum(destination in c.destination_applicability for c in cases)
        for destination in sorted(DESTINATIONS)
    }
    by_failure_category = {
        category: sum(FAILURE_CATEGORY_BY_FAMILY.get(case.family) == category for case in cases)
        for category in sorted(set(FAILURE_CATEGORY_BY_FAMILY.values()))
    }
    return {
        **checked,
        "by_format": by_format,
        "by_family": by_family,
        "by_evidence_status": by_status,
        "by_kind": by_kind,
        "by_destination": by_destination,
        # These are categories intentionally covered by the library, not observed
        # production failure counts. Keep execution evidence separate below.
        "by_failure_category": by_failure_category,
        "unexecuted_outcome_total": sum(case.evidence_status == "unexecuted" for case in cases),
    }


def run_regression_suites(run=subprocess.run) -> dict[str, Any]:
    """Run named offline regressions in one subprocess, never paid/live tests."""
    suites = list(REGRESSION_SUITES)
    missing = [suite for suite in REGRESSION_SUITES if not (ROOT.parent / suite).exists()]
    if missing:
        return {
            "tier": "structural_regression",
            "suites": list(REGRESSION_SUITES),
            "missing_suites": missing,
            "passed": False,
            "returncode": 2,
            "output_tail": "required regression suite missing: " + ", ".join(missing),
        }
    env = os.environ.copy()
    env["NOVA_EVAL_MODE"] = "replay"
    command = [sys.executable, "-m", "pytest", "--eval-mode=replay", *REGRESSION_SUITES, "-q"]
    completed = run(
        command,
        cwd=ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return {
        "tier": "structural_regression",
        "suites": suites,
        "missing_suites": [],
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "output_tail": (completed.stdout + completed.stderr)[-2000:],
    }


def replay_exit_code(result: dict[str, Any]) -> int:
    """Return the CLI status for a report, including regression failures."""
    return (
        0
        if result.get("valid") and result.get("structural_regression", {}).get("passed", True)
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    cases = load_cases()
    result = report(cases)
    if args.replay:
        result["structural_regression"] = run_regression_suites()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return replay_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
