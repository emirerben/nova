"""Green 4 / Green 5 (2026-11-04): the Talk-to-camera draft's "Heads-up for this video" lines.

Both drafts ended with lines about things the creator never asked for, using the planner's raw
labels: "The balanced pacing isn't applied on this video yet", "The native direction isn't
applied …", the planner's own hook idea and story structure. The Turkish card kept the labels in
English ("balanced tempo …", "native yönü …"). The creator's asks were green screen, cleanup,
caption look and no music; none is about pacing, direction, an opening hook or a story order.

Ways the fix could fail (written before the fix):
1. A line for a planner-only field still shows (pacing, direction, hook, story) on the Talking
   route although no live ask is about it.
2. Dropping the lines also drops a disclosure the creator DID ask for: a stated length the route
   can't render (the existing `stated_length` gate) must keep its line.
3. The card still shows the planner's "Balanced pacing" chip while the route ignores pacing, so
   the card claims a pace nobody asked for and nothing follows (the silence KRI-1097 removed).
4. A remaining line still carries a raw enum ("native", "balanced", "fast_montage") or, in
   Turkish, an English word.
5. The switch off is not byte-identical to before (the four lines come back exactly).

Evidence level: offline replay of the real plans (`green45_headsup.json`), no model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement
from app.kria.reply_language import reply_language_for
from app.kria.route_disclosure import card_disclosures, card_text

FIXTURE = json.loads((Path(__file__).parent / "green45_headsup.json").read_text())
TALKING = "talking_head"
PLANNER_ONLY = {"pacing", "direction", "intro_hook", "story_structure"}
RAW_LABELS = ("native", "balanced", "fast montage", "fast_montage")


def _requirements(case: dict) -> list[BriefRequirement]:
    return [BriefRequirement.model_validate(raw) for raw in case["requirements"]]


def test_green4_card_names_nothing_the_creator_did_not_ask_for() -> None:
    case = FIXTURE["green4"]
    disclosures = card_disclosures(_requirements(case), case["strategy"], route=TALKING)
    assert not {d.subject for d in disclosures} & PLANNER_ONLY, card_text(disclosures)


def test_green5_turkish_card_names_nothing_unasked_and_no_english_labels() -> None:
    case = FIXTURE["green5_chip"]
    with reply_language_for("tr"):
        disclosures = card_disclosures(_requirements(case), case["strategy"], route=TALKING)
        text = card_text(disclosures)
    assert not {d.subject for d in disclosures} & PLANNER_ONLY, text
    assert not any(label in text.casefold() for label in RAW_LABELS), text


def test_a_length_the_creator_asked_for_is_still_disclosed() -> None:
    """Negative control: an ask the route cannot follow keeps its line."""
    case = FIXTURE["green4"]
    strategy = {**case["strategy"], "creator_duration_s": 15, "target_duration_s": 15}
    disclosures = card_disclosures(_requirements(case), strategy, route=TALKING)
    assert "target_duration_s" in {d.subject for d in disclosures}, card_text(disclosures)


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_card_chips_do_not_claim_an_unasked_pace_the_route_ignores(language: str) -> None:
    from app.tasks.kria_runtime import _strategy_changes

    case = FIXTURE["green4"]
    arguments = SimpleNamespace(
        summary=None,
        strategy=SimpleNamespace(pacing=case["strategy"]["pacing"], edit_format="subtitled"),
    )
    with reply_language_for(language):
        try:
            chips = _strategy_changes(arguments, route=TALKING)
        except TypeError:  # before the fix the chips know no route: judge what they say
            chips = _strategy_changes(arguments)
    assert not any("pacing" in c.casefold() or "tempo" in c.casefold() for c in chips), chips
