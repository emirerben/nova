"""One-off, owner-reviewed repair of editor draft heads left on an old generation.

    python -m app.cli.repair_editor_heads --item <uuid> [--variant <key>]            # dry run
    python -m app.cli.repair_editor_heads --item <uuid> --variant <key> --apply \\
        --operation {retire,keep-ai-lanes}
    python -m app.cli.repair_editor_heads --list-stranded [--limit N]

The server never moves such a head by itself (``app/kria/editor_head.py``): it is not current
and readers ignore it. This tool is the only code that writes a head for that reason (besides the
Save-time replacement of a stranded pinned saved draft, ``replace_stranded_saved_draft``). It
exists for an owner who has looked at one item. The dry run
(the default; the session is read-only) prints, per variant, the head, its generations, the
pinned draft, its flat lanes and exactly what each operation would leave. ``--apply`` needs an
explicit ``--operation`` and an explicit ``--variant``, runs in one transaction (Item, then
Job, the head last) and a second run finds nothing to do. A head that is current is reported as
such and refused.

``retire``         the head becomes the committed document (what a fresh read would create).
``keep-ai-lanes``  the head is stamped current and keeps its flat lanes, over the committed
                   video; ``sections`` is the committed document. Use it only if nothing was
                   saved or rendered after the draft. It is reachable from this tool alone.

Production runs need the owner's approval: show the dry run first.
"""

from __future__ import annotations

import argparse
import copy
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.database import sync_session
from app.kria.drafts import KriaDraftDocument, canonical_snapshot, committed_document
from app.kria.editor_head import is_head_current, payload_base, variant_baseline
from app.models import CreationThread, CreatorEditDraft, Job, PlanItem

DEFAULT_LIST_LIMIT = 50

# Heads on an editor payload that ``editor_head.is_head_current`` rejects: another job, or a column
# or payload stamp that is neither the variant's baseline nor the generation its phone publish was
# made from (``published_source_generation``). ``variants`` is guarded because an old or broken
# job may hold a non-array.
_STRANDED_SQL = text(
    """
    SELECT d.id, d.item_id, d.variant_key, d.base_job_id, j.id AS current_job,
           d.base_generation_id,
           d.snapshot_json->'editor_payload'->>'base_generation' AS payload_base,
           NULLIF(s.gen, '') AS gen,
           v->'editor_draft'->>'draft_id' AS pinned
    FROM creator_edit_drafts d
    JOIN plan_items i ON i.id = d.item_id
    JOIN jobs j ON j.id = i.current_job_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(j.assembly_plan->'variants') = 'array'
             THEN j.assembly_plan->'variants' ELSE '[]'::jsonb END
    ) v
    CROSS JOIN LATERAL (
        SELECT COALESCE(NULLIF(v->>'render_generation_id', ''), v->>'render_finished_at', '')
                   AS gen,
               CASE WHEN j.assembly_plan->'_device_render_v1'->d.variant_key->'status'->>'phase'
                         = 'published'
                     AND COALESCE(v->>'render_generation_id', '') <> ''
                     AND j.assembly_plan->'_device_render_v1'->d.variant_key->>'published_attempt'
                         = v->>'render_generation_id'
                    THEN NULLIF(
                        j.assembly_plan->'_device_render_v1'->d.variant_key->'status'
                            ->>'base_generation', '')
               END AS src
    ) s
    WHERE d.is_head
      AND jsonb_typeof(d.snapshot_json->'editor_payload') = 'object'
      AND v->>'variant_id' = d.variant_key
      AND COALESCE(v->>'editor_state', '') <> 'empty'
      AND NOT (
        d.base_job_id IS NOT DISTINCT FROM j.id
        AND COALESCE(d.base_generation_id, '') IN (s.gen, COALESCE(s.src, s.gen))
        AND COALESCE(d.snapshot_json->'editor_payload'->>'base_generation', '')
            IN (s.gen, COALESCE(s.src, s.gen))
      )
    ORDER BY d.updated_at DESC
    LIMIT :limit
    """
)


Operation = Literal["retire", "keep-ai-lanes"]
OPERATIONS: tuple[Operation, ...] = ("retire", "keep-ai-lanes")

KEEP_WARNING = (
    "keep-ai-lanes puts these lanes over the committed video: use only if nothing was saved or "
    "rendered after this draft"
)

# Keys of an editor payload that are not an edit lane.
_NON_LANE_KEYS = frozenset(
    {
        "base_generation",
        "sections",
        "editor_state",
        "editor_state_version",
        "operation",
        "candidate_id",
        "expected_revision",
        "copilot_receipt_ids",
        "learning_temporary",
        "deletions",
        "accepted_suggestion_ids",
        "retry_guided_revision",
        "guided_revision",
        "guided_revision_number",
    }
)


