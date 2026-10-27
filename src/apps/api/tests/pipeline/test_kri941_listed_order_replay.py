"""KRI-941: the Don Julio walk-in M1 chat, replayed offline through the real planner and receipts.

Prod 2026-10-25, job b3820ddc, thread 33BA8AFB: "Start on the DON JULIO window sign ... end on
my friend ... saying 'Super excited!'. In between, in this order: the door, the butcher, the
grill, the flowers on the ceiling, the Michelin plaques." The clip-intent planner wrote the five
middle subjects as ONE position-less ``order`` row. ``apply_sequence`` seats a row's members in
the order they arrived and only orders ACROSS rows, so the middle played plaques, door, grill,
butcher, flowers, and the unified planner's outcome for that row read ``landed``: the creator
was told (receipt r2 "met / checked") the order landed.

``tests/fixtures/kri941_donjulio_m1_turn2.json`` holds the approved brief, the strategy exactly
as the job stored it (its resolved clip intents), the clip records the plan reads (attachment
order) and prod's own cut order and receipts, with every path, hash and transcript stripped.
No model call and no network: the real ``plan_unified_montage`` and ``build_receipts``.

* the recorded one-row strategy reproduces prod's cut order and its "met" receipt
* that receipt is a KNOWN GAP (``xfail(strict=True)``): from ids alone the server cannot tell a
  list from a group, and wording is never parsed (KRI-667). Closing it needs a structured field
* the same asks written one row per subject (what the planner prompt now says) land in the
  listed order and read "met"
* negative control: the same five rows with two of them swapped in the final cut read NOT met
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.kria.brief import CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.pipeline import unified_montage
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri941_donjulio_m1_turn2.json").read_text(
        encoding="utf-8"
    )
)


def _media(short: str) -> str:
    return f"analysis-proxy-ios-{short}.mp4"


# The clips by what the creator called them.
SIGN = _media("197B7255-5AC4-4E2B-B23A-42BFBE04B77D")
DOOR = _media("D4C43FA3-8B5B-4C10-901C-55F8A4E4710E")
BUTCHER = _media("B2C132E8-59A6-44C8-B288-7BC86E81F386")
GRILL = _media("3749EC57-BD55-4FAF-8CF2-FE5894C53CE7")
FLOWERS = _media("D36DBDCF-780A-45A5-B4F1-244FB74AFC08")
PLAQUES = _media("EC7B4FD2-2502-4824-B1A9-49C54DF0F2E0")
FRIEND = _media("F1681162-CF0F-43E6-A7A1-8833DBA8F276")

LISTED = [DOOR, BUTCHER, GRILL, FLOWERS, PLAQUES]
LISTED_NAMES = (
    "the door",
    "the butcher",
    "the grill",
    "the flowers on the ceiling",
    "the Michelin plaques",
)
# What prod rendered (job b3820ddc ``unified_montage.clip_ids``): the five listed clips in the
# order they were attached, not the order the creator listed.
PROD_CUT = [SIGN, PLAQUES, DOOR, GRILL, BUTCHER, FLOWERS, FRIEND]
# What the creator asked for.
LISTED_CUT = [SIGN, *LISTED, FRIEND]


def _brief() -> CreativeBrief:
    return CreativeBrief.model_validate(copy.deepcopy(FIXTURE["brief"]))


def _clips() -> list[UnifiedClip]:
    """The clips of the draft (the job's ``selected_media_ids``) in attachment order."""
    selected = set(FIXTURE["strategy"]["selected_media_ids"])
    return [
        UnifiedClip(
            media_id=row["media_id"],
            proxy_path=f"proxy/{row['media_id']}",
            generation="1",
            duration_s=row["duration_s"],
            width=row["width"],
            height=row["height"],
            analysis=copy.deepcopy(row["analysis"]),
            capture_time=datetime.fromisoformat(row["capture_time"]),
        )
        for row in FIXTURE["clips"]
        if row["media_id"] in selected
    ]


def _recorded_strategy() -> dict:
    """The strategy as the planner wrote it: the listed order is ONE position-less row."""
    return copy.deepcopy(FIXTURE["strategy"])


def _listed_strategy() -> dict:
    """The same strategy with the list as the planner prompt now asks: one row per subject."""
    strategy = _recorded_strategy()
    intents = strategy["resolved_clip_intents"]
    (index,) = [i for i, row in enumerate(intents) if row["intent_id"] == "order_sequence_between"]
    (collapsed,) = intents[index : index + 1]
    assert [a["media_id"] for a in collapsed["assignments"]] == LISTED
    rows = [
        {
            "op": "order",
            "status": "resolved",
            "attribute": name,
            "intent_id": f"order_listed_{n}",
            "assignments": [assignment],
        }
        for n, (name, assignment) in enumerate(
            zip(LISTED_NAMES, collapsed["assignments"], strict=True), start=1
        )
    ]
    intents[index : index + 1] = rows
    return strategy


def _plan(strategy: dict):
    brief = _brief()
    return plan_unified_montage(
        _clips(), brief_view(brief), strategy=strategy, clip_intents_enabled=True
    )


def _receipts(plan) -> dict:
    """The worker's receipts for the draft: every live requirement, bound writer."""
    rows = build_receipts(
        _brief().live(), plan_facts_from_unified_montage(plan.record()), include_unchecked=True
    )
    return {row.requirement_id: row for row in rows}


def _codes(plan) -> dict[str, str]:
    return {row["name"]: row["code"] for row in plan.record()["intent_outcomes"]}


# -- the replay is faithful -----------------------------------------------------------------


def test_the_fixture_is_the_walk_in_take_and_its_one_row_list() -> None:
    """Guards the fixture itself: a hand edit changes the incident."""
    assert len(FIXTURE["clips"]) == 8
    assert len(_clips()) == 7  # the second butcher clip was deselected
    orders = [r for r in FIXTURE["strategy"]["resolved_clip_intents"] if r["op"] == "order"]
    assert [(r.get("position"), len(r["assignments"])) for r in orders] == [
        ("first", 1),
        ("last", 1),
        (None, 5),
    ]
    (order,) = [r for r in FIXTURE["brief"]["requirements"] if r["kind"] == "order"]
    assert order["id"] == "r2" and set(order["facts"]) == {"first_clip", "last_clip"}


def test_prods_recorded_one_row_strategy_reproduces_prods_cut_order() -> None:
    """The reproduction: the same strategy through the same planner gives prod's cut."""
    plan = _plan(_recorded_strategy())
    assert plan.clip_ids == PROD_CUT == FIXTURE["prod"]["clip_ids"]
    # ...which is NOT the order the creator listed: four of the five middle clips are wrong.
    assert plan.clip_ids != LISTED_CUT
    wrong = [m for m, asked in zip(plan.clip_ids[1:-1], LISTED, strict=True) if m != asked]
    assert len(wrong) == 4
    prod = {row["name"]: row["code"] for row in FIXTURE["prod"]["intent_outcomes"]}
    assert set(prod.values()) == {"landed"}


def test_a_saved_list_without_ordering_certainty_cannot_claim_checked_order() -> None:
    receipt = _receipts(_plan(_recorded_strategy()))["r2"]
    assert receipt.status == "partial"
    assert "order" in receipt.reason.casefold() and "saved" in receipt.reason.casefold()


# -- the fix: one row per subject -----------------------------------------------------------


def test_the_list_written_one_row_per_subject_lands_in_the_listed_order() -> None:
    plan = _plan(_listed_strategy())
    assert plan.clip_ids == LISTED_CUT
    assert set(_codes(plan).values()) == {"landed"}
    assert len(_codes(plan)) == 7
    receipt = _receipts(plan)["r2"]
    assert (receipt.status, receipt.verification, receipt.reason) == ("met", "checked", None)


def test_two_listed_rows_swapped_in_the_cut_read_not_met(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: the seating puts the flowers before the grill. Both rows' clips play,
    but the cut is not the listed order, and the receipt must not say it is."""
    seat = unified_montage._apply_sequence

    def swapped(ordered, rows):  # noqa: ANN001, ANN202
        seated = list(seat(ordered, rows))
        at = {clip.media_id: i for i, clip in enumerate(seated)}
        a, b = at[GRILL], at[FLOWERS]
        seated[a], seated[b] = seated[b], seated[a]
        return seated

    monkeypatch.setattr(unified_montage, "_apply_sequence", swapped)
    plan = _plan(_listed_strategy())
    assert plan.clip_ids == [SIGN, DOOR, BUTCHER, FLOWERS, GRILL, PLAQUES, FRIEND]
    # The row listed after the grill plays before it: that row is the one out of place.
    misplaced = [name for name, code in _codes(plan).items() if code == "misplaced"]
    assert misplaced == ["then: the flowers on the ceiling"]
    receipt = _receipts(plan)["r2"]
    assert receipt.status != "met"
    assert "the flowers on the ceiling" in receipt.reason and "where you asked" in receipt.reason


def test_the_recorded_cut_under_five_rows_is_judged_row_by_row() -> None:
    """Prod's own cut (plaques, door, grill, butcher, flowers) judged against the five rows the
    creator's list should have been. A row is out of place when it plays before a row listed
    ahead of it: the grill (before the butcher) and the plaques (before all of them)."""
    by_id = {clip.media_id: clip for clip in _clips()}
    rows = unified_montage._sequence_intents(_listed_strategy(), True)
    outcomes = unified_montage._sequence_outcomes(rows, [by_id[m] for m in PROD_CUT])
    assert [row["name"] for row in outcomes if row["code"] == "misplaced"] == [
        "then: the grill",
        "then: the Michelin plaques",
    ]
    assert [row["code"] for row in outcomes].count("landed") == 5
