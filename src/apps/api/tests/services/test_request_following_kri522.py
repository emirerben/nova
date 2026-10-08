"""KRI-522: three plain asks the creator typed were dropped, and the receipts said "met".

"Start with the video that is blue. Add a hook there, animated with typewriter. Then show
everything chronologically. Add placeholder location to each video to the bottom left."
The final montage had the blue clip 7th, a static title and real place names centred at the
bottom, because the creator's words were paraphrased into a brief with no fields for the start
clip, the animation, the corner or the placeholder, and the next turn re-planned from the
paraphrase alone.

Failure modes, written before the code (each is a test below):

* a later turn's planner drops the start clip: the brief must still pin it (contract question)
* the receipt reads "met" for an order whose stated first clip was never seated
* the animation / corner / placeholder live only in prose and never reach the renderer
* the planner's placeholder is lost on a replan, so labels turn into footage-read place names
* the brief-on planner is given only the brief paraphrase, never the creator's own words
* a plain montage (no style asks) changes a single byte of its text elements
* an unknown animation / corner word raises instead of falling back to the default look

Everything about the render runs the REAL planner, guided compiler and phone compiler.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.schemas.text_style_intent import normalize_label_position, normalize_title_animation
from tests.services.test_order_sequence_contract import (
    TEAL,
    _brief,
    _clips,
    _id,
    _intent,
    _plan,
    _recipe,
    _strategy,
    _world,
)

HOOK = "come with me to my favorite restaurant and ice cream place in lisbon"


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _anchored_brief() -> CreativeBrief:
    """The brief the extractor now writes for the incident message."""
    return CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description="chronological, starting with the teal video",
                facts={"key": "capture_time", "first_clip": "the teal video"},
            ),
            BriefRequirement(
                id="r2",
                kind="text",
                scope="title",
                literal=HOOK,
                description="a hook animated with typewriter",
                facts={"animation": "typewriter"},
            ),
            BriefRequirement(
                id="r3",
                kind="text",
                scope="per_clip",
                description="placeholder location at the bottom left",
                facts={"placeholder": True, "position": "bottom left"},
            ),
        ],
    )


def _incident_strategy() -> dict:
    """What the LATER turn's planner left in the strategy: a real place label intent, no
    placeholder, no start clip, no animation, no corner (the prod job's shape)."""
    strategy = _strategy()
    strategy["opening_title"] = HOOK
    strategy["resolved_clip_intents"].append(
        {
            "op": "label",
            "status": "resolved",
            "intent_id": "label_location",
            "attribute": "the location",
            "assignments": [
                {"media_id": _id(0), "value": "Lisbon street", "grounding": "record_span"},
                {"media_id": _id(1), "value": "Urban park square", "grounding": "record_span"},
            ],
        }
    )
    return strategy


def _text_elements(plan) -> dict[str, dict]:
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    return {element["id"]: element for element in compiled["text_elements"]}


# -- the brief outlives the turn: animation, corner and placeholder reach the pixels -----


def test_the_incident_asks_reach_the_compiled_text_even_though_the_strategy_dropped_them():
    plan = plan_unified_montage(
        _clips(),
        brief_view(_anchored_brief()),
        strategy=_incident_strategy(),
        clip_intents_enabled=True,
    )
    elements = _text_elements(plan)

    title = elements["guided-title"]
    assert title["text"] == HOOK
    assert title["animation_phases"]["entrance"] == "typewriter"

    labels = [e for key, e in elements.items() if key.startswith("clip-label-")]
    assert len(labels) == len(_clips())  # every clip, not only the ones with footage evidence
    assert {e["text"] for e in labels} == {"Name"}  # the placeholder, never a read place
    assert {(e["x_frac"], e["y_frac"], e["alignment"]) for e in labels} == {(0.08, 0.78, "left")}


def test_the_phone_recipe_types_the_title_and_pins_the_labels_left():
    plan = plan_unified_montage(
        _clips(),
        brief_view(_anchored_brief()),
        strategy=_incident_strategy(),
        clip_intents_enabled=True,
    )
    layers = _recipe(plan).model_dump(mode="json")["text_layers"]
    title, *labels = layers
    assert title["effect"] == "typewriter" and title["discrete_reveal"] is not None
    assert len(labels) == len(_clips())
    assert all(label["effect"] == "static" for label in labels)
    # Left-pinned: the anchor is the line's left edge, well inside the left third.
    width = 1920.0
    assert all(label["anchor_x"] < width / 4 for label in labels)
    assert all(label["anchor_y"] > 1080 * 0.7 for label in labels)


def test_the_strategys_typed_fields_work_without_any_brief():
    strategy = {**_strategy(), "opening_title": HOOK}
    strategy["title_animation"] = "typewriter"
    strategy["label_position"] = "bottom_right"
    strategy["shot_labels"] = ["One", "Two"]
    plan = plan_unified_montage(_clips(), strategy=strategy, clip_intents_enabled=True)
    elements = _text_elements(plan)
    assert elements["guided-title"]["animation_phases"]["entrance"] == "typewriter"
    label = next(e for key, e in elements.items() if key.startswith("clip-label-"))
    assert (label["x_frac"], label["alignment"]) == (0.92, "right")


def test_a_plain_montage_is_byte_identical():
    plain = plan_unified_montage(
        _clips(),
        brief_view(_brief()),
        strategy={**_strategy(), "opening_title": "Title", "shot_labels": ["One", "Two"]},
        clip_intents_enabled=True,
    )
    elements = _text_elements(plain)
    assert elements["guided-title"].get("animation_phases") is None
    label = next(e for key, e in elements.items() if key.startswith("clip-label-"))
    assert (label["x_frac"], label["y_frac"], label["alignment"]) == (0.5, 0.78, "center")
    snapshot = plain.snapshot.model_dump(mode="json")
    assert "title_animation" not in snapshot and "label_position" not in snapshot


def test_unknown_words_fall_back_to_the_default_look_instead_of_raising():
    assert normalize_title_animation("sparkly") is None
    assert normalize_label_position("somewhere nice") is None
    assert normalize_title_animation(None) is None
    assert normalize_label_position(["bottom left"]) is None
    assert normalize_title_animation("Type-Writer") == "typewriter"
    assert normalize_label_position("Bottom_Left") == "bottom_left"
    brief = _anchored_brief()
    brief.requirements[1].facts["animation"] = "sparkly"
    brief.requirements[2].facts["position"] = "somewhere nice"
    view = brief_view(brief)
    assert view.title_animation is None and view.label_position is None


# -- "starting with the teal video" survives a turn whose planner dropped it ----------------


def test_a_stated_start_clip_with_no_seated_intent_is_asked_about_not_silently_dropped():
    """The incident: the later turn's strategy has only the basis order. Before, the contract
    pinned pure filming order and the receipt read "met"."""
    _brief_, contract = _world(_strategy(), _anchored_brief())
    assert contract.order_required
    assert contract.order_ids == ()
    assert any("the teal video" in question for question in contract.unresolved)


def test_a_seated_start_clip_is_unchanged_by_the_brief_anchor():
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    _brief_, contract = _world(strategy, _anchored_brief())
    assert contract.order_ids[0] == _id(TEAL)
    assert not contract.unresolved


def test_the_receipt_does_not_read_met_when_the_stated_first_clip_was_never_seated():
    brief = _anchored_brief()
    unseated = _plan(_strategy(), brief)
    (order,) = [
        r
        for r in build_receipts(
            brief.live(), plan_facts_from_unified_montage(unseated.record()), strict_order=True
        )
        if r.requirement_id == "r1"
    ]
    assert order.status == "partial"
    assert "start with the teal video" in order.reason

    seated = _plan(_strategy(_intent("first", "the teal video", TEAL)), brief)
    (met,) = [
        r
        for r in build_receipts(
            brief.live(), plan_facts_from_unified_montage(seated.record()), strict_order=True
        )
        if r.requirement_id == "r1"
    ]
    assert met.status == "met"


def test_a_plain_chronological_brief_is_still_met_without_any_anchor():
    brief = _brief()
    plan = _plan(_strategy(), brief)
    (receipt,) = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), strict_order=True
    )
    assert receipt.status == "met"


