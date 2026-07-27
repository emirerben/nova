"""Shared proactive editor-Director helpers."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections import OrderedDict
from typing import Literal

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_director import (
    EDIT_DIRECTOR_PROMPT_VERSION,
    EditDirectorAgent,
    EditDirectorFallbackAgent,
    EditDirectorInput,
    EditorSuggestion,
)
from app.config import settings

log = structlog.get_logger()
_MAX_SNAPSHOT_BYTES = 20 * 1024
_MAX_DIRECTOR_LOCKS = 512
_director_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
_latest_revision_by_job: dict[str, str] = {}


class DirectorSuggestionsBody(BaseModel):
    snapshot: dict = Field(default_factory=dict)
    snapshot_revision: str = Field(min_length=1, max_length=128)
    dismissed_suggestion_ids: list[str] = Field(default_factory=list, max_length=30)


class DirectorSuggestionsResponse(BaseModel):
    suggestions: list[EditorSuggestion]
    snapshot_revision: str
    requested_model: str
    model_used: str
    fallback_reason: str | None = None


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
) -> DirectorSuggestionsResponse:
    job_key = str(job_id)
    _latest_revision_by_job[job_key] = body.snapshot_revision
    async with _director_lock(job_key):
        if _latest_revision_by_job.get(job_key) != body.snapshot_revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="edit_director_request_superseded",
            )
        return await _run_director_once(body, job_id=job_id)


async def _run_director_once(
    body: DirectorSuggestionsBody,
    *,
    job_id: uuid.UUID,
) -> DirectorSuggestionsResponse:
    if _snapshot_size(body.snapshot) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="snapshot exceeds 20KB",
        )
    director_snapshot = copy.deepcopy(body.snapshot)
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
        omni_enabled=settings.omni_generated_video_enabled,
    )
    ctx = RunContext(job_id=str(job_id), request_id=body.snapshot_revision)
    fallback_reason: str | None = None
    try:
        output = await asyncio.to_thread(
            EditDirectorAgent(default_client()).run,
            agent_input,
            ctx=ctx,
        )
        model_used = settings.edit_director_model
    except TerminalError as exc:
        fallback_reason = type(exc.__cause__ or exc).__name__
        log.warning(
            "edit_director.primary_failed",
            job_id=str(job_id),
            requested_model=settings.edit_director_model,
            fallback_model=settings.edit_director_fallback_model,
            fallback_reason=fallback_reason,
            error=str(exc)[:300],
        )
        try:
            output = await asyncio.to_thread(
                EditDirectorFallbackAgent(default_client()).run,
                agent_input,
                ctx=RunContext(
                    job_id=str(job_id),
                    request_id=body.snapshot_revision,
                    extra={"fallback_reason": fallback_reason},
                ),
            )
            model_used = settings.edit_director_fallback_model
        except TerminalError as fallback_exc:
            log.warning(
                "edit_director.fallback_failed",
                job_id=str(job_id),
                error=str(fallback_exc)[:300],
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="edit_director_failed",
            ) from fallback_exc

    return DirectorSuggestionsResponse(
        suggestions=output.suggestions,
        snapshot_revision=body.snapshot_revision,
        requested_model=settings.edit_director_model,
        model_used=model_used,
        fallback_reason=fallback_reason,
    )


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
