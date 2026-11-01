"""Leverkusen M3 run 1, turn 3 (KRI-1110): "TOR!" on the two goal clips the creator named is
held back as "Text landed on 2 of 6 clips."

Prod thread 83f2dcaa (2026-10-31 13:22:58Z, Fly 7b5504165, TestFlight 1819): five iPhone clips
and one Visual (the ticket photo). The creator wrote: Leave the names and minutes out, just
write "TOR!" on both goal clips. The goals are IMG_1037 and IMG_1039. Brief v3 recorded r8 as a
dictated shot text (``is_shot_text``: kind text, scope per_clip, literal "TOR!", description
"both goal clips (IMG_1037 and IMG_1039)"). At 13:24:00 approval readiness held the draft on
r8, needs_choice, "Text landed on 2 of 6 clips.", and offered "Make it without these", which
would have dropped the creator's text. The 13:24 resolver saw only three order intents, so the
"TOR!" placement reached the draft without the resolver: the creator named both clips by the
file name the app shows, which the server settles itself (KRI-614).

Root cause pinned here: before a render, the receipt facts come from
``plan_facts_from_strategy`` (approval readiness and the turn's own receipts), which never fills
``label_scope_clip_ids``. ``_check_per_clip_text`` then judges an exact text for two described
shots against EVERY media in the manifest (all uploads, chosen or not), so text on exactly the
two clips the creator named can never be "met". Only the worker's montage record
(``unified_montage`` -> ``plan_facts_from_unified_montage``) carries the scope (KRI-296).

Ways this can fail, written before any fix (each is pinned below or named as out of scope):

 F1 the incident: text on exactly the two named clips reads "2 of 6" and approval is held.
 F2 the confounder: the "6" counts the ticket photo. It is in the count because it is an upload
    in the manifest, not because the withdrawn "start with my ticket" seated it (KRI-1111): with
    the ticket left out of the plan it still reads "2 of 6"; only with no ticket upload at all
    is it "2 of 5". Every case must be met.
 F3 a fix that scopes by "clips that carry text" would also pass a text placed on the WRONG two
    clips; the scope must be the clips placed for this requirement's literal (the settled
    label/caption intent), which here are exactly IMG_1037 and IMG_1039 (asserted as a
    precondition).
 F4 a per-clip RULE ("the place on each clip", no literal) must still be judged on every clip.
    Not pinned here (see tests/kria/test_unified_montage_receipts.py, test_creative_brief.py).
 F5 the worker path disagrees with the draft path: the worker's own plan prints "TOR!" on the
    two goal clips and its receipt is met (control below), so the draft held back an edit the
    worker would have made correctly.
 Out of scope: the "same clip in two places" r1 verdict on the same turn (KRI-1109, owned by
 brief_checks._check_order / clip_order_sequence) and the ticket being seated (KRI-1111).

Runs the REAL KRI-614 file-name settlement, approval readiness (manifest, policy, render
contract, receipts) and the worker's phone montage plan over brief v3 and the recorded media
snapshot (`m3_kri1110_labels.json`, which says what is recorded and what is reconstructed).
No model is called.
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
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_strategy,
    plan_facts_from_unified_montage,
)
from app.schemas.clip_intents import ClipIntent
from app.services.clip_file_names import clip_display_name
from app.services.clip_intent_planning import apply_file_name_references
from app.services.clip_intent_resolution import IntentClip
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.phone_montage_preflight import preflight_montage_plan
from app.tasks.generative_build import unified_visual

CASE: dict[str, Any] = json.loads(Path(__file__).with_name("m3_kri1110_labels.json").read_text())
SNAPSHOT: dict[str, Any] = CASE["media_snapshot"]
CREATOR = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
ITEM = CASE["source"]["plan_item"]
MESSAGE = CASE["source"]["message"]
BY_FILE = {
    clip_display_name(row["source_filename"]).rsplit("-", 1)[-1]: row["media_id"]
    for row in [*SNAPSHOT["clip_assignments"], *SNAPSHOT["visuals"]]
}
GOALS = (BY_FILE["IMG_1037"], BY_FILE["IMG_1039"])
TICKET = SNAPSHOT["visuals"][0]["media_id"]
CASES = {
    # 13:24 as it happened: "start with my ticket" (withdrawn at 13:20) seated the ticket.
    "ticket_in_plan": {"seated": True, "uploaded": True},
    # What the creator asked: the ticket photo is still an upload, but not in the edit.
    "ticket_left_out": {"seated": False, "uploaded": True},
    # No ticket photo at all: the only case whose count is "of 5".
    "no_ticket_upload": {"seated": False, "uploaded": False},
}


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED / CLIP_FACTS_ENABLED are on in prod; the readiness gate was on for
    # this account (turn 3 carried `approval_readiness`).
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)


def _brief(*, seated: bool = True) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief_v3"])
    if not seated:
        # The creator's 13:20 "Then leave the ticket out", honoured: r1 no longer opens on
        # "my ticket" (KRI-1111 kept that pin; that is not this bug).
        data["requirements"][0]["facts"].pop("first_clip")
    return CreativeBrief.model_validate(data)


def _snapshot(*, uploaded: bool) -> dict[str, Any]:
    snapshot = copy.deepcopy(SNAPSHOT)
    if not uploaded:
        snapshot["visuals"] = []
    return snapshot


def _tor_on_named_goals(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The "TOR!" placement as the server settles "The goals are IMG_1037 and IMG_1039"
    (KRI-614, before any resolver): a creator-worded label on exactly those two clips."""
    clips = [
        IntentClip(
            media_id=row["media_id"],
            kind=row.get("kind") or "video",
            analysis=row.get("analysis") or {},
            file_name=clip_display_name(row["source_filename"]),
        )
        for row in [*snapshot["clip_assignments"], *snapshot["visuals"]]
    ]
    asked = ClipIntent(
        intent_id="label_goals_tor",
        op="label",
        attribute="IMG_1037 and IMG_1039",
        creator_text="TOR!",
    )
    settled, remaining, _split = apply_file_name_references(
        [asked], clips=clips, creator_request=MESSAGE
    )
    assert not remaining and len(settled) == 1, (settled, remaining)
    return settled[0].model_dump(mode="json", exclude_none=True)


