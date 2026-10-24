"""KRI-816 T06: "The Historical Journey… Start fresh… include sources" on a 28-clip Montage.

Feeds the exact prod turn's Main Creator input (fixture scrubbed from the read-only
proposal trace: run outcome ``terminal_schema``, 3 attempts, ``policy_refusal``) through
the real parse. Only the input is real: the trace kept no model output, so each
strategy below is a hypothesis for which rule refused, not a replay of it.

Failure modes written before the fix:
1. A model-chosen "explainer" format the iPhone cannot render (Talking, talking head,
   slides) is silently swapped for another format (KRI-129) instead of a named refusal.
2. A model-invented voiceover (nothing recorded) is refused although this turn's brief
   records that the creator asked for original camera audio.
3. The voiceover repair fires without that typed fact (it must then ask, precisely), or
   misses the fact when it is only in the saved brief (T06's r4) and not re-emitted.
4. The refusal tag stays a catch-all, so the next prod trace cannot name the rule.
5. The repair reads meaning from the creator's words (KRI-667) instead of typed facts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient, ModelInvocation, SchemaError
from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import BriefExtractorAgent
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria.brief import (
    BriefRequirement,
    CreativeBrief,
    apply_updates,
    render_brief_request,
)
from app.kria.brief_checks import check_requirement, plan_facts_from_strategy

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "kri816_troya_historical_journey_turn.json"
)
LATEST = json.loads(FIXTURE.read_text())["user_message"]


def _input(**changes) -> MainCreatorInput:
    data = json.loads(FIXTURE.read_text())
    data.pop("_source")
    return MainCreatorInput.model_validate({**data, **changes})


SOURCE_AUDIO = [
    {
        "kind": "audio",
        "scope": "global",
        "description": "Keep original camera audio, with no music",
        "facts": {"audio_intent": "source_unchanged"},
    }
]


def _parse(strategy: dict, brief_updates=None, **changes):
    agent = MainCreatorAgent.__new__(MainCreatorAgent)
    inp = _input(**changes)
    ids = [media.media_id for media in inp.capability_manifest.media]
    raw = {
        **({"brief_updates": brief_updates} if brief_updates else {}),
        "action": {
            "kind": "propose_strategy",
            "summary": "The Historical Journey",
            "strategy": {
                "rationale": "A researched walk through Troy's layers, paced for short notes.",
                "target_duration_s": 30,
                "selected_media_ids": ids[:12],
                **strategy,
            },
        },
    }
    return agent.parse(json.dumps(raw), inp)


@pytest.mark.parametrize("edit_format", ["subtitled", "talking_head", "slides"])
def test_t06_unrenderable_explainer_format_is_a_named_refusal_not_a_silent_swap(edit_format):
    with pytest.raises(SchemaError) as refused:
        _parse({"edit_format": edit_format, "audio_strategy": "original_audio"})
    assert refused.value.error_class.startswith("policy_")
    assert refused.value.error_class != "policy_refusal"


def test_t06_saved_brief_source_audio_repairs_an_invented_voiceover_without_a_new_update():
    output = _parse({"edit_format": "montage", "audio_strategy": "voiceover"})

    assert output.action.strategy.audio_strategy == "original_audio"


def test_t06_invented_voiceover_is_repaired_to_the_requested_camera_audio():
    output = _parse(
        {"edit_format": "montage", "audio_strategy": "voiceover"}, brief_updates=SOURCE_AUDIO
    )

    assert output.action.strategy.audio_strategy == "original_audio"
    assert output.brief_updates[0].facts == {"audio_intent": "source_unchanged"}


def test_without_the_typed_audio_fact_the_voiceover_refusal_names_its_rule():
    data = json.loads(FIXTURE.read_text())
    without_audio = "\n".join(
        line for line in data["creator_request"].splitlines() if "[audio/" not in line
    )
    with pytest.raises(SchemaError) as refused:
        _parse(
            {"edit_format": "montage", "audio_strategy": "voiceover"},
            creator_request=without_audio,
        )
    assert refused.value.error_class == "policy_phone_voiceover_unavailable"


# ---------------------------------------------------------------------------------------
# "Start fresh" supersedes the saved brief, and the turn recovers across attempts.
#
# Failure modes written before the fix:
# 6. "Start fresh" leaves r1 (25 s), r2 (all 28 clips) and r5 (title, "no other text")
#    live, so a plan that follows the latest message is blocked by obsolete requirements.
# 7. The withdrawal is read from the creator's words on the server (KRI-667) instead of
#    the extraction step's typed declaration.
# 8. A start-fresh batch drops what the latest message restates (original camera audio).
# 9. A refused explainer format spends every attempt because the retry is not told which
#    formats can render, so the turn still ends in creator_planning_failed.

SAVED_BRIEF = CreativeBrief(
    version=2,
    requirements=[
        BriefRequirement(
            id="r1",
            kind="timing",
            scope="global",
            description="25-second montage",
            facts={"duration_s": 25},
        ),
        BriefRequirement(
            id="r2",
            kind="select",
            scope="global",
            description="use all 28 Troya clips exactly once",
        ),
        BriefRequirement(
            id="r3",
            kind="order",
            scope="global",
            description="upload order, keeping the horse reveal first, car interior second, "
            "and the museum ending",
            facts={
                "closing": "inside the museum",
                "first_clip": "the wooden horse reveal",
                "key": "upload_time",
            },
        ),
        BriefRequirement(
            id="r4",
            kind="audio",
            scope="global",
            description="Keep original camera audio, with no music",
            facts={"audio_intent": "source_unchanged"},
        ),
        BriefRequirement(
            id="r5",
            kind="text",
            scope="title",
            literal="Troya’da Bir Gün",
            description="Show from 0 to 3 seconds and no other text",
        ),
    ],
)

# The extraction step's recorded answer for T06's latest message: it declares the fresh
# start and restates only what that message asks for.
FRESH_EXTRACTION = {
    "starts_fresh": True,
    "delegates_all_choices": False,
    "request_scope": "rebuild",
    "brief_updates": SOURCE_AUDIO,
}


def _extract(answer: dict):
    agent = BriefExtractorAgent.__new__(BriefExtractorAgent)
    inp = BriefExtractionInput(
        creator_request=render_brief_request(SAVED_BRIEF, latest_message=LATEST),
        user_message=LATEST,
        current_brief=SAVED_BRIEF,
        require_request_scope=True,
    )
    return agent.parse(json.dumps(answer), inp)


def _receipts(brief: CreativeBrief, output) -> dict[str, str]:
    inp = _input(creator_request=render_brief_request(brief, latest_message=LATEST))
    strategy = output.action.strategy.model_dump(mode="json")
    facts = plan_facts_from_strategy(
        strategy,
        clip_ids=[media.media_id for media in inp.capability_manifest.media],
        manifest=inp.capability_manifest,
    )
    return {req.id: check_requirement(req, facts).status for req in brief.live()}


def test_t06_start_fresh_withdraws_the_stale_brief_and_keeps_the_restated_audio():
    extraction = _extract(FRESH_EXTRACTION)
    effective = apply_updates(SAVED_BRIEF, extraction.brief_updates, source_turn_id=None)

    live = effective.live()
    assert [req.kind for req in live] == ["audio"]
    assert live[0].facts == {"audio_intent": "source_unchanged"}
    request = render_brief_request(effective, latest_message=LATEST)
    for stale in ("25-second", "all 28", "no other text", "Troya’da Bir Gün"):
        assert stale not in request.split("Latest message:")[0]

    # The 30 s, 12-clip researched plan follows the latest message.
    output = _parse(
        {"edit_format": "montage", "audio_strategy": "voiceover"},
        creator_request=request,
    )
    assert output.action.strategy.audio_strategy == "original_audio"
    # Only the restated audio is still owed (the mix is checked on the render, so its
    # plan-time receipt is "partial"); the stale 25 s / 28 clips / "no other text" rules
    # the same plan would otherwise still be held to are gone.
    assert _receipts(effective, output) == {"r6": "partial"}
    assert set(_receipts(SAVED_BRIEF, output)) == {"r1", "r2", "r3", "r4", "r5"}


def test_without_the_typed_declaration_nothing_is_withdrawn():
    extraction = _extract({**FRESH_EXTRACTION, "starts_fresh": False})
    effective = apply_updates(SAVED_BRIEF, extraction.brief_updates, source_turn_id=None)

    assert {req.id for req in effective.live()} >= {"r1", "r2", "r3", "r4", "r5"}


def test_main_creator_start_fresh_supersedes_the_saved_audio_fallback():
    # The saved r4 says camera audio; a fresh start that restates no audio must not keep
    # using it to repair a voiceover (it is a named refusal again).
    data = json.loads(FIXTURE.read_text())
    agent = MainCreatorAgent.__new__(MainCreatorAgent)
    inp = _input()
    raw = {
        "starts_fresh": True,
        "action": {
            "kind": "propose_strategy",
            "summary": "The Historical Journey",
            "strategy": {
                "rationale": "A researched walk through Troy's layers.",
                "target_duration_s": 30,
                "selected_media_ids": [m.media_id for m in inp.capability_manifest.media][:12],
                "edit_format": "montage",
                "audio_strategy": "voiceover",
            },
        },
    }
    assert "[audio/global]" in data["creator_request"]
    with pytest.raises(SchemaError) as refused:
        agent.parse(json.dumps(raw), inp)
    assert refused.value.error_class == "policy_phone_voiceover_unavailable"


class _RecordedClient(ModelClient):
    """Replays a fixed sequence of model answers and records every prompt."""

    def __init__(self, answers: list[dict]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def invoke(self, *, model: str, prompt: str, **_: object) -> ModelInvocation:  # noqa: ARG002
        self.prompts.append(prompt)
        return ModelInvocation(raw_text=json.dumps(self.answers.pop(0)))


def _strategy_answer(**strategy) -> dict:
    ids = [media.media_id for media in _input().capability_manifest.media]
    return {
        "brief_updates": SOURCE_AUDIO,
        "action": {
            "kind": "propose_strategy",
            "summary": "The Historical Journey",
            "strategy": {
                "rationale": "A researched walk through Troy's layers, paced for short notes.",
                "target_duration_s": 30,
                "selected_media_ids": ids[:12],
                **strategy,
            },
        },
    }


@pytest.mark.parametrize("refused_format", ["talking_head", "subtitled"])
def test_t06_turn_recovers_after_an_unrenderable_explainer_format(refused_format):
    client = _RecordedClient(
        [
            _strategy_answer(edit_format=refused_format, audio_strategy="original_audio"),
            _strategy_answer(edit_format="montage", audio_strategy="voiceover"),
        ]
    )
    output = MainCreatorAgent(client).run(_input())

    assert len(client.prompts) == 2
    retry = client.prompts[1]
    assert "edit_format values that can render here: " in retry
    renderable = retry.split("edit_format values that can render here: ")[1].split(".")[0]
    assert "montage" in renderable
    assert refused_format not in renderable.split(", ")
    assert output.action.strategy.edit_format == "montage"
    assert output.action.strategy.audio_strategy == "original_audio"
