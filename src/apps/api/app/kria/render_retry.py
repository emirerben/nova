"""Exact recovery of a failed render; never enters a creative planner.

Only persisted execution programs can be recovered. A strategy whose planner
failed before producing a recipe is not a frozen edit and is refused explicitly.
The failed receipt stays immutable; one new approval/receipt belongs to it.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import sync_session
from app.db_locks import acquire_locked_rows, acquire_locked_rows_sync
from app.kria.api_schemas import RenderRetryBody, RenderRetryOut
from app.kria.brief_binding import media_identity, snapshot_media
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.runtime import RuntimeFailure, _append_event
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.creation_thread_titles import conversation_revision_matches
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.device_render import (
    apply_retry_variant_reset,
    approved_runtime_generation,
    device_contract_blocks_retry,
    device_record,
    device_status,
    retry_device_render,
    verify_device_record_contract,
)
from app.services.job_storage_paths import project_media_reference_lock_key

RETRY_FIELD = "render_retry"
_NEEDS_CHANGE = {"unsupported_recipe", "cancelled_by_user"}
_MISSING_ORIGINAL = {"original_missing", "missing_original", "original_unavailable"}


def retry_program_available(job: Job, execution: CreatorAgentExecution) -> bool:
    """Projection hint only; the locked recovery boundary validates every pin."""
    records = (job.assembly_plan or {}).get("_device_render_v1") or {}
    variant = execution.target_variant_id or (next(iter(records)) if len(records) == 1 else None)
    try:
        record, status = device_record(job, variant), device_status(job, variant)
        return (
            status.phase == "needs_attention"
            and not device_contract_blocks_retry(record)
            and status.reason_code not in _NEEDS_CHANGE | _MISSING_ORIGINAL
        )
    except (KeyError, ValueError, TypeError):
        return False


def _failure(code: str, message: str) -> RuntimeFailure:
    return RuntimeFailure(409, code, message, phase="dispatch", recovery="manual")


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _targets(source, session, item, approval_id=None):  # noqa: ANN001, ANN202
    return {
        ContentPlan: item.content_plan_id,
        PlanItem: item.id,
        Job: source.target_job_id,
        CreatorAgentSession: session.id,
        CreatorAgentTurn: source.turn_id,
        CreatorEditDraft: source.target_draft_id,
        CreatorAgentApproval: approval_id or _uuid((source.result or {}).get("approval_id")),
        CreatorAgentExecution: source.id,
        CreationThread: source.target_thread_id,
    }


def _validate(
    rows, *, creator_id, source, source_approval, retry=None, accepted=False, automatic=False
):  # noqa: ANN001, ANN202
    if any(
        rows.get(cls) is None
        for cls in (
            ContentPlan,
            PlanItem,
            Job,
            CreatorAgentSession,
            CreatorAgentTurn,
            CreatorEditDraft,
            CreationThread,
        )
    ):
        raise _failure(
            "render_retry_target_stale",
            "That approved edit is no longer available. Refresh the project.",
        )
    plan, item, job = (rows[c] for c in (ContentPlan, PlanItem, Job))
    session, turn, draft, thread = (
        rows[c] for c in (CreatorAgentSession, CreatorAgentTurn, CreatorEditDraft, CreationThread)
    )
    if thread.creator_id != creator_id or plan.user_id != creator_id or job.user_id != creator_id:
        raise RuntimeFailure(404, "render_retry_not_found", "Failed render not found")
    valid = (
        thread.status == "active"
        and thread.runtime_version == 2
        and thread.active_creator_agent_session_id == session.id
        and thread.active_plan_item_id == item.id
        and thread.active_job_id == job.id
        and item.current_job_id == job.id
        and job.content_plan_item_id == item.id
        and session.plan_item_id == item.id
        and session.creator_id == creator_id
        and session.target_job_id == job.id
        and source.session_id == session.id
        and source.target_thread_id == thread.id
        and source.turn_id == turn.id
        and draft.creator_id == creator_id
        and draft.thread_id == thread.id
        and draft.item_id == item.id
        and draft.is_head
        and draft.snapshot_json is not None
        and source.target_draft_id == draft.id
        and source.target_draft_revision == draft.draft_revision
        and int(plan.ownership_epoch or 0) == int(session.ownership_epoch)
        and int(job.content_plan_ownership_epoch or 0) == int(session.ownership_epoch)
        and source.target_ownership_epoch == session.ownership_epoch
        and source.target_manifest_hash == session.manifest_hash
        and source.target_generation_id == session.target_generation_id
        and source.tool_name == "render.request"
        and source_approval is not None
        and source_approval.status == "consumed"
        and source_approval.creator_id == creator_id
        and source_approval.thread_id == thread.id
        and source_approval.session_id == session.id
        and source_approval.turn_id == turn.id
        and source_approval.draft_id == draft.id
        and source_approval.draft_revision == draft.draft_revision
        and str(source.id) in source_approval.execution_ids
    )
    if not valid:
        raise _failure(
            "render_retry_target_stale",
            "The edit or its source changed. Refresh the project before retrying.",
        )
    if source.status == "outcome_unknown":
        raise _failure(
            "render_retry_outcome_unknown",
            "The previous attempt has an unknown outcome. Refresh to check its status; "
            "another render has not started.",
        )
    if source.status not in ({"accepted", "dispatched"} if automatic else {"failed"}):
        raise _failure(
            "render_retry_not_failed",
            "This render is still running or has already finished. Refresh the project.",
        )
    try:
        document = KriaDraftDocument.model_validate(draft.snapshot_json)
        valid_hash = canonical_snapshot(document)[1] == draft.snapshot_hash
    except ValueError:
        valid_hash = False
    if not valid_hash:
        raise _failure(
            "render_retry_target_stale",
            "The saved approved draft changed. Review it again before rendering.",
        )
    if document.brief_binding is not None:
        document.brief_binding.resolve(thread.id)
        if document.brief_binding.media_snapshot and media_identity(
            document.brief_binding.media_snapshot
        ) != media_identity(snapshot_media(item)):
            raise _failure(
                "render_retry_target_stale",
                "The original source selection changed. Review the updated edit first.",
            )
    records = (job.assembly_plan or {}).get("_device_render_v1") or {}
    variant = source.target_variant_id
    if not variant and len(records) == 1:
        variant = next(iter(records))
    try:
        record = device_record(job, variant)
        status = device_status(job, variant)
    except (KeyError, ValueError, TypeError) as exc:
        raise _failure(
            "render_retry_snapshot_unavailable",
            "This attempt stopped before a complete render plan was saved. Kria cannot retry "
            "the exact edit; ask for a new version and review its approval.",
        ) from exc
    variant_row = next(
        (
            v
            for v in (job.assembly_plan or {}).get("variants", [])
            if v.get("variant_id") == variant
        ),
        None,
    )
    if (
        record.get("base_generation")
        != approved_runtime_generation(job, source.target_generation_id)
        or variant_row is None
        or variant_row.get("render_generation_id") != record.get("base_generation")
    ):
        raise _failure(
            "render_retry_target_stale",
            "The saved render belongs to another version of this edit. Refresh the project.",
        )
    from app.services.phone_destination import phone_rendering_allowed_for_job  # noqa: PLC0415

    # Accepted recovery only finishes delivery bookkeeping; the program was
    # already pinned under consent. A temporary rollout pause must not retire it.
    if not accepted and (
        not phone_rendering_allowed_for_job(job)
        or status.request.recipe.required_capabilities
        - set(settings.phone_render_verified_features)
    ):
        raise _failure(
            "render_retry_capability_unavailable",
            "This edit's iPhone rendering features are temporarily unavailable. "
            "Your approved edit is saved; refresh before trying again.",
        )
    if retry is not None:
        if media_identity(retry["media_snapshot"]) != media_identity(snapshot_media(item)):
            raise _failure(
                "render_retry_target_stale",
                "The original source selection changed after the retry was prepared.",
            )
        identity = retry.get("accepted_identity") if accepted else retry.get("failed_identity")
        if status.request.identity.model_dump(mode="json") != identity:
            raise _failure(
                "render_retry_target_stale",
                "Another attempt already changed this render. Refresh the project.",
            )
    if accepted:
        verify_device_record_contract(job, record, status)
        return status
    if status.phase != "needs_attention":
        raise _failure(
            "render_retry_outcome_unknown",
            "This render is still being delivered or has already finished. "
            "Refresh before starting another attempt.",
        )
    if status.reason_code in _MISSING_ORIGINAL:
        raise _failure(
            "render_retry_originals_missing",
            "An original video is unavailable on this iPhone. "
            "Restore the original before retrying; your edit is saved.",
        )
    if device_contract_blocks_retry(record) or status.reason_code in _NEEDS_CHANGE:
        raise _failure(
            "render_retry_requires_change",
            "This failure needs a change before another render. Your approved edit has been kept.",
        )
    try:
        verify_device_record_contract(job, record, status)
    except CreatorRenderContractError as exc:
        raise _failure("render_retry_requires_change", str(exc)) from exc
    return status


async def prepare_render_retry(
    db: AsyncSession, *, thread_id: uuid.UUID, creator_id: uuid.UUID, body: RenderRetryBody
) -> RenderRetryOut:
    source = await db.get(CreatorAgentExecution, body.execution_id)
    thread_ref = await db.get(CreationThread, thread_id)
    if (
        source is None
        or thread_ref is None
        or thread_ref.creator_id != creator_id
        or source.target_thread_id != thread_id
    ):
        raise RuntimeFailure(404, "render_retry_not_found", "Failed render not found")
    session_ref = await db.get(CreatorAgentSession, source.session_id)
    item_ref = await db.get(PlanItem, session_ref.plan_item_id) if session_ref else None
    if item_ref is None:
        raise _failure(
            "render_retry_target_stale", "The saved edit is unavailable. Refresh the project."
        )
    lookup = select(CreatorAgentExecution).where(
        CreatorAgentExecution.session_id == source.session_id,
        CreatorAgentExecution.idempotency_key == f"render-retry:{source.id}",
    )
    previous = (await db.execute(lookup)).scalar_one_or_none()
    approval_lock = _uuid((previous.result or {}).get("approval_id")) if previous else None
    rows = await acquire_locked_rows(
        db, _targets(source, session_ref, item_ref, approval_lock), populate_existing=True
    )
    source = rows[CreatorAgentExecution]
    thread = rows[CreationThread]
    if source is None or thread is None:
        raise RuntimeFailure(404, "render_retry_not_found", "Failed render not found")
    # The source-execution lock serializes all taps, including different client ids.
    existing = (
        await db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.session_id == source.session_id,
                CreatorAgentExecution.idempotency_key == f"render-retry:{source.id}",
            )
        )
    ).scalar_one_or_none()
    latest = (
        await db.execute(
            select(CreatorAgentTurn.id)
            .where(CreatorAgentTurn.thread_id == thread.id)
            .order_by(CreatorAgentTurn.created_at.desc(), CreatorAgentTurn.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest != source.turn_id:
        raise _failure(
            "render_retry_target_stale",
            "You have changed this project since that failure. Refresh and use the current edit.",
        )
    from app.kria.approval_readiness import (  # noqa: PLC0415
        NOT_READY,
        ApprovalReadinessError,
        assess_saved_document,
        requires_readiness,
    )

    readiness_report = None
    raw_draft = rows[CreatorEditDraft].snapshot_json or {}
    if settings.approval_readiness_for(creator_id) or (
        isinstance(raw_draft, dict)
        and (raw_draft.get("brief_coverage") or {}).get("approval_readiness") is not None
    ):
        try:
            readiness_document = KriaDraftDocument.model_validate(raw_draft)
            # Historical snapshots without the requirement binding stay legacy.
            if requires_readiness(readiness_document, creator_id):
                readiness_report = await assess_saved_document(
                    db, rows[PlanItem], readiness_document, creator_id=creator_id
                )
        except ApprovalReadinessError as exc:
            raise _failure(NOT_READY, str(exc)) from exc
        except ValueError as exc:
            raise _failure(
                NOT_READY,
                "The saved edit's rendering evidence is unavailable. Ask Kria to prepare it again.",
            ) from exc
    if existing is not None:
        current_approval = await db.get(
            CreatorAgentApproval,
            _uuid((existing.result or {}).get("approval_id")),
            populate_existing=True,
        )
        if current_approval is None:
            raise _failure("render_retry_target_stale", "The retry approval is unavailable.")
        now = datetime.now(UTC)
        expired = current_approval.status == "pending" and current_approval.expires_at <= now
        if (
            expired or current_approval.status in {"expired", "denied", "cancelled"}
        ) and existing.accepted_at is None:
            # Lock the previous consent before Execution/Thread. A concurrent
            # renewal that advanced it is replayed, never locked in reverse order.
            if (
                rows.get(CreatorAgentApproval) is None
                or rows[CreatorAgentApproval].id != current_approval.id
            ):
                raise _failure("thread_revision_stale", "The retry changed. Refresh and try again.")
            source_approval = await db.get(
                CreatorAgentApproval, _uuid((source.result or {}).get("approval_id"))
            )
            _validate(
                rows,
                creator_id=creator_id,
                source=source,
                source_approval=source_approval,
                retry=existing.result[RETRY_FIELD],
            )
            if not await conversation_revision_matches(db, thread, body.expected_thread_revision):
                raise _failure(
                    "thread_revision_stale", "The project changed. Refresh before retrying."
                )
            if expired:
                current_approval.status = "expired"
                await db.flush()
            renewal = CreatorAgentApproval(
                **{
                    key: getattr(current_approval, key)
                    for key in (
                        "creator_id",
                        "thread_id",
                        "session_id",
                        "turn_id",
                        "draft_id",
                        "draft_revision",
                        "target_job_id",
                        "target_variant_id",
                        "target_generation_id",
                        "target_manifest_hash",
                        "target_ownership_epoch",
                        "execution_ids",
                        "consequence_summary",
                        "cost_summary",
                    )
                },
                status="pending",
                expires_at=now + timedelta(minutes=30),
            )
            db.add(renewal)
            await db.flush()
            existing.result = {**existing.result, "approval_id": str(renewal.id)}
            existing.status = "awaiting_approval"
            existing.error = None
            existing.completed_at = None
            rows[CreatorAgentTurn].status = "awaiting_approval"
            rows[CreatorAgentTurn].completed_at = None
            rows[CreatorAgentTurn].error = None
            rows[CreatorAgentSession].status = "awaiting_confirmation"
            await _append_event(
                db,
                thread,
                role="system",
                event_type="approval_requested",
                content=None,
                payload={
                    "turn_id": str(existing.turn_id),
                    "approval_id": str(renewal.id),
                    "draft_id": str(renewal.draft_id),
                    "draft_revision": renewal.draft_revision,
                    "retry_of_execution_id": str(source.id),
                },
            )
            response = RenderRetryOut(
                turn_id=str(existing.turn_id),
                approval_id=str(renewal.id),
                thread_revision=int(thread.revision),
            )
            await db.commit()
            return response
        return RenderRetryOut(
            turn_id=str(existing.turn_id),
            approval_id=existing.result["approval_id"],
            thread_revision=int(thread.revision),
            replayed=True,
        )
    status = _validate(
        rows, creator_id=creator_id, source=source, source_approval=rows.get(CreatorAgentApproval)
    )
    if not await conversation_revision_matches(db, thread, body.expected_thread_revision):
        raise _failure("thread_revision_stale", "The project changed. Refresh before retrying.")
    session, turn, draft, job, item = (
        rows[c] for c in (CreatorAgentSession, CreatorAgentTurn, CreatorEditDraft, Job, PlanItem)
    )
    now = datetime.now(UTC)
    execution = CreatorAgentExecution(
        session_id=session.id,
        turn_id=turn.id,
        idempotency_key=f"render-retry:{source.id}",
        request_digest=source.request_digest,
        expected_revision=int(session.revision),
        expected_manifest_hash=session.manifest_hash,
        tool_name="render.request",
        tool_version=1,
        risk="approval_required",
        dependency_group=1,
        group_order=1,
        target_thread_id=thread.id,
        target_draft_id=draft.id,
        target_draft_revision=draft.draft_revision,
        target_job_id=job.id,
        target_variant_id=status.request.identity.variant_id,
        target_generation_id=session.target_generation_id,
        target_manifest_hash=session.manifest_hash,
        target_ownership_epoch=session.ownership_epoch,
        status="awaiting_approval",
        started_at=now,
        awaiting_approval_at=now,
        result={
            **(
                {
                    "approval_readiness": readiness_report,
                    "approval_readiness_digest": readiness_report["digest"],
                }
                if readiness_report is not None
                else {}
            ),
            RETRY_FIELD: {
                "source_execution_id": str(source.id),
                "failed_identity": status.request.identity.model_dump(mode="json"),
                "media_snapshot": snapshot_media(item),
                "render_shape": {
                    "output_orientation": "landscape"
                    if status.request.recipe.canvas.width > status.request.recipe.canvas.height
                    else "portrait",
                    "landscape_fit": (
                        (job.all_candidates or {}).get("creator_render_shape") or {}
                    ).get("landscape_fit", "fill"),
                },
            },
        },
    )
    db.add(execution)
    await db.flush()
    approval = CreatorAgentApproval(
        creator_id=creator_id,
        thread_id=thread.id,
        session_id=session.id,
        turn_id=turn.id,
        draft_id=draft.id,
        draft_revision=draft.draft_revision,
        target_job_id=job.id,
        target_variant_id=execution.target_variant_id,
        target_generation_id=execution.target_generation_id,
        target_manifest_hash=session.manifest_hash,
        target_ownership_epoch=session.ownership_epoch,
        execution_ids=[str(execution.id)],
        consequence_summary=(
            "Retry this exact approved edit. Keep its clips, text, timing and audio unchanged."
        ),
        cost_summary="One render; no new creative planning",
        status="pending",
        expires_at=now + timedelta(minutes=30),
    )
    db.add(approval)
    await db.flush()
    execution.result = {**execution.result, "approval_id": str(approval.id)}
    turn.status = "awaiting_approval"
    turn.error = None
    turn.completed_at = None
    session.status = "awaiting_confirmation"
    session.target_variant_id = execution.target_variant_id
    await _append_event(
        db,
        thread,
        role="system",
        event_type="approval_requested",
        content=None,
        payload={
            "turn_id": str(turn.id),
            "approval_id": str(approval.id),
            "draft_id": str(draft.id),
            "draft_revision": draft.draft_revision,
            "execution_id": str(execution.id),
            "retry_of_execution_id": str(source.id),
            "consequence": approval.consequence_summary,
        },
    )
    result = RenderRetryOut(
        turn_id=str(turn.id), approval_id=str(approval.id), thread_revision=int(thread.revision)
    )
    await db.commit()
    return result


def _next_prepared_attempt(db, approval, draft, item, job, variant_id):  # noqa: ANN001, ANN202
    """KRI-722: same snapshot, new attempt id; the Job's stamp moves to the new attempt.

    Re-verifies the snapshot against the live draft, sources and capabilities first.
    Legacy (unstamped) approvals return None and keep the KRI-674 path unchanged.
    """
    from app.kria import prepared_execution as prepared  # noqa: PLC0415
    from app.models import CreatorPreparedExecution  # noqa: PLC0415

    if getattr(approval, "prepared_execution_id", None) is None:
        return None
    row = db.get(CreatorPreparedExecution, approval.prepared_execution_id)
    if row is None or row.creator_id != approval.creator_id:
        raise prepared.PreparedExecutionError(prepared.STALE, "The prepared edit is missing.")
    stamp = prepared.variant_stamp(job.assembly_plan, variant_id)
    if stamp is None or stamp.get("id") != str(row.id):
        raise prepared.PreparedExecutionError(
            prepared.STALE, "This render no longer belongs to the approved edit."
        )
    snapshot = prepared.parse(row)
    if row.invalidated_at is not None:
        raise prepared.PreparedExecutionError(
            row.invalidation_code or prepared.STALE, "The prepared edit was invalidated."
        )
    prepared.verify(
        snapshot,
        draft=draft,
        document=KriaDraftDocument.model_validate(draft.snapshot_json or {}),
        thread_id=approval.thread_id,
        sources=prepared.load_source_identities_sync(
            db,
            item,
            approval.creator_id,
            slides=prepared.SLIDE_POST_REF in snapshot.compiled_refs,
        ),
        item=item,
    )
    record = {
        **stamp,
        "attempt_id": prepared.new_attempt_id(),
        "approval_id": str(approval.id),
        "retry_of_attempt_id": stamp.get("attempt_id"),
        # Only the retried variant moves to the new attempt; siblings keep theirs.
        "variant_id": variant_id,
    }
    prepared.stamp_job(job, record)
    return record


def claim_render_retry(approval_id: uuid.UUID):  # noqa: ANN201
    """Return (handled, claim). An accepted delivery never re-pins a second time."""
    from app.tasks.kria_runtime import _ApprovalDispatchClaim  # noqa: PLC0415

    with sync_session() as db:
        approval_ref = db.get(CreatorAgentApproval, approval_id)
        execution_ref = (
            db.get(CreatorAgentExecution, _uuid(approval_ref.execution_ids[0]))
            if approval_ref and approval_ref.execution_ids
            else None
        )
        retry = (execution_ref.result or {}).get(RETRY_FIELD) if execution_ref else None
        if not isinstance(retry, dict):
            return False, None
        source = db.get(CreatorAgentExecution, _uuid(retry.get("source_execution_id")))
        session = db.get(CreatorAgentSession, execution_ref.session_id)
        item = db.get(PlanItem, session.plan_item_id) if session else None
        if source is None or item is None:
            return True, None
        # The media-reference trigger takes the owner lock on a Job write.
        # Match phone polling's owner -> project-row order before any row lock.
        db.execute(
            select(
                func.pg_advisory_xact_lock(
                    project_media_reference_lock_key(approval_ref.creator_id)
                )
            )
        )
        rows = acquire_locked_rows_sync(
            db, _targets(execution_ref, session, item, approval_id), populate_existing=True
        )
        if any(row is None for row in rows.values()):
            return True, None
        approval, execution = rows[CreatorAgentApproval], rows[CreatorAgentExecution]
        if approval.status not in {"approved", "consumed"} or execution.status not in {
            "awaiting_approval",
            "accepted",
        }:
            return True, None
        thread, turn, job, draft, session = (
            rows[c]
            for c in (CreationThread, CreatorAgentTurn, Job, CreatorEditDraft, CreatorAgentSession)
        )
        source_approval = db.get(
            CreatorAgentApproval, _uuid((source.result or {}).get("approval_id"))
        )
        latest = db.execute(
            select(CreatorAgentTurn.id)
            .where(CreatorAgentTurn.thread_id == thread.id)
            .order_by(CreatorAgentTurn.created_at.desc(), CreatorAgentTurn.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if execution.status == "awaiting_approval":
            # A refused retry must not commit a moved stamp with no recipe under it.
            job_before = (copy.deepcopy(job.all_candidates), copy.deepcopy(job.assembly_plan))
            try:
                _validate(
                    rows,
                    creator_id=approval.creator_id,
                    source=source,
                    source_approval=source_approval,
                    retry=retry,
                )
                if latest != turn.id:
                    raise _failure(
                        "render_retry_target_stale", "A newer request replaced this edit."
                    )
                if approval.draft_id != draft.id or approval.draft_revision != draft.draft_revision:
                    raise _failure("render_retry_target_stale", "The approved draft changed.")
                prepared_record = _next_prepared_attempt(
                    db, approval, draft, item, job, execution.target_variant_id
                )
                status = retry_device_render(job, execution.target_variant_id)
                apply_retry_variant_reset(job, execution.target_variant_id)
            except (RuntimeFailure, ValueError) as exc:
                job.all_candidates, job.assembly_plan = job_before
                execution.status = "failed"
                execution.error = {
                    "code": getattr(exc, "code", "render_retry_requires_change"),
                    "retryable": False,
                    "recovery": "manual",
                }
                approval.status = "cancelled"
                turn.status = "failed"
                turn.error = execution.error
                from app.tasks.kria_runtime import _append_sync_event  # noqa: PLC0415

                _append_sync_event(
                    db,
                    thread,
                    role="assistant",
                    event_type="assistant_error",
                    content=getattr(
                        exc,
                        "message",
                        "This exact retry is no longer available. Refresh the project.",
                    ),
                    payload={"turn_id": str(turn.id), **execution.error},
                )
                if session.target_job_id == job.id:
                    session.status = "awaiting_feedback"
                db.commit()
                return True, None
            now = datetime.now(UTC)
            approval.status = "consumed"
            approval.consumed_at = now
            execution.status = "accepted"
            execution.accepted_at = now
            execution.result = {
                **execution.result,
                RETRY_FIELD: {
                    **retry,
                    "accepted_identity": status.request.identity.model_dump(mode="json"),
                },
                "editor_prep": {
                    "render_destination": "device",
                    "device_recipe_revision": status.request.identity.recipe_revision,
                },
                **({"prepared_execution": prepared_record} if prepared_record else {}),
            }
            turn.status = "executing"
            session.status = "executing"
            db.commit()
        else:
            try:
                _validate(
                    rows,
                    creator_id=approval.creator_id,
                    source=source,
                    source_approval=source_approval,
                    retry=retry,
                    accepted=True,
                )
            except (RuntimeFailure, ValueError):
                # Retire only this receipt. A newer request owns the session/UI;
                # reconciler redelivery must not revive it or spin forever.
                execution.status = "stale"
                execution.completed_at = datetime.now(UTC)
                execution.error = {"code": "render_retry_target_stale", "retryable": False}
                db.commit()
                return True, None
            if latest != turn.id:
                execution.status = "stale"
                execution.completed_at = datetime.now(UTC)
                execution.error = {"code": "render_retry_target_stale", "retryable": False}
                db.commit()
                return True, None
            prep = (execution.result or {}).get("editor_prep") or {}
            if device_status(
                job, execution.target_variant_id
            ).request.identity.recipe_revision != prep.get("device_recipe_revision"):
                return True, None
        return True, _ApprovalDispatchClaim(
            approval_id=approval.id,
            execution_id=execution.id,
            thread_id=thread.id,
            turn_id=turn.id,
            session_id=session.id,
            item_id=item.id,
            ownership_epoch=int(session.ownership_epoch),
            draft_kind="retry",
            strategy=None,
            editor_prep=execution.result["editor_prep"],
            target_job_id=job.id,
            target_variant_id=execution.target_variant_id,
            target_generation_id=execution.target_generation_id,
        )
