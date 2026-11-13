"""KRI-1386 + KRI-1387: a chat edit applies what it can, repairs what it can't, names the rest.

Each case replays recorded model answers through the real copilot prompt and parser, the per-op
editor compile the Kria planner binds to the draft (``editor_compile_check``), the shared turn
route (``run_copilot_turn``), and then the runtime's own ``compile_editor_ops`` on the ops the
turn returns. Only the provider transport is scripted.

Failure modes, written before the code:
F1  a style applies without its timing pair, and the reply says both happened
                                                    -> test_top_left_mistake_is_repaired...
F2  a bulk op is split (half of "every image" applies)
F3  a patch_text_appearance sibling applies without the appearance change
                                                    -> test_atomic_bundle_never_applies_in_part
F4  the reply claims a rejected op                  -> test_three_asks_one_unfixable...
F5  dropping a sibling shifts what a later index points at
                                                    -> test_dropping_a_sibling_never_shifts...
F6  a repair call starts with too little turn time left and the turn times out
                                                    -> test_no_repair_without_time_for_a_call
F7  a repair answer silently drops a rejected op    -> test_stale_index_the_repair_ignores...
F8  a failed repair call fails the whole turn       -> test_failed_repair_call_keeps...
F9  an unsure repair answer throws the kept ops away
                                                    -> test_unsure_repair_answer_keeps...
F10 an op the parser accepts but the compiler refuses sinks every sibling at draft time
                                                    -> test_compile_refusal_reaches_the_model...
F11 a text-timing conflict (a creator decision) is turned into a refusal
                                                    -> test_text_timing_conflict_passes_through
"""

from __future__ import annotations

import copy
import json
import time
import uuid

import pytest

from app.agents._runtime import ModelInvocation, TerminalError
from app.routes import _copilot as copilot
from app.services import kria_editor_ops as ops_mod
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    editor_compile_check,
    validate_ops_per_op,
)
from tests.services.test_duration_text_preservation import retime, sequence
from tests.services.test_kria_editor_ops import _job, _variant

TEXTS = ("Hook", "Beach", "Sunset")
CAPABILITIES = {
    "text_elements": True,
    "timeline": True,
    "clips": {"transitions": {"editable": True}},
    "music_operations": {"remove": True},
    "mix": True,
}
HOOK = {"op": "edit_text", "bar_index": 0, "text": "Matcha time"}
RED_BEACH = {"op": "patch_text_style", "bar_index": 1, "patch": {"color": "#FF3B30"}}
PAPYRUS_SUNSET = {"op": "patch_text_style", "bar_index": 2, "patch": {"font_family": "Papyrus"}}
PREPARED = "I prepared the supported changes."
# What a fully valid proposal shows (the phone planner swaps it for its staged line).
WHOLE_EDIT = "I prepared this edit for the editor to validate and stage."


@pytest.fixture
def edit(monkeypatch):
    monkeypatch.setattr(ops_mod, "_editor_capabilities", lambda _job, _variant: CAPABILITIES)
    variant = _variant()
    bar = variant["text_elements"][0]
    variant["text_elements"] = [
        {**bar, "id": f"text-{i}", "text": text, "start_s": float(i), "end_s": float(i) + 1.0}
        for i, text in enumerate(TEXTS)
    ]
    job = _job(variant)
    return job, variant, build_editor_snapshot(job, variant)


class _Recorded:
    """The provider: one recorded answer per call, in order. An exception answer is raised."""

    def __init__(self, *answers: dict | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def invoke(self, **kwargs) -> ModelInvocation:  # noqa: ANN003
        self.prompts.append(kwargs["prompt"])
        assert self.answers, "the turn made more model calls than recorded"
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ModelInvocation(raw_text=json.dumps(answer))


def _answer(*ops: dict, reply: str = "Done.", unmet: list[dict] | None = None, **extra) -> dict:
    return {
        "intent": "edit",
        "confidence": 0.95,
        "reply": reply,
        "ops": list(ops),
        "claimed_ops": [op["op"] for op in ops],
        "unmet_requests": unmet or [],
        "needs_clarification": False,
        "suggestions": [],
        **extra,
    }


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


def _applied(edit, ops: list[dict]) -> list[dict]:
    """The draft the runtime compiles from the turn's ops: one row per text."""
    job, variant, _ = edit
    return compile_editor_ops(job, copy.deepcopy(variant), ops).payload.text_elements


async def test_three_asks_one_unfixable_two_apply_and_the_third_is_named(monkeypatch, edit):
    reply = "New hook, a red beach label, and Papyrus on the sunset."
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, RED_BEACH, PAPYRUS_SUNSET, reply=reply),
        _answer(
            unmet=[
                {
                    "request": "Papyrus on the sunset label",
                    "reason": "Papyrus isn't one of the fonts this edit can use.",
                    "repairs": "r1",
                }
            ]
        ),
        message="New hook 'Matcha time', make the beach label red and the sunset one Papyrus",
    )

    assert len(model.prompts) == 2
    assert "## REPAIR: OPERATIONS THAT WERE NOT APPLIED" not in model.prompts[0]
    repair_block = model.prompts[1].split("## REPAIR: OPERATIONS THAT WERE NOT APPLIED")[1]
    assert '- r1 `patch_text_style` {"bar_index": 2' in repair_block
    assert "Papyrus" in repair_block
    assert "Matcha time" not in repair_block, "accepted ops are not re-asked"

    assert response.outcome == "proposed"
    assert response.ops == [HOOK, RED_BEACH]
    assert response.reply.startswith(PREPARED)
    assert "Papyrus isn't one of the fonts this edit can use." in response.reply
    assert [item["request"] for item in response.unmet_requests] == ["Papyrus on the sunset label"]
    texts = _applied(edit, response.ops)
    assert [row["text"] for row in texts] == ["Matcha time", "Beach", "Sunset"]
    assert texts[1]["color"] == "#FF3B30"
    assert texts[2]["font_family"] == "Playfair Display"


