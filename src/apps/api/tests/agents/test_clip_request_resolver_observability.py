"""The clip-request resolver's saved `agent_run` input keeps what diagnoses a missed match and not
the creator's whole request.

Context (PR #1973, KRI-1076): the chat planner now gives the clip-intent agents a plan item, so
`persist_agent_run` saves the resolver's runs. Its saved `input_json` carried `creator_request` (the
creator's entire message) verbatim, while the planner, which receives the same text, saves a
`{"redacted", "chars", "sha256"}` marker (`ClipIntentPlannerAgent.project_input_for_observability`).
Probe on main 4159406a1 and on #1973 d6c04ad22: the chat path saves nothing on main; the
creator-agent route (`routes/creator_agent.py`) already saved the same unredacted resolver input
for session-owned runs.

What a missed match needs is the intents asked (their attribute) and the clips' records they were
matched against, plus the answer. The whole request is not needed for that.

Ways this can fail (each pinned below):
 R1 the saved input still contains the creator's request text.
 R2 the diagnostics are lost with it: the intents or a clip's description disappear, so
    "why did the resolver miss the seat photo" cannot be answered.
 R3 the marker cannot correlate runs (no length, no digest), or differs from the planner's.
 R4 the projection changes what the model sees: the prompt, or the validated input, differs.
 R5 the runtime does not use the projection when it saves the row.
 R6 an empty or missing request breaks the projection.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from app.agents import _persistence
from app.agents._runtime import ModelInvocation, RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ResolverClipIn,
    ResolverIntentIn,
)

REQUEST = (
    "Montage of my night: Taha invited me to his VIP box. Skip the photo of the empty train "
    'seat. Title: "Pokalnacht in Leverkusen".'
)
SEAT = "Train seat and window view"


def _input(request: str = REQUEST) -> ClipRequestResolverInput:
    return ClipRequestResolverInput(
        creator_request=request,
        intents=[
            ResolverIntentIn(
                intent_id="i_seat", op="include", attribute="the photo of the empty train seat"
            )
        ],
        clips=[
            ResolverClipIn(alias="m001", kind="image", record={"subject": SEAT}),
            ResolverClipIn(alias="m002", kind="video", record={"subject": "crowd singing"}),
        ],
    )


def _agent() -> ClipRequestResolverAgent:
    return ClipRequestResolverAgent(None)  # type: ignore[arg-type]


def _projected(request: str = REQUEST) -> dict[str, Any]:
    dumped = _input(request).model_dump()
    projected = _agent().project_input_for_observability(dumped)
    assert projected is not None
    return projected


def test_r1_the_saved_input_does_not_hold_the_creators_request() -> None:
    saved = json.dumps(_projected(), ensure_ascii=False)
    for fragment in (REQUEST, "Taha", "VIP box", "Pokalnacht"):
        assert fragment not in saved


def test_r2_the_intents_and_the_clip_records_stay_readable() -> None:
    projected = _projected()
    assert [i["attribute"] for i in projected["intents"]] == ["the photo of the empty train seat"]
    assert [c["record"]["subject"] for c in projected["clips"]] == [SEAT, "crowd singing"]
    assert SEAT in json.dumps(projected)


def test_r3_the_marker_is_the_planners_marker() -> None:
    marker = _projected()["creator_request"]
    assert marker == {
        "redacted": True,
        "chars": len(REQUEST),
        "sha256": hashlib.sha256(REQUEST.encode()).hexdigest(),
    }
    planner_marker = ClipIntentPlannerAgent(None).project_input_for_observability(  # type: ignore[arg-type]
        {"creator_request": REQUEST, "candidate_intents": [], "confirmed_titles": []}
    )["creator_request"]
    assert marker == planner_marker


def test_r4_the_model_still_sees_the_request() -> None:
    agent = _agent()
    before = agent.render_prompt(_input())
    agent.project_input_for_observability(_input().model_dump())
    assert agent.render_prompt(_input()) == before
    assert _input().creator_request == REQUEST


@pytest.mark.parametrize("request_text", ["", "   "])
def test_r6_an_empty_request_is_a_marker_not_a_failure(request_text: str) -> None:
    marker = _projected(request_text)["creator_request"]
    assert marker["redacted"] is True and marker["chars"] == len(request_text)


def test_r6_a_missing_input_projects_to_nothing() -> None:
    assert _agent().project_input_for_observability(None) is None


class _Provider:
    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        empty = {"intent_id": "i_seat", "assignments": [], "needs_vision": []}
        return ModelInvocation(raw_text=json.dumps({"intents": [empty]}))


def test_r5_the_row_the_runtime_saves_uses_the_projection(
    prod_profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(_persistence, "persist_agent_run", lambda **kw: saved.append(kw))
    ClipRequestResolverAgent(_Provider()).run(  # type: ignore[arg-type]
        _input(), ctx=RunContext(plan_item_id="00000000-0000-0000-0000-000000000001")
    )
    [row] = saved
    blob = json.dumps(row["input_dict"], ensure_ascii=False)
    assert "Taha" not in blob and REQUEST not in blob
    assert row["input_dict"]["creator_request"]["redacted"] is True
    assert SEAT in blob  # the diagnostic the PR exists for survives
    assert row["output_dict"]["intents"][0]["assignments"] == []
