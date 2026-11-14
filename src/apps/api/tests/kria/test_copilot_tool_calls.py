"""KRI-1393: the chat copilot on typed tool calls, replayed through the real turn path.

Each case replays recorded tool-call answers (the provider envelope a client returns) through
the tools agent and the real copilot parser, the per-op editor compile the Kria planner binds to
the draft, the shared turn route (``run_copilot_turn`` with ``EDIT_COPILOT_OUTPUT_MODE=tools``),
and then ``compile_editor_ops`` on the ops the turn returns. Only the provider is scripted.

Failure modes, written before the code:
T1  the model calls a tool that does not exist         -> test_unknown_tool_is_named_never_applied
T2  a call's arguments are not a JSON object           -> test_malformed_arguments_are_repaired
T3  the model calls an op whose family is closed      -> test_closed_family_call_is_refused
T4  parallel calls apply out of order                 -> test_calls_apply_in_the_order_made
T5  the model answers in prose, no tool call          -> test_prose_answer_is_retried_as_tools
T6  the model calls ops but never `reply` in the same response (one tool per response)
                                                       -> test_operations_without_reply_wait...
T7  the reply never comes                             -> test_no_reply_applies_nothing
T8  the answer is truncated                           -> test_truncated_answer_is_retried_lighter
T9  a provider rejects the tool schemas (strict mode, Gemini's size limit, recursive refs):
    the tools are non-strict, nesting is described in text, no $ref
                                                       -> tests/agents/test_copilot_tools.py
T10 a rejected op is re-asked without its tool result, or an accepted op is sent again
                                                       -> test_rejected_op_comes_back_as_a_tool...
T11 a later round loses the provider's own turn (reasoning, signatures)
                                                       -> test_rejected_op_comes_back_as_a_tool...
"""

from __future__ import annotations

import copy
import time
import uuid

import pytest

from app.agents._runtime import ModelInvocation
from app.agents._tool_calls import ToolAnswer, ToolCall
from app.config import settings
from app.routes import _copilot as copilot
from app.services import kria_editor_ops as ops_mod
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    editor_compile_check,
)
from tests.services.test_kria_editor_ops import _job, _variant

TEXTS = ("Hook", "Beach", "Sunset")
CAPABILITIES = {"text_elements": True, "timeline": True, "clips": {}}
PREPARED = "I prepared the supported changes."
WHOLE_EDIT = "I prepared this edit for the editor to validate and stage."


@pytest.fixture
def edit(monkeypatch):
    monkeypatch.setattr(settings, "edit_copilot_output_mode", "tools")
    monkeypatch.setattr(ops_mod, "_editor_capabilities", lambda _job, _variant: CAPABILITIES)
    variant = _variant()
    bar = variant["text_elements"][0]
    variant["text_elements"] = [
        {**bar, "id": f"text-{i}", "text": text, "start_s": float(i), "end_s": float(i) + 1.0}
        for i, text in enumerate(TEXTS)
    ]
    job = _job(variant)
    return job, variant, build_editor_snapshot(job, variant)


def call(name: str, arguments: dict | None = None, call_id: str | None = None) -> ToolCall:
    return ToolCall(
        id=call_id or f"call_{name}_{uuid.uuid4().hex[:6]}", name=name, arguments=arguments
    )


def reply(text: str = "Done.", *claimed: str, **fields) -> ToolCall:
    return call(
        "reply",
        {
            "intent": "edit",
            "confidence": 0.95,
            "reply": text,
            "claimed_ops": list(claimed),
            **fields,
        },
    )


def answer(*calls: ToolCall, text: str = "", continuation: list | None = None) -> ModelInvocation:
    envelope = ToolAnswer(
        provider="openai", calls=list(calls), text=text, continuation=continuation or []
    ).envelope()
    return ModelInvocation(raw_text=envelope)


class _Recorded:
    """The provider: one recorded answer per call, in order."""

    def __init__(self, *answers: ModelInvocation) -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []

    def invoke(self, **kwargs) -> ModelInvocation:  # noqa: ANN003
        self.requests.append(kwargs)
        assert self.answers, "the turn made more model calls than recorded"
        return self.answers.pop(0)


