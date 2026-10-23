"""KRI-816 T06: "The Historical Journey… Start fresh… include sources" on a 28-clip Montage.

Replays the exact prod turn's Main Creator input (fixture scrubbed from the read-only
proposal trace: run outcome ``terminal_schema``, 3 attempts, ``policy_refusal``) through
the real parse, and the saved brief v2 through the brief ledger.

Failure modes written before the fix:
1. A model-chosen "explainer" format the iPhone cannot render (Talking, talking head,
   slides) is refused on every attempt -> creator_planning_failed, no plan.
2. A model-invented voiceover (nothing recorded) is refused although this turn's brief
   records that the creator asked for original camera audio.
3. The voiceover repair fires without that typed fact (it must then ask, precisely).
4. The refusal tag stays a catch-all, so the next prod trace cannot name the rule.
5. The repair reads meaning from the creator's words (KRI-667) instead of typed facts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

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
def test_t06_unrenderable_explainer_format_returns_to_the_creators_montage(edit_format):
    output = _parse({"edit_format": edit_format, "audio_strategy": "original_audio"})

    assert output.action.kind == "propose_strategy"
    assert output.action.strategy.edit_format == "montage"
    assert output.action.strategy.audio_strategy == "original_audio"


def test_t06_invented_voiceover_is_repaired_to_the_requested_camera_audio():
    output = _parse(
        {"edit_format": "montage", "audio_strategy": "voiceover"}, brief_updates=SOURCE_AUDIO
    )

    assert output.action.strategy.audio_strategy == "original_audio"
    assert output.brief_updates[0].facts == {"audio_intent": "source_unchanged"}


def test_without_the_typed_audio_fact_the_voiceover_refusal_names_its_rule():
    with pytest.raises(SchemaError) as refused:
        _parse({"edit_format": "montage", "audio_strategy": "voiceover"})
    assert refused.value.error_class == "policy_phone_voiceover_unavailable"
