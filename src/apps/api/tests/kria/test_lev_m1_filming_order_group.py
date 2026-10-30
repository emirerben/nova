"""Leverkusen M1 run 2 #2 (KRI-1080): "in the order I filmed it, starting with the train ride"
is a filming-order draft that reaches approval, with no question about the train ride's
inner order and no "I can't confirm the order".

Prod thread 1a2189cc (2026-10-30, Fly eed794157, TestFlight 1819): eight dated iPhone clips
and five dated Visuals photos. "Starting with the train ride" became a ``first`` group of three
(the display photo, the seat photo, the announcement clip) with no ``ordered_members``. Turn 2
held approval on "The order inside this saved group wasn't recorded. Choose the order, or
confirm that any order within the group is fine." After the creator answered "any order inside
the train group is fine", turn 3 held it on "I can't confirm this draft does it yet." (receipt:
"I can't confirm the order this draft uses."). The only way out was "Make it without these",
which dropped the filming order.

Ways this can fail, written before the fix (each is pinned below):

 F1 the incident, turn 2: under a filming-order ask a multi-clip first/last group with no stated
    inner order is asked about, though the planner plays it in filming order by construction.
 F2 the incident, turn 3: with the group settled, a draft whose every clip and photo is dated
    still reads "can't confirm the order" and approval is held (``evidence_missing``).
 F3 the draft is approved but the worker disagrees: its plan marks the group
    ``member_order_unknown``, or plays an order other than the one the contract pins (the phone
    verifier then refuses the render after approval).
 F4 an undated photo or clip still passes the order check.
 F5 a group the creator ordered inside ("the display photo, then the train") is overwritten.
 F6 no filming order (a pin-only "start with the train ride"): the group's inner order really
    was never said, so it is still asked about, but in the creator's words.
 F7 a random-order ask changes behaviour for a positioned group.
 F8 the readiness credit reaches a writer no phone verifier pins (not the phone montage).
 F9 "saved group" reaches a creator anywhere: the draft receipt, an old receipt re-read after
    reopening, or the render's own group outcome; in English or Turkish.
 F10 a basis intent (``order_by``) or a one-clip group is stamped (the schema refuses
    ``ordered_members`` with ``order_by``; a one-clip group has no inner order).

Runs the REAL planner order hook (`brief_order_fields`), approval readiness (manifest, policy,
render contract, receipts) and the worker's own phone montage plan (`preflight_montage_plan`)
over this run's brief and the media snapshot approval pinned (`lev_m1_run2_filming_order.json`,
which says what is recorded and what is reconstructed). No model is called.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import assess_readiness
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.planner import brief_order_fields
from app.kria.reply_language import reply_language_for
from app.pipeline import unified_montage
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.clip_order_sequence import (
    unconfirmed_order_groups,
    unconfirmed_order_reason,
)
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract
from app.services.phone_montage_preflight import preflight_montage_plan
from app.tasks.generative_build import unified_visual

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("lev_m1_run2_filming_order.json").read_text()
)
SNAPSHOT: dict[str, Any] = CASE["media_snapshot"]
CREATOR = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
ITEM = CASE["source"]["plan_item"]


def _file(media_id: str) -> str:
    rows = [*SNAPSHOT["clip_assignments"], *SNAPSHOT["visuals"]]
    name = next(row["source_filename"] for row in rows if row["media_id"] == media_id)
    return name.rsplit("-", 1)[-1].split(".")[0]


BY_FILE = {
    _file(row["media_id"]): row["media_id"]
    for row in [*SNAPSHOT["clip_assignments"], *SNAPSHOT["visuals"]]
}
# What the edit shows (the approved selection), in filming order: the train display photo
# 17:34:31Z, the announcement clip 17:42:36Z, the box view 18:52:22Z ... the players 21:47:37Z.
FILMED = [
    BY_FILE[name]
    for name in (
        "IMG_0995",
        "IMG_1002",
        "IMG_1019",
        "IMG_1026",
        "IMG_1027",
        "IMG_1028",
        "IMG_1036",
        "IMG_1037",
        "IMG_1038",
        "IMG_1039",
        "IMG_1043",
    )
]
SAVED_GROUP_EN = "saved group"
SAVED_GROUP_TR = "Kaydedilmiş"


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED / CLIP_FACTS_ENABLED are on in prod; the readiness gate was on for
    # this account (the turns carried `approval_readiness`).
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)


def _brief(**r1_facts: Any) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief_v1"])
    if r1_facts:
        data["requirements"][0]["facts"] = r1_facts
    return CreativeBrief.model_validate(data)


def _group(ordered_members: bool | None = None, **changes: Any) -> dict[str, Any]:
    group = {**copy.deepcopy(CASE["train_group"]), **changes}
    if ordered_members is not None:
        group["ordered_members"] = ordered_members
    return group


def _drafted(brief: CreativeBrief, *groups: dict[str, Any]) -> dict[str, Any]:
    """The turn's strategy as the planner writes it: its resolved intents plus ``groups``,
    through the planner's own order hook (`brief_order_fields`)."""
    strategy = copy.deepcopy(CASE["strategy"])
    intents = [
        ResolvedClipIntent.model_validate(raw)
        for raw in [*strategy["resolved_clip_intents"], *groups]
    ]
    intents, choice = brief_order_fields(intents, brief, None)
    strategy["resolved_clip_intents"] = [intent.model_dump(mode="json") for intent in intents]
    if choice:
        strategy["ordering_choice"] = choice
    return strategy


