from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.kria.editor_sequence_timing import sequence_timing_plan
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.services.test_duration_text_preservation import sequence


@pytest.mark.parametrize(
    "message",
    [
        "Spread the text across the whole video",
        "The text are overlapping. They shouldn’t overlap "
        "and the last one should end at the end of the video",
        "Retime text to 12s",
        "Yazıları 12 saniyeye yay",
    ],
)
def test_sequence_request_is_one_atomic_existing_word_edit(monkeypatch, message):
    job, variant = sequence(monkeypatch, end_s=12.876)
    before = copy.deepcopy(variant["text_elements"])
    plan = sequence_timing_plan(message, build_editor_snapshot(job, variant))
    assert plan is not None
    assert len(plan.operations) <= 2
    assert plan.operations[-1]["op"] == "retime_text_sequence"
    compiled = compile_editor_ops(job, variant, list(plan.operations))
    assert [r["id"] for r in compiled.payload.text_elements] == [r["id"] for r in before]
    assert [r["text"] for r in compiled.payload.text_elements] == [r["text"] for r in before]
    assert compiled.payload.text_elements[-1]["end_s"] == plan.operations[-1]["end_s"]


@pytest.mark.parametrize(
    "message",
    [
        "Don't touch the text, shorten all clips to 12 seconds",
        "Don't spread the text across the whole video",
        "Spread the text across the whole video and change the font",
        "Spread the text across the whole video and shorten it to 12 seconds",
        "Make the text smaller",
        "Spread the text across the whole video and make it bold",
        "Spread the text across the whole video and make it red",
    ],
)
def test_sequence_route_does_not_take_over_other_requests(monkeypatch, message):
    job, variant = sequence(monkeypatch)
    assert sequence_timing_plan(message, build_editor_snapshot(job, variant)) is None


@pytest.mark.asyncio
async def test_real_editor_planner_bypasses_provider_for_sequence_timing(monkeypatch):
    from app.kria import planner

    job, variant = sequence(monkeypatch, end_s=12.876)
    snapshot = build_editor_snapshot(job, variant)
    monkeypatch.setattr(
        planner,
        "_load_editor_target",
        AsyncMock(return_value=planner._EditorTarget(job.id, snapshot, [])),
    )
    provider = AsyncMock(
        side_effect=AssertionError("No provider call is needed for this sequence request")
    )
    monkeypatch.setattr(planner, "run_copilot_turn", provider)
    plan = await planner._plan_editor_revision(
        SimpleNamespace(rollback=AsyncMock()),
        thread_id=job.id,
        item=SimpleNamespace(id=job.id),
        user_message="Retime text to 12s",
    )
    assert plan.mode == "act"
    assert plan.intents[0].arguments["operations"][-1]["op"] == "retime_text_sequence"
    provider.assert_not_awaited()
