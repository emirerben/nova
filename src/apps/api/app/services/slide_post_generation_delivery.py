"""Bounded delivery for keyed SlidePost first renders; a running claim is never reclaimed."""

from __future__ import annotations

import contextvars
import copy
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps

import structlog
from sqlalchemy import JSON, and_, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import sync_session
from app.db_locks import CONTENT_PLAN_LOCK
from app.models import ContentPlan, Job, PlanItem, SlidePostGenerationReceipt
from app.services.content_plan_persona import (
    PlanPersonaOwnershipError,
    load_owned_plan_persona_sync,
)
from app.services.job_status import PLAN_ITEM_JOB_TERMINAL

log = structlog.get_logger()
active_job: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "slide_post_claim", default=None
)
PUBLISH_ATTEMPTS = 5
FINALIZE_ATTEMPTS = 5
QUEUE_DEADLINE = timedelta(minutes=35)
RUN_DEADLINE = timedelta(minutes=35)  # Beyond the worker's enforced 1800s hard limit.


def lock_graph(db: Session, job_id: uuid.UUID):
    """Acquire Plan -> Persona -> Item -> Job -> Receipt, never the inverse."""
    job_ref = db.get(Job, job_id)
    item_ref = (
        db.get(PlanItem, job_ref.content_plan_item_id)
        if job_ref and job_ref.content_plan_item_id
        else None
    )
    if item_ref is None:
        return None
    plan = db.get(
        ContentPlan,
        item_ref.content_plan_id,
        with_for_update=CONTENT_PLAN_LOCK,
        populate_existing=True,
    )
    if plan is None:
        return None
    try:
        load_owned_plan_persona_sync(db, plan, for_update=True)
    except PlanPersonaOwnershipError:
        return None
    item = db.get(PlanItem, item_ref.id, with_for_update=True, populate_existing=True)
    job = db.get(Job, job_id, with_for_update=True, populate_existing=True)
    receipt = db.execute(
        select(SlidePostGenerationReceipt)
        .where(
            SlidePostGenerationReceipt.job_id == job_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if item is None or job is None or receipt is None:
        return None
    if (
        item.current_job_id != job.id
        or item.content_plan_id != plan.id
        or job.user_id != plan.user_id
        or receipt.owner_id != plan.user_id
        or receipt.item_id != item.id
        or job.content_plan_item_id != item.id
        or receipt.ownership_epoch != int(plan.ownership_epoch or 0)
        or int(job.content_plan_ownership_epoch or 0) != receipt.ownership_epoch
    ):
        return None
    return item, job, receipt


def _receipt_for_job(db: Session, job_id: uuid.UUID):
    return db.execute(
        select(SlidePostGenerationReceipt).where(
            SlidePostGenerationReceipt.job_id == job_id,
        )
    ).scalar_one_or_none()


def claim_first_render(job_id: str) -> str:
    identifier = uuid.UUID(job_id)
    with sync_session() as db:
        if _receipt_for_job(db, identifier) is None:
            return "legacy"
        graph = lock_graph(db, identifier)
        if graph is None:
            return "skip"
        _item, job, receipt = graph
        if receipt.execution_state != "pending":
            return "skip"
        if job.status != "queued":
            # A pre-rollout worker may already own this Job. Never repeat it,
            # stamp its draft, or let an ineligible receipt starve later scans.
            receipt.execution_state = "completed" if job.status == "variants_ready" else "failed"
            db.commit()
            return "skip"
        receipt.execution_state = "running"
        receipt.claimed_at = datetime.now(UTC)
        job.status = "processing"
        db.commit()
        return "claimed"


def run_claimed(job_id: str, run: Callable[[], object]) -> bool:
    claim = claim_first_render(job_id)
    if claim == "skip":
        return False
    token = active_job.set(uuid.UUID(job_id) if claim == "claimed" else None)
    try:
        run()
        return True
    finally:
        active_job.reset(token)


def with_first_render_claim(fn):
    @wraps(fn)
    def wrapped(task, job_id: str, *args, **kwargs):
        return run_claimed(job_id, lambda: fn(task, job_id, *args, **kwargs))

    return wrapped


def write_rejected(db: Session, job: Job) -> bool:
    claimed_job = active_job.get()
    if claimed_job is None or claimed_job != job.id:
        return False
    receipt = _receipt_for_job(db, job.id)
    return receipt is None or receipt.execution_state != "running"


def finish_locked(item: PlanItem, job: Job, receipt: SlidePostGenerationReceipt) -> None:
    if job.status not in PLAN_ITEM_JOB_TERMINAL:
        return
    ready = job.status == "variants_ready"
    receipt.execution_state = "completed" if ready else "failed"
    if ready and item.current_job_id == job.id:
        current = item.slide_post
        if isinstance(current, dict) and current.get("version") == receipt.draft_version:
            item.slide_post = {**current, "rendered_version": receipt.draft_version}


def stage_result(job_id: str, result: dict) -> bool:
    identifier = uuid.UUID(job_id)
    if active_job.get() != identifier:
        return True
    with sync_session() as db:
        graph = lock_graph(db, identifier)
        if graph is None or graph[2].execution_state != "running":
            return False
        receipt = graph[2]
        if receipt.staged_result is not None and receipt.staged_result != result:
            return False
        receipt.staged_result = copy.deepcopy(result)
        db.commit()
        return True


def _fail_locked(job: Job, receipt: SlidePostGenerationReceipt, reason: str) -> None:
    receipt.execution_state = "failed"
    job.status = "processing_failed"
    job.failure_reason = reason
    job.error_detail = (
        "This render could not finish. Your saved draft is safe; choose Create to try again."
    )


def publish_pending(request_id: uuid.UUID, *, now: datetime | None = None) -> str:
    if not settings.slide_post_generation_recovery_enabled:
        return "disabled"
    now = now or datetime.now(UTC)
    with sync_session() as db:
        ref = db.get(SlidePostGenerationReceipt, request_id)
        if ref is None:
            return "skip"
        graph = lock_graph(db, ref.job_id)
        if graph is None:
            # Retire only receipt metadata; a successor's product state is never touched.
            ref = db.get(
                SlidePostGenerationReceipt, request_id, with_for_update=True, populate_existing=True
            )
            if ref and ref.execution_state == "pending":
                ref.execution_state = "failed"
                db.commit()
            return "skip"
        _item, job, receipt = graph
        if receipt.execution_state != "pending":
            return "skip"
        if job.status != "queued":
            receipt.execution_state = "completed" if job.status == "variants_ready" else "failed"
            db.commit()
            return "skip"
        if receipt.dispatch_state == "published":
            if receipt.created_at + QUEUE_DEADLINE <= now:
                _fail_locked(job, receipt, "slide_post_queue_timeout")
                db.commit()
                return "queue_timeout"
            receipt.next_publish_at = receipt.created_at + QUEUE_DEADLINE
            db.commit()
            return "waiting"
        if receipt.next_publish_at and receipt.next_publish_at > now:
            return "waiting"
        if receipt.publish_attempts >= PUBLISH_ATTEMPTS:
            _fail_locked(job, receipt, "slide_post_dispatch_unconfirmed")
            db.commit()
            return "exhausted"
        receipt.publish_attempts += 1
        receipt.next_publish_at = now + timedelta(
            seconds=min(300, 30 * 2 ** (receipt.publish_attempts - 1))
        )
        job_id, queue = str(job.id), receipt.dispatch_queue
        db.commit()
    from app.services.job_dispatch import enqueue_orchestrator_sync
    from app.services.slide_post_generation_receipts import record_publication
    from app.tasks.generative_build import orchestrate_generative_job

    try:
        enqueue_orchestrator_sync(
            orchestrate_generative_job, job_id, queue=queue, preserve_unknown=True
        )
    except Exception:
        log.exception("slide_post_delivery.publish_unconfirmed", request_id=str(request_id))
        return "unconfirmed"
    record_publication(request_id, published=True)
    return "published"


def reconcile_claimed(request_id: uuid.UUID, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    staged = None
    with sync_session() as db:
        ref = db.get(SlidePostGenerationReceipt, request_id)
        if ref is None:
            return "skip"
        graph = lock_graph(db, ref.job_id)
        if graph is None:
            ref = db.get(
                SlidePostGenerationReceipt, request_id, with_for_update=True, populate_existing=True
            )
            if ref and ref.execution_state == "running":
                ref.execution_state = "failed"
                db.commit()
            return "skip"
        item, job, receipt = graph
        if receipt.execution_state != "running":
            return "skip"
        if job.status in PLAN_ITEM_JOB_TERMINAL:
            finish_locked(item, job, receipt)
            db.commit()
            return "terminal"
        if receipt.staged_result is not None:
            if receipt.next_finalize_at and receipt.next_finalize_at > now:
                return "waiting"
            if receipt.finalize_attempts >= FINALIZE_ATTEMPTS:
                _fail_locked(job, receipt, "slide_post_publication_failed")
                db.commit()
                return "exhausted"
            receipt.finalize_attempts += 1
            receipt.next_finalize_at = now + timedelta(
                seconds=min(300, 30 * 2 ** (receipt.finalize_attempts - 1))
            )
            staged = copy.deepcopy(receipt.staged_result)
            job_id = str(job.id)
            db.commit()
        elif receipt.claimed_at and receipt.claimed_at + RUN_DEADLINE <= now:
            # Policy cancellation, not a claim that a clock proves worker death.
            # The permanent terminal receipt rejects late worker publication.
            _fail_locked(job, receipt, "slide_post_render_interrupted")
            db.commit()
            return "failed"
        else:
            return "waiting"
    from app.tasks.generative_build import _finalize_job

    token = active_job.set(uuid.UUID(job_id))
    try:
        return "completed" if _finalize_job(job_id, [staged]) else "retry"
    except Exception:
        log.exception("slide_post_delivery.finalize_retry", request_id=str(request_id))
        return "retry"
    finally:
        active_job.reset(token)


def reconcile_generations(*, now: datetime | None = None) -> dict[str, int]:
    if not settings.slide_post_generation_recovery_enabled:
        return {}
    now = now or datetime.now(UTC)
    with sync_session() as db:
        pending = list(
            db.scalars(
                select(SlidePostGenerationReceipt.client_request_id)
                .where(
                    SlidePostGenerationReceipt.execution_state == "pending",
                    or_(
                        SlidePostGenerationReceipt.next_publish_at.is_(None),
                        SlidePostGenerationReceipt.next_publish_at <= now,
                    ),
                )
                .order_by(
                    SlidePostGenerationReceipt.next_publish_at.asc().nullsfirst(),
                    SlidePostGenerationReceipt.client_request_id,
                )
                .limit(50)
            )
        )
        running = list(
            db.scalars(
                select(SlidePostGenerationReceipt.client_request_id)
                .where(
                    SlidePostGenerationReceipt.execution_state == "running",
                    or_(
                        SlidePostGenerationReceipt.next_finalize_at.is_(None),
                        SlidePostGenerationReceipt.next_finalize_at <= now,
                    ),
                    or_(
                        and_(
                            SlidePostGenerationReceipt.staged_result.is_not(None),
                            SlidePostGenerationReceipt.staged_result != JSON.NULL,
                        ),
                        SlidePostGenerationReceipt.claimed_at <= now - RUN_DEADLINE,
                    ),
                )
                .order_by(
                    SlidePostGenerationReceipt.claimed_at,
                    SlidePostGenerationReceipt.client_request_id,
                )
                .limit(50)
            )
        )
    outcomes: dict[str, int] = {}
    for candidates, observe in ((pending, publish_pending), (running, reconcile_claimed)):
        for key in candidates:
            try:
                outcome = observe(key, now=now)
            except Exception:
                log.exception("slide_post_delivery.observation_failed", request_id=str(key))
                outcome = "observation_failed"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    from app.services.slide_post_render_revisions import reconcile_revisions

    reconcile_revisions(now=now)
    return outcomes
