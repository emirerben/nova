"""Unit tests for app/services/content_plan_dedup (pure — no network, no DB)."""

from __future__ import annotations

from app.agents._schemas.content_plan import PlanItemSpec
from app.services.content_plan_dedup import (
    choose_replacements,
    flag_replacement_indices,
    idea_similarity,
)


def _item(day: int, idea: str, theme: str = "pillar") -> PlanItemSpec:
    return PlanItemSpec(day_index=day, theme=theme, idea=idea)


def test_near_duplicate_ideas_score_high() -> None:
    sim = idea_similarity(
        "5am gym workout motivation routine",
        "early morning gym workout motivation routine",
    )
    assert sim >= 0.72


def test_distinct_ideas_sharing_stopwords_score_low() -> None:
    # Both are prose with the same function words but different concepts.
    sim = idea_similarity(
        "a tour of my favorite coffee shops in the city",
        "a guide to the best hiking trails in the mountains",
    )
    assert sim < 0.72


def test_flag_keeps_first_occurrence_of_a_cluster() -> None:
    items = [
        _item(1, "5am gym workout motivation routine"),
        _item(2, "my favorite weekend brunch spots downtown"),
        _item(3, "early morning gym workout motivation routine"),  # dup of day 1
    ]
    flagged = flag_replacement_indices(items)
    assert flagged == [2]  # the LATER member, never the first


def test_distinct_plan_flags_nothing() -> None:
    items = [
        _item(1, "tour of my favorite coffee shops"),
        _item(2, "best hiking trails near me"),
        _item(3, "how I meal prep for the week"),
    ]
    assert flag_replacement_indices(items) == []


def test_max_fraction_cap_limits_flag_count() -> None:
    # Ten identical ideas: without a cap, 9 would flag. Cap at 0.4 → floor(10*0.4)=4.
    items = [_item(d, "5am gym workout motivation routine") for d in range(1, 11)]
    flagged = flag_replacement_indices(items, max_fraction=0.4)
    assert len(flagged) == 4


def test_choose_replacements_returns_only_distinct_candidates() -> None:
    avoid = ["5am gym workout motivation routine"]
    candidates = [
        _item(1, "early morning gym workout motivation routine"),  # ~dup of avoid → skip
        _item(2, "a tour of my favorite coffee shops downtown"),  # distinct → take
        _item(3, "a guide to my favorite coffee shops downtown"),  # ~dup of #2 → skip
        _item(4, "how I meal prep lunches for the week"),  # distinct → take
    ]
    chosen = choose_replacements(2, candidates, avoid)
    assert [c.day_index for c in chosen] == [2, 4]


def test_choose_replacements_degrades_when_pool_too_small() -> None:
    avoid = ["coffee shop tour downtown"]
    candidates = [_item(1, "best coffee shops downtown tour")]  # only ~dup available
    assert choose_replacements(3, candidates, avoid) == []


def test_choose_replacements_carries_all_content_fields() -> None:
    cand = PlanItemSpec(
        day_index=9,
        theme="cooking",
        idea="one continuous shot of plating a pasta dish",
        filming_suggestion="overhead, single take",
        rationale="save-worthy single hero shot",
        edit_format="single_hero",
    )
    chosen = choose_replacements(1, [cand], avoid_ideas=["unrelated travel vlog"])
    assert len(chosen) == 1
    assert chosen[0].edit_format == "single_hero"
    assert chosen[0].rationale == "save-worthy single hero shot"
