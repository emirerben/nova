"""When the paid intent review stands in for the legacy Gemini quality review.

The legacy review is retired for a runtime-v2 thread only while the intent review can still
give the creator a verdict. A review that ended without one (budget, provider, nothing to
check, stale evidence) hands the generation back to the legacy review on the next reconcile.
"""

from __future__ import annotations

from sqlalchemy import select

from app.kria.intent_review_tier import intent_review_replaces_legacy
from app.models import CreationThread, CreatorAgentExecution, CreatorGenerationReview

NO_VERDICT_STATUSES = frozenset({"unavailable", "exhausted", "stale", "review_unknown"})


def retirement_probe(session_id):
    """The session's runtime-v2 thread owner and the status of its newest intent review."""
    newest = (
        select(CreatorGenerationReview.status)
        .join(
            CreatorAgentExecution, CreatorAgentExecution.id == CreatorGenerationReview.execution_id
        )
        .where(
            CreatorAgentExecution.session_id == session_id,
            CreatorGenerationReview.parent_review_id.is_(None),
        )
        .order_by(CreatorGenerationReview.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(CreationThread.creator_id, newest.label("newest_status"))
        .where(
            CreationThread.active_creator_agent_session_id == session_id,
            CreationThread.runtime_version == 2,
        )
        .limit(1)
    )


def legacy_review_retired(probe_row) -> bool:
    if probe_row is None:
        return False
    return intent_review_replaces_legacy(
        probe_row.creator_id, probe_row.newest_status in NO_VERDICT_STATUSES
    )