def _strategy(*, seated: bool, uploaded: bool) -> dict[str, Any]:
    snapshot = _snapshot(uploaded=uploaded)
    strategy = copy.deepcopy(CASE["strategy_base"])
    orders = copy.deepcopy(CASE["resolved_order_1324"])
    if not seated:
        orders = [i for i in orders if i["intent_id"] != "order_ticket_first"]
        strategy["selected_media_ids"] = [m for m in strategy["selected_media_ids"] if m != TICKET]
    strategy["resolved_clip_intents"] = [*orders, _tor_on_named_goals(snapshot)]
    return strategy


def _manifest(snapshot: dict[str, Any]) -> Any:
    return resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            *(
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in snapshot["clip_assignments"]
            ),
            *({"media_id": row["media_id"], "kind": "image"} for row in snapshot["visuals"]),
        ],
    )


def _visuals(strategy: dict[str, Any], snapshot: dict[str, Any]) -> list[Any]:
    selected = set(strategy.get("selected_media_ids") or ())
    return [
        unified_visual(
            SimpleNamespace(
                id=uuid.UUID(row["media_id"].removeprefix("asset-")),
                kind="image",
                gcs_path=f"users/u-leverkusen/plan/pool/{row['media_id']}.webp",
                gcs_generation="9",
                duration_s=None,
                analysis={},
                aspect=9 / 16,
                source_filename=row["source_filename"],
                user_context="",
                content_hash=None,
                capture=row.get("capture"),
            )
        )
        for row in snapshot["visuals"]
        if row["media_id"] in selected
    ]


# -- F1 / F2 / F3: the draft that was held back ----------------------------------------------