async def _turn(monkeypatch, edit, *answers, message: str, seconds_left: float = 170.0):
    """What the Kria planner does for a chat edit on a ready video."""
    job, variant, snapshot = edit
    model = _Recorded(*answers)
    monkeypatch.setattr(copilot, "default_client", lambda: model)
    response = await copilot.run_copilot_turn(
        copilot.CopilotTurnBody(message=message, snapshot=snapshot, client_contract_version=2),
        job_id=uuid.uuid4(),
        deadline_monotonic=time.monotonic() + seconds_left,
        timeout_override_s=120.0,
        compile_check=editor_compile_check(job, variant),
    )
    return response, model


def _texts(edit, ops: list[dict]) -> list[str]:
    job, variant, _ = edit
    return [
        row["text"]
        for row in compile_editor_ops(job, copy.deepcopy(variant), ops).payload.text_elements
    ]


def _results(model: _Recorded, index: int) -> dict[str, tuple[str, bool]]:
    exchange = model.requests[index]["tool_request"].history[-1]
    return {r.call_id: (r.content, r.is_error) for r in exchange.results}


async def test_the_request_offers_open_families_only_and_keeps_rules_in_the_system_block(
    monkeypatch, edit
):
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(
            call("edit_text", {"bar_index": 0, "text": "Matcha time"}),
            reply("New hook.", "edit_text"),
        ),
        message="New hook 'Matcha time'",
    )

    request = model.requests[0]
    tools = {tool.name for tool in request["tool_request"].tools}
    assert {"edit_text", "patch_text_style", "reply"} <= tools
    assert not tools & {"swap_music", "add_sfx", "add_overlay"}, "closed families"
    assert "## Redirects for unavailable capabilities" in request["tool_request"].system
    assert (
        "Matcha time" in request["prompt"] and "Matcha time" not in request["tool_request"].system
    )
    assert response.ops == [{"op": "edit_text", "bar_index": 0, "text": "Matcha time"}]
    assert _texts(edit, response.ops) == ["Matcha time", "Beach", "Sunset"]


async def test_unknown_tool_is_named_never_applied(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(
            call("edit_text", {"bar_index": 0, "text": "Matcha time"}),
            call("make_it_pop", {"amount": 3}, "c_pop"),
            reply("New hook and more pop.", "edit_text", "make_it_pop"),
        ),
        answer(
            reply(
                "-",
                unmet_requests=[
                    {"request": "more pop", "reason": "No such control.", "repairs": "r1"}
                ],
            )
        ),
        message="New hook 'Matcha time' and make it pop",
    )

    content, is_error = _results(model, 1)["c_pop"]
    assert is_error and content.startswith("NOT applied, repair id r1.")
    assert "Operation families open on this edit" in content
    assert [op["op"] for op in response.ops] == ["edit_text"]
    assert response.reply.startswith(PREPARED)
    assert "more pop" in [item["request"] for item in response.unmet_requests]


async def test_malformed_arguments_are_repaired(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(call("edit_text", None, "c_bad"), reply("New hook.", "edit_text")),
        answer(call("edit_text", {"bar_index": 0, "text": "Matcha time"})),
        message="New hook 'Matcha time'",
    )

    content, is_error = _results(model, 1)["c_bad"]
    assert is_error and "a required field is missing" in content
    assert response.unmet_requests == []
    assert _texts(edit, response.ops) == ["Matcha time", "Beach", "Sunset"]


async def test_closed_family_call_is_refused(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(
            call("swap_music", {"track_id": "track_1"}, "c_music"),
            reply("Swapped the song.", "swap_music"),
        ),
        answer(
            reply(
                "-",
                unmet_requests=[
                    {"request": "swap the song", "reason": "Music is locked here.", "repairs": "r1"}
                ],
            )
        ),
        message="Swap the song",
    )

    assert "swap_music" not in {t.name for t in model.requests[0]["tool_request"].tools}
    assert _results(model, 1)["c_music"][1] is True
    assert response.ops == []
    assert "Swapped the song." not in response.reply


