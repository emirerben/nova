"""Bounded, explicitly scoped capture. No providers, media reads, or inferred truth."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    CreationThread,
    CreationThreadDeletion,
    Job,
    RequestFollowingCase,
    RequestFollowingCollector,
)
from app.services.request_following_cases import digest
from app.services.training_eligibility import evaluate_training_eligibility_async

LOCK_ID = 202610160215
BATCH_SIZE = 25
RETENTION_DAYS = 30
MAX_CAPTURE_BYTES = 2_000_000


def scope():
    """One malformed identifier fails the entire configured cohort closed."""
    try:
        users = {uuid.UUID(v) for v in settings.request_following_user_ids}
        threads = {uuid.UUID(v) for v in settings.request_following_thread_ids}
        holdouts = {uuid.UUID(v) for v in settings.request_following_holdout_thread_ids}
        return users, threads, holdouts
    except (ValueError, TypeError, AttributeError):
        return set(), set(), set()


def in_scope(creator_id, thread_id):
    users, threads, _ = scope()
    return creator_id in users or thread_id in threads


def eligibility_key(decision):
    if not decision.eligible:
        return None
    return f"{decision.basis}:{decision.internal_grant_id or decision.consent_event_id}"


async def eligible_case(db, case):
    if not in_scope(case.creator_id, case.thread_id) or case.expires_at <= datetime.now(UTC):
        return False
    if await db.scalar(
        select(CreationThreadDeletion.thread_id).where(
            CreationThreadDeletion.thread_id == case.thread_id
        )
    ):
        return False
    current = await evaluate_training_eligibility_async(db, case.creator_id)
    return eligibility_key(current) == case.eligibility_key


async def source_capture(db: AsyncSession, thread: CreationThread):
    # Reuse the operator projections, including signed-URL scrubbing and the
    # same owner/coherence predicate used by the creator-facing read route.
    from app.routes.admin_creation_threads import (
        _scrub,
        creation_thread_events,
        creation_thread_integrity,
        creation_thread_turns,
    )
    from app.routes.creation_threads import _load_authorized_projection_rows

    source = {"source": "automatic", "thread_id": str(thread.id), "sections": {}, "missing": {}}
    for name, reader, limit in (
        ("events", creation_thread_events, 500),
        ("turns", creation_thread_turns, 200),
    ):
        rows, cursor = [], None
        for _ in range(4):
            page = (await reader(str(thread.id), db, limit=limit, cursor=cursor)).model_dump(
                mode="json"
            )
            rows.extend(page[name])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        source["sections"][name] = rows
        if cursor:
            source["missing"][name] = "bounded_capture_incomplete"
    integrity = (await creation_thread_integrity(str(thread.id), db)).model_dump(mode="json")
    source["sections"]["integrity"] = integrity
    item, _, job, _ = await _load_authorized_projection_rows(db, thread, degrade=True)
    # Operator integrity diagnostics deliberately inspect raw broken links.
    # Automatic learning capture must not retain their unowned video metadata.
    if job is None:
        integrity["video"] = None
    if item is not None:
        source["sections"]["plan_item"] = {
            "id": str(item.id),
            "edit_format": item.edit_format,
            "edit_proposal": item.edit_proposal,
            "slide_post": item.slide_post,
            "audio_mode": item.audio_mode,
            "scenes": item.scenes,
            "clip_assignments": item.clip_assignments,
            "user_edited": item.user_edited,
            "smart_captions_enabled": item.smart_captions_enabled,
        }
    job_ids = {uuid.UUID(j) for turn in source["sections"]["turns"] for j in turn["job_ids"]}
    if job is not None:
        job_ids.add(job.id)
    if len(job_ids) > 50:
        source["missing"]["jobs"] = "bounded_capture_incomplete"
    if job_ids:
        jobs = (
            await db.scalars(
                select(Job).where(
                    Job.id.in_(sorted(job_ids)[:50]), Job.user_id == thread.creator_id
                )
            )
        ).all()
        for row in jobs:
            source["sections"][f"job-{row.id}"] = {
                "id": str(row.id),
                "status": row.status,
                "assembly_plan": row.assembly_plan,
            }
        if len(jobs) != min(len(job_ids), 50):
            source["missing"]["jobs"] = "missing_or_unowned_jobs"
    # Scrub the complete envelope too: event content may itself contain URLs.
    source = _scrub(source)
    import json

    if len(json.dumps(source).encode()) > MAX_CAPTURE_BYTES:
        # No partial text can accidentally acquire a complete-review badge.
        source["sections"] = {}
        source["missing"] = {"source": "capture_exceeds_size_limit"}
    return source


def diagnostics(source):
    turns = source["sections"].get("turns", [])
    failed = [t["id"] for t in turns if t["status"] == "failed"]
    broken = [
        name
        for name, edge in source["sections"].get("integrity", {}).get("edges", {}).items()
        if not edge["coherent"]
    ]
    return {
        "failed_turns": failed,
        "broken_edges": broken,
        "missing": source["missing"],
        "intent_verdict": "unassessed",
        "rendered_output": "missing",
        "next_action": "Review creator intent and inspect turn receipts"
        if failed or broken
        else "Review creator intent; add a runnable regression and output evidence",
    }


async def collect_batch(db: AsyncSession):
    now = datetime.now(UTC)
    # Purge expired bodies even when collection is switched off. Metadata keeps
    # holdout membership and prevents unchanged expired content being recaptured.
    expired = (
        select(RequestFollowingCase.id)
        .where(RequestFollowingCase.expires_at <= now, RequestFollowingCase.capture.is_not(None))
        .limit(100)
    )
    await db.execute(
        update(RequestFollowingCase)
        .where(RequestFollowingCase.id.in_(expired))
        .values(capture=None, review=None, diagnostics={})
    )
    if not settings.request_following_collection_enabled:
        await db.commit()
        return {"status": "disabled"}
    users, threads, holdouts = scope()
    if not users and not threads:
        await db.commit()
        return {"status": "empty_scope"}
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_ID}):
        await db.rollback()
        return {"status": "busy"}
    cursor = await db.get(RequestFollowingCollector, 1)
    if cursor is None:
        cursor = RequestFollowingCollector(id=1)
        db.add(cursor)
        await db.flush()
    query = select(CreationThread).where(
        or_(CreationThread.creator_id.in_(users), CreationThread.id.in_(threads))
    )
    if cursor.after_thread_id:
        query = query.where(CreationThread.id > cursor.after_thread_id)
    batch = (await db.scalars(query.order_by(CreationThread.id).limit(BATCH_SIZE))).all()
    summary = {
        "status": "complete",
        "scanned": len(batch),
        "captured": 0,
        "unchanged": 0,
        "ineligible": 0,
        "held_out": 0,
        "errors": 0,
    }
    for thread in batch:
        try:
            async with db.begin_nested():
                if await db.scalar(
                    select(CreationThreadDeletion.thread_id).where(
                        CreationThreadDeletion.thread_id == thread.id
                    )
                ):
                    await db.execute(
                        delete(RequestFollowingCase).where(
                            RequestFollowingCase.thread_id == thread.id
                        )
                    )
                    summary["ineligible"] += 1
                    continue
                decision = await evaluate_training_eligibility_async(db, thread.creator_id)
                key = eligibility_key(decision)
                if not key:
                    # Revoked bodies are erased when encountered, not merely hidden.
                    await db.execute(
                        update(RequestFollowingCase)
                        .where(RequestFollowingCase.thread_id == thread.id)
                        .values(capture=None, review=None, diagnostics={})
                    )
                    summary["ineligible"] += 1
                    continue
                previous = await db.scalar(
                    select(RequestFollowingCase).where(
                        RequestFollowingCase.thread_id == thread.id,
                        RequestFollowingCase.superseded.is_(False),
                    )
                )
                split = (
                    previous.split
                    if previous
                    else ("holdout" if thread.id in holdouts else "development")
                )
                # Adding a previously exposed development thread to holdout cannot
                # retroactively create an unseen evaluation; split stays immutable.
                source = None if split == "holdout" else await source_capture(db, thread)
                source_hash = (
                    digest(source) if source is not None else digest({"holdout": str(thread.id)})
                )
                if (
                    previous
                    and previous.source_sha256 == source_hash
                    and previous.eligibility_key == key
                ):
                    summary["unchanged"] += 1
                    continue
                if previous:
                    previous.superseded = True
                    await db.flush()
                db.add(
                    RequestFollowingCase(
                        creator_id=thread.creator_id,
                        thread_id=thread.id,
                        source_sha256=source_hash,
                        eligibility_key=key,
                        split=split,
                        capture=source,
                        diagnostics=diagnostics(source) if source else {},
                        expires_at=now + timedelta(days=RETENTION_DAYS),
                    )
                )
                summary["held_out" if split == "holdout" else "captured"] += 1
        except Exception:
            # Savepoint isolates bad source rows; never log raw creator content.
            summary["errors"] += 1
    cursor.after_thread_id = batch[-1].id if len(batch) == BATCH_SIZE else None
    cursor.last_run_at = now
    cursor.summary = summary
    await db.commit()
    return summary
