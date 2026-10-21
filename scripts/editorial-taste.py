#!/usr/bin/env python3
"""Calibrate an owner-specific private taste corpus without providers or production writes."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/apps/api"))

from app.services.editorial_taste import TasteStore, read_json  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser(
        "import", help="Import only creator-confirmed positive examples"
    )
    ingest.add_argument("--input", required=True, type=Path)
    ingest.add_argument("--owner", required=True)
    for name in ("reviewer", "approval"):
        ingest.add_argument("--" + name, required=True)
    propose = commands.add_parser("propose-profile")
    propose.add_argument("--rules", required=True, type=Path)
    propose.add_argument("--expected-revision", required=True, type=int)
    approve = commands.add_parser("approve-profile")
    approve.add_argument("--expected-revision", required=True, type=int)
    approve.add_argument("--profile-sha256", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--approval", required=True)
    context = commands.add_parser("context")
    context.add_argument("--owner", required=True)
    context.add_argument("--query", default="")
    context.add_argument("--kind", choices=("video", "slides"))
    held = commands.add_parser(
        "holdout", help="Reserve an unexposed project id, without its body"
    )
    held.add_argument("--project", required=True)
    calibration = commands.add_parser(
        "calibration", help="Record a development proposal or comparison"
    )
    calibration.add_argument("--input", required=True, type=Path)
    calibration.add_argument("--expected-revision", required=True, type=int)
    judge = commands.add_parser(
        "judge", help="Record an actual creator choice, tie or neither"
    )
    judge.add_argument("--id", required=True)
    judge.add_argument("--input", required=True, type=Path)
    judge.add_argument("--expected-revision", required=True, type=int)
    commands.add_parser("report")
    args = parser.parse_args()
    try:
        store = TasteStore(args.store)
        if args.command == "import":
            result = store.import_capture(
                read_json(args.input), args.owner, args.reviewer, args.approval
            )
        elif args.command == "propose-profile":
            result = store.propose(read_json(args.rules), args.expected_revision)
        elif args.command == "approve-profile":
            result = store.approve(
                args.expected_revision,
                args.profile_sha256,
                args.reviewer,
                args.approval,
            )
        elif args.command == "context":
            result = store.context(args.owner, args.query, args.kind)
        elif args.command == "holdout":
            result = store.holdout(args.project)
        elif args.command == "calibration":
            result = store.add_calibration(
                read_json(args.input), args.expected_revision
            )
        elif args.command == "judge":
            result = store.judge(args.id, read_json(args.input), args.expected_revision)
        else:
            result = store.report(store.load())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Editorial taste: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
