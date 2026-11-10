"""S3 Yorum2 (prod thread cc0e3706, 2026-11-08): a Talking edit's stated first / last take never
resolved, and the one refusal hid every other blocker.

Three talking takes: A (20.1 s, home, "Wait, the light... okay. Why do brands do food
marketing?"), B (7.0 s, home, "And food is the most shared culture..."), C (7.15 s, parked car).
The creator said "20-second home clip first, then the 7-second home clip, then the car clip
last", then "First the clip that starts “Wait, the light”, ...". Each turn held on "I couldn't
tell which clip "..." means, so I can't confirm the order." A Talking edit plans no clip intents
(`CLIP_INTENT_FREE_EDIT_FORMATS`), so no resolver ever seated the brief's ``first_clip`` /
``last_clip`` and the render contract refused every wording. The readiness gate then copied that
one line onto all eight requirements, so the cleanup shortfall said in turn 1 vanished until the
creator gave up on the order (turn 4).

Ways this can fail, written before the code (each is pinned below):

 F1 the incident: a take named by its length, its opening words or where it was filmed is
    refused by name although exactly one take matches.
 F2 a guess: a description two takes match ("the 7-second clip": B is 7.0 s, C 7.15 s; "the
    home clip") is seated instead of asked about. KRI-1340: a stated count ("two at home") is no
    guess when exactly that many takes match; any other count is still asked about.
 F3 first and last name the same take, and both are seated (one take cannot be both).
 F4 a montage (which has the clip-intent resolver) changes: only Talking edits read the takes.
 F5 the order refusal is copied onto every requirement, hiding their own verdicts (Bug F).
 F6 a problem that is not the order ask's (a text whose clip is unknown) stops holding the
    whole draft.

Real contract builder and readiness gate over this run's clip rows, brief and strategy
(`s3_yorum2_talking_order.json` says what is reconstructed). No model is called.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.kria import approval_readiness
from app.kria.approval_readiness import assess_readiness, readiness_reply
from app.kria.brief import CreativeBrief
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import _pin_not_found, build_render_contract

CASE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "s3_yorum2_talking_order.json").read_text()
)
A, B, C = (CASE["clips"][k] for k in "ABC")


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _brief(first: str, last: str = "the car clip", **facts: Any) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief"])
    data["requirements"][0]["facts"] = {
        "key": "capture_time",
        "first_clip": first,
        "last_clip": last,
        **facts,
    }
    return CreativeBrief.model_validate(data)


def _contract(brief: CreativeBrief, *, edit_format: str = "subtitled"):
    return build_render_contract(
        {**CASE["strategy"], "edit_format": edit_format},
        generation_id="g",
        brief=brief,
        media_snapshot=CASE["snapshot"],
    )


@pytest.mark.parametrize(
    "first",
    [
        "20-second home clip",
        "the clip that starts “Wait, the light”",
        "the 20 saniyelik video",
    ],
    ids=["length+place", "opening-words", "length-tr"],
)
def test_f1_a_take_named_by_length_words_or_place_pins_the_order(first: str) -> None:
    contract = _contract(_brief(first))

    assert contract.unresolved == ()
    assert contract.order_basis == "capture_time"
    assert contract.order_ids == (A, B, C)


def test_f1_the_second_take_by_its_opening_words_leads() -> None:
    contract = _contract(_brief("the clip that starts “And food is the most shared culture”"))

    assert contract.unresolved == ()
    assert contract.order_ids == (B, A, C)


@pytest.mark.parametrize(
    "first", ["the 7-second clip", "the home clip", "three at home", "the blue one"]
)
def test_f2_a_description_two_takes_or_none_match_is_still_asked_by_name(first: str) -> None:
    contract = _contract(_brief(first))

    assert contract.unresolved == (_pin_not_found(first),)
    assert contract.order_ids == ()


def test_f2_a_counted_group_seats_exactly_the_takes_it_counts() -> None:
    """KRI-1340 (S3 Yorum1's own words): "two at home and one in the car" names the two home
    takes as the opening group, by their count, and the car take last. Filming order decides
    inside the group, as the brief's basis says."""
    contract = _contract(_brief("two at home", "one in the car"))

    assert contract.unresolved == ()
    assert contract.order_ids == (A, B, C)


def test_f3_one_take_is_never_seated_first_and_last() -> None:
    contract = _contract(_brief("the car clip", "the clip in the car"))

    assert contract.unresolved == (_pin_not_found("the clip in the car"),)


def test_f4_a_montage_never_reads_the_takes_itself() -> None:
    """The montage resolver seats its own rows; with none, the stated ends are refused as before."""
    contract = _contract(_brief("20-second home clip"), edit_format="montage")

    assert _pin_not_found("20-second home clip") in contract.unresolved


def _readiness(brief: CreativeBrief) -> dict[str, Any]:
    manifest = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="subtitled",
        media=[
            {
                "media_id": row["media_id"],
                "kind": "video",
                "duration_s": row["analysis"]["duration_s"],
            }
            for row in CASE["snapshot"]["clip_assignments"]
        ],
    )
    return assess_readiness(brief, CASE["strategy"], media=CASE["snapshot"], manifest=manifest)


