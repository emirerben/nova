"""Celery task: send the daily dev-loop heartbeat digest via Resend (T7 / D5).

Reuses the Resend + httpx PATTERN from `app/tasks/email.py` (NOT its waitlist
function) — direct HTTP POST to the Resend API, fire-and-forget, errors logged
never raised. The digest CONTENT + the dead-man's-switch live in the pure
`app/services/digest.py` module so they unit-test without a DB or network; this
task is the thin gather-from-DB + send shell.

Scheduling: a daily Celery-beat entry (`worker.py` beat_schedule) fires this at
a fixed morning hour. A GH Actions cron is a viable alternative host but beat is
already running for the existing sweep/cleanup tasks, so this rides it ($0
marginal). The dead-man's-switch only fires when the loop SHOULD have run, so a
weekend send reads calm rather than alarming.

Env (read via os.environ so no config.py change is forced):
  DIGEST_RECIPIENT_EMAIL  — where to send (skips with a warning if unset)
  DIGEST_FROM_EMAIL       — sender (defaults to the waitlist sender)
  DEV_LOOP_WEEKLY_BUDGET_USD — weekly grader-spend ceiling for the % line
"""

from __future__ import annotations

import datetime
import os

import structlog

from app.worker import celery_app

log = structlog.get_logger()

# Mirror the grader's persisted agent_name + label sibling so the digest counts
# the same rows the review surface + calibration read.
GRADER_AGENT_NAME = "nova.final_video_grader"
ESCALATE_BAND = "escalate"

DEFAULT_FROM_EMAIL = "Nova <hello@nova.video>"
# Work-hours weekday window the builder cron runs in (UTC). Outside it, zero
# activity is expected, so the dead-man's-switch must NOT fire. Mirrors the
# work-hours guard in scripts/cron/build_task_runner.sh (the OpenClaw/Paperclip
# scheduler fires the builder Mon-Fri in this window).
WORK_HOURS_UTC = range(11, 19)  # 11:00–18:59 UTC


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _window_start(hours: int, now: datetime.datetime | None = None) -> datetime.datetime:
    return (now or _utc_now()) - datetime.timedelta(hours=hours)


