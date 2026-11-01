"""Planner boundary replay (KRI-1097 wave 2, lane L7): raw Main Creator responses through the
real parse, the runtime-v2 policy check (`check_strategy_for_runtime_v2`, the strategy the draft
stores) and the approval card lines, with PLANNER_BOUNDARY_HONEST_ENABLED on and off.

Failure modes, listed before the code (agents/DECISIONS.md "Prompt fidelity wave 2: planner"):

F1  a typed timing / cadence / reuse ask in words the English/Turkish recognizers do not read
    is erased by their silence (Spanish).
F2  the model invents a timing, a cadence or repeats nobody asked for, and it is kept.
F3  the recognizers' explicit reading loses to the model's value.
F4  a latest-turn "stop alternating" loses to a stale model cadence.
F5  a kept ask the manifest cannot render makes the parse refuse (a retry spent, a turn lost).
F6  a quote that is not the creator's own words (a paraphrase, one word) grounds a value.
F7  a title animation / label position the renderer has no name for vanishes silently.
F8  a known look word, an alias or an "off" word is declined.
F9  the model writes ``planner_declines`` itself.
F10 the switch off is not byte-identical: any recorded response parses differently, or the
    card shows a planner line.
F11 a decline blocks approval or changes the render (it is a card line only).

Each case in ``tests/fixtures/kria_replay/planner_boundary/cases.json`` names the modes it
covers; the recorded corpus (``tests/fixtures/agent_evals/main_creator``) and the Assos draft
cover F2 and F10 on real responses.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents.main_creator import MainCreatorAgent
from app.config import settings
from app.kria import route_disclosure
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2

FIXTURES = Path(__file__).parents[1] / "fixtures"
CASES = json.loads((FIXTURES / "kria_replay/planner_boundary/cases.json").read_text())
CORPUS = sorted((FIXTURES / "agent_evals/main_creator").glob("*.json"))
BASE_INPUT = json.loads((FIXTURES / "agent_evals/main_creator/montage_strategy.json").read_text())[
    "input"
]


def _input(case: dict) -> MainCreatorAgent.Input:
    data = copy.deepcopy(BASE_INPUT)
    data["user_message"] = case["message"]
    data["conversation"] = [{"role": "user", "content": text} for text in case["conversation"]]
    manifest = data["capability_manifest"]
    for media in manifest["media"]:
        if media["kind"] == "video":
            media["duration_s"] = 9.0
    manifest["capabilities"].update(CASES["manifests"][case["manifest"]])
    return MainCreatorAgent.Input.model_validate(data)


def _parse(monkeypatch, raw: str, agent_input, *, on: bool) -> dict:
    monkeypatch.setattr(settings, "planner_boundary_honest_enabled", on)
    return MainCreatorAgent(None).parse(raw, agent_input).model_dump(mode="json")


def _checked(agent_input, strategy: dict, message: str) -> dict:
    """The strategy the runtime-v2 draft stores: the Kria policy check over the parse."""
    checked = check_strategy_for_runtime_v2(
        agent_input.capability_manifest,
        CreativeStrategy.model_validate(strategy),
        ask_before_simplifying=True,
        simplify_unsupported=True,
        creator_text=message,
    )
    # F5 / F11: a kept ask or a decline never turns the draft into a question.
    assert isinstance(checked, CheckedStrategy), checked
    return checked.strategy.model_dump(mode="json", exclude_none=True)


def _card(strategy: dict, route: str, language: str) -> tuple[list, str]:
    with reply_language_for(language):
        lines = route_disclosure.strategy_notices(strategy, route=route)
        return lines, route_disclosure.card_text(lines)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["id"])
def test_planner_boundary_case(monkeypatch, case: dict) -> None:
    raw = json.dumps(case["raw"])
    for state in ("on", "off"):
        output = _parse(monkeypatch, raw, _input(case), on=state == "on")
        strategy = output["action"]["strategy"]
        got = {field: strategy.get(field) for field in case["expect"][state]}
        assert got == case["expect"][state], f"{case['id']} ({state}): {case['about']}"
        if state == "on":
            on_strategy = strategy
        else:
            off_strategy = strategy

    subjects = case["expect"].get("card_subjects", [])
    monkeypatch.setattr(settings, "planner_boundary_honest_enabled", True)
    drafted = _checked(_input(case), on_strategy, case["message"])
    for field, want in case["expect"]["on"].items():
        assert drafted.get(field) == want, f"{case['id']}: the check lost {field}"
    for route in ("unified_montage", "voiceover_montage", "talking_head"):
        for language in ("en", "tr"):
            lines, text = _card(drafted, route, language)
            planner = [line for line in lines if line.subject.startswith("planner_")]
            assert [line.subject for line in planner] == subjects, (route, language)
            # F11: a line under the heads-up, never a receipt or a question.
            assert all(line.code == route_disclosure.NOT_APPLIED for line in planner)
            assert all(line.text in text for line in planner)
    # F10: the off card has no planner line, even for a stored strategy that carries one.
    monkeypatch.setattr(settings, "planner_boundary_honest_enabled", False)
    lines, _text = _card(on_strategy, "unified_montage", "en")
    assert not [line for line in lines if line.subject.startswith("planner_")]
    # F11: a look decline leaves every render field as the off parse has it.
    if case["id"] == "unknown_looks":
        assert {k: v for k, v in on_strategy.items() if k != "planner_declines"} == off_strategy


def test_decline_lines_read_in_both_languages(monkeypatch) -> None:
    case = next(c for c in CASES["cases"] if c["id"] == "unknown_looks")
    strategy = _parse(monkeypatch, json.dumps(case["raw"]), _input(case), on=True)["action"][
        "strategy"
    ]
    _lines, en = _card(strategy, "unified_montage", "en")
    _lines, tr = _card(strategy, "unified_montage", "tr")
    assert '"bounce" animation for the title yet, so it won\'t be used' in en
    assert 'clip labels "upper middle" yet' in en
    assert '"bounce" animasyonum yok' in tr
    assert '"upper middle" konumuna yerleştiremiyorum' in tr


@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.stem)
def test_recorded_responses_parse_the_same_with_the_switch_on(monkeypatch, path: Path) -> None:
    """F2 / F10 on real responses: none carries a quote for these fields or an unknown look,
    so the switch changes nothing (`video_reuse_default` keeps dropping the very fast photo
    timing the model invented for "Create a slow Summer in Madrid montage")."""
    fixture = json.loads(path.read_text())
    agent_input = MainCreatorAgent.Input.model_validate(fixture["input"])
    assert _parse(monkeypatch, fixture["raw_text"], agent_input, on=True) == _parse(
        monkeypatch, fixture["raw_text"], agent_input, on=False
    )


def test_assos_draft_parses_the_same_with_the_switch_on(monkeypatch) -> None:
    """The Assos planner response (its recorded post-parse strategy, server fields removed) over
    its 16 clips: no timing, cadence, repeat or unknown look, so nothing moves."""
    fixture = json.loads((FIXTURES / "kria_replay/assos/fixture.json").read_text())
    strategy = {k: v for k, v in fixture["strategy"].items() if k != "selected_music_section"}
    data = copy.deepcopy(BASE_INPUT)
    data["user_message"] = fixture["message"]
    data["media_context"] = [{"media_id": clip["id"], "kind": "video"} for clip in fixture["clips"]]
    manifest = data["capability_manifest"]
    manifest["media"] = [
        {"media_id": clip["id"], "kind": "video", "duration_s": clip["duration_s"]}
        for clip in fixture["clips"]
    ]
    agent_input = MainCreatorAgent.Input.model_validate(data)
    raw = json.dumps({"action": {"kind": "propose_strategy", "strategy": strategy}})
    on = _parse(monkeypatch, raw, agent_input, on=True)
    assert on == _parse(monkeypatch, raw, agent_input, on=False)
    parsed = on["action"]["strategy"]
    assert parsed["video_reuse_policy"] == "once"
    assert "mixed_media_timing" not in parsed or parsed["mixed_media_timing"] is None
    assert "planner_declines" not in parsed
