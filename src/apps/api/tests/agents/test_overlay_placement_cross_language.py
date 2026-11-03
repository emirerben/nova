"""Visuals autoplace must match across languages (prod 2026-11-03, Beer vs "alkol").

Nothing here calls a model. The recorded-response fixtures only pin the output contract;
whether the real model follows the new prompt on this input is UNPROVEN until a live run.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.overlay_placement import (
    OverlayPlacementAgent,
    OverlayPlacementInput,
    PlacementAsset,
)

GOLDEN = Path(__file__).parents[1] / "fixtures/agent_evals/overlay_placement/golden"


def _prompt(**kw) -> str:
    agent = OverlayPlacementAgent.__new__(OverlayPlacementAgent)
    return agent.render_prompt(
        OverlayPlacementInput(
            words=[{"word": "alkol", "start_s": 48.24, "end_s": 48.68}],
            assets=[
                PlacementAsset(
                    asset_id="a1", user_context="Beer", description="Beer", origin="generated"
                )
            ],
            duration_s=60.0,
            **kw,
        )
    )


def test_prompt_version_is_bumped():
    assert OverlayPlacementAgent.spec.prompt_version == "1.4.0"


def test_spoken_language_and_origin_reach_the_prompt():
    prompt = _prompt(spoken_language="tr")
    assert "Spoken language: tr" in prompt
    assets = json.loads(prompt.split("## The asset pool (analyzed)\n", 1)[1].split("\n", 1)[0])
    assert assets[0]["origin"] == "generated"


def test_unknown_language_is_said_plainly_and_uploads_keep_their_payload():
    agent = OverlayPlacementAgent.__new__(OverlayPlacementAgent)
    prompt = agent.render_prompt(
        OverlayPlacementInput(
            words=[{"word": "hi", "start_s": 0.1, "end_s": 0.3}],
            assets=[PlacementAsset(asset_id="a1", user_context="x")],
            duration_s=10.0,
        )
    )
    assert "Spoken language: unknown" in prompt
    assert '"origin"' not in prompt


def test_prompt_teaches_meaning_matching_across_languages():
    prompt = _prompt(spoken_language="tr")
    assert "different language" in prompt
    assert "alkol" in prompt and "alcohol" in prompt
    assert "transcript_anchor must be words that appear in the transcript" in prompt


def test_weak_matches_are_still_omitted_but_a_clear_topic_is_accepted():
    prompt = _prompt()
    assert "clear topical match" in prompt
    assert "Unrelated" in prompt and "OMIT" in prompt


def _golden(name: str):
    return json.loads((GOLDEN / name).read_text())


def test_replay_fixture_places_the_beer_image_at_the_first_alkol():
    fx = _golden("generated_image_cross_language_match.json")
    agent = OverlayPlacementAgent.__new__(OverlayPlacementAgent)
    inp = OverlayPlacementInput.model_validate(fx["input"])
    assert inp.spoken_language == "tr"
    out = agent.parse(fx["raw_text"], inp)
    (placement,) = out.placements
    assert placement.asset_id == "asset_beer"
    assert abs(placement.start_s - 48.24) < 0.01


def test_replay_control_keeps_an_unrelated_image_out():
    fx = _golden("generated_image_unrelated_control_omitted.json")
    agent = OverlayPlacementAgent.__new__(OverlayPlacementAgent)
    inp = OverlayPlacementInput.model_validate(fx["input"])
    assert agent.parse(fx["raw_text"], inp).placements == []