@pytest.mark.parametrize("case", list(CASES))
def test_tor_on_exactly_the_two_named_goal_clips_is_met_before_approval(case: str) -> None:
    brief = _brief(seated=CASES[case]["seated"])
    [r8] = [req for req in brief.live() if req.id == "r8"]
    assert r8.is_shot_text and r8.literal == "TOR!"
    strategy = _strategy(**CASES[case])
    snapshot = _snapshot(uploaded=CASES[case]["uploaded"])
    manifest = _manifest(snapshot)
    # F3 precondition: the draft prints the creator's words on exactly the two goal clips.
    facts = plan_facts_from_strategy(
        strategy, clip_ids=[m.media_id for m in manifest.media], manifest=manifest
    )
    assert facts.per_clip_text == {goal: "TOR!" for goal in GOALS}
    # The turn's own receipt (kria_runtime `plan_facts_from_strategy` -> `build_receipts`).
    [receipt] = build_receipts([r8], facts, include_unchecked=True, strict_order=True)
    # Approval readiness, the gate that held turn 3.
    report = assess_readiness(
        brief, strategy, media=snapshot, manifest=manifest, creator_id=CREATOR
    )
    rows = {row["requirement_id"]: row for row in report["requirements"]}
    # As in prod (turn 3 had policy_problem null): nothing else blocks r8's own verdict.
    assert report.get("policy_problem") is None, report.get("policy_problem")
    assert (receipt.status, rows["r8"]["disposition"]) == ("met", "executable"), (
        receipt.reason,
        rows["r8"],
    )


# -- F5 control: the worker's own plan gets it right -----------------------------------------


@pytest.mark.parametrize("case", ["ticket_in_plan", "ticket_left_out"])
def test_worker_plan_prints_tor_on_the_goal_clips_and_judges_it_met(case: str) -> None:
    brief = _brief(seated=CASES[case]["seated"])
    strategy = _strategy(**CASES[case])
    snapshot = _snapshot(uploaded=CASES[case]["uploaded"])
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
    facts = plan_facts_from_unified_montage(plan.record())
    assert facts.per_clip_text == {goal: "TOR!" for goal in GOALS}
    assert set(facts.label_scope_clip_ids) == set(GOALS)
    [r8] = [req for req in brief.live() if req.id == "r8"]
    [receipt] = build_receipts([r8], facts, include_unchecked=True)
    assert (receipt.status, receipt.verification) == ("met", "checked"), receipt.reason


# -- F3 / F4 guards: the scope is this requirement's own placement, never "any text" ----------


def test_a_per_clip_rule_without_words_is_still_judged_on_every_clip() -> None:
    """F4: r5 ("who scored and in which minute on each goal clip") has no literal, so the
    clips "TOR!" landed on are not its placement: it stays judged on every clip. (v3 had
    superseded it; judged here as if still open.)"""
    brief = _brief(seated=False)
    [r5] = [
        req.model_copy(update={"status": "open"}) for req in brief.requirements if req.id == "r5"
    ]
    assert not r5.literal and not r5.is_shot_text
    snapshot = _snapshot(uploaded=True)
    manifest = _manifest(snapshot)
    facts = plan_facts_from_strategy(
        _strategy(seated=False, uploaded=True),
        clip_ids=[m.media_id for m in manifest.media],
        manifest=manifest,
    )
    [receipt] = build_receipts([r5], facts, include_unchecked=True, strict_order=True)
    assert receipt.status != "met", receipt


def test_other_words_are_not_scoped_by_the_tor_placement() -> None:
    """F3: a shot text whose words no clip carries is not met because "TOR!" was placed."""
    brief = _brief(seated=False)
    data = brief.model_dump(mode="json")
    [r8] = [row for row in data["requirements"] if row["id"] == "r8"]
    r8["literal"] = "GOAL!"
    other = next(req for req in CreativeBrief.model_validate(data).live() if req.id == "r8")
    snapshot = _snapshot(uploaded=True)
    manifest = _manifest(snapshot)
    facts = plan_facts_from_strategy(
        _strategy(seated=False, uploaded=True),
        clip_ids=[m.media_id for m in manifest.media],
        manifest=manifest,
    )
    [receipt] = build_receipts([other], facts, include_unchecked=True, strict_order=True)
    assert receipt.status != "met", receipt
