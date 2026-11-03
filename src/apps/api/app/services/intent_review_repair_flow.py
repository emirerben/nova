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
from typing import Any, NamedTuple

import structlog
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.intent_review import IntentFinding, counts_as_met
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
from app.services.intent_review_fence import check_authority, thread_busy
from app.services.intent_review_flow import _lock_row, _now, finish_row
from app.services.intent_review_post import post_intent_review_outcome
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
UNAPPROVED_REASONS = frozenset({"approval_rejected", "approval_expired", "unapproved_timeout"})
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


DECLINED_ABANDONS = UNAPPROVED_REASONS | {"undone"}


def decline_key(candidate: dict[str, Any]) -> str:
    """A candidate's identity across generations: what it does to which element."""
    return _sha(
        candidate["kind"],
        candidate["element_ref"],
        json.dumps(candidate["target"], sort_keys=True),
    )


def declined_candidate_keys(db, session_id, review_id) -> frozenset[str]:
    """Candidates this project's creator turned down, or the follow-up review judged worse."""
    reviews = db.scalars(
        select(CreatorGenerationReview.review)
        .join(
            CreatorAgentExecution, CreatorAgentExecution.id == CreatorGenerationReview.execution_id
        )
        .where(
            CreatorAgentExecution.session_id == session_id,
            CreatorGenerationReview.id != review_id,
            or_(
                CreatorGenerationReview.status == "repair_reverted",
                and_(
                    CreatorGenerationReview.status == "repair_abandoned",
                    CreatorGenerationReview.reason.in_(DECLINED_ABANDONS),
                ),
            ),
        )
    )
    keys: set[str] = set()
    for review in reviews:
        staged = set(((review or {}).get("repair") or {}).get("candidate_ids") or [])
        for candidate in ((review or {}).get("catalog") or {}).get("candidates") or []:
            if candidate["candidate_id"] in staged:
                keys.add(decline_key(candidate))
    return frozenset(keys)


def _live_candidate_ids(db, rows, row) -> frozenset[str]:
    """Candidates a rebuild from the brief as it stands now would still emit, minus any the
    creator or the follow-up review already turned down."""
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
    declined = declined_candidate_keys(db, rows.session.id, row.id)
    return frozenset(
        c.candidate_id
        for c in catalog.candidates
        if c.auto and decline_key(c.model_dump(mode="json")) not in declined
    )


def repair_preconditions(db, rows, row) -> RepairPreconditions:
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
    if thread_busy(db, rows.thread.id):
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
    pre = repair_preconditions(db, rows, row)
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
    except SoftTimeLimitExceeded:
        raise
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


def _touched_briefs(parent: dict[str, Any]) -> set[str]:
    """Requirements mapped to an element the staged repair edits."""
    catalog = parent.get("catalog") or {}
    staged = set((parent.get("repair") or {}).get("candidate_ids") or [])
    elements = {
        c["element_ref"] for c in catalog.get("candidates") or [] if c["candidate_id"] in staged
    }
    return {
        item["brief_id"]
        for item in catalog.get("requirements") or []
        if elements & set(item.get("element_refs") or [])
    }


def compare_reviews(parent: dict[str, Any], child: dict[str, Any]) -> tuple[bool, bool]:
    """(fixed, worse): did the repaired render meet what was targeted, and did it lose ground on
    a requirement the repair could have touched. Another requirement flipping on a second,
    independent look at different frames is noise, not the repair's doing."""
    before, after = _by_brief(parent), _by_brief(child)

    def high(findings: dict[str, dict[str, Any]], verdict: str) -> set[str]:
        return {
            brief
            for brief, f in findings.items()
            if f["verdict"] == verdict and f["confidence"] == "high"
        }

    targeted = set((parent.get("repair") or {}).get("targeted_brief_ids") or [])
    touched = _touched_briefs(parent) | targeted
    parent_met = {brief for brief, f in before.items() if f["verdict"] == "met"}
    child_unmet = high(after, "unmet") & touched
    met_after = {
        brief for brief, f in after.items() if counts_as_met(f["verdict"], f["confidence"])
    }
    fixed = bool(targeted) and targeted <= met_after
    worse = bool(child_unmet & parent_met) or (
        not fixed and len(child_unmet) > len(high(before, "unmet") & touched)
    )
    return fixed, worse


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


class Reverted(NamedTuple):
    reason: str
    successor: str | None = None


class _Verdict(NamedTuple):
    status: str
    reason: str
    comparison: dict[str, Any]
    successor: str | None = None


def _already_undone(db, draft: CreatorEditDraft) -> bool:
    """Has `undo_draft` already written the revision that restores this draft's parent?"""
    if draft.parent_draft_id is None:
        return False
    on_top = [
        str(identifier)
        for identifier in db.scalars(
            select(CreatorEditDraft.id).where(CreatorEditDraft.parent_draft_id == draft.id)
        )
    ]
    return bool(on_top) and bool(
        db.scalar(
            select(func.count())
            .select_from(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == draft.thread_id,
                CreationThreadEvent.event_type == "draft_undone",
                CreationThreadEvent.payload["restored_from_draft_id"].astext
                == str(draft.parent_draft_id),
                CreationThreadEvent.payload["draft_id"].astext.in_(on_top),
            )
        )
    )


