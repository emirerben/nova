"""Prepare provider-neutral fine-tuning rows from a canonical edit export.

This command performs no network calls and can never change the production
model. Its input is the consent-filtered canonical JSONL produced by the admin
export job; provider adapters can consume the generated generic JSONL later.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.schemas.edit_training import CanonicalEditTrainingRecord
from app.services.edit_training_dataset import (
    assert_export_safe,
    build_preference_pairs,
    generic_fine_tuning_rows,
)


def _load_jsonl(path: Path) -> list[CanonicalEditTrainingRecord]:
    records: list[CanonicalEditTrainingRecord] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            assert_export_safe(payload)
            records.append(CanonicalEditTrainingRecord.model_validate(payload))
        except (json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"{path}:{line_number}: invalid canonical record: {exc}") from exc
    return records


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            assert_export_safe(row)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def _write_replay_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write eval-compatible cassettes whose raw_text is derived safe output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            projection = {key: value for key, value in row.items() if key != "raw_text"}
            assert_export_safe(projection)
            assert_export_safe(json.loads(str(row["raw_text"])))
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument(
        "--agent",
        required=True,
        choices=("edit_guide", "edit_proposal", "edit_copilot"),
    )
    parser.add_argument("--format", choices=("generic",), default="generic")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    records = _load_jsonl(args.dataset)
    rows = generic_fine_tuning_rows(records, agent=args.agent)
    output = args.out / f"{args.agent}-{args.format}.jsonl"
    _write_jsonl(output, rows)
    preferences = [
        pair.model_dump(mode="json")
        for pair in build_preference_pairs(records)
        if pair.agent == args.agent
    ]
    preference_output = args.out / f"{args.agent}-preferences.jsonl"
    _write_jsonl(preference_output, preferences)
    replays = [
        {
            "agent": {
                "edit_guide": "nova.plan.edit_guide",
                "edit_proposal": "nova.plan.edit_proposal",
                "edit_copilot": "nova.edit.copilot",
            }[record.agent],
            "prompt_version": record.versions.get("prompt", "unknown"),
            "input": {
                "media_summary": record.media_summary,
                "user_intent": record.user_intent,
            },
            "raw_text": json.dumps(record.proposed, ensure_ascii=False, sort_keys=True),
            "output": record.proposed,
            "meta": {
                "artifact_key": record.artifact_key,
                "split": record.split,
                "labels": record.labels,
            },
        }
        for record in records
        if record.agent == args.agent and record.split in {"validation", "test"}
    ]
    replay_output = args.out / f"{args.agent}-held-out-replays.jsonl"
    _write_replay_jsonl(replay_output, replays)
    print(
        f"prepared {len(rows)} {args.agent} rows, {len(preferences)} preferences, "
        f"and {len(replays)} held-out replays at {args.out}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
