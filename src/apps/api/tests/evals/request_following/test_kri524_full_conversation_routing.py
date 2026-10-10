"""KRI-524 full-turn routing regressions with offline model responses.

The planner, narrow extractor parser, editor parser, compiler, and draft
projection are real. Scope and faster responses are authored; the initial word
response is a redacted recorded capture with an authored snapshot. Database and
context access are stubbed. No provider, save, or render call is made here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelClient
from app.agents._schemas.creator_agent import AskUser
from app.agents.brief_extractor import BriefExtractorAgent
from app.agents.edit_copilot import EditCopilotAgent
from app.kria import planner
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import compile_editor_ops, project_editor_draft
from tests._prod_profile import apply_prod_profile
from tests.evals.runners.snapshot_variant import build_synthetic_job, build_synthetic_variant
from tests.kria.test_creative_brief import _ASK, _wire_planner

_SEQUENCE_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures/prompt_coverage/postdeploy_title_sequence.json"
)


def _capture() -> dict:
    return json.loads(_SEQUENCE_FIXTURE.read_text())


def _extractor_response(scope: str, *, clarification: str | None = None) -> dict:
    if scope == "clarify" and clarification is None:
        clarification = "Which title words should I use?"
    response = {"brief_updates": [], "request_scope": scope}
    if clarification is not None:
        response["clarification"] = clarification
    return response


def _editor_response(capture: dict) -> dict:
    return copy.deepcopy(capture["responses"]["pro"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Try again to Split the title text into words. Then animate each word by word",
        "Please split the title into individual words and animate them sequentially",
    ],
)
async def test_scoped_retry_routes_to_editor_and_compiles_sequence(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    capture = _capture()
    snapshot = capture["input"]["variant_snapshot"]
    variant = build_synthetic_variant(snapshot)
    job = build_synthetic_job(variant)
    actual_editor = planner._plan_editor_revision
    output = SimpleNamespace(action=AskUser(**_ASK), brief_updates=[])
    db, item, creator_id, _copilot, creator_runs = _wire_planner(
        monkeypatch, output=output, editor_plan=None, snapshot=snapshot
    )
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(planner, "_plan_editor_revision", actual_editor)
    monkeypatch.setattr(planner, "load_intent_clips_for_item", AsyncMock(return_value=[]))

    async def extract(inputs, *, creator_request, **_kwargs):  # noqa: ANN001
        agent = BriefExtractorAgent(ModelClient())
        return agent.parse(
            json.dumps(_extractor_response("edit")),
            agent.Input(
                creator_request=creator_request,
                user_message=inputs.agent_input.user_message,
                conversation=inputs.agent_input.conversation,
                current_brief=getattr(inputs.agent_input, "current_brief", None),
                require_request_scope=True,
            ),
        )

    async def editor(body, **_kwargs):  # noqa: ANN001
        agent = EditCopilotAgent(ModelClient())
        return agent.parse(
            json.dumps(_editor_response(capture)),
            agent.Input(
                utterance=body.message,
                variant_snapshot=body.snapshot,
                prior_turns=body.turns,
                original_request=body.original_request,
            ),
        )

    monkeypatch.setattr(planner, "_call_brief_extractor", extract)
    monkeypatch.setattr(planner, "run_copilot_turn", editor)
    monkeypatch.setattr(gj.storage, "object_exists", lambda *_a, **_kw: True)
    result = await planner.plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message=message,
    )

    assert result.brief_route == "editor_ops"
    assert not creator_runs, "a scoped edit must not invoke the full creator or render"
    assert result.plan.mode == "act"
    assert result.plan.intents and result.plan.intents[0].tool_name == "draft.apply_editor_ops"
    operations = result.plan.intents[0].arguments["operations"]
    compiled = compile_editor_ops(job, variant, operations).payload
    rows = list(compiled.text_elements)
    assert [row["text"] for row in rows[:13]] == capture["expected"]["segments"]
    assert all(
        row["animation_phases"]["entrance"] == "fade" and row["animation_phases"]["exit"] == "fade"
        for row in rows[:13]
    )
    assert all(left["end_s"] <= right["start_s"] for left, right in zip(rows[:13], rows[1:13]))
    projected = project_editor_draft(variant, compiled.model_dump(mode="json"))
    assert projected["mix"] == snapshot["mix"]["music_level"]
    assert projected["music_track_id"] is None
    for before, after in zip(snapshot["slots"], projected["ai_timeline"]["slots"], strict=True):
        assert {key: after.get(key) for key in before} == before
    assert projected["text_elements"][-2]["text"] == "Name"
    assert projected["text_elements"][-1]["text"] == "Name"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_target", [False, True])
async def test_rebuild_scope_stays_on_full_replan_and_clarify_has_no_actions(
    monkeypatch: pytest.MonkeyPatch,
    has_target: bool,
) -> None:
    capture = _capture()
    snapshot = capture["input"]["variant_snapshot"]
    actual_editor = planner._plan_editor_revision
    output = SimpleNamespace(action=AskUser(**_ASK), brief_updates=[])
    db, item, creator_id, copilot, creator_runs = _wire_planner(
        monkeypatch, output=output, editor_plan=None, snapshot=snapshot if has_target else None
    )
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(planner, "_plan_editor_revision", actual_editor)
    monkeypatch.setattr(planner, "load_intent_clips_for_item", AsyncMock(return_value=[]))
    requested_scope = {"value": "rebuild"}

    async def extract(inputs, *, creator_request, **_kwargs):  # noqa: ANN001
        agent = BriefExtractorAgent(ModelClient())
        return agent.parse(
            json.dumps(_extractor_response(requested_scope["value"])),
            agent.Input(
                creator_request=creator_request,
                user_message=inputs.agent_input.user_message,
                conversation=inputs.agent_input.conversation,
                current_brief=getattr(inputs.agent_input, "current_brief", None),
                require_request_scope=True,
            ),
        )

    monkeypatch.setattr(planner, "_call_brief_extractor", extract)
    monkeypatch.setattr(gj.storage, "object_exists", lambda *_a, **_kw: True)
    rebuild = await planner.plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message="Rebuild the whole edit from the request.",
    )
    assert rebuild.brief_route == "replan"
    assert not copilot.await_args_list
    assert creator_runs
    assert rebuild.plan.mode in {"act", "respond"}

    requested_scope["value"] = "clarify"
    creator_runs.clear()
    copilot.reset_mock()
    clarify = await planner.plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message="I mean the title, but which words should it use?",
    )
    assert clarify.brief_route is None
    assert clarify.plan.mode == "respond"
    assert not clarify.plan.intents
    assert not clarify.brief_updates
    assert not creator_runs
    copilot.assert_not_awaited()


@pytest.mark.asyncio
async def test_faster_followup_routes_through_editor_against_resulting_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second scoped turn retimes generated children without losing fades."""
    capture = _capture()
    snapshot = capture["input"]["variant_snapshot"]
    actual_editor = planner._plan_editor_revision
    output = SimpleNamespace(action=AskUser(**_ASK), brief_updates=[])
    db, item, creator_id, _copilot, creator_runs = _wire_planner(
        monkeypatch, output=output, editor_plan=None, snapshot=snapshot
    )
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(planner, "_plan_editor_revision", actual_editor)
    monkeypatch.setattr(planner, "load_intent_clips_for_item", AsyncMock(return_value=[]))
    target_snapshot = {"value": snapshot}
    target = SimpleNamespace(job_id=item.current_job_id, snapshot=snapshot, conversation=[])

    async def load_target(_db, *, thread_id, item, **_kwargs):  # noqa: ANN001
        target.snapshot = target_snapshot["value"]
        return target

    async def extract(inputs, *, creator_request, **_kwargs):  # noqa: ANN001
        agent = BriefExtractorAgent(ModelClient())
        return agent.parse(
            json.dumps(_extractor_response("edit")),
            agent.Input(
                creator_request=creator_request,
                user_message=inputs.agent_input.user_message,
                conversation=inputs.agent_input.conversation,
                current_brief=getattr(inputs.agent_input, "current_brief", None),
                require_request_scope=True,
            ),
        )

    calls = {"count": 0}

    async def editor(body, **_kwargs):  # noqa: ANN001
        if calls["count"] == 0:
            response = _editor_response(capture)
        else:
            response = {
                "intent": "edit",
                "ops": [
                    {
                        "op": "set_text_timing",
                        "bar_index": index,
                        "start_s": index * 0.084615,
                        "end_s": (index + 1) * 0.084615,
                    }
                    for index in range(13)
                ],
                "confidence": 1.0,
                "reply": "I made the word sequence faster while keeping the fades.",
            }
        calls["count"] += 1
        agent = EditCopilotAgent(ModelClient())
        return agent.parse(
            json.dumps(response),
            agent.Input(
                utterance=body.message,
                variant_snapshot=body.snapshot,
                prior_turns=body.turns,
                original_request=body.original_request,
            ),
        )

    monkeypatch.setattr(planner, "_load_editor_target", load_target)
    monkeypatch.setattr(planner, "_call_brief_extractor", extract)
    monkeypatch.setattr(planner, "run_copilot_turn", editor)
    monkeypatch.setattr(gj.storage, "object_exists", lambda *_a, **_kw: True)
    first_result = await planner.plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message=capture["input"]["utterance"],
    )
    assert first_result.brief_route == "editor_ops"
    assert not creator_runs
    first_variant = build_synthetic_variant(snapshot)
    first_payload = compile_editor_ops(
        build_synthetic_job(first_variant),
        first_variant,
        first_result.plan.intents[0].arguments["operations"],
    ).payload
    projected = project_editor_draft(first_variant, first_payload.model_dump(mode="json"))
    followup_snapshot = copy.deepcopy(snapshot)
    followup_snapshot["text_bars"] = projected["text_elements"]
    target_snapshot["value"] = followup_snapshot

    followup_result = await planner.plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message="Make the word sequence twice as fast. Keep the fades.",
    )
    assert followup_result.brief_route == "editor_ops"
    assert calls["count"] == 2
    followup_variant = build_synthetic_variant(followup_snapshot)
    payload = compile_editor_ops(
        build_synthetic_job(followup_variant),
        followup_variant,
        followup_result.plan.intents[0].arguments["operations"],
    ).payload
    rows = list(payload.text_elements)
    assert [(row["start_s"], row["end_s"]) for row in rows[:13]] == [
        (index * 0.084615, (index + 1) * 0.084615) for index in range(13)
    ]
    assert all(
        row["animation_phases"]["entrance"] == "fade" and row["animation_phases"]["exit"] == "fade"
        for row in rows[:13]
    )
    assert [row["text"] for row in rows[:13]] == capture["expected"]["segments"]
    assert [row["text"] for row in rows[13:]] == ["Name", "Name"]
