"""Synthetic contract replay only; these responses do not measure model relevance."""

import json

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError
from app.agents.camera_roll_stories import CameraRollStoriesAgent
from app.agents.media_moment_relevance import (
    MomentCandidate,
    MomentRelevanceAgent,
    MomentRelevanceInput,
)

from .runners.eval_runner import CassetteModelClient, build_eval_run_context


def evidence():
    return MomentRelevanceInput(
        query="Find the illuminated house at the end of the take",
        candidates=[
            MomentCandidate(
                id="11111111-1111-1111-1111-111111111111",
                source="22222222-2222-2222-2222-222222222222",
                kind="visual",
                text="An illuminated house beside the ferry",
                quality_notes="Dark evening footage; the house is the subject",
                start_s=170,
                end_s=177,
            )
        ],
    )


@pytest.mark.parametrize(
    "agent_cls,raw",
    [
        (MomentRelevanceAgent, '{"moment_ids": []}'),
        (CameraRollStoriesAgent, '{"stories": []}'),
    ],
)
def test_no_match_is_valid_with_one_bounded_replay_call(agent_cls, raw):
    client = CassetteModelClient(raw)
    agent = agent_cls(client)
    result = agent.run(
        evidence(), ctx=build_eval_run_context("camera-roll/no-match", is_live=False)
    )
    assert result.model_dump() in ({"moment_ids": []}, {"stories": []})
    assert client.invocations == 1
    assert agent.spec.max_attempts == 1
    assert agent.spec.sensitive_io
    assert agent.spec.timeout_s <= 20


def test_relevance_replay_cannot_invent_or_duplicate_source_intervals():
    value = evidence()
    known = value.candidates[0].id
    client = CassetteModelClient(json.dumps({"moment_ids": ["not-owned", known, known]}))
    agent = MomentRelevanceAgent(client)
    output = agent.run(value, ctx=build_eval_run_context("camera-roll/source-ids", is_live=False))
    assert output.moment_ids == [known]
    assert value.candidates[0].start_s == 170 and value.candidates[0].end_s == 177
    prompt = agent.render_prompt(value)
    assert "Dark evening" in prompt and "fallible" in prompt
    assert client.invocations == 1


def test_story_replay_keeps_review_copy_out_of_the_execution_contract():
    value = evidence()
    story = {
        "title": "Arrival after dark",
        "hook": "The ferry crossing",
        "payoff": "The lit house",
        "rationale": "Recorded arrival",
        "moment_ids": [value.candidates[0].id],
    }
    client = CassetteModelClient(json.dumps({"stories": [story]}))
    agent = CameraRollStoriesAgent(client)
    output = agent.run(value, ctx=build_eval_run_context("camera-roll/late-arrival", is_live=False))
    assert output.stories[0].moment_ids == [value.candidates[0].id]
    assert "on_screen_copy" not in output.model_dump()
    assert "none is on-screen" in agent.render_prompt(value)
    assert client.invocations == 1
    with pytest.raises(SchemaError):
        agent.parse(json.dumps({"stories": [story] * 4}), value)


def test_candidate_budget_cannot_grow_with_collection_size():
    value = evidence()
    with pytest.raises(ValidationError):
        MomentRelevanceInput(query=value.query, candidates=value.candidates * 101)