async def test_calls_apply_in_the_order_made(monkeypatch, edit):
    response, _ = await _turn(
        monkeypatch,
        edit,
        answer(
            call("remove_text", {"bar_index": 0}),
            call("edit_text", {"bar_index": 2, "text": "Golden hour"}),
            reply("Dropped the hook and renamed the last label.", "remove_text", "edit_text"),
        ),
        message="Drop the hook and call the last label 'Golden hour'",
    )

    assert [op["op"] for op in response.ops] == ["remove_text", "edit_text"]
    assert _texts(edit, response.ops) == ["Beach", "Golden hour"]


async def test_prose_answer_is_retried_as_tools(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(text="Sure, I'll change the hook to Matcha time."),
        answer(
            call("edit_text", {"bar_index": 0, "text": "Matcha time"}),
            reply("New hook.", "edit_text"),
        ),
        message="New hook 'Matcha time'",
    )

    assert len(model.requests) == 2
    assert "answer only with tool calls" in model.requests[1]["prompt"]
    assert _texts(edit, response.ops) == ["Matcha time", "Beach", "Sunset"]


async def test_operations_without_reply_wait_for_it_then_apply(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(call("edit_text", {"bar_index": 0, "text": "Matcha time"}, "c1")),
        answer(call("edit_text", {"bar_index": 2, "text": "Golden hour"}, "c2")),
        answer(reply("New hook and a golden last label.", "edit_text")),
        message="New hook 'Matcha time' and call the last label 'Golden hour'",
    )

    assert len(model.requests) == 3
    assert _results(model, 1)["c1"] == (
        "Accepted.\n\nNow call `reply` for this message: say what applied, list in "
        "unmet_requests what did not, and name in claimed_ops only operations that applied. "
        "Correct a NOT applied operation by calling its tool again first, in the same response.",
        False,
    )
    assert response.outcome == "proposed"
    assert response.reply == WHOLE_EDIT
    assert _texts(edit, response.ops) == ["Matcha time", "Beach", "Golden hour"]


async def test_no_reply_applies_nothing(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(call("edit_text", {"bar_index": 0, "text": "Matcha time"})),
        message="New hook 'Matcha time'",
        seconds_left=100.0,
    )

    assert len(model.requests) == 1, "100 s left is less than one 120 s copilot call"
    assert response.ops == []
    assert response.reply == "I couldn't finish that edit. Nothing changed."


async def test_truncated_answer_is_retried_lighter(monkeypatch, edit):
    truncated = answer(call("edit_text", {"bar_index": 0}))
    truncated.finish_status = "truncated"
    response, model = await _turn(
        monkeypatch,
        edit,
        truncated,
        answer(
            call("edit_text", {"bar_index": 0, "text": "Matcha time"}),
            reply("New hook.", "edit_text"),
        ),
        message="New hook 'Matcha time'",
    )

    assert [r["thinking_level"] for r in model.requests] == ["high", "low"]
    assert _texts(edit, response.ops) == ["Matcha time", "Beach", "Sunset"]


async def test_rejected_op_comes_back_as_a_tool_result_and_is_repaired(monkeypatch, edit):
    reasoning = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}]
    response, model = await _turn(
        monkeypatch,
        edit,
        answer(
            call("edit_text", {"bar_index": 0, "text": "Matcha time"}, "c_hook"),
            call("edit_text", {"bar_index": 5, "text": "Golden hour"}, "c_stale"),
            reply("New hook and a golden last label.", "edit_text"),
            continuation=reasoning,
        ),
        answer(call("edit_text", {"bar_index": 2, "text": "Golden hour"})),
        message="New hook 'Matcha time' and call the last label 'Golden hour'",
    )

    history = model.requests[1]["tool_request"].history
    assert history[0].answer.continuation == reasoning
    results = _results(model, 1)
    assert results["c_hook"] == ("Accepted.", False)
    stale, is_error = results["c_stale"]
    assert is_error and "Valid targets: bar_index 0-2." in stale
    assert model.requests[1]["prompt"] == model.requests[0]["prompt"], "one user turn, no re-ask"
    assert response.unmet_requests == []
    assert _texts(edit, response.ops) == ["Matcha time", "Beach", "Golden hour"]