def flat_lane_names(payload: dict[str, Any] | None) -> list[str]:
    """The top-level lanes of an editor payload: what a chat turn changed and has not saved."""
    if not isinstance(payload, dict):
        return []
    # `remove_music: false` is a Save default the phone always encodes, not a lane.
    return [
        key
        for key, value in payload.items()
        if key not in _NON_LANE_KEYS and not (key == "remove_music" and value is False)
    ]


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
    current: bool = False
    other_job: bool = False
    empty_state: bool = False
    flat_lanes: tuple[str, ...] = ()

    @property
    def needs_repair(self) -> bool:
        return (
            self.head_id is not None
            and self.has_payload
            and not self.current
            and not self.empty_state
        )


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
    base = payload_base(head)
    pinned = (variant.get("editor_draft") or {}).get("draft_id")
    payload = (head.snapshot_json or {}).get("editor_payload")
    payload = payload if isinstance(payload, dict) else {}
    return HeadReport(
        variant_key=key,
        generation=generation,
        head_id=head.id,
        revision=head.draft_revision,
        column_base=head.base_generation_id,
        payload_base=base,
        base_job_id=head.base_job_id,
        pinned=str(pinned) if pinned else None,
        has_payload=base is not None,
        current=is_head_current(job, variant, head),
        other_job=head.base_job_id != job.id,
        empty_state=variant.get("editor_state") == "empty",
        flat_lanes=tuple(flat_lane_names(payload)),
    )


def _effect(report: HeadReport, operation: Operation) -> str:
    """What ``operation`` would leave of this head."""
    if operation == "retire":
        return "the head becomes the committed document: nothing pending, no flat lane kept"
    if report.other_job:
        return "refused: the head is of another job, its lanes do not exist on this timeline"
    lanes = ", ".join(report.flat_lanes) or "none"
    return f"stamped current; flat lanes kept: {lanes}; sections become the committed document"


def _line(report: HeadReport) -> str:
    if report.head_id is None:
        return f"variant {report.variant_key} generation={report.generation or '-'}\n  no head"
    head = (
        f"  head={report.head_id} rev={report.revision} column_base={report.column_base or '-'} "
        f"payload_base={report.payload_base if report.payload_base is not None else '-'} "
        f"base_job_id={report.base_job_id} pinned={report.pinned or '-'} "
        f"current={'yes' if report.current else 'no'}"
    )
    notes = []
    if report.other_job:
        notes.append("other job")
    if not report.has_payload:
        notes.append("no editor payload, not touched")
    if report.empty_state:
        notes.append("empty-state variant, not touched")
    if report.current:
        notes.append("current, not touched")
    suffix = f" ({'; '.join(notes)})" if notes else ""
    lines = [
        f"variant {report.variant_key} generation={report.generation or '-'}",
        f"{head}{suffix}",
    ]
    if report.needs_repair:
        lines.append(f"  flat lanes: {', '.join(report.flat_lanes) or 'none'}")
        lines.extend(f"  {name}: {_effect(report, name)}" for name in OPERATIONS)
        lines.append(f"  WARNING: {KEEP_WARNING}")
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


def _variant(job: Job, key: str) -> dict[str, Any]:
    return next(row for row in _variants(job) if str(row.get("variant_id") or "") == key)


