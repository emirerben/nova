"""Tests for app.agents._online_eval — sampling + judge + score posting."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_modules(monkeypatch):
    """Each test resets env + module cache so the lazy singletons re-init."""
    for k in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "ANTHROPIC_API_KEY",
        "NOVA_ONLINE_EVAL_SAMPLE_RATE",
    ):
        monkeypatch.delenv(k, raising=False)
    for mod in ("app.agents._langfuse", "app.agents._online_eval"):
        sys.modules.pop(mod, None)
    yield
    for mod in ("app.agents._langfuse", "app.agents._online_eval"):
        sys.modules.pop(mod, None)


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


# ── maybe_schedule_judge ────────────────────────────────────────────────────


def test_maybe_schedule_judge_noop_when_not_sampled(monkeypatch):
    """Sample rate 0 → never even checks rubric or API key."""
    from app.agents import _online_eval

    fake_task = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "app.tasks.online_eval",
        MagicMock(score_trace_async=fake_task),
    )

    _online_eval.maybe_schedule_judge(
        trace_id="t-abc",
        agent_name="nova.compose.template_recipe",
        input_dict={},
        output_dict={},
    )
    fake_task.delay.assert_not_called()


def test_maybe_schedule_judge_noop_without_anthropic_key(monkeypatch):
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "1.0")
    # ANTHROPIC_API_KEY deliberately unset

    fake_task = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "app.tasks.online_eval",
        MagicMock(score_trace_async=fake_task),
    )

    from app.agents._online_eval import maybe_schedule_judge

    maybe_schedule_judge(
        trace_id="t-abc",
        agent_name="nova.compose.template_recipe",
        input_dict={},
        output_dict={},
    )
    fake_task.delay.assert_not_called()


def test_maybe_schedule_judge_noop_when_rubric_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    fake_task = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "app.tasks.online_eval",
        MagicMock(score_trace_async=fake_task),
    )

    from app.agents import _online_eval

    # Re-point rubrics dir to a known-empty path
    monkeypatch.setattr(_online_eval, "_RUBRICS_ROOT", tmp_path)

    _online_eval.maybe_schedule_judge(
        trace_id="t-abc",
        agent_name="nova.unknown.agent",
        input_dict={},
        output_dict={},
    )
    fake_task.delay.assert_not_called()


def test_maybe_schedule_judge_dispatches_when_all_conditions_met(monkeypatch):
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    fake_task = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "app.tasks.online_eval",
        MagicMock(score_trace_async=fake_task),
    )

    from app.agents._online_eval import maybe_schedule_judge

    maybe_schedule_judge(
        trace_id="t-abc",
        agent_name="nova.compose.template_recipe",  # real agent with a rubric
        input_dict={"file_uri": "files/x"},
        output_dict={"shot_count": 4},
    )
    fake_task.delay.assert_called_once()
    call_kwargs = fake_task.delay.call_args.kwargs
    assert call_kwargs["trace_id"] == "t-abc"
    assert call_kwargs["agent_name"] == "nova.compose.template_recipe"


def test_maybe_schedule_judge_swallows_task_import_error(monkeypatch):
    """If the Celery task module fails to import, fail open."""
    monkeypatch.setenv("NOVA_ONLINE_EVAL_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def broken_import(name, *args, **kwargs):
        if name == "app.tasks.online_eval":
            raise ImportError("boom")
        return real_import(name, *args, **kwargs)

    import builtins

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", broken_import)

    from app.agents._online_eval import maybe_schedule_judge

    # Must not raise
    maybe_schedule_judge(
        trace_id="t-abc",
        agent_name="nova.compose.template_recipe",
        input_dict={},
        output_dict={},
    )


# ── run_judge_and_score (the worker-side function) ──────────────────────────


def test_run_judge_and_score_posts_per_dimension_scores(monkeypatch):
    """End-to-end: mock Anthropic + Langfuse, verify judge runs + scores post."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")

    # Fake Langfuse SDK
    fake_lf = MagicMock()
    fake_client = MagicMock()
    fake_lf.Langfuse.return_value = fake_client
    monkeypatch.setitem(sys.modules, "langfuse", fake_lf)

    # Fake Anthropic SDK
    fake_anthropic = MagicMock()
    fake_client_a = MagicMock()
    fake_response = MagicMock()
    fake_block = MagicMock()
    fake_block.text = (
        '{"scores": {"slot_design": 4, "transition_appropriateness": 5}, "reasoning": "looks good"}'
    )
    fake_response.content = [fake_block]
    fake_client_a.messages.create.return_value = fake_response
    fake_anthropic.Anthropic.return_value = fake_client_a
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    from app.agents._online_eval import run_judge_and_score

    scores = run_judge_and_score(
        trace_id="t-prod-123",
        agent_name="nova.compose.template_recipe",
        input_dict={"file_uri": "files/x"},
        output_dict={"shot_count": 4},
    )

    assert scores == {"slot_design": 4.0, "transition_appropriateness": 5.0}
    # 4 score posts: 2 dimensions + judge_avg + judge_passed
    assert fake_client.score.call_count == 4
    posted_names = sorted(call.kwargs["name"] for call in fake_client.score.call_args_list)
    assert posted_names == [
        "judge_avg",
        "judge_passed",
        "judge_slot_design",
        "judge_transition_appropriateness",
    ]


def test_run_judge_and_score_returns_none_on_judge_failure(monkeypatch):
    """Anthropic call raises → no scores posted, returns None."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-test")

    fake_lf = MagicMock()
    fake_client = MagicMock()
    fake_lf.Langfuse.return_value = fake_client
    monkeypatch.setitem(sys.modules, "langfuse", fake_lf)

    fake_anthropic = MagicMock()
    fake_client_a = MagicMock()
    fake_client_a.messages.create.side_effect = RuntimeError("rate limit")
    fake_anthropic.Anthropic.return_value = fake_client_a
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    from app.agents._online_eval import run_judge_and_score

    result = run_judge_and_score(
        trace_id="t-prod-123",
        agent_name="nova.compose.template_recipe",
        input_dict={},
        output_dict={},
    )
    assert result is None
    fake_client.score.assert_not_called()


def test_run_judge_and_score_returns_none_when_rubric_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    from app.agents import _online_eval

    monkeypatch.setattr(_online_eval, "_RUBRICS_ROOT", tmp_path)

    result = _online_eval.run_judge_and_score(
        trace_id="t-prod-123",
        agent_name="nova.unknown.agent",
        input_dict={},
        output_dict={},
    )
    assert result is None
