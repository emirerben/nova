"""M3 run 1 (KRI-1113): Kria's own "Animate the title" chip dead-ends in "I can't make this yet".

Prod thread 83f2dcaa (2026-10-31, Fly main 7b5504165, TestFlight 1819, protocol 25). The phone
render of job 3c415e0e was ready at 13:48:22 with the opening title "Leverkusen 5:2 Nürnberg"
(0-2.2 s, ``animation_phases`` null) and brief v6 r4 (text / title / literal, facts {}) read
"Done". At 13:48:27 the creator tapped the ``title_animation`` chip "Animate the title". The
Main Creator wrote brief v7 r4 = "Title: ... animated", facts {"animation": "fade"}, and
approval readiness held the draft: r4 ``evidence_missing`` "I can't confirm this draft does it
yet." The card offered "Make it without these" (leaves out r4). The creator took it at 13:51:14;
brief v8 r4 went back to "Title: ..." but still carried {"animation": "fade"}, and readiness held
the PLAIN title (rendered and checked at 13:48) with the same sentence, offering to leave out the
title itself. No video came out of either turn.

Ways this can fail, written before any fix (each is pinned below):

 F1 the incident, 13:49: a title-scope text ask whose words the draft carries but whose look
    (``facts.animation``) only the finished video shows is held as ``evidence_missing``
    instead of approved and named for a look after render (KRI-983's ``unverified``). Its
    words and look share one receipt: ``_with_text_look`` turns the met word check into
    "partial / unchecked / I can't verify this one automatically yet" when the strategy draft
    has no text lane (``text_styles`` None), and ``_no_pre_render_checker`` refuses
    ``unverified`` to every ``kind == "text"`` ask (docs/reviews/kri-721: known gap).
 F2 the incident, 13:51: after "Make it without these", the PLAIN title the render already
    showed is held on the same sentence, because the look fact is still on r4.
 F3 "Make it without these" leaves the animation in force: the Main Creator restated r4 this
    turn without it, the restatement re-carried {"animation": "fade"} (``STICKY_FACTS``,
    KRI-522), and the server's own omission never ran (a model update on the turn cancels the
    consent; the offer was also pinned to brief v7, the turn persisted v8).
 F4 a fix approves an animation the phone writer would not draw (the approval would then be a
    false promise). Pinned as a control: the unified montage writer reads r4's animation.
 F5 a fix lets the after-render review pass a title that did NOT animate. Pinned as a control:
    the rendered 13:48 title (static) is not "met" for v7 r4.
 F6 a fix changes the other verdicts of the same turn (r2/r7/r8/r9/r10): pinned to prod's rows.
 F7 the plain title (brief v6) stops being executable.
 F8 the chip is offered as an editor ``patch_text`` on an allow-list (no witness), but on this
    thread every turn re-plans (a creator-supplied title keeps ``creative_copy_pending``), so a
    fix that only proves the editor op leaves the chip dead. Pinned as a control.

Runs the REAL approval readiness (manifest, policy, render contract, receipts), the REAL phone
unified montage writer (`preflight_montage_plan`), the REAL brief merge (`apply_updates`), the
REAL simpler-version offer and consent (`kria_runtime`), and the REAL rendered-variant checks over
this run's briefs, approved strategy and media snapshot (`m3_kri1113_title_animation.json`, whose
``source`` says what is recorded and what is reconstructed). No model is called.
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
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.style_asks import text_look_facts
from app.services.creative_copy_decisions import fold_creative_copy
from app.services.creator_capabilities import resolve_creator_manifest
from app.services.phone_montage_preflight import preflight_montage_plan
from app.services.suggestion_chips import CHIP_KIND_GATE, ChipKind
from app.tasks.kria_runtime import _simpler_version_consent, _simpler_version_offer

CASE: dict[str, Any] = json.loads(
    Path(__file__).with_name("m3_kri1113_title_animation.json").read_text(encoding="utf-8")
)
SNAPSHOT: dict[str, Any] = CASE["media_snapshot"]
CREATOR = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
ITEM = CASE["source"]["plan_item"]
TITLE = "Leverkusen 5:2 Nürnberg"
CANT_CONFIRM = "I can't confirm this draft does it yet."
SIMPLER_TURN = "094deee9-b815-4af0-8b47-c646a383b203"


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED / CLIP_FACTS_ENABLED are on in prod; the readiness gate was on for
    # this account (both turns carried `approval_readiness`).
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)


def _brief(version: int) -> CreativeBrief:
    return CreativeBrief.model_validate(copy.deepcopy(CASE[f"brief_v{version}"]))


def _r4(brief: CreativeBrief) -> Any:
    return next(req for req in brief.live() if req.id == "r4")


def _readiness(brief: CreativeBrief) -> dict[str, dict[str, Any]]:
    """Approval readiness exactly as the draft turn runs it (strategy draft, phone montage)."""
    media = copy.deepcopy(SNAPSHOT)
    manifest = resolve_creator_manifest(
        item_id=ITEM,
        edit_format="montage",
        media=[
            *(
                {"media_id": row["media_id"], "kind": "video", "duration_s": row["duration_s"]}
                for row in media["length_plan"]["sources"]
            ),
            *({"media_id": row["media_id"], "kind": "image"} for row in media["visuals"]),
        ],
    )
    report = assess_readiness(
        brief,
        copy.deepcopy(CASE["strategy"]),
        media=media,
        manifest=manifest,
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}


# -- F1 / F2: the incident -------------------------------------------------------------------


def test_f1_chip_turn_animated_title_is_not_held() -> None:
    """13:49: "Animate the title" on a phone montage whose writer draws the title entrance."""
    r4 = _readiness(_brief(7))["r4"]
    assert r4["disposition"] in APPROVABLE, r4
    assert r4["reason"] != CANT_CONFIRM, r4


def test_f2_after_make_it_without_these_the_plain_title_is_not_held() -> None:
    """13:51: brief v8 as persisted; the plain title was rendered and checked at 13:48."""
    r4 = _readiness(_brief(8))["r4"]
    assert r4["disposition"] in APPROVABLE, r4
    assert r4["reason"] != CANT_CONFIRM, r4


# -- F3: "Make it without these" really leaves the animation out ---------------------------


class _Rows:
    def __init__(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, dict[str, Any]]]:
        return self._rows


class _Db:
    """The thread's assistant events, newest first, as `_simpler_version_consent` selects them."""

    def __init__(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self._rows = rows

    def execute(self, _statement: object) -> _Rows:
        return _Rows(self._rows)


def _restated_r4(prior: CreativeBrief) -> BriefUpdate:
    """The Main Creator's 13:51 restatement of r4 (its output is redacted in prod): a complete
    replacement, as the prompt asks for a change, without the word "animated" and without the
    animation fact. Applied to brief v7 it reproduces the recorded brief v8 exactly."""
    target = _r4(prior)
    return BriefUpdate(
        operation="change",
        target_requirement_id="r4",
        expected_version=prior.version,
        kind=target.kind,
        scope=target.scope,
        literal=target.literal,
        description=f"Title: “{TITLE}”",
        facts={},
    )


def test_f3_the_simpler_version_turn_leaves_the_animation_out() -> None:
    v7 = _brief(7)
    # The 13:49 offer, rebuilt by the real code, is the one the creator answered.
    sentence, question = _simpler_version_offer(v7, ["r4"])
    recorded = CASE["offer_1349"]["payload"]["choice_question"]
    assert question["omitted_requirement_ids"] == recorded["omitted_requirement_ids"] == ["r4"]
    assert question["brief_version"] == recorded["brief_version"] == 7
    assert sentence in CASE["offer_1349"]["content"]
    # The creator's tap is a valid, named consent at the brief the offer named.
    offer = {**CASE["offer_1349"]["payload"], "choice_question": recorded}
    db = _Db([("assistant_response", offer)])
    source = SimpleNamespace(**CASE["selection_1351"])
    assert (
        _simpler_version_consent(db, uuid.uuid4(), ["r4"], brief=v7, source_event=source) == "named"
    )
    # What the turn persisted: the model's restatement merged by the real brief code.
    persisted = apply_updates(v7, [_restated_r4(v7)], source_turn_id=SIMPLER_TURN)
    recorded_v8 = _r4(_brief(8))
    assert _r4(persisted).description == recorded_v8.description
    assert _r4(persisted).facts == recorded_v8.facts
    # Once the turn persisted it, the consent the creator gave no longer counts...
    consent = _simpler_version_consent(
        db,
        uuid.uuid4(),
        ["r4"],
        brief=persisted.model_copy(update={"version": 8}),
        source_event=source,
    )
    # ...so after "Make it without these" either the server's omission runs, or the brief the
    # turn wrote no longer asks for the animation. Today neither: the animation comes back.
    assert consent is not None or not text_look_facts(_r4(persisted)), (
        consent,
        _r4(persisted).facts,
    )


# -- F4 / F5 / F6 / F7: controls (pass today; a fix must keep them) -------------------------


def test_f4_the_phone_montage_writer_draws_the_title_animation() -> None:
    """The draft renders on the phone unified montage (`creator_route`), which reads r4's
    animation into the plan it hands the phone (KRI-522)."""
    assert CASE["creator_route"]["route"] == "unified_montage"
    planned = preflight_montage_plan(
        creator_id=CREATOR,
        edit_format="montage",
        brief=_brief(7),
        strategy=copy.deepcopy(CASE["strategy"]),
        media_snapshot=copy.deepcopy(SNAPSHOT),
        visuals=[],
    )
    assert isinstance(planned, tuple), planned
    plan, _aliases = planned
    assert plan.snapshot.opening_title == TITLE
    assert plan.snapshot.title_animation == "fade"


def _rendered(entrance: str | None) -> dict[str, Any]:
    title = copy.deepcopy(CASE["rendered_title"])
    title["animation_phases"] = None if entrance is None else {"entrance": entrance, "exit": None}
    return {
        "variant_id": "guided_story",
        "resolved_archetype": CASE["rendered_archetype"],
        "text_elements": [title],
    }


def test_f5_the_after_render_review_judges_the_title_animation() -> None:
    req = _r4(_brief(7))
    [static] = build_receipts(
        [req], plan_facts_from_phone_variant(_rendered(None)), include_unchecked=True
    )
    assert (static.status, static.verification) == ("partial", "checked"), static
    [faded] = build_receipts(
        [req], plan_facts_from_phone_variant(_rendered("fade")), include_unchecked=True
    )
    assert (faded.status, faded.verification) == ("met", "checked"), faded


def test_f6_the_rest_of_the_chip_turn_is_unchanged() -> None:
    rows = _readiness(_brief(7))
    prod = CASE["prod_readiness_v7"]
    assert {k: r["disposition"] for k, r in rows.items() if k != "r4"} == {
        k: r["disposition"] for k, r in prod.items() if k != "r4"
    }
    # This replay reproduces prod's verdict on r4, word for word.
    assert prod["r4"] == {"disposition": "evidence_missing", "reason": CANT_CONFIRM}


def test_f7_the_plain_title_stays_executable() -> None:
    r4 = _readiness(_brief(6))["r4"]
    assert r4["disposition"] == "executable", r4
    assert text_look_facts(_r4(_brief(6))) == {}


def test_f8_the_chip_claims_an_editor_op_but_this_thread_always_replans() -> None:
    """Why the tap reached strategy readiness at all: the chip is offered on an allow-list as an
    editor ``patch_text``, but a creator-supplied title (13:21, creative-copy flow) makes the
    planner keep every later turn of the thread on the re-plan path (`creative_copy_pending`),
    whose draft has no text lane to read an entrance from."""
    spec = CHIP_KIND_GATE[ChipKind.TITLE_ANIMATION]
    assert spec.claimed_op == "patch_text"
    assert spec.dry_run == {"ready": "allowlist"}
    resolution = CASE["title_resolution_1321"]
    states = fold_creative_copy(
        [("assistant", resolution)],
        dependency_digest=resolution["creative_copy_resolution"]["dependency_digest"],
    )
    assert states, "creative_copy_pending would be False"
    assert states["opening_title"].approved == TITLE