def _locked_head(db: Session, item_id: uuid.UUID, variant_key: str) -> CreatorEditDraft | None:
    return db.execute(
        select(CreatorEditDraft)
        .where(
            CreatorEditDraft.item_id == item_id,
            CreatorEditDraft.variant_key == variant_key,
            CreatorEditDraft.is_head.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _replace_head(
    db: Session,
    item: PlanItem,
    job: Job,
    key: str,
    head: CreatorEditDraft,
    document: KriaDraftDocument,
) -> None:
    """Demote ``head`` and insert ``document`` as the head on the variant's generation.

    The variant's pinned saved draft follows only when it IS the replaced head.
    """
    variant = _variant(job, key)
    snapshot, digest = canonical_snapshot(document)
    revision = int(
        db.execute(
            select(func.coalesce(func.max(CreatorEditDraft.draft_revision), -1)).where(
                CreatorEditDraft.item_id == item.id, CreatorEditDraft.variant_key == key
            )
        ).scalar_one()
    )
    head.is_head = False
    db.flush()
    row = CreatorEditDraft(
        creator_id=head.creator_id,
        thread_id=head.thread_id,
        item_id=item.id,
        variant_key=key,
        base_job_id=job.id,
        base_generation_id=variant_baseline(variant) or None,
        draft_revision=revision + 1,
        parent_draft_id=head.id,
        snapshot_json=snapshot,
        snapshot_hash=digest,
        is_head=True,
    )
    db.add(row)
    db.flush()
    reference = variant.get("editor_draft")
    if (
        variant.get("editor_state_version") == 2
        and isinstance(reference, dict)
        and str(reference.get("draft_id")) == str(head.id)
    ):
        # JSON columns are replaced, never mutated in place.
        assembly = copy.deepcopy(job.assembly_plan)
        pinned = next(v for v in assembly["variants"] if v.get("variant_id") == key)
        pinned["editor_draft"] = {**pinned["editor_draft"], "draft_id": str(row.id)}
        job.assembly_plan = assembly


def _committed(db: Session, item: PlanItem, job: Job, key: str, head: CreatorEditDraft):  # noqa: ANN202
    thread = db.get(CreationThread, head.thread_id)
    return committed_document(
        state=dict(thread.state or {}) if thread is not None else {},
        item=item,
        job=job,
        variant_key=key,
    )


def _keep_ai_lanes(db: Session, item: PlanItem, job: Job, key: str, head: CreatorEditDraft) -> None:
    """Stamp the head current and keep its flat lanes; ``sections`` is the committed document."""
    committed = _committed(db, item, job, key, head)
    snapshot = dict(head.snapshot_json or {})
    snapshot["editor_payload"] = {
        **snapshot["editor_payload"],
        "sections": (committed.editor_payload or {}).get("sections") or {},
        "base_generation": variant_baseline(_variant(job, key)),
    }
    _replace_head(db, item, job, key, head, KriaDraftDocument.model_validate(snapshot))


def repair_item(
    db: Session, item_id: uuid.UUID, variant_key: str, operation: Operation
) -> list[tuple[HeadReport, HeadReport | None]]:
    """Apply ``operation`` to the item's head for ``variant_key``; the caller commits.

    Lock order is Item, then Job; the head is locked last.
    """
    item = db.get(PlanItem, item_id, with_for_update=True, populate_existing=True)
    if item is None or item.current_job_id is None:
        return []
    job = db.get(Job, item.current_job_id, with_for_update=True, populate_existing=True)
    if job is None:
        return []
    results: list[tuple[HeadReport, HeadReport | None]] = []
    for variant in _targets(job, variant_key):
        before = _report(db, item, job, variant)
        after = None
        if before.needs_repair and not (operation == "keep-ai-lanes" and before.other_job):
            head = _locked_head(db, item.id, before.variant_key)
            if operation == "retire":
                _replace_head(
                    db,
                    item,
                    job,
                    before.variant_key,
                    head,
                    _committed(db, item, job, before.variant_key, head),
                )
            else:
                _keep_ai_lanes(db, item, job, before.variant_key, head)
            after = _report(db, item, job, variant)
        results.append((before, after))
    return results


def list_stranded(db: Session, limit: int) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in db.execute(_STRANDED_SQL, {"limit": limit})]


def _print_item(
    reports: list[tuple[HeadReport, HeadReport | None]], operation: Operation | None
) -> int:
    for before, after in reports:
        print(_line(before))
        if after is not None:
            print(f"  {operation} -> new head={after.head_id}")
            print(_line(after))
    return sum(
        1
        for before, after in reports
        if before.needs_repair and (after is not None or operation is None)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.repair_editor_heads", description=__doc__
    )
    parser.add_argument("--item", type=uuid.UUID, help="plan item id")
    parser.add_argument("--variant", help="variant key (required with --apply)")
    parser.add_argument(
        "--apply", action="store_true", help="move the head; the default is a dry run"
    )
    parser.add_argument(
        "--operation",
        choices=OPERATIONS,
        help="required with --apply: retire, or keep-ai-lanes (read the dry run's warning)",
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
    if args.apply and (args.operation is None or args.variant is None):
        parser.error("--apply requires an explicit --operation and an explicit --variant")
    if args.operation is not None and not args.apply:
        parser.error("--operation only applies with --apply; a dry run prints every operation")

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
            results = repair_item(db, args.item, args.variant, args.operation)
            repaired = _print_item(results, args.operation)
            if any(before.current for before, _ in results):
                db.rollback()
                print(
                    "refused: this head is current; moving it would drop edits that are "
                    "still current",
                    file=sys.stderr,
                )
                return 2
            db.commit()
            print(f"applied: {repaired} head(s) replaced" if repaired else "nothing to do")
        else:
            repaired = _print_item(
                [(r, None) for r in inspect_item(db, args.item, args.variant)], None
            )
            db.rollback()
            print(
                f"dry run: {repaired} head(s) would be replaced; pass --apply"
                if repaired
                else "nothing to do"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
