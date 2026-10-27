"""KRI-992: the typed brief declarations arrive beside `action`, or inside it.

Failure modes (written before the fix):
1. Model puts `delegates_all_choices` / `starts_fresh` INSIDE the `propose_strategy` action
   (prod 2026-10-27, 7 of 32 planner calls): the strict adapter rejects `extra_forbidden`,
   the runtime spends a second full planner call. => ONE call, flags read.
2. Flag both inside the action and at the top level: top level wins (never overridden).
3. A nested flag that is not a real boolean (``"true"``) must not become a declaration.
4. Any OTHER unknown key inside the action still fails closed (retry), flags or not.
5. Brief off: the nested flag is still stripped (no retry) but declares nothing.
6. `delegates_all_choices` beside real brief_updates still fails closed to False.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.main_creator import MainCreatorAgent
from tests.agents.conftest import MockModelClient
from tests.agents.test_main_creator_agent import _input, _raw


def _bad_envelope(*, inside: dict, top: dict | None = None, extra: dict | None = None) -> str:
    """A recorded-shape response: `_raw`'s valid proposal plus misplaced typed flags."""

    data = json.loads(_raw(audio_strategy="licensed_music", selected=[]))
    data["action"].update(inside)
    data["action"].update(extra or {})
    data.update(top or {})
    return json.dumps(data)


def _run(raw_first: str, *, brief_enabled: bool = True):
    client = MockModelClient()
    # A clean second response, so a (wasted) retry is visible as invocation #2, not an error.
    client.queue(
        MainCreatorAgent.spec.model,
        raw_first,
        _raw(audio_strategy="licensed_music", selected=[]),
    )
    agent_input = _input().model_copy(update={"brief_enabled": brief_enabled})
    output = MainCreatorAgent(client).run(agent_input)
    return output, client


def test_flags_inside_the_action_cost_no_second_planner_call() -> None:
    output, client = _run(
        _bad_envelope(inside={"delegates_all_choices": True, "starts_fresh": False})
    )
    assert len(client.invocations) == 1
    assert output.delegates_all_choices is True
    assert output.starts_fresh is False


def test_starts_fresh_inside_the_action_is_read() -> None:
    output, client = _run(
        _bad_envelope(inside={"delegates_all_choices": False, "starts_fresh": True})
    )
    assert len(client.invocations) == 1
    assert output.starts_fresh is True
    assert output.delegates_all_choices is False


def test_top_level_flag_wins_over_a_nested_copy() -> None:
    output, client = _run(
        _bad_envelope(
            inside={"delegates_all_choices": True, "starts_fresh": True},
            top={"delegates_all_choices": False, "starts_fresh": False},
        )
    )
    assert len(client.invocations) == 1
    assert output.delegates_all_choices is False
    assert output.starts_fresh is False


def test_a_nested_non_boolean_flag_is_never_a_declaration() -> None:
    output, _ = _run(_bad_envelope(inside={"delegates_all_choices": "true"}))
    assert output.delegates_all_choices is False


def test_other_unknown_action_keys_still_fail_closed() -> None:
    agent = MainCreatorAgent(None)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        agent.parse(
            _bad_envelope(inside={"delegates_all_choices": True}, extra={"mystery": 1}),
            _input().model_copy(update={"brief_enabled": True}),
        )


def test_brief_off_strips_the_nested_flag_but_declares_nothing() -> None:
    output, client = _run(
        _bad_envelope(inside={"delegates_all_choices": True}), brief_enabled=False
    )
    assert len(client.invocations) == 1
    assert output.delegates_all_choices is False
