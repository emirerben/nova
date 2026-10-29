"""Random / "any order" clip orders (29 Ekim lane B).

Ways this can fail, written before the tests:
1. A negated or mixed ask ("not random", "don't shuffle", "chronological, not in any order",
   "order doesn't matter but start with the beach") is read as a plain shuffle / any-order and
   stamped permanently onto the stored requirement.
2. A structured ``facts.key`` loses to the creator's words (or the other way round).
3. The seeded order changes between runs, equals the input order, or drops/repeats a clip.
4. The receipt says "met" for an order that is not the stored seed's permutation.
5. A creator's hand reorder is silently overridden by a still-open random ask, or the receipt
   reports it unmet.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.order_mode import (
    order_mode,
    seeded_permutation,
    stamp_order_facts,
)
from tests.pipeline.test_unified_montage import clip


def _req(description: str | None, facts: dict | None = None, kind: str = "order"):
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, facts=facts or {}
    )


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Show all content in random order", "random"),
        ("shuffle them", "random"),
        ("Rastgele sırayla göster", "random"),
        ("any order is fine", "any"),
        ("no particular order", "any"),
        ("order doesn’t matter", "any"),
        ("sıra fark etmez", "any"),
    ],
)
def test_plain_asks_are_recognised(description, expected):
    assert order_mode(_req(description)) == expected


@pytest.mark.parametrize(
    "description",
    [
        "keep the order I added them, not random",
        "don't shuffle the clips",
        "no random order please",
        "sıra karışık olmasın",
        "chronological, not in any order",
        "order doesn't matter much but start with the beach",
        "random order but end with the sunset",
    ],
)
def test_negated_or_mixed_asks_stay_unknown(description):
    assert order_mode(_req(description)) is None
    assert stamp_order_facts("order", description, None, {}, req_id="r1", turn_id="t") == {}


def test_structured_key_decides_over_the_words():
    assert order_mode(_req("keep my order, not random", {"key": "random"})) == "random"
    assert order_mode(_req("Show all content in random order", {"key": "capture_time"})) is None
    assert order_mode(_req("whatever", {"key": "any_order"}, kind="select")) is None


def test_plain_ask_is_stamped_once_with_a_stable_seed():
    first = stamp_order_facts("order", "random order", None, {}, req_id="r1", turn_id="t1")
    again = stamp_order_facts("order", "random order", None, dict(first), req_id="r9", turn_id="t9")
    assert first["key"] == "random"
    assert again["seed"] == first["seed"]


def test_seeded_permutation_is_stable_complete_and_never_the_input():
    items = [f"c{i}" for i in range(27)]
    once = seeded_permutation(items, 7)
    assert once == seeded_permutation(items, 7)
    assert sorted(once) == sorted(items) and once != items
    assert seeded_permutation(items, 8) != once
    assert seeded_permutation(["a", "b"], 0) == ["b", "a"]


def _brief(description="Show all content in random order", **facts):
    req = _req(description, {"key": "random", "seed": 11, **facts})
    return CreativeBrief(version=1, requirements=[req]), req


def _plan(brief, creator_order=()):
    clips = [clip(i) for i in range(6)]
    return clips, plan_unified_montage(
        clips, brief_view(brief), strategy={}, creator_order=creator_order
    )


def test_montage_follows_the_stored_seed_and_the_receipt_proves_it():
    brief, req = _brief()
    clips, plan = _plan(brief)
    source = [c.media_id for c in clips]
    assert plan.ordering_basis == "random"
    assert plan.clip_ids == seeded_permutation(source, 11)
    assert plan.clip_ids == _plan(brief)[1].clip_ids
    receipt = build_receipts(
        [req], plan_facts_from_unified_montage(plan.record()), strict_order=True
    )
    assert receipt[0].status == "met"


def test_receipt_rejects_an_order_that_is_not_the_seeds_permutation():
    brief, req = _brief()
    record = _plan(brief)[1].record()
    record["clip_ids"] = list(reversed(record["clip_ids"]))
    receipt = build_receipts([req], plan_facts_from_unified_montage(record), strict_order=True)
    assert receipt[0].status == "not_possible"


def test_creator_hand_reorder_wins_and_the_receipt_says_so():
    brief, req = _brief()
    clips, plan = _plan(brief, creator_order=[5, 4, 3, 2, 1, 0])
    assert plan.ordering_basis == "creator_order"
    assert plan.clip_ids == [c.media_id for c in reversed(clips)]
    receipt = build_receipts(
        [req], plan_facts_from_unified_montage(plan.record()), strict_order=True
    )
    assert receipt[0].status == "met"
    assert "yourself" in str(receipt[0].reason)
