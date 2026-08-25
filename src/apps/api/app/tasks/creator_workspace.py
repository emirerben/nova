"""Crash-resumable relevance analysis for off-plan workspace media."""

from __future__ import annotations

import hashlib
import json
import uuid

import structlog
from sqlalchemy import select

from app.agents.detect_plan_relevance import DetectPlanRelevanceAgent
from app.database import sync_session
from app.models import ContentPlan, CreatorWorkspaceProposal, PlanItem
from app.worker import celery_app

log = structlog.get_logger()


def proposal_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fail(proposal_id: str, code: str) -> None:
    with sync_session() as db:
        row = db.get(CreatorWorkspaceProposal, uuid.UUID(proposal_id), with_for_update=True)
        if row is not None and row.status == "pending":
            row.status = "failed"
            row.error_code = code
            db.commit()


@celery_app.task(
    name="tasks.detect_plan_relevance",
    soft_time_limit=120,
    time_limit=150,
    acks_late=True,
    reject_on_worker_lost=True,
)
def detect_plan_relevance(proposal_id: str) -> None:
    """Analyze one proposal without mutating plans, items, or render state.

    A worker crash leaves the row ``pending`` and Celery requeues it.  A retry
    after a successful commit is a no-op because terminal rows are immutable to
    this task.
    """

    try:
        with sync_session() as db:
            row = db.get(
                CreatorWorkspaceProposal,
                uuid.UUID(proposal_id),
                with_for_update=True,
            )
            if row is None or row.status != "pending":
                return
            plan = db.get(ContentPlan, row.plan_id, with_for_update=True)
            if plan is None or plan.user_id != row.creator_id:
                row.status = "failed"
                row.error_code = "plan_not_owned"
                db.commit()
                return
            if int(plan.ownership_epoch or 0) != int(row.ownership_epoch):
                row.status = "failed"
                row.error_code = "stale_ownership_epoch"
                db.commit()
                return
            item_rows = (
                db.execute(
                    select(PlanItem)
                    .where(PlanItem.content_plan_id == plan.id)
                    .order_by(PlanItem.position, PlanItem.id)
                )
                .scalars()
                .all()
            )
            media = list(row.media_snapshot or [])
            items = [
                {"id": str(item.id), "theme": item.theme or "", "idea": item.idea or ""}
                for item in item_rows
            ]

        # Do not hold a DB lock while classification runs.  Exact upload
        # identities remain in the proposal snapshot and are re-fenced at
        # approval time.
        result = DetectPlanRelevanceAgent().run({"media": media, "plan_items": items})

        with sync_session() as db:
            row = db.get(
                CreatorWorkspaceProposal,
                uuid.UUID(proposal_id),
                with_for_update=True,
            )
            if row is None or row.status != "pending":
                return
            plan = db.get(ContentPlan, row.plan_id, with_for_update=True)
            if plan is None or plan.user_id != row.creator_id:
                row.status = "failed"
                row.error_code = "plan_not_owned"
            elif int(plan.ownership_epoch or 0) != int(row.ownership_epoch):
                row.status = "failed"
                row.error_code = "stale_ownership_epoch"
            else:
                row.status = "ready"
                row.relevance = result.relevance
                row.target_plan_item_id = (
                    uuid.UUID(result.target_plan_item_id) if result.target_plan_item_id else None
                )
                row.topic = result.topic
                row.rationale = result.rationale
                row.confidence = result.confidence
                row.proposal_hash = proposal_hash(
                    {
                        "proposal_id": str(row.id),
                        "creator_id": str(row.creator_id),
                        "plan_id": str(row.plan_id),
                        "ownership_epoch": int(row.ownership_epoch),
                        "media_ids": list(row.media_ids or []),
                        "media_snapshot": list(row.media_snapshot or []),
                        "relevance": row.relevance,
                        "target_plan_item_id": str(row.target_plan_item_id)
                        if row.target_plan_item_id
                        else None,
                        "topic": row.topic,
                    }
                )
            db.commit()
    except Exception as exc:  # noqa: BLE001 — persist stable failure, then surface for Celery
        log.exception("creator_workspace_relevance_failed", proposal_id=proposal_id)
        _fail(proposal_id, type(exc).__name__)
        raise


__all__ = ["detect_plan_relevance", "proposal_hash"]
