#!/usr/bin/env python3
"""Fail closed when KRI-499 duration enforcement lacks promotion evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"KRI-499 promotion: cannot read evidence: {exc}") from exc
    if not isinstance(evidence, dict):
        raise SystemExit("KRI-499 promotion: evidence must be a JSON object")

    from app.services.contract_check_rollout import validate_promotion

    report = validate_promotion(evidence).to_dict()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.write_text(rendered)
    print(rendered, end="")
    if report["status"] != "passed":
        raise SystemExit("KRI-499 promotion: evidence remains pending")


if __name__ == "__main__":
    main()