async def test_top_left_mistake_is_repaired_and_all_three_apply(monkeypatch, edit):
    # "Put the beach label top left for the first 3 seconds": the style and its timing pair.
    top_left = {"op": "patch_text_style", "bar_index": 1, "patch": {"alignment": "top_left"}}
    timing = {"op": "set_text_timing", "bar_index": 1, "start_s": 0.0, "end_s": 3.0}
    reply = "New hook, and the beach label sits top left for the first 3 seconds."
    fixed = {
        "op": "patch_text_style",
        "bar_index": 1,
        "patch": {"position": "custom", "x_frac": 0.08, "y_frac": 0.12, "alignment": "left"},
    }
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, top_left, timing, reply=reply),
        _answer({**fixed, "repairs": "r1"}),
        message="New hook 'Matcha time' and put the beach label top left for the first 3 seconds",
    )

    assert len(model.prompts) == 2
    assert "alignment left|center|right" in model.prompts[1]
    assert response.outcome == "proposed"
    assert response.unmet_requests == []
    assert response.reply == WHOLE_EDIT, "the same reply a first-time valid edit gets"
    assert sorted(op["op"] for op in response.ops) == [
        "edit_text",
        "patch_text_style",
        "set_text_timing",
    ]
    beach = _applied(edit, response.ops)[1]
    assert (beach["alignment"], beach["x_frac"], beach["y_frac"]) == ("left", 0.08, 0.12)
    assert (beach["start_s"], beach["end_s"]) == (0.0, 3.0)


async def test_stale_index_is_repaired_with_the_valid_range(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, {"op": "edit_text", "bar_index": 5, "text": "Golden hour"}),
        _answer({"op": "edit_text", "bar_index": 2, "text": "Golden hour", "repairs": "r1"}),
        message="New hook 'Matcha time' and call the last label 'Golden hour'",
    )

    assert "Valid targets: bar_index 0-2." in model.prompts[1]
    assert response.unmet_requests == []
    texts = _applied(edit, response.ops)
    assert [row["text"] for row in texts] == ["Matcha time", "Beach", "Golden hour"]


async def test_stale_index_the_repair_ignores_is_named_after_two_rounds(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, {"op": "edit_text", "bar_index": 5, "text": "Golden hour"}),
        _answer(),
        _answer(),
        message="New hook 'Matcha time' and call the sixth label 'Golden hour'",
    )

    assert len(model.prompts) == 3, "two repair rounds, never a third"
    assert response.ops == [HOOK]
    assert response.reply.startswith(PREPARED)
    assert response.unmet_requests == [
        {"request": "change text 6", "reason": "that part isn't in this edit"}
    ]
    assert response.reply == f"{PREPARED} I couldn't change text 6: that part isn't in this edit."


async def test_dropping_a_sibling_never_shifts_a_later_index(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(
            {"op": "remove_text", "bar_index": 0},
            {"op": "patch_text_style", "bar_index": 1, "patch": {"font_family": "Papyrus"}},
            {"op": "edit_text", "bar_index": 2, "text": "Golden hour"},
        ),
        message="Drop the hook, Papyrus on the beach label, last label 'Golden hour'",
        seconds_left=60.0,
    )

    assert len(model.prompts) == 1
    assert [op["op"] for op in response.ops] == ["remove_text", "edit_text"]
    assert [row["text"] for row in _applied(edit, response.ops)] == ["Beach", "Golden hour"]
    (named,) = response.unmet_requests
    assert named["request"] == "change text 2"
    assert "Papyrus" in named["reason"]
    assert "I couldn't change text 2:" in response.reply


