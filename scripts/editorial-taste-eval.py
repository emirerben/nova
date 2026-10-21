#!/usr/bin/env python3
"""Freeze private two-arm idea comparisons and actual creator judgments. No provider I/O."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/apps/api"))

from app.services.editorial_taste import read_json  # noqa: E402
from app.services.editorial_taste_evaluation import Evaluation  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("preregister")
    register.add_argument("--input", required=True, type=Path)
    register.add_argument("--corpus", required=True, type=Path)
    output = commands.add_parser("output")
    output.add_argument("--case", required=True)
    output.add_argument("--arm", required=True, choices=("baseline", "personalized"))
    output.add_argument("--input", required=True, type=Path)
    cards = commands.add_parser("cards")
    cards.add_argument("--case", required=True)
    judge = commands.add_parser("judge")
    judge.add_argument("--case", required=True)
    judge.add_argument("--input", required=True, type=Path)
    report = commands.add_parser("report")
    report.add_argument("--corpus", required=True, type=Path)
    args = parser.parse_args()
    try:
        evaluation = Evaluation(args.store)
        if args.command == "preregister":
            result = evaluation.register(read_json(args.input), args.corpus)
        elif args.command == "output":
            result = evaluation.output(args.case, args.arm, read_json(args.input))
        elif args.command == "cards":
            result = evaluation.cards(args.case)
        elif args.command == "judge":
            result = evaluation.judge(args.case, read_json(args.input))
        else:
            result = evaluation.report(args.corpus)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Editorial comparison: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
