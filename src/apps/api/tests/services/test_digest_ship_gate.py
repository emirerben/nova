"""Tests for the Phase 2 ship-gate additions to the daily digest (pure, no DB).

Covers `awaiting_approval` (PRs waiting to merge) as a sign of life + to-do, and
`stale_gating` (built tasks no gate tick picked up) as a half-dead-loop alarm.
"""

from __future__ import annotations

from app.services.digest import (
    ResilienceEvents,
    build_digest,
    digest_html,
    digest_subject,
)


def _digest(**kw):
    base = dict(
        window_hours=24,
        built=0,
        graded=0,
        escalated=0,
        grader_spend_usd=0.0,
        weekly_spend_usd=0.0,
        weekly_budget_usd=35.0,
        resilience=ResilienceEvents(),
        loop_should_have_run=False,
    )
    base.update(kw)
    return build_digest(**base)


def test_awaiting_approval_is_activity_and_renders():
    d = _digest(awaiting_approval=2)
    assert d.awaiting_approval == 2
    assert d.total_activity >= 2  # a waiting PR is a sign of life
    assert "to merge" in digest_subject(d)
    assert "Awaiting merge" in digest_html(d)


def test_awaiting_approval_prevents_dead_mans_switch():
    # Loop should have run; the only activity is PRs waiting → NOT a dead loop.
    d = _digest(awaiting_approval=1, loop_should_have_run=True)
    assert d.dead_mans_switch is False


def test_stale_gating_triggers_attention_subject_and_banner():
    d = _digest(stale_gating=1)
    assert d.needs_attention is True
    assert "gate tick stalled" in digest_subject(d)
    assert "not gated" in digest_html(d)


def test_no_stale_gating_reads_calm():
    d = _digest(built=1, awaiting_approval=1)
    assert d.needs_attention is False
    assert "stalled" not in digest_subject(d)


def test_dead_mans_switch_outranks_stale_gating_in_subject():
    # Zero activity when it should have run → the dead-man's-switch subject wins.
    d = _digest(stale_gating=0, loop_should_have_run=True)
    assert d.dead_mans_switch is True
    assert "DEAD-MAN'S-SWITCH" in digest_subject(d)
