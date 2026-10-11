#!/usr/bin/env python3
"""Prepare, but never execute, a KRI-499 duration-check promotion.

The only supported promotion changes the declared creator-duration check from
``log_only`` to ``enforce``. It validates measured evidence and emits the
immutable GitHub workflow command an operator can review and dispatch.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
MODE = "CREATOR_DURATION_CHECK_MODE"


def _checked_out_sha(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("KRI-499 promotion: cannot resolve checked-out main revision") from exc


def _require_rollout_targets(repo_root: Path) -> None:
    config = repo_root / "src/apps/api/app/config.py"
    workflow = repo_root / ".github/workflows/fly-deploy.yml"
    if not config.is_file() or not workflow.is_file():
        raise SystemExit("KRI-499 promotion: repository workflow/configuration is unavailable")
    config_text = config.read_text()
    workflow_text = workflow.read_text()
    if 'creator_duration_check_mode: Literal["log_only", "enforce"] = "log_only"' not in config_text:
        raise SystemExit("KRI-499 promotion: creator duration rollout setting is not declared")
    required_workflow_fragments = (
        "creator_duration_check_mode:",
        "length_promotion_evidence:",
        MODE,
        "Validate KRI-499 enforcement promotion evidence",
    )
    if any(fragment not in workflow_text for fragment in required_workflow_fragments):
        raise SystemExit("KRI-499 promotion: Fly workflow cannot validate and apply enforcement")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"KRI-499 promotion: cannot read evidence: {exc}") from exc
    if not isinstance(evidence, dict):
        raise SystemExit("KRI-499 promotion: evidence must be a JSON object")

    from app.services.contract_check_rollout import validate_promotion

    report = validate_promotion(evidence).to_dict()
    candidate = report["candidate_sha"]
    plan = {"promotion_report": report, "status": "pending", "next_command": None}
    if report["status"] != "passed":
        print(json.dumps(plan, indent=2, sort_keys=True))
        raise SystemExit("KRI-499 promotion: evidence remains pending")
    if not isinstance(candidate, str) or not SHA.fullmatch(candidate):
        raise SystemExit("KRI-499 promotion: candidate SHA is invalid")
    _require_rollout_targets(args.repo_root)
    if _checked_out_sha(args.repo_root) != candidate:
        plan["reason"] = "checked-out revision does not equal evidence candidate SHA"
        print(json.dumps(plan, indent=2, sort_keys=True))
        raise SystemExit("KRI-499 promotion: refusing mutable-ref workflow dispatch")

    evidence_path = shlex.quote(str(args.evidence.resolve()))
    command = (
        "gh workflow run fly-deploy.yml --ref main "
        f"-f expected_sha={candidate} "
        "-f creator_duration_check_mode=enforce "
        f'-f "length_promotion_evidence=$(cat {evidence_path})"'
    )
    plan.update(
        status="ready_for_human_dispatch",
        setting={"key": MODE, "value": "enforce"},
        next_command=command,
        note=(
            "The workflow rechecks expected_sha against main before the Fly guard, "
            "then validates this evidence before any Fly mutation."
        ),
    )
    print(json.dumps(plan, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
