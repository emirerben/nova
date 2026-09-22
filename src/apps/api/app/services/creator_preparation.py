"""Durable semantic preparation before a creator's first footage-based plan.

The attempt is an outbox receipt, never an executable render approval. All
network work happens after commit; every checkpoint rechecks the owned graph.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    ContentPlan,
    CreatorAgentEvent,
    CreatorAgentSession,
    CreatorPlanningAttempt,
    PlanItem,
    PlanItemAsset,
)
from app.services.creator_clip_analysis import clip_analysis_ready, source_media_kind

log = structlog.get_logger()
ACTIVE_STATUSES = ("queued", "running")
PUBLIC_FIELDS = ("status", "completed", "total", "message", "error_code", "retryable")


def public_preparation(session: Any) -> dict | None:
    value = getattr(session, "preparation", None)
    if not isinstance(value, dict):
        return None
    return {key: value[key] for key in PUBLIC_FIELDS if key in value}


def progress(
    attempt_id: uuid.UUID,
    status: str,
    completed: int,
    total: int,
    *,
    error_code: str | None = None,
    retryable: bool = False,
) -> dict:
    message = {
        "queued": "Preparing to analyze your clips. I'll continue automatically.",
        "analyzing": f"Analyzing your clips ({completed} of {total}). I'll continue automatically.",
        "resolving": "Your clips are analyzed. I'm matching them to your request.",
        "ready": "Your clips are ready.",
        "failed": "Clip analysis is unavailable right now. Your request is saved; try again later.",
    }[status]
    if error_code == "media_unavailable":
        message = (
            "A source file is unavailable or has changed. "
            "Reattach it to continue. Your request is saved."
        )
    elif error_code == "planning_unavailable":
        message = (
            "Planning your edit is unavailable right now. Your request is saved; try again later."
        )
    elif error_code == "preparation_unavailable":
        message = (
            "I couldn't save clip preparation right now. "
            "Your request is saved; try again in a moment."
        )
    elif error_code == "preparation_stale":
        message = (
            "Your footage changed while I was analyzing it. Send your next instruction to continue."
        )
    return dict(
        attempt_id=str(attempt_id),
        status=status,
        completed=completed,
        total=total,
        message=message,
        error_code=error_code,
        retryable=retryable,
    )


def asset_query(item: PlanItem, creator_id: uuid.UUID):
    return (
        select(PlanItemAsset)
        .where(
            PlanItemAsset.plan_item_id == item.id,
            PlanItemAsset.user_id == creator_id,
            PlanItemAsset.deduplicated_to_asset_id.is_(None),
            PlanItemAsset.status.in_(("uploaded", "queued", "analyzing", "ready", "failed")),
        )
        .order_by(PlanItemAsset.created_at, PlanItemAsset.id)
        .limit(50)
    )


def source_snapshot(item: PlanItem, assets: list[PlanItemAsset]) -> list[dict]:
    """Same identities/order as creator context; never trust client evidence."""
    sources = []
    for index, raw in enumerate(item.clip_assignments or []):
        if not isinstance(raw, dict) or not raw.get("gcs_path"):
            continue
        sources.append(
            {
                **raw,
                "media_id": raw.get("media_id") or f"clip-{index + 1}",
                "kind": raw.get("kind") or source_media_kind("", raw["gcs_path"]),
            }
        )
    if not sources and not assets:
        sources = [
            dict(media_id=f"legacy-clip-{i + 1}", gcs_path=path, kind=source_media_kind("", path))
            for i, path in enumerate(item.clip_gcs_paths or [])
        ]
    for asset in assets:
        sources.append(
            dict(
                media_id=f"asset-{asset.id}",
                asset_id=str(asset.id),
                gcs_path=asset.gcs_path,
                storage_generation=str(asset.gcs_generation or ""),
                generation=str(asset.gcs_generation or "") if asset.status == "ready" else None,
                kind=asset.kind,
                analysis=asset.analysis if asset.status == "ready" else None,
                duration_s=asset.duration_s,
                aspect=asset.aspect,
                asset_status=asset.status,
            )
        )
    # Match the existing creator-manifest cap, without silently losing sources.
    if len(sources) > 50:
        raise ValueError("creator source limit exceeded")
    return sources


def source_digest(sources: list[dict]) -> str:
    identities = [
        {k: raw.get(k) for k in ("media_id", "asset_id", "gcs_path", "storage_generation", "kind")}
        for raw in sources
    ]
    return hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()


def owns_attempt(
    attempt: CreatorPlanningAttempt,
    session: CreatorAgentSession,
    plan: ContentPlan,
    item: PlanItem,
    sources: list[dict],
) -> bool:
    return bool(
        plan.user_id == attempt.creator_id == session.creator_id
        and session.plan_item_id == item.id == attempt.plan_item_id
        and item.content_plan_id == plan.id
        and not getattr(plan, "ownership_quarantined_at", None)
        and int(plan.ownership_epoch or 0) == attempt.ownership_epoch == session.ownership_epoch
        and session.revision == attempt.session_revision
        and session.status in {"planning", "revising"}
        and source_digest(sources) == attempt.source_digest
    )


async def maybe_prepare(
    db: AsyncSession,
    *,
    item: PlanItem,
    plan: ContentPlan,
    session: CreatorAgentSession,
    inputs: dict,
) -> bool:
    assets = list((await db.execute(asset_query(item, session.creator_id))).scalars())
    sources = source_snapshot(item, assets)
    if not sources:
        return False
    source_event = (
        await db.execute(
            select(CreatorAgentEvent)
            .where(
                CreatorAgentEvent.session_id == session.id,
                CreatorAgentEvent.event_type == "user_message",
            )
            .order_by(CreatorAgentEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one()
    attempt = (
        await db.execute(
            select(CreatorPlanningAttempt).where(
                CreatorPlanningAttempt.source_event_id == source_event.id
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        attempt = CreatorPlanningAttempt(
            id=uuid.uuid4(),
            session_id=session.id,
            source_event_id=source_event.id,
            creator_id=session.creator_id,
            plan_item_id=item.id,
            ownership_epoch=int(plan.ownership_epoch or 0),
            session_revision=session.revision,
            source_digest=source_digest(sources),
            inputs=inputs,
            status="queued",
            attempts=0,
        )
        db.add(attempt)
        session.last_error = None
        session.preparation = progress(
            attempt.id,
            "queued",
            sum(clip_analysis_ready(raw, kind=raw.get("kind")) for raw in sources),
            len(sources),
        )
    attempt.last_dispatched_at = datetime.now(UTC)
    identifier = str(attempt.id)
    await db.commit()
    publish_preparation(identifier)
    return True


def publish_preparation(attempt_id: str) -> None:
    from app.tasks.creator_preparation import prepare_creator_clips  # noqa: PLC0415

    try:
        prepare_creator_clips.apply_async(
            args=[attempt_id], task_id=f"creator-prepare-{attempt_id}"
        )
    except Exception:  # noqa: BLE001 — durable outbox is reconciled by Beat
        log.warning("creator_preparation.publish_failed", attempt_id=attempt_id)


async def require_current_attempt(
    db: AsyncSession, attempt_id: str, token: str
) -> CreatorPlanningAttempt:
    """Lock Plan -> Item -> Session -> Attempt before accepting external results."""
    from fastapi import HTTPException  # noqa: PLC0415

    ref = await db.get(CreatorPlanningAttempt, uuid.UUID(attempt_id))
    item_ref = await db.get(PlanItem, ref.plan_item_id) if ref else None
    if not ref or not item_ref:
        raise HTTPException(409, "Creator preparation changed")
    plan = await db.get(
        ContentPlan, item_ref.content_plan_id, with_for_update=True, populate_existing=True
    )
    item = await db.get(PlanItem, ref.plan_item_id, with_for_update=True, populate_existing=True)
    session = await db.get(
        CreatorAgentSession,
        ref.session_id,
        with_for_update=True,
        populate_existing=True,
        # Refreshing the row also unloads relationships. Planning reads the
        # saved conversation synchronously, so reload it within this await.
        options=[selectinload(CreatorAgentSession.events)],
    )
    attempt = await db.get(
        CreatorPlanningAttempt, ref.id, with_for_update=True, populate_existing=True
    )
    assets = (
        list((await db.execute(asset_query(item, attempt.creator_id))).scalars()) if item else []
    )
    if (
        not session
        or not item
        or not plan
        or attempt.status != "running"
        or attempt.lease_token != token
        or not owns_attempt(attempt, session, plan, item, source_snapshot(item, assets))
        or not attempt.lease_until
        or attempt.lease_until <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Creator preparation changed")
    return attempt


async def finish_preparation(db: AsyncSession, session: CreatorAgentSession) -> None:
    """Settle the receipt in the same transaction as its assistant event."""
    state = getattr(session, "preparation", None)
    if not isinstance(state, dict) or state.get("status") not in {
        "queued",
        "analyzing",
        "resolving",
    }:
        return
    if session.status in {"planning", "revising"}:
        return
    attempt = await db.get(
        CreatorPlanningAttempt, uuid.UUID(state["attempt_id"]), with_for_update=True
    )
    if attempt and attempt.status in ACTIVE_STATUSES and attempt.session_id == session.id:
        failed = bool(session.last_error)
        attempt.status = "failed" if failed else "completed"
        attempt.lease_until = None
        attempt.error_code = (session.last_error or {}).get("code")
        session.preparation = progress(
            attempt.id,
            "failed" if failed else "ready",
            int(state.get("completed", 0)),
            int(state.get("total", 0)),
            error_code=attempt.error_code,
            retryable=failed
            and attempt.error_code
            not in {"provider_outcome_unknown", "media_unavailable", "clip_media_unavailable"},
        )


async def retry_inputs(db: AsyncSession, session: CreatorAgentSession, message: str) -> dict | None:
    """The retry affordance resumes the saved request; the new event is still its receipt."""
    state = getattr(session, "preparation", None)
    if (
        message.strip().rstrip(".") != "Retry preparing my clips"
        or not isinstance(state, dict)
        or state.get("status") != "failed"
    ):
        return None
    attempt = await db.get(CreatorPlanningAttempt, uuid.UUID(state["attempt_id"]))
    if not attempt or attempt.session_id != session.id or attempt.creator_id != session.creator_id:
        return None
    return {
        key: attempt.inputs[key]
        for key in ("user_message", "previous_active_plan")
        if key in attempt.inputs
    }


async def checkpoint_answers(db: AsyncSession, attempt_id: str, token: str, answers: dict) -> None:
    """Persist each completed check before the next paid batch, fenced to exact bytes."""
    from app.services.clip_intent_resolution import ANSWERS_KEY  # noqa: PLC0415
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        mutate_plan_item_media,
    )
    from app.services.speech_cleanup_preflight import (
        mutation_current_analysis_async,  # noqa: PLC0415
    )

    attempt = await require_current_attempt(db, attempt_id, token)
    item = await db.get(PlanItem, attempt.plan_item_id)
    assignments = [dict(raw) for raw in item.clip_assignments or []]
    changed = False
    for index, raw in enumerate(assignments):
        media_id = raw.get("media_id") or f"clip-{index + 1}"
        fresh = {
            key: value
            for key, value in answers.get(media_id, {}).items()
            if raw.get("generation") and str(value.get("generation")) == str(raw["generation"])
        }
        if fresh:
            analysis = dict(raw.get("analysis") or {})
            analysis[ANSWERS_KEY] = {**(analysis.get(ANSWERS_KEY) or {}), **fresh}
            raw["analysis"] = analysis
            changed = True
    if changed:
        current = await mutation_current_analysis_async(db, item.id, for_update=True)
        mutate_plan_item_media(
            item,
            detector_policy=current_detector_policy(),
            clip_assignments=assignments,
            current_analysis=current,
        )
    for asset in (await db.execute(asset_query(item, attempt.creator_id))).scalars():
        fresh = {
            key: value
            for key, value in answers.get(f"asset-{asset.id}", {}).items()
            if asset.gcs_generation and str(value.get("generation")) == str(asset.gcs_generation)
        }
        if fresh and asset.status == "ready":
            analysis = dict(asset.analysis or {})
            analysis[ANSWERS_KEY] = {**(analysis.get(ANSWERS_KEY) or {}), **fresh}
            asset.analysis = analysis
    await db.commit()
