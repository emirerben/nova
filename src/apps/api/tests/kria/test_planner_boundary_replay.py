"""Planner boundary replay (KRI-1097 wave 2, lane L7): raw Main Creator responses through the
real parse, the runtime-v2 policy check (`check_strategy_for_runtime_v2`, the strategy the draft
stores) and the approval card lines, in every state of PLANNER_BOUNDARY_HONEST_ENABLED (the
quote rule, default off) and PLANNER_UNKNOWN_LOOK_DECLINE_ENABLED (unknown looks on the card,
default on).

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
F9  the model writes ``planner_declines`` itself and it is anything but refused as the unknown
    key it is on the foundation.
F10 a switch off is not byte-identical: with both off a recorded response parses differently, a
    response the foundation refuses (a boundary quote, a model-written ``planner_declines``)
    parses or fails differently, a run reports another prompt version, or the card shows a
    planner line; with one off, that switch's behaviour leaks through the other.
F11 a decline blocks approval or changes the render (it is a card line only).
F12 a fragment of the creator's clause grounds a value, so the words it leaves out turn the ask
    around or change its subject ("las fotos pasen muy rápido" out of "No quiero que las fotos
    pasen muy rápido", "montaje lento" out of "Crea un montaje lento del verano en Madrid",
    Turkish "geçmesin" left off).
F13 the model writes seconds the creator never wrote (a 1 s cut for "cada 2 segundos", a photo
    hold nobody gave) and the quote keeps them.
F14 a decline sentence is reworded into a claim ("I kept the photo timing you asked for") in
    either language and nothing fails.
F15 a quoted cadence carries the model's own ``reuse_policy: allow_repeat``, or the cadence
    sentence (or a part of it) is reused as the repeat quote, so the plan loops footage the
    creator never allowed.
F16 a negation earlier in the sentence escapes the check because a comma, a semicolon, an aside,
    a colon or a list break sits between it and the quote ("No quiero:\\n- fotos muy rápido"),
    or a later clause of the sentence turns the quoted words around.
F17 a timing quote keeps grouping the model read elsewhere (photo runs, sport groups and their
    order), or a digit that is not seconds ("Somos 2 amigos") grounds a photo hold.
F18 a client draft save writes ``planner_declines`` (a forged card line), keeps a decline whose
    field the client changed (a stale line: "bounce" not used under a title set to fade), or a
    stored strategy carrying a decline whose switch is off validates to anything but the
    foundation's strategy without it.
F19 a kept model cadence alternates other than the manifest's exactly two videos (three videos,
    a video and a photo), which the recognizers' path never allows.
F20 the creator's text and the model's quote spell an accent differently (NFD and NFC) and a
    real ask is dropped.

The checks behind F12, F13, F15, F16, F17 and F20 are language-neutral (sentence marks, list
marks, digits and the seconds unit written after them, Unicode normalization): the language
policy (KRI-773/786) leaves what a sentence means to the model's reading. Each case in
``tests/fixtures/kria_replay/planner_boundary/cases.json`` names the modes it covers; the
``attack_*`` cases (reviewer 4) record the creator's ``direction`` per field, and those marked
``limit`` are documented limits of the quote design, not fixed: a whole sentence about something
else, a reversal in the next sentence or a later turn, seconds written as words or as 2", an
empty quote. The recorded corpus (``tests/fixtures/agent_evals/main_creator``) and the Assos
draft cover F2 and F10 on real responses. F14 is the route card golden
(``tests/kria/test_route_lines_golden.py``, probe ``planner_declines``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents.main_creator import MAIN_CREATOR_PROMPT_VERSION, MainCreatorAgent
from app.config import settings
from app.kria import route_disclosure
from app.kria.drafts import _sanitize_strategy_duration_provenance
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2

FIXTURES = Path(__file__).parents[1] / "fixtures"
CASES = json.loads((FIXTURES / "kria_replay/planner_boundary/cases.json").read_text())
CORPUS = sorted((FIXTURES / "agent_evals/main_creator").glob("*.json"))
BASE_INPUT = json.loads((FIXTURES / "agent_evals/main_creator/montage_strategy.json").read_text())[
    "input"
]
LOOKS = {"title_animation", "label_position"}
# (quote rule, unknown looks) per state; "off" is the foundation.
STATES = {
    "on": (True, True),
    "off": (False, False),
    "default": (False, True),
    "quotes": (True, False),
}


def _input(case: dict) -> MainCreatorAgent.Input:
    data = copy.deepcopy(BASE_INPUT)
    data["user_message"] = case["message"]
    data["conversation"] = [{"role": "user", "content": text} for text in case["conversation"]]
    manifest = data["capability_manifest"]
    for media in manifest["media"]:
        if media["kind"] == "video":
            media["duration_s"] = 9.0
    for media in case.get("extra_media", ()):
        manifest["media"].append({**media, "duration_s": 9.0})
        data["media_context"].append(
            {"media_id": media["media_id"], "kind": media["kind"], "creator_note": media["label"]}
        )
    manifest["capabilities"].update(CASES["manifests"][case["manifest"]])
    return MainCreatorAgent.Input.model_validate(data)


def _switch(monkeypatch, state: str) -> None:
    quotes, looks = STATES[state]
    monkeypatch.setattr(settings, "planner_boundary_honest_enabled", quotes)
    monkeypatch.setattr(settings, "planner_unknown_look_decline_enabled", looks)


def _parse(monkeypatch, raw: str, agent_input, *, state: str) -> dict:
    _switch(monkeypatch, state)
    return MainCreatorAgent(None).parse(raw, agent_input).model_dump(mode="json")


def _expected(case: dict, state: str) -> dict:
    """The switch contract: the quote rule's fields as ``on`` or ``off`` (the foundation) has
    them, and the look declines of ``on`` only while their switch is on, ahead of the rest."""
    quotes, looks = STATES[state]
    on = case["expect"]["on"]
    base = on if quotes else case["expect"]["off"]
    if "error_class" in base:
        return base
    want = dict(base)
    look_declines = [] if "error_class" in on else on.get("planner_declines") or []
    declines = [d for d in look_declines if looks and d["field"] in LOOKS] + [
        d for d in base.get("planner_declines") or [] if d["field"] not in LOOKS
    ]
    if "planner_declines" in want or declines:
        want["planner_declines"] = declines or None
    return want


def _subjects(case: dict, state: str) -> list[str]:
    quotes, looks = STATES[state]
    return [
        subject
        for subject in case["expect"].get("card_subjects", [])
        if (looks if subject.removeprefix("planner_") in LOOKS else quotes)
    ]


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


def _planner_lines(strategy: dict) -> list[str]:
    lines, _text = _card(strategy, "unified_montage", "en")
    return [line.subject for line in lines if line.subject.startswith("planner_")]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["id"])
def test_planner_boundary_case(monkeypatch, case: dict) -> None:
    raw = json.dumps(case["raw"])
    parsed: dict[str, dict] = {}
    for state in STATES:
        want = _expected(case, state)
        if "error_class" in want:
            # F9 / F10: the foundation refuses this response (unknown keys), so this state does
            # too: the same SchemaError, the same retry.
            with pytest.raises(SchemaError) as refused:
                _parse(monkeypatch, raw, _input(case), state=state)
            assert refused.value.error_class == want["error_class"], (case["id"], state)
            continue
        strategy = _parse(monkeypatch, raw, _input(case), state=state)["action"]["strategy"]
        got = {field: strategy.get(field) for field in want}
        assert got == want, f"{case['id']} ({state}): {case['about']}"
        parsed[state] = strategy

        drafted = _checked(_input(case), strategy, case["message"])
        for field, value in want.items():
            assert drafted.get(field) == value, f"{case['id']} ({state}): the check lost {field}"
        for route in ("unified_montage", "voiceover_montage", "talking_head"):
            for language in ("en", "tr"):
                lines, text = _card(drafted, route, language)
                planner = [line for line in lines if line.subject.startswith("planner_")]
                assert [line.subject for line in planner] == _subjects(case, state), (
                    case["id"],
                    state,
                    route,
                    language,
                )
                # F11: a line under the heads-up, never a receipt or a question.
                assert all(line.code == route_disclosure.NOT_APPLIED for line in planner)
                assert all(line.text in text for line in planner)

    if "on" in parsed:
        # F10 / F18: a strategy stored with both switches on shows only the lines whose switch
        # is on now, and none with both off.
        for state in STATES:
            _switch(monkeypatch, state)
            assert _planner_lines(parsed["on"]) == _subjects(case, state), (case["id"], state)
    # F11: a look decline leaves every render field as the foundation's parse has it.
    if case["id"] == "unknown_looks":
        assert {k: v for k, v in parsed["on"].items() if k != "planner_declines"} == parsed["off"]


def _without_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    return value


@pytest.mark.parametrize(
    "case", [case for case in CASES["cases"] if "direction" in case], ids=lambda case: case["id"]
)
def test_the_quote_rule_follows_the_creators_direction(monkeypatch, case: dict) -> None:
    """Guard for a later flip of the PLANNER_BOUNDARY_HONEST_ENABLED default (reviewer 4): with
    the quote rule on, each field keeps the model's value exactly when the creator asked for it.
    A case marked ``limit`` is a documented limit of the quote design where it does not; a
    change that fixes one must drop the mark."""
    strategy = _parse(monkeypatch, json.dumps(case["raw"]), _input(case), state="on")["action"][
        "strategy"
    ]
    model = CreativeStrategy.model_validate(
        {k: v for k, v in case["raw"]["action"]["strategy"].items()}
    ).model_dump(mode="json")
    followed = all(
        (_without_none(strategy.get(field)) == _without_none(model.get(field)))
        == (direction == "keep")
        for field, direction in case["direction"].items()
    )
    assert followed is not bool(case.get("limit")), (case["id"], case.get("limit"))


@pytest.mark.parametrize("state", STATES)
def test_a_client_draft_save_keeps_only_the_head_drafts_declines(monkeypatch, state: str) -> None:
    """F18: `drafts.write_draft` takes the strategy from the client; its `planner_declines`
    never survive, the head draft's (the parse wrote them) do while their switch is on and the
    client left their field as the head had it, and with both switches off none does, so the
    saved strategy is the foundation's."""
    _switch(monkeypatch, state)
    quotes, looks = STATES[state]
    case = next(c for c in CASES["cases"] if c["id"] == "forged_declines")
    clean = {k: v for k, v in case["raw"]["action"]["strategy"].items() if k != "planner_declines"}
    head_declines = [
        {"field": "title_animation", "value": "bounce"},
        {"field": "mixed_media_timing"},
    ]
    head = {**_sanitize_strategy_duration_provenance(clean), "planner_declines": head_declines}
    forged = {**clean, "planner_declines": [{"field": "label_position", "value": "spin"}]}
    enabled = [d for d in head_declines if (looks if d["field"] in LOOKS else quotes)]

    assert "planner_declines" not in _sanitize_strategy_duration_provenance(forged)
    saved = _sanitize_strategy_duration_provenance(forged, head_strategy=head)
    assert saved.get("planner_declines") == (enabled or None)
    if not enabled:
        assert saved == _sanitize_strategy_duration_provenance(clean)

    # The client sets the title to fade: "bounce" is no longer the title's story.
    edited = _sanitize_strategy_duration_provenance(
        {**clean, "opening_title": "Lisbon", "title_animation": "fade"}, head_strategy=head
    )
    assert edited.get("title_animation") == "fade"
    assert edited.get("planner_declines") == (
        [d for d in enabled if d["field"] != "title_animation"] or None
    )
    # An unknown word typed back is still unset, so the line stays true.
    retyped = _sanitize_strategy_duration_provenance(
        {**clean, "title_animation": "bounce"}, head_strategy=head
    )
    assert retyped.get("planner_declines") == (enabled or None)


