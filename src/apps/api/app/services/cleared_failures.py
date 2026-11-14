"""A render failure that a retry would otherwise erase (KRI-1382).

`Job.failure_reason` is the only trace a failed render leaves, and every retry of the same Job
clears it (an iPhone retry or auto-resume, an admin retry, the creator's Retry on a failed
Create video). Counting the failure only while the field is set made a phone failure vanish
from the creation-health report the moment it was retried. The failure is appended here
before the clear, so it stays countable as its own failed attempt.

The log lives in `Job.assembly_plan["cleared_failures"]`, not in a column: no migration, and
the heartbeat never rewrites `assembly_plan` (models.Job), so it cannot race a retry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

CLEARED_FAILURES_FIELD = "cleared_failures"
MAX_CLEARED_FAILURES = 20


def record_cleared_failure(job: Any, *, now: datetime | None = None) -> bool:
    """Append `job.failure_reason` to the Job's log; call it just before clearing the field.

    Returns whether a failure was recorded. A Job with no failure reason (a retry of a render
    that never failed, or a second call after the first cleared it) records nothing, so one
    failure is never logged twice.
    """
    reason = job.failure_reason
    if not reason:
        return False
    assembly = dict(job.assembly_plan or {})
    log = [e for e in assembly.get(CLEARED_FAILURES_FIELD) or [] if isinstance(e, dict)]
    log.append({"reason": reason, "at": (now or datetime.now(UTC)).isoformat()})
    assembly[CLEARED_FAILURES_FIELD] = log[-MAX_CLEARED_FAILURES:]
    job.assembly_plan = assembly
    return True