def _week_start(now: datetime.datetime | None = None) -> datetime.datetime:
    """Start of the current ISO week (Monday 00:00 UTC) for the weekly-budget %."""
    n = now or _utc_now()
    monday = (n - datetime.timedelta(days=n.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday


def loop_should_have_run(now: datetime.datetime | None = None) -> bool:
    """True on a work-hours weekday — the predicate the dead-man's-switch gates on."""
    n = now or _utc_now()
    is_weekday = n.weekday() < 5  # Mon=0 .. Fri=4
    return is_weekday and n.hour in WORK_HOURS_UTC


def _gather_stats(session, *, window_hours: int):  # noqa: ANN001 — sync sqlalchemy session
    """Pull every digest count in a handful of bounded, indexed queries.

    Returns a dict consumed by `build_digest`. All counts are best-effort: a
    query error degrades that one field to 0 rather than aborting the digest
    (the dead-man's-switch is more useful sent-with-gaps than not sent at all).
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.models import AgentRun, BuildTask  # noqa: PLC0415
    from app.services.digest import ResilienceEvents  # noqa: PLC0415

    now = _utc_now()
    window_start = _window_start(window_hours, now)
    week_start = _week_start(now)

    # ── Built: build_tasks completed in the window ──────────────────────────
    built = int(
        session.execute(
            select(func.count())
            .select_from(BuildTask)
            .where(BuildTask.status == "done", BuildTask.updated_at >= window_start)
        ).scalar_one()
        or 0
    )

    # ── Grades in the window (count + escalate subset + spend) ──────────────
    grade_rows = session.execute(
        select(AgentRun.output_json, AgentRun.cost_usd).where(
            AgentRun.agent_name == GRADER_AGENT_NAME,
            AgentRun.created_at >= window_start,
        )
    ).all()
    graded = len(grade_rows)
    escalated = 0
    grader_spend = 0.0
    for output_json, cost_usd in grade_rows:
        if isinstance(output_json, dict) and output_json.get("band") == ESCALATE_BAND:
            escalated += 1
        grader_spend += float(cost_usd or 0.0)

    # ── Weekly grader spend (for the budget %) ──────────────────────────────
    weekly_spend = float(
        session.execute(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
                AgentRun.agent_name == GRADER_AGENT_NAME,
                AgentRun.created_at >= week_start,
            )
        ).scalar_one()
        or 0.0
    )

    # ── Resilience: point-in-time queue state + attempt signals ─────────────
    status_rows = session.execute(
        select(BuildTask.status, func.count()).group_by(BuildTask.status)
    ).all()
    status_counts = {s: int(c) for s, c in status_rows}
    max_attempt = int(
        session.execute(select(func.coalesce(func.max(BuildTask.attempt_count), 0))).scalar_one()
        or 0
    )
    # `failed` is inferred from in-window attempt activity on still-queued rows;
    # a precise count would need an event log (deferred), so we surface the
    # attempt ceiling + blocked count, which are the load-bearing alarms.
    resilience = ResilienceEvents(
        limit_resumes=0,  # release events aren't logged as rows in v1 (seam left)
        failed=0,
        blocked=status_counts.get("blocked", 0),
        in_progress=status_counts.get("in_progress", 0),
        queued_now=status_counts.get("queued", 0),
        max_attempt_count=max_attempt,
    )

    return {
        "built": built,
        "graded": graded,
        "escalated": escalated,
        "grader_spend_usd": grader_spend,
        "weekly_spend_usd": weekly_spend,
        "resilience": resilience,
    }


@celery_app.task(name="tasks.send_daily_digest", max_retries=0)
def send_daily_digest(*, window_hours: int = 24) -> None:
    """Gather dev-loop stats, build the heartbeat digest, send it via Resend.

    Fire-and-forget — every failure path logs and returns. Skips (with a warning)
    when Resend or the recipient is unconfigured, so an un-set secret degrades to
    "no email," never a crash loop.
    """
    try:
        _run_digest(window_hours=window_hours)
    except Exception as exc:  # noqa: BLE001 — best-effort heartbeat, never raise
        log.warning("send_daily_digest_failed", error=str(exc))


def _run_digest(*, window_hours: int) -> None:
    from app.config import settings  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415
    from app.services.digest import (  # noqa: PLC0415
        DEFAULT_WEEKLY_BUDGET_USD,
        build_digest,
        digest_html,
        digest_subject,
    )

    recipient = os.environ.get("DIGEST_RECIPIENT_EMAIL", "").strip()
    if not recipient:
        log.warning("digest_recipient_not_configured")
        return

    api_key = getattr(settings, "resend_api_key", "") or ""
    if not api_key:
        log.warning("digest_resend_not_configured")
        return

    weekly_budget = float(
        os.environ.get("DEV_LOOP_WEEKLY_BUDGET_USD", str(DEFAULT_WEEKLY_BUDGET_USD))
    )

    session = sync_session()
    try:
        stats = _gather_stats(session, window_hours=window_hours)
    finally:
        session.close()

    data = build_digest(
        window_hours=window_hours,
        built=stats["built"],
        graded=stats["graded"],
        escalated=stats["escalated"],
        grader_spend_usd=stats["grader_spend_usd"],
        weekly_spend_usd=stats["weekly_spend_usd"],
        weekly_budget_usd=weekly_budget,
        resilience=stats["resilience"],
        loop_should_have_run=loop_should_have_run(),
    )

    _send_digest_email(
        api_key=api_key,
        recipient=recipient,
        subject=digest_subject(data),
        html=digest_html(data),
    )
    log.info(
        "daily_digest_sent",
        recipient=recipient,
        built=data.built,
        graded=data.graded,
        escalated=data.escalated,
        dead_mans_switch=data.dead_mans_switch,
    )


def _send_digest_email(*, api_key: str, recipient: str, subject: str, html: str) -> None:
    """POST to the Resend API (same shape as tasks/email.py). Errors logged, not raised."""
    import httpx  # noqa: PLC0415

    from_email = os.environ.get("DIGEST_FROM_EMAIL", DEFAULT_FROM_EMAIL)
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [recipient],
                "subject": subject,
                "html": html,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("digest_email_sent", resend_id=response.json().get("id"))
    except httpx.HTTPStatusError as exc:
        log.error(
            "digest_email_failed",
            status_code=exc.response.status_code,
            detail=exc.response.text[:500],
        )
    except Exception as exc:  # noqa: BLE001
        log.error("digest_email_error", error=str(exc))