def test_a_stored_decline_validates_to_the_strategy_without_it_once_its_switch_is_off(
    monkeypatch,
) -> None:
    """F18: a strategy stored (or echoed by a client) while a switch was on stays readable after
    that switch is turned off and validates to exactly what the foundation made of it without
    the declines of that switch; a model output carrying the key is still refused
    (``forged_declines``)."""
    stored = []
    for case in CASES["cases"]:
        if "error_class" in case["expect"]["on"] or not case["expect"]["on"].get(
            "planner_declines"
        ):
            continue
        parsed = _parse(monkeypatch, json.dumps(case["raw"]), _input(case), state="on")
        stored.append((case["id"], parsed["action"]["strategy"]))
    fields = {d["field"] for _cid, strategy in stored for d in strategy["planner_declines"]}
    assert LOOKS <= fields and fields - LOOKS, fields
    for state, (quotes, looks) in STATES.items():
        _switch(monkeypatch, state)
        for cid, strategy in stored:
            bare = {k: v for k, v in strategy.items() if k != "planner_declines"}
            kept = [
                d
                for d in strategy["planner_declines"]
                if (looks if d["field"] in LOOKS else quotes)
            ]
            want = CreativeStrategy.model_validate(
                {**bare, **({"planner_declines": kept} if kept else {})}
            ).model_dump(mode="json")
            for value in (strategy["planner_declines"], None, []):
                got = CreativeStrategy.model_validate({**bare, "planner_declines": value})
                expected = (
                    want if value else CreativeStrategy.model_validate(bare).model_dump(mode="json")
                )
                assert got.model_dump(mode="json") == expected, (cid, state, value)


