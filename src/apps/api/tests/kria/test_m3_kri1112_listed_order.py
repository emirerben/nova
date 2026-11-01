"""Leverkusen M3 run 1 (KRI-1112 part 1/3): a complete order the creator listed by file name is
held as "I can't confirm this draft does it yet."

Prod thread 83f2dcaa (2026-10-31, Fly 7b5504165, TestFlight 1819, protocol 25), Montage. Five
iPhone clips staged as ``<upload UUID>-IMG_10xx.mov`` (the app shows ``IMG_10xx``) plus the
ticket photo as a Visual (left out, r10).

13:38:18Z "Use exactly this order: IMG_1028, IMG_1036, IMG_1037, IMG_1039, IMG_1043. This
replaces my earlier order and ending requests." The KRI-614 file-name step settled all five
clips (``order_specific_clips-1..5``, evidence "file name"), so no resolver ran, and the phone
writer plays exactly that order (pinned below). 13:39:04Z approval readiness held r1
``evidence_missing`` "I can't confirm this draft does it yet." (receipt: "I can't confirm the
order this draft uses."): the brief key ``exact_sequence`` is no capture / random / any key
(``order_mode`` None), the render contract skips a placed rule with no basis (KRI-510) so it
pins no order, ``approval_readiness._planned_order_basis`` credits only a random or a filming
order, and ``brief_checks._check_order`` returns ``_CANT_CONFIRM_ORDER`` when
``facts.ordering_basis`` is None. A complete creator-listed order had no way to earn
pre-render credit.

Ways this can fail, written before any fix (each is pinned below):

 F1 the incident: a list that names every clip of the edit by file name, each settled with no
    model, is not approvable (with the model's ``exact_sequence`` key or with no key).
 F2 the credit is not backed: readiness approves the list but the render contract pins no
    order, so the phone verifier would accept any other order.
 F3 over-credit: a list that leaves out a clip the edit shows, or has a name nobody resolved,
    becomes approvable (its place is not stated, so the order is not the creator's).
 F4 the credit outruns the writer: the pinned order is not the order the phone plan plays
    (forward and reversed lists both checked against the real plan).
 Out of scope here: the brief keeping the replaced first/last words as sticky facts (brief v5
 r1 ``first_clip: "my ticket"``, KRI-1112 part 3/3), names said in an earlier sentence, and a
 resolver citing another clip's file name (part 2/3).

Runs the REAL approval readiness, render contract and phone montage plan. No model is called.
Fixture ``m3_kri1112_listed_order.json`` says what is recorded and what is reconstructed.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import assess_readiness
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.schemas.clip_intents import FILE_NAME_EVIDENCE
from app.services.clip_file_names import clip_display_name
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.creator_render_contract import build_render_contract
from app.services.phone_montage_preflight import preflight_montage_plan

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("m3_kri1112_listed_order.json").read_text(encoding="utf-8")
)
CREATOR = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
ITEM = CASE["source"]["plan_item"]
NAME_OF = {c["media_id"]: clip_display_name(c["source_filename"]) for c in CASE["clips"]}
BY_NAME = {name: media_id for media_id, name in NAME_OF.items()}
LISTED = ["IMG_1028", "IMG_1036", "IMG_1037", "IMG_1039", "IMG_1043"]  # the 13:38 message
SEQUENCE = "order_specific_clips-"
CANT_CONFIRM_ORDER = "I can't confirm the order this draft uses."


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # On in prod for this account: the turns carried `approval_readiness`.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)


def test_fixture_names_are_what_the_app_shows() -> None:
    """The iOS staging UUID and extension are stripped; every listed name is one clip."""
    assert NAME_OF[BY_NAME["IMG_1037"]] == "IMG_1037"
    assert [c["alias"] for c in CASE["clips"] if NAME_OF[c["media_id"]] == "IMG_1039"] == ["m001"]
    assert all(name in BY_NAME for name in LISTED)


def _brief(r1_facts: dict[str, Any]) -> CreativeBrief:
    """Brief v5 with r1's facts replaced (the recorded sticky first/last words are part 3/3)."""
    data = copy.deepcopy(CASE["brief_v5"])
    data["requirements"][0]["facts"] = r1_facts
    return CreativeBrief.model_validate(data)


def _strategy(
    *, leave_out: str | None = None, unresolved: str | None = None, reverse: bool = False
) -> dict[str, Any]:
    """The 13:39 draft: the creator's list as the file-name step settled it, plus the rest."""
    strategy = copy.deepcopy(CASE["strategy_1339"])
    rows = strategy["resolved_clip_intents"]
    listed = [
        r for r in rows if r["intent_id"].startswith(SEQUENCE) and r["attribute"] != leave_out
    ]
    for row in listed:
        if row["attribute"] == unresolved:
            row.update(status="needs_creator", assignments=[], question="Which clip is it?")
    if reverse:
        listed.reverse()
    strategy["resolved_clip_intents"] = [
        *listed,
        *(r for r in rows if not r["intent_id"].startswith(SEQUENCE)),
    ]
    return strategy


