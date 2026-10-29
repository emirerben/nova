"""29 Ekim lane E (KRI-1039): compose with the editor ops instead of refusing.

Written from the failure list, before the code was trusted:
* the flag-off path changed (routing, fast-path set, replan cues, op shape, windows);
* a sequence's windows leave a gap or overlap (they must tile the source window exactly);
* weighted windows are not proportional, or a one-letter word gets a zero-length window;
* a cumulative build-up drops the full sentence, loses the hold at the end, or its steps are
  not prefixes of each other;
* the evidence layer reads a cumulative chain as one long wrong sentence;
* a text_sequence ask turns into a creator question (a checked, unmet receipt) when it simply
  has not been composed yet;
* a follow-up after a FAILED latest render still reaches nova.creator.main although an editor
  target exists.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief, CurrentPlanShape, route_requirements
from app.kria.brief_checks import PlanFacts, build_receipts, check_requirement, is_judged
from app.kria.contracts import KriaTurnPlan
from app.pipeline.sequence_text_evidence import complete_sequences
from app.services.creation_text_composition import _check_copy
from app.services.kria_editor_ops import compile_editor_ops
from tests.agents.test_editor_text_sequence import _job, _op, _snapshot, _variant
from tests.test_edit_copilot import _parse

REPO = Path(__file__).resolve().parents[5]


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_29_ekim", REPO / "scripts" / "kria" / "score_29_ekim.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flag(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", value)


def _compile(segments: list[str], monkeypatch, *, flag: bool, **extra) -> list[dict]:
    _flag(monkeypatch, flag)
    parsed = _parse([_op(segments, **extra)], snapshot=_snapshot(" ".join(segments)))
    variant = _variant(" ".join(segments))
    compiled = compile_editor_ops(_job(variant), variant, parsed.ops)
    return compiled.payload.text_elements


def _tiles(rows: list[dict], start: float = 0.0, end: float = 9.0) -> bool:
    edges = [(r["start_s"], r["end_s"]) for r in rows]
    return (
        edges[0][0] == start
        and edges[-1][1] == end
        and all(a[1] == b[0] for a, b in zip(edges, edges[1:], strict=False))
    )


def test_compose_unmet_flag_off_is_byte_identical(monkeypatch) -> None:
    _flag(monkeypatch, False)
    # The op keeps its legacy shape and equal windows, whatever the model asked for.
    rows = _compile(["one", "two", "three"], monkeypatch, flag=False, mode="cumulative")
    assert [r["text"] for r in rows] == ["one", "two", "three"]
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]
    # Routing helpers: the fast-path set and the replan cues are the earlier ones.
    replace_sequence = KriaTurnPlan.model_validate(
        {
            "mode": "act",
            "turn_value": "action",
            "intents": [
                {
                    "intent_id": "i",
                    "tool_name": "draft.apply_editor_ops",
                    "tool_version": 1,
                    "arguments": {"operations": [{"op": "replace_text_sequence"}]},
                }
            ],
        }
    )
    assert planner._is_fast_path_plan(replace_sequence) is False
    assert planner._fast_path_eligible("make the font different") is False
    # A turn after a FAILED latest render is a fresh plan: Main Creator, no copilot.
    card = _scorecard()
    fx = card.load_fixture()
    with card._prod_flags():
        legacy = asyncio.run(card.replay_followup_turn(fx, "T5", job_status="processing_failed"))
        ready = asyncio.run(card.replay_followup_turn(fx, "T5", job_status="variants_ready"))
    assert legacy["model_calls"] == ["nova.creator.main"]
    assert ready["model_calls"] == ["nova.brief.extractor", "nova.edit.copilot"]


def test_followup_after_a_failed_render_reaches_the_copilot(monkeypatch) -> None:
    _flag(monkeypatch, True)
    card = _scorecard()
    fx = card.load_fixture()
    with card._prod_flags():
        replay = asyncio.run(card.replay_followup_turn(fx, "T5", job_status="processing_failed"))
    assert replay["model_calls"] == ["nova.brief.extractor", "nova.edit.copilot"]
    assert replay["route"] == "editor_ops" and replay["ops"] == ["replace_text_sequence"]
    assert planner._fast_path_eligible("make the font different") is True
    assert planner._fast_path_eligible("give me another version") is False


def test_weighted_windows_tile_the_source_and_follow_character_count(monkeypatch) -> None:
    words = ["a", "wonderful", "day"]
    rows = _compile(words, monkeypatch, flag=True)
    assert _tiles(rows)
    spans = [r["end_s"] - r["start_s"] for r in rows]
    assert spans[1] > spans[2] > spans[0] > 0  # 9 chars > 3 chars > 1 char, none zero
    # (len + 2) / total: 3, 11, 5 of 19
    assert spans[1] / sum(spans) == pytest.approx(11 / 19, abs=1e-5)


def test_equal_weight_is_still_available(monkeypatch) -> None:
    rows = _compile(["one", "two", "three"], monkeypatch, flag=True, weight="equal")
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]


def test_cumulative_builds_up_and_holds_the_full_sentence(monkeypatch) -> None:
    words = ["Bu", "güzel", "vatan", "için"]
    rows = _compile(words, monkeypatch, flag=True, mode="cumulative")
    assert [r["text"] for r in rows] == ["Bu", "Bu güzel", "Bu güzel vatan", "Bu güzel vatan için"]
    assert _tiles(rows)
    hold = rows[-1]["end_s"] - rows[-1]["start_s"]
    assert hold == pytest.approx(9.0 * 0.25, abs=1e-5)  # the full sentence holds to the end
    assert all((r["source_params"] or {}).get("sequence_mode") == "cumulative" for r in rows)


def test_a_cumulative_chain_is_read_as_its_last_step_not_a_longer_sentence(monkeypatch) -> None:
    rows = _compile(["Bu", "güzel", "vatan"], monkeypatch, flag=True, mode="cumulative")
    composed = complete_sequences(rows)
    assert [c["text"] for c in composed] == ["Bu güzel vatan"]
    # A chain whose steps are not prefixes of each other is not a cumulative sentence.
    broken = [dict(r) for r in rows]
    broken[1] = {**broken[1], "text": "something else"}
    assert complete_sequences(broken) == []
    # The confirmed-copy check accepts the chain as the one sentence it is.
    _check_copy(
        [{**r, "id": f"guided-title::sequence-{i + 1}"} for i, r in enumerate(rows)],
        {"guided-title": "Bu güzel vatan"},
    )


def test_an_uncomposed_text_sequence_never_becomes_a_question() -> None:
    brief = CreativeBrief.model_validate(
        {
            "version": 1,
            "requirements": [
                {
                    "id": "r1",
                    "kind": "text_sequence",
                    "scope": "title",
                    "facts": {"unit": "word", "mode": "agent_choice"},
                    "status": "open",
                    "literal": "Bu güzel vatan",
                    "description": "Show the words one at a time",
                }
            ],
        }
    )
    req = brief.live()[0]
    static_title = PlanFacts(texts=("Bu güzel vatan",), title="Bu güzel vatan", editor=True)
    receipt = check_requirement(req, static_title)
    assert receipt.status == "partial" and not is_judged(req, receipt)
    assert build_receipts(brief.live(), static_title, include_unchecked=True)[0].verification == (
        "unchecked"
    )
    shown = PlanFacts(
        texts=("Bu", "güzel", "vatan", "Bu güzel vatan"),
        sequence_texts=("Bu güzel vatan",),
        editor=True,
    )
    assert check_requirement(req, shown).status == "met"


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        (CurrentPlanShape(has_render=True, can_edit_text=True), "editor_ops"),
        (CurrentPlanShape(has_render=True, can_edit_text=False), "replan"),
        (CurrentPlanShape(has_render=False), "replan"),
    ],
)
def test_a_text_sequence_ask_routes_to_the_editor_when_there_is_text_to_edit(
    shape, expected
) -> None:
    brief = CreativeBrief.model_validate(
        {
            "version": 1,
            "requirements": [
                {
                    "id": "r1",
                    "kind": "text_sequence",
                    "scope": "title",
                    "facts": {"unit": "word", "mode": "agent_choice"},
                    "status": "open",
                    "literal": "Bu güzel vatan",
                    "description": "Show the words one at a time",
                }
            ],
        }
    )
    assert route_requirements(brief.live(), shape, message="split it word by word") == expected


def test_replace_text_sequence_rides_the_fast_path_when_composing(monkeypatch) -> None:
    _flag(monkeypatch, True)
    plan = KriaTurnPlan.model_validate(
        {
            "mode": "act",
            "turn_value": "action",
            "intents": [
                {
                    "intent_id": "i",
                    "tool_name": "draft.apply_editor_ops",
                    "tool_version": 1,
                    "arguments": {"operations": [json.loads('{"op": "replace_text_sequence"}')]},
                }
            ],
        }
    )
    assert planner._is_fast_path_plan(plan) is True
