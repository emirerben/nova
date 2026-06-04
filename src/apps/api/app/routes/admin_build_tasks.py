"""Admin endpoints for the autonomous-dev-loop build_task queue (M4).

POST   /admin/build-tasks          — mint a task (trusted provenance only)
POST   /admin/build-tasks/claim    — atomically claim the oldest queued task
PATCH  /admin/build-tasks/{id}     — checkpoint / status transition from a run
GET    /admin/build-tasks          — list tasks (optional status filter)

The GitHub Actions builder reads/claims/updates via `scripts/admin.py`
(X-Admin-Token). All status transitions go through
`app.services.build_task_repo` — these handlers are thin wrappers that own the
HTTP shape + DB session, never raw SQL.

Auth: X-Admin-Token header (same `_require_admin` gate as the rest of admin.*).

SECURITY (CEO D3): the mint endpoint refuses any non-trusted provenance — an
untrusted signal can never auto-mint a builder-claimable task in v1. The repo
layer enforces it (`UntrustedProvenanceError`); this route maps it to 403.

NOTE: these handlers use the SYNC session (`sync_session`) rather than the
async `get_db`, because `build_task_repo` is a sync module (the reaper +
builder share it, and the SKIP-LOCKED claim is a sync Session API). The
handlers run the small, indexed queries in a threadpool via FastAPI's sync
def → no event-loop blocking concern at this query volume (one row per tick).
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.models import BuildTask
from app.routes.admin import _require_admin
from app.services import build_task_repo

log = structlog.get_logger()

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────


class BuildTaskPayload(BaseModel):
    id: str
    status: str
    stage: str | None
    progress_note: str | None
    branch: str | None
    attempt_count: int
    provenance: str
    priority: int
    claimed_at: str | None
    claimed_by: str | None
    title: str
    body: str | None
    created_at: str | None
    updated_at: str | None
    # Ship-gate (Phase 2) fields.
    head_sha: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    gate_report: dict | None = None


def _to_payload(task: BuildTask) -> BuildTaskPayload:
    return BuildTaskPayload(
        id=str(task.id),
        status=task.status,
        stage=task.stage,
        progress_note=task.progress_note,
        branch=task.branch,
        attempt_count=task.attempt_count,
        provenance=task.provenance,
        priority=task.priority,
        claimed_at=task.claimed_at.isoformat() if task.claimed_at else None,
        claimed_by=task.claimed_by,
        title=task.title,
        body=task.body,
        created_at=task.created_at.isoformat() if task.created_at else None,
        updated_at=task.updated_at.isoformat() if task.updated_at else None,
        head_sha=task.head_sha,
        pr_url=task.pr_url,
        pr_number=task.pr_number,
        gate_report=task.gate_report,
    )


class CreateBuildTaskRequest(BaseModel):
    title: str = Field(min_length=1)
    body: str | None = None
    # Defaults to trusted; an untrusted value is REJECTED (security invariant).
    provenance: Literal["trusted", "untrusted"] = "trusted"
    priority: int = 100


class ClaimRequest(BaseModel):
    # Opaque run identity (e.g. the GH Actions run id) for observability.
    claimed_by: str | None = None


class PatchBuildTaskRequest(BaseModel):
    """One PATCH covers every per-run transition the builder needs.

    `action` selects the transition (so the builder doesn't have to know the
    repo function names); the optional fields carry the checkpoint payload.
      - checkpoint : persist stage/progress_note/branch, status unchanged.
      - complete   : mark done (idempotent).
      - release    : soft-exit on a usage limit → back to queued, NO attempt bump.
      - fail       : genuine failure → bump attempt, requeue or block at the cap.
      - block / reset : manual escalation / un-block.
      - start_gating: built → gating (Phase 2); requires head_sha (the pushed
                      commit the gate tick must match).
      - open_pr    : gates green → awaiting_approval; requires pr_url, carries
                     pr_number + gate_report for the PR body / digest.
      - gate_failed: a BLOCKING gate failed → record gate_report, bump + requeue/
                     block (distinct from release, which is a gate-tick abort).
    """

    action: Literal[
        "checkpoint",
        "complete",
        "release",
        "fail",
        "block",
        "reset",
        "start_gating",
        "open_pr",
        "gate_failed",
    ] = "checkpoint"
    stage: str | None = None
    progress_note: str | None = None
    branch: str | None = None
    # Ship-gate (Phase 2) payload.
    head_sha: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    gate_report: dict | None = None


class ListBuildTasksResponse(BaseModel):
    items: list[BuildTaskPayload]


# ── Endpoints ────────────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=BuildTaskPayload,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
def create_build_task(req: CreateBuildTaskRequest) -> BuildTaskPayload:
    """Mint a new queued task. Untrusted provenance → 403 (security invariant)."""
    from app.database import sync_session

    with sync_session() as db:
        try:
            task = build_task_repo.create_build_task(
                db,
                title=req.title,
                body=req.body,
                provenance=req.provenance,
                priority=req.priority,
            )
        except build_task_repo.UntrustedProvenanceError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        db.commit()
        return _to_payload(task)


@router.post(
    "/claim",
    response_model=BuildTaskPayload | None,
    dependencies=[Depends(_require_admin)],
)
def claim_build_task(req: ClaimRequest | None = None) -> BuildTaskPayload | None:
    """Atomically claim the oldest queued task, or 204-shaped null if none.

    Returns the claimed task (now `in_progress`) or `null` when the queue is
    empty. SKIP LOCKED guarantees no double-claim across overlapping runs.
    """
    from app.database import sync_session

    claimed_by = req.claimed_by if req else None
    with sync_session() as db:
        task = build_task_repo.claim_next_task(db, claimed_by=claimed_by)
        if task is None:
            db.commit()
            return None
        payload = _to_payload(task)
        db.commit()
        return payload


@router.post(
    "/claim-gating",
    response_model=BuildTaskPayload | None,
    dependencies=[Depends(_require_admin)],
)
def claim_gating_build_task(req: ClaimRequest | None = None) -> BuildTaskPayload | None:
    """Atomically claim the oldest unclaimed `gating` task for a gate tick, or null.

    The gate tick's claim: picks up a built task the builder left in `gating`
    (claimed_at NULL) to run the hard gates. SKIP LOCKED guarantees no two gate
    ticks gate the same row.
    """
    from app.database import sync_session

    claimed_by = req.claimed_by if req else None
    with sync_session() as db:
        task = build_task_repo.claim_next_gating_task(db, claimed_by=claimed_by)
        if task is None:
            db.commit()
            return None
        payload = _to_payload(task)
        db.commit()
        return payload


@router.patch(
    "/{task_id}",
    response_model=BuildTaskPayload,
    dependencies=[Depends(_require_admin)],
)
def patch_build_task(task_id: str, req: PatchBuildTaskRequest) -> BuildTaskPayload:
    """Apply a per-run transition (checkpoint / complete / release / fail / ...)."""
    from app.database import sync_session

    with sync_session() as db:
        existing = build_task_repo.get_task(db, task_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Build task not found"
            )

        if req.action == "checkpoint":
            task = build_task_repo.checkpoint_task(
                db,
                task_id,
                stage=req.stage,
                progress_note=req.progress_note,
                branch=req.branch,
            )
        elif req.action == "complete":
            # Persist a final checkpoint first so progress_note/branch reflect
            # the completing run, then mark done.
            build_task_repo.checkpoint_task(
                db,
                task_id,
                stage=req.stage,
                progress_note=req.progress_note,
                branch=req.branch,
            )
            task = build_task_repo.complete_task(db, task_id)
        elif req.action == "release":
            build_task_repo.checkpoint_task(
                db,
                task_id,
                stage=req.stage,
                branch=req.branch,
            )
            task = build_task_repo.release_task(db, task_id, progress_note=req.progress_note)
        elif req.action == "fail":
            build_task_repo.checkpoint_task(
                db,
                task_id,
                stage=req.stage,
                progress_note=req.progress_note,
                branch=req.branch,
            )
            task = build_task_repo.fail_task(db, task_id)
        elif req.action == "block":
            task = build_task_repo.block_task(db, task_id)
        elif req.action == "reset":
            task = build_task_repo.reset_task(db, task_id)
        elif req.action == "start_gating":
            if not req.head_sha:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="start_gating requires head_sha",
                )
            build_task_repo.checkpoint_task(
                db, task_id, stage=req.stage, progress_note=req.progress_note
            )
            task = build_task_repo.start_gating(
                db, task_id, head_sha=req.head_sha, branch=req.branch
            )
        elif req.action == "open_pr":
            if not req.pr_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="open_pr requires pr_url",
                )
            task = build_task_repo.open_pr(
                db,
                task_id,
                pr_url=req.pr_url,
                pr_number=req.pr_number,
                gate_report=req.gate_report,
                branch=req.branch,
            )
        elif req.action == "gate_failed":
            build_task_repo.checkpoint_task(db, task_id, stage=req.stage, branch=req.branch)
            task = build_task_repo.gate_failed(
                db,
                task_id,
                gate_report=req.gate_report,
                progress_note=req.progress_note,
            )
        else:  # pragma: no cover - Literal guards the input
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown action")

        db.commit()
        # task is non-None here (existence checked above), but be defensive.
        if task is None:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Build task not found"
            )
        return _to_payload(task)


@router.get(
    "",
    response_model=ListBuildTasksResponse,
    dependencies=[Depends(_require_admin)],
)
def list_build_tasks(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
) -> ListBuildTasksResponse:
    """List tasks, optionally filtered by status (queued/in_progress/blocked/done)."""
    from app.database import sync_session

    with sync_session() as db:
        tasks = build_task_repo.list_tasks(db, status=status_filter, limit=limit)
        return ListBuildTasksResponse(items=[_to_payload(t) for t in tasks])