def _readiness(brief: CreativeBrief, strategy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    media = CASE["media_snapshot"]
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
    report = assess_readiness(brief, strategy, media=media, manifest=manifest, creator_id=CREATOR)
    return {row["requirement_id"]: row for row in report["requirements"]}


# What prod recorded for the other requirements at 13:39 (turn 27d7ce94 `approval_readiness`).
RECORDED_OTHERS = {
    "r2": "unverified",
    "r4": "executable",
    "r7": "executable",
    "r8": "executable",
    "r9": "unverified",
    "r10": "unverified",
}
KEYS = pytest.mark.parametrize(
    "r1_facts",
    [{"key": "exact_sequence"}, {}],
    ids=["model-key-exact_sequence", "no-key"],
)


def test_the_draft_settled_every_listed_clip_by_file_name() -> None:
    """Why no resolver ran at 13:39: the list was settled with no model, in the order named."""
    rows = [
        r
        for r in CASE["strategy_1339"]["resolved_clip_intents"]
        if r["intent_id"].startswith(SEQUENCE)
    ]
    assert [r["attribute"] for r in rows] == LISTED
    assert [[a["media_id"] for a in r["assignments"]] for r in rows] == [
        [BY_NAME[n]] for n in LISTED
    ]
    assert {a["evidence"] for r in rows for a in r["assignments"]} == {FILE_NAME_EVIDENCE}


@KEYS
def test_a_list_naming_every_clip_by_file_is_approvable(r1_facts: dict[str, Any]) -> None:
    """F1 (the incident). The draft commits to the creator's complete list, every clip settled
    by its file name; the phone writer plays it and judges it met (see F4)."""
    rows = _readiness(_brief(r1_facts), _strategy())
    # The replay is the prod turn for everything but the order.
    assert {k: v["disposition"] for k, v in rows.items() if k != "r1"} == RECORDED_OTHERS
    assert rows["r1"]["disposition"] == "executable", (
        f"13:39 again ({CANT_CONFIRM_ORDER!r} on the receipt): {rows['r1']}"
    )


@KEYS
def test_the_render_contract_pins_the_listed_order(r1_facts: dict[str, Any]) -> None:
    """F2. The credit is the contract's commitment: the phone verifier refuses any render whose
    picture order is not ``order_ids`` (`verify_phone_recipe`)."""
    contract = build_render_contract(
        _strategy(),
        generation_id="m3-1339",
        brief=_brief(r1_facts),
        media_snapshot=CASE["media_snapshot"],
    )
    assert contract is not None and not contract.unresolved, contract
    assert contract.order_required
    assert [NAME_OF[c] for c in contract.order_ids] == LISTED


@pytest.mark.parametrize(
    "change",
    [{"leave_out": "IMG_1037"}, {"unresolved": "IMG_1036"}],
    ids=["a-shown-clip-unlisted", "a-name-unresolved"],
)
def test_an_incomplete_or_unresolved_list_is_not_credited(change: dict[str, str]) -> None:
    """F3. Guard (passes today): only a list that places every shown clip states the order."""
    r1 = _readiness(_brief({"key": "exact_sequence"}), _strategy(**change))["r1"]
    assert r1["disposition"] != "executable", r1
    contract = build_render_contract(
        _strategy(**change),
        generation_id="m3-1339",
        brief=_brief({"key": "exact_sequence"}),
        media_snapshot=CASE["media_snapshot"],
    )
    assert contract is None or not contract.order_ids, contract


@pytest.mark.parametrize("reverse", [False, True], ids=["as-listed", "reversed-list"])
def test_the_phone_plan_plays_the_listed_order_and_judges_it_met(reverse: bool) -> None:
    """F4. Guard (passes today): the unified montage plan seats each file-named row in the
    order listed (``apply_sequence``) and its own receipt is checked + met, whatever order the
    creator listed."""
    brief = _brief({"key": "exact_sequence"})
    strategy = _strategy(reverse=reverse)
    planned = preflight_montage_plan(
        creator_id=CREATOR,
        edit_format="montage",
        brief=brief,
        strategy=strategy,
        media_snapshot=CASE["media_snapshot"],
        visuals=[],
    )
    assert isinstance(planned, tuple), planned
    facts = plan_facts_from_unified_montage(planned[0].record())
    listed = list(reversed(LISTED)) if reverse else LISTED
    assert [NAME_OF[c] for c in facts.clip_ids] == listed
    [receipt] = build_receipts(brief.live()[:1], facts, include_unchecked=True)
    assert (receipt.status, receipt.verification) == ("met", "checked"), receipt


@pytest.mark.parametrize("reverse", [False, True], ids=["as-listed", "reversed-list"])
def test_the_pinned_order_is_the_order_the_phone_plan_plays(reverse: bool) -> None:
    """F4. What the contract pins and what the writer plays never differ."""
    brief = _brief({"key": "exact_sequence"})
    strategy = _strategy(reverse=reverse)
    planned = preflight_montage_plan(
        creator_id=CREATOR,
        edit_format="montage",
        brief=brief,
        strategy=strategy,
        media_snapshot=CASE["media_snapshot"],
        visuals=[],
    )
    assert isinstance(planned, tuple), planned
    contract = build_render_contract(
        strategy, generation_id="m3-1339", brief=brief, media_snapshot=CASE["media_snapshot"]
    )
    assert contract is not None
    assert list(contract.order_ids) == list(planned[0].clip_ids)
