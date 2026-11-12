"""When a draft head is "current", and the only two moves the server makes on it.

An AI chat edit is a ``CreatorEditDraft`` head staged on the generation the variant had when the
edit was made. When the variant's generation later moves, the head is either (a) moved by a
writer that KNOWS what the move did, or (b) left where it is: a head on an old generation is not
current and every reader ignores it, exactly as before this module existed. The server never
merges draft content and never guesses; the phone converges on the latest by itself.

The two moves, each made inside the writer's own transaction (Item and Job locked by the caller):

* ``retire``  approval execution. The render holds the approved edit, so the head becomes the
  document a fresh read would create for the variant: nothing is pending.
* ``restamp`` phone publish. Same document, new generation id, and only for a head that sits on
  the very generation the publish was made from.

Lock order is Plan, Item, Job, Session, Turn, Draft; the head is locked last, here. Never call
``record_pipeline_event`` from this module: it opens its own connection to the jobs row and
self-deadlocks under FOR UPDATE. ``drafts.py`` imports this module, so draft helpers are imported
inside the functions that need them.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Literal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import CreationThread, CreatorEditDraft, Job, PlanItem

log = structlog.get_logger(__name__)

MAX_RESTAGE_CHAIN = 16


def variant_baseline(variant: dict[str, Any]) -> str:
    from app.routes.generative_jobs import variant_render_baseline  # noqa: PLC0415

    return variant_render_baseline(variant)


def current_bases(job: Any, variant: dict[str, Any]) -> frozenset[str]:  # noqa: ANN401
    """Generations a draft may name and still be on the variant's current document.

    The render baseline, plus the generation a phone publish was made from: publishing
    replaces ``render_generation_id`` with the upload attempt id while the Save that
    produced it pinned the generation the phone was asked to render.
    """
    from app.services.device_render import published_source_generation  # noqa: PLC0415

    return frozenset(
        base
        for base in (variant_baseline(variant), published_source_generation(job, variant))
        if base
    )


def payload_base(head: CreatorEditDraft) -> str | None:
    """The generation stamped inside the head's editor payload; None when it has none."""
    snapshot = head.snapshot_json
    payload = snapshot.get("editor_payload") if isinstance(snapshot, dict) else None
    if not isinstance(payload, dict):
        return None
    return str(payload.get("base_generation") or "")


def is_head_current(job: Any, variant: dict[str, Any], head: CreatorEditDraft) -> bool:  # noqa: ANN401
    """The head belongs to this job and both of its generation stamps are current."""
    if head.base_job_id != job.id:
        return False
    bases = current_bases(job, variant)
    if not variant_baseline(variant):
        # A variant that never finished a render has nothing to be stale against; its
        # drafts carry the empty stamp.
        bases = bases | {""}
    if (head.base_generation_id or "") not in bases:
        return False
    base = payload_base(head)
    return base is None or base in bases


def is_head_on_baseline(job: Any, variant: dict[str, Any], head: CreatorEditDraft) -> bool:  # noqa: ANN401
    """Stricter than ``is_head_current``: both stamps equal the baseline itself."""
    baseline = variant_baseline(variant)
    base = payload_base(head)
    return (
        head.base_job_id == job.id
        and (head.base_generation_id or "") == baseline
        and (base is None or base == baseline)
    )


# --- restage lineage ----------------------------------------------------------------------
#
# A row the server wrote carries ``restage: {of, reason, origin_revision}``: the row it moved,
# the writer, and the revision of the creator-authored row at the start of the chain. Only the
# approval's own render and a phone publish leave an approval's draft "the one the creator is
# looking at"; a manual Save, a speech cut or a repair does not.

OWN_RENDER_REASONS = frozenset({"approval_execution", "phone_publish"})
RESTAMP_REASONS = frozenset({"phone_publish"})


def restage_marker(row: Any) -> dict[str, Any] | None:  # noqa: ANN401
    snapshot = getattr(row, "snapshot_json", None)
    marker = snapshot.get("restage") if isinstance(snapshot, dict) else None
    return marker if isinstance(marker, dict) else None


def is_restage_row(row: Any) -> bool:  # noqa: ANN401
    return restage_marker(row) is not None


def authored_revision(row: Any) -> int | None:  # noqa: ANN401
    """The revision a creator could have seen: a restage row stands for the row it moved."""
    marker = restage_marker(row)
    origin = marker.get("origin_revision") if marker is not None else None
    return int(origin) if isinstance(origin, int) else getattr(row, "draft_revision", None)