def _snapshot(*, undated: str | None = None) -> dict[str, Any]:
    snapshot = copy.deepcopy(SNAPSHOT)
    for row in [*snapshot["clip_assignments"], *snapshot["visuals"]]:
        if undated is not None and row["media_id"] == BY_FILE[undated]:
            row["capture"] = {}
    return snapshot


def _readiness(
    brief: CreativeBrief,
    strategy: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    creator_id: object = CREATOR,
) -> dict[str, dict[str, Any]]:
    media = snapshot if snapshot is not None else _snapshot()
    manifest = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            *(
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in media["clip_assignments"]
            ),
            *({"media_id": row["media_id"], "kind": "image"} for row in media["visuals"]),
        ],
    )
    report = assess_readiness(
        brief, strategy, media=media, manifest=manifest, creator_id=creator_id
    )
    return {row["requirement_id"]: row for row in report["requirements"]}


def _visuals(strategy: dict[str, Any], snapshot: dict[str, Any]) -> list[Any]:
    """The ready Visuals rows the worker places (the approved selection), as it reads them."""
    selected = set(strategy.get("selected_media_ids") or ())
    return [
        unified_visual(
            SimpleNamespace(
                id=uuid.UUID(row["media_id"].removeprefix("asset-")),
                kind="image",
                gcs_path=f"users/u-leverkusen/plan/pool/{row['media_id']}.jpg",
                gcs_generation="9",
                duration_s=None,
                analysis={},
                aspect=4 / 3,
                source_filename=row["source_filename"],
                user_context="",
                content_hash=None,
                capture=row.get("capture"),
            )
        )
        for row in snapshot["visuals"]
        if row["media_id"] in selected
    ]


# -- F1 / F2: the incident ------------------------------------------------------------------


def test_turn2_filming_order_group_is_not_asked_about() -> None:
    """F1: the train ride plays in filming order inside; nothing asks how to order it."""
    brief = _brief()
    strategy = _drafted(brief, _group())
    [group] = [
        i for i in strategy["resolved_clip_intents"] if i["intent_id"] == "order_train_ride_first"
    ]
    assert group["ordered_members"] is False
    assert not unconfirmed_order_groups(strategy["resolved_clip_intents"])
    r1 = _readiness(brief, strategy)["r1"]
    assert r1["disposition"] == "executable", r1
    assert SAVED_GROUP_EN not in str(r1["reason"])


@pytest.mark.parametrize("answered", [False, True], ids=["unanswered", "creator-answered"])
def test_dated_filming_order_draft_is_executable_before_approval(answered: bool) -> None:
    """F2: every clip and photo is dated, the contract pins that order, so r1 is executable
    (turn 3: the creator's own "any order inside the train group is fine")."""
    brief = _brief()
    strategy = _drafted(brief, _group(ordered_members=False if answered else None))
    rows = _readiness(brief, strategy)
    assert rows["r1"]["disposition"] == "executable", rows["r1"]
    # The rest of the turn's verdicts are unchanged by this fix (r5 is a separate bug).
    assert {key: row["disposition"] for key, row in rows.items() if key != "r5"} == {
        "r1": "executable",
        "r2": "unverified",
        "r3": "unverified",
        "r4": "unverified",
        "r6": "executable",
    }


def test_plain_filming_order_draft_is_executable_before_approval() -> None:
    """The same credit with no first group at all (Plaza / Don Julio M1: "in the order we
    filmed it")."""
    brief = _brief()
    rows = _readiness(brief, _drafted(brief))
    assert rows["r1"]["disposition"] == "executable", rows["r1"]


# -- F3: the worker builds what the draft promised -------------------------------------------


