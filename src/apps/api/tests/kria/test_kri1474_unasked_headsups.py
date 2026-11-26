"""KRI-1474: the draft's "Heads-up for this video" names Kria's own ideas the creator never asked
for, three runs after the Green 4/5 asked-only rule shipped.

* Cusco T5 "Cusco'da bir gün" (Montage; asked: montage, all clips, fill, 20 s, a title):
  the opening hook idea, the planned story structure and "the day vlog story archetype".
* Cusco T2 "list 1" (Talking; asked: four labels in the creator's words, Turkish captions):
  the opening hook idea.
* Roasted carrots "Tahini" (Voiceover; asked: a title in the creator's words, their own opening
  order): the opening hook idea (its story-structure line stays: see 3).

Ways the fix could fail (written before the fix):
1. A title or label the creator wrote word for word still counts as asking for Kria's hook
   idea, so the hook line stays (T2, T5, Tahini, and the original Q1/Q2 title runs).
2. The story-structure line still reaches the card through the "story will be built" path,
   which skipped the asked-only rule (T5).
3. Over-suppression: a route that ignores the story structure may drop a clip order the
   creator asked for, so with a live order ask the line must stay (Tahini; the route-replay
   scorer's own control holds the same rule).
4. The story archetype (a planner field nobody can ask for by type) still has a line on the
   draft and, as the last one left, on the approval card (T5).
5. Over-suppression: an opening text the creator asks Kria to write (no words given) loses the
   hook line, though the hook idea is exactly what would fill it.
6. Over-suppression: a disclosure about something the creator did ask (a stated length the route
   can't render) disappears.
7. The replay is not faithful: with the switch off it does not reproduce the recorded lines, so a
   pass would prove nothing.
8. The switch off (`kria_headsup_asked_only_enabled`) is not byte-identical to before.

Evidence level: offline replay of the three runs' real plans and briefs
(`kri1474_unasked_headsups.json`), no model call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.route_disclosure import (
    approval_card_disclosures,
    card_disclosures,
    card_text,
)

FIXTURE = json.loads((Path(__file__).parent / "kri1474_unasked_headsups.json").read_text())
RUNS = ("cusco_t5", "cusco_t2", "tahini")
UNASKED = {"intro_hook", "story_structure", "archetype"}
# Tahini's creator asked for an order: its story-structure line is the one that must stay.
KEPT = {"cusco_t5": set(), "cusco_t2": set(), "tahini": {"story_structure"}}


@pytest.fixture(autouse=True)
def _prod_phones_draw_crossfades(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod phones are verified for the crossfade, so T5's guided story is built and its line
    came from the story-lines path (failure mode 2). Locally the list may lack it."""
    features = set(settings.phone_render_verified_features) | {"crossfade"}
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _replay(name: str, strategy_patch: dict | None = None, extra: list[dict] | None = None):
    case = FIXTURE[name]
    requirements = [
        BriefRequirement.model_validate(raw) for raw in [*case["requirements"], *(extra or [])]
    ]
    strategy = {**case["strategy"], **(strategy_patch or {})}
    return card_disclosures(requirements, strategy, route=case["route"])


@pytest.mark.parametrize("name", RUNS)
def test_the_draft_names_none_of_kria_s_own_ideas(name: str) -> None:
    disclosures = _replay(name)
    subjects = {d.subject for d in disclosures}
    assert subjects & UNASKED == KEPT[name], card_text(disclosures)


def test_t5_approval_card_has_no_archetype_line() -> None:
    disclosures = _replay("cusco_t5")
    card = approval_card_disclosures(disclosures)
    assert "archetype" not in {d.subject for d in card}, card_text(card)


def test_an_opening_text_kria_is_asked_to_write_keeps_the_hook_line() -> None:
    """5: "add a catchy opening line" (no words given) is the creator asking for the hook."""
    ask = {
        "id": "r9",
        "kind": "text",
        "scope": "title",
        "literal": None,
        "description": "Add a catchy opening line at the start.",
        "status": "open",
    }
    disclosures = _replay("cusco_t2", extra=[ask])
    assert "intro_hook" in {d.subject for d in disclosures}, card_text(disclosures)


def test_a_length_the_creator_asked_for_is_still_disclosed() -> None:
    """6: an ask the route cannot follow keeps its line (T2's Talking route, a stated 15 s)."""
    disclosures = _replay(
        "cusco_t2", strategy_patch={"creator_duration_s": 15, "target_duration_s": 15}
    )
    assert "target_duration_s" in {d.subject for d in disclosures}, card_text(disclosures)


@pytest.mark.parametrize("name", RUNS)
def test_switch_off_reproduces_the_recorded_lines(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7 + 8: off, the replay gives back every line the run's draft recorded, word for word."""
    monkeypatch.setattr(settings, "kria_headsup_asked_only_enabled", False)
    got = {(d.subject, d.text) for d in _replay(name)}
    recorded = {(row["subject"], row["text"]) for row in FIXTURE[name]["recorded_not_applied"]}
    assert recorded <= got, sorted(recorded - got)