def draft_still_current(db: Session, draft: CreatorEditDraft) -> bool:
    """The creator has not moved past ``draft``: it is the head, or the head is its own render.

    Every link from the head back to ``draft`` must be a restage (it records ``restage.of``)
    whose reason is the approval's own render or a phone publish, and the head must still be
    current for its variant: a manual Save moves the variant past a head the server did not move.
    An old pending approval, render retry or intent-review post never runs on top of those.
    """
    if draft.is_head:
        return True
    head = db.execute(
        select(CreatorEditDraft).where(
            CreatorEditDraft.item_id == draft.item_id,
            CreatorEditDraft.variant_key == draft.variant_key,
            CreatorEditDraft.is_head.is_(True),
        )
    ).scalar_one_or_none()
    if head is None or head.base_job_id != draft.base_job_id:
        return False
    link: CreatorEditDraft | None = head
    for _ in range(MAX_RESTAGE_CHAIN):
        marker = restage_marker(link)
        if (
            marker is None
            or marker.get("reason") not in OWN_RENDER_REASONS
            or str(marker.get("of")) != str(link.parent_draft_id)
        ):
            return False
        parent = db.get(CreatorEditDraft, link.parent_draft_id)
        if parent is None:
            return False
        if parent.id == draft.id:
            job = db.get(Job, head.base_job_id) if head.base_job_id is not None else None
            variant = _variant_of(job, str(head.variant_key)) if job is not None else None
            return variant is not None and is_head_current(job, variant, head)
        link = parent
    return False


