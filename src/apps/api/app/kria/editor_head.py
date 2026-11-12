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
from typing import Any, Literal

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


# A restage is the head moved onto a newer generation by something other than the creator's
# edit. Only the approval's own render and a phone publish (same document, new generation id)
# leave an approval's draft "the one the creator is looking at"; a manual Save, a speech cut, a
# repair or a heal after a legacy dispatcher does not.
OWN_RENDER_REASONS = frozenset({"approval_execution", "phone_publish"})


def restage_marker(row: Any) -> dict[str, Any] | None:  # noqa: ANN401
    snapshot = getattr(row, "snapshot_json", None)
    marker = snapshot.get("restage") if isinstance(snapshot, dict) else None
    return marker if isinstance(marker, dict) else None


def is_restage_row(row: Any) -> bool:  # noqa: ANN401
    return restage_marker(row) is not None


def draft_still_current(db: Session, draft: CreatorEditDraft) -> bool:
    """The creator has not moved past ``draft``: it is the head, or the head is its own render.

    Every link from the head back to ``draft`` must be a restage (it records ``restage.of`` and
    a reason) whose reason is the approval's own render or a phone publish. A restage caused by a
    manual Save, a speech cut, a repair or a heal after a legacy dispatcher breaks the lineage: an
    old pending approval, render retry or intent-review post must not run on top of those.
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
    for _ in range(_MAX_RESTAGE_CHAIN):
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


# --- operations -------------------------------------------------------------------------
#
# The server never merges draft content. A head's editor payload names its edits in one of two
# shapes: flat top-level lanes (how an AI chat turn records what it changed; the phone stages
# exactly those, ``stagedChatEdit`` in ``Services.swift``) and/or a full-document ``sections``
# object (bootstrap, phone-written, v2 Save). When a variant's generation moves, the writer that
# moved it names ONE of these operations; nothing hashes or compares lanes.
#
#   restamp     content byte-identical, only the generation stamps move (phone publish)
#   retire      the head becomes the committed document: committed ``sections``, no flat lane
#               (approval execution; manual Save; a head of another job)
#   drop_lanes  committed ``sections``; the named flat lanes are removed, the others kept
#               (speech-cut publish drops the timeline lanes it rewrote)
#   heal        committed ``sections``; every flat lane kept, so an AI's unsaved edit stays
#               reachable; a head with no flat lane becomes the committed document (a writer
#               nobody named bumped the generation)
#
# An empty-state variant's committed sections are its last non-empty render, not its saved
# state: its head keeps its own sections under every operation and only the stamps move.

HeadOperation = Literal["restamp", "retire", "drop_lanes", "heal"]

# Keys of an editor payload that are not an edit lane.
NON_LANE_KEYS = frozenset(
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
# The flat keys a chat turn writes for a timeline edit (the phone applies them as one lane).
TIMELINE_LANES = frozenset({"timeline_slots", "split_screen_sections"})


def flat_lane_names(payload: dict[str, Any] | None) -> list[str]:
    """The top-level lanes of an editor payload: what a chat turn changed and has not saved."""
    if not isinstance(payload, dict):
        return []
    # `remove_music: false` is a Save default the phone always encodes, not a lane.
    return [
        key
        for key, value in payload.items()
        if key not in NON_LANE_KEYS and not (key == "remove_music" and value is False)
    ]


def _variant_of(job: Any, variant_key: str) -> dict[str, Any] | None:  # noqa: ANN401
    from app.kria.drafts import _variant  # noqa: PLC0415

    return _variant(job, variant_key)


def effective_operation(
    job: Any,  # noqa: ANN401
    variant: dict[str, Any],
    head: Any,  # noqa: ANN401
    operation: HeadOperation,
) -> HeadOperation:
    """``operation`` as it applies to ``head``: a head of another job names slots that are not
    on this timeline, so nothing of it is kept."""
    if head.base_job_id != job.id and not _is_empty(variant):
        return "retire"
    return operation


def _is_empty(variant: dict[str, Any]) -> bool:
    return variant.get("editor_state") == "empty"


def moved_document(
    job: Any,  # noqa: ANN401
    variant_key: str,
    head: Any,  # noqa: ANN401
    operation: HeadOperation,
    lanes: frozenset[str] = frozenset(),
) -> Any:  # noqa: ANN401
    """``head``'s draft document on the variant's current generation, by ``operation``."""
    from app.kria.drafts import KriaDraftDocument, bootstrap_editor_payload  # noqa: PLC0415

    variant = _variant_of(job, variant_key) or {}
    baseline = variant_baseline(variant)
    operation = effective_operation(job, variant, head, operation)
    document = dict(head.snapshot_json or {})
    document.pop("restage", None)
    old = dict(document.get("editor_payload") or {})
    cleared = False
    if operation == "restamp" or _is_empty(variant):
        payload = {**old, "base_generation": baseline}
        cleared = operation == "retire"
    else:
        committed = bootstrap_editor_payload(job, variant_key, baseline)
        dropped = lanes if operation == "drop_lanes" else frozenset()
        kept = (
            [] if operation == "retire" else [k for k in flat_lane_names(old) if k not in dropped]
        )
        if kept:
            payload = {key: value for key, value in old.items() if key not in dropped}
            payload["sections"] = committed["sections"]
            payload["base_generation"] = baseline
        else:
            payload = committed
            if "editor_state_version" in old:
                payload["editor_state_version"] = old["editor_state_version"]
            cleared = True
    document["editor_payload"] = payload
    if cleared:
        document["changes"] = []
        for stale in ("editor_text_diff", "client_state_id"):
            document.pop(stale, None)
    return KriaDraftDocument.model_validate(document)


def document_on_baseline(
    job: Any,  # noqa: ANN401
    variant_key: str,
    head: Any,  # noqa: ANN401
    operation: HeadOperation = "heal",
) -> Any:  # noqa: ANN401
    """The head's document, moved only when it is off the baseline. Read-only."""
    from app.kria.drafts import KriaDraftDocument  # noqa: PLC0415

    variant = _variant_of(job, variant_key) or {}
    if is_head_on_baseline(job, variant, head):
        return KriaDraftDocument.model_validate(
            {key: value for key, value in (head.snapshot_json or {}).items() if key != "restage"}
        )
    return moved_document(job, variant_key, head, operation)


def restage_editor_head_sync(
    db: Session,
    *,
    item: PlanItem,
    job: Job,
    variant_key: str,
    creator_id: uuid.UUID,
    operation: HeadOperation,
    reason: str,
    lanes: frozenset[str] = frozenset(),
) -> CreatorEditDraft | None:
    """Insert a head revision on the variant's current generation; None when nothing to do.

    ``operation`` says what happens to the head's content (see the table above), ``reason``
    names the writer. Nothing to do: no editor head, or both its stamps already equal the
    baseline exactly (a retried Celery task lands here). The new row records
    ``restage: {of, reason}`` so a restage is never mistaken for a creator's edit. Repoints a v2
    / empty variant's ``editor_draft`` at the new head. The caller commits.
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

    document = moved_document(job, variant_key, head, operation, lanes).model_copy(
        update={"restage": {"of": str(head.id), "reason": reason}}
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
    _repoint_pinned_draft(job, variant_key, row.id)
    log.info(
        "editor_head_restaged",
        operation=effective_operation(job, variant, head, operation),
        reason=reason,
        item_id=str(item.id),
        job_id=str(job.id),
        variant_key=variant_key,
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
    operation: HeadOperation,
    reason: str,
    lanes: frozenset[str] = frozenset(),
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
            lanes=lanes,
        )

    return await db.run_sync(run)
