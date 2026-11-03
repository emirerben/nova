"""Visuals autoplace for creator-generated images whose title and the speech differ in language.

Nothing here calls a model. The `contract_*` fixtures are hand-authored recorded responses: they
pin the OUTPUT CONTRACT (what a correct answer looks like and that `parse` accepts it) and, with
the prompt tests, the PROMPT CONTENT. They do not measure what the real model does on this
input; only a live run can (see the PR body for the paid-eval command).
"""

from __future__ import annotations

import json
from pathlib import Path
from string import Template

import pytest

from app.agents.overlay_placement import (
    OverlayPlacementAgent,
    OverlayPlacementInput,
    PlacementAsset,
)
from app.config import settings

TESTS = Path(__file__).parents[1]
GOLDEN = TESTS / "fixtures/agent_evals/overlay_placement/golden"
OLD_PROMPT = TESTS / "fixtures/overlay_placement_prompt_1_3_0.txt"
ASSET_POOL_HEADER = "## The asset pool (analyzed)\n"
WORDS_HEADER = "## The spoken transcript (word-level timestamps, seconds)\n"

CONTRACT_FIXTURES = (
    "contract_generated_image_cross_language_match.json",
    "contract_generated_image_cross_language_mixed.json",
    "contract_generated_image_near_miss_controls.json",
    "contract_generated_image_unrelated_control_omitted.json",
    "contract_upload_loose_asset_omitted.json",
)


def _agent() -> OverlayPlacementAgent:
    return OverlayPlacementAgent.__new__(OverlayPlacementAgent)


def _input(assets: list[PlacementAsset], *, language: str = "tr", words=None):
    return OverlayPlacementInput(
        words=words or [{"word": "kahve", "start_s": 4.0, "end_s": 4.4}],
        assets=assets,
        duration_s=30.0,
        spoken_language=language,
    )


def _generated(**kw) -> PlacementAsset:
    base = {"asset_id": "g1", "origin": "generated", "user_context": "Coffee", "subject": "Coffee"}
    return PlacementAsset(**(base | kw))


def _line_after(prompt: str, header: str) -> str:
    return prompt.split(header, 1)[1].split("\n", 1)[0]


def _render_1_3_0(prompt_now: str, inp: OverlayPlacementInput) -> str:
    """The 1.3.0 template filled with the words and asset JSON the current renderer produced."""
    return Template(OLD_PROMPT.read_text(encoding="utf-8")).safe_substitute(
        words_json=_line_after(prompt_now, WORDS_HEADER),
        assets_json=_line_after(prompt_now, ASSET_POOL_HEADER),
        occupied_json=json.dumps([[round(a, 2), round(b, 2)] for a, b in (inp.occupied or [])]),
        duration_s=f"{inp.duration_s:.1f}",
        archetype=inp.archetype,
    )


def test_prompt_version_is_bumped():
    assert OverlayPlacementAgent.spec.prompt_version == "1.4.0"


def test_an_upload_only_prompt_is_byte_identical_to_1_3_0():
    """Uploads and the web path render the same template: nothing about them may loosen."""
    inp = _input([PlacementAsset(asset_id="u1", user_context="Wine", description="red wine")])
    now = _agent().render_prompt(inp)
    assert now == _render_1_3_0(now, inp)


def test_an_upload_with_a_loosely_related_title_keeps_the_strict_rules():
    prompt = _agent().render_prompt(
        _input(
            [PlacementAsset(asset_id="u1", user_context="Wine")],
            words=[{"word": "bira", "start_s": 4.0, "end_s": 4.4}],
        )
    )
    assert "Creator-generated images" not in prompt
    assert "Anything weaker: OMIT it" in prompt
    assert "neighbouring" not in prompt and "broader" not in prompt


def test_kill_switch_off_restores_the_1_3_0_prompt_for_everything(monkeypatch):
    monkeypatch.setattr(settings, "overlay_placement_cross_language_enabled", False)
    inp = _input([_generated(generation_prompt="a cup of coffee")])
    prompt = _agent().render_prompt(inp)
    pool = json.loads(_line_after(prompt, ASSET_POOL_HEADER))
    assert "origin" not in pool[0] and "generation_prompt" not in pool[0]
    assert prompt == _render_1_3_0(prompt, inp)


def test_kill_switch_defaults_on_and_is_documented():
    assert settings.overlay_placement_cross_language_enabled is True
    env_example = (TESTS.parents[3] / ".env.example").read_text(encoding="utf-8")
    assert "OVERLAY_PLACEMENT_CROSS_LANGUAGE_ENABLED" in env_example


def test_generated_assets_add_the_cross_language_section_and_the_spoken_language():
    prompt = _agent().render_prompt(
        _input([_generated(generation_prompt="a steaming cup on a table")])
    )
    assert "Spoken language: tr" in prompt
    assert "Creator-generated images" in prompt
    pool = json.loads(_line_after(prompt, ASSET_POOL_HEADER))
    assert pool[0]["origin"] == "generated"
    assert pool[0]["generation_prompt"] == "a steaming cup on a table"