def locked_head(db: Session, item_id: uuid.UUID, variant_key: str) -> CreatorEditDraft | None:
    statement = (
        select(CreatorEditDraft)
        .where(
            CreatorEditDraft.item_id == item_id,
            CreatorEditDraft.variant_key == variant_key,
            CreatorEditDraft.is_head.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # A writer that held the lock before us demotes its head and inserts a new one. The
    # lock wait re-checks "is_head" on the demoted row and skips it, and our statement
    # snapshot predates the new row, so only a second statement can see the new head.
    for _ in range(2):
        head = db.execute(statement).scalar_one_or_none()
        if head is not None:
            return head
    return None


def _variant_of(job: Any, variant_key: str) -> dict[str, Any] | None:  # noqa: ANN401
    from app.kria.drafts import _variant  # noqa: PLC0415

    return _variant(job, variant_key)


def _pinned_id(variant: dict[str, Any] | None) -> str | None:
    """The saved draft a v2 / empty-state variant pins, as written by its Save."""
    if variant is None or not (
        variant.get("editor_state") == "empty" or variant.get("editor_state_version") == 2
    ):
        return None
    reference = variant.get("editor_draft")
    pinned = reference.get("draft_id") if isinstance(reference, dict) else None
    return str(pinned) if pinned else None


def _repoint_pinned_draft(job: Job, variant_key: str, head_id: uuid.UUID) -> bool:
    """Point the variant's pinned ``editor_draft`` at ``head_id``. True when changed.

    The caller has already established that the row it replaces is the pinned one: a chat or
    phone draft that merely sits on the baseline is never the saved draft.
    """
    assembly = job.assembly_plan or {}
    variant = _variant_of(job, variant_key)
    pinned = _pinned_id(variant)
    if pinned is None or pinned == str(head_id):
        return False
    # JSON columns are replaced, never mutated in place: an in-place edit is not persisted.
    assembly = copy.deepcopy(assembly)
    target = next(
        value
        for value in assembly["variants"]
        if isinstance(value, dict) and str(value.get("variant_id") or "") == variant_key
    )
    target["editor_draft"] = {**target["editor_draft"], "draft_id": str(head_id)}
    job.assembly_plan = assembly
    return True


HeadOperation = Literal["restamp", "retire"]


def insert_restage_row(
    db: Session,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    head: CreatorEditDraft,
    document: Any,  # noqa: ANN401
    reason: str,
    retires_pinned: bool = False,
) -> CreatorEditDraft:
    """Demote ``head`` and insert ``document`` as the new head on the variant's baseline.

    The variant's pinned saved draft follows the new row only when it is the row replaced: the
    head itself, or (``retires_pinned``) a stranded pinned row this document supersedes.
    """
    from app.kria.drafts import canonical_snapshot  # noqa: PLC0415

    baseline = variant_baseline(_variant_of(job, variant_key) or {})
    document = document.model_copy(
        update={
            "restage": {
                "of": str(head.id),
                "reason": reason,
                "origin_revision": authored_revision(head),
            }
        }
    )
    snapshot, snapshot_hash = canonical_snapshot(document)
    latest_revision = int(
        db.execute(
            select(func.coalesce(func.max(CreatorEditDraft.draft_revision), -1)).where(
                CreatorEditDraft.item_id == item.id,
                CreatorEditDraft.variant_key == variant_key,
            )
        ).scalar_one()
    )
    head.is_head = False
    db.flush()
    row = CreatorEditDraft(
        creator_id=creator_id,
        thread_id=head.thread_id,
        item_id=item.id,
        variant_key=variant_key,
        base_job_id=job.id,
        base_generation_id=baseline or None,
        draft_revision=latest_revision + 1,
        parent_draft_id=head.id,
        snapshot_json=snapshot,
        snapshot_hash=snapshot_hash,
        is_head=True,
    )
    db.add(row)
    db.flush()
    if retires_pinned or _pinned_id(_variant_of(job, variant_key)) == str(head.id):
        _repoint_pinned_draft(job, variant_key, row.id)
    log.info(
        "editor_head_restaged",
        reason=reason,
        item_id=str(item.id),
        job_id=str(job.id),
        variant_key=variant_key,
        from_generation=head.base_generation_id,
        to_generation=baseline,
    )
    return row


def restage_editor_head_sync(
    db: Session,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    operation: HeadOperation,
    reason: str,
    retires_pinned: bool = False,
) -> CreatorEditDraft | None:
    """Move the variant's editor head by ``operation``; None when there is nothing to do.

    Nothing to do: no editor head; the head already sits on the baseline (a retried Celery task
    lands here); ``retire`` of an empty-state variant (its content lives only in its draft); or
    ``restamp`` of a head that is not on the generation the phone's publish was made from.
    ``retires_pinned`` (the Save path) supersedes a stranded pinned draft with the committed
    document even when the head is a different row; the pin never follows any other row.
    The caller holds Item and Job and commits.
    """
    from app.kria.drafts import KriaDraftDocument, committed_document  # noqa: PLC0415
    from app.services.device_render import published_source_generation  # noqa: PLC0415

    variant = _variant_of(job, variant_key)
    if variant is None:
        return None
    head = locked_head(db, item.id, variant_key)
    if head is None:
        return None
    pinned = _pinned_id(variant)
    stranded_pin = retires_pinned and operation == "retire" and pinned not in (None, str(head.id))
    if not stranded_pin and (payload_base(head) is None or is_head_on_baseline(job, variant, head)):
        return None
    if operation == "restamp":
        source = published_source_generation(job, variant)
        if (
            not source
            or head.base_job_id != job.id
            or (head.base_generation_id or "") != source
            or payload_base(head) != source
        ):
            return None
        document = dict(head.snapshot_json or {})
        document.pop("restage", None)
        document["editor_payload"] = {
            **(document.get("editor_payload") or {}),
            "base_generation": variant_baseline(variant),
        }
        moved = KriaDraftDocument.model_validate(document)
    else:
        if variant.get("editor_state") == "empty":
            return None
        thread = db.get(CreationThread, head.thread_id)
        moved = committed_document(
            state=dict(thread.state or {}) if thread is not None else {},
            item=item,
            job=job,
            variant_key=variant_key,
        )
    return insert_restage_row(
        db,
        item=item,
        job=job,
        variant_key=variant_key,
        creator_id=creator_id,
        head=head,
        document=moved,
        reason=reason,
        retires_pinned=retires_pinned,
    )


async def restage_editor_head(
    db: AsyncSession,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    operation: HeadOperation,
    reason: str,
    retires_pinned: bool = False,
) -> CreatorEditDraft | None:
    """Async twin of :func:`restage_editor_head_sync`; the same code runs through ``run_sync``."""

    def run(session: Session) -> CreatorEditDraft | None:
        return restage_editor_head_sync(
            session,
            item=item,
            job=job,
            variant_key=variant_key,
            creator_id=creator_id,
            operation=operation,
            reason=reason,
            retires_pinned=retires_pinned,
        )

    return await db.run_sync(run)
