"""Daily heartbeat digest for the autonomous dev loop (plan D5 / M4 / T7).

The morning email doubles as a HEALTH REPORT plus a dead-man's-switch (CEO D5):
"silent factory death must not masquerade as a quiet day." This module is the
pure data + presentation layer — it computes the digest from the DB and renders
the HTML/subject. The Celery task (`app/tasks/send_daily_digest.py`) gathers the
inputs, calls `build_digest`, and ships it via Resend (reusing the httpx pattern
from `tasks/email.py`).

What the digest summarizes over the trailing window (default 24h):
  - built     : build_tasks completed (status → done)
  - graded    : final-video grades persisted (grader AgentRun rows)
  - escalated : grades in the `escalate` band (the ~10% that hit the phone)
  - $ spent   : grader Gemini spend (summed AgentRun.cost_usd)
  - weekly-budget %: spend-to-date vs a self-imposed weekly ceiling (the
    shared-subscription guard — surfaced so a budget-hungry loop is visible)
  - resilience events: limit / resume (release) / blocked / attempt-cap, read
    from build_task state so a stalled or looping loop is never silent.

The DEAD-MAN'S-SWITCH: when the loop SHOULD have run (a work-hours weekday) but
produced ZERO activity (nothing built, graded, claimed, OR even queued), the
digest is flagged `dead_mans_switch=True` so the email screams instead of
reading like a calm day. This is the whole point — a dead cron is the failure
that hides as silence.

Kept dependency-light + pure so the digest-content test drives it with synthetic
counts (no DB, no Resend, no network).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# Self-imposed weekly grader-spend ceiling (the metered side of the loop). Only
# surfaced as a % in the digest — there is no programmatic enforcement here (the
# per-grade daily cap in grade_final_video.py is the enforcement point). Override
# per-deploy via env in the task layer.
DEFAULT_WEEKLY_BUDGET_USD = 35.0


@dataclass
class ResilienceEvents:
    """Counts of build-loop resilience signals over the window + current state.

    `limit_resumes` = soft-exits on a usage limit (release_task → back to queued
    with NO attempt bump). `failed` = genuine failures (attempt bumped). `blocked`
    = tasks that tripped the attempt cap (need a human). `in_progress` /
    `queued_now` are point-in-time queue depth, surfaced so a wedged loop shows.
    """

    limit_resumes: int = 0
    failed: int = 0
    blocked: int = 0
    in_progress: int = 0
    queued_now: int = 0
    max_attempt_count: int = 0

    @property
    def has_concern(self) -> bool:
        """True if a human should glance — something blocked or looping."""
        return self.blocked > 0 or self.max_attempt_count >= 3


@dataclass
class DigestData:
    """Everything the daily heartbeat email needs, computed + ready to render."""

    window_hours: int = 24
    built: int = 0
    graded: int = 0
    escalated: int = 0
    # PRs the loop has gated + opened, now resting in `awaiting_approval` for the
    # founder to merge by hand (Phase 2). A sign of life AND the daily to-do.
    awaiting_approval: int = 0
    # UNCLAIMED `gating` rows older than the stale threshold: built tasks no gate
    # tick has picked up = the gate tick is likely down. A half-dead-loop alarm.
    stale_gating: int = 0
    grader_spend_usd: float = 0.0
    weekly_spend_usd: float = 0.0
    weekly_budget_usd: float = DEFAULT_WEEKLY_BUDGET_USD
    resilience: ResilienceEvents = field(default_factory=ResilienceEvents)
    # True when the loop should have run but did nothing (dead-man's-switch).
    dead_mans_switch: bool = False
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def weekly_budget_pct(self) -> float:
        if self.weekly_budget_usd <= 0:
            return 0.0
        return 100.0 * self.weekly_spend_usd / self.weekly_budget_usd

    @property
    def total_activity(self) -> int:
        """Any sign of life over the window (drives the dead-man's-switch)."""
        return (
            self.built
            + self.graded
            + self.awaiting_approval
            + self.resilience.limit_resumes
            + self.resilience.failed
            + self.resilience.in_progress
            + self.resilience.queued_now
        )

    @property
    def needs_attention(self) -> bool:
        # A stalled gate tick (built work nobody gated) is a half-dead loop —
        # surface it like the other concerns.
        return self.dead_mans_switch or self.stale_gating > 0 or self.resilience.has_concern


def compute_dead_mans_switch(*, total_activity: int, loop_should_have_run: bool) -> bool:
    """Fire the dead-man's-switch only when the loop SHOULD have run yet did nothing.

    Pure so the branch is unit-tested both ways. `loop_should_have_run` is the
    schedule predicate (a work-hours weekday) the task layer supplies; on a
    weekend / off-hours digest, zero activity is expected and NOT an alarm.
    """
    return loop_should_have_run and total_activity == 0


def build_digest(
    *,
    window_hours: int,
    built: int,
    graded: int,
    escalated: int,
    grader_spend_usd: float,
    weekly_spend_usd: float,
    weekly_budget_usd: float,
    resilience: ResilienceEvents,
    loop_should_have_run: bool,
    awaiting_approval: int = 0,
    stale_gating: int = 0,
    generated_at: datetime | None = None,
) -> DigestData:
    """Assemble a `DigestData` from pre-computed counts, deriving the switch."""
    data = DigestData(
        window_hours=window_hours,
        built=built,
        graded=graded,
        escalated=escalated,
        awaiting_approval=awaiting_approval,
        stale_gating=stale_gating,
        grader_spend_usd=round(grader_spend_usd, 4),
        weekly_spend_usd=round(weekly_spend_usd, 4),
        weekly_budget_usd=weekly_budget_usd,
        resilience=resilience,
        generated_at=generated_at or datetime.now(UTC),
    )
    data.dead_mans_switch = compute_dead_mans_switch(
        total_activity=data.total_activity,
        loop_should_have_run=loop_should_have_run,
    )
    return data


# ── Rendering ────────────────────────────────────────────────────────────────


def digest_subject(data: DigestData) -> str:
    """The email subject line — leads with the alarm when one fires."""
    if data.dead_mans_switch:
        return "[Kria loop] ⚠️ DEAD-MAN'S-SWITCH — zero activity when the loop should have run"
    if data.stale_gating > 0:
        return (
            f"[Kria loop] ⚠️ gate tick stalled — {data.stale_gating} built task(s) "
            f"not gated; check the gate_runner schedule"
        )
    if data.resilience.has_concern:
        return (
            f"[Kria loop] ⚠️ needs a look — "
            f"{data.resilience.blocked} blocked, {data.escalated} to review"
        )
    return (
        f"[Kria loop] built {data.built} · {data.awaiting_approval} to merge · "
        f"{data.escalated} to review · ${data.grader_spend_usd:.2f}"
    )


def digest_html(data: DigestData) -> str:
    """Render the digest as a minimal HTML email body."""
    r = data.resilience
    banner = ""
    if data.dead_mans_switch:
        banner = (
            '<div style="background:#7f1d1d;color:#fff;padding:12px 16px;border-radius:8px;'
            'margin-bottom:16px;font-weight:600;">'
            "⚠️ Dead-man's-switch: the loop should have run but produced ZERO activity. "
            "Check the GitHub Actions cron, the OAuth token, and the API health."
            "</div>"
        )
    elif data.stale_gating > 0:
        banner = (
            '<div style="background:#78350f;color:#fff;padding:12px 16px;border-radius:8px;'
            'margin-bottom:16px;font-weight:600;">'
            f"⚠️ {data.stale_gating} task(s) built but not gated for over an hour — "
            "the gate tick may be down. Check the gate_runner schedule."
            "</div>"
        )
    elif r.has_concern:
        banner = (
            '<div style="background:#78350f;color:#fff;padding:12px 16px;border-radius:8px;'
            'margin-bottom:16px;font-weight:600;">'
            f"⚠️ {r.blocked} task(s) blocked"
            + (f", a task is at attempt {r.max_attempt_count}" if r.max_attempt_count >= 3 else "")
            + ". A human should look.</div>"
        )

    def row(label: str, value: str) -> str:
        return (
            '<tr><td style="padding:6px 0;color:#555;">'
            f'{label}</td><td style="padding:6px 0;text-align:right;font-weight:600;">'
            f"{value}</td></tr>"
        )

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:520px;margin:0 auto;padding:32px 20px;">
        <h1 style="font-size:20px;margin-bottom:4px;">Kria dev-loop heartbeat</h1>
        <p style="color:#999;font-size:12px;margin-top:0;">
            Last {data.window_hours}h · generated {data.generated_at.strftime("%Y-%m-%d %H:%M UTC")}
        </p>
        {banner}
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            {row("Built (PRs done)", str(data.built))}
            {row("Awaiting merge (PRs)", str(data.awaiting_approval))}
            {row("Graded", str(data.graded))}
            {row("Escalated to phone", str(data.escalated))}
            {row("Grader spend (window)", f"${data.grader_spend_usd:.2f}")}
            {
        row(
            "Weekly budget used",
            f"${data.weekly_spend_usd:.2f} / ${data.weekly_budget_usd:.0f} "
            f"({data.weekly_budget_pct:.0f}%)",
        )
    }
        </table>
        <h2 style="font-size:14px;margin-top:24px;margin-bottom:4px;color:#555;">Resilience</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            {row("Limit-hit resumes", str(r.limit_resumes))}
            {row("Failed runs", str(r.failed))}
            {row("Blocked (need a human)", str(r.blocked))}
            {row("In progress now", str(r.in_progress))}
            {row("Queued now", str(r.queued_now))}
        </table>
        <hr style="border:none;border-top:1px solid #eee;margin:28px 0;" />
        <p style="color:#bbb;font-size:11px;">
            Kria autonomous dev loop · review escalations at /admin/review
        </p>
    </div>
    """
