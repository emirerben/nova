"""Stage one reversible repair for a reviewed generation, then settle it from the repaired render.

A repair is one ordinary runtime-v2 draft turn, minted by the reviewer under a system source
event, completed through the same `_complete_draft_turn` that every creator edit uses, and left
waiting for the creator's approval. Nothing here renders. The review row records the draft and the
render execution so the follow-up review of the repaired video can settle it, and a repair that
turned out worse is undone through the draft's own undo chain, never by restoring a stored video.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.intent_review import IntentFinding
from app.config import settings
from app.database import sync_session
from app.kria.brief import load_latest_brief_sync
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument
from app.kria.intent_review_catalog import Candidate, ReviewCatalog, build_review_catalog
from app.kria.intent_review_copy import repair_summary
from app.kria.intent_review_disposition import Blocked, RepairPreconditions, dispose
from app.kria.intent_review_repair import (
    AUTHORITY_KIND,
    INTENT_REPAIR_EVENT,
    IntentRepairRefused,
    IntentRepairSpec,
    build_repair_ops,
    planned_repair_turn,
)
from app.kria.intent_review_tier import intent_review_tier, lower_tier, tier_rank
from app.kria.reply_language import reply_language_for, thread_reply_language
from app.models import (
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentTurn,
    CreatorEditDraft,
    CreatorGenerationReview,
)
from app.services.creator_render_contract import CreatorRenderContract
from app.services.intent_review_flow import _lock_row, _now, check_authority, finish_row
from app.services.phone_rollout import stamped_client_protocol

log = structlog.get_logger()

STAGING_LEASE_S = 120
UNAPPROVED_TIMEOUT = timedelta(hours=24)
REREVIEW_GRACE = timedelta(minutes=10)
TERMINAL = frozenset(
    {
        "unavailable",
        "stale",
        "exhausted",
        "review_unknown",
        "no_findings",
        "suggestion",
        "needs_confirmation",
        "repair_verified",
        "repair_unverified",
        "repair_reverted",
        "repair_abandoned",
    }
)
_BUSY_TURNS = (
    "pending",
    "queued",
    "planning",
    "executing",
    "awaiting_approval",
    "observing",
)
_DRAFTED_TURNS = frozenset({"awaiting_approval", "executing", "observing", "completed"})
_QUESTION_OR_SUGGESTION = ("suggestion", "needs_confirmation")


def client_key(review_id) -> str:
    return f"intent-review:{review_id}"


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class _Staging:
    review_id: uuid.UUID
    token: uuid.UUID
    turn_id: uuid.UUID
    thread_id: uuid.UUID
    claimed_revision: int
    planned: Any
    protocol: int | None
    language: str | None


@dataclass(frozen=True)
class _Drafted:
    review_id: uuid.UUID
    token: uuid.UUID
    turn_id: uuid.UUID


def repair_candidate_ids(review: dict[str, Any]) -> list[str]:
    return list((review.get("disposition") or {}).get("repair_candidate_ids") or [])


def wants_staging(row: CreatorGenerationReview | None) -> bool:
    return bool(
        row is not None
        and row.status == "reviewing"
        and row.repair_draft_id is None
        and repair_candidate_ids(row.review or {})
    )


def _head(db, rows) -> CreatorEditDraft | None:
    return db.scalar(
        select(CreatorEditDraft).where(
            CreatorEditDraft.item_id == rows.item.id,
            CreatorEditDraft.variant_key == str(rows.session.target_variant_id or "initial"),
            CreatorEditDraft.is_head.is_(True),
        )
    )


def _thread_busy(db, rows) -> bool:
    pending = db.scalar(
        select(func.count())
        .select_from(CreatorAgentApproval)
        .where(
            CreatorAgentApproval.thread_id == rows.thread.id,
            CreatorAgentApproval.status == "pending",
        )
    )
    active = db.scalar(
        select(func.count())
        .select_from(CreatorAgentTurn)
        .where(
            CreatorAgentTurn.thread_id == rows.thread.id,
            CreatorAgentTurn.status.in_(_BUSY_TURNS),
        )
    )
    return bool(pending or active)


def _live_candidate_ids(db, rows, row) -> frozenset[str]:
    """Candidates a rebuild from the brief as it stands now would still emit."""
    packet = row.result["packet"]
    try:
        binding = BriefBinding.model_validate_json(packet["binding_json"])
        live = BriefBinding.create(
            binding.thread_id,
            load_latest_brief_sync(db, rows.thread.id),
            protected_regions=list(binding.protected_regions),
        )
        catalog = build_review_catalog(
            packet,
            KriaDraftDocument.model_validate(json.loads(packet["approved_plan_json"])),
            CreatorRenderContract.model_validate_json(packet["contract_json"]),
            live,
            [],
            (row.result.get("evidence") or {}).get("rendered_duration_s"),
        )
    except Exception:
        log.warning("intent_review_live_catalog_unavailable", review_id=str(row.id), exc_info=True)
        return frozenset()
    return frozenset(c.candidate_id for c in catalog.candidates if c.auto)


def repair_preconditions(db, rows, row, review: dict[str, Any]) -> RepairPreconditions:
    """What the staging step finds under the authority locks; the first thing in the way wins."""
    head = _head(db, rows)
    draft = rows.draft
    if (
        head is None
        or head.id != draft.id
        or head.draft_revision != draft.draft_revision
        or head.snapshot_hash != draft.snapshot_hash
    ):
        return RepairPreconditions(blocked="head_moved")
    if _thread_busy(db, rows):
        return RepairPreconditions(blocked="thread_busy")
    return RepairPreconditions(allowed=_live_candidate_ids(db, rows, row))


def _targeted_brief_ids(
    review: dict[str, Any], catalog: ReviewCatalog, candidates: list[Candidate]
) -> list[str]:
    unmet = {
        f["requirement_id"]: f
        for f in review.get("findings") or []
        if f.get("verdict") == "unmet" and f.get("confidence") == "high"
    }
    briefs: list[str] = []
    for candidate in candidates:
        for alias in candidate.requirement_ids:
            finding = unmet.get(alias)
            requirement = catalog.requirement(alias)
            if (
                finding is not None
                and requirement is not None
                and finding.get("defect_kind") in candidate.defect_kinds
                and requirement.brief_id not in briefs
            ):
                briefs.append(requirement.brief_id)
    return briefs


def downgraded(
    review: dict[str, Any], why: Blocked | None, *, tier: str
) -> tuple[str, dict[str, Any]]:
    """The disposition the same findings give when the repair is held back, and its row status."""
    catalog = ReviewCatalog.model_validate(review["catalog"])
    findings = [IntentFinding.model_validate(f) for f in review.get("findings") or []]
    disposition = dispose(
        findings,
        catalog,
        tier=tier,
        repair_preconditions=RepairPreconditions(blocked=why),
    )
    status = disposition.outcome if disposition.outcome in _QUESTION_OR_SUGGESTION else "suggestion"
    return status, disposition.model_dump(mode="json")


def _apply_downgrade(row: CreatorGenerationReview, why: Blocked | None, *, tier: str) -> str:
    status, disposition = downgraded(dict(row.review or {}), why, tier=tier)
    row.review = {**(row.review or {}), "disposition": disposition}
    row.status, row.reason = status, "reviewed"
    row.lease_token = row.lease_until = None
    return status


def _downgrade_by_id(review_id, token, why: Blocked | None, *, tier: str = "repair") -> str:
    with sync_session() as db:
        row = _lock_row(db, review_id)
        if row is None or row.status != "reviewing" or row.lease_token != token:
            return "ignored"
        status = _apply_downgrade(row, why, tier=tier)
        db.commit()
        return status


def _stand_down(review_id, token, turn_id, why: Blocked) -> str | _Drafted:
    """Give up a repair turn that has not produced a draft; a draft that exists is kept."""
    from app.tasks import kria_runtime as runtime

    thread_id = None
    with sync_session() as db:
        turn = db.execute(
            select(CreatorAgentTurn).where(CreatorAgentTurn.id == turn_id).with_for_update()
        ).scalar_one_or_none()
        if turn is not None and turn.status in _DRAFTED_TURNS:
            return _Drafted(review_id, token, turn_id)
        if turn is not None and turn.status in {"planning", "pending", "queued"}:
            thread_id = turn.thread_id
            turn.status = "cancelled"
            turn.lease_owner = turn.lease_expires_at = None
            turn.completed_at = _now()
        row = _lock_row(db, review_id)
        if row is None or row.status != "reviewing" or row.lease_token != token:
            outcome = "ignored"
        else:
            outcome = _apply_downgrade(row, why, tier="repair")
        db.commit()
    if thread_id is not None:
        successor = runtime._promote_queued_successor_sync(thread_id)
        if successor is not None:
            runtime.run_kria_turn.apply_async(
                args=[successor], task_id=successor, queue="agent-control"
            )
    return outcome


def _abort(staging: _Staging, why: Blocked) -> str:
    stood_down = _stand_down(staging.review_id, staging.token, staging.turn_id, why)
    return _finalize(stood_down) if isinstance(stood_down, _Drafted) else stood_down


def _find_turn(db, review_id) -> CreatorAgentTurn | None:
    return db.scalar(
        select(CreatorAgentTurn).where(CreatorAgentTurn.client_event_id == client_key(review_id))
    )


def _prepare(review_id, token) -> str | _Staging | _Drafted:
    with sync_session() as db:
        first = db.get(CreatorGenerationReview, review_id)
        if not wants_staging(first) or first.lease_token != token:
            return "ignored"
        turn = _find_turn(db, review_id)
        if turn is None:
            return _open_staging(db, first, review_id, token)
        stuck = turn.id
    return _stand_down(review_id, token, stuck, "thread_busy")


def _held_back(db, row, why: Blocked | None, *, tier: str = "repair") -> str:
    status = _apply_downgrade(row, why, tier=tier)
    db.commit()
    return status


def _open_staging(db, first, review_id, token) -> str | _Staging:
    from app.tasks.kria_runtime import _append_sync_event

    rows, stale = check_authority(db, first)
    row = _lock_row(db, review_id)
    if not wants_staging(row) or row.lease_token != token:
        return "ignored"
    review = dict(row.review or {})
    live = lower_tier(row.tier, intent_review_tier(row.owner_id))
    if stale is not None:
        row.status, row.reason = "stale", stale
        row.lease_token = row.lease_until = None
        db.commit()
        return "stale"
    if row.parent_review_id is not None or tier_rank(live) < tier_rank("repair"):
        return _held_back(db, row, None, tier=lower_tier(live, "review"))
    pre = repair_preconditions(db, rows, row, review)
    ids = repair_candidate_ids(review)
    blocked = pre.blocked or (
        "protected" if pre.allowed is not None and not set(ids) <= pre.allowed else None
    )
    catalog = ReviewCatalog.model_validate(review["catalog"])
    candidates = [c for c in (catalog.candidate(identifier) for identifier in ids) if c]
    if blocked is None and len(candidates) != len(ids):
        blocked = "protected"
    ops: list[dict[str, Any]] = []
    expected: dict[str, dict[str, Any]] = {}
    if blocked is None:
        try:
            ops, expected = build_repair_ops(candidates)
        except IntentRepairRefused as exc:
            blocked = exc.reason
    if blocked is not None:
        return _held_back(db, row, blocked)
    packet = row.result["packet"]
    language = thread_reply_language(rows.thread)
    with reply_language_for(language):
        summary = repair_summary([(c.kind, c.role) for c in candidates])
    source = db.get(CreationThreadEvent, rows.turn.source_event_id)
    protocol = stamped_client_protocol(
        (source.payload or {}).get("client_protocol") if source is not None else None
    )
    spec = IntentRepairSpec(
        review_id=str(review_id),
        candidate_ids=tuple(ids),
        base_draft_id=str(rows.draft.id),
        base_draft_revision=int(rows.draft.draft_revision),
        base_snapshot_hash=rows.draft.snapshot_hash,
        variant_id=str(rows.execution.target_variant_id),
        render_generation_id=str(packet["published_attempt"]),
        expected=expected,
        authority={
            "kind": AUTHORITY_KIND,
            "approval": "creator_tap",
            "review_id": str(review_id),
            "prompt_version": review.get("prompt_version"),
        },
    )
    planned = planned_repair_turn(
        spec=spec,
        ops=ops,
        summary=summary,
        manifest_hash=rows.session.manifest_hash,
        context_hash=None,
        client_protocol=protocol,
    )
    turn_id = uuid.uuid4()
    try:
        with db.begin_nested():
            event = _append_sync_event(
                db,
                rows.thread,
                role="system",
                event_type=INTENT_REPAIR_EVENT,
                content=None,
                payload={
                    "review_id": str(review_id),
                    "candidate_ids": ids,
                    "turn_id": str(turn_id),
                    "runtime_version": 2,
                    **({"client_protocol": protocol} if protocol is not None else {}),
                },
                client_event_id=client_key(review_id),
            )
            db.add(
                CreatorAgentTurn(
                    id=turn_id,
                    thread_id=rows.thread.id,
                    session_id=rows.session.id,
                    source_event_id=event.id,
                    client_event_id=client_key(review_id),
                    request_digest=_sha(str(review_id), *sorted(ids)),
                    status="planning",
                    lease_owner=client_key(review_id),
                    lease_epoch=1,
                    lease_expires_at=db.scalar(select(func.now()))
                    + timedelta(seconds=STAGING_LEASE_S),
                )
            )
            db.flush()
    except IntegrityError:
        db.rollback()
        return _downgrade_by_id(review_id, token, "thread_busy")
    row.review = {
        **review,
        "repair": {
            "source_event_id": str(event.id),
            "turn_id": str(turn_id),
            "candidate_ids": ids,
            "targeted_brief_ids": _targeted_brief_ids(review, catalog, candidates),
        },
    }
    row.lease_until = _now() + timedelta(seconds=STAGING_LEASE_S)
    staging = _Staging(
        review_id,
        token,
        turn_id,
        rows.thread.id,
        int(rows.thread.revision),
        planned,
        protocol,
        language,
    )
    db.commit()
    return staging


def _drive(staging: _Staging) -> str:
    from app.tasks.kria_runtime import _complete_draft_turn

    try:
        with reply_language_for(staging.language):
            completion = _complete_draft_turn(
                staging.turn_id,
                lease_owner=client_key(staging.review_id),
                lease_epoch=1,
                claimed_thread_revision=staging.claimed_revision,
                planned=staging.planned,
                client_protocol=staging.protocol,
            )
    except IntentRepairRefused as exc:
        return _abort(staging, exc.reason)
    except Exception:
        log.warning(
            "intent_review_repair_turn_failed", review_id=str(staging.review_id), exc_info=True
        )
        return _abort(staging, "protected")
    if not completion.committed or completion.response_only:
        return _abort(staging, "thread_busy")
    return _finalize(_Drafted(staging.review_id, staging.token, staging.turn_id))


def _finalize(drafted: _Drafted) -> str:
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, drafted.turn_id)
        execution = (
            db.scalar(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.turn_id == drafted.turn_id,
                    CreatorAgentExecution.tool_name == "render.request",
                )
            )
            if turn is not None
            else None
        )
        row = _lock_row(db, drafted.review_id)
        if row is None or row.status != "reviewing" or row.lease_token != drafted.token:
            return "ignored"
        if execution is None or execution.target_draft_id is None:
            row_status = _apply_downgrade(row, "thread_busy", tier="repair")
            db.commit()
            return row_status
        review = dict(row.review or {})
        row.review = {
            **review,
            "repair": {
                **(review.get("repair") or {}),
                "draft_id": str(execution.target_draft_id),
                "render_execution_id": str(execution.id),
            },
        }
        row.status, row.reason = "repair_staged", "awaiting_approval"
        row.repair_draft_id = execution.target_draft_id
        row.repair_render_execution_id = execution.id
        row.repair_staged_at = _now()
        row.lease_token = row.lease_until = None
        db.commit()
        return "repair_staged"


def stage_intent_repair(review_id, *, token) -> str:
    """Idempotent: a retry resumes from the source event and turn the first attempt left."""
    prepared = _prepare(review_id, token)
    if isinstance(prepared, str):
        return prepared
    if isinstance(prepared, _Drafted):
        return _finalize(prepared)
    return _drive(prepared)


def repair_parent_id(db, execution) -> uuid.UUID | None:
    """The review that staged the draft this execution renders, found by its source event."""
    turn = db.get(CreatorAgentTurn, execution.turn_id)
    source = db.get(CreationThreadEvent, turn.source_event_id) if turn is not None else None
    if source is None or source.event_type != INTENT_REPAIR_EVENT:
        return None
    try:
        parent = uuid.UUID(str((source.payload or {}).get("review_id")))
    except (TypeError, ValueError):
        return None
    status = db.scalar(
        select(CreatorGenerationReview.status).where(CreatorGenerationReview.id == parent)
    )
    return parent if status in {"reviewing", "repair_staged"} else None


def inherited_request(db, review: CreatorGenerationReview) -> str | None:
    if review.parent_review_id is None:
        return None
    parent = db.get(CreatorGenerationReview, review.parent_review_id)
    packet = (parent.result or {}).get("packet") if parent is not None else None
    return packet.get("original_request") if isinstance(packet, dict) else None


def inherited_requirements(db, review: CreatorGenerationReview) -> tuple[str, ...] | None:
    if review.parent_review_id is None:
        return None
    parent = db.get(CreatorGenerationReview, review.parent_review_id)
    catalog = (parent.review or {}).get("catalog") if parent is not None else None
    if not isinstance(catalog, dict):
        return None
    return tuple(item["brief_id"] for item in catalog.get("requirements") or [])


def _by_brief(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
    aliases = {
        item["requirement_id"]: item["brief_id"]
        for item in (review.get("catalog") or {}).get("requirements") or []
    }
    return {
        aliases[f["requirement_id"]]: f
        for f in review.get("findings") or []
        if f.get("requirement_id") in aliases
    }


def compare_reviews(parent: dict[str, Any], child: dict[str, Any]) -> tuple[bool, bool]:
    """(fixed, worse): did the repaired render meet what was targeted, and did it lose ground."""
    before, after = _by_brief(parent), _by_brief(child)

    def high(findings: dict[str, dict[str, Any]], verdict: str) -> set[str]:
        return {
            brief
            for brief, f in findings.items()
            if f["verdict"] == verdict and f["confidence"] == "high"
        }

    parent_met = {brief for brief, f in before.items() if f["verdict"] == "met"}
    child_unmet = high(after, "unmet")
    worse = bool(child_unmet & parent_met) or len(child_unmet) > len(high(before, "unmet"))
    targeted = set((parent.get("repair") or {}).get("targeted_brief_ids") or [])
    met_after = {brief for brief, f in after.items() if f["verdict"] == "met"}
    return bool(targeted) and targeted <= met_after, worse


def _undo_head(thread_id, creator_id, expected_revision: int) -> str | None:
    from app.kria.drafts import undo_draft

    async def run():
        engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                return await undo_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=creator_id,
                    expected_revision=expected_revision,
                )
        finally:
            await engine.dispose()

    _, successor = asyncio.run(run())
    return successor


def revert_repair(parent_id) -> str:
    """Undo the repair draft if it is still the head at its own revision; otherwise leave it."""
    from app.kria.runtime import RuntimeFailure

    with sync_session() as db:
        parent = db.get(CreatorGenerationReview, parent_id)
        draft = db.get(CreatorEditDraft, parent.repair_draft_id) if parent else None
        if parent is None or draft is None:
            return "repair_reverted:worse_head_moved"
        head = db.scalar(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == draft.item_id,
                CreatorEditDraft.variant_key == draft.variant_key,
                CreatorEditDraft.is_head.is_(True),
            )
        )
        if head is None or head.id != draft.id:
            return "repair_reverted:worse_head_moved"
        thread_id, creator_id, revision = draft.thread_id, draft.creator_id, head.draft_revision
    try:
        successor = _undo_head(thread_id, creator_id, revision)
    except RuntimeFailure:
        return "repair_reverted:worse_head_moved"
    if successor is not None:
        from app.tasks.kria_runtime import run_kria_turn

        run_kria_turn.apply_async(args=[successor], task_id=successor, queue="agent-control")
    return "repair_reverted:worse"


def _close_parent(parent_id, status: str, reason: str, patch: dict[str, Any] | None = None) -> str:
    changed = finish_row(
        parent_id, expect="repair_staged", status=status, reason=reason, patch=patch
    )
    return status if changed else "ignored"


def settle_parent_from_child(child_id) -> str | None:
    """Close the parent of a finished follow-up review. No-op for anything else; idempotent."""
    with sync_session() as db:
        child = db.get(CreatorGenerationReview, child_id)
        if child is None or child.parent_review_id is None or child.status not in TERMINAL:
            return None
        parent = db.get(CreatorGenerationReview, child.parent_review_id)
        if parent is None or parent.status != "repair_staged":
            return None
        parent_id, child_review = parent.id, dict(child.review or {})
        parent_review = dict(parent.review or {})
        child_status = child.status
    reviewed = child_status in {"no_findings", "suggestion", "needs_confirmation"}
    comparison = {"child_review_id": str(child_id)}
    if not reviewed:
        return _close_parent(
            parent_id,
            "repair_unverified",
            "rereview_unavailable",
            {"comparison": {**comparison, "fixed": None, "worse": None}},
        )
    fixed, worse = compare_reviews(parent_review, child_review)
    patch = {"comparison": {**comparison, "fixed": fixed, "worse": worse}}
    if worse:
        reverted = revert_repair(parent_id).partition(":")[2]
        return _close_parent(parent_id, "repair_reverted", reverted, patch)
    if fixed:
        return _close_parent(parent_id, "repair_verified", "fixed", patch)
    return _close_parent(parent_id, "repair_unverified", "not_confirmed", patch)


def _abandon_reason(db, row: CreatorGenerationReview, now: datetime) -> str | None:
    if row.repair_render_execution_id is None:
        return "render_failed"
    execution = db.get(CreatorAgentExecution, row.repair_render_execution_id)
    if execution is None:
        return "render_failed"
    approval = db.scalar(
        select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == execution.turn_id)
    )
    if execution.status in {"failed", "stale"}:
        return "render_failed"
    if approval is not None and approval.status == "denied":
        return "approval_rejected"
    if approval is not None and approval.status in {"cancelled", "expired"}:
        undone = db.scalar(
            select(func.count())
            .select_from(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == approval.thread_id,
                CreationThreadEvent.event_type == "draft_undone",
                CreationThreadEvent.payload["cancelled_approval_ids"].contains([str(approval.id)]),
            )
        )
        return "undone" if undone else "approval_expired"
    if (
        approval is not None
        and approval.status == "pending"
        and row.repair_staged_at is not None
        and now - row.repair_staged_at > UNAPPROVED_TIMEOUT
    ):
        return "unapproved_timeout"
    return None


def _render_finished_before(db, row: CreatorGenerationReview, cutoff: datetime) -> bool:
    execution = (
        db.get(CreatorAgentExecution, row.repair_render_execution_id)
        if row.repair_render_execution_id is not None
        else None
    )
    return bool(
        execution is not None
        and execution.status == "completed"
        and execution.completed_at is not None
        and execution.completed_at < cutoff
    )


def recover_repair_staged(limit: int = 25) -> int:
    """Settle staged repairs from their follow-up review or from how the repair draft ended."""
    now = _now()
    with sync_session() as db:
        staged = list(
            db.scalars(
                select(CreatorGenerationReview.id)
                .where(CreatorGenerationReview.status == "repair_staged")
                .order_by(CreatorGenerationReview.repair_staged_at)
                .limit(limit)
            )
        )
    handled = 0
    for parent_id in staged:
        with sync_session() as db:
            row = db.get(CreatorGenerationReview, parent_id)
            if row is None or row.status != "repair_staged":
                continue
            child_id = db.scalar(
                select(CreatorGenerationReview.id).where(
                    CreatorGenerationReview.parent_review_id == parent_id,
                    CreatorGenerationReview.status.in_(TERMINAL),
                )
            )
            reason = None if child_id else _abandon_reason(db, row, now)
            unreviewed = (
                child_id is None
                and reason is None
                and _render_finished_before(db, row, now - REREVIEW_GRACE)
                and db.scalar(
                    select(func.count())
                    .select_from(CreatorGenerationReview)
                    .where(CreatorGenerationReview.parent_review_id == parent_id)
                )
                == 0
            )
        if child_id is not None:
            handled += settle_parent_from_child(child_id) is not None
        elif reason is not None:
            handled += _close_parent(parent_id, "repair_abandoned", reason) != "ignored"
        elif unreviewed:
            handled += _close_parent(parent_id, "repair_unverified", "no_rereview") != "ignored"
    return handled
