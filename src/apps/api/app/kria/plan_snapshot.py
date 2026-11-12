"""`GET /creation-threads/{id}/plan`: the reduced live-plan state for a cold client.

Read-only: no locks, no draft bootstrap, no admission check. It reduces the thread's
`plan_block` events for the LATEST job with the same forward-only rule the client uses
(`plan_blocks.merge_block`), adds `editable`, the head draft (for "Undo all") and freshly
signed URLs (never persisted in events). Contract: docs/pipelines/live-plan-blocks.md.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.kria import plan_blocks
from app.kria.drafts import _head, _target, draft_etag
from app.kria.plan_contract import (
    SCOPABLE_SECTIONS,
    SECTION_ORDER,
    DraftHead,
    PlanBlockOut,
    PlanSnapshotOut,
    PreviousValue,
    UpdateSummary,
)
from app.kria.runtime import RuntimeFailure, _owned_thread
from app.models import CreationThreadEvent, Job, MusicTrack

log = structlog.get_logger()

_EVENT_TYPES = (plan_blocks.EVENT_TYPE, plan_blocks.SUMMARY_EVENT_TYPE, "render_cancelled")
_EVENT_LIMIT = 700
_URL_TTL_MIN = 60


def _unavailable() -> RuntimeFailure:
    return RuntimeFailure(
        404, "live_plan_review_unavailable", "Live plan review is not available", phase="tool"
    )


def _image_paths(job: Job | None) -> dict[str, str]:
    """media_id -> storage path for still images in the job's plan (signed at read time)."""
    if job is None or not isinstance(job.assembly_plan, dict):
        return {}
    plan = job.assembly_plan
    timelines: list[Any] = []
    guided = plan.get("guided_story_execution_plan")
    if isinstance(guided, dict):
        timelines.append(guided.get("story_timeline"))
    for variant in plan.get("variants") or []:
        if isinstance(variant, dict):
            timelines.append(variant.get("story_timeline"))
    paths: dict[str, str] = {}
    for timeline in timelines:
        for moment in timeline or []:
            if (
                isinstance(moment, dict)
                and moment.get("kind") == "image"
                and moment.get("media_id")
                and moment.get("gcs_path")
            ):
                paths.setdefault(str(moment["media_id"]), str(moment["gcs_path"]))
    return paths


def _sign(path: str) -> str | None:
    try:
        from app.storage import signed_get_url  # noqa: PLC0415

        return signed_get_url(path, _URL_TTL_MIN)
    except Exception as exc:  # noqa: BLE001 - a missing thumbnail is not an error
        log.info("plan_snapshot_sign_failed", error=str(exc)[:160])
        return None


async def _fill_urls(
    db: AsyncSession, section: str, payload: dict[str, Any], job: Job | None
) -> dict[str, Any]:
    """Fresh signed URLs: still-image clip thumbnails and the music art (null in events)."""
    filled = copy.deepcopy(payload)
    if section == "clips":
        images = _image_paths(job)
        for clip in filled.get("clips") or []:
            path = images.get(str(clip.get("media_id"))) if clip.get("kind") == "image" else None
            if path and (url := _sign(path)):
                clip["thumbnail_url"] = url
    elif section == "music" and filled.get("track_id"):
        track = await db.get(MusicTrack, str(filled["track_id"]))
        if track is not None and track.thumbnail_url:
            filled["art_url"] = track.thumbnail_url
    return filled


