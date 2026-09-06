#!/usr/bin/env python3
"""Create an immutable, signed chat speech-cleanup promotion receipt.

This operator-side command validates a bounded aggregate evidence export.  It
never accepts raw rows or media, never changes feature flags, and never contacts
production.  A missing/failing canary creates a signed HALT receipt and exits 2;
malformed or unsafe input creates no receipt and exits 1.

Usage:
  python scripts/audit_chat_speech_cleanup_rollout.py \
    --evidence /secure/evidence-10-to-25.json \
    --target-percent 25 \
    --output /secure/receipts/speech-cleanup-25.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.speech_cleanup_rollout import (  # noqa: E402
    MAX_EVIDENCE_BYTES,
    ROLLOUT_STAGES,
    RolloutEvidenceError,
    audit_rollout_evidence,
    sign_rollout_receipt,
)

_SECRET_ENV = "SPEECH_CLEANUP_ROLLOUT_RECEIPT_SECRET"
_KEY_ID_ENV = "SPEECH_CLEANUP_ROLLOUT_RECEIPT_KEY_ID"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--target-percent", required=True, type=int, choices=ROLLOUT_STAGES)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def _read_evidence(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RolloutEvidenceError("evidence file is unavailable") from exc
    if size <= 0 or size > MAX_EVIDENCE_BYTES:
        raise RolloutEvidenceError("evidence file exceeds the bounded size contract")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RolloutEvidenceError("evidence file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RolloutEvidenceError("evidence file must contain one JSON object")
    return value


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
    try:
        with path.open("x", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            handle.write(encoded)
            handle.write("\n")
    except FileExistsError as exc:
        raise RolloutEvidenceError(
            "receipt output already exists; archival receipts are immutable"
        ) from exc
    except OSError as exc:
        raise RolloutEvidenceError("receipt output could not be written") from exc


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        evidence = _read_evidence(args.evidence)
        audit = audit_rollout_evidence(evidence, target_percent=args.target_percent)
        secret = os.environ.get(_SECRET_ENV, "")
        key_id = os.environ.get(_KEY_ID_ENV, "")
        forbidden = tuple(
            os.environ.get(name, "")
            for name in ("ADMIN_API_KEY", "INTERNAL_API_KEY", "TRAINING_DATASET_SPLIT_SECRET")
        )
        receipt = sign_rollout_receipt(
            audit.receipt,
            secret=secret,
            key_id=key_id,
            forbidden_secrets=forbidden,
        )
        _write_receipt(args.output, receipt)
    except RolloutEvidenceError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    result = "PASS" if audit.promotion_allowed else "HALT"
    print(f"{result}: signed receipt written to {args.output}")
    if audit.halt_reasons:
        print("halt reasons: " + ", ".join(audit.halt_reasons))
    return 0 if audit.promotion_allowed else 2


if __name__ == "__main__":
    raise SystemExit(run())