def test_f5_an_unplaced_take_holds_the_order_ask_alone_and_every_other_keeps_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prod turn 2 read the order line under all eight asks; the cleanup shortfall of turn 1
    (only the long pauses are cut) was hidden until the creator dropped the order."""
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True)
    brief = _brief("the 7-second clip")

    report = _readiness(brief)
    rows = {row["requirement_id"]: row for row in report["requirements"]}
    reply = readiness_reply(brief, report)

    assert report["policy_problem"] is None
    assert not report["ready"]
    assert rows["r1"]["disposition"] == "unsupported"
    assert rows["r1"]["reason"] == _pin_not_found("the 7-second clip")
    assert rows["r2"]["disposition"] == "needs_choice"
    assert "only the long pauses" in rows["r2"]["reason"]
    assert all(row["reason"] != rows["r1"]["reason"] for rid, row in rows.items() if rid != "r1")
    assert reply.count(_pin_not_found("the 7-second clip")) == 1, reply
    assert "Tell me which clip you mean" in reply
    assert "only the long pauses" in reply


def test_f6_a_problem_that_is_not_the_order_asks_still_holds_the_whole_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "I couldn't tell which clip \"Episode 10\" goes on, so I can't confirm that text."
    real = approval_readiness.build_render_contract

    def _with_text_problem(*args: Any, **kwargs: Any):
        contract = real(*args, **kwargs)
        return contract.model_copy(update={"unresolved": (*contract.unresolved, other)})

    monkeypatch.setattr(approval_readiness, "build_render_contract", _with_text_problem)

    report = _readiness(_brief("the 7-second clip"))

    assert report["policy_problem"] == _pin_not_found("the 7-second clip")
    assert {row["reason"] for row in report["requirements"]} == {report["policy_problem"]}


# A3: once the takes are seated, the phone Talking writer plays the pinned order
# (`generative_build._speakers_in_pinned_order`) and the verifier refuses any other, so the
# draft can vouch for the order instead of holding on "I can't confirm the order this draft
# uses" (prod turn 1).

CREATOR = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")


def _phone_readiness(brief: CreativeBrief, strategy: dict[str, Any] | None = None):
    manifest = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="subtitled",
        media=[
            {
                "media_id": row["media_id"],
                "kind": "video",
                "duration_s": row["analysis"]["duration_s"],
            }
            for row in CASE["snapshot"]["clip_assignments"]
        ],
    )
    report = assess_readiness(
        brief,
        strategy or CASE["strategy"],
        media=CASE["snapshot"],
        manifest=manifest,
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}


@pytest.fixture
def _phone_talking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", True)


@pytest.mark.usefixtures("_phone_talking")
def test_a3_the_phone_talking_writer_vouches_for_the_pinned_take_order() -> None:
    rows = _phone_readiness(_brief("20-second home clip"))

    assert rows["r1"]["disposition"] == "executable", rows["r1"]


@pytest.mark.usefixtures("_phone_talking")
def test_a3_a_random_order_is_never_vouched_for_on_talking() -> None:
    """The Talking writer never shuffles its takes: a random-order stamp is not its promise."""
    rows = _phone_readiness(
        _brief("20-second home clip"), {**CASE["strategy"], "ordering_choice": "random"}
    )

    assert rows["r1"]["disposition"] != "executable", rows["r1"]


def test_a3_off_the_phone_talking_writer_the_order_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", True)
    rows = _phone_readiness(_brief("20-second home clip"))

    assert rows["r1"]["disposition"] == "evidence_missing", rows["r1"]


@pytest.mark.usefixtures("_phone_talking")
def test_a3_with_several_take_talking_off_the_order_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", False)
    rows = _phone_readiness(_brief("20-second home clip"))

    assert rows["r1"]["disposition"] != "executable", rows["r1"]


@pytest.mark.usefixtures("_phone_talking")
def test_a3_a_silent_footage_clip_among_the_takes_keeps_the_order_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clip that cannot talk plays as a cutaway, which the phone verifier counts in the
    picture order; vouching there could pass approval and then refuse the render."""
    import copy

    case = copy.deepcopy(CASE)
    car = case["snapshot"]["clip_assignments"][-1]
    car["analysis"]["understanding"]["speech"] = {"has_speech": False, "transcript": ""}
    monkeypatch.setitem(CASE, "snapshot", case["snapshot"])
    rows = _phone_readiness(_brief("20-second home clip"))

    assert rows["r1"]["disposition"] != "executable", rows["r1"]
