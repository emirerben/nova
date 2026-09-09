"""Shared proactive editor-Director helpers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._model_client import default_client
from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    RunContext,
    TerminalError,
)
from app.agents.edit_director import (
    EDIT_DIRECTOR_PROMPT_VERSION,
    EditDirectorAgent,
    EditDirectorInput,
    EditorSuggestion,
)
from app.config import settings
from app.models import DirectorReviewCache
from app.services.copilot_limits import COPILOT_SNAPSHOT_MAX_BYTES

log = structlog.get_logger()
_MAX_DIRECTOR_LOCKS = 512
_director_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
_latest_revision_by_job: dict[str, str] = {}


class DirectorSuggestionsBody(BaseModel):
    snapshot: dict = Field(default_factory=dict)
    snapshot_revision: str = Field(min_length=1, max_length=128)
    dismissed_suggestion_ids: list[str] = Field(default_factory=list, max_length=30)
    omni_enabled: bool = False

    @field_validator("snapshot")
    @classmethod
    def _validate_snapshot_size(cls, snapshot: dict) -> dict:
        if _snapshot_size(snapshot) > COPILOT_SNAPSHOT_MAX_BYTES:
            raise ValueError("snapshot exceeds 512KiB")
        return snapshot


class DirectorSuggestionsResponse(BaseModel):
    suggestions: list[EditorSuggestion] = Field(default_factory=list, max_length=5)
    snapshot_revision: str
    requested_model: str
    model_used: str
    # Compatibility for clients/cache rows written before the one-paid-call
    # policy. Runtime Director reviews never populate a fallback reason.
    fallback_reason: str | None = None
    omni_max_cost_per_second_usd: float | None = None


class DirectorFeedbackBody(BaseModel):
    suggestion_id: str = Field(min_length=1, max_length=100)
    action: Literal["accepted", "dismissed"]
    category: str = Field(default="", max_length=40)
    model_used: str = Field(default="", max_length=100)


def _snapshot_size(snapshot: dict) -> int:
    try:
        return len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="snapshot must be JSON-serializable",
        ) from exc


def _director_lock(job_id: str) -> asyncio.Lock:
    lock = _director_locks.get(job_id)
    if lock is None:
        lock = asyncio.Lock()
        _director_locks[job_id] = lock
    _director_locks.move_to_end(job_id)
    while len(_director_locks) > _MAX_DIRECTOR_LOCKS:
        stale_job_id, stale_lock = next(iter(_director_locks.items()))
        if stale_lock.locked():
            _director_locks.move_to_end(stale_job_id)
            break
        _director_locks.pop(stale_job_id, None)
        _latest_revision_by_job.pop(stale_job_id, None)
    return lock


async def run_director(
    body: DirectorSuggestionsBody,
    *,
    job_id: uuid.UUID,
    creator_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    usage_purpose: str | None = None,
    test_run_id: str | None = None,
    estimated_max_cost_usd: float | None = None,
    reservation_approved: bool = False,
    release_canary_id: str | None = None,
    authoritative_speech_cut: dict | None = None,
) -> DirectorSuggestionsResponse:
    job_key = str(job_id)
    _latest_revision_by_job[job_key] = body.snapshot_revision
    async with _director_lock(job_key):
        if _latest_revision_by_job.get(job_key) != body.snapshot_revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="edit_director_request_superseded",
            )
        return await _run_director_once(
            body,
            job_id=job_id,
            creator_id=creator_id,
            db=db,
            usage_purpose=usage_purpose,
            test_run_id=test_run_id,
            estimated_max_cost_usd=estimated_max_cost_usd,
            reservation_approved=reservation_approved,
            release_canary_id=release_canary_id,
            authoritative_speech_cut=authoritative_speech_cut,
        )


async def _run_director_once(
    body: DirectorSuggestionsBody,
    *,
    job_id: uuid.UUID,
    creator_id: uuid.UUID | None = None,
    db: AsyncSession | None = None,
    usage_purpose: str | None = None,
    test_run_id: str | None = None,
    estimated_max_cost_usd: float | None = None,
    reservation_approved: bool = False,
    release_canary_id: str | None = None,
    authoritative_speech_cut: dict | None = None,
) -> DirectorSuggestionsResponse:
    if _snapshot_size(body.snapshot) > COPILOT_SNAPSHOT_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="snapshot exceeds 512KiB",
        )
    director_snapshot = copy.deepcopy(body.snapshot)
    # Client snapshots are prompt context, never authorization. Replace every
    # cut-related field with the row-locked/server-derived variant context.
    director_snapshot.pop("automatic_cut", None)
    director_snapshot.pop("speech_cut_candidates", None)
    director_snapshot.pop("speech_cut_revision", None)
    if authoritative_speech_cut:
        director_snapshot.update(copy.deepcopy(authoritative_speech_cut))
        if authoritative_speech_cut.get("automatic_cut") is True:
            families = director_snapshot.get("allowed_op_families")
            if isinstance(families, list) and "automatic_cut" not in families:
                director_snapshot["allowed_op_families"] = [*families, "automatic_cut"]
    allowed = director_snapshot.get("allowed_op_families")
    if isinstance(allowed, list):
        director_snapshot["allowed_op_families"] = [
            family for family in allowed if str(family).strip().lower() != "render"
        ]
    # Intro layout is a server re-render in the legacy chat copilot, not an
    # undoable local editor operation. The proactive Director must never see
    # it as an available treatment.
    director_snapshot.pop("intro", None)
    agent_input = EditDirectorInput(
        variant_snapshot=director_snapshot,
        dismissed_suggestion_ids=body.dismissed_suggestion_ids,
        omni_enabled=(
            settings.omni_generated_video_enabled
            and settings.ai_usage_environment == "lab"
            and body.omni_enabled
        ),
    )
    snapshot_digest = hashlib.sha256(
        json.dumps(
            agent_input.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if db is not None and creator_id is not None:
        cached = (
            await db.execute(
                select(DirectorReviewCache).where(
                    DirectorReviewCache.creator_id == creator_id,
                    DirectorReviewCache.snapshot_digest == snapshot_digest,
                    DirectorReviewCache.prompt_version == EDIT_DIRECTOR_PROMPT_VERSION,
                    DirectorReviewCache.model == settings.edit_director_model,
                    DirectorReviewCache.expires_at > datetime.now(UTC),
                )
            )
        ).scalar_one_or_none()
        if cached is not None:
            response = DirectorSuggestionsResponse.model_validate(cached.response_json)
            return response.model_copy(
                update={
                    "snapshot_revision": body.snapshot_revision,
                    "omni_max_cost_per_second_usd": (
                        settings.ai_omni_cost_per_second_usd if agent_input.omni_enabled else None
                    ),
                }
            )

    ctx = RunContext(
        job_id=str(job_id),
        creator_id=str(creator_id) if creator_id else None,
        request_id=body.snapshot_revision,
        usage_purpose=usage_purpose,
        test_run_id=test_run_id,
        estimated_max_cost_usd=estimated_max_cost_usd,
        reservation_approved=reservation_approved,
        release_canary_id=release_canary_id,
        extra={"cached_behavior_available": False},
    )
    try:
        output = await asyncio.to_thread(
            EditDirectorAgent(default_client()).run,
            agent_input,
            ctx=ctx,
        )
        model_used = settings.edit_director_model
    except (AiBudgetExceededError, ProviderOutcomeUnknownError):
        # Budget rejection and unknown provider outcomes are never candidates
        # for a fallback: either would turn one explicit review into a second
        # paid request.
        raise
    except TerminalError as exc:
        log.warning(
            "edit_director.primary_failed",
            job_id=str(job_id),
            requested_model=settings.edit_director_model,
            error=str(exc)[:300],
        )
        if _latest_revision_by_job.get(str(job_id)) != body.snapshot_revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="edit_director_request_superseded",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="edit_director_failed",
        ) from exc

    response = DirectorSuggestionsResponse(
        suggestions=output.suggestions,
        snapshot_revision=body.snapshot_revision,
        requested_model=settings.edit_director_model,
        model_used=model_used,
        fallback_reason=None,
        omni_max_cost_per_second_usd=(
            settings.ai_omni_cost_per_second_usd if agent_input.omni_enabled else None
        ),
    )
    if db is not None and creator_id is not None:
        await db.execute(
            insert(DirectorReviewCache)
            .values(
                creator_id=creator_id,
                job_id=job_id,
                snapshot_revision=body.snapshot_revision,
                snapshot_digest=snapshot_digest,
                prompt_version=EDIT_DIRECTOR_PROMPT_VERSION,
                model=model_used,
                response_json=response.model_dump(mode="json"),
                expires_at=datetime.now(UTC)
                + timedelta(days=settings.edit_director_cache_ttl_days),
            )
            .on_conflict_do_nothing(constraint="uq_director_review_cache_identity")
        )
        await db.commit()
    return response


def record_director_feedback(
    body: DirectorFeedbackBody,
    *,
    job_id: uuid.UUID,
) -> None:
    log.info(
        "edit_director.feedback",
        job_id=str(job_id),
        suggestion_id=body.suggestion_id,
        action=body.action,
        category=body.category,
        model_used=body.model_used,
        prompt_version=EDIT_DIRECTOR_PROMPT_VERSION,
    )