def test_unknown_spoken_language_is_said_plainly():
    prompt = _agent().render_prompt(_input([_generated()], language=""))
    assert "Spoken language: unknown" in prompt


def _section(prompt: str) -> str:
    return prompt.split("## Creator-generated images")[1].split("## Return STRICT JSON")[0]


def test_the_loose_rules_apply_to_generated_assets_only():
    section = _section(_agent().render_prompt(_input([_generated()])))
    assert "ONLY" in section
    assert "Every other asset keeps rules 1 and 7 exactly as written" in section
    assert "OMIT" in section
    assert "neighbouring" not in section


def test_prompt_examples_are_generic_and_not_the_incident():
    """A future live replay of the incident input must be a real test, so the prompt may not
    teach the incident pair (or the near-miss controls the fixtures use)."""
    prompt = _agent().render_prompt(_input([_generated(user_context="Sunset", subject="Sunset")]))
    lowered = prompt.casefold()
    for incident_word in ("alkol", "bira", "beer", "içki", "wine", "harita", "şehir"):
        assert incident_word not in lowered, incident_word
    assert _section(prompt).count('"') >= 12  # quoted examples, in several languages


def test_the_prompt_does_not_over_trust_model_written_text():
    lowered = _section(_agent().render_prompt(_input([_generated()]))).casefold()
    assert "own words" not in lowered
    assert "on purpose" not in lowered
    assert "intended" not in lowered
    assert "image-making" in lowered


def _golden(name: str):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", CONTRACT_FIXTURES)
def test_contract_fixtures_are_labelled_as_contracts_not_as_replays(name):
    fx = _golden(name)
    assert fx["meta"]["kind"] == "contract"
    assert "not model behaviour" in fx["meta"]["pins"]
    assert "incident" not in name and "replay" not in name
    inp = OverlayPlacementInput.model_validate(fx["input"])
    _agent().parse(fx["raw_text"], inp)


def test_no_old_incident_named_fixture_is_left_behind():
    assert sorted(p.name for p in GOLDEN.glob("generated_image_*.json")) == []


def test_cross_language_match_contract_places_a_translated_title():
    fx = _golden("contract_generated_image_cross_language_match.json")
    inp = OverlayPlacementInput.model_validate(fx["input"])
    assert inp.spoken_language == "tr"
    (placement,) = _agent().parse(fx["raw_text"], inp).placements
    spoken = " ".join(w["word"] for w in inp.words).casefold()
    assert placement.transcript_anchor.casefold() in spoken
    assert placement.asset_id == inp.assets[0].asset_id
    assert placement.confidence_tier == "likely"


def test_mixed_contract_places_the_translation_and_omits_the_sibling():
    fx = _golden("contract_generated_image_cross_language_mixed.json")
    inp = OverlayPlacementInput.model_validate(fx["input"])
    out = _agent().parse(fx["raw_text"], inp)
    titles = {a.asset_id: a.user_context for a in inp.assets}
    assert [titles[p.asset_id] for p in out.placements] == ["Coffee"]
    assert "Tea" in titles.values()


def test_near_miss_controls_stay_omitted():
    fx = _golden("contract_generated_image_near_miss_controls.json")
    inp = OverlayPlacementInput.model_validate(fx["input"])
    assert {"Wine", "Tea", "Map"} <= {a.user_context for a in inp.assets}
    assert _agent().parse(fx["raw_text"], inp).placements == []


def test_unrelated_control_stays_omitted():
    fx = _golden("contract_generated_image_unrelated_control_omitted.json")
    inp = OverlayPlacementInput.model_validate(fx["input"])
    assert _agent().parse(fx["raw_text"], inp).placements == []


def test_an_upload_with_a_loosely_related_title_is_omitted_and_gets_no_loose_rules():
    fx = _golden("contract_upload_loose_asset_omitted.json")
    inp = OverlayPlacementInput.model_validate(fx["input"])
    assert all(not a.origin for a in inp.assets)
    assert _agent().parse(fx["raw_text"], inp).placements == []
    assert "Creator-generated images" not in _agent().render_prompt(inp)


def test_a_stored_generation_prompt_reaches_the_model_but_an_upload_sends_none():
    prompt = _agent().render_prompt(
        _input(
            [
                _generated(generation_prompt="a steaming cup on a table"),
                PlacementAsset(asset_id="u1", user_context="Coffee"),
            ]
        )
    )
    pool = json.loads(_line_after(prompt, ASSET_POOL_HEADER))
    assert pool[0]["generation_prompt"] == "a steaming cup on a table"
    assert "generation_prompt" not in pool[1] and "origin" not in pool[1]
