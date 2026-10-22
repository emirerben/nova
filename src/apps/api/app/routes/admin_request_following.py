"""Human intent review over consented immutable source versions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import CreationThread, RequestFollowingCase, RequestFollowingCollector
from app.routes.admin import _require_admin
from app.services.request_following_cases import build_candidate, digest, validate_intent
from app.services.request_following_collection import (
    LOCK_ID,
    eligible_case,
    in_scope,
    scope,
    source_capture,
)
from app.services.request_following_outcomes import (
    OutcomeReview,
    activity_at,
    current_export,
    daily_report,
    validate_outcome,
)

router = APIRouter(dependencies=[Depends(_require_admin)])
report_router = APIRouter()
DB = Annotated[AsyncSession, Depends(get_db)]


@report_router.get("")
async def report(db: DB, x_report_token: str = Header(default="")):
    import hmac

    expected = settings.request_following_report_token
    if not expected or not hmac.compare_digest(x_report_token, expected):
        raise HTTPException(401, "Unauthorized")
    return await daily_report(db)


class Expectation(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    source_pointers: list[str] = Field(min_length=1, max_length=20)


class ReviewIn(BaseModel):
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_version: int = Field(ge=0)
    decision: Literal["approved", "needs_clarification", "rejected"]
    reviewer: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=4000)
    loss_stage: Literal[
        "understanding", "handoff", "operations", "persistence", "rendering", "unknown"
    ] = "unknown"
    expectations: list[Expectation] = Field(default_factory=list, max_length=50)
    tickets: list[str] = Field(default_factory=list, max_length=20)
    outcome_review: OutcomeReview | None = None

    @field_validator("reviewer", "rationale")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value.strip()

    @field_validator("tickets")
    @classmethod
    def ticket_ids(cls, values):
        import re

        if any(not re.fullmatch(r"KRI-[0-9]+", value) for value in values):
            raise ValueError("Use KRI issue identifiers")
        return values


def summary(case):
    return {
        "request_preview": next(
            (
                event.get("content", "")[:160]
                for event in (case.capture or {}).get("sections", {}).get("events", [])
                if event.get("kind") == "user_message" and event.get("content")
            ),
            "",
        ),
        "id": str(case.id),
        "thread_id": str(case.thread_id),
        "source_sha256": case.source_sha256,
        "created_at": case.created_at.isoformat(),
        "expires_at": case.expires_at.isoformat(),
        "review_state": "superseded"
        if case.superseded
        else (case.review or {}).get("decision", "unreviewed"),
        "review_version": case.review_version,
        "activity_at": activity_at(case),
        "diagnostics": case.diagnostics,
    }


@router.get("")
async def list_cases(
    db: DB,
    cursor: uuid.UUID | None = None,
    limit: int = Query(25, ge=1, le=50),
    review_state: Literal[
        "all", "unreviewed", "approved", "needs_clarification", "rejected"
    ] = "all",
):
    # Scan a bounded page, then revalidate consent. A short page may still have
    # a next_cursor; revoked rows must not turn into a count/content side channel.
    query = select(RequestFollowingCase).where(
        RequestFollowingCase.superseded.is_(False),
        RequestFollowingCase.expires_at > datetime.now(UTC),
    )
    if review_state == "unreviewed":
        query = query.where(RequestFollowingCase.review.is_(None))
    elif review_state != "all":
        query = query.where(RequestFollowingCase.review["decision"].astext == review_state)
    if cursor:
        query = query.where(RequestFollowingCase.id > cursor)
    rows = list((await db.scalars(query.order_by(RequestFollowingCase.id).limit(limit + 1))).all())
    items, held = [], 0
    for case in rows[:limit]:
        if await eligible_case(db, case):
            if case.split == "holdout":
                held += 1
            elif case.capture is not None:
                items.append(summary(case))
    collector = await db.get(RequestFollowingCollector, 1)
    users, threads, _, all_users = scope()
    return {
        "items": items,
        "holdout_count": held,
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
        "collector": {
            "enabled": settings.request_following_collection_enabled,
            "all_users": all_users,
            "scoped_users": len(users),
            "scoped_threads": len(threads),
            "interval_minutes": 15,
            "last_run_at": collector.last_run_at.isoformat()
            if collector and collector.last_run_at
            else None,
            "last_run": collector.summary if collector else None,
        },
        "regression_runs_url": "https://github.com/emirerben/nova/actions/workflows/request-following-regressions.yml",
        "coverage_note": (
            "Runtime diagnostics are not intent verdicts. "
            "Offline replay does not verify unseen live understanding or rendered video."
        ),
    }


async def get_case(db, case_id, *, lock=False):
    if lock and not await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_ID}
    ):
        raise HTTPException(409, "collector_busy_retry")
    query = select(RequestFollowingCase).where(RequestFollowingCase.id == case_id)
    if lock:
        query = query.with_for_update()
    case = await db.scalar(query)
    if (
        case is None
        or case.split == "holdout"
        or case.capture is None
        or not await eligible_case(db, case)
    ):
        raise HTTPException(404, "case_unavailable")
    return case


async def require_current(db, case):
    if case.superseded:
        raise HTTPException(409, "source_superseded")
    thread = await db.get(CreationThread, case.thread_id)
    if thread is None or not in_scope(thread.creator_id, thread.id):
        raise HTTPException(404, "case_unavailable")
    if digest(await source_capture(db, thread)) != case.source_sha256:
        raise HTTPException(409, "source_changed_wait_for_collection")


@router.get("/comparison-development-groups")
async def comparison_development_groups(db: DB):
    """Historical exposure metadata only, including revoked/expired versions.

    Never read request bodies or held-out identities. Content fingerprints need
    a separate trusted asset inventory; thread identities cannot stand in for it.
    A bounded result is explicitly incomplete when the bound is exceeded.
    """
    rows = (
        await db.execute(
            select(RequestFollowingCase.creator_id, RequestFollowingCase.thread_id)
            .where(RequestFollowingCase.split == "development")
            .distinct()
            .order_by(RequestFollowingCase.creator_id, RequestFollowingCase.thread_id)
            .limit(10_001)
        )
    ).all()
    complete = len(rows) <= 10_000
    rows = rows[:10_000]
    owners = {str(r[0]) for r in rows}
    threads = {str(r[1]) for r in rows}
    groups = sorted({*("creator:" + v for v in owners), *("event:" + v for v in threads)})
    return {
        "schema_version": 1,
        "groups": groups,
        "groups_sha256": digest(groups),
        "creator_count": len(owners),
        "thread_count": len(threads),
        "owner_map_complete": complete,
        "media_map_complete": not rows and complete,
        "metadata_only": True,
        "generated_at": datetime.now(UTC).isoformat(),
    }


@router.get("/comparison-eligibility")
async def comparison_eligibility(
    db: DB,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    grant_key: str = Query(max_length=120),
    captured_at: datetime = Query(),
    expires_at: datetime = Query(),
):
    return await _comparison_eligibility(
        db, thread_id, creator_id, grant_key, captured_at, expires_at, holdout=False
    )


@router.get("/comparison-holdout-eligibility")
async def comparison_holdout_eligibility(
    db: DB,
    thread_id: uuid.UUID,
    creator_id: uuid.UUID,
    grant_key: str = Query(max_length=120),
    captured_at: datetime = Query(),
    expires_at: datetime = Query(),
    x_comparison_evaluator_token: str = Header(default=""),
):
    import hmac

    expected = settings.creator_comparison_holdout_token
    if not expected or not hmac.compare_digest(expected, x_comparison_evaluator_token):
        raise HTTPException(403, "sealed_evaluator_required")
    return await _comparison_eligibility(
        db, thread_id, creator_id, grant_key, captured_at, expires_at, holdout=True
    )


async def _comparison_eligibility(
    db, thread_id, creator_id, grant_key, captured_at, expires_at, *, holdout
):
    """Read-only eligibility for private exact captures; returns no case bodies.

    A queue snapshot is not the invocation input. Bind directly to the existing
    grant and owner instead of requiring a mutable queue version to stay current.
    Protected holdouts remain unavailable through this development boundary.
    """
    from datetime import timedelta

    from app.models import CreationThreadDeletion
    from app.services.request_following_collection import eligibility_key
    from app.services.training_eligibility import evaluate_training_eligibility_async

    now = datetime.now(UTC)
    if (
        captured_at.tzinfo is None
        or expires_at.tzinfo is None
        or not captured_at <= now < expires_at <= captured_at + timedelta(days=30)
    ):
        return {"eligible": False}
    if (
        not settings.creator_comparison_capture_enabled
        or str(creator_id) not in settings.creator_comparison_capture_user_ids
    ):
        return {"eligible": False}
    _, _, holdouts, _ = scope()
    thread = await db.get(CreationThread, thread_id)
    if (
        thread is None
        or thread.creator_id != creator_id
        or not in_scope(creator_id, thread_id)
        or await db.get(CreationThreadDeletion, thread_id)
    ):
        return {"eligible": False}
    # Historical holdout assignment survives configuration changes.
    held_out = thread_id in holdouts or bool(
        await db.scalar(
            select(RequestFollowingCase.id)
            .where(
                RequestFollowingCase.thread_id == thread_id, RequestFollowingCase.split == "holdout"
            )
            .limit(1)
        )
    )
    if held_out != holdout:
        return {"eligible": False}
    decision = await evaluate_training_eligibility_async(db, creator_id)
    return {"eligible": eligibility_key(decision) == grant_key, "checked_at": now.isoformat()}


@router.get("/{case_id}")
async def detail(case_id: uuid.UUID, db: DB):
    case = await get_case(db, case_id)
    return {
        **summary(case),
        "capture": case.capture,
        "review": case.review,
        "evidence": {
            "understanding": "unassessed",
            "persisted_execution": "unassessed",
            "rendered_output": "recorded_human_review_recheck_media"
            if (case.review or {}).get("outcome_review")
            else "missing",
        },
    }


@router.get("/{case_id}/media")
async def media(case_id: uuid.UUID, db: DB, variant_id: str | None = Query(None, max_length=120)):
    case = await get_case(db, case_id)
    await require_current(db, case)
    return await current_export(db, case, variant_id)


@router.post("/{case_id}/review")
async def save_review(case_id: uuid.UUID, body: ReviewIn, db: DB):
    case = await get_case(db, case_id, lock=True)
    await require_current(db, case)
    if body.source_sha256 != case.source_sha256 or body.review_version != case.review_version:
        raise HTTPException(409, "review_changed_reload")
    try:
        validate_intent(case.capture, body.model_dump())
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    await validate_outcome(db, case, body)
    case.review_version += 1
    case.review = {
        **body.model_dump(),
        "reviewed_at": datetime.now(UTC).isoformat(),
        "version": case.review_version,
    }
    await db.commit()
    return {**summary(case), "review": case.review}


@router.get("/{case_id}/export")
async def export_case(case_id: uuid.UUID, db: DB):
    case = await get_case(db, case_id, lock=True)
    await require_current(db, case)
    if not case.review or case.review["decision"] != "approved":
        raise HTTPException(409, "intent_review_required")
    candidate = build_candidate(
        case.capture,
        case_id=f"queue-{case.id}",
        session_id="session-" + digest(str(case.thread_id))[:16],
        source_sha256=case.source_sha256,
        tickets=case.review["tickets"],
    )
    candidate.update(
        expectations=case.review["expectations"],
        loss_stage=case.review["loss_stage"],
        ambiguities=[],
        intent_review=case.review,
    )
    # Promotion still needs a runnable IncidentRecord, redaction and a separate
    # review bound to the whole candidate, enforced by the existing CLI.
    return {
        "schema_version": 1,
        "capture": case.capture,
        "candidate": candidate,
        "eligibility": {
            "source_sha256": case.source_sha256,
            "thread_id": str(case.thread_id),
            "creator_id": str(case.creator_id),
            "eligibility_key": case.eligibility_key,
            "checked_at": datetime.now(UTC).isoformat(),
            "captured_at": case.created_at.isoformat(),
            "expires_at": case.expires_at.isoformat(),
        },
    }
