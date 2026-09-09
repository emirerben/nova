#!/usr/bin/env python3
"""Compare the checked-in GCS lifecycle policy with the live bucket policy."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PATH = ROOT / "infra" / "gcs-lifecycle.json"


def _normalized(payload: dict[str, Any]) -> list[tuple[str, int, tuple[str, ...]]]:
    rules = payload.get("lifecycle", {}).get("rule", [])
    normalized = []
    for rule in rules:
        action = str(rule.get("action", {}).get("type") or "")
        condition = rule.get("condition", {})
        age = int(condition.get("age", 0))
        prefixes = tuple(
            sorted(str(value) for value in condition.get("matchesPrefix", []))
        )
        normalized.append((action, age, prefixes))
    return sorted(normalized)


def _live_policy(bucket: str) -> dict[str, Any]:
    result = subprocess.run(
        ["gsutil", "lifecycle", "get", f"gs://{bucket}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", default=os.environ.get("STORAGE_BUCKET", ""))
    parser.add_argument("--live-file", type=Path)
    args = parser.parse_args()
    if args.live_file is None and not args.bucket:
        parser.error("--bucket or STORAGE_BUCKET is required")
    expected = json.loads(EXPECTED_PATH.read_text())
    live = (
        json.loads(args.live_file.read_text())
        if args.live_file
        else _live_policy(args.bucket)
    )
    if _normalized(expected) == _normalized(live):
        print("GCS lifecycle matches infra/gcs-lifecycle.json")
        return 0
    print("GCS lifecycle drift detected", file=sys.stderr)
    print(f"expected={_normalized(expected)!r}", file=sys.stderr)
    print(f"live={_normalized(live)!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
