"""Copilot ``set_edit_direction`` follows the creator's direction and pace, or says why it can't
(KRI-1097 wave 2).

Guard for ``COPILOT_DIRECTION_FOLLOWS_REQUEST_ENABLED``. Each turn runs the real op path: a
recorded Copilot answer through the real ``EditCopilotAgent`` prompt + parser, the plan-item
route, the real ``validate_guided_snapshot`` over a stored approval, the real
``plan_direction_snapshot`` and a recorded planner answer through the real
``EditProposalAgent.parse`` (or the planner's own fallback after a ``TerminalError``). The stamped
snapshot and the staged op are read back. Only the guided revision token and the two model
answers are stand-ins.

The editor drawer stages one direction: a fast montage. Its cuts do not follow a stated pace
(the planner's 1.2 s fast-cut cap, ``deterministic_fast_cuts`` takes no pace, the web validator
accepts 0.4 to 1.2 s cuts). No client stages story beats.

Failure modes written before the change:
F1  a text_explainer / guided_story ask is lost as a bare invalid_value, or restaged as a fast
    montage: the parser must decline it with the registry's direction line, before any planner
    call;
F2  a stated balanced / relaxed pace is claimed in the reply or put on the op while the cuts stay
    fast (the recorded planner and the deterministic fallback alike): the op must be the plain
    montage op and the reply must name the pace with the registry's pacing line;
F3  a stated "fast" pace (what the montage does) gets a "not applied" line;
F4  no pace stated, yet the fast montage op, its planner call or its reply changes;
F5  an unknown direction or pace reaches the planner;
F6  the typed wire (EDIT_COPILOT_OUTPUT_MODE native / json_guarded) fails to build
    (`unregistered wire field type: pace`), cannot carry the stated pace with the switch on, or
    accepts it with the switch off;
F7  with the switch off anything differs: the op, the planner call, the reply, the prompt.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agents import edit_proposal
from app.agents._creator_wire import WireContract, copilot_schema
from app.agents._runtime import TerminalError
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
FAST_REPLY = "I built a fast, music-led replacement draft from the strongest analyzed moments."
MONTAGE_OP_KEYS = [
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


def _planner_raw() -> str:
    """The recorded fast-montage planner answer (aliases m001.. as the planner prompt shows)."""
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


def _snapshot() -> dict:
    """The web drawer's snapshot for a guided revision (`snapshot.ts` lists `direction`)."""
    return {
        "allowed_op_families": ["direction"],
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

    seen: dict = {"stamped": [], "planner_inputs": [], "prompts": [], "planner_fails": False}

    class _RecordedPlanner:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, agent_input, ctx=None):  # noqa: ANN001, ARG002
            seen["planner_inputs"].append(agent_input)
            if seen["planner_fails"]:
                raise TerminalError("recorded planner outage")
            real = edit_proposal.EditProposalAgent(None)  # type: ignore[arg-type]
            return real.parse(_planner_raw(), agent_input)

    real_plan = edit_direction_planner.plan_direction_snapshot

    def _stamping(*args, **kwargs):  # noqa: ANN002, ANN003
        stamped = real_plan(*args, **kwargs)
        seen["stamped"].append(stamped)
        return stamped

    monkeypatch.setattr(edit_direction_planner, "EditProposalAgent", _RecordedPlanner)
    monkeypatch.setattr(edit_direction_planner, "plan_direction_snapshot", _stamping)

    def submit(op: dict, *, message: str = "change the direction", planner_fails: bool = False):
        raw = _copilot_raw(op)
        seen["planner_fails"] = planner_fails

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
            json={"message": message, "turns": [], "snapshot": _snapshot()},
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
    assert list(op) == MONTAGE_OP_KEYS
    assert op["direction"] == "fast_montage"
    assert op["clear_text"] == [{"id": "guided-thought-1", "expected_text": "AI thought"}]
    assert len(op["cuts"]) == 6
    assert body["reply"] == FAST_REPLY
    (stamped,) = seen["stamped"]
    assert (stamped.direction, stamped.pace) == ("fast_montage", "fast")
    assert seen["planner_inputs"][0].goal == "Show the strongest visual moments quickly."
    assert (DIRECTION_FRAGMENT in seen["prompts"][0]) is follows


