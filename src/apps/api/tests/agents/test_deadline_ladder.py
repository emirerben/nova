"""Main Creator provider-deadline ladder (KRI-1037).

Ways this can fail, written before the tests:

1. A deadline repeats the same heavy call (prod thread 729b3686, 180 s then 389 s of nothing).
2. The ladder spends more than the shared turn budget (three calls, each with its own timeout).
3. A transport / SDK outcome-unknown error is treated as slowness and degrades the call.
4. The kill switch or a missing shared deadline changes the legacy single-call behavior.
5. "Try again" after a deadline opens on the heavy rung again, or an agent that never opted in
   reads the flag.
6. A deadline lowers the thinking level of an agent whose model has no named levels.

On a non-Gemini primary (KRI-1385, Sol with a Gemini Pro fallback):

7. Sol at "medium" repeats "medium" after a deadline instead of stepping to "low".
8. Sol's lighter rung also times out and the turn ends instead of Gemini answering.
9. Gemini inherits a level only Sol has ("xhigh") and rejects the call.
10. A Sol transport failure, outage, rejection, spend cap or ledger refusal ends the turn
    although Gemini could answer.
11. The cross-provider fallback starts with no turn budget left.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents import _runtime as runtime_mod
from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    ModelClient,
    ModelInvocation,
    ProviderDeadlineExceededError,
    ProviderOutcomeUnknownError,
    RunContext,
    TerminalError,
    TransientError,
)
from app.agents.main_creator import MainCreatorAgent
from app.agents.model_registry import SOL

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures/agent_evals/main_creator/kri178_talking_reaction_beats.json"
)
_TURN_BUDGET_S = 180.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Provider(ModelClient):
    """Answers each call after ``latency_s[level]``; None or past the timeout = a deadline."""

    def __init__(
        self,
        clock: _Clock,
        raw_text: str,
        latency_s: dict[str, float | BaseException | None],
        *,
        error: type[ProviderOutcomeUnknownError] = ProviderDeadlineExceededError,
    ) -> None:
        self.clock = clock
        self.raw_text = raw_text
        self.latency_s = latency_s
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> ModelInvocation:
        level = kwargs.get("thinking_level") or "none"
        timeout_s = float(kwargs["timeout_s"])
        latency = self.latency_s.get(
            f"{kwargs['model']}:{level}",
            self.latency_s.get(kwargs["model"], self.latency_s.get(level, self.latency_s.get("*"))),
        )
        if isinstance(latency, BaseException):
            self.calls.append({"model": kwargs["model"], "thinking_level": level, "t_s": 0.0})
            raise latency
        self.calls.append(
            {
                "model": kwargs["model"],
                "thinking_level": level,
                "timeout_s": timeout_s,
                "t_s": self.clock.now - 1000.0,
            }
        )
        if latency is None or latency > timeout_s:
            self.clock.now += timeout_s
            raise self.error(f"provider outcome unknown after {timeout_s:.1f}s")
        self.clock.now += latency
        return ModelInvocation(raw_text=self.raw_text, tokens_in=1, tokens_out=1)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(runtime_mod, "time", SimpleNamespace(monotonic=c.monotonic, sleep=c.sleep))
    return c


def _run(
    clock: _Clock,
    latency_s: dict[str, float | BaseException | None],
    *,
    agent_cls: type[MainCreatorAgent] = MainCreatorAgent,
    deadline: bool = True,
    start_degraded: bool = False,
    error: type[ProviderOutcomeUnknownError] = ProviderDeadlineExceededError,
    turn_budget_s: float = _TURN_BUDGET_S,
) -> tuple[_Provider, BaseException | None]:
    fixture = json.loads(_FIXTURE.read_text())
    provider = _Provider(clock, fixture["raw_text"], latency_s, error=error)
    ctx = RunContext(
        deadline_monotonic=clock.now + turn_budget_s if deadline else None,
        start_degraded=start_degraded,
        extra={"skip_agent_run_persist": True},
    )
    failure: BaseException | None = None
    try:
        agent_cls(provider).run(fixture["input"], ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - the test inspects it
        failure = exc
    return provider, failure


def test_deadline_steps_thinking_down_to_medium_instead_of_repeating_high(clock: _Clock) -> None:
    provider, failure = _run(clock, {"high": None, "medium": 40.0})

    assert failure is None
    assert [c["thinking_level"] for c in provider.calls] == ["high", "medium"]
    assert provider.calls[0]["timeout_s"] < MainCreatorAgent.spec.timeout_s  # medium keeps room
    assert provider.calls[1]["timeout_s"] == MainCreatorAgent.spec.degraded_timeout_s
    assert clock.now - 1000.0 <= _TURN_BUDGET_S


def test_second_deadline_falls_to_the_fallback_model_at_low_inside_the_budget(
    clock: _Clock,
) -> None:
    provider, failure = _run(clock, {"high": None, "medium": None, "low": 12.0})

    assert failure is None
    assert [(c["model"], c["thinking_level"]) for c in provider.calls] == [
        (MainCreatorAgent.spec.model, "high"),
        (MainCreatorAgent.spec.model, "medium"),
        (MainCreatorAgent.spec.fallback_models[0], "low"),
    ]
    assert clock.now - 1000.0 <= _TURN_BUDGET_S


def test_every_rung_timing_out_still_ends_inside_the_turn_budget(clock: _Clock) -> None:
    provider, failure = _run(clock, {"*": None})

    assert isinstance(failure, ProviderOutcomeUnknownError)
    assert len(provider.calls) <= 3
    assert clock.now - 1000.0 <= _TURN_BUDGET_S + 1e-6


def test_a_transport_outcome_unknown_is_fenced_not_degraded(clock: _Clock) -> None:
    provider, failure = _run(clock, {"*": None}, error=ProviderOutcomeUnknownError)

    assert isinstance(failure, ProviderOutcomeUnknownError)
    assert not isinstance(failure, ProviderDeadlineExceededError)
    assert [c["thinking_level"] for c in provider.calls] == ["high"]


def test_ladder_off_is_the_legacy_single_heavy_call(clock: _Clock) -> None:
    class _Off(MainCreatorAgent):
        spec = replace(MainCreatorAgent.spec, degrade_on_deadline=False)

    provider, failure = _run(clock, {"high": None, "medium": 40.0}, agent_cls=_Off)

    assert isinstance(failure, ProviderDeadlineExceededError)
    assert [c["thinking_level"] for c in provider.calls] == ["high"]
    assert provider.calls[0]["timeout_s"] == MainCreatorAgent.spec.timeout_s


def test_without_a_shared_turn_deadline_nothing_degrades(clock: _Clock) -> None:
    provider, failure = _run(clock, {"high": None, "medium": 40.0}, deadline=False)

    assert isinstance(failure, ProviderDeadlineExceededError)
    assert [c["thinking_level"] for c in provider.calls] == ["high"]


def test_try_again_after_a_deadline_opens_on_medium(clock: _Clock) -> None:
    provider, failure = _run(clock, {"high": None, "medium": 40.0}, start_degraded=True)

    assert failure is None
    assert [c["thinking_level"] for c in provider.calls] == ["medium"]


def test_start_degraded_is_ignored_by_an_agent_that_did_not_opt_in(clock: _Clock) -> None:
    class _NotOptedIn(MainCreatorAgent):
        spec = replace(MainCreatorAgent.spec, degrade_on_deadline=False)

    provider, failure = _run(clock, {"high": 40.0}, agent_cls=_NotOptedIn, start_degraded=True)

    assert failure is None
    assert [c["thinking_level"] for c in provider.calls] == ["high"]


def test_a_model_without_named_thinking_levels_is_not_lowered(clock: _Clock) -> None:
    class _Gemini25(MainCreatorAgent):
        spec = replace(MainCreatorAgent.spec, model="gemini-2.5-pro", thinking_level="high")

    provider, failure = _run(clock, {"*": None}, agent_cls=_Gemini25, start_degraded=True)

    assert isinstance(failure, ProviderOutcomeUnknownError)
    primary = [c for c in provider.calls if c["model"] == "gemini-2.5-pro"]
    assert primary and {c["thinking_level"] for c in primary} == {"high"}


@pytest.fixture(autouse=True)
def _ledger_admits_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sol's planning spend is always metered; the reservation authority itself is covered
    # against PostgreSQL in tests/services/test_credit_cap_budgets.py.
    from app.services import ai_cost_control

    monkeypatch.setattr(ai_cost_control, "reserve_paid_call", lambda request: None)


class _Sol(MainCreatorAgent):
    """The Main Creator as `MAIN_CREATOR_MODEL=gpt-6.1-sol` builds it (Gemini Pro fallback)."""

    spec = replace(
        MainCreatorAgent.spec,
        model=SOL,
        fallback_models=("gemini-3.1-pro-preview",),
        thinking_level="medium",
    )


_PRO = "gemini-3.1-pro-preview"


def test_sol_deadline_steps_medium_to_low_then_gemini_answers_at_low(clock: _Clock) -> None:
    provider, failure = _run(clock, {SOL: None, f"{_PRO}:low": 20.0}, agent_cls=_Sol)

    assert failure is None
    assert [(c["model"], c["thinking_level"]) for c in provider.calls] == [
        (SOL, "medium"),
        (SOL, "low"),
        (_PRO, "low"),
    ]
    assert clock.now - 1000.0 <= _TURN_BUDGET_S


def test_sol_xhigh_reaches_gemini_as_a_level_gemini_takes(clock: _Clock) -> None:
    class _SolXHigh(_Sol):
        spec = replace(_Sol.spec, thinking_level="xhigh", degrade_on_deadline=False)

    provider, failure = _run(
        clock, {SOL: TransientError("HTTP 503"), _PRO: 30.0}, agent_cls=_SolXHigh
    )

    assert failure is None
    assert provider.calls[-1]["model"] == _PRO
    assert provider.calls[-1]["thinking_level"] == "high"


@pytest.mark.parametrize(
    "sol_failure",
    [
        ProviderOutcomeUnknownError("connection reset after the request was sent"),
        AiBudgetExceededError(
            scope="provider:openai:capability:planning",
            reset_at="2026-12-01T00:00:00+00:00",
            cached_behavior_available=False,
        ),
        TransientError("HTTP 503"),
        AiCostControlPolicyError(
            scope="production:internal_untagged", reason="release_canary_attribution_required"
        ),
        TerminalError("text provider rejected request (HTTP 400)"),
    ],
    ids=["outcome-unknown", "planning-budget-spent", "outage", "ledger-refused", "rejected"],
)
def test_a_sol_failure_is_answered_by_gemini_in_the_same_turn(
    clock: _Clock, sol_failure: BaseException
) -> None:
    provider, failure = _run(clock, {SOL: sol_failure, _PRO: 30.0}, agent_cls=_Sol)

    assert failure is None
    assert provider.calls[-1]["model"] == _PRO
    sol_calls = [c for c in provider.calls if c["model"] == SOL]
    # An unknown outcome is never repeated at Sol; an outage is retried within max_attempts.
    expected = _Sol.spec.max_attempts if isinstance(sol_failure, TransientError) else 1
    assert len(sol_calls) == expected


def test_no_cross_provider_fallback_without_turn_budget(clock: _Clock) -> None:
    class _SolLate(_Sol):
        spec = replace(_Sol.spec, degrade_on_deadline=False)

    provider, failure = _run(
        clock,
        {SOL: ProviderOutcomeUnknownError("connection reset"), _PRO: 1.0},
        agent_cls=_SolLate,
        turn_budget_s=5.0,
    )

    assert isinstance(failure, ProviderOutcomeUnknownError)
    assert [c["model"] for c in provider.calls] == [SOL]
