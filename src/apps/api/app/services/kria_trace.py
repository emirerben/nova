"""Privacy-safe Kria trace projection and reconciliation inspection.

Operators start with a thread or turn identity.  This module resolves the full
control-plane chain without exposing creator messages, draft bodies, media
metadata, storage paths, or model prompts.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
)
from app.services.job_status import PLAN_ITEM_JOB_TERMINAL


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _error_code(value: object) -> str | None:
    if isinstance(value, dict) and value.get("code"):
        return str(value["code"])[:120]
    return None


def _parsed(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _duration_ms(start: object, end: object) -> int | None:
    start_at = _parsed(start)
    end_at = _parsed(end)
    if start_at is None or end_at is None or end_at < start_at:
        return None
    return round((end_at - start_at).total_seconds() * 1000)


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * percentile) + 0.999999) - 1))
    return ordered[index]


async def resolve_kria_trace(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """Resolve one redacted trace from either stable operator identity."""

    if thread_id is None and turn_id is None:
        raise ValueError("thread_id or turn_id is required")
    selected_turn = await db.get(CreatorAgentTurn, turn_id) if turn_id is not None else None
    if turn_id is not None and selected_turn is None:
        return None
    resolved_thread_id = selected_turn.thread_id if selected_turn is not None else thread_id
    assert resolved_thread_id is not None
    thread = await db.get(CreationThread, resolved_thread_id)
    if thread is None or int(thread.runtime_version) != 2:
        return None

    turn_statement = select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread.id)
    if turn_id is not None:
        turn_statement = turn_statement.where(CreatorAgentTurn.id == turn_id)
    turns = list(
        (
            await db.execute(
                turn_statement.order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
            )
        )
        .scalars()
        .all()
    )
    turn_ids = [row.id for row in turns]
    executions = (
        list(
            (
                await db.execute(
                    select(CreatorAgentExecution)
                    .where(CreatorAgentExecution.turn_id.in_(turn_ids))
                    .order_by(
                        CreatorAgentExecution.created_at,
                        CreatorAgentExecution.dependency_group,
                        CreatorAgentExecution.group_order,
                    )
                )
            )
            .scalars()
            .all()
        )
        if turn_ids
        else []
    )
    approvals = (
        list(
            (
                await db.execute(
                    select(CreatorAgentApproval)
                    .where(CreatorAgentApproval.turn_id.in_(turn_ids))
                    .order_by(CreatorAgentApproval.created_at)
                )
            )
            .scalars()
            .all()
        )
        if turn_ids
        else []
    )
    draft_ids = {row.target_draft_id for row in executions if row.target_draft_id is not None}
    draft_ids.update(row.draft_id for row in approvals if row.draft_id is not None)
    drafts = (
        list(
            (
                await db.execute(
                    select(CreatorEditDraft)
                    .where(CreatorEditDraft.id.in_(draft_ids))
                    .order_by(CreatorEditDraft.created_at)
                )
            )
            .scalars()
            .all()
        )
        if draft_ids
        else []
    )
    job_ids = {row.target_job_id for row in executions if row.target_job_id is not None}
    job_ids.update(row.target_job_id for row in approvals if row.target_job_id is not None)
    jobs = (
        list((await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars().all())
        if job_ids
        else []
    )
    events = list(
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread.id)
                .order_by(CreationThreadEvent.sequence)
            )
        )
        .scalars()
        .all()
    )

    return {
        "schema_version": 1,
        "redacted": True,
        "thread": {
            "thread_id": str(thread.id),
            "runtime_version": int(thread.runtime_version),
            "status": thread.status,
            "revision": int(thread.revision),
            "item_id": str(thread.active_plan_item_id) if thread.active_plan_item_id else None,
            "session_id": (
                str(thread.active_creator_agent_session_id)
                if thread.active_creator_agent_session_id
                else None
            ),
            "created_at": _iso(thread.created_at),
            "updated_at": _iso(thread.updated_at),
        },
        "turns": [
            {
                "turn_id": str(row.id),
                "session_id": str(row.session_id) if row.session_id else None,
                "status": row.status,
                "lease_epoch": int(row.lease_epoch),
                "lease_expires_at": _iso(row.lease_expires_at),
                "error_code": _error_code(row.error),
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "completed_at": _iso(row.completed_at),
            }
            for row in turns
        ],
        "drafts": [
            {
                "draft_id": str(row.id),
                "revision": int(row.draft_revision),
                "variant_id": row.variant_key,
                "base_job_id": str(row.base_job_id) if row.base_job_id else None,
                "base_generation_id": row.base_generation_id,
                "snapshot_hash": row.snapshot_hash,
                "is_head": bool(row.is_head),
                "body_available": row.snapshot_json is not None,
                "created_at": _iso(row.created_at),
            }
            for row in drafts
        ],
        "approvals": [
            {
                "approval_id": str(row.id),
                "turn_id": str(row.turn_id),
                "draft_id": str(row.draft_id) if row.draft_id else None,
                "status": row.status,
                "target_job_id": str(row.target_job_id) if row.target_job_id else None,
                "target_variant_id": row.target_variant_id,
                "target_generation_id": row.target_generation_id,
                "expires_at": _iso(row.expires_at),
                "consumed_at": _iso(row.consumed_at),
            }
            for row in approvals
        ],
        "executions": [
            {
                "execution_id": str(row.id),
                "turn_id": str(row.turn_id) if row.turn_id else None,
                "tool": (f"{row.tool_name}@{row.tool_version}" if row.tool_name else None),
                "risk": row.risk,
                "status": row.status,
                "job_id": str(row.target_job_id) if row.target_job_id else None,
                "variant_id": row.target_variant_id,
                "generation_id": row.target_generation_id,
                "task_id": row.external_task_id,
                "error_code": _error_code(row.error),
                "started_at": _iso(row.started_at),
                "accepted_at": _iso(row.accepted_at),
                "dispatched_at": _iso(row.dispatched_at),
                "observed_at": _iso(row.observed_at),
                "completed_at": _iso(row.completed_at),
            }
            for row in executions
        ],
        "jobs": [
            {
                "job_id": str(row.id),
                "status": row.status,
                "task_id": row.celery_task_id,
                "failure_code": str(row.failure_reason)[:120] if row.failure_reason else None,
                "created_at": _iso(row.created_at),
                "finished_at": _iso(row.finished_at),
            }
            for row in jobs
        ],
        "events": [
            {
                "event_id": str(row.id),
                "sequence": int(row.sequence),
                "revision": int(row.revision),
                "role": row.role,
                "event_type": row.event_type,
                "created_at": _iso(row.created_at),
            }
            for row in events
            if turn_id is None
            or not isinstance(row.payload, dict)
            or row.payload.get("turn_id") in {None, str(turn_id)}
        ],
    }


def reconciliation_actions(trace: dict[str, Any], *, now: datetime | None = None) -> list[dict]:
    """Return mutation-free recovery recommendations for an operator."""

    now = now or datetime.now(UTC)
    actions: list[dict] = []
    active_statuses = {str(row["status"]) for row in trace["turns"]}
    for turn in trace["turns"]:
        status = str(turn["status"])
        if status == "pending":
            actions.append({"action": "publish_turn", "turn_id": turn["turn_id"]})
        elif status == "planning" and turn.get("lease_expires_at"):
            expires_at = datetime.fromisoformat(str(turn["lease_expires_at"]))
            if expires_at <= now:
                actions.append({"action": "reclaim_expired_turn", "turn_id": turn["turn_id"]})
        elif status == "queued" and not active_statuses.intersection(
            {"pending", "planning", "executing", "awaiting_approval", "observing"}
        ):
            actions.append({"action": "promote_successor", "turn_id": turn["turn_id"]})
    for approval in trace["approvals"]:
        if approval["status"] == "approved":
            actions.append({"action": "publish_approval", "approval_id": approval["approval_id"]})
        elif approval["status"] == "pending" and approval.get("expires_at"):
            if datetime.fromisoformat(str(approval["expires_at"])) <= now:
                actions.append(
                    {"action": "expire_approval", "approval_id": approval["approval_id"]}
                )
    for execution in trace["executions"]:
        if execution["status"] == "accepted":
            actions.append(
                {"action": "publish_approval", "execution_id": execution["execution_id"]}
            )
        elif execution["status"] == "dispatched":
            actions.append({"action": "observe_job", "execution_id": execution["execution_id"]})
        elif execution["status"] == "outcome_unknown":
            actions.append(
                {"action": "reconcile_dispatch", "execution_id": execution["execution_id"]}
            )
    return actions


def trace_alerts(trace: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Evaluate day-one runtime alerts without mutating durable state."""

    now = now or datetime.now(UTC)
    alerts: list[dict[str, Any]] = []
    for turn in trace["turns"]:
        expires_at = _parsed(turn.get("lease_expires_at"))
        if turn.get("status") == "planning" and expires_at is not None and expires_at <= now:
            alerts.append(
                {
                    "code": "expired_turn_lease",
                    "severity": "warning",
                    "turn_id": turn["turn_id"],
                    "action": "reclaim_expired_turn",
                }
            )
    for approval in trace["approvals"]:
        expires_at = _parsed(approval.get("expires_at"))
        if approval.get("status") == "pending" and expires_at is not None and expires_at <= now:
            alerts.append(
                {
                    "code": "expired_pending_approval",
                    "severity": "warning",
                    "approval_id": approval["approval_id"],
                    "action": "expire_approval",
                }
            )

    jobs = {str(row["job_id"]): row for row in trace["jobs"]}
    stale_after = now - timedelta(minutes=2)
    for execution in trace["executions"]:
        status = str(execution.get("status"))
        started_at = _parsed(execution.get("started_at"))
        if status in {"accepted", "outcome_unknown"} and started_at is not None:
            if started_at <= stale_after:
                alerts.append(
                    {
                        "code": (
                            "accepted_dispatch_stalled"
                            if status == "accepted"
                            else "dispatch_outcome_unknown_stale"
                        ),
                        "severity": "critical",
                        "execution_id": execution["execution_id"],
                        "action": (
                            "publish_approval" if status == "accepted" else "reconcile_dispatch"
                        ),
                    }
                )
        job = jobs.get(str(execution.get("job_id")))
        if status == "dispatched" and job and job.get("status") in PLAN_ITEM_JOB_TERMINAL:
            dispatched_at = _parsed(execution.get("dispatched_at"))
            if dispatched_at is not None and dispatched_at <= stale_after:
                alerts.append(
                    {
                        "code": "terminal_job_observer_lag",
                        "severity": "critical",
                        "execution_id": execution["execution_id"],
                        "job_id": job["job_id"],
                        "action": "observe_job",
                    }
                )

    generation_counts = Counter(
        (str(row.get("job_id")), str(row.get("generation_id")))
        for row in trace["executions"]
        if row.get("risk") == "approval_required" and row.get("job_id") and row.get("generation_id")
    )
    for (job_id, generation_id), count in generation_counts.items():
        if count > 1:
            alerts.append(
                {
                    "code": "duplicate_approved_generation",
                    "severity": "critical",
                    "job_id": job_id,
                    "generation_id": generation_id,
                    "count": count,
                    "action": "manual_investigation",
                }
            )
    return alerts


