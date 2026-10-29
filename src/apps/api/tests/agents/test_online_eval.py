"""Tests for app.agents._online_eval — sampling + judge + score posting."""

from __future__ import annotations

import sys
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.agents._runtime import ModelInvocation, ProviderOutcomeUnknownError
from app.config import settings
from app.database import sync_engine
from tests.services.test_ai_cost_control import _request
from tests.services.test_ai_cost_control import ledger as ledger


@pytest.fixture(autouse=True)
def _clear_judge_environment(monkeypatch):
    """Keep sampling and external score clients disabled unless explicitly mocked."""
    for k in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "ANTHROPIC_API_KEY",
        "NOVA_ONLINE_EVAL_SAMPLE_RATE",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# ── _should_sample / _sample_rate ────────────────────────────────────────────


def test_sample_rate_default_is_zero():
    from app.agents._online_eval import _sample_rate

    assert _sample_rate() == 0.0


def test_sample_rate_parses_valid_float(monkeypatch):
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "0.1")
    from app.agents._online_eval import _sample_rate

    assert _sample_rate() == 0.1


def test_sample_rate_clamps_to_unit_interval(monkeypatch):
    from app.agents._online_eval import _sample_rate

    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "-5")
    assert _sample_rate() == 0.0
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "5")
    assert _sample_rate() == 1.0


def test_sample_rate_invalid_returns_zero(monkeypatch):
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "nope")
    from app.agents._online_eval import _sample_rate

    assert _sample_rate() == 0.0


def test_should_sample_zero_rate_never_fires(monkeypatch):
    from app.agents._online_eval import _should_sample

    for _ in range(20):
        assert not _should_sample()


def test_should_sample_full_rate_always_fires(monkeypatch):
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "1.0")
    from app.agents._online_eval import _should_sample

    for _ in range(20):
        assert _should_sample()


# Failure modes: disabled or unauthorised spend, dropped attribution, budget bypass,
# duplicate delivery, unknown outcome retry, invalid scores and proposal mutation.


@pytest.fixture
def approved(ledger, monkeypatch):
    monkeypatch.setattr(settings, "credit_quality_review_enabled", True)
    monkeypatch.setattr(settings, "credit_quality_review_billing_verified", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    ctx = _request(ledger, "judge", estimate=0.01).ctx
    yield ctx
    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai_cost_reservations WHERE test_run_id=:run"),
            {"run": ctx.test_run_id},
        )


def _args(ctx):
    return dict(
        trace_id=ctx.request_id,
        agent_name="nova.compose.template_recipe",
        input_dict={"scene": "coffee"},
        output_dict={"shot_count": 4},
        attribution=asdict(ctx),
    )


def _transport(monkeypatch, *, error=None):
    from app.agents._text_providers import AnthropicTextClient

    invoke = MagicMock(
        side_effect=error,
        return_value=ModelInvocation(
            raw_text='{"scores":{"slot_design":4,"transition_appropriateness":5},"reasoning":"fits"}',
            tokens_in=100,
            tokens_out=30,
            model_used="claude-sonnet-5-5",
        ),
    )
    monkeypatch.setattr(AnthropicTextClient, "invoke", invoke)
    from app.agents import _langfuse

    score = MagicMock()
    monkeypatch.setattr(_langfuse, "score_trace", score)
    return invoke, score


def test_schedule_keeps_bounded_context_without_callbacks(approved, monkeypatch):
    from app.agents import _online_eval

    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "1")
    fake_task = MagicMock()
    monkeypatch.setitem(
        sys.modules, "app.tasks.online_eval", MagicMock(score_trace_async=fake_task)
    )
    approved.extra = {"unrelated_secret": "must-not-serialize"}
    approved.thought_summary_callback = lambda _: None
    _online_eval.maybe_schedule_judge(
        trace_id="trace",
        agent_name="nova.compose.template_recipe",
        input_dict={},
        output_dict={},
        ctx=approved,
    )
    payload = fake_task.delay.call_args.kwargs["attribution"]
    assert "thought_summary_callback" not in payload
    assert "unrelated_secret" not in str(payload)
    assert payload["test_run_id"] == approved.test_run_id


@pytest.mark.parametrize(
    "gate",
    [
        "credit_quality_review_enabled",
        "credit_quality_review_billing_verified",
        "ai_cost_control_enabled",
    ],
)
def test_judge_disabled_gate_never_contacts_provider(approved, monkeypatch, gate):
    from app.agents._online_eval import run_judge_and_score

    invoke, score = _transport(monkeypatch)
    monkeypatch.setattr(settings, gate, False)
    assert run_judge_and_score(**_args(approved)) is None
    invoke.assert_not_called()
    score.assert_not_called()


