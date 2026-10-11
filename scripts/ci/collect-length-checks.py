#!/usr/bin/env python3
"""Collect bounded, sanitized KRI-499 log-only observation evidence.

The caller supplies a verified fleet proof and explicitly attests to one
uninterrupted interval.  This tool does not authenticate Fly logs or the proof;
it only fail-closes malformed, stale, disconnected, or mismatched input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping


SHA = re.compile(r"^[0-9a-f]{40}$")
MINIMUM_WINDOW_S = 24 * 60 * 60
MAX_PROOF_AGE_S = 5 * 60


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _pending(reason: str) -> dict[str, object]:
    return {"status": "pending", "reasons": [reason], "clean_observations": []}


def _proof_error(proof: object, now: int) -> str | None:
    if not isinstance(proof, Mapping):
        return "fleet proof is not a JSON object"
    candidate = proof.get("expected_candidate_sha")
    timestamp = proof.get("timestamp_epoch_s")
    deployed = proof.get("deployment_started_epoch_s")
    if not isinstance(candidate, str) or not SHA.fullmatch(candidate):
        return "fleet proof lacks an exact candidate SHA"
    if proof.get("api_revision") != candidate or proof.get("worker_revision") != candidate:
        return "fleet proof revisions do not match the candidate"
    if not _is_int(timestamp) or timestamp < 0 or timestamp > now or now - timestamp > MAX_PROOF_AGE_S:
        return "fleet proof is stale or has an invalid timestamp"
    if not _is_int(deployed) or deployed < 0 or deployed > now:
        return "fleet proof lacks a valid deployment timestamp"
    provenance = proof.get("log_source_provenance")
    if not isinstance(provenance, Mapping) or not provenance:
        return "fleet proof lacks log source provenance"
    return None


def _read_events(
    stream: Any, *, candidate: str, deployed: int, interval_start: int, interval_end: int
) -> tuple[int, int, str | None]:
    checks = declines = 0
    for line in stream:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return 0, 0, "malformed log input resets coverage"
        if isinstance(event, Mapping) and isinstance(event.get("message"), str):
            message = event["message"]
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                if "creator_length_check" in message:
                    return 0, 0, "malformed required check event resets coverage"
                continue
        if not isinstance(event, Mapping) or event.get("event") != "creator_length_check":
            continue
        timestamp = event.get("timestamp_epoch_s")
        count = event.get("check_observations")
        event_declines = event.get("check_declines")
        if (
            event.get("check_mode") != "log_only"
            or event.get("api_revision") != candidate
            or event.get("worker_revision") != candidate
        ):
            return 0, 0, "check event mode or revision mismatch resets coverage"
        if (
            not _is_int(timestamp)
            or timestamp < deployed
            or timestamp < interval_start
            or timestamp > interval_end
            or not _is_int(count)
            or count < 0
            or not _is_int(event_declines)
            or event_declines < 0
        ):
            return 0, 0, "malformed required check event resets coverage"
        checks += count
        declines += event_declines
    return checks, declines, None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet-proof", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--interval-start", type=int, required=True)
    parser.add_argument("--interval-end", type=int, required=True)
    parser.add_argument("--stream-complete", action="store_true")
    parser.add_argument("--now-epoch-s", type=int)
    args = parser.parse_args()
    now = int(time.time()) if args.now_epoch_s is None else args.now_epoch_s
    try:
        proof = json.loads(args.fleet_proof.read_text())
    except (OSError, ValueError):
        result = _pending("fleet proof cannot be read")
    else:
        error = _proof_error(proof, now)
        if error is not None:
            result = _pending(error)
        elif (
            not args.stream_complete
            or args.interval_start < proof["deployment_started_epoch_s"]
            or args.interval_end <= args.interval_start
            or args.interval_end > now
            or args.interval_end - args.interval_start < MINIMUM_WINDOW_S
        ):
            result = _pending("incomplete or too-short stream resets coverage")
        else:
            checks, declines, error = _read_events(
                sys.stdin,
                candidate=proof["expected_candidate_sha"],
                deployed=proof["deployment_started_epoch_s"],
                interval_start=args.interval_start,
                interval_end=args.interval_end,
            )
            if error is not None:
                result = _pending(error)
            elif declines != 0:
                result = _pending("contract-check declines reset coverage")
            elif checks <= 0:
                result = _pending("empty stream resets coverage")
            else:
                result = {
                    "status": "collected",
                    "reasons": [],
                    "clean_observations": [
                        {
                            "interval_start_epoch_s": args.interval_start,
                            "interval_end_epoch_s": args.interval_end,
                            "stream_complete": True,
                            "check_mode": "log_only",
                            "check_observations": checks,
                            "check_declines": 0,
                            "api_revision": proof["expected_candidate_sha"],
                            "worker_revision": proof["expected_candidate_sha"],
                        }
                    ],
                }
    # The journal is intentionally a replaceable checkpoint: it contains only
    # aggregate counts, bounded timestamps, and revisions, never log lines.
    args.journal.write_text(json.dumps(result, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