def trace_metrics(trace: dict[str, Any]) -> dict[str, Any]:
    """Small deterministic summary suitable for runbooks and alert polling."""

    useful_response_ms = [
        duration
        for row in trace["turns"]
        if (duration := _duration_ms(row.get("created_at"), row.get("completed_at"))) is not None
    ]
    observation_ms = [
        duration
        for row in trace["executions"]
        if (duration := _duration_ms(row.get("accepted_at"), row.get("observed_at"))) is not None
    ]
    return {
        "turn_statuses": dict(Counter(str(row["status"]) for row in trace["turns"])),
        "execution_statuses": dict(Counter(str(row["status"]) for row in trace["executions"])),
        "approval_statuses": dict(Counter(str(row["status"]) for row in trace["approvals"])),
        "job_statuses": dict(Counter(str(row["status"]) for row in trace["jobs"])),
        "recovery_action_count": len(reconciliation_actions(trace)),
        "alert_count": len(trace_alerts(trace)),
        "useful_response_latency_ms": {
            "count": len(useful_response_ms),
            "p50": _percentile(useful_response_ms, 0.50),
            "p95": _percentile(useful_response_ms, 0.95),
            "max": max(useful_response_ms, default=None),
        },
        "approval_to_observation_latency_ms": {
            "count": len(observation_ms),
            "p50": _percentile(observation_ms, 0.50),
            "p95": _percentile(observation_ms, 0.95),
            "max": max(observation_ms, default=None),
        },
    }
