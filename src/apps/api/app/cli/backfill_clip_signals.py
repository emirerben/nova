"""Backfill measured clip signals onto clips that are already understood (skeleton).

    python -m app.cli.backfill_clip_signals                       # dry run: what is missing
    python -m app.cli.backfill_clip_signals --item-id <uuid>      # one plan item
    python -m app.cli.backfill_clip_signals --limit 200 --json

Read-only today. ``--write`` is refused until a signal is implemented AND the fenced store lands:
the write must mirror ``tasks/kria_clip_understanding._store`` (lock the PlanItem, match
``media_id`` + ``gcs_path`` + ``storage_generation``, merge through ``mutate_plan_item_media``, and
roll back if it would change footage identity), and it must go through
``clip_signals.merge_signals`` so a signal only ever writes its own key.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.services import clip_signals
from app.services.clip_signals.backfill import rows_needing_signals

DEFAULT_LIMIT = 100


def scan(item_id: uuid.UUID | None, limit: int) -> dict[str, Any]:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import PlanItem  # noqa: PLC0415

    statement = select(PlanItem.id, PlanItem.clip_assignments)
    statement = (
        statement.where(PlanItem.id == item_id)
        if item_id is not None
        else statement.order_by(PlanItem.id).limit(limit)
    )
    missing_by_signal: Counter[str] = Counter()
    items = clips = pending_clips = 0
    with sync_session() as db:
        for _id, assignments in db.execute(statement):
            items += 1
            clips += len(assignments or [])
            for pending in rows_needing_signals(assignments or []):
                pending_clips += 1
                missing_by_signal.update(pending.missing)
    return {
        "signals_version": clip_signals.signals_version(),
        "implemented": sorted(s.name for s in clip_signals.specs() if s.implemented),
        "items_scanned": items,
        "clips_scanned": clips,
        "clips_needing_signals": pending_clips,
        "missing_by_signal": dict(sorted(missing_by_signal.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--item-id", type=uuid.UUID, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--write", action="store_true", help="refused until a store is wired")
    args = parser.parse_args(argv)
    if args.write:
        print(
            "refusing --write: no fenced signal store is wired and no signal is implemented yet",
            file=sys.stderr,
        )
        return 2
    report = scan(args.item_id, max(1, args.limit))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