@pytest.mark.parametrize("planner_fails", [False, True], ids=["planner", "fallback"])
@pytest.mark.parametrize("pace", ["relaxed", "balanced"])
def test_a_pace_the_montage_cannot_follow_is_named_not_claimed(
    turn, monkeypatch, pace, planner_fails
) -> None:
    _flag(monkeypatch, True)
    plain, _ = turn(_op("fast_montage"), planner_fails=planner_fails)
    body, seen = turn(
        _op("fast_montage", pace=pace), message="a montage, but calm", planner_fails=planner_fails
    )

    assert body["ops"] == plain["ops"]
    assert list(body["ops"][0]) == MONTAGE_OP_KEYS
    assert [(s.direction, s.pace) for s in seen["stamped"]] == [("fast_montage", "fast")] * 2
    assert body["outcome"] == "applied"
    assert body["reply"] == (
        f"{FAST_REPLY} The {pace} pacing isn't applied on this video yet: a fast montage keeps "
        "its short cuts."
    )


def test_a_stated_fast_pace_is_what_the_montage_does(turn, monkeypatch) -> None:
    _flag(monkeypatch, True)
    plain, _ = turn(_op("fast_montage"))
    body, _ = turn(_op("fast_montage", pace="fast"), message="a snappy montage")

    assert body["ops"] == plain["ops"]
    assert body["reply"] == FAST_REPLY


@pytest.mark.parametrize("direction", ["text_explainer", "guided_story"])
@pytest.mark.parametrize("pace", [None, "relaxed"])
def test_a_story_direction_is_said_not_planned(turn, monkeypatch, direction, pace) -> None:
    _flag(monkeypatch, True)
    body, seen = turn(_op(direction, pace=pace), message="turn this into something calmer")

    assert body["ops"] == []
    assert body["outcome"] == "unsupported"
    assert body["rejection_reasons"][0]["reason"] == "capability_unavailable"
    assert body["reply"] == (
        f"The {direction.replace('_', ' ')} direction isn't applied on this video yet: this "
        "editor can only restage it as a fast montage, so nothing changed. A different "
        "direction needs a fresh edit."
    )
    assert seen["stamped"] == [] and seen["planner_inputs"] == []


@pytest.mark.parametrize(
    "op", [{**_op("fast_montage"), "direction": "story"}, {**_op("fast_montage"), "pace": "slow"}]
)
def test_unknown_direction_or_pace_never_reaches_the_planner(turn, monkeypatch, op) -> None:
    _flag(monkeypatch, True)
    body, seen = turn(op)

    assert body["ops"] == [] and body["outcome"] == "failed"
    assert body["rejection_reasons"][0]["reason"] == "invalid_value"
    assert seen["stamped"] == []


@pytest.mark.parametrize("follows", [True, False])
def test_typed_wire_carries_the_stated_pace_only_with_the_switch_on(monkeypatch, follows) -> None:
    _flag(monkeypatch, follows)
    contract = WireContract(copilot_schema())
    value = {"intent": "edit", "reply": "Prepared", "ops": [_op("fast_montage", pace="relaxed")]}

    if not follows:
        with pytest.raises(ValueError):
            contract.encode(value)
        return
    assert contract.decode(contract.encode(value)) == value
    with pytest.raises(ValueError):
        contract.encode({**value, "ops": [_op("fast_montage", pace="slow")]})
    agent = EditCopilotAgent(None)  # type: ignore[arg-type]
    data = agent.Input.model_validate(
        {"utterance": "calm montage", "variant_snapshot": _snapshot()}
    )
    (parsed,) = agent.parse(json.dumps(contract.decode(contract.encode(value))), data).ops
    assert parsed["pace"] == "relaxed"


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
    body, seen = turn(op)

    if op.get("direction") == "fast_montage":
        (sent,) = body["ops"]
        assert list(sent) == MONTAGE_OP_KEYS
        assert body["reply"] == FAST_REPLY
        (stamped,) = seen["stamped"]
        assert (stamped.direction, stamped.pace) == ("fast_montage", "fast")
    else:
        assert body["ops"] == [] and body["outcome"] == "failed"
        assert body["rejection_reasons"][0]["reason"] == "invalid_value"
        assert seen["stamped"] == []
    assert DIRECTION_FRAGMENT not in seen["prompts"][0]