async def read_plan_snapshot(
    db: AsyncSession, *, thread_id: uuid.UUID, creator_id: uuid.UUID
) -> PlanSnapshotOut:
    if not settings.live_plan_review_enabled:
        raise _unavailable()
    thread = await _owned_thread(
        db, thread_id=thread_id, creator_id=creator_id, lock=False, require_active=False
    )
    rows = list(
        (
            await db.execute(
                select(
                    CreationThreadEvent.sequence,
                    CreationThreadEvent.event_type,
                    CreationThreadEvent.payload,
                )
                .where(
                    CreationThreadEvent.thread_id == thread.id,
                    CreationThreadEvent.event_type.in_(_EVENT_TYPES),
                )
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(_EVENT_LIMIT)
            )
        ).all()
    )
    rows.reverse()
    block_events = [
        row.payload
        for row in rows
        if row.event_type == plan_blocks.EVENT_TYPE and isinstance(row.payload, dict)
    ]
    if not block_events:
        log.info("plan_snapshot_read", thread_id=str(thread.id), status="empty")
        return PlanSnapshotOut(
            thread_id=str(thread.id),
            thread_revision=int(thread.revision),
            status="empty",
            decided_count=0,
            total_count=0,
            blocks=[],
            next_after_sequence=rows[-1].sequence if rows else -1,
        )

    latest = block_events[-1]
    job_id = str(latest.get("job_id"))
    reduced = plan_blocks.reduce_job_blocks(block_events, job_id)
    scope = plan_blocks.scope_of(block_events, job_id)
    # A job that began before `post_caption` existed has no event for it: show "Not used"
    # once everything else is decided instead of waiting forever.
    if "post_caption" not in reduced and all(
        reduced.get(s, {}).get("state") == "decided" for s in SECTION_ORDER[:-1]
    ):
        reduced["post_caption"] = {
            "section_id": "post_caption",
            "state": "decided",
            "summary": "Not used",
            "skipped": True,
            "revision": 1,
        }
    cancelled = any(
        row.event_type == "render_cancelled"
        and isinstance(row.payload, dict)
        and str(row.payload.get("job_id")) == job_id
        for row in rows
    )
    decided = sum(1 for s in SECTION_ORDER if reduced.get(s, {}).get("state") == "decided")
    if cancelled:
        status = "cancelled"
    elif decided == len(SECTION_ORDER):
        status = "ready"
    elif scope:
        status = "updating"
    else:
        status = "planning"

    target = None
    try:
        target = await _target(db, thread_id=thread.id, creator_id=creator_id, lock_item=False)
    except RuntimeFailure:
        target = None
    editable_variant = target is not None and target.job is not None
    draft: DraftHead | None = None
    if target is not None and target.job is not None:
        head = await _head(db, item_id=target.item.id, variant_key=target.variant_key, lock=False)
        if head is not None:
            draft = DraftHead(
                draft_id=str(head.id),
                draft_revision=int(head.draft_revision),
                etag=draft_etag(head.snapshot_hash),
                can_undo=head.parent_draft_id is not None,
            )
    job: Job | None = None
    if target is not None and target.job is not None and str(target.job.id) == job_id:
        job = target.job
    else:
        try:
            job = await db.get(Job, uuid.UUID(job_id))
            if job is not None and job.user_id != creator_id:
                job = None
        except (ValueError, TypeError):
            job = None

    blocks: list[PlanBlockOut] = []
    for section in SECTION_ORDER:
        entry = reduced.get(section) or {"section_id": section, "state": "waiting"}
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        if payload is not None:
            payload = await _fill_urls(db, section, payload, job)
        previous = entry.get("previous")
        try:
            previous_out = PreviousValue.model_validate(previous) if previous else None
        except ValueError:
            previous_out = None
        blocks.append(
            PlanBlockOut(
                section_id=section,  # type: ignore[arg-type]
                state=entry.get("state", "waiting"),
                summary=entry.get("summary"),
                detail=entry.get("detail"),
                intent=bool(entry.get("intent")),
                skipped=bool(entry.get("skipped")),
                decided_at=entry.get("decided_at"),
                revision=plan_blocks.block_revision(entry),
                changed=bool(entry.get("changed")),
                payload=payload,
                previous=previous_out,
                editable=bool(
                    status == "ready"
                    and editable_variant
                    and section in SCOPABLE_SECTIONS
                    and not entry.get("skipped")
                ),
            )
        )

    summary: UpdateSummary | None = None
    for row in reversed(rows):
        if (
            row.event_type == plan_blocks.SUMMARY_EVENT_TYPE
            and isinstance(row.payload, dict)
            and str(row.payload.get("job_id")) == job_id
        ):
            try:
                summary = UpdateSummary.model_validate(
                    {**row.payload, "turn_id": str(row.payload.get("turn_id") or "")}
                )
            except ValueError:
                summary = None
            break

    log.info("plan_snapshot_read", thread_id=str(thread.id), status=status)
    return PlanSnapshotOut(
        thread_id=str(thread.id),
        thread_revision=int(thread.revision),
        job_id=job_id,
        turn_id=str(latest["turn_id"]) if latest.get("turn_id") else None,
        previous_job_id=plan_blocks.previous_job_id_of(block_events, job_id),
        status=status,
        scope=scope if status == "updating" else None,  # type: ignore[arg-type]
        decided_count=decided,
        total_count=len(SECTION_ORDER),
        blocks=blocks,
        draft=draft,
        update_summary=summary,
        next_after_sequence=rows[-1].sequence,
    )
