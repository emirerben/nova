"""Create or inspect a consent-safe canonical edit-training dataset locally."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import settings
from app.database import sync_session
from app.services.edit_training_dataset import (
    dataset_readiness,
    records_to_jsonl,
    records_to_parquet,
)
from app.services.edit_training_exports import build_training_records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("jsonl", "parquet"), default="jsonl")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not args.dry_run and args.out is None:
        parser.error("--out is required unless --dry-run is set")

    with sync_session() as db:
        records, artifacts = build_training_records(
            db,
            secret=settings.training_dataset_split_secret,
        )
    readiness = dataset_readiness(records)
    print(
        f"eligible artifacts={len(artifacts)} records={len(records)} "
        f"fully_reviewed={readiness.reviewed_artifacts} ready={readiness.ready}"
    )
    for blocker in readiness.blockers:
        print(f"blocked: {blocker}")
    if args.dry_run:
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "jsonl":
        args.out.write_bytes(records_to_jsonl(records))
    else:
        records_to_parquet(records, str(args.out))
    print(f"wrote {len(records)} canonical records to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
