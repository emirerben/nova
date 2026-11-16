"""A render failure that a retry would otherwise erase (KRI-1382).

`Job.failure_reason` is the only trace a failed render leaves, and every retry of the same Job
clears it (an iPhone retry or auto-resume, an admin retry, the creator's Retry on a failed
Create video). Counting the failure only while the field is set made a phone failure vanish
from the creation-health report the moment it was retried. The failure is appended here
before the clear, so it stays countable as its own failed attempt.

The log lives in `Job.assembly_plan["cleared_failures"]`, not in a column: no migration, and
the heartbeat never rewrites `assembly_plan` (models.Job), so it cannot race a retry.

The creator's Retry on a failed render also reuses the chat turn: it flips the row back to
`awaiting_approval` and clears `error`. The turn's failure is logged here too, on the turn's own
`plan_json["diagnostics"]`, so a turn that failed and was retried stays a failed turn on the day
it failed instead of turning into a success after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

CLEARED_FAILURES_FIELD = "cleared_failures"
MAX_CLEARED_FAILURES = 20
UNKNOWN_TURN_FAILURE = "unknown_failure"


def record_cleared_failure(
    job: Any, *, fallback_reason: str | None = None, now: datetime | None = None
) -> bool:
    """Append `job.failure_reason` to the Job's log; call it just before clearing the field.

    `fallback_reason` is the code for a Job the caller knows had failed but whose worker wrote no
    reason (the report counts those as `no_reason_<status>`); a caller that cannot tell passes
    nothing. Returns whether a failure was recorded. A Job with no reason and no fallback (a
    retry of a render that never failed, or a second call after the first cleared it) records
    nothing, so one failure is never logged twice.
    """
    reason = job.failure_reason or fallback_reason
    if not reason:
        return False
    assembly = dict(job.assembly_plan or {})
    log = [e for e in assembly.get(CLEARED_FAILURES_FIELD) or [] if isinstance(e, dict)]
    log.append({"reason": reason, "at": (now or datetime.now(UTC)).isoformat()})
    assembly[CLEARED_FAILURES_FIELD] = log[-MAX_CLEARED_FAILURES:]
    job.assembly_plan = assembly
    return True


def record_cleared_turn_failure(turn: Any, *, now: datetime | None = None) -> bool:
    """Append a failed turn's error code to its own log; call it just before a retry resets it.

    A turn that is not `failed`, or that failed only because its draft sat undecided
    (`ABANDONED_DRAFT_CODES`: a success, not a fault), records nothing. A failed turn with no
    error code is logged as `unknown_failure`, the code the report gives it.
    """
    from app.kria.turn_outcome import ABANDONED_DRAFT_CODES  # noqa: PLC0415

    if turn.status != "failed":
        return False
    code = (turn.error or {}).get("code") or UNKNOWN_TURN_FAILURE
    if code in ABANDONED_DRAFT_CODES:
        return False
    plan = dict(turn.plan_json or {})
    diagnostics = dict(plan.get("diagnostics") or {})
    log = [e for e in diagnostics.get(CLEARED_FAILURES_FIELD) or [] if isinstance(e, dict)]
    log.append({"reason": code, "at": (now or datetime.now(UTC)).isoformat()})
    diagnostics[CLEARED_FAILURES_FIELD] = log[-MAX_CLEARED_FAILURES:]
    plan["diagnostics"] = diagnostics
    turn.plan_json = plan
    return True
