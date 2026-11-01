"""Synthetic interval evidence through the real approval gate and phone compiler.

These are consumer-boundary tests. Source loading/provider replay is covered separately.
They do not establish model quality or replay the missing original Cabildo output.
"""

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import SchemaError
from app.agents.clip_question import ClipQuestionInput
from app.agents.source_moment_location import SourceMomentLocationAgent
from app.config import settings
from app.kria import planner
from app.services import prompt_moment_inspection
from app.services.named_moments import (
    Grounding,
    InspectedGroundings,
    Interval,
)
from tests.kria.test_kri723_moment_authority_m2 import POINTED, take_windows
from tests.kria.test_kri775_merope_m2_replay import (
    CREATOR_ID,
    RENDERS,
    THREAD_ID,
    gate_media,
    gate_strategy,
    m2_brief_with_moment_ask,
    planned_turn,
    prod_flags,
    run_worker,
)


def inspection(named, *, digest=None):
    return InspectedGroundings(
        digest or named.digest,
        {
            m.key: Grounding(
                "grounded",
                found=Interval(
                    *POINTED[m.words],
                    m.words,
                    "targeted_model_inspection",
                    evidence={"verification": "targeted_model_inspection", "uncertain": True},
                ),
            )
            for m in named.moments
        },
    )


async def gate(monkeypatch, *, altered=None, enabled=True, answers=True):
    prod_flags(monkeypatch)
    monkeypatch.setattr(settings, "kria_prompt_moment_inspection_enabled", enabled)
    monkeypatch.setattr(settings, "kria_prompt_moment_inspection_allow_provider", False)
    render = copy.deepcopy(RENDERS[1])
    planned = planned_turn(render)
    brief = m2_brief_with_moment_ask(render)
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    calls = []

    async def inspect(db, **kwargs):
        calls.append(kwargs)
        result = inspection(kwargs["moment_set"])
        return altered(result) if altered else result

    monkeypatch.setattr(prompt_moment_inspection, "inspect_named_moments", inspect)
    result = await planner._gate_unresolved_choices(
        SimpleNamespace(),
        planned,
        thread_id=THREAD_ID,
        creator_id=CREATOR_ID,
        item_id=THREAD_ID,
        answers_moment_questions=answers,
    )
    return result, render, brief, calls


@pytest.mark.asyncio
async def test_inspected_windows_reach_native_compiler_unchanged_in_requested_order(monkeypatch):
    result, render, brief, calls = await gate(monkeypatch)
    assert len(calls) == 1 and calls[0]["allow_provider"] is False
    assert result.plan.mode == "act"
    media, strategy = gate_media(result), gate_strategy(result)
    assert take_windows(media) == list(POINTED.values())
    assert all(
        m["evidence"]["kind"] == "targeted_model_inspection"
        for m in media["length_plan"]["moments"]
    )
    import app.pipeline.unified_montage as unified
    from app.pipeline.guided_story import compile_execution_plan

    seen = {}
    build = unified.plan_unified_montage

    def capture(*args, **kwargs):
        seen["plan"] = build(*args, **kwargs)
        return seen["plan"]

    monkeypatch.setattr(unified, "plan_unified_montage", capture)
    outcome = run_worker(monkeypatch, render, brief=brief, media=media, strategy=strategy)
    assert outcome.refusal is None
    assert not outcome.failures
    execution = compile_execution_plan(seen["plan"].guided_edit(), track=None)
    cuts = [row for row in execution["story_timeline"] if row["kind"] == "video"]
    assert [(row["source_start_s"], row["source_end_s"]) for row in cuts] == list(POINTED.values())
    tampered = copy.deepcopy(media)
    tampered["length_plan"]["windows"][0]["source_start_s"] += 0.25
    with pytest.raises(ValueError, match="length plan moment window changed"):
        run_worker(monkeypatch, render, brief=brief, media=tampered, strategy=strategy)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,answers", [(False, True), (True, False)])
async def test_dark_switch_and_old_client_do_not_inspect(monkeypatch, enabled, answers):
    _, _, _, calls = await gate(monkeypatch, enabled=enabled, answers=answers)
    assert not calls


@pytest.mark.asyncio
async def test_stale_request_evidence_cannot_unlock_approval(monkeypatch):
    result, _, _, _ = await gate(
        monkeypatch, altered=lambda result: replace(result, moment_set_digest="stale")
    )
    assert result.plan.mode == "respond" and result.plan.moment_order_question


@pytest.mark.asyncio
async def test_conflicting_actions_on_same_window_require_clarification(monkeypatch):
    def collide(result):
        rows = dict(result.groundings)
        keys = list(rows)
        rows[keys[1]] = rows[keys[0]]
        return replace(result, groundings=rows)

    result, _, _, _ = await gate(monkeypatch, altered=collide)
    assert result.plan.mode == "respond" and result.plan.moment_order_question


def test_localizer_rejects_clip_level_yes_and_invalid_source_times():
    agent = SourceMomentLocationAgent(None)
    request = ClipQuestionInput(file_uri="fixture://sample", question="inside the Cabildo")
    for raw in [
        '{"answer":"yes","confidence":1,"evidence":"building"}',
        '{"matches":[{"start_s":2,"end_s":1,"confidence":1,"evidence":"inside"}]}',
        '{"matches":[{"start_s":0,"end_s":2,"confidence":NaN,"evidence":"inside"}]}',
    ]:
        with pytest.raises(SchemaError):
            agent.parse(raw, request)
    assert not agent.parse('{"status":"absent","matches":[]}', request).matches


@pytest.mark.asyncio
async def test_partially_overlapping_located_actions_require_clarification(monkeypatch):
    def overlap(result):
        rows = dict(result.groundings)
        first, second = list(rows)[:2]
        span = rows[first].found
        rows[second] = Grounding("grounded", found=replace(span, start_s=span.start_s + 0.5))
        return replace(result, groundings=rows)

    result, _, _, _ = await gate(monkeypatch, altered=overlap)
    assert result.plan.mode == "respond" and result.plan.moment_order_question