def revert_repair(parent_id) -> Reverted:
    """Undo the repair draft if it is still the head at its own revision; otherwise leave it.

    `worse` means the draft stands restored, by this call or by an earlier one that died before
    its parent was closed, so a redelivery never mistakes its own undo for the creator's newer
    work. `worse_head_moved` means the creator's draft moved on and was left alone.
    """
    from app.kria.runtime import RuntimeFailure

    with sync_session() as db:
        parent = db.get(CreatorGenerationReview, parent_id)
        draft = db.get(CreatorEditDraft, parent.repair_draft_id) if parent else None
        if parent is None or draft is None:
            return Reverted("worse_head_moved")
        if _already_undone(db, draft):
            return Reverted("worse")
        head = db.scalar(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == draft.item_id,
                CreatorEditDraft.variant_key == draft.variant_key,
                CreatorEditDraft.is_head.is_(True),
            )
        )
        if head is None or head.id != draft.id:
            return Reverted("worse_head_moved")
        draft_id = draft.id
        thread_id, creator_id, revision = draft.thread_id, draft.creator_id, head.draft_revision
    try:
        successor = _undo_head(thread_id, creator_id, revision)
    except RuntimeFailure:
        with sync_session() as db:
            raced = _already_undone(db, db.get(CreatorEditDraft, draft_id))
        return Reverted("worse" if raced else "worse_head_moved")
    return Reverted("worse", successor)


def _dispatch(turn_id: str | None) -> None:
    """Publish a turn the undo promoted; a lost publish is republished by the turn reconciler."""
    if turn_id is None:
        return
    from app.tasks.kria_runtime import run_kria_turn

    try:
        run_kria_turn.apply_async(args=[turn_id], task_id=turn_id, queue="agent-control")
    except Exception:
        log.warning("intent_review_successor_dispatch_failed", turn_id=turn_id, exc_info=True)


def _close_parent(parent_id, status: str, reason: str) -> str:
    changed = finish_row(parent_id, expect="repair_staged", status=status, reason=reason)
    return status if changed else "ignored"


def _verdict_on_repair(parent: CreatorGenerationReview, child: CreatorGenerationReview):
    """The status, reason and comparison the follow-up review gives the staged repair."""
    comparison: dict[str, Any] = {"child_review_id": str(child.id)}
    if child.status not in {"no_findings", "suggestion", "needs_confirmation"}:
        comparison.update(fixed=None, worse=None)
        return _Verdict("repair_unverified", "rereview_unavailable", comparison)
    fixed, worse = compare_reviews(dict(parent.review or {}), dict(child.review or {}))
    comparison.update(fixed=fixed, worse=worse)
    if worse:
        reverted = revert_repair(parent.id)
        return _Verdict("repair_reverted", reverted.reason, comparison, reverted.successor)
    if fixed:
        return _Verdict("repair_verified", "fixed", comparison)
    return _Verdict("repair_unverified", "not_confirmed", comparison)


def settle_parent_from_child(child_id) -> str | None:
    """Close the parent of a finished follow-up review. No-op for anything else; idempotent.

    The parent row stays locked while a worse repair is undone, so two settlers cannot both
    decide, and a settler that dies after the undo finds it again on the next pass. Anything
    the undo promoted is published only once the parent is closed.
    """
    with sync_session() as db:
        child = db.get(CreatorGenerationReview, child_id)
        if child is None or child.parent_review_id is None or child.status not in TERMINAL:
            return None
        parent_id = child.parent_review_id
        parent = _lock_row(db, parent_id)
        if parent is None or parent.status != "repair_staged":
            return None
        verdict = _verdict_on_repair(parent, child)
        parent.review = {**(parent.review or {}), "comparison": verdict.comparison}
        parent.status, parent.reason = verdict.status, verdict.reason
        parent.lease_token = parent.lease_until = None
        db.commit()
    _dispatch(verdict.successor)
    post_intent_review_outcome(parent_id)
    return verdict.status


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


def _end_unreviewable_follow_ups(parent_id) -> None:
    """A follow-up admitted below the review tier has evidence and will never be reviewed."""
    with sync_session() as db:
        stuck = list(
            db.scalars(
                select(CreatorGenerationReview.id).where(
                    CreatorGenerationReview.parent_review_id == parent_id,
                    CreatorGenerationReview.status == "evidence_ready",
                    CreatorGenerationReview.tier == "evidence",
                )
            )
        )
    for identifier in stuck:
        finish_row(
            identifier, expect="evidence_ready", status="unavailable", reason="paid_disabled"
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
        _end_unreviewable_follow_ups(parent_id)
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
            successor = revert_repair(parent_id).successor if reason in UNAPPROVED_REASONS else None
            handled += _close_parent(parent_id, "repair_abandoned", reason) != "ignored"
            _dispatch(successor)
        elif unreviewed:
            closed = _close_parent(parent_id, "repair_unverified", "no_rereview") != "ignored"
            if closed:
                post_intent_review_outcome(parent_id)
            handled += closed
    return handled
