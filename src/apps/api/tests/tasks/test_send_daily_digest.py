"""Tests for the daily dev-loop heartbeat digest (T7 / D5).

Covers the pure digest builder (counts → DigestData + HTML), BOTH branches of
the dead-man's-switch, and the Celery task's send shell (Resend mocked, no DB,
no network). The digest content + the switch live in services/digest.py so the
content assertions need no DB; the task test mocks the session + httpx.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from app.services.digest import (
    DEFAULT_WEEKLY_BUDGET_USD,
    ResilienceEvents,
    build_digest,
    compute_dead_mans_switch,
    digest_html,
    digest_subject,
)


def _events(**kw) -> ResilienceEvents:
    return ResilienceEvents(**kw)


# ── Pure digest content ──────────────────────────────────────────────────────


class TestBuildDigestContent:
    def test_counts_and_budget_pct(self):
        data = build_digest(
            window_hours=24,
            built=3,
            graded=12,
            escalated=2,
            grader_spend_usd=1.2345,
            weekly_spend_usd=7.0,
            weekly_budget_usd=35.0,
            resilience=_events(in_progress=1, queued_now=2),
            loop_should_have_run=True,
        )
        assert data.built == 3
        assert data.graded == 12
        assert data.escalated == 2
        assert round(data.grader_spend_usd, 4) == 1.2345
        # 7 / 35 = 20%
        assert round(data.weekly_budget_pct, 1) == 20.0
        # there was activity → no switch
        assert data.dead_mans_switch is False
        assert data.needs_attention is False

    def test_budget_pct_zero_budget_is_safe(self):
        data = build_digest(
            window_hours=24,
            built=1,
            graded=0,
            escalated=0,
            grader_spend_usd=0.0,
            weekly_spend_usd=5.0,
            weekly_budget_usd=0.0,
            resilience=_events(),
            loop_should_have_run=True,
        )
        assert data.weekly_budget_pct == 0.0

    def test_html_renders_all_counts(self):
        data = build_digest(
            window_hours=24,
            built=4,
            graded=9,
            escalated=3,
            grader_spend_usd=2.5,
            weekly_spend_usd=10.0,
            weekly_budget_usd=DEFAULT_WEEKLY_BUDGET_USD,
            resilience=_events(blocked=0, in_progress=2, queued_now=1, limit_resumes=1),
            loop_should_have_run=True,
        )
        html = digest_html(data)
        assert "Nova dev-loop heartbeat" in html
        assert ">4<" in html  # built
        assert ">9<" in html  # graded
        assert ">3<" in html  # escalated
        assert "$2.50" in html  # grader spend
        # weekly budget line present
        assert f"${DEFAULT_WEEKLY_BUDGET_USD:.0f}" in html
        # normal day → no alarm banner
        assert "Dead-man" not in html


# ── Dead-man's-switch (both branches) ────────────────────────────────────────


class TestDeadMansSwitch:
    def test_fires_on_zero_activity_when_loop_should_have_run(self):
        assert compute_dead_mans_switch(total_activity=0, loop_should_have_run=True) is True

    def test_silent_on_zero_activity_when_loop_should_not_have_run(self):
        # Weekend / off-hours: zero activity is expected, NOT an alarm.
        assert compute_dead_mans_switch(total_activity=0, loop_should_have_run=False) is False

    def test_silent_when_there_was_activity(self):
        assert compute_dead_mans_switch(total_activity=5, loop_should_have_run=True) is False

    def test_build_digest_sets_switch_and_alarm_subject(self):
        data = build_digest(
            window_hours=24,
            built=0,
            graded=0,
            escalated=0,
            grader_spend_usd=0.0,
            weekly_spend_usd=0.0,
            weekly_budget_usd=35.0,
            resilience=_events(),  # nothing in progress, nothing queued
            loop_should_have_run=True,
        )
        assert data.total_activity == 0
        assert data.dead_mans_switch is True
        assert data.needs_attention is True
        assert "DEAD-MAN" in digest_subject(data).upper()
        assert "Dead-man" in digest_html(data)

    def test_queued_or_in_progress_counts_as_activity(self):
        # A loop that's mid-task (in_progress) on a quiet morning is alive.
        data = build_digest(
            window_hours=24,
            built=0,
            graded=0,
            escalated=0,
            grader_spend_usd=0.0,
            weekly_spend_usd=0.0,
            weekly_budget_usd=35.0,
            resilience=_events(in_progress=1),
            loop_should_have_run=True,
        )
        assert data.dead_mans_switch is False


# ── Resilience concern surfacing ─────────────────────────────────────────────


class TestResilienceConcern:
    def test_blocked_task_raises_concern(self):
        data = build_digest(
            window_hours=24,
            built=2,
            graded=2,
            escalated=0,
            grader_spend_usd=0.1,
            weekly_spend_usd=0.1,
            weekly_budget_usd=35.0,
            resilience=_events(blocked=1, queued_now=1),
            loop_should_have_run=True,
        )
        assert data.dead_mans_switch is False  # there was activity
        assert data.resilience.has_concern is True
        assert data.needs_attention is True
        sub = digest_subject(data)
        assert "blocked" in sub.lower()
        assert "blocked" in digest_html(data).lower()

    def test_high_attempt_count_raises_concern(self):
        ev = _events(max_attempt_count=3, queued_now=1)
        assert ev.has_concern is True


# ── loop_should_have_run predicate ───────────────────────────────────────────


class TestLoopShouldHaveRun:
    def test_weekday_work_hours_true(self):
        from app.tasks.send_daily_digest import loop_should_have_run

        # Monday 13:00 UTC
        monday_1pm = datetime.datetime(2026, 6, 1, 13, 0, tzinfo=datetime.UTC)
        assert loop_should_have_run(monday_1pm) is True

    def test_weekend_false(self):
        from app.tasks.send_daily_digest import loop_should_have_run

        # Saturday 13:00 UTC
        saturday_1pm = datetime.datetime(2026, 6, 6, 13, 0, tzinfo=datetime.UTC)
        assert loop_should_have_run(saturday_1pm) is False

    def test_weekday_off_hours_false(self):
        from app.tasks.send_daily_digest import loop_should_have_run

        # Monday 03:00 UTC (before the work-hours window)
        monday_3am = datetime.datetime(2026, 6, 1, 3, 0, tzinfo=datetime.UTC)
        assert loop_should_have_run(monday_3am) is False


# ── Task send shell (Resend mocked) ──────────────────────────────────────────


class TestSendDailyDigestTask:
    def test_skips_when_recipient_unset(self):
        with (
            patch.dict("os.environ", {"DIGEST_RECIPIENT_EMAIL": ""}, clear=False),
            patch("httpx.post") as mock_post,
        ):
            from app.tasks.send_daily_digest import _run_digest

            _run_digest(window_hours=24)
            mock_post.assert_not_called()

    def test_skips_when_resend_unset(self):
        with (
            patch.dict("os.environ", {"DIGEST_RECIPIENT_EMAIL": "me@x.com"}, clear=False),
            patch("app.config.settings") as mock_settings,
            patch("httpx.post") as mock_post,
        ):
            mock_settings.resend_api_key = ""
            from app.tasks.send_daily_digest import _run_digest

            _run_digest(window_hours=24)
            mock_post.assert_not_called()

    def test_sends_when_configured(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "resend-xyz"}
        mock_response.raise_for_status = MagicMock()

        fake_session = MagicMock()
        fake_session.close = MagicMock()

        stats = {
            "built": 2,
            "graded": 5,
            "escalated": 1,
            "awaiting_approval": 1,
            "stale_gating": 0,
            "grader_spend_usd": 0.5,
            "weekly_spend_usd": 3.0,
            "resilience": ResilienceEvents(in_progress=1, queued_now=1),
        }

        with (
            patch.dict("os.environ", {"DIGEST_RECIPIENT_EMAIL": "founder@nova.video"}, clear=False),
            patch("app.config.settings") as mock_settings,
            patch("app.database.sync_session", return_value=fake_session),
            patch("app.tasks.send_daily_digest._gather_stats", return_value=stats),
            patch("httpx.post", return_value=mock_response) as mock_post,
        ):
            mock_settings.resend_api_key = "re_test"
            from app.tasks.send_daily_digest import _run_digest

            _run_digest(window_hours=24)

            mock_post.assert_called_once()
            payload = mock_post.call_args[1]["json"]
            assert payload["to"] == ["founder@nova.video"]
            assert "Nova loop" in payload["subject"]
            assert "Nova dev-loop heartbeat" in payload["html"]
            # the session was closed (no leak)
            fake_session.close.assert_called_once()

    def test_task_never_raises(self):
        # The public task wrapper swallows everything (fire-and-forget).
        with patch("app.tasks.send_daily_digest._run_digest", side_effect=RuntimeError("boom")):
            from app.tasks.send_daily_digest import send_daily_digest

            # .run() invokes the body directly without a broker.
            send_daily_digest.run(window_hours=24)