def test_worker_plan_plays_the_pinned_filming_order_and_judges_it_met() -> None:
    brief = _brief()
    strategy = _drafted(brief, _group())
    snapshot = _snapshot()
    contract = build_render_contract(
        strategy, generation_id="lev-m1", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None and not contract.unresolved
    assert contract.order_basis == "capture_time"
    assert list(contract.order_ids) == FILMED
    planned = preflight_montage_plan(
        creator_id=CREATOR,
        edit_format="montage",
        brief=brief,
        strategy=strategy,
        media_snapshot=snapshot,
        visuals=_visuals(strategy, snapshot),
    )
    assert isinstance(planned, tuple), planned
    plan, _aliases = planned
    record = plan.record()
    facts = plan_facts_from_unified_montage(record)
    assert facts.ordering_basis == "capture_time"
    # The plan names a Visual by its row id; the phone verifier reads it as `asset-<id>`.
    clips = {row["media_id"] for row in snapshot["clip_assignments"]}
    played = [m if m in clips else f"asset-{m}" for m in facts.clip_ids]
    assert played == list(contract.order_ids)
    [r1] = build_receipts(brief.live()[:1], facts, include_unchecked=True)
    assert (r1.status, r1.verification) == ("met", "checked"), r1
    assert not facts.sequence_unconfirmed


# -- F4: an undated item never passes ---------------------------------------------------------


@pytest.mark.parametrize("undated", ["IMG_1038", "IMG_1043"], ids=["photo", "clip"])
def test_an_undated_photo_or_clip_is_not_executable(undated: str) -> None:
    brief = _brief()
    rows = _readiness(brief, _drafted(brief, _group()), snapshot=_snapshot(undated=undated))
    assert rows["r1"]["disposition"] != "executable", rows["r1"]


# -- F5 / F6 / F7 / F10: what the group rule leaves alone -------------------------------------


def _stamped(brief: CreativeBrief, group: dict[str, Any]) -> object:
    strategy = _drafted(brief, group)
    [out] = [i for i in strategy["resolved_clip_intents"] if i["intent_id"] == group["intent_id"]]
    return out.get("ordered_members")


def test_a_group_the_creator_ordered_keeps_its_order() -> None:
    """F5."""
    assert _stamped(_brief(), _group(ordered_members=True)) is True


def test_without_filming_order_the_group_is_still_asked_in_plain_words() -> None:
    """F6 + F9: a pin-only "start with the train ride" never said the inner order."""
    brief = _brief(first_clip="the train ride")
    strategy = _drafted(brief, _group())
    assert ("first", "the train ride") in {
        (position, name)
        for position, name, _members in unconfirmed_order_groups(strategy["resolved_clip_intents"])
    }
    r1 = _readiness(brief, strategy)["r1"]
    assert r1["disposition"] == "needs_choice", r1
    assert '"the train ride"' in r1["reason"] and SAVED_GROUP_EN not in r1["reason"]


def test_random_order_leaves_a_positioned_group_as_it_was() -> None:
    """F7: 29 Ekim's rule (positionless groups only) is unchanged."""
    assert _stamped(_brief(key="random", seed=7), _group()) is None


def test_one_clip_group_is_not_stamped() -> None:
    """F10: a basis intent never takes the marker (the schema would refuse it) and a
    one-clip group has no inner order to confirm."""
    brief = _brief()
    one = _group(assignments=CASE["train_group"]["assignments"][2:])
    assert _stamped(brief, one) is None
    strategy = _drafted(brief, _group())
    basis = next(
        i for i in strategy["resolved_clip_intents"] if i["intent_id"] == "order_capture_time_all"
    )
    assert "ordered_members" not in basis


# -- F8: only the phone montage writer's pinned order counts ---------------------------------


def test_no_phone_route_gets_no_order_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    brief = _brief()
    r1 = _readiness(brief, _drafted(brief, _group()))["r1"]
    assert r1["disposition"] != "executable", r1


def test_group_first_answer_gets_no_filming_order_credit() -> None:
    """F8: "group first" (KRI-282) regroups the clips in the planner; the contract still pins
    pure filming order, so that draft does not commit to the order the contract checks."""
    brief = _brief()
    strategy = {**_drafted(brief, _group()), "ordering_choice": "group_first"}
    r1 = _readiness(brief, strategy)["r1"]
    assert r1["disposition"] != "executable", r1


# -- F9: the words a creator can read ---------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "tr"])
def test_unconfirmed_group_wording_names_the_group_in_plain_words(language: str) -> None:
    with reply_language_for(language):
        text = unconfirmed_order_reason(["the train ride"])
    assert '"the train ride"' in text
    assert SAVED_GROUP_EN not in text and SAVED_GROUP_TR not in text
    assert "saved group" not in unified_montage.UNCONFIRMED_MEMBER_ORDER
