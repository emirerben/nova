"""KRI-992: the typed brief declarations arrive beside `action`, or inside it.

Failure modes (written before the fix):
1. Model puts `delegates_all_choices` / `starts_fresh` INSIDE the `propose_strategy` action
   (prod 2026-10-27, 7 of 32 planner calls): the strict adapter rejects `extra_forbidden`,
   the runtime spends a second full planner call. => ONE call, flags read.
2. Flag both inside the action and at the top level: top level wins (never overridden).
3. A nested flag that is not a real boolean (``"true"``) must not become a declaration.
4. Any OTHER unknown key inside the action still fails closed (retry), flags or not.
5. Brief off: the nested flag is still stripped (no retry) but declares nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents.main_creator import MainCreatorAgent
from tests.agents.conftest import MockModelClient
from tests.agents.test_main_creator_agent import _input, _raw

# Expected outcomes live in a fixture, separate from this test (request-to-output evidence).
_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri992_planner_flag_placement.json").read_text()
)


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


@pytest.mark.parametrize("case", _FIXTURE["placement_cases"], ids=lambda c: c["name"])
def test_flags_inside_the_action_cost_no_second_planner_call(case: dict) -> None:
    output, client = _run(_bad_envelope(inside=case["inside"], top=case["top"]))
    expect = case["expect"]
    assert len(client.invocations) == expect["invocations"]
    assert output.delegates_all_choices is expect["delegates_all_choices"]
    assert output.starts_fresh is expect["starts_fresh"]


def test_a_nested_non_boolean_flag_is_never_a_declaration() -> None:
    output, client = _run(_bad_envelope(inside={"delegates_all_choices": "true"}))
    assert len(client.invocations) == 2  # not lifted: still fails closed through the retry
    assert output.delegates_all_choices is False


def test_other_unknown_action_keys_still_fail_closed() -> None:
    control = _FIXTURE["unknown_key_control"]
    assert control["expect"] == "schema_error"
    agent = MainCreatorAgent(None)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        agent.parse(
            _bad_envelope(inside=control["inside"], extra=control["extra"]),
            _input().model_copy(update={"brief_enabled": True}),
        )


def test_brief_off_strips_the_nested_flag_but_declares_nothing() -> None:
    output, client = _run(
        _bad_envelope(inside={"delegates_all_choices": True}), brief_enabled=False
    )
    assert len(client.invocations) == 1
    assert output.delegates_all_choices is False
