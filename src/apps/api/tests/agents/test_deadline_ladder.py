"""Main Creator provider-deadline ladder (KRI-1037).

Ways this can fail, written before the tests:

1. A deadline repeats the same heavy call (prod thread 729b3686, 180 s then 389 s of nothing).
2. The ladder spends more than the shared turn budget (three calls, each with its own timeout).
3. A transport / SDK outcome-unknown error is treated as slowness and degrades the call.
4. The kill switch or a missing shared deadline changes the legacy single-call behavior.
5. "Try again" after a deadline opens on the heavy rung again, or an agent that never opted in
   reads the flag.
6. A deadline lowers the thinking level of an agent whose model has no named levels.
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
    ModelClient,
    ModelInvocation,
    ProviderDeadlineExceededError,
    ProviderOutcomeUnknownError,
    RunContext,
)
from app.agents.main_creator import MainCreatorAgent

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
        latency_s: dict[str, float | None],
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
        latency = self.latency_s.get(level, self.latency_s.get("*"))
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
    latency_s: dict[str, float | None],
    *,
    agent_cls: type[MainCreatorAgent] = MainCreatorAgent,
    deadline: bool = True,
    start_degraded: bool = False,
    error: type[ProviderOutcomeUnknownError] = ProviderDeadlineExceededError,
) -> tuple[_Provider, BaseException | None]:
    fixture = json.loads(_FIXTURE.read_text())
    provider = _Provider(clock, fixture["raw_text"], latency_s, error=error)
    ctx = RunContext(
        deadline_monotonic=clock.now + _TURN_BUDGET_S if deadline else None,
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
