"""Move stranded editor draft heads onto their variant's current generation.

    python -m app.cli.repair_editor_heads --item <uuid> [--variant <key>]            # dry run
    python -m app.cli.repair_editor_heads --item <uuid> [--variant <key>] --apply \\
        [--operation {heal,retire}]
    python -m app.cli.repair_editor_heads --list-stranded [--limit N]

The dry run prints, per variant of the item's current job, the head, whether it sits on the
variant's baseline and, for a head that needs repair, its flat lane names, whether it has
``sections`` and what each operation would do to it (see ``app/kria/editor_head.py``); it
changes nothing (the session is read-only). ``--apply`` runs one operation, in one transaction
per item, locking Item, then Job (the head is locked last inside the restage). ``heal`` (the
default) keeps the head's flat lanes, ``retire`` makes the head the committed document: pick it
when the committed video is known to contain the draft (an approval rendered, then the phone
published). A second run finds nothing to do.

Production runs need the owner's approval: show the dry run first.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import sync_session
from app.kria.editor_head import (
    HeadOperation,
    flat_lane_names,
    is_head_on_baseline,
    restage_editor_head_sync,
    variant_baseline,
)
from app.models import CreatorEditDraft, Job, PlanItem

REASON = "repair_cli"
DEFAULT_LIST_LIMIT = 50

# Heads on an editor payload whose column or payload stamp is not the variant's baseline.
# ``variants`` is guarded because an old or broken job may hold a non-array.
_STRANDED_SQL = text(
    """
    SELECT d.id, d.item_id, d.variant_key, d.base_job_id, j.id AS current_job,
           d.base_generation_id,
           d.snapshot_json->'editor_payload'->>'base_generation' AS payload_base,
           COALESCE(NULLIF(v->>'render_generation_id', ''), v->>'render_finished_at') AS gen,
           v->'editor_draft'->>'draft_id' AS pinned
    FROM creator_edit_drafts d
    JOIN plan_items i ON i.id = d.item_id
    JOIN jobs j ON j.id = i.current_job_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(j.assembly_plan->'variants') = 'array'
             THEN j.assembly_plan->'variants' ELSE '[]'::jsonb END
    ) v
    WHERE d.is_head
      AND jsonb_typeof(d.snapshot_json->'editor_payload') = 'object'
      AND v->>'variant_id' = d.variant_key
      AND (
        d.base_job_id IS DISTINCT FROM j.id
        OR COALESCE(d.base_generation_id, '') IS DISTINCT FROM
           COALESCE(NULLIF(v->>'render_generation_id', ''), v->>'render_finished_at', '')
        OR COALESCE(d.snapshot_json->'editor_payload'->>'base_generation', '') IS DISTINCT FROM
           COALESCE(NULLIF(v->>'render_generation_id', ''), v->>'render_finished_at', '')
      )
    ORDER BY d.updated_at DESC
    LIMIT :limit
    """
)


OPERATIONS: tuple[HeadOperation, ...] = ("heal", "retire")


@dataclass(frozen=True)
class HeadReport:
    variant_key: str
    generation: str
    head_id: uuid.UUID | None = None
    revision: int | None = None
    column_base: str | None = None
    payload_base: str | None = None
    base_job_id: uuid.UUID | None = None
    pinned: str | None = None
    has_payload: bool = False
    on_baseline: bool = True
    other_job: bool = False
    empty_state: bool = False
    flat_lanes: tuple[str, ...] = ()
    has_sections: bool = False

    @property
    def needs_repair(self) -> bool:
        return self.head_id is not None and self.has_payload and not self.on_baseline


def _variants(job: Job) -> list[dict[str, Any]]:
    rows = (job.assembly_plan or {}).get("variants")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _head_of(db: Session, item_id: uuid.UUID, variant_key: str) -> CreatorEditDraft | None:
    return db.execute(
        select(CreatorEditDraft)
        .where(
            CreatorEditDraft.item_id == item_id,
            CreatorEditDraft.variant_key == variant_key,
            CreatorEditDraft.is_head.is_(True),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _report(db: Session, item: PlanItem, job: Job, variant: dict[str, Any]) -> HeadReport:
    key = str(variant.get("variant_id") or "")
    generation = variant_baseline(variant)
    head = _head_of(db, item.id, key)
    if head is None:
        return HeadReport(variant_key=key, generation=generation)
    from app.kria.editor_head import _payload_base  # noqa: PLC0415

    payload_base = _payload_base(head)
    pinned = (variant.get("editor_draft") or {}).get("draft_id")
    on_baseline = is_head_on_baseline(job, variant, head)
    payload = (head.snapshot_json or {}).get("editor_payload")
    payload = payload if isinstance(payload, dict) else {}
    return HeadReport(
        variant_key=key,
        generation=generation,
        head_id=head.id,
        revision=head.draft_revision,
        column_base=head.base_generation_id,
        payload_base=payload_base,
        base_job_id=head.base_job_id,
        pinned=str(pinned) if pinned else None,
        has_payload=payload_base is not None,
        on_baseline=on_baseline,
        other_job=head.base_job_id != job.id,
        empty_state=variant.get("editor_state") == "empty",
        flat_lanes=tuple(flat_lane_names(payload)),
        has_sections=isinstance(payload.get("sections"), dict),
    )


def _effect(report: HeadReport, operation: HeadOperation) -> str:
    """What ``operation`` would do to this head; the same rules as ``moved_document``."""
    if report.empty_state:
        return "sections kept as they are (empty-state variant); only the generation stamps move"
    if report.other_job:
        return "head of another job: becomes the committed document, no flat lane kept"
    if operation == "retire" or not report.flat_lanes:
        return "becomes the committed document: sections replaced, no flat lane kept"
    return (
        f"sections become the committed document; flat lanes kept: {', '.join(report.flat_lanes)}"
    )


def _line(report: HeadReport, operation: HeadOperation) -> str:
    if report.head_id is None:
        return f"variant {report.variant_key} generation={report.generation or '-'}\n  no head"
    head = (
        f"  head={report.head_id} rev={report.revision} column_base={report.column_base or '-'} "
        f"payload_base={report.payload_base if report.payload_base is not None else '-'} "
        f"base_job_id={report.base_job_id} pinned={report.pinned or '-'} "
        f"on_baseline={'yes' if report.on_baseline else 'no'}"
    )
    notes = []
    if report.other_job:
        notes.append("other job")
    if not report.has_payload:
        notes.append("no editor payload, not touched")
    suffix = f" ({'; '.join(notes)})" if notes else ""
    lines = [
        f"variant {report.variant_key} generation={report.generation or '-'}",
        f"{head}{suffix}",
    ]
    if report.needs_repair:
        lines.append(
            f"  flat lanes: {', '.join(report.flat_lanes) or 'none'}; "
            f"has sections: {'yes' if report.has_sections else 'no'}"
        )
        lines.extend(
            f"  {name}{' (selected)' if name == operation else ''}: {_effect(report, name)}"
            for name in OPERATIONS
        )
    return "\n".join(lines)


def _targets(job: Job, only: str | None) -> list[dict[str, Any]]:
    variants = _variants(job)
    if only is None:
        return variants
    return [row for row in variants if str(row.get("variant_id") or "") == only]


def inspect_item(db: Session, item_id: uuid.UUID, only: str | None = None) -> list[HeadReport]:
    """One report per variant of the item's current job; read-only."""
    item = db.get(PlanItem, item_id)
    job = db.get(Job, item.current_job_id) if item is not None and item.current_job_id else None
    if item is None or job is None:
        return []
    return [_report(db, item, job, variant) for variant in _targets(job, only)]


