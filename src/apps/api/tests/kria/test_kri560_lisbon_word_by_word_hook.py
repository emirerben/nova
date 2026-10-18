"""KRI-560: the Lisbon "typewriter, word by word" hook, replayed from the prod trace.

Prod job 1e2d4937 (thread 92154c24, 2026-10-09 06:47 UTC) was approved and then refused with
``phone_plan_unsupported`` / ``opening_title`` ("missing confirmed on-screen text"). The phone
CAN draw the hook: the plan splits it into 13 one-word fragments (``guided-title::sequence-N``)
shown one after the other. The refusal was a plan-shape bug -- the phone compiler dropped the
fragment ids, so the pin-time verifier saw 13 unrelated words and no title (fixed by #1511).

The fixture is that job's real plan, brief and source descriptors (ids scrubbed, hashes
placeholders). Ways this can fail again, written down before the code:

* the compiler renames fragment layers, so the pin-time check loses the lineage (the prod bug);
* the pin-time check accepts a fragment run that is incomplete, out of order, or has a gap;
* the finished-video review reads the same fragments one row at a time and tells the creator
  "That exact text isn't in this draft" about a hook that is on screen (found while replaying
  this trace: the review still said it after #1511);
* the review over-claims: it calls the hook shown when a word was removed or the run is broken.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from app.kria.brief import CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding

FIXTURE = Path(__file__).parent.parent / "fixtures" / "kri560_lisbon_word_by_word_hook.json"
HOOK = "come with me to my favorite restaurant and ice cream place in lisbon"


@pytest.fixture
def trace() -> dict:
    return json.loads(FIXTURE.read_text())


def _compiled(trace: dict):
    plan = GuidedStoryExecutionPlan.model_validate(trace["guided_story_execution_plan"])
    bindings = tuple(
        PhoneSourceBinding(
            media_id=row["media_id"],
            proxy_path=row["proxy_path"],
            generation=row["generation"],
            original=OriginalMediaDescriptor.model_validate(row["original"]),
        )
        for row in trace["media"]
    )
    return compile_phone_guided_plan(plan, bindings)


def _contract(trace: dict) -> CreatorRenderContract:
    return CreatorRenderContract(generation_id="g").rebind(
        exact_texts=tuple(TextRequirement(**row) for row in trace["exact_texts"]),
        order_required=trace["order_required"],
        order_ids=tuple(trace["order_ids"]),
    )


def _hook_receipt(text_elements: list[dict], trace: dict):
    facts = plan_facts_from_phone_variant(
        {"text_elements": text_elements, "resolved_archetype": "guided_story"}
    )
    brief = CreativeBrief.model_validate(trace["brief"])
    receipts = {r.requirement_id: r for r in build_receipts(brief.live(), facts)}
    return receipts["r2"]  # r2 = the typewriter / word-by-word hook


def test_trace_is_the_prod_shape(trace: dict) -> None:
    rows = trace["guided_story_execution_plan"]["text_elements"]
    hook = [row for row in rows if row["id"].startswith("guided-title::sequence-")]
    assert [row["text"] for row in hook] == HOOK.split()
    assert {row["text"] for row in trace["exact_texts"][:1]} == {HOOK}
    assert uuid.UUID("92154c24-afb6-4934-881e-5d99c72900f4").hex not in json.dumps(trace)


def test_approved_lisbon_hook_passes_the_phone_pin_check(trace: dict) -> None:
    recipe = _compiled(trace)

    # The compiler keeps the fragment lineage, so the verifier can read the 13 words as one hook.
    assert [layer.id for layer in recipe.text_layers[:13]] == [
        f"text-guided-title::sequence-{n}" for n in range(1, 14)
    ]
    # Before #1511 this raised "This edit is missing confirmed on-screen text." (opening_title).
    verify_phone_recipe(_contract(trace), recipe)


def test_pin_check_still_refuses_a_hook_with_a_word_missing(trace: dict) -> None:
    broken = copy.deepcopy(trace)
    rows = broken["guided_story_execution_plan"]["text_elements"]
    broken["guided_story_execution_plan"]["text_elements"] = [
        row for row in rows if row["id"] != "guided-title::sequence-7"
    ]

    with pytest.raises(Exception, match="missing confirmed on-screen text") as raised:
        verify_phone_recipe(_contract(broken), _compiled(broken))
    assert raised.value.field_path == "opening_title"
    assert raised.value.missing_text == HOOK


def test_review_of_the_finished_lisbon_edit_sees_the_hook(trace: dict) -> None:
    rows = trace["guided_story_execution_plan"]["text_elements"]

    receipt = _hook_receipt(rows, trace)

    # Before this fix: partial, "That exact text isn't in this draft." -- about a hook on screen.
    assert (receipt.status, receipt.reason) == ("met", None)


@pytest.mark.parametrize("damage", ["word_removed", "word_gone", "gap"])
def test_review_does_not_call_a_broken_hook_shown(trace: dict, damage: str) -> None:
    rows = copy.deepcopy(trace["guided_story_execution_plan"]["text_elements"])
    seventh = next(row for row in rows if row["id"] == "guided-title::sequence-7")
    if damage == "word_removed":
        seventh["removed"] = True
    elif damage == "word_gone":
        rows.remove(seventh)
    else:
        seventh["start_s"] += 1.0
        seventh["end_s"] += 1.0

    receipt = _hook_receipt(rows, trace)

    assert receipt.status == "partial"
    assert receipt.reason == "That exact text isn't in this draft."