# -- the planner gets the creator's own words, not only the brief's paraphrase ---------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_every_creator_message_is_loaded_in_order_for_the_clip_intent_planner():
    rows = [
        SimpleNamespace(role="user", content="Start with the video that is blue. Add a hook."),
        SimpleNamespace(role="user", content='The hook should say "come with me"'),
    ]
    request = await planner._load_raw_creator_request(
        _FakeDb(rows), thread_id=None, user_message='The hook should say "come with me"'
    )
    assert request == (
        'Start with the video that is blue. Add a hook.\nThe hook should say "come with me"'
    )


@pytest.mark.asyncio
async def test_an_over_long_request_falls_back_to_none_not_an_error():
    rows = [SimpleNamespace(role="user", content="x" * 13_000)]
    assert (
        await planner._load_raw_creator_request(_FakeDb(rows), thread_id=None, user_message="y")
        is None
    )


# -- the thread: the hook's wording arrives in a LATER turn ---------------------------------


def _turn_one() -> CreativeBrief:
    """What the extractor stores for the first message (the creator gave no hook wording)."""
    updates = [
        BriefUpdate(
            kind="order",
            scope="global",
            description="chronological, starting with the teal video",
            facts={"key": "capture_time", "first_clip": "the teal video"},
        ),
        BriefUpdate(
            kind="text",
            scope="title",
            description="a hook animated with typewriter",
            facts={"animation": "typewriter"},
        ),
        BriefUpdate(
            kind="text",
            scope="per_clip",
            description="placeholder location at the bottom left",
            facts={"placeholder": True, "position": "bottom left"},
        ),
    ]
    return apply_updates(None, updates, source_turn_id="t1").model_copy(update={"version": 1})