async def test_no_repair_without_time_for_a_call(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, RED_BEACH, PAPYRUS_SUNSET, reply="All three done."),
        message="New hook 'Matcha time', red beach label, Papyrus sunset",
        seconds_left=100.0,
    )

    assert len(model.prompts) == 1, "100 s left is less than one 120 s copilot call"
    assert response.ops == [HOOK, RED_BEACH]
    assert "All three done." not in response.reply
    assert [item["request"] for item in response.unmet_requests] == ["change text 3"]


async def test_failed_repair_call_keeps_the_partial_edit(monkeypatch, edit):
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, RED_BEACH, PAPYRUS_SUNSET, reply="All three done."),
        TerminalError("provider down"),
        message="New hook 'Matcha time', red beach label, Papyrus sunset",
    )

    assert len(model.prompts) == 2
    assert response.outcome == "proposed"
    assert response.ops == [HOOK, RED_BEACH]
    assert [item["request"] for item in response.unmet_requests] == ["change text 3"]


async def test_unsure_repair_answer_keeps_the_kept_ops(monkeypatch, edit):
    stale = {"op": "edit_text", "bar_index": 9, "text": "Golden hour"}
    response, _ = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, stale, reply="New hook and a golden last label."),
        _answer(
            {"op": "edit_text", "bar_index": 2, "text": "Golden hour", "repairs": "r1"},
            confidence=0.2,
            needs_clarification=True,
            intent="clarify",
        ),
        message="New hook 'Matcha time' and the last label 'Golden hour'",
    )

    assert response.needs_clarification is False
    assert response.outcome == "proposed"
    assert response.reply == WHOLE_EDIT
    assert [row["text"] for row in _applied(edit, response.ops)] == [
        "Matcha time",
        "Beach",
        "Golden hour",
    ]


async def test_compile_refusal_reaches_the_model_and_is_repaired(monkeypatch, edit):
    outside = {"op": "split_clip", "slot_index": 1, "at_s": 0.05}
    inside = {"op": "split_clip", "slot_index": 1, "at_s": 1.0}
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, outside),
        _answer({**inside, "repairs": "r1"}),
        message="New hook 'Matcha time' and split the second clip",
    )

    assert (
        "Not applied (the editor refused it): The split point is outside the clip"
        in (model.prompts[1])
    )
    assert response.ops == [HOOK, inside]
    job, variant, _ = edit
    assert (
        len(compile_editor_ops(job, copy.deepcopy(variant), response.ops).payload.timeline_slots)
        == 3
    )


@pytest.mark.parametrize(
    "atomic",
    [
        # "every unused image" with a selector that cannot mean that.
        {
            "op": "add_unused_sources",
            "selector": {"scope": "timeline", "media_kind": "image", "quantifier": "all"},
        },
        # This snapshot has no text-appearance inventory, so the appearance change fails.
        {
            "op": "patch_text_appearance",
            "selector": {"category": "text"},
            "patch": {"font_family": "Inter"},
            "text_appearance_version": 1,
        },
    ],
    ids=["bulk", "text_appearance"],
)
async def test_atomic_bundle_never_applies_in_part(monkeypatch, edit, atomic):
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, atomic, reply="New hook and the rest."),
        message="New hook 'Matcha time' and the rest",
        seconds_left=60.0,
    )

    assert len(model.prompts) == 1
    assert response.ops == []
    assert response.outcome == "failed"
    assert "New hook and the rest." not in response.reply


async def test_atomic_bundle_goes_back_whole_for_repair(monkeypatch, edit):
    appearance = {
        "op": "patch_text_appearance",
        "selector": {"category": "text"},
        "patch": {"font_family": "Inter"},
        "text_appearance_version": 1,
    }
    response, model = await _turn(
        monkeypatch,
        edit,
        _answer(HOOK, appearance),
        _answer(
            {**HOOK, "repairs": "r2"},
            unmet=[{"request": "Inter everywhere", "reason": "Not on this edit.", "repairs": "r1"}],
        ),
        message="New hook 'Matcha time' and Inter on every text",
    )

    repair_block = model.prompts[1].split("## REPAIR: OPERATIONS THAT WERE NOT APPLIED")[1]
    assert "r1 `patch_text_appearance`" in repair_block
    assert "r2 `edit_text`" in repair_block and "held back" in repair_block
    assert response.ops == [HOOK]
    assert response.reply.startswith(PREPARED)


def test_text_timing_conflict_passes_through(monkeypatch):
    job, variant = sequence(monkeypatch)
    unreadable = retime(variant["text_elements"], 0.5)

    assert validate_ops_per_op(job, variant, [unreadable]) == ([unreadable], [])