def test_real_ledger_judge_settles_and_posts_scores_once(approved, monkeypatch):
    from app.agents._online_eval import run_judge_and_score

    invoke, score = _transport(monkeypatch)
    args = _args(approved)
    assert run_judge_and_score(**args) == {"slot_design": 4.0, "transition_appropriateness": 5.0}
    assert run_judge_and_score(**args) is None
    assert invoke.call_count == 1
    assert invoke.call_args.kwargs["max_output_tokens"] == 800
    assert invoke.call_args.kwargs["model"] == "claude-sonnet-5-5"
    assert score.call_count == 4
    with sync_engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT status,usage_json FROM ai_cost_reservations WHERE test_run_id=:run"),
                {"run": approved.test_run_id},
            )
            .mappings()
            .one()
        )
    assert row["status"] == "settled"
    assert row["usage_json"]["_budget_capability"] == "evaluation"
    assert args["output_dict"] == {"shot_count": 4}


def test_unknown_judge_outcome_retained_and_never_retried(approved, monkeypatch):
    from app.agents._online_eval import run_judge_and_score

    invoke, score = _transport(monkeypatch, error=ProviderOutcomeUnknownError("transport lost"))
    assert run_judge_and_score(**_args(approved)) is None
    assert run_judge_and_score(**_args(approved)) is None
    assert invoke.call_count == 1
    score.assert_not_called()
    with sync_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT status,estimated_cost_usd FROM ai_cost_reservations "
                    "WHERE test_run_id=:run"
                ),
                {"run": approved.test_run_id},
            )
            .mappings()
            .one()
        )
    assert row["status"] == "unknown"
    assert row["estimated_cost_usd"] > 0


def test_judge_requires_approval_and_evaluation_budget(approved, monkeypatch):
    from app.agents._online_eval import run_judge_and_score

    invoke, _ = _transport(monkeypatch)
    approved.reservation_approved = False
    assert run_judge_and_score(**_args(approved)) is None
    approved.reservation_approved = True
    monkeypatch.setattr(settings, "ai_anthropic_evaluation_budget_usd", 0.0)
    assert run_judge_and_score(**_args(approved)) is None
    invoke.assert_not_called()


def test_production_cannot_use_test_context_to_authorize_judge(approved, monkeypatch):
    from app.agents._online_eval import run_judge_and_score

    invoke, _ = _transport(monkeypatch)
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "credit_quality_review_approved_run_id", "")
    monkeypatch.setattr(settings, "credit_quality_review_max_run_usd", 0.0)
    assert run_judge_and_score(**_args(approved)) is None
    invoke.assert_not_called()


@pytest.mark.parametrize(
    "raw", ['{"scores":{"accuracy":NaN}}', '{"scores":{"accuracy":6}}', "[]", '{"scores":"bad"}']
)
def test_invalid_judge_scores_are_not_reported(raw):
    from app.agents._online_eval import _parse_judge_json

    assert _parse_judge_json(raw) == ({}, "")


def test_missing_attribution_never_schedules(monkeypatch):
    from app.agents._online_eval import maybe_schedule_judge

    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "1")
    task = MagicMock()
    monkeypatch.setitem(sys.modules, "app.tasks.online_eval", MagicMock(score_trace_async=task))
    maybe_schedule_judge(
        trace_id="no-owner",
        agent_name="nova.compose.template_recipe",
        input_dict={},
        output_dict={},
    )
    task.delay.assert_not_called()


def test_production_approval_uses_server_run_and_runtime_owner(approved, monkeypatch):
    from app.agents._online_eval import _approved_context

    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "credit_quality_review_approved_run_id", "operator-run")
    monkeypatch.setattr(settings, "credit_quality_review_max_run_usd", 0.1)
    assert _approved_context(_args(approved)["attribution"]) is None
    approved.creator_id = "owner-from-runtime"
    approved.extra = {"paid_experiment_envelope_id": "client-header-envelope"}
    ctx = _approved_context(_args(approved)["attribution"])
    assert ctx.creator_id == "owner-from-runtime"
    assert ctx.test_run_id == "operator-run"
    assert ctx.estimated_max_cost_usd == 0.1
    assert ctx.usage_purpose == "customer"
    assert ctx.extra == {}


def test_judge_task_forwards_attribution_without_retries(monkeypatch):
    from app.agents import _online_eval
    from app.tasks.online_eval import score_trace_async

    execute = MagicMock()
    monkeypatch.setattr(_online_eval, "run_judge_and_score", execute)
    score_trace_async.run(
        trace_id="trace",
        agent_name="agent",
        input_dict={},
        output_dict={},
        attribution={"creator_id": "owner"},
    )
    assert execute.call_args.kwargs["attribution"] == {"creator_id": "owner"}
    assert score_trace_async.max_retries == 0
