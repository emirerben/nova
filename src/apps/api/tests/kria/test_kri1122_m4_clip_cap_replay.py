"""KRI-1122 incident replay: the Leverkusen M4 story fits the clip-instruction cap.

Prod 2026-10-31 (thread eeac2d3b, `tests/fixtures/m4_leverkusen_clip_cap.json`): a 10-beat story
with six labels, one skip and one keep is 18 clip operations. At the old cap of 16 the planner
held them all back with "I can apply at most 16 clip instructions at a time … Please restate
just those", and every restate re-read the same request and hit the cap again. The creator had
to withdraw three labels to get a draft.

Replayed through the real planner parser (no model call). The red control pins the cap back to
16 and gets the live over-cap question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.agents.clip_intent_planner as planner
from app.agents.clip_intent_planner import ClipIntentPlannerAgent, ClipIntentPlannerInput

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "m4_leverkusen_clip_cap.json").read_text("utf-8")
)
REQUEST: str = FIXTURE["request"]
PLANNED: list[dict] = FIXTURE["planned_intents"]


def _raw() -> str:
    intents = [
        {
            "label_source": "clip",
            "transcript_kind": None,
            "creator_text": None,
            "caption_attribute": None,
            "position": None,
            "placeholder": False,
            "requirement_id": None,
            **row,
        }
        for row in PLANNED
    ]
    return json.dumps({"intents": intents, "question": None}, ensure_ascii=False)


def _parse():
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]
    return agent.parse(
        _raw(), ClipIntentPlannerInput(creator_request=REQUEST, clip_exclusions=True)
    )


def test_every_quote_is_the_creators_own_words() -> None:
    assert all(row["source_quote"] in REQUEST for row in PLANNED)
    assert len(PLANNED) == 18


def test_the_m4_story_is_planned_whole_without_a_restate_question() -> None:
    out = _parse()

    assert out.salvage_question is None
    assert out.salvage_reasons == []
    assert [i.intent_id for i in out.intents] == [row["intent_id"] for row in PLANNED]
    labels = {i.intent_id: i.creator_text for i in out.intents if i.op == "label"}
    assert labels == {
        "label_0_0_pyro": "Kick-off",
        "label_stadium_outside": "Arrival",
        "label_warm_up": "Warm-up",
        "label_nurnberg_fans": "Away end",
        "label_first_goal": "GOAL",
        "label_nurnberg_players_thanking": "Full time",
    }


def test_red_control_the_old_cap_asks_the_live_over_cap_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planner, "MAX_CLIP_INTENTS", 16)

    out = _parse()

    assert len(out.intents) == 16  # the planning service then acts on none of them
    assert out.salvage_reasons == ["over_cap"]
    assert out.salvage_question is not None
    assert out.salvage_question.startswith("I understood 16 of your clip instructions.")
    assert "I can apply at most 16 clip instructions at a time, so 2 more weren't included." in (
        out.salvage_question
    )
