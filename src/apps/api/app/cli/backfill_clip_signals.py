"""Backfill measured clip signals onto clips that are already understood.

    python -m app.cli.backfill_clip_signals                         # dry run: what is missing
    python -m app.cli.backfill_clip_signals --item-id <uuid>        # one plan item
    python -m app.cli.backfill_clip_signals --write --limit 200     # measure + store
    python -m app.cli.backfill_clip_signals --write --after-item-id <next_after_item_id>

Dry run (the default) downloads nothing. ``--write`` downloads each clip at its registered storage
generation, measures the still-missing implemented signals, and stores them through the same
fenced path as ``tasks/kria_clip_understanding._store`` (lock the PlanItem, match ``media_id`` +
``gcs_path`` + generation, merge through ``mutate_plan_item_media`` so the speech-cleanup policy
is re-checked, roll back if footage identity would change). Each signal only writes its own key
(``clip_signals.merge_signals``). Selection looks at the ``signals`` entries alone, so a re-run
skips what is done: it is idempotent, and resumable from the ``next_after_item_id`` it prints.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.services import clip_signals
from app.services.clip_signals import registry
from app.services.clip_signals.backfill import Pending, rows_needing_signals

DEFAULT_LIMIT = 100
DEFAULT_MAX_CLIPS = 200
DEFAULT_RATE_PER_S = 2.0
# Originals can be large and a measurement needs a few frames: a huge file is skipped rather than
# pulled whole into a worker.
DEFAULT_MAX_BYTES = 400 * 1024 * 1024


def implemented_missing(pending: Pending, only: frozenset[str] | None) -> frozenset[str]:
    """The signals this run can actually compute for a pending clip (stubs never count)."""
    out = set()
    for name in pending.missing:
        spec = registry.get(name)
        if spec is not None and spec.implemented and (only is None or name in only):
            out.add(name)
    return frozenset(out)


def fenced_merge(
    rows: Iterable[Any],
    *,
    media_id: str,
    gcs_path: str,
    generation: str,
    computed: Mapping[str, Mapping[str, Any]],
) -> list[Any] | None:
    """``rows`` with ``computed`` merged into the one matching clip, or None when nothing should
    be written: the clip is gone, its path was replaced, its object generation moved while we
    measured, or every signal is already stored (a concurrent run got there first)."""
    out: list[Any] = []
    changed = False
    for row in rows:
        if (
            isinstance(row, dict)
            and str(row.get("media_id")) == media_id
            and str(row.get("gcs_path")) == gcs_path
        ):
            expected = str(row.get("storage_generation") or row.get("generation") or "")
            if expected and expected != generation:
                out.append(row)  # replaced while we measured: the hashes describe other bytes
                continue
            current = registry.read_signals(row.get("analysis"))
            fresh = {name: data for name, data in computed.items() if name not in current}
            if fresh:
                out.append({**row, "analysis": registry.merge_signals(row.get("analysis"), fresh)})
                changed = True
                continue
        out.append(row)
    return out if changed else None


def store_signals(
    item_id: uuid.UUID,
    *,
    media_id: str,
    gcs_path: str,
    generation: str,
    computed: Mapping[str, Mapping[str, Any]],
) -> bool:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import PlanItem  # noqa: PLC0415
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        mutation_current_analysis_sync,
    )

    with sync_session() as db:
        item = db.execute(
            select(PlanItem).where(PlanItem.id == item_id).with_for_update()
        ).scalar_one_or_none()
        if item is None:
            return False
        rows = fenced_merge(
            item.clip_assignments or [],
            media_id=media_id,
            gcs_path=gcs_path,
            generation=generation,
            computed=computed,
        )
        if rows is None:
            return False
        result = mutate_plan_item_media(
            item,
            detector_policy=current_detector_policy(),
            clip_assignments=rows,
            current_analysis=mutation_current_analysis_sync(db, item.id, for_update=True),
        )
        if result.source_changed:
            db.rollback()  # an analysis-only write must never change footage identity
            return False
        db.commit()
        return True


def _items(item_id: uuid.UUID | None, after: uuid.UUID | None, limit: int) -> list[Any]:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import PlanItem  # noqa: PLC0415

    statement = select(PlanItem.id, PlanItem.clip_assignments)
    if item_id is not None:
        statement = statement.where(PlanItem.id == item_id)
    else:
        if after is not None:
            statement = statement.where(PlanItem.id > after)
        statement = statement.order_by(PlanItem.id).limit(limit)
    with sync_session() as db:
        return [(row_id, list(assignments or [])) for row_id, assignments in db.execute(statement)]


def _duration(row: Mapping[str, Any]) -> float | None:
    analysis = row.get("analysis")
    for source in (row, analysis if isinstance(analysis, Mapping) else {}):
        value = source.get("duration_s")
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def _measure(
    row: Mapping[str, Any], pending: Pending, todo: frozenset[str], max_bytes: int
) -> tuple[str, str, dict[str, Any]]:
    """``(outcome, generation, computed)`` for one clip: download its registered generation and
    measure ``todo``. An outcome other than ``measured`` says why the clip was skipped."""
    from app import storage  # noqa: PLC0415

    metadata = storage.object_metadata(pending.gcs_path)
    generation = str(metadata.generation)
    registered = str(row.get("storage_generation") or "")
    if registered and registered != generation:
        return "generation_changed", generation, {}
    if metadata.size and metadata.size > max_bytes:
        return "too_large", generation, {}
    with tempfile.TemporaryDirectory(prefix="clip-signals-") as tmp:
        local = str(Path(tmp) / (Path(pending.gcs_path).name or "media"))
        storage.download_generation_to_file(pending.gcs_path, local, generation=generation)
        analysis = row.get("analysis")
        computed = clip_signals.compute_signals(
            clip_signals.SignalInputs(
                media_id=pending.media_id,
                kind=pending.kind,
                local_path=local,
                duration_s=_duration(row),
                analysis=analysis if isinstance(analysis, Mapping) else None,
            ),
            only=todo,
        )
    return ("measured" if computed else "no_result"), generation, computed


def _process(
    item_id: uuid.UUID,
    row: Mapping[str, Any],
    pending: Pending,
    todo: frozenset[str],
    max_bytes: int,
) -> str:
    try:
        outcome, generation, computed = _measure(row, pending, todo, max_bytes)
        if outcome != "measured":
            return outcome
        stored = store_signals(
            item_id,
            media_id=pending.media_id,
            gcs_path=pending.gcs_path,
            generation=generation,
            computed=computed,
        )
        return "stored" if stored else "not_stored"
    except Exception as exc:  # noqa: BLE001 - one bad clip never stops the run
        return f"error:{type(exc).__name__}"


def run(
    *,
    item_id: uuid.UUID | None = None,
    after_item_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
    write: bool = False,
    only: frozenset[str] | None = None,
    max_clips: int = DEFAULT_MAX_CLIPS,
    rate_per_s: float = DEFAULT_RATE_PER_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    missing_by_signal: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    items = clips = pending_clips = attempted = 0
    resume_after: uuid.UUID | None = None
    stopped_early = False
    pause = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
    for row_id, assignments in _items(item_id, after_item_id, limit):
        by_media = {str(r.get("media_id")): r for r in assignments if isinstance(r, dict)}
        todo_rows = [
            (p, todo)
            for p in rows_needing_signals(assignments)
            if (todo := implemented_missing(p, only))
        ]
        if write and attempted + len(todo_rows) > max_clips and attempted > 0:
            # Whole items only, so the cursor is exact: stop before the item that would overshoot.
            stopped_early = True
            break
        items += 1
        clips += len(assignments)
        for pending, todo in todo_rows:
            pending_clips += 1
            missing_by_signal.update(todo)
            if not write:
                continue
            attempted += 1
            outcomes[_process(row_id, by_media[pending.media_id], pending, todo, max_bytes)] += 1
            if pause:
                time.sleep(pause)
        resume_after = row_id
    return {
        "mode": "write" if write else "dry_run",
        "signals_version": clip_signals.signals_version(),
        "implemented": sorted(s.name for s in clip_signals.specs() if s.implemented),
        "items_scanned": items,
        "clips_scanned": clips,
        "clips_needing_signals": pending_clips,
        "missing_by_signal": dict(sorted(missing_by_signal.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "next_after_item_id": str(resume_after) if resume_after is not None else None,
        "stopped_early": stopped_early,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--item-id", type=uuid.UUID, default=None)
    parser.add_argument("--after-item-id", type=uuid.UUID, default=None, help="resume cursor")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="plan items to scan")
    parser.add_argument("--signal", action="append", default=None, help="restrict to one signal")
    parser.add_argument("--write", action="store_true", help="measure and store (default: report)")
    parser.add_argument("--dry-run", action="store_true", help="report only (the default)")
    parser.add_argument("--max-clips", type=int, default=DEFAULT_MAX_CLIPS)
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_PER_S, help="clips per second")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if args.write and args.dry_run:
        parser.error("--write and --dry-run are opposites")
    only = frozenset(args.signal) if args.signal else None
    if only is not None and not only <= clip_signals.names():
        parser.error(f"unknown signal(s): {sorted(only - clip_signals.names())}")
    report = run(
        item_id=args.item_id,
        after_item_id=args.after_item_id,
        limit=max(1, args.limit),
        write=args.write,
        only=only,
        max_clips=max(1, args.max_clips),
        rate_per_s=args.rate,
        max_bytes=args.max_bytes,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
