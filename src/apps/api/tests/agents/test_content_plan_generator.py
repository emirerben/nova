"""Unit tests for nova.plan.content_plan_generator (no network, no DB).

Covers the parse() guarantees that keep partial garbage out of the DB:
range-clamping, duplicate-day dedup, empty-field drop, sort, and refusal when
nothing valid survives.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agents._runtime import RefusalError, SchemaError
from app.agents._schemas.content_plan import ContentPlanInput
from app.agents._schemas.persona import Persona
from app.agents.content_plan_generator import ContentPlanGeneratorAgent


def _agent() -> ContentPlanGeneratorAgent:
    return ContentPlanGeneratorAgent(MagicMock())


def _input(horizon: int = 30) -> ContentPlanInput:
    return ContentPlanInput(
        persona=Persona(
            summary="s",
            content_pillars=["a", "b"],
            tone="warm",
            audience="students",
            posting_cadence="4/week",
            sample_topics=["x"],
        ),
        events="",
        horizon_days=horizon,
    )


def _items(*specs: tuple[int, str, str]) -> str:
    return json.dumps({"items": [{"day_index": d, "theme": t, "idea": i} for d, t, i in specs]})


def test_parse_happy_path_sorted() -> None:
    out = _agent().parse(_items((3, "t3", "i3"), (1, "t1", "i1"), (2, "t2", "i2")), _input())
    assert [it.day_index for it in out.items] == [1, 2, 3]


def test_parse_drops_out_of_range_days() -> None:
    out = _agent().parse(_items((1, "ok", "ok"), (99, "too", "far")), _input(horizon=30))
    assert [it.day_index for it in out.items] == [1]


def test_parse_dedupes_duplicate_days_keeping_first() -> None:
    out = _agent().parse(_items((1, "first", "first"), (1, "second", "second")), _input())
    assert len(out.items) == 1
    assert out.items[0].theme == "first"


def test_parse_drops_items_with_empty_fields() -> None:
    out = _agent().parse(_items((1, "ok", "ok"), (2, "", "no theme")), _input())
    assert [it.day_index for it in out.items] == [1]


def test_parse_refuses_when_nothing_valid() -> None:
    with pytest.raises(RefusalError):
        _agent().parse(_items((99, "x", "y")), _input(horizon=30))


def test_parse_rejects_missing_items_array() -> None:
    with pytest.raises(SchemaError):
        _agent().parse(json.dumps({"nope": []}), _input())
