"""Fences every intent-review stage takes under lock before it writes or posts.

`check_authority` says whether the review's frozen inputs are still the live ones. `thread_busy`
says whether a write to the thread would land under a turn in flight or an approval the creator
is still deciding. Both sit below the flow, repair and posting modules so none of them has to
import another to reach them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.kria.review_canon import digest as canonical_digest
from app.models import CreatorAgentApproval, CreatorAgentTurn
from app.services.render_review_admission import _inputs, load_review_rows

IN_FLIGHT_TURNS = ("pending", "queued", "planning", "executing", "observing")
_OWNERSHIP_REASONS = frozenset(
    {"authority_changed", "approved_binding_missing_or_changed", "authority_missing"}
)


def check_authority(db, row) -> tuple[Any, str | None]:
    """Lock the authority graph (before the review row) and say why it is no longer current."""
    try:
        rows = load_review_rows(db, row.execution_id, locked=True)
        current = _inputs(rows)
    except ValueError as exc:
        reason = "ownership_changed" if str(exc) in _OWNERSHIP_REASONS else "generation_changed"
        return None, reason
    except (TypeError, KeyError, AttributeError, StopIteration):
        return None, "generation_changed"
    stored = row.inputs
    if (
        canonical_digest(current) == row.input_digest
        and canonical_digest(stored) == row.input_digest
    ):
        return rows, None
    changed_owner = current.get("owner_epoch") != stored.get("owner_epoch") or (
        (current.get("binding") or {}).get("digest") != (stored.get("binding") or {}).get("digest")
    )
    return rows, "ownership_changed" if changed_owner else "generation_changed"


def thread_busy(db, thread_id, *, own_approval_is_pending: bool = False) -> bool:
    """A turn in flight, or the creator mid-decision on an approval that is not the caller's own."""
    if db.scalar(
        select(func.count())
        .select_from(CreatorAgentTurn)
        .where(
            CreatorAgentTurn.thread_id == thread_id, CreatorAgentTurn.status.in_(IN_FLIGHT_TURNS)
        )
    ):
        return True
    if own_approval_is_pending:
        return False
    return bool(
        db.scalar(
            select(func.count())
            .select_from(CreatorAgentApproval)
            .where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        )
    )