def repair_item(
    db: Session,
    item_id: uuid.UUID,
    only: str | None = None,
    operation: HeadOperation = "heal",
) -> list[tuple[HeadReport, HeadReport | None]]:
    """Move every stranded head of the item (before, after); the caller commits.

    Lock order is Item, then Job; the restage locks the head last.
    """
    item = db.get(PlanItem, item_id, with_for_update=True, populate_existing=True)
    if item is None or item.current_job_id is None:
        return []
    job = db.get(Job, item.current_job_id, with_for_update=True, populate_existing=True)
    if job is None:
        return []
    results: list[tuple[HeadReport, HeadReport | None]] = []
    for variant in _targets(job, only):
        before = _report(db, item, job, variant)
        after = None
        if before.needs_repair:
            head = _head_of(db, item.id, before.variant_key)
            restage_editor_head_sync(
                db,
                item=item,
                job=job,
                variant_key=before.variant_key,
                creator_id=head.creator_id,
                operation=operation,
                reason=REASON,
            )
            after = _report(db, item, job, variant)
        results.append((before, after))
    return results


def list_stranded(db: Session, limit: int) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in db.execute(_STRANDED_SQL, {"limit": limit})]


def _print_item(
    reports: list[tuple[HeadReport, HeadReport | None]], operation: HeadOperation
) -> int:
    for before, after in reports:
        print(_line(before, operation))
        if after is not None:
            print(f"  {operation} -> new head={after.head_id}")
            print(_line(after, operation))
    return sum(1 for before, _ in reports if before.needs_repair)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.repair_editor_heads", description=__doc__
    )
    parser.add_argument("--item", type=uuid.UUID, help="plan item id")
    parser.add_argument("--variant", help="only this variant key (default: every variant)")
    parser.add_argument("--apply", action="store_true", help="move heads; the default is a dry run")
    parser.add_argument(
        "--operation",
        choices=OPERATIONS,
        default="heal",
        help="heal keeps a head's flat lanes (default); retire makes it the committed document",
    )
    parser.add_argument(
        "--list-stranded", action="store_true", help="read-only list of stranded heads"
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIST_LIMIT)
    args = parser.parse_args(argv)

    if args.list_stranded:
        if args.apply or args.item:
            parser.error("--list-stranded is read-only and takes no --item or --apply")
        with sync_session() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            rows = list_stranded(db, max(1, args.limit))
            db.rollback()
        for row in rows:
            print(
                f"item={row['item_id']} variant={row['variant_key']} head={row['id']} "
                f"base_job_id={row['base_job_id']} current_job={row['current_job']} "
                f"column_base={row['base_generation_id'] or '-'} "
                f"payload_base={row['payload_base'] or '-'} generation={row['gen'] or '-'} "
                f"pinned={row['pinned'] or '-'}"
            )
        print(f"{len(rows)} stranded head(s) listed (limit {max(1, args.limit)})")
        return 0
    if args.item is None:
        parser.error("--item is required unless --list-stranded is given")

    with sync_session() as db:
        if not args.apply:
            db.execute(text("SET TRANSACTION READ ONLY"))
        item = db.get(PlanItem, args.item)
        if item is None:
            print(f"item {args.item} not found", file=sys.stderr)
            return 2
        if item.current_job_id is None:
            print(f"item {args.item} has no current job\nnothing to do")
            return 0
        print(f"item {args.item} current_job={item.current_job_id}")
        job = db.get(Job, item.current_job_id)
        if args.variant and not _targets(job, args.variant):
            print(f"variant {args.variant!r} not found on job {job.id}", file=sys.stderr)
            return 2
        if args.apply:
            repaired = _print_item(
                repair_item(db, args.item, args.variant, args.operation), args.operation
            )
            db.commit()
            print(f"applied: {repaired} head(s) restaged" if repaired else "nothing to do")
        else:
            repaired = _print_item(
                [(r, None) for r in inspect_item(db, args.item, args.variant)], args.operation
            )
            db.rollback()
            print(
                f"dry run: {repaired} head(s) would be restaged; pass --apply"
                if repaired
                else "nothing to do"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
