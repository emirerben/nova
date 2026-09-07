"""Thin, rollout-gated HTTP adapter for Kria runtime-v2."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.kria.api_schemas import (
    ApprovalDecisionBody,
    ApprovalDecisionOut,
    ApprovalSnapshotOut,
    DraftSnapshotOut,
    DraftUndoBody,
    DraftWriteBody,
    KriaProblemOut,
    SubmitTurnBody,
    ThreadDeltaOut,
    TurnAccepted,
    TurnCancelBody,
    TurnCancelled,
)
from app.kria.drafts import read_or_bootstrap_draft, undo_draft, write_draft
from app.kria.http import KriaRuntimeRoute, problem_response
from app.kria.runtime import (
    RuntimeFailure,
    cancel_turn,
    decide_approval,
    read_approval,
    read_delta,
    submit_turn,
)
from app.limiter import limiter

router = APIRouter(
    route_class=KriaRuntimeRoute,
    responses={
        401: {"model": KriaProblemOut},
        403: {"model": KriaProblemOut},
        404: {"model": KriaProblemOut},
        409: {"model": KriaProblemOut},
        410: {"model": KriaProblemOut},
        422: {"model": KriaProblemOut},
        428: {"model": KriaProblemOut},
        429: {"model": KriaProblemOut},
        500: {"model": KriaProblemOut},
        503: {"model": KriaProblemOut},
    },
)
log = structlog.get_logger()


def _runtime_enabled(user: CurrentUser) -> None:
    _ = user
    if not settings.kria_runtime_v2_enabled:
        raise RuntimeFailure(404, "kria_runtime_unavailable", "Creation chat unavailable")


def _problem(request: Request, failure: RuntimeFailure) -> JSONResponse:
    return problem_response(
        request,
        status_code=failure.status_code,
        code=failure.code,
        phase=failure.phase,
        message=failure.message,
        retryable=failure.retryable,
        recovery=failure.recovery,
        current_revision=failure.current_revision,
    )


def _uuid(value: str, *, code: str, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise RuntimeFailure(404, code, message) from exc


def _publish_turn(turn_id: str) -> None:
    """Publish only after the accepting transaction committed."""

    from app.tasks.kria_runtime import run_kria_turn  # noqa: PLC0415

    run_kria_turn.apply_async(args=[turn_id], task_id=turn_id, queue="agent-control")


def _publish_approval(approval_id: str) -> None:
    """Resume one committed approval through the control-plane worker."""

    from app.tasks.kria_runtime import execute_kria_approval  # noqa: PLC0415

    execute_kria_approval.apply_async(
        args=[approval_id],
        task_id=f"kria-approval:{approval_id}",
        queue="agent-control",
    )


@router.post(
    "/{thread_id}/turns",
    response_model=TurnAccepted,
    status_code=202,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
@limiter.limit("12/minute")
async def create_turn(
    request: Request,
    thread_id: str,
    body: SubmitTurnBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TurnAccepted | JSONResponse:
    try:
        _runtime_enabled(user)
        response, should_publish = await submit_turn(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            creator_id=user.id,
            body=body,
        )
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)
    if should_publish:
        try:
            _publish_turn(response.turn_id)
        except Exception as exc:  # noqa: BLE001 - pending row is the recovery ledger
            log.error(
                "kria_turn_publish_failed",
                turn_id=response.turn_id,
                error_class=type(exc).__name__,
            )
    return response


@router.get(
    "/{thread_id}/delta",
    response_model=ThreadDeltaOut,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
async def get_thread_delta(
    request: Request,
    thread_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    after_sequence: int = Query(default=-1, ge=-1),
    before_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> ThreadDeltaOut | JSONResponse:
    try:
        _runtime_enabled(user)
        return await read_delta(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            creator_id=user.id,
            after_sequence=after_sequence,
            before_sequence=before_sequence,
            limit=limit,
        )
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)


@router.post(
    "/{thread_id}/turns/{turn_id}/cancel",
    response_model=TurnCancelled,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
@limiter.limit("30/minute")
async def cancel_runtime_turn(
    request: Request,
    thread_id: str,
    turn_id: str,
    body: TurnCancelBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TurnCancelled | JSONResponse:
    try:
        _runtime_enabled(user)
        response, successor_turn_id = await cancel_turn(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            turn_id=_uuid(turn_id, code="turn_not_found", message="Kria turn not found"),
            creator_id=user.id,
            expected_thread_revision=body.expected_thread_revision,
        )
        if successor_turn_id is not None:
            try:
                _publish_turn(successor_turn_id)
            except Exception as exc:  # noqa: BLE001 - durable pending turn remains retryable
                log.error(
                    "kria_successor_publish_failed",
                    turn_id=successor_turn_id,
                    error_class=type(exc).__name__,
                )
        return response
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)


@router.post(
    "/{thread_id}/approvals/{approval_id}/{decision}",
    response_model=ApprovalDecisionOut,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
@limiter.limit("30/minute")
async def decide_runtime_approval(
    request: Request,
    thread_id: str,
    approval_id: str,
    decision: Literal["approve", "deny"],
    body: ApprovalDecisionBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalDecisionOut | JSONResponse:
    try:
        _runtime_enabled(user)
        response, successor_turn_id = await decide_approval(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            approval_id=_uuid(approval_id, code="approval_not_found", message="Approval not found"),
            creator_id=user.id,
            decision=decision,
            body=body,
        )
        if decision == "approve":
            try:
                _publish_approval(response.approval_id)
            except Exception as exc:  # noqa: BLE001 - approved row is the recovery ledger
                log.error(
                    "kria_approval_publish_failed",
                    approval_id=response.approval_id,
                    error_class=type(exc).__name__,
                )
        if successor_turn_id is not None:
            try:
                _publish_turn(successor_turn_id)
            except Exception as exc:  # noqa: BLE001 - reconciler republishes pending row
                log.error(
                    "kria_successor_publish_failed",
                    turn_id=successor_turn_id,
                    error_class=type(exc).__name__,
                )
        return response
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)


@router.get(
    "/{thread_id}/approvals/{approval_id}",
    response_model=ApprovalSnapshotOut,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
async def get_runtime_approval(
    request: Request,
    thread_id: str,
    approval_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalSnapshotOut | JSONResponse:
    try:
        _runtime_enabled(user)
        return await read_approval(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            approval_id=_uuid(approval_id, code="approval_not_found", message="Approval not found"),
            creator_id=user.id,
        )
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)


@router.get(
    "/{thread_id}/draft",
    response_model=DraftSnapshotOut,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
async def get_runtime_draft(
    request: Request,
    thread_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DraftSnapshotOut | JSONResponse:
    try:
        _runtime_enabled(user)
        return await read_or_bootstrap_draft(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            creator_id=user.id,
        )
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)


@router.put(
    "/{thread_id}/draft",
    response_model=DraftSnapshotOut,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
@limiter.limit("30/minute")
async def put_runtime_draft(
    request: Request,
    thread_id: str,
    body: DraftWriteBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> DraftSnapshotOut | JSONResponse:
    try:
        _runtime_enabled(user)
        if if_match is None:
            raise RuntimeFailure(
                428,
                "draft_precondition_required",
                "Refresh the draft before saving.",
                phase="tool",
                recovery="refresh_replan",
            )
        return await write_draft(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            creator_id=user.id,
            expected_revision=body.expected_draft_revision,
            expected_etag=if_match,
            snapshot=body.snapshot,
        )
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)


@router.post(
    "/{thread_id}/draft/undo",
    response_model=DraftSnapshotOut,
    responses={404: {"model": KriaProblemOut}, 409: {"model": KriaProblemOut}},
)
@limiter.limit("30/minute")
async def undo_runtime_draft(
    request: Request,
    thread_id: str,
    body: DraftUndoBody,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DraftSnapshotOut | JSONResponse:
    try:
        _runtime_enabled(user)
        draft, successor_turn_id = await undo_draft(
            db,
            thread_id=_uuid(
                thread_id, code="thread_not_found", message="Creation thread not found"
            ),
            creator_id=user.id,
            expected_revision=body.expected_draft_revision,
        )
        if successor_turn_id is not None:
            try:
                _publish_turn(successor_turn_id)
            except Exception as exc:  # noqa: BLE001 - pending successor is recoverable
                log.error(
                    "kria_successor_publish_failed",
                    turn_id=successor_turn_id,
                    error_class=type(exc).__name__,
                )
        return draft
    except RuntimeFailure as failure:
        await db.rollback()
        return _problem(request, failure)