def _hook_wording_turn(prior: CreativeBrief, *, scope: str = "title", **facts) -> CreativeBrief:
    title = next(r for r in prior.live() if r.kind == "text" and r.scope == "title")
    change = BriefUpdate(
        operation="change",
        target_requirement_id=title.id,
        expected_version=prior.version,
        kind="text",
        scope=scope,
        literal=HOOK,
        description="a hook animated with typewriter",
        facts=facts,
    )
    return apply_updates(prior, [change], source_turn_id="t2")


def test_the_hook_wording_turn_keeps_the_typewriter_ask():
    """The incident: the extractor restated the title with the words and `facts: {}`."""
    brief = _hook_wording_turn(_turn_one())
    title = next(r for r in brief.live() if r.scope == "title")
    assert title.literal == HOOK
    assert title.facts == {"animation": "typewriter"}
    plan = plan_unified_montage(
        _clips(), brief_view(brief), strategy=_strategy(), clip_intents_enabled=True
    )
    assert _text_elements(plan)["guided-title"]["animation_phases"]["entrance"] == "typewriter"


def test_a_restated_fact_wins_and_a_moved_requirement_inherits_nothing():
    swapped = _hook_wording_turn(_turn_one(), animation="fade")
    assert next(r for r in swapped.live() if r.scope == "title").facts == {"animation": "fade"}
    moved = _hook_wording_turn(_turn_one(), scope="global")
    assert next(r for r in moved.live() if r.literal == HOOK).facts == {}


def test_the_whole_thread_ends_with_every_ask_in_the_plan_and_honest_receipts():
    brief = _hook_wording_turn(_turn_one())
    strategy = _strategy(_intent("first", "the teal video", TEAL))
    strategy["opening_title"] = HOOK
    plan = plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )
    assert plan.clip_ids[0] == _id(TEAL)
    elements = _text_elements(plan)
    assert elements["guided-title"]["animation_phases"]["entrance"] == "typewriter"
    assert {e["text"] for k, e in elements.items() if k.startswith("clip-label-")} == {"Name"}
    receipts = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), strict_order=True
    )
    order = next(r for r in receipts if r.requirement_id == "r1")
    assert order.status == "met"
