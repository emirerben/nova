"""Put a finished intent review in the creator's chat, once, and only where it is still true.

An outcome is posted at most once per status: the decision (posted, or skipped and why) is
recorded in `review.posting[<status>]`, the events carry a client id derived from the review and
status, and the posters serialise on the thread row and then the review row. The posting never
raises into its caller; a crash between the terminal state and the post leaves a row the beat
sweep posts while the outcome is recent.

A receipt is informational. It is posted only when the chat is quiet: the reviewed render is
still the creator's current video, the creator has not spoken since it, no turn is in flight (an
event would move the thread revision under a turn that is planning and throw it back to plan
again), and the review is still switched on. A turn in flight only defers the post (the sweep
retries while the outcome is recent); a creator who moved on, or edited past a fix that is
announced with "you can still undo it", ends it. A revert that changed the creator's draft is
different: it explains a state change, so it is posted whatever the chat is doing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.database import sync_session
from app.kria.brief_binding import BriefBinding
from app.kria.intent_review_copy import UncheckedReview
from app.kria.intent_review_posts import POSTABLE_STATUSES, Post, compose_posts
from app.kria.intent_review_tier import intent_review_tier, lower_tier, tier_rank
from app.kria.reply_language import thread_reply_language
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentExecution,
    CreatorEditDraft,
    CreatorGenerationReview,
)
from app.services.intent_review_fence import check_authority, thread_busy
from app.services.pipeline_trace import pipeline_trace_for, record_pipeline_event

log = structlog.get_logger()

SWEEP_STATUSES = POSTABLE_STATUSES
STATE_CHANGING = frozenset({"repair_reverted"})
INFORMATIONAL = POSTABLE_STATUSES - STATE_CHANGING
SETTLED = frozenset({"repair_verified", "repair_unverified", "repair_reverted"})
FIX_IN_NEW_VIDEO = frozenset({"repair_verified", "repair_unverified"})
POST_WINDOW = timedelta(minutes=30)
STATE_CHANGE_WINDOW = timedelta(hours=6)
SWEEP_LIMIT = 50


def _now() -> datetime:
    return datetime.now(UTC)


def _decision(review: dict[str, Any] | None, status: str) -> str | None:
    posting = (review or {}).get("posting")
    return posting.get(status) if isinstance(posting, dict) else None


def _with_decision(row: CreatorGenerationReview, decision: str) -> dict[str, Any]:
    review = dict(row.review or {})
    return {**review, "posting": {**(review.get("posting") or {}), row.status: decision}}


def _skip(db, row: CreatorGenerationReview, why: str) -> str:
    row.review = _with_decision(row, f"skipped:{why}")
    db.commit()
    log.info("intent_review_post_skipped", review_id=str(row.id), status=row.status, why=why)
    return "skipped"


def _lock_thread(db, review: CreatorGenerationReview) -> CreationThread | None:
    execution = db.get(CreatorAgentExecution, review.execution_id)
    if execution is None or execution.target_thread_id is None:
        return None
    return db.scalar(
        select(CreationThread)
        .where(CreationThread.id == execution.target_thread_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


_BUSY = object()


def _lock_row_if_free(db, review_id):
    """The review row, or `_BUSY` when a settler or recovery holds it.

    The thread is already locked here and every chat write waits on it, so this never waits for
    a row lock: the settler holds the row while its undo needs the thread, and the sweep posts
    the outcome on its next beat.
    """
    try:
        return db.scalar(
            select(CreatorGenerationReview)
            .where(CreatorGenerationReview.id == review_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except OperationalError as exc:
        if getattr(exc.orig, "pgcode", None) == "55P03":
            return _BUSY
        raise


def _creator_spoke_since(db, thread_id, since: datetime) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "user",
                CreationThreadEvent.event_type == "user_message",
                CreationThreadEvent.created_at > since,
            )
        )
    )


def _follow_up(db, row: CreatorGenerationReview) -> CreatorGenerationReview | None:
    child_id = ((row.review or {}).get("comparison") or {}).get("child_review_id")
    child = db.get(CreatorGenerationReview, child_id) if child_id else None
    return child if child is not None and child.parent_review_id == row.id else None


def _repair_is_head(db, row: CreatorGenerationReview) -> bool:
    """Is the repair still the creator's current draft, so that Undo would undo the fix?"""
    draft = db.get(CreatorEditDraft, row.repair_draft_id) if row.repair_draft_id else None
    return bool(draft is not None and draft.is_head)


def _reference_time(db, row: CreatorGenerationReview, child) -> datetime | None:
    """The moment the video being reported on became the creator's: later chat is 'since'."""
    if child is not None:
        return child.created_at
    if row.status in SETTLED:
        execution = (
            db.get(CreatorAgentExecution, row.repair_render_execution_id)
            if row.repair_render_execution_id
            else None
        )
        return (execution.completed_at if execution else None) or row.repair_staged_at
    return row.created_at


def _brief_version(row: CreatorGenerationReview) -> int | None:
    try:
        binding = BriefBinding.model_validate_json(row.result["packet"]["binding_json"])
    except (KeyError, TypeError, ValueError):
        return None
    return binding.brief.version if binding.brief is not None else None


