"""When the paid intent review stands in for the legacy Gemini quality review.

The legacy review is retired for a runtime-v2 thread only while the intent review of that very
generation can still give the creator a verdict: it exists and has not ended without one, or
it is about to be admitted. A generation whose review ended without a verdict (budget,
provider, nothing to check, stale evidence), or that will never get one, is handed to the
legacy review on the next reconcile.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select

from app.kria.intent_review_tier import intent_review_replaces_legacy
from app.models import CreationThread, CreatorAgentExecution, CreatorGenerationReview

LEGACY_REVIEW_RETIRED = "legacy_review_retired"
OBSERVER_WINDOW = timedelta(minutes=5)
NO_VERDICT_STATUSES = frozenset({"unavailable", "exhausted", "stale", "review_unknown"})


def retirement_probe(session_id, *, job_id, variant_id, generation_id):
    """The session's runtime-v2 thread owner, the status of the intent review of exactly this
    generation, and whether that review is still to be admitted (its execution awaits the
    observer, or its admission is pending recovery)."""
    target = and_(
        CreatorAgentExecution.session_id == session_id,
        CreatorAgentExecution.target_job_id == job_id,
        CreatorAgentExecution.target_variant_id == variant_id,
    )
    published = CreatorAgentExecution.result["render_generation_id"].astext == generation_id
    newest = (
        select(CreatorGenerationReview.status)
        .join(
            CreatorAgentExecution, CreatorAgentExecution.id == CreatorGenerationReview.execution_id
        )
        .where(target, published)
        .order_by(CreatorGenerationReview.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    admission_expected = (
        select(CreatorAgentExecution.id)
        .where(
            target,
            or_(
                and_(
                    CreatorAgentExecution.status == "dispatched",
                    CreatorAgentExecution.dispatched_at > datetime.now(UTC) - OBSERVER_WINDOW,
                ),
                and_(
                    published,
                    CreatorAgentExecution.result["intent_review_admission"]["status"].astext
                    == "pending",
                ),
            ),
        )
        .exists()
    )
    return (
        select(
            CreationThread.creator_id,
            newest.label("newest_status"),
            admission_expected.label("admission_expected"),
        )
        .where(
            CreationThread.active_creator_agent_session_id == session_id,
            CreationThread.runtime_version == 2,
        )
        .limit(1)
    )


def legacy_review_retired(probe_row) -> bool:
    if probe_row is None:
        return False
    status = probe_row.newest_status
    return intent_review_replaces_legacy(
        probe_row.creator_id,
        status in NO_VERDICT_STATUSES if status is not None else not probe_row.admission_expected,
    )
