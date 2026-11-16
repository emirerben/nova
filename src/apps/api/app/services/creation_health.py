"""Daily success rate and top failure reasons for creations (KRI-1382).

Two stages are counted, each from its own table, and never added together: one creation is a
chat turn that plans a draft and then, once approved, a render Job. Adding them would count one
creation twice.

Definitions (also returned by the endpoint, see `DEFINITIONS`):

Chat turn attempt. One `creator_agent_turns` row created in the window, bucketed by its UTC
creation day. A creator message is one turn, so a creator who tries again counts again. A client
resend of the same message is the same row (unique `client_event_id`), and a worker-side claim
retry or continuation reuses the row, so neither counts twice. `superseded` and `cancelled` turns
are not attempts: the creator replaced or withdrew them, and so is a turn that finished with the
`superseded` outcome (a newer message arrived while it was still working). So is an `inert` turn:
a message that is not a creation request (the same text sent again, "what's happening", "what can
you do"), answered without planning anything. A turn still running is `in_progress` and is left
out of the rate.
- success: the turn delivered its answer or draft (`completed`, or `awaiting_approval` once a
  draft is offered) and recorded no outcome code.
- refused / blocked / recovery: `completed`, but the turn recorded an outcome code
  (`kria.turn_outcome`): it said it cannot do this as asked, held the draft back, or could not
  finish planning.
- abandoned: the draft was delivered and then never decided, so the approval was closed as
  `approval_expired` (30 minutes, by the sweep, the creator's next message, or a late Approve
  tap) or `source_replaced` (the creator swapped the media while the card was pending). The
  turn row ends `failed`, but the turn did its job, so it counts as a success and is also shown
  as `abandoned`. Counting it a failure would lower a past day's rate as drafts sit unapproved
  and report a creator's choice as a system fault.
- failed: `status = 'failed'`, with `error.code` (any other code, including an Approve tap the
  system could not honour: `approval_target_stale`, a draft that was not ready).
  A failed turn the creator retries (the Retry on a failed Create video reuses the turn row and
  resets it) logs its code first (`cleared_failures` on the turn's plan diagnostics), and that
  failure counts as its own failed attempt on the UTC day it was recorded; the retry itself is
  the row's own attempt, so a past day's failed count never drops because of a retry.

Render attempt. One `jobs` row created in the window, counted by its current outcome and its
creation day, plus every failure a retry of that same Job erased (below). `cancelled` and `draft`
jobs are not attempts, and neither are admin test renders (jobs owned by the synthetic admin user).
A re-render from a plan item mints a new Job and counts as a new attempt. An iPhone retry, the
auto-resume of a timed-out phone, an admin retry and the creator's Retry on a failed Create video
reuse the Job and clear `Job.failure_reason`; each failure they clear was logged first
(`cleared_failures`), and counts as its own failed attempt on the UTC day it was recorded, so a
retried failure is not lost and the retry is its own attempt. Not counted: a failure a worker
re-enqueue or an in-place variant re-render clears (the same attempt run again), an export the
server repaired automatically (the creator never saw it fail; see `repair_history` on the device
record), and a phone failure while an accepted video exists (the creator keeps that video).
- success: a ready status, plus `posting_failed` / `posting_partial` / `clips_ready_partial` (the
  render finished; only publishing or some clips fell short).
- failed: a failed status, or `awaiting_device` with `Job.failure_reason` set (an iPhone render
  that failed). The reason is `Job.failure_reason`, or `no_reason_<status>` when the worker wrote
  none. A logged, since-cleared failure carries the reason it was logged with.
- anything else is `in_progress`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import SYNTHETIC_USER_ID
from app.kria.turn_outcome import ABANDONED_DRAFT_CODES
from app.models import CreatorAgentTurn, Job
from app.services.cleared_failures import CLEARED_FAILURES_FIELD, UNKNOWN_TURN_FAILURE
from app.services.job_failure_reasons import JOB_FAILURE_COPY
from app.services.job_status import PLAN_ITEM_JOB_FAILED, PLAN_ITEM_JOB_READY, JobStatus

MAX_DAYS = 90
# Per source and kind, so a large `failed` group cannot push every refusal off the list.
TOP_REASONS = 10

TURN_IN_PROGRESS = ("pending", "queued", "planning", "executing", "observing")
TURN_NOT_AN_ATTEMPT = ("superseded", "cancelled")
NOT_AN_ATTEMPT_OUTCOMES = ("superseded", "inert")
TURN_FAILED = "failed"
TURN_ABANDONED = "abandoned"
# Turn-completion statuses that delivered the draft or answer (the outcome code decides the rest).
TURN_DELIVERED = ("completed", "awaiting_approval")

# posting_* and clips_ready_partial: the render finished; only publishing or some clips fell short.
_RENDER_SUCCESS = tuple(
    sorted(
        PLAN_ITEM_JOB_READY
        | {
            JobStatus.POSTING_FAILED.value,
            JobStatus.POSTING_PARTIAL.value,
            JobStatus.CLIPS_READY_PARTIAL.value,
        }
    )
)
# `failed` is no JobStatus member: `_compensate_failed_dispatch` writes it (with the reason
# `kria_dispatch_aborted`) on the Job an approval minted but could not settle.
_RENDER_ABORTED_STATUS = "failed"
_RENDER_FAILED = tuple(
    sorted(
        (
            PLAN_ITEM_JOB_FAILED
            - {
                JobStatus.CANCELLED.value,
                JobStatus.POSTING_FAILED.value,
            }
        )
        | {_RENDER_ABORTED_STATUS}
    )
)
# An iPhone render that failed never leaves `awaiting_device`: the phone, the reaper and export
# verification only set `Job.failure_reason` (`apply_device_failure_variant_update`), and a retry
# clears it again. So this status with a reason is a failed render, without one it is waiting.
_RENDER_DEVICE_STATUS = JobStatus.AWAITING_DEVICE.value
_RENDER_NOT_AN_ATTEMPT = (JobStatus.CANCELLED.value, JobStatus.DRAFT.value)
_CLEARED_SCAN_SKEW = timedelta(minutes=5)

DEFINITIONS: dict[str, str] = {
    "turn_attempt": (
        "One runtime-v2 chat turn created in the window (UTC day). A creator message is one turn, "
        "so trying again counts again; a client resend of the same message and a worker retry "
        "reuse the same turn and do not. Superseded and cancelled turns, and turns that ended "
        "with the superseded outcome (a newer message arrived mid-turn), are not attempts; neither "
        "are inert turns (the same text sent again with a new client event id, a status or help "
        "question: answered without planning anything). Turns still running are in_progress and "
        "left out of the rate. A failed turn the creator retries logs its failure first and "
        "counts it as its own failed attempt on the day it was recorded; the retry is the turn's "
        "own attempt."
    ),
    "turn_success": (
        "The turn delivered its answer or draft and recorded no outcome code. refused, blocked "
        "and recovery are completed turns that recorded an outcome code (cannot do this as asked, "
        "draft held back, could not finish planning). failed turns have status failed and an "
        "error code. abandoned turns delivered a draft that was then never decided (approval "
        "expired after 30 minutes, or the creator replaced the media): the turn row ends failed "
        "but it counts as a success, is shown under abandoned, and is not a failure reason."
    ),
    "render_attempt": (
        "One Job created in the window (UTC day), counted by its current outcome, plus every "
        "failure a retry of that same Job cleared, counted as its own failed attempt on the UTC "
        "day it was recorded (an iPhone retry or auto-resume, an admin retry and the creator's "
        "Retry on a failed Create video reuse the Job and clear Job.failure_reason). A re-render "
        "from a plan item is a new Job and a new attempt. Cancelled and draft jobs, and admin "
        "test renders (synthetic user), are not attempts. Jobs still running, and iPhone renders "
        "still waiting on the phone, are in_progress. Not counted: failures a worker re-enqueue "
        "or an in-place variant re-render clears (the same attempt run again), exports the "
        "server repaired automatically, and a phone failure while an accepted video exists."
    ),
    "render_success": (
        "A ready status, or posting_failed / posting_partial / clips_ready_partial (the render "
        "finished, only publishing or some clips fell short). failed jobs are a failed status, or "
        "awaiting_device with Job.failure_reason set (a failed iPhone render); they carry "
        "Job.failure_reason, or no_reason_<status> when none was written."
    ),
    "share": (
        "A top_failure_reasons row's count divided by all failures of its own source (turn or "
        "render). The two sources are never pooled into one denominator. The list holds the top "
        f"{TOP_REASONS} reasons for each source and kind (failed, refused, blocked, recovery), so "
        "no kind is cut by another's volume."
    ),
    "success_rate": (
        "successes / attempts per stage, null when a day has no attempts. Turns and renders are "
        "separate stages of one creation and are never added together."
    ),
}


def _rate(successes: int, attempts: int) -> float | None:
    return round(successes / attempts, 4) if attempts else None


def _utc_day(column: Any) -> Any:
    return cast(func.timezone("UTC", column), Date)


def _window(days: int, now: datetime) -> tuple[datetime, datetime, list[date]]:
    today = now.astimezone(UTC).date()
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=UTC)
    return since, since + timedelta(days=days), [first + timedelta(days=i) for i in range(days)]


async def _turn_rows(
    db: AsyncSession, since: datetime, until: datetime
) -> list[tuple[date, str, str | None, int]]:
    outcome = CreatorAgentTurn.plan_json["diagnostics"]["outcome"]
    outcome_kind = outcome["kind"].astext
    outcome_code = outcome["code"].astext
    status = CreatorAgentTurn.status
    bucket = case(
        (status.in_(TURN_IN_PROGRESS), "in_progress"),
        (
            and_(
                status == TURN_FAILED,
                CreatorAgentTurn.error["code"].astext.in_(ABANDONED_DRAFT_CODES),
            ),
            TURN_ABANDONED,
        ),
        (status == TURN_FAILED, "failed"),
        (outcome_code.is_not(None), func.coalesce(outcome_kind, "refused")),
        else_="success",
    )
    code = case(
        (status == TURN_FAILED, CreatorAgentTurn.error["code"].astext),
        (status.in_(TURN_DELIVERED), outcome_code),
        else_=None,
    )
    day = _utc_day(CreatorAgentTurn.created_at)
    rows = await db.execute(
        select(day, cast(bucket, String), code, func.count())
        .where(
            CreatorAgentTurn.created_at >= since,
            CreatorAgentTurn.created_at < until,
            status.not_in(TURN_NOT_AN_ATTEMPT),
            or_(outcome_kind.is_(None), outcome_kind.not_in(NOT_AN_ATTEMPT_OUTCOMES)),
        )
        .group_by(day, bucket, code)
    )
    return [(d, b, c, int(n)) for d, b, c, n in rows.all()]


async def _render_rows(
    db: AsyncSession, since: datetime, until: datetime
) -> list[tuple[date, str, str | None, int]]:
    status = Job.status
    device_failed = and_(status == _RENDER_DEVICE_STATUS, Job.failure_reason.is_not(None))
    failed = or_(status.in_(_RENDER_FAILED), device_failed)
    bucket = case(
        (status.in_(_RENDER_SUCCESS), "success"),
        (failed, "failed"),
        else_="in_progress",
    )
    code = case(
        (failed, func.coalesce(Job.failure_reason, "no_reason_" + status)),
        else_=None,
    )
    day = _utc_day(Job.created_at)
    rows = await db.execute(
        select(day, cast(bucket, String), code, func.count())
        .where(
            Job.created_at >= since,
            Job.created_at < until,
            status.not_in(_RENDER_NOT_AN_ATTEMPT),
            Job.user_id != SYNTHETIC_USER_ID,
        )
        .group_by(day, bucket, code)
    )
    return [(d, b, c, int(n)) for d, b, c, n in rows.all()]


def _count_log_entries(logs: Any, since: datetime, until: datetime) -> list[tuple[date, str, int]]:
    counts: Counter[tuple[date, str]] = Counter()
    for (log,) in logs.all():
        for entry in log if isinstance(log, list) else []:
            at = _entry_time(entry)
            if at is not None and since <= at < until:
                counts[(at.date(), str(entry.get("reason") or "no_reason"))] += 1
    return [(day, reason, n) for (day, reason), n in counts.items()]


async def _cleared_turn_failure_rows(
    db: AsyncSession, since: datetime, until: datetime
) -> list[tuple[date, str, str | None, int]]:
    log = CreatorAgentTurn.plan_json["diagnostics"][CLEARED_FAILURES_FIELD]
    outcome_kind = CreatorAgentTurn.plan_json["diagnostics"]["outcome"]["kind"].astext
    logs = await db.execute(
        select(log).where(
            CreatorAgentTurn.updated_at >= since - _CLEARED_SCAN_SKEW,
            log.is_not(None),
            CreatorAgentTurn.status.not_in(TURN_NOT_AN_ATTEMPT),
            or_(outcome_kind.is_(None), outcome_kind.not_in(NOT_AN_ATTEMPT_OUTCOMES)),
        )
    )
    counted = _count_log_entries(logs, since, until)
    return [(day, TURN_FAILED, reason, n) for day, reason, n in counted]


async def _cleared_failure_rows(
    db: AsyncSession, since: datetime, until: datetime
) -> list[tuple[date, str, str | None, int]]:
    # `updated_at` only narrows the scan. `at` is the worker clock, `updated_at` the transaction's
    # start on the database clock, so a failure's `at` can trail the bump: allow for the skew.
    logs = await db.execute(
        select(Job.assembly_plan[CLEARED_FAILURES_FIELD]).where(
            Job.updated_at >= since - _CLEARED_SCAN_SKEW,
            Job.assembly_plan.has_key(CLEARED_FAILURES_FIELD),
            Job.status.not_in(_RENDER_NOT_AN_ATTEMPT),
            Job.user_id != SYNTHETIC_USER_ID,
        )
    )
    return [(day, "failed", reason, n) for day, reason, n in _count_log_entries(logs, since, until)]


def _entry_time(entry: Any) -> datetime | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("at"), str):
        return None
    try:
        at = datetime.fromisoformat(entry["at"])
    except ValueError:
        return None
    return at.astimezone(UTC) if at.tzinfo else None


def _stage_totals(counts: Counter[str]) -> dict[str, Any]:
    attempts = sum(n for bucket, n in counts.items() if bucket != "in_progress")
    successes = counts["success"] + counts[TURN_ABANDONED]
    out: dict[str, Any] = {
        "attempts": attempts,
        "successes": successes,
        "success_rate": _rate(successes, attempts),
        "in_progress": counts["in_progress"],
    }
    for bucket in sorted(set(counts) - {"success", "in_progress"}):
        out[bucket] = counts[bucket]
    return out


def _reason_label(source: str, code: str) -> str | None:
    if source != "render":
        return None
    reason = JOB_FAILURE_COPY.get(code)
    return reason[0] if reason is not None else None


async def creation_health(
    db: AsyncSession, *, days: int, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since, until, window_days = _window(days, now)
    stages = {
        "turns": [
            *await _turn_rows(db, since, until),
            *await _cleared_turn_failure_rows(db, since, until),
        ],
        "renders": [
            *await _render_rows(db, since, until),
            *await _cleared_failure_rows(db, since, until),
        ],
    }

    per_day: dict[str, dict[date, Counter[str]]] = {stage: defaultdict(Counter) for stage in stages}
    totals: dict[str, Counter[str]] = {stage: Counter() for stage in stages}
    reasons: Counter[tuple[str, str, str]] = Counter()
    for stage, rows in stages.items():
        source = "turn" if stage == "turns" else "render"
        for day, bucket, code, n in rows:
            per_day[stage][day][bucket] += n
            totals[stage][bucket] += n
            if bucket not in {"success", "in_progress", TURN_ABANDONED}:
                fallback = UNKNOWN_TURN_FAILURE if source == "turn" else "no_reason"
                reasons[(source, bucket, code or fallback)] += n

    failures: Counter[str] = Counter()
    for (source, _kind, _code), n in reasons.items():
        failures[source] += n
    ranked = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    kept: Counter[tuple[str, str]] = Counter()
    top = []
    for (source, kind, code), n in ranked:
        if kept[(source, kind)] >= TOP_REASONS:
            continue
        kept[(source, kind)] += 1
        top.append(
            {
                "source": source,
                "kind": kind,
                "code": code,
                "label": _reason_label(source, code),
                "count": n,
                "share": round(n / failures[source], 4),
            }
        )
    return {
        "window": {
            "days": days,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "timezone": "UTC",
        },
        "definitions": DEFINITIONS,
        "turns": _stage_totals(totals["turns"]),
        "renders": _stage_totals(totals["renders"]),
        "daily": [
            {
                "date": day.isoformat(),
                "turns": _stage_totals(per_day["turns"][day]),
                "renders": _stage_totals(per_day["renders"][day]),
            }
            for day in window_days
        ],
        "top_failure_reasons": top,
    }