def _generation(row: CreatorGenerationReview, child) -> str | None:
    source = child if child is not None else row
    try:
        return str(source.result["packet"]["published_attempt"])
    except (KeyError, TypeError):
        return None


def _write(db, thread: CreationThread, row: CreatorGenerationReview, posts: list[Post]) -> None:
    from app.tasks.kria_runtime import _append_sync_event  # noqa: PLC0415

    for post in posts:
        event = _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type=post.event_type,
            content=post.content,
            payload=post.payload,
            client_event_id=f"intent-review:{row.id}:{row.status}:{post.kind}",
        )
        if post.kind == "receipt":
            row.receipt_event_id = event.id
        else:
            row.question_event_id = event.id


def _post(review_id) -> tuple[str, dict[str, Any] | None]:
    with sync_session() as db:
        first = db.get(CreatorGenerationReview, review_id)
        if (
            first is None
            or first.parent_review_id is not None
            or first.status not in POSTABLE_STATUSES
            or _decision(first.review, first.status) is not None
        ):
            return "nothing", None
        status = first.status
        stale = None
        if status in SETTLED:
            thread = _lock_thread(db, first)
        else:
            rows, stale = check_authority(db, first)
            thread = rows.thread if rows is not None else None
        row = _lock_row_if_free(db, review_id)
        if row is _BUSY:
            db.rollback()
            return "busy", None
        if row is None or row.status != status or _decision(row.review, status) is not None:
            return "nothing", None
        informational = status in INFORMATIONAL
        if informational and tier_rank(
            lower_tier(row.tier, intent_review_tier(row.owner_id))
        ) < tier_rank("review"):
            return _skip(db, row, "tier_off"), None
        if thread is None or thread.creator_id != row.owner_id or thread.runtime_version != 2:
            return _skip(db, row, "stale" if stale else "thread_changed"), None
        if stale is not None:
            return _skip(db, row, "stale"), None
        child = _follow_up(db, row) if status in SETTLED else None
        if informational:
            since = _reference_time(db, row, child)
            if since is None or _creator_spoke_since(db, thread.id, since):
                return _skip(db, row, "creator_moved_on"), None
            if thread_busy(db, thread.id, own_approval_is_pending=status == "repair_staged"):
                db.rollback()
                return "busy", None
            if status in FIX_IN_NEW_VIDEO and not _repair_is_head(db, row):
                return _skip(db, row, "head_moved"), None
        try:
            posts = compose_posts(
                review_id=str(row.id),
                status=status,
                reason=row.reason,
                review=row.review or {},
                brief_version=_brief_version(row),
                generation_id=_generation(row, child),
                child_review=(child.review if child is not None else None),
                language=thread_reply_language(thread),
            )
        except UncheckedReview:
            log.warning("intent_review_post_unchecked", review_id=str(row.id), status=status)
            return _skip(db, row, "unchecked"), None
        if not posts:
            return _skip(db, row, "nothing_to_say"), None
        _write(db, thread, row, posts)
        row.review = _with_decision(row, "posted")
        trace = {
            "review_id": str(row.id),
            "status": status,
            "events": [post.event_type for post in posts],
            "job_id": (row.inputs.get("basis") or {}).get("job_id"),
        }
        db.commit()
        return "posted", trace


def post_intent_review_outcome(review_id) -> str:
    """'posted'; 'skipped' (decided against, recorded); 'nothing' (not for this row); 'busy' (a
    settler holds the row: the sweep retries) or 'failed'."""
    try:
        outcome, trace = _post(review_id)
    except Exception:
        log.warning("intent_review_post_failed", review_id=str(review_id), exc_info=True)
        return "failed"
    if trace is not None:
        log.info("intent_review_posted", **{k: v for k, v in trace.items() if k != "job_id"})
        try:
            with pipeline_trace_for(trace["job_id"]):
                record_pipeline_event(
                    "intent_review", "posted", {k: v for k, v in trace.items() if k != "job_id"}
                )
        except Exception:
            log.warning("intent_review_post_trace_failed", review_id=str(review_id), exc_info=True)
    return outcome


def _window(status: str) -> timedelta:
    return STATE_CHANGE_WINDOW if status in STATE_CHANGING else POST_WINDOW


def post_unposted_outcomes(limit: int = SWEEP_LIMIT) -> int:
    """Post the recent outcomes a crash left unposted; returns how many were posted."""
    now = _now()
    with sync_session() as db:
        candidates = list(
            db.execute(
                select(
                    CreatorGenerationReview.id,
                    CreatorGenerationReview.status,
                    CreatorGenerationReview.updated_at,
                )
                .where(
                    CreatorGenerationReview.parent_review_id.is_(None),
                    CreatorGenerationReview.status.in_(SWEEP_STATUSES),
                    CreatorGenerationReview.updated_at > now - STATE_CHANGE_WINDOW,
                    func.jsonb_extract_path_text(
                        CreatorGenerationReview.review, "posting", CreatorGenerationReview.status
                    ).is_(None),
                )
                .order_by(CreatorGenerationReview.updated_at)
                .limit(limit)
            )
        )
    posted = 0
    for identifier, status, updated_at in candidates:
        if now - updated_at <= _window(status):
            posted += post_intent_review_outcome(identifier) == "posted"
    return posted
