"""A bounded execution grant for the exact original approved device program.

No planner, provider call, approval consumption, or new edit is performed here.
The original execution remains observing; each corrective export has a fresh
device identity and its own measured report. All mutations run under canonical
owner/row locks and commit with the rejected report and spent budget.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_locks import acquire_locked_rows
from app.kria.brief_binding import media_identity, snapshot_media
from app.kria.device_render import DeviceRepairInstruction
from app.kria.render_retry import _targets, _validate
from app.kria.runtime import RuntimeFailure
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.device_render import (
    approved_runtime_generation,
    device_record,
    device_status,
    mark_device_failed,
    retry_device_render,
    save_device_record,
)


async def _authority(db: AsyncSession, job: Job, variant_id: str):
    if job.content_plan_item_id is None:
        return None
    record = device_record(job, variant_id)
    candidates = (
        (
            await db.execute(
                select(CreatorAgentExecution)
                .where(
                    CreatorAgentExecution.target_job_id == job.id,
                    CreatorAgentExecution.tool_name == "render.request",
                    CreatorAgentExecution.status.in_(["accepted", "dispatched"]),
                )
                .order_by(CreatorAgentExecution.started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for source in candidates:
        grant = (source.result or {}).get("automatic_repair_grant")
        if (
            not isinstance(grant, dict)
            or grant.get("version") != 1
            or grant.get("max_attempts") != 2
        ):
            continue
        if approved_runtime_generation(job, source.target_generation_id) != record.get(
            "base_generation"
        ):
            continue
        session = await db.get(CreatorAgentSession, source.session_id)
        item = await db.get(PlanItem, session.plan_item_id) if session else None
        if not item:
            continue
        rows = await acquire_locked_rows(
            db, _targets(source, session, item), populate_existing=True
        )
        source = rows.get(CreatorAgentExecution)
        if not source:
            continue
        grant = (source.result or {}).get("automatic_repair_grant") or {}
        try:
            _validate(
                rows,
                creator_id=job.user_id,
                source=source,
                source_approval=rows.get(CreatorAgentApproval),
                accepted=True,
                automatic=True,
            )
            approved_at = datetime.fromisoformat(grant["approved_at"])
            expires_at = datetime.fromisoformat(grant["expires_at"])
            now = datetime.now(UTC)
            if (
                grant.get("version") != 1
                or grant.get("max_attempts") != 2
                or approved_at > now
                or expires_at <= now
                or not timedelta(0) < expires_at - approved_at <= timedelta(minutes=30)
            ):
                return None
            latest = (
                await db.execute(
                    select(CreatorAgentTurn.id)
                    .where(CreatorAgentTurn.thread_id == rows[CreationThread].id)
                    .order_by(CreatorAgentTurn.created_at.desc(), CreatorAgentTurn.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest != source.turn_id or media_identity(
                grant["media_snapshot"]
            ) != media_identity(snapshot_media(rows[PlanItem])):
                return None
        except (RuntimeFailure, ValueError, KeyError, TypeError):
            return None
        return source, grant, rows[CreatorEditDraft]
    return None


async def bind_repair_grant(db: AsyncSession, job: Job, variant_id: str) -> None:
    """Freeze the compiled recipe at first completion, before any bytes are checked."""
    authority = await _authority(db, job, variant_id)
    if authority is None:
        return
    source, grant, draft = authority
    record = device_record(job, variant_id)
    status = device_status(job, variant_id)
    if "actual_export_policy" not in record or "repair_authority" in record:
        return
    record["repair_authority"] = {
        "execution_id": str(source.id),
        "approval_id": (source.result or {})["approval_id"],
        "identity": status.request.identity.model_dump(mode="json"),
        "draft_hash": draft.snapshot_hash,
        "source_receipts_digest": record["actual_export_policy"].get("source_receipts_digest"),
        "expires_at": grant["expires_at"],
    }
    save_device_record(job, variant_id, record)


async def repair_failed_export(
    db: AsyncSession, job: Job, variant_id: str, attempt_id: str, report: dict
) -> bool:
    """Authorize a specific source normalization, never an unbounded plain retry."""
    record = device_record(job, variant_id)
    binding = record.get("repair_authority")
    if not isinstance(binding, dict) or report.get("status") != "rejected":
        return False
    checks = report.get("checks", [])
    failed = [c for c in checks if c.get("status") != "passed"]
    if not failed or any(
        c.get("status") != "failed" or c.get("kind") not in {"picture", "audio"} for c in failed
    ):
        return False
    if not any(c.get("kind") == "picture" for c in failed):
        return False
    authority = await _authority(db, job, variant_id)
    if authority is None:
        return False
    source, grant, draft = authority
    status = device_status(job, variant_id)
    if type(grant.get("used")) is not int or not 0 <= grant["used"] < 2:
        return False
    if (
        binding.get("execution_id") != str(source.id)
        or binding.get("draft_hash") != draft.snapshot_hash
        or binding.get("identity", {}).get("recipe_digest") != status.request.identity.recipe_digest
        or binding.get("source_receipts_digest")
        != record.get("actual_export_policy", {}).get("source_receipts_digest")
    ):
        return False
    clips = {c.id: c.source_asset_id for t in status.request.recipe.tracks for c in t.clips}
    if any(c.get("clip_id") not in clips for c in failed):
        return False
    affected = {clips[c["clip_id"]] for c in failed}
    history = record.get("repair_history", [])
    previous = {asset for row in history for asset in row["asset_ids"]}
    if affected & previous:  # a persistent failure has no new supported corrective action
        return False
    count = grant["used"] + 1
    # Every rejected candidate and its actual measurements remain auditable.
    history = [
        *history,
        {
            "attempt": count,
            "failed_attempt_id": attempt_id,
            "failed_identity": status.request.identity.model_dump(mode="json"),
            "report": copy.deepcopy(report),
            "cause": {
                "code": "source_decode_mismatch",
                "requirement_ids": sorted({f"{c['kind']}:{c['clip_id']}" for c in failed}),
                "field_paths": [
                    f"tracks.{track_index}.clips.{clip_index}"
                    for track_index, track in enumerate(status.request.recipe.tracks)
                    for clip_index, clip in enumerate(track.clips)
                    if any(c["clip_id"] == clip.id for c in failed)
                ],
                "measurements": copy.deepcopy(failed),
            },
            "change": {"recipe_fields_changed": [], "source_assets_normalized": sorted(affected)},
            "asset_ids": sorted(affected),
            "action": "normalize_sources",
            "started_at": datetime.now(UTC).isoformat(),
            "model_calls": 0,
        },
    ]
    if status.phase == "syncing":
        mark_device_failed(
            job,
            variant_id,
            reason_code="export_failed",
            detail="Repairing the source decoding copy.",
        )
    status = retry_device_render(job, variant_id)
    status.request.recovery = DeviceRepairInstruction(
        attempt=count, asset_ids=sorted(affected | previous)
    )
    fresh = device_record(job, variant_id)
    fresh.update(
        status=status.model_dump(mode="json"), repair_authority=binding, repair_history=history
    )
    # Rejected bytes cannot become published through a delayed old callback.
    save_device_record(job, variant_id, fresh)
    source.result = {**source.result, "automatic_repair_grant": {**grant, "used": count}}
    job.status = "awaiting_device"
    job.failure_reason = None
    job.error_detail = None
    return True
