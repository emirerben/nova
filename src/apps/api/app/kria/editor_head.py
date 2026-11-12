"""The editor draft head always sits on the variant's current generation.

An AI chat edit is a ``CreatorEditDraft`` head staged on the generation the variant had
when the edit was made. An approval render, a phone publish or a legacy dispatcher then
moves the variant to a newer generation, and nothing re-based the head. This module is
the one definition of "current" and the one way to move a head onto it.

Lock order is Plan, Item, Job, Session, Turn, Draft. The caller already holds Item and
Job; the head is locked last, here. Never call ``record_pipeline_event`` from this module:
it opens its own connection to the jobs row and self-deadlocks under FOR UPDATE.

``drafts.py`` and the services import this module, so the draft helpers are imported
inside the restage function rather than at module level.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import CreatorEditDraft, Job, PlanItem

log = structlog.get_logger(__name__)

_MAX_RESTAGE_CHAIN = 16


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


def _payload_base(head: CreatorEditDraft) -> str | None:
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
    payload_base = _payload_base(head)
    return payload_base is None or payload_base in bases


def is_head_on_baseline(job: Any, variant: dict[str, Any], head: CreatorEditDraft) -> bool:  # noqa: ANN401
    """Stricter than ``is_head_current``: both stamps equal the baseline itself.

    A head current only through a phone publish still names the pre-publish generation;
    it is moved to the exact id the first time anyone observes it.
    """
    baseline = variant_baseline(variant)
    payload_base = _payload_base(head)
    return (
        head.base_job_id == job.id
        and (head.base_generation_id or "") == baseline
        and (payload_base is None or payload_base == baseline)
    )


def _stamps(row: CreatorEditDraft) -> tuple[str, str | None]:
    return (row.base_generation_id or "", _payload_base(row))


def _pending_changes(row: CreatorEditDraft) -> tuple[Any, Any]:
    snapshot = row.snapshot_json if isinstance(row.snapshot_json, dict) else {}
    return (snapshot.get("changes") or [], snapshot.get("editor_text_diff") or [])


def draft_still_current(db: Session, draft: CreatorEditDraft) -> bool:
    """The creator has not moved past ``draft``: it is the head, or the head is its restage.

    An approval render advances the generation and restages the head, which demotes the
    approved draft without anyone having edited anything. A restage is told from a creator
    write by what it changes: it always moves the generation stamps (a write heals the head
    first, so its stamps equal its parent's), and it never adds a pending change the approved
    draft did not carry (a contained restage clears them).
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
    allowed = _pending_changes(draft)
    link: CreatorEditDraft | None = head
    for _ in range(_MAX_RESTAGE_CHAIN):
        parent = db.get(CreatorEditDraft, link.parent_draft_id) if link.parent_draft_id else None
        if parent is None or _stamps(link) == _stamps(parent):
            return False
        if _pending_changes(link) not in (allowed, ([], [])):
            return False
        if parent.id == draft.id:
            return True
        link = parent
    return False


def _locked_head(db: Session, item_id: uuid.UUID, variant_key: str) -> CreatorEditDraft | None:
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


def _repoint_pinned_draft(job: Job, variant_key: str, head_id: uuid.UUID) -> bool:
    """Point a v2 / empty-state variant's ``editor_draft`` at ``head_id``. True when changed."""
    assembly = job.assembly_plan or {}
    variant = next(
        (
            value
            for value in assembly.get("variants") or []
            if isinstance(value, dict) and str(value.get("variant_id") or "") == variant_key
        ),
        None,
    )
    if (
        variant is None
        or not (variant.get("editor_state") == "empty" or variant.get("editor_state_version") == 2)
        or not isinstance(variant.get("editor_draft"), dict)
        or variant["editor_draft"].get("draft_id") == str(head_id)
    ):
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


def rebased_document(
    job: Any,  # noqa: ANN401
    variant_key: str,
    head: CreatorEditDraft,
    *,
    contained: bool,
) -> Any:  # noqa: ANN401
    """The head's draft document moved onto the variant's current generation.

    Sections come from the committed variant. ``contained`` means the committed variant
    already holds the head's edits (an approval rendered them), so the result is the
    committed state with an empty diff. Otherwise the head's lane content is laid back
    on top of it, a whole section at a time.

    An empty-state variant's committed sections are its last non-empty render, not the
    saved draft, so its head keeps its own sections either way. A head from another job
    names slots that do not exist on this job's timeline, so it is never replayed.
    """
    from app.kria.drafts import (  # noqa: PLC0415
        KriaDraftDocument,
        _variant,
        bootstrap_editor_payload,
    )

    variant = _variant(job, variant_key) or {}
    baseline = variant_baseline(variant)
    old = dict(head.snapshot_json or {})
    old_payload = old.get("editor_payload") or {}
    same_job = head.base_job_id == job.id
    keeps_head_lanes = same_job and (not contained or variant.get("editor_state") == "empty")

    if keeps_head_lanes and "sections" not in old_payload:
        # A chat draft stores its lanes flat and overlays them on the variant; attaching
        # committed sections beside them would add a second, competing copy.
        document = {**old, "editor_payload": {**old_payload, "base_generation": baseline}}
        return KriaDraftDocument.model_validate(document)

    sections = bootstrap_editor_payload(job, variant_key, baseline)["sections"]
    if keeps_head_lanes:
        sections = {**sections, **copy.deepcopy(old_payload.get("sections") or {})}
    payload = {
        **{k: v for k, v in old_payload.items() if k not in ("sections", "base_generation")},
        "base_generation": baseline,
        "sections": sections,
    }
    document = {**old, "editor_payload": payload}
    if not (same_job and not contained):
        document["changes"] = []
        for stale in ("editor_text_diff", "client_state_id"):
            document.pop(stale, None)
    return KriaDraftDocument.model_validate(document)


def restage_editor_head_sync(
    db: Session,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    contained: bool,
    reason: str,
) -> CreatorEditDraft | None:
    """Insert a head revision on the variant's current generation; None when nothing to do.

    Nothing to do: no editor head, or both its stamps already equal the baseline exactly
    (a retried Celery task lands here). Repoints a v2 / empty variant's ``editor_draft``
    at the new head. The caller commits.
    """
    from app.kria.drafts import _variant, canonical_snapshot  # noqa: PLC0415

    variant = _variant(job, variant_key)
    if variant is None:
        return None
    baseline = variant_baseline(variant)
    head = _locked_head(db, item.id, variant_key)
    if head is None or _payload_base(head) is None:
        return None
    if is_head_on_baseline(job, variant, head):
        _repoint_pinned_draft(job, variant_key, head.id)
        return None

    snapshot, snapshot_hash = canonical_snapshot(
        rebased_document(job, variant_key, head, contained=contained)
    )
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
    _repoint_pinned_draft(job, variant_key, row.id)
    log.info(
        "editor_head_restaged",
        reason=reason,
        item_id=str(item.id),
        job_id=str(job.id),
        variant_key=variant_key,
        contained=contained,
        from_generation=head.base_generation_id,
        to_generation=baseline,
    )
    return row


async def restage_editor_head(
    db: AsyncSession,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    contained: bool,
    reason: str,
) -> CreatorEditDraft | None:
    """Async twin of :func:`restage_editor_head_sync`; the same code runs through ``run_sync``."""

    def run(session: Session) -> CreatorEditDraft | None:
        return restage_editor_head_sync(
            session,
            item=item,
            job=job,
            variant_key=variant_key,
            creator_id=creator_id,
            contained=contained,
            reason=reason,
        )

    return await db.run_sync(run)
