"""Copilot ``set_edit_direction`` takes direction and pace from the creator (KRI-1097 wave 2).

Guard for ``COPILOT_DIRECTION_FOLLOWS_REQUEST_ENABLED``. Each case runs the real op path: a
recorded Copilot answer through the real ``EditCopilotAgent`` prompt + parser, the plan-item
route, the real ``validate_guided_snapshot`` over a stored approval, the real
``plan_direction_snapshot`` and a recorded planner answer through the real
``EditProposalAgent.parse``. The planner's return (the stamped snapshot) and the staged op are
read back. Only the guided revision token and the two model answers are stand-ins.

Failure modes written before the change:
F1  the parser still refuses text_explainer / guided_story, so the creator's direction is lost;
F2  the parser accepts it but the route still plans fast_montage;
F3  the planner gets the direction but the op re-stamps fast_montage;
F4  a stated pace never reaches the planner (it still gets "fast");
F5  no pace stated, yet the fast montage op or its planner call changes (it must not);
F6  an unknown direction or pace reaches the planner (a 500 or a vague failure);
F7  a story op reaches a client that only stages fast-montage cuts (it rejects it after a reply
    that claimed a draft was built): the parser must say, in the registry's own line, what
    this editor cannot restage, and the planner must not run;
F8  a story plan is staged from fast_cuts (None), so the turn fails as "no cuts";
F9  a story op clears the story's thoughts (minimal_text) or keeps fast-montage-only fields;
F10 with the switch off anything differs: the op, the planner call, the reply, the prompt.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agents import edit_proposal
from app.agents.edit_copilot import EditCopilotAgent
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import ContentPlan, Job, Persona, PlanItem
from app.pipeline.guided_story import canonical_media_digest
from app.routes import _copilot as copilot_route
from app.routes import generative_jobs
from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, StoryBeat
from app.services import edit_direction_planner

REVISION = {"revision_number": 3, "base_generation": "render-abc"}
MEDIA_IDS = (
    "clip-harbour",
    "clip-market",
    "clip-sunset",
    "clip-ferry",
    "clip-beach",
    "clip-taverna",
)
DIRECTION_FRAGMENT = "EDIT DIRECTION FOLLOWS THE CREATOR"


def _approved() -> EditProposalSnapshot:
    return EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the Corfu day",
        pace="balanced",
        duration_s=6,
        title="Corfu in one day",
        media=[
            MediaRef(
                lane="clip",
                media_id=media_id,
                gcs_path=f"users/test/{media_id}.mp4",
                generation=str(index + 1),
                kind="video",
                duration_s=20,
                analysis={
                    "subject": media_id.split("-")[1],
                    "description": f"A walk through the {media_id.split('-')[1]}",
                    "best_moments": [{"start_s": 2.0, "end_s": 8.0, "energy": 0.8}],
                },
            )
            for index, media_id in enumerate(MEDIA_IDS)
        ],
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{index + 1}", topic=media_id, media_ids=[media_id], duration_s=1
            )
            for index, media_id in enumerate(MEDIA_IDS)
        ],
    )


def _guided_edit(approved: EditProposalSnapshot) -> dict:
    return {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(approved.media, approved.narration),
        "approved_proposal": approved.model_dump(mode="json"),
        "media_identities": [
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
            }
            for ref in approved.media
        ],
    }


def _planner_raw(direction: str) -> str:
    """Recorded planner answers (aliases m001.. as the planner prompt shows the media)."""
    if direction == "fast_montage":
        return json.dumps(
            {
                "title": "Corfu, fast",
                "duration_s": 6,
                "fast_cuts": [
                    {
                        "cut_id": f"cut-{index + 1}",
                        "media_id": f"m{index + 1:03d}",
                        "source_start_s": 2.0,
                        "source_end_s": 3.0,
                        "output_duration_s": 1.0,
                        "role": "hook" if index == 0 else "payoff" if index == 5 else "build",
                    }
                    for index in range(6)
                ],
            }
        )
    return json.dumps(
        {
            "title": "Corfu, explained",
            "duration_s": 6,
            "story_beats": [
                {
                    "beat_id": f"beat-{index + 1}",
                    "topic": topic,
                    "thought": thought,
                    "media_ids": [f"m{index + 1:03d}"],
                    "layout": "fullscreen",
                    "duration_s": 2,
                }
                for index, (topic, thought) in enumerate(
                    (
                        ("Harbour", "Boats leave before the heat"),
                        ("Market", "Figs are cheapest at nine"),
                        ("Sunset", "The west cliffs fill up early"),
                    )
                )
            ],
        }
    )


def _copilot_raw(op: dict) -> str:
    return json.dumps(
        {
            "intent": "edit",
            "ops": [op],
            "confidence": 0.95,
            "reply": "Done.",
            "suggestions": [],
            "needs_clarification": False,
        }
    )


def _op(direction: str, *, pace: str | None = None) -> dict:
    montage = direction == "fast_montage"
    return {
        "op": "set_edit_direction",
        "direction": direction,
        **({"pace": pace} if pace else {}),
        **REVISION,
        "hard_cuts": montage,
        "minimal_text": montage,
    }


def _snapshot(*, story: bool) -> dict:
    return {
        "allowed_op_families": ["direction", "story_direction"] if story else ["direction"],
        "guided_revision": dict(REVISION),
        "slots": [
            {
                "index": index,
                "clip_index": index,
                "output_start_s": index * 4.0,
                "output_end_s": index * 4.0 + 4.0,
                "duration_s": 4.0,
                "source_in_s": 0.0,
            }
            for index in range(3)
        ],
        "text_bars": [],
        "total_duration_s": 12.0,
        "revision": 8,
    }


@pytest.fixture()
def turn(monkeypatch):
    """Submit one Copilot turn for a plan item; return the response, the stamped snapshot, the
    planner input and the rendered Copilot prompt."""
    monkeypatch.setattr(settings, "edit_copilot_enabled", True)
    monkeypatch.setattr(settings, "edit_proposal_semantic_enabled", False)
    user = MagicMock()
    user.id = uuid.uuid4()
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.idea = "Corfu day trip"
    item.theme = "Travel"
    job = MagicMock()
    job.id = uuid.uuid4()
    job.status = "variants_ready"
    job.all_candidates = {}
    job.raw_storage_path = "source.mp4"
    job.content_plan_item_id = item.id
    job.assembly_plan = {
        "variants": [{"variant_id": "v1", "render_status": "ready"}],
        "guided_edit": _guided_edit(_approved()),
        "guided_story_execution_plan": {
            "text_elements": [{"id": "guided-thought-1", "text": "AI thought"}]
        },
    }
    item.current_job_id = job.id
    item.current_job = job
    plan = MagicMock()
    plan.id = item.content_plan_id
    plan.user_id = user.id
    plan.persona_id = uuid.uuid4()
    plan.ownership_quarantined_at = None
    persona = MagicMock()
    persona.id = plan.persona_id
    persona.user_id = user.id

    async def _execute(stmt):  # noqa: ANN001
        res = MagicMock()
        entity = stmt.column_descriptions[0].get("entity")
        res.scalar_one_or_none = MagicMock(return_value=persona if entity is Persona else item)
        return res

    async def _get(model, _object_id, **_kwargs):  # noqa: ANN001
        return {ContentPlan: plan, Job: job, PlanItem: item}.get(model)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.get = AsyncMock(side_effect=_get)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(
        generative_jobs,
        "_guided_v2_revision",
        lambda _job, _variant: {
            "revision_number": REVISION["revision_number"],
            "text_elements": [{"id": "guided-thought-1", "text": "AI thought"}],
        },
    )
    monkeypatch.setattr(
        generative_jobs, "variant_render_baseline", lambda _variant: REVISION["base_generation"]
    )

    seen: dict = {"stamped": [], "planner_inputs": [], "prompts": []}

    class _RecordedPlanner:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, agent_input, ctx=None):  # noqa: ANN001, ARG002
            seen["planner_inputs"].append(agent_input)
            real = edit_proposal.EditProposalAgent(None)  # type: ignore[arg-type]
            return real.parse(_planner_raw(agent_input.direction), agent_input)

    real_plan = edit_direction_planner.plan_direction_snapshot

    def _stamping(*args, **kwargs):  # noqa: ANN002, ANN003
        stamped = real_plan(*args, **kwargs)
        seen["stamped"].append(stamped)
        return stamped

    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", _RecordedPlanner)
    monkeypatch.setattr(edit_direction_planner, "plan_direction_snapshot", _stamping)

    def submit(op: dict, *, story_client: bool = False, message: str = "change the direction"):
        raw = _copilot_raw(op)

        class _RecordedCopilot:
            def __init__(self, _client) -> None:  # noqa: ANN001
                pass

            def run(self, agent_input, *, ctx=None):  # noqa: ANN001, ARG002
                real = EditCopilotAgent(None)  # type: ignore[arg-type]
                seen["prompts"].append(real.render_prompt(agent_input))
                return real.parse(raw, agent_input)

        monkeypatch.setattr(copilot_route, "EditCopilotAgent", _RecordedCopilot)
        response = TestClient(app, raise_server_exceptions=False).post(
            f"/plan-items/{item.id}/variants/v1/copilot/turn",
            json={"message": message, "turns": [], "snapshot": _snapshot(story=story_client)},
        )
        assert response.status_code == 200, response.text
        return response.json(), seen

    yield submit
    app.dependency_overrides.clear()


def _flag(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(settings, "copilot_direction_follows_request_enabled", value)


@pytest.mark.parametrize("follows", [True, False])
def test_fast_montage_without_a_pace_is_the_same_op_either_way(turn, monkeypatch, follows) -> None:
    _flag(monkeypatch, follows)
    body, seen = turn(_op("fast_montage"), message="make it a fast paced montage")

    (op,) = body["ops"]
    assert list(op) == [
        "op",
        "direction",
        "revision_number",
        "base_generation",
        "server_planned",
        "cuts",
        "clear_text",
        "hard_cuts",
        "minimal_text",
    ]
    assert op["direction"] == "fast_montage"
    assert op["clear_text"] == [{"id": "guided-thought-1", "expected_text": "AI thought"}]
    assert len(op["cuts"]) == 6
    assert body["reply"] == (
        "I built a fast, music-led replacement draft from the strongest analyzed moments."
    )
    (stamped,) = seen["stamped"]
    assert (stamped.direction, stamped.pace) == ("fast_montage", "fast")
    assert seen["planner_inputs"][0].goal == "Show the strongest visual moments quickly."
    assert (DIRECTION_FRAGMENT in seen["prompts"][0]) is follows


def test_text_explainer_is_planned_and_staged_as_a_story(turn, monkeypatch) -> None:
    _flag(monkeypatch, True)
    body, seen = turn(
        _op("text_explainer"), story_client=True, message="turn this into a text explainer"
    )

    (stamped,) = seen["stamped"]
    assert stamped.direction == "text_explainer"
    assert stamped.pace == "balanced"  # the approved story's pace: none was stated
    assert [beat.thought for beat in stamped.story_beats] == [
        "Boats leave before the heat",
        "Figs are cheapest at nine",
        "The west cliffs fill up early",
    ]
    (op,) = body["ops"]
    assert op["direction"] == "text_explainer"
    assert "pace" not in op
    assert "cuts" not in op and "clear_text" not in op
    assert op["minimal_text"] is False and op["hard_cuts"] is False
    assert [beat["thought"] for beat in op["beats"]] == [
        beat.thought for beat in stamped.story_beats
    ]
    assert [beat["media_ids"] for beat in op["beats"]] == [[m] for m in MEDIA_IDS[:3]]
    assert body["reply"] == (
        "I built a balanced text explainer replacement draft from your analyzed footage."
    )


def test_relaxed_pace_reaches_the_planner_and_the_op(turn, monkeypatch) -> None:
    _flag(monkeypatch, True)
    body, seen = turn(
        _op("guided_story", pace="relaxed"),
        story_client=True,
        message="make it a calm story, let each moment breathe",
    )

    (stamped,) = seen["stamped"]
    assert (stamped.direction, stamped.pace) == ("guided_story", "relaxed")
    assert seen["planner_inputs"][0].pace == "relaxed"
    (op,) = body["ops"]
    assert (op["direction"], op["pace"]) == ("guided_story", "relaxed")
    assert op["hard_cuts"] is False  # a relaxed story crossfades


def test_relaxed_montage_on_the_web_client_keeps_cuts_and_records_the_pace(
    turn, monkeypatch
) -> None:
    _flag(monkeypatch, True)
    body, seen = turn(_op("fast_montage", pace="relaxed"), message="a montage, but calm")

    (stamped,) = seen["stamped"]
    assert (stamped.direction, stamped.pace) == ("fast_montage", "relaxed")
    (op,) = body["ops"]
    assert (op["direction"], op["pace"]) == ("fast_montage", "relaxed")
    assert len(op["cuts"]) == 6 and op["minimal_text"] is True
    assert "fast, music-led" not in body["reply"]


def test_story_direction_on_a_cuts_only_client_is_said_not_planned(turn, monkeypatch) -> None:
    _flag(monkeypatch, True)
    body, seen = turn(_op("text_explainer"), message="turn this into a text explainer")

    assert body["ops"] == []
    assert body["outcome"] == "unsupported"
    assert body["rejection_reasons"][0]["reason"] == "capability_unavailable"
    assert body["reply"] == (
        "The text explainer direction isn't applied on this video yet: this editor can only "
        "restage it as a fast montage, so nothing changed. A different direction needs a fresh "
        "edit."
    )
    assert seen["stamped"] == [] and seen["planner_inputs"] == []


def test_stated_pace_not_applied_is_said(turn, monkeypatch) -> None:
    _flag(monkeypatch, True)
    monkeypatch.setattr(settings, "pace_drives_cuts_enabled", False)
    body, seen = turn(_op("fast_montage", pace="relaxed"), message="a montage, but calm")

    (stamped,) = seen["stamped"]
    assert stamped.pace == "fast"
    (op,) = body["ops"]
    assert "pace" not in op
    assert body["reply"].endswith(
        "The relaxed pacing isn't applied on this video yet: it keeps the fast pace."
    )


@pytest.mark.parametrize(
    "op",
    [
        _op("text_explainer"),
        _op("fast_montage", pace="relaxed"),
        {**_op("fast_montage"), "direction": "story"},
        {**_op("fast_montage"), "pace": "slow"},
    ],
)
def test_switch_off_is_the_foundation_contract(turn, monkeypatch, op) -> None:
    _flag(monkeypatch, False)
    body, seen = turn(op, story_client=True)

    if op.get("direction") == "fast_montage" and op.get("pace") != "slow":
        (sent,) = body["ops"]
        assert "pace" not in sent and sent["direction"] == "fast_montage"
        (stamped,) = seen["stamped"]
        assert (stamped.direction, stamped.pace) == ("fast_montage", "fast")
    elif op.get("pace") == "slow":
        (sent,) = body["ops"]
        assert "pace" not in sent
    else:
        assert body["ops"] == [] and body["outcome"] == "failed"
        assert body["rejection_reasons"][0]["reason"] == "invalid_value"
        assert seen["stamped"] == []
    assert DIRECTION_FRAGMENT not in seen["prompts"][0]


@pytest.mark.parametrize(
    "op", [{**_op("fast_montage"), "direction": "story"}, {**_op("fast_montage"), "pace": "slow"}]
)
def test_unknown_direction_or_pace_never_reaches_the_planner(turn, monkeypatch, op) -> None:
    _flag(monkeypatch, True)
    body, seen = turn(op, story_client=True)

    assert body["ops"] == [] and body["outcome"] == "failed"
    assert body["rejection_reasons"][0]["reason"] == "invalid_value"
    assert seen["stamped"] == []