@pytest.mark.parametrize("looks", [True, False])
def test_only_the_quote_rule_reports_the_v109_prompt(monkeypatch, looks: bool) -> None:
    """F10: with the quote rule off the rendered prompt is v108's byte for byte, so the run
    records v108; the look switch changes no prompt."""
    monkeypatch.setattr(settings, "planner_unknown_look_decline_enabled", looks)
    monkeypatch.setattr(settings, "planner_boundary_honest_enabled", False)
    assert MainCreatorAgent(None).spec.prompt_version == "2026-11-01-v108"
    monkeypatch.setattr(settings, "planner_boundary_honest_enabled", True)
    assert MainCreatorAgent(None).spec.prompt_version == MAIN_CREATOR_PROMPT_VERSION


def test_the_shipped_defaults_keep_the_quote_rule_dark() -> None:
    """Reviewer 4: the quote rule keeps the model's value on a whole sentence about something
    else (the ``limit`` cases), so it stays off until the paid live v109 eval runs; the look
    declines carry no such risk."""
    assert type(settings).model_fields["planner_boundary_honest_enabled"].default is False
    assert type(settings).model_fields["planner_unknown_look_decline_enabled"].default is True


@pytest.mark.parametrize("state", ["on", "default"])
def test_decline_lines_read_in_both_languages(monkeypatch, state: str) -> None:
    case = next(c for c in CASES["cases"] if c["id"] == "unknown_looks")
    strategy = _parse(monkeypatch, json.dumps(case["raw"]), _input(case), state=state)["action"][
        "strategy"
    ]
    _lines, en = _card(strategy, "unified_montage", "en")
    _lines, tr = _card(strategy, "unified_montage", "tr")
    assert '"bounce" animation for the title yet, so it won\'t be used' in en
    assert 'clip labels "upper middle" yet' in en
    assert '"bounce" animasyonum yok' in tr
    assert '"upper middle" konumuna yerleştiremiyorum' in tr


@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.stem)
def test_recorded_responses_parse_the_same_in_every_switch_state(monkeypatch, path: Path) -> None:
    """F2 / F10 on real responses: none carries a quote for these fields or an unknown look,
    so no switch changes anything (`video_reuse_default` keeps dropping the very fast photo
    timing the model invented for "Create a slow Summer in Madrid montage")."""
    fixture = json.loads(path.read_text())
    agent_input = MainCreatorAgent.Input.model_validate(fixture["input"])
    off = _parse(monkeypatch, fixture["raw_text"], agent_input, state="off")
    for state in ("on", "default", "quotes"):
        assert _parse(monkeypatch, fixture["raw_text"], agent_input, state=state) == off, state


def test_assos_draft_parses_the_same_in_every_switch_state(monkeypatch) -> None:
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
    off = _parse(monkeypatch, raw, agent_input, state="off")
    for state in ("on", "default", "quotes"):
        assert _parse(monkeypatch, raw, agent_input, state=state) == off, state
    parsed = off["action"]["strategy"]
    assert parsed["video_reuse_policy"] == "once"
    assert "mixed_media_timing" not in parsed or parsed["mixed_media_timing"] is None
    assert "planner_declines" not in parsed
