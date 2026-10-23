"""KRI-789: quoted start / end LINES inside one take are recorded as a trim, not a clip order.

Merope T1 (prod 042c405c9, thread 32a5b608): "Start at "Hi, I'm Karolina, I'm from Ecuador"
and end right after "I'm gonna be here all week"" on one video came back from the Main
Creator (v64) as a key-less `order` requirement, so turn 1 asked "You gave me a specific
order for the clips". Every case replays the turn's reply (the eval fixture
``kri789_one_take_start_end_lines_trim.json``) through the REAL Main Creator parser, with one
thing about the input or the reply changed.

Ways the re-filing could go wrong, each pinned below:
1. the T1 reply on one video still reaches the brief as `order` (the incident);
2. the re-filed trim is not what the existing start-line reader (KRI-628) and the order gate
   read: the phone would not cut, or the gate would still ask;
3. several videos, or a video plus a photo: the start / end may name clips, so the model's
   `order` must stand;
4. unquoted words (a place, "from Arnavutköy to Eminönü"), a one-word place pair with a key
   (KRI-837: a key-less one-word quote like "Cheers!" IS a line), a first / last clip pin or a
   route fact: about clips or places, never re-filed;
5. only a start line: no invented end line;
6. a `change` of an existing requirement loses its target id / version when re-filed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria.brief import apply_updates
from app.kria.opening_cut_ask import requirement_opening_quote
from app.services.choice_questions import CONFLICT_ORDER_BASIS, collect_conflicts
from app.services.creator_render_contract import build_render_contract

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "agent_evals"
    / "main_creator"
    / "kri789_one_take_start_end_lines_trim.json"
)
START = "Hi, I'm Karolina, I'm from Ecuador"
END = "I'm gonna be here all week"


def _case() -> tuple[dict, dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return copy.deepcopy(data["input"]), json.loads(data["raw_text"])


def _parse(agent_input: dict, raw: dict):
    model_input = MainCreatorInput.model_validate(agent_input)
    return MainCreatorAgent(None).parse(json.dumps(raw), model_input)  # type: ignore[arg-type]


def _order_update(raw: dict) -> dict:
    return next(u for u in raw["brief_updates"] if u["kind"] == "order")


def _start_end_updates(output) -> list:
    return [
        u
        for u in output.brief_updates
        if u.kind == "order" or (u.kind == "timing" and "start_line" in u.facts)
    ]


def test_t1_reply_on_one_video_is_recorded_as_a_trim() -> None:
    output = _parse(*_case())

    (trim,) = _start_end_updates(output)
    assert (trim.kind, trim.scope) == ("timing", "global")
    assert trim.facts == {"start_line": START, "end_line": END}
    assert trim.description and "Karolina" in trim.description
    # The stated length is its own requirement, untouched.
    assert [u.facts for u in output.brief_updates if u.kind == "timing"] == [
        {"duration_s": 35},
        {"start_line": START, "end_line": END},
    ]


def test_the_recorded_trim_is_what_the_start_cut_and_the_order_gate_read() -> None:
    agent_input, raw = _case()
    output = _parse(agent_input, raw)
    brief = apply_updates(None, output.brief_updates, source_turn_id="t1")
    trim = next(r for r in brief.live() if "start_line" in r.facts)

    # KRI-628's phone Talking start cut reads the same line off the requirement.
    assert requirement_opening_quote(trim) == START

    take = agent_input["capability_manifest"]["media"][0]["media_id"]
    snapshot = {"clip_assignments": [{"media_id": take, "kind": "video", "duration_s": 68.37}]}
    strategy = raw["action"]["strategy"]
    kinds = [c.kind for c in collect_conflicts(strategy, brief, snapshot)]
    assert CONFLICT_ORDER_BASIS not in kinds, kinds
    contract = build_render_contract(
        strategy, generation_id="kri789", brief=brief, media_snapshot=snapshot
    )
    assert not contract.unresolved, contract.unresolved
    assert not contract.order_required


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"media_id": "clip-t1-second", "kind": "video", "duration_s": 20.0}, id="2v"),
        pytest.param({"media_id": "clip-t1-photo", "kind": "image"}, id="video+photo"),
    ],
)
def test_more_than_one_clip_keeps_the_models_order(extra: dict) -> None:
    agent_input, raw = _case()
    agent_input["capability_manifest"]["media"].append(extra)

    (kept,) = _start_end_updates(_parse(agent_input, raw))

    assert kept.kind == "order"
    assert kept.facts == {"start": START, "end": END}


def _unquoted(agent_input: dict, raw: dict) -> None:
    agent_input["user_message"] = agent_input["user_message"].replace('"', "")


def _one_word(agent_input: dict, raw: dict) -> None:
    agent_input["user_message"] = agent_input["user_message"].replace(f'"{END}"', '"week"')
    _order_update(raw)["facts"]["end"] = "week"


def _one_word_places_with_key(agent_input: dict, raw: dict) -> None:
    message = agent_input["user_message"]
    message = message.replace(f'"{START}"', '"Arnavutköy"').replace(f'"{END}"', '"Eminönü"')
    agent_input["user_message"] = message
    _order_update(raw)["facts"].update(key="capture_time", start="Arnavutköy", end="Eminönü")


def _one_word_end_with_key(agent_input: dict, raw: dict) -> None:
    _one_word(agent_input, raw)
    _order_update(raw)["facts"]["key"] = "capture_time"


def _pin(agent_input: dict, raw: dict) -> None:
    _order_update(raw)["facts"]["first_clip"] = "the video where she waves"


def _route(agent_input: dict, raw: dict) -> None:
    _order_update(raw)["facts"]["distance_km"] = 5


@pytest.mark.parametrize(
    "change",
    [_unquoted, _one_word_places_with_key, _one_word_end_with_key, _pin, _route],
    ids=lambda f: f.__name__,
)
def test_anything_but_quoted_lines_keeps_the_models_order(change) -> None:
    agent_input, raw = _case()
    change(agent_input, raw)
    want = dict(_order_update(raw)["facts"])

    (kept,) = _start_end_updates(_parse(agent_input, raw))

    assert kept.kind == "order"
    assert kept.facts == want


def test_a_quoted_one_word_end_without_a_key_is_a_line_too() -> None:
    """KRI-837: "end right after the "Cheers!"" is a line; only a key makes it a place."""
    agent_input, raw = _case()
    _one_word(agent_input, raw)

    (trim,) = _start_end_updates(_parse(agent_input, raw))

    assert trim.kind == "timing"
    assert trim.facts == {"start_line": START, "end_line": "week"}


def test_a_start_line_alone_invents_no_end() -> None:
    agent_input, raw = _case()
    del _order_update(raw)["facts"]["end"]

    (trim,) = _start_end_updates(_parse(agent_input, raw))

    assert trim.kind == "timing"
    assert trim.facts == {"start_line": START}


def test_a_change_keeps_its_target() -> None:
    agent_input, raw = _case()
    order = _order_update(raw)
    order.update(operation="change", target_requirement_id="r2", expected_version=1)

    (trim,) = _start_end_updates(_parse(agent_input, raw))

    assert (trim.operation, trim.kind) == ("change", "timing")
    assert (trim.target_requirement_id, trim.expected_version) == ("r2", 1)
    assert trim.facts == {"start_line": START, "end_line": END}
