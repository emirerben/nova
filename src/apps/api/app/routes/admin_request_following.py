"""Human intent review over consented immutable source versions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
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

router = APIRouter(dependencies=[Depends(_require_admin)])
DB = Annotated[AsyncSession, Depends(get_db)]


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
            "rendered_output": "missing",
        },
    }


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
