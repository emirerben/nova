"""Stage 2 creator-review coordinator.

This module owns exact-generation fencing and review persistence.  The actual
media grader is deliberately supplied by dependency injection; this keeps
offline workers/tests from downloading or sending creator media anywhere.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

import structlog

from app.worker import celery_app

log = structlog.get_logger()
QUALITY_REVIEW_AGENT_NAME = "nova.creator_quality_reviewer"
QUALITY_REVIEW_PROMPT_VERSION = "2026-08-25"
QUALITY_REVIEW_MODEL = "gemini-2.5-flash"
READY_JOB_STATUSES = frozenset({"variants_ready", "variants_ready_partial"})


def review_key(session_id: str, job_id: str, variant_id: str, generation_id: str) -> str:
    return ":".join((session_id, job_id, variant_id, generation_id))


def _task_id(key: str) -> str:
    return "creator-review-" + hashlib.sha256(key.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def queue_creator_quality_review(
    session: Any, *, job_id: str, variant_id: str, render_generation_id: str
) -> bool:
    key = review_key(str(session.id), job_id, variant_id, render_generation_id)
    current = session.last_review if isinstance(getattr(session, "last_review", None), dict) else {}
    if current.get("review_key") == key:
        return False
    pending = {
        "status": "pending",
        "review_key": key,
        "creator_id": str(session.creator_id),
        "creator_session_id": str(session.id),
        "plan_item_id": str(session.plan_item_id),
        "ownership_epoch": int(session.ownership_epoch or 0),
        "session_revision": int(session.revision or 0),
        "job_id": job_id,
        "variant_id": variant_id,
        "render_generation_id": render_generation_id,
        "review_mode": "objective",
        "reviewer": "video_quality_grader",
        "queued_at": _now(),
    }
    session.last_review = pending
    try:
        quality_review_creator_session.apply_async(
            kwargs={
                "session_id": str(session.id),
                "job_id": job_id,
                "variant_id": variant_id,
                "render_generation_id": render_generation_id,
            },
            task_id=_task_id(key),
        )
    except Exception as exc:  # noqa: BLE001 — fail-open to manual feedback
        session.last_review = {
            **pending,
            "status": "unavailable",
            "decision": "unavailable",
            "error_code": "review_enqueue_failed",
            "error_message": str(exc)[:240],
            "failed_at": _now(),
        }
        log.warning("creator_quality_review_enqueue_failed", error_type=type(exc).__name__)
        return False
    return True


def build_review_payload(
    session: Any, *, job_id: str, variant_id: str, generation_id: str, verdict: Any
) -> dict[str, Any]:
    """Convert a mocked/DI grader verdict to the bounded persisted receipt."""

    from app.agents._schemas.creator_agent import (  # noqa: PLC0415
        CreatorReviewReceipt,
        canonical_context_hash,
    )

    key = review_key(str(session.id), job_id, variant_id, generation_id)
    evidence = []
    for index, (dimension, score) in enumerate((verdict.scores or {}).items()):
        evidence.append(
            {
                "evidence_id": f"{key}-evidence-{index}",
                "kind": "caption" if "text" in dimension or "caption" in dimension else "visual",
                "severity": "warning" if float(score) < 4 else "info",
                "start_s": float(index),
                "end_s": float(index + 1),
                "observation": (
                    f"{dimension.replace('_', ' ')} scored {float(score):.1f}/5. "
                    f"{str(verdict.reasoning or 'Review this moment manually.')[:380]}"
                )[:500],
            }
        )
    evidence = evidence[:12]
    decision = "approve" if verdict.band.value == "auto_pass" else "revise"
    proposal = None
    if decision == "revise":
        proposal = {
            "revision_id": f"{key}-revision",
            "summary": "Strengthen the weakest scored moments before another render.",
            "rationale": str(
                verdict.reasoning or "The objective review found room for improvement."
            )[:1000],
            "evidence_ids": [row["evidence_id"] for row in evidence[:8]],
        }
    active = session.active_plan if isinstance(session.active_plan, dict) else {}
    manifest = getattr(session, "manifest_hash", None)
    context = active.get("plan_hash")
    receipt = CreatorReviewReceipt(
        creator_id=str(session.creator_id),
        creator_session_id=str(session.id),
        plan_item_id=str(session.plan_item_id),
        ownership_epoch=int(session.ownership_epoch or 0),
        session_revision=int(session.revision or 0),
        job_id=job_id,
        variant_id=variant_id,
        render_generation_id=generation_id,
        manifest_hash=(
            manifest
            if isinstance(manifest, str) and len(manifest) == 64
            else canonical_context_hash({"session": str(session.id)})
        ),
        context_hash=(
            context
            if isinstance(context, str) and len(context) == 64
            else canonical_context_hash({"job": job_id, "generation": generation_id})
        ),
        review_mode="objective",
        decision=decision,
        quality_score=float(verdict.avg),
        confidence=float(verdict.confidence),
        evidence=evidence,
        proposed_revision=proposal,
        reviewed_at=_now(),
    )
    return {"status": "complete", **receipt.model_dump(mode="json")}


def claim_exact_review(
    db: Any, *, session_id: str, job_id: str, variant_id: str, generation_id: str
) -> tuple[Any, str] | None:
    """Claim a pending review only if the Job/variant/generation still match."""

    from app.models import CreatorAgentSession, Job  # noqa: PLC0415

    row = db.get(CreatorAgentSession, uuid.UUID(session_id), with_for_update=True)
    current = row.last_review if row and isinstance(row.last_review, dict) else {}
    key = review_key(session_id, job_id, variant_id, generation_id)
    if row is None or current.get("review_key") != key or current.get("status") != "pending":
        return None
    job = db.get(Job, uuid.UUID(job_id))
    variants = (job.assembly_plan or {}).get("variants") if job else []
    variant = next(
        (
            value
            for value in variants
            if isinstance(value, dict) and value.get("variant_id") == variant_id
        ),
        None,
    )
    if not (
        job
        and job.status in READY_JOB_STATUSES
        and job.user_id == row.creator_id
        and job.content_plan_item_id == row.plan_item_id
        and row.target_job_id == job.id
        and row.target_variant_id == variant_id
        and row.target_generation_id == generation_id
        and variant
        and variant.get("render_status") == "ready"
        and variant.get("render_generation_id") == generation_id
    ):
        mark_review_unavailable(
            db,
            session_id=session_id,
            job_id=job_id,
            variant_id=variant_id,
            generation_id=generation_id,
            code="review_target_stale",
            message="The rendered generation changed before review completed.",
        )
        return None
    row.last_review = {**current, "status": "running", "started_at": _now()}
    db.commit()
    return row, str(variant.get("video_path") or "")


def persist_review_if_current(
    db: Any,
    *,
    session_id: str,
    job_id: str,
    variant_id: str,
    generation_id: str,
    payload: dict[str, Any],
) -> bool:
    """Persist only when the session still owns the exact review target."""

    from app.models import CreatorAgentSession  # noqa: PLC0415

    row = db.get(CreatorAgentSession, uuid.UUID(session_id), with_for_update=True)
    current = row.last_review if row and isinstance(row.last_review, dict) else {}
    if row is None or current.get("review_key") != review_key(
        session_id, job_id, variant_id, generation_id
    ):
        return False
    if (
        row.target_job_id != uuid.UUID(job_id)
        or row.target_variant_id != variant_id
        or row.target_generation_id != generation_id
    ):
        return False
    row.last_review = payload
    db.commit()
    return True


def run_quality_review(
    *,
    session_id: str,
    job_id: str,
    variant_id: str,
    render_generation_id: str,
    db_factory: Callable[[], Any],
    reviewer: Callable[[str], Any] | None,
    persist_run: Callable[..., None],
) -> None:
    """Run one injected review and persist both receipt and AgentRun.

    ``reviewer`` is intentionally required from the caller.  This offline-safe
    coordinator never chooses a network/media implementation itself; a future
    rollout can inject the existing ``VideoQualityGrader`` adapter explicitly.
    """

    db = db_factory()
    try:
        target = claim_exact_review(
            db,
            session_id=session_id,
            job_id=job_id,
            variant_id=variant_id,
            generation_id=render_generation_id,
        )
    finally:
        db.close()
    if target is None:
        return
    session, video_path = target
    if reviewer is None:
        db = db_factory()
        try:
            mark_review_unavailable(
                db,
                session_id=session_id,
                job_id=job_id,
                variant_id=variant_id,
                generation_id=render_generation_id,
                code="reviewer_unavailable",
                message="Quality review is unavailable; watch the render and give manual feedback.",
            )
        finally:
            db.close()
        persist_run(
            job_id=job_id,
            creator_agent_session_id=session_id,
            segment_idx=None,
            agent_name=QUALITY_REVIEW_AGENT_NAME,
            prompt_version=QUALITY_REVIEW_PROMPT_VERSION,
            model="offline",
            outcome="failed",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0,
            input_dict={"session_id": session_id, "job_id": job_id, "variant_id": variant_id},
            output_dict=None,
            raw_text=None,
            error="reviewer_unavailable",
        )
        return
    try:
        verdict = reviewer(video_path)
        payload = build_review_payload(
            session,
            job_id=job_id,
            variant_id=variant_id,
            generation_id=render_generation_id,
            verdict=verdict,
        )
    except Exception as exc:  # noqa: BLE001 — critic failure is visible/fail-open
        db = db_factory()
        try:
            mark_review_unavailable(
                db,
                session_id=session_id,
                job_id=job_id,
                variant_id=variant_id,
                generation_id=render_generation_id,
                code="review_failed",
                message=str(exc),
            )
        finally:
            db.close()
        persist_run(
            job_id=job_id,
            creator_agent_session_id=session_id,
            segment_idx=None,
            agent_name=QUALITY_REVIEW_AGENT_NAME,
            prompt_version=QUALITY_REVIEW_PROMPT_VERSION,
            model=QUALITY_REVIEW_MODEL,
            outcome="failed",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0,
            input_dict={"session_id": session_id, "job_id": job_id, "variant_id": variant_id},
            output_dict=None,
            raw_text=None,
            error=str(exc)[:500],
        )
        return
    db = db_factory()
    try:
        if not persist_review_if_current(
            db,
            session_id=session_id,
            job_id=job_id,
            variant_id=variant_id,
            generation_id=render_generation_id,
            payload=payload,
        ):
            return
    finally:
        db.close()
    persist_run(
        job_id=job_id,
        creator_agent_session_id=session_id,
        segment_idx=None,
        agent_name=QUALITY_REVIEW_AGENT_NAME,
        prompt_version=QUALITY_REVIEW_PROMPT_VERSION,
        model=QUALITY_REVIEW_MODEL,
        outcome="ok",
        attempts=1,
        tokens_in=int(getattr(verdict, "tokens_in", 0) or 0),
        tokens_out=int(getattr(verdict, "tokens_out", 0) or 0),
        cost_usd=0.0,
        latency_ms=0,
        input_dict={"session_id": session_id, "job_id": job_id, "variant_id": variant_id},
        output_dict=payload,
        raw_text=getattr(verdict, "raw_response", None),
        error=None,
    )


def mark_review_unavailable(
    db: Any,
    *,
    session_id: str,
    job_id: str,
    variant_id: str,
    generation_id: str,
    code: str,
    message: str,
) -> bool:
    from app.models import CreatorAgentSession  # noqa: PLC0415

    row = db.get(CreatorAgentSession, uuid.UUID(session_id), with_for_update=True)
    current = row.last_review if row and isinstance(row.last_review, dict) else {}
    if row is None or current.get("review_key") != review_key(
        session_id, job_id, variant_id, generation_id
    ):
        return False
    row.last_review = {
        **current,
        "status": "unavailable",
        "decision": "unavailable",
        "error_code": code,
        "error_message": message[:240],
        "failed_at": _now(),
    }
    db.commit()
    return True


@celery_app.task(
    name="tasks.creator_quality_review",
    bind=True,
    max_retries=0,
    soft_time_limit=240,
    time_limit=300,
)
def quality_review_creator_session(
    self,
    *,
    session_id: str,
    job_id: str,
    variant_id: str,
    render_generation_id: str,
) -> None:
    """Offline-safe worker entry point; a reviewer adapter is rollout-owned."""

    from app.agents._persistence import persist_agent_run  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415

    run_quality_review(
        session_id=session_id,
        job_id=job_id,
        variant_id=variant_id,
        render_generation_id=render_generation_id,
        db_factory=sync_session,
        reviewer=None,
        persist_run=persist_agent_run,
    )


__all__ = [
    "build_review_payload",
    "claim_exact_review",
    "mark_review_unavailable",
    "persist_review_if_current",
    "quality_review_creator_session",
    "queue_creator_quality_review",
    "review_key",
    "run_quality_review",
]
