"""Leverkusen M3 run 1 (KRI-1109): "in the order I filmed them, ending on IMG_1043" is held
with "You asked for the same clip in two places: ... (last). I kept the first place you named."

Prod thread 83f2dcaa (2026-10-31, Fly 7b5504165, TestFlight 1819). Brief r1 is filming order
(``key: capture_time``) with ``last_clip: "the Nürnberg players thanking their fans
(IMG_1043)"``. The clip-intent planner wrote the filming order NOT as a basis intent
(``order_by: capture_time``: resolved by the server, never a sequence row) but as a positionless
described group, "the match from kick-off to the end", which the resolver filled with all five
videos (IMG_1043 included), plus a ``last`` pin on IMG_1043 listed after it. The KRI-764 rule
(`clip_order_sequence.row_relations`) seats every clip of the earlier positionless group at
"then", so the ``last`` pin reads as "already placed at another spot" and approval is held on
r1 (``needs_choice``), although the creator named ONE place for that clip and the cut ends on
it anyway (IMG_1043 is the last clip filmed). 13:24 (brief v3, "the trophy celebration" = m005
again, and the ticket first) is the same shape.

Ways this can fail (each is pinned below):

 F1 the incident: a filming order written as a whole-edit positionless group plus one ending
    pin is reported as "the same clip in two places" and holds approval.
 F2 the verdict depends on the order the planner happened to list the two intents in.
 F3 weakening KRI-764: one clip asked to be first AND last must still be said (guard; the pure
    rule is also pinned by `tests/services/test_order_pin_repeats.py`).

Runs the REAL answered brief (`answered_brief`), planner order hook (`brief_order_fields`),
draft facts (`plan_facts_from_strategy`), approval readiness (`assess_readiness`) and render
contract over this run's recorded briefs, the recorded planner/resolver I/O of both turns and
the media snapshot (`m3_kri1109_order.json`, which says what is recorded and what is
reconstructed; the briefs replayed here have the turns' recorded ``brief_digest``). No model
is called.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import assess_readiness, digest
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import plan_facts_from_strategy
from app.kria.planner import brief_order_fields
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.choice_questions import answered_brief
from app.services.clip_order_sequence import row_relations, sequence_rows
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract

CASE: dict[str, Any] = json.loads(Path(__file__).with_name("m3_kri1109_order.json").read_text())
SNAPSHOT: dict[str, Any] = CASE["media_snapshot"]
ALIASES: dict[str, str] = CASE["media_aliases"]
CREATOR = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
ITEM = CASE["source"]["plan_item"]
IMG_1043 = ALIASES["m005"]
VIDEOS = [row["media_id"] for row in SNAPSHOT["clip_assignments"]]
# (fixture turn, brief version)
TURNS = [("turn_1335", 4), ("turn_1324", 3)]
TURN_IDS = ["13:35", "13:24"]
TWO_PLACES = "same clip in two places"


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)


def _resolved(turn: str) -> list[dict[str, Any]]:
    """The turn's order intents as the server resolves a confident resolver answer: the
    planner's intent, status resolved, the resolver's assignments with alias -> media id."""
    recorded = CASE[turn]
    out = {row["intent_id"]: row for row in recorded["resolver_output"]}
    intents = []
    for planned in recorded["planner_intents"]:
        intent = {k: v for k, v in planned.items() if v is not None}
        intent["status"] = "resolved"
        intent["assignments"] = [
            {
                "media_id": ALIASES[a["media"]],
                "evidence": a["evidence"],
                "confidence": a["confidence"],
            }
            for a in out[planned["intent_id"]]["assignments"]
        ]
        intents.append(intent)
    return intents


def _turn(
    turn: str, version: int, order_intents: list[dict[str, Any]] | None = None
) -> tuple[CreativeBrief, dict[str, Any]]:
    """(the brief the turn judged, the turn's strategy): the stored brief with the turn's
    answers applied (`answered_brief`), and the strategy through the planner's own order hook
    (`brief_order_fields`)."""
    strategy = copy.deepcopy(CASE["base_strategy"])
    if CASE[turn]["choice_answers"]:
        strategy["choice_answers"] = copy.deepcopy(CASE[turn]["choice_answers"])
    brief = answered_brief(
        CreativeBrief.model_validate(copy.deepcopy(CASE[f"brief_v{version}"])), strategy
    )
    intents = [
        ResolvedClipIntent.model_validate(raw)
        for raw in [
            *strategy["resolved_clip_intents"],
            *(order_intents if order_intents is not None else _resolved(turn)),
        ]
    ]
    intents, choice = brief_order_fields(intents, brief, None)
    strategy["resolved_clip_intents"] = [intent.model_dump(mode="json") for intent in intents]
    if choice:
        strategy["ordering_choice"] = choice
    return brief, strategy


def _r1(brief: CreativeBrief, strategy: dict[str, Any]) -> dict[str, Any]:
    manifest = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            *(
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in SNAPSHOT["clip_assignments"]
            ),
            *({"media_id": row["media_id"], "kind": "image"} for row in SNAPSHOT["visuals"]),
        ],
    )
    report = assess_readiness(
        brief, strategy, media=copy.deepcopy(SNAPSHOT), manifest=manifest, creator_id=CREATOR
    )
    return next(row for row in report["requirements"] if row["requirement_id"] == "r1")


# -- the recorded state (passes: this is what prod judged) --------------------------------------


@pytest.mark.parametrize(("turn", "version"), TURNS, ids=TURN_IDS)
def test_replay_is_the_turns_recorded_brief_and_shape(turn: str, version: int) -> None:
    brief, strategy = _turn(turn, version)
    # The brief replayed is byte-for-byte the brief the turn's readiness judged.
    assert digest(brief.model_dump(mode="json")) == CASE[turn]["recorded_readiness"]["brief_digest"]
    rows = sequence_rows(strategy["resolved_clip_intents"])
    group = next(row for row in rows if row[0] is None)
    ending = next(row for row in rows if row[0] == "last")
    # The planner's "chronological" is a positionless described group (no `order_by`) over
    # every video, the ending pin is IMG_1043 alone, and the group is listed before it.
    assert sorted(group[2]) == sorted(VIDEOS)
    assert ending[2] == [IMG_1043]
    assert rows.index(group) < rows.index(ending)
    # Every video carries a capture time and IMG_1043 is the last one filmed: the filming
    # order the brief asks for ends on the very clip the pin names.
    stamps = {
        row["media_id"]: row["capture"]["capture_time"] for row in SNAPSHOT["clip_assignments"]
    }
    assert len(stamps) == 5 and all(stamps.values())
    assert max(stamps, key=stamps.__getitem__) == IMG_1043
    # The render contract pins filming order and IMG_1043 last among the videos.
    contract = build_render_contract(
        strategy, generation_id="m3", brief=brief, media_snapshot=SNAPSHOT
    )
    assert contract is not None and not contract.unresolved, contract
    assert contract.order_basis == "capture_time"
    assert [m for m in contract.order_ids if m in VIDEOS][-1] == IMG_1043


# -- F1: the incident -------------------------------------------------------------------------


@pytest.mark.parametrize(("turn", "version"), TURNS, ids=TURN_IDS)
def test_filming_order_ending_on_one_clip_is_not_the_same_clip_in_two_places(
    turn: str, version: int
) -> None:
    brief, strategy = _turn(turn, version)
    facts = plan_facts_from_strategy(strategy, clip_ids=VIDEOS)
    r1 = _r1(brief, strategy)
    assert TWO_PLACES not in str(r1["reason"]), r1
    assert facts.sequence_conflicts == (), facts.sequence_conflicts


# -- F2: the planner's list order decides nothing ----------------------------------------------


def test_the_verdict_does_not_depend_on_which_intent_the_planner_listed_first() -> None:
    listed = _resolved("turn_1335")
    verdicts = []
    for intents in (listed, list(reversed(listed))):
        _brief, strategy = _turn("turn_1335", 4, intents)
        verdicts.append(plan_facts_from_strategy(strategy, clip_ids=VIDEOS).sequence_conflicts)
    assert verdicts[0] == verdicts[1], verdicts


# -- F3: a real first-and-last ask is still said (guard) ----------------------------------------


def test_one_clip_asked_first_and_last_is_still_flagged() -> None:
    """IMG_1043 asked to open AND close the edit: the earlier claim stands and the creator
    hears it before approval (KRI-764). Must keep passing after the KRI-1109 fix."""
    first = {
        "op": "order",
        "position": "first",
        "attribute": "the Nürnberg players",
        "intent_id": "order_players_first",
        "status": "resolved",
        "assignments": [{"media_id": IMG_1043, "evidence": "file name", "confidence": 1.0}],
    }
    brief, strategy = _turn("turn_1335", 4, [first, *_resolved("turn_1335")])
    rows = sequence_rows(strategy["resolved_clip_intents"])
    assert row_relations(rows)[-1] == "conflict"
    r1 = _r1(brief, strategy)
    assert r1["disposition"] == "needs_choice", r1
    assert TWO_PLACES in str(r1["reason"])
    assert "the Nürnberg players thanking their fans (IMG_1043) (last)" in str(r1["reason"])
