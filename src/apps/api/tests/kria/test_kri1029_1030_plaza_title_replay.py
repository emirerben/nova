"""KRI-1029 / KRI-1030: the Plaza de Mayo M1 title, replayed offline from the real run.

Prod dde04eb74 (Fly v1904), iPhone TestFlight 1784 (protocol 23), 2026-10-28, thread
A228E85B, plan item 08b8b60a. The creator asked: 'Curved title "Plaza de Mayo 🇦🇷" on the
palm trees'. The flag was refused (KRI-1024), so the next turn said 'Make the title just
"Plaza de Mayo", no flag'. The approved job 97d55f9a then drew:

- KRI-1030: the title twice on the palms clip, `guided-title` (top, 0-2.2 s) and
  `clip-label-unified-cut-1` (bottom, 0-2.7 s). The re-draft confirmed
  ``opening_title: "Plaza de Mayo"`` while brief r5 stayed ``text/clip:<palms>``.
- KRI-1029: no curve anywhere. Brief v2 r5 was "Curved title on the palm trees" with no fact;
  the turn-4 rewrite ("title on the palm trees") had nothing to keep, and the review said
  "Done".

``tests/fixtures/kri1029_plaza_m1_title.json`` holds the run's own brief v2 and v3, both
strategies, the approved media snapshot (seven clip records), prod's exact texts and text
elements, and prod's stored guided edit + execution plan. Everything below runs the real
server code on them: the brief merge, the phone montage planner the worker and the
pre-approval check share, the guided compiler, the phone recipe compiler with the contract
repair, and the receipts. No model, no storage, no network.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_checks import (
    build_receipts,
    is_format_limit,
    plan_facts_from_phone_variant,
    plan_facts_from_unified_montage,
)
from app.kria.reply_language import reply_language_for
from app.kria.text_shape_ask import CURVED, CURVED_TEXT_BEND, shape_in_words, text_shape

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1029_plaza_m1_title.json").read_text(
        encoding="utf-8"
    )
)
CREATOR_ID = "7b56dd6f-6b9a-4634-89c8-ce91bb50a50d"
PALMS = "analysis-proxy-ios-4869C2B5-F7C3-4304-9BA2-D713372FBD21.mp4"
TITLE = "Plaza de Mayo"
# The capabilities this recipe needs; prod's PHONE_RENDER_VERIFIED_FEATURES has them all.
PHONE_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "authoredText",
    "audioMix",
]


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod settings this chat ran under (phone pilot, clip intents + facts, textBend)."""
    for flag in (
        "phone_rendering_enabled",
        "clip_intents_enabled",
        "clip_facts_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
        "kria_phone_montage_preflight_enabled",
        "text_bend_enabled",
    ):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, "phone_render_verified_features", [*PHONE_FEATURES, "textBend"])


def prod_brief(version: str) -> CreativeBrief:
    """Brief ``"v2"`` / ``"v3"`` exactly as prod stored it."""
    return CreativeBrief.model_validate(copy.deepcopy(FIXTURE[f"brief_{version}"]))


def replayed_brief() -> CreativeBrief:
    """Brief v3 as this server builds it from the same model updates.

    Turn 1 added every v2 requirement (r1 in its turn-3 wording, which changes nothing
    here); turn 4 changed r5 to v3's words and an empty ``facts``, as prod recorded.
    """
    v2 = prod_brief("v2")
    added = apply_updates(
        CreativeBrief(),
        [
            BriefUpdate(
                kind=req.kind,
                scope=req.scope,
                literal=req.literal,
                description=req.description,
                facts=dict(req.facts),
            )
            for req in v2.requirements
        ],
        source_turn_id=None,
    ).model_copy(update={"version": 2})
    r5 = next(req for req in prod_brief("v3").requirements if req.id == "r5")
    changed = apply_updates(
        added,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id="r5",
                expected_version=2,
                kind=r5.kind,
                scope=r5.scope,
                literal=r5.literal,
                description=r5.description,
                facts={},
            )
        ],
        source_turn_id=None,
    )
    return changed.model_copy(update={"version": 3})


def plan(brief: CreativeBrief, *, client_protocol: int | None = 23):
    """The phone montage plan the worker builds (shared with the pre-approval check)."""
    from app.services.phone_montage_preflight import preflight_montage_plan  # noqa: PLC0415

    planned = preflight_montage_plan(
        creator_id=CREATOR_ID,
        edit_format="montage",
        brief=brief,
        strategy=copy.deepcopy(FIXTURE["strategy_t5"]),
        media_snapshot=copy.deepcopy(FIXTURE["media_snapshot"]),
        visuals=[],
        client_protocol=client_protocol,
    )
    assert isinstance(planned, tuple), planned
    return planned[0]


def text_elements(unified) -> list[dict]:  # noqa: ANN001
    from app.pipeline.guided_story import compile_proposal_execution_plan  # noqa: PLC0415

    return compile_proposal_execution_plan(unified.snapshot)["text_elements"]


def titles(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["text"] == TITLE]


def phone_recipe(brief: CreativeBrief, unified):  # noqa: ANN001
    """The worker's phone compile: guided plan -> contract repair -> recipe."""
    from app.pipeline.guided_story import (  # noqa: PLC0415
        GuidedStoryExecutionPlan,
        compile_proposal_execution_plan,
    )
    from app.pipeline.phone_plan_repair import (  # noqa: PLC0415
        _dry_run_bindings,
        compile_phone_guided_repaired,
    )
    from app.services.creator_render_contract import (  # noqa: PLC0415
        build_render_contract,
        verify_phone_recipe,
    )

    guided = GuidedStoryExecutionPlan.model_validate(
        compile_proposal_execution_plan(unified.snapshot)
    )
    media = copy.deepcopy(FIXTURE["media_snapshot"])
    bindings, visuals, narration = _dry_run_bindings(
        guided, unified.snapshot, media["clip_assignments"]
    )
    contract = build_render_contract(
        copy.deepcopy(FIXTURE["strategy_t5"]),
        generation_id="kri1029-replay",
        brief=brief,
        media_snapshot=media,
    )
    compiled = compile_phone_guided_repaired(
        guided,
        bindings,
        contract=contract,
        visuals=visuals,
        narration=narration,
        landscape_fit="fit",
    )
    source_audio = {row["media_id"]: bool(row["has_audio"]) for row in media["clip_assignments"]}
    verify_phone_recipe(contract, compiled.recipe, source_audio=source_audio)
    return compiled, contract


def r5_receipt(brief: CreativeBrief, facts) -> dict:  # noqa: ANN001
    with reply_language_for("en"):
        receipts = build_receipts(brief.live(), facts, include_unchecked=True)
    return next(r.model_dump(mode="json") for r in receipts if r.requirement_id == "r5")


# -- the fixture is the incident ----------------------------------------------------------


def test_prod_drew_the_title_twice_and_straight() -> None:
    drawn = titles(FIXTURE["prod_text_elements"])
    assert [(row["id"], row["y_frac"], row["bend"]) for row in drawn] == [
        ("guided-title", 0.16, None),
        ("clip-label-unified-cut-1", 0.78, None),
    ]
    assert [(row["role"], row["media_id"]) for row in FIXTURE["prod_exact_texts"][:2]] == [
        ("opening", None),
        ("clip", PALMS),
    ]
    assert FIXTURE["strategy_t5"]["opening_title"] == TITLE
    r5 = {req["id"]: req for req in FIXTURE["brief_v2"]["requirements"]}["r5"]
    assert r5["description"] == "Curved title on the palm trees" and r5["facts"] == {}
    r5 = {req["id"]: req for req in FIXTURE["brief_v3"]["requirements"]}["r5"]
    assert r5["description"] == "title on the palm trees" and r5["facts"] == {}


# -- KRI-1029: the curve reaches the brief, and survives the rewrite -----------------------


def test_the_curve_survives_the_title_rewrite() -> None:
    brief = replayed_brief()
    r5 = next(req for req in brief.live() if req.id == "r5")
    assert (r5.literal, r5.description) == (TITLE, "title on the palm trees")
    assert r5.facts == {"text_shape": CURVED}
    assert text_shape(r5) == CURVED
    # Nothing else in the brief picked up a shape.
    assert [req.id for req in brief.live() if text_shape(req)] == ["r5"]


# -- KRI-1030: one title, the creator's own, where they put it -----------------------------


def test_the_title_is_drawn_once_on_the_palms() -> None:
    unified = plan(prod_brief("v3"))
    drawn = titles(text_elements(unified))
    assert [(row["id"], row["y_frac"], row["start_s"]) for row in drawn] == [
        ("clip-label-unified-cut-1", 0.78, 0.0)
    ]
    assert unified.snapshot.fast_cuts[0].media_id == PALMS
    record = unified.record()
    assert record["title"] is None and record["title_shown_as_label"] == PALMS
    # The other labels are untouched.
    assert [row["text"] for row in record["labels"]] == [TITLE, "Casa Rosada", "Catedral"]


def test_the_first_draft_never_makes_a_label_the_opening_title() -> None:
    """The failed first job 40684a25 (strategy t3: the title as a label intent on the palms,
    no ``opening_title``), replayed with its own strategy, media snapshot and length plan.

    Its brief files 'label "Casa Rosada" on the pink building' as global text, and ``_title``
    took that first global literal as the opening title: "Casa Rosada" at the top over the
    palm trees, then again on the pink building (found by the KRI-1024 session once the flag
    stopped refusing the render). The flag is written as the creator wrote it the next turn,
    so this checks the title alone (KRI-1024 owns the flag).
    """

    def unflagged(value):  # noqa: ANN001, ANN202
        return json.loads(json.dumps(value).replace(" \\ud83c\\udde6\\ud83c\\uddf7", ""))

    from app.services.phone_montage_preflight import preflight_montage_plan  # noqa: PLC0415

    brief = CreativeBrief.model_validate(unflagged(FIXTURE["brief_v2"]))
    planned = preflight_montage_plan(
        creator_id=CREATOR_ID,
        edit_format="montage",
        brief=brief,
        strategy=unflagged(FIXTURE["first_job_strategy"]),
        media_snapshot=copy.deepcopy(FIXTURE["first_job_media_snapshot"]),
        visuals=[],
        client_protocol=23,
    )
    assert isinstance(planned, tuple), planned
    rows = text_elements(planned[0])
    assert [row["text"] for row in rows if row["start_s"] == 0.0] == [TITLE]
    for words in (TITLE, "Casa Rosada", "Catedral"):
        assert [row["text"] for row in rows].count(words) == 1, words
    record = planned[0].record()
    assert (record["title"], record["title_source"]) == (None, "none")


def test_a_title_ask_reads_the_label_as_the_title() -> None:
    """A brief that also says "add a title" (no words) is still a "Done": the opening text is
    the creator's own label, drawn once from 0 s."""
    brief = apply_updates(
        prod_brief("v3"),
        [BriefUpdate(kind="text", scope="title", description="add a title")],
        source_turn_id=None,
    )
    asked = brief.live()[-1]
    unified = plan(brief)
    assert unified.record()["title_shown_as_label"] == PALMS
    with reply_language_for("en"):
        receipts = build_receipts(
            brief.live(), plan_facts_from_unified_montage(unified.record()), include_unchecked=True
        )
    receipt = next(r for r in receipts if r.requirement_id == asked.id)
    assert (receipt.status, receipt.reason) == ("met", None)


def test_the_phone_recipe_draws_one_curved_title_and_the_contract_accepts_it() -> None:
    brief = replayed_brief()
    unified = plan(brief)
    compiled, contract = phone_recipe(brief, unified)
    # The contract still pins the words twice (opening + clip): the label at 0 s on the
    # palms answers both, so the repair puts nothing back (no second copy).
    assert [row.role for row in contract.exact_texts if row.text == TITLE] == ["opening", "clip"]
    assert compiled.notes == []

    def words(layer) -> str:  # noqa: ANN001
        return "".join(run.text for run in layer.runs)

    layers = [layer for layer in compiled.recipe.text_layers if words(layer) == TITLE]
    assert [layer.bend for layer in layers] == [CURVED_TEXT_BEND]
    assert layers[0].start == 0.0
    assert "textBend" in compiled.recipe.required_capabilities
    others = [layer for layer in compiled.recipe.text_layers if words(layer) != TITLE]
    assert others and all(layer.bend is None for layer in others)


def test_the_draft_says_done_when_the_phone_bends_it() -> None:
    from app.services.phone_montage_preflight import montage_blockers  # noqa: PLC0415

    with reply_language_for("en"):
        rows = montage_blockers(
            creator_id=CREATOR_ID,
            edit_format="montage",
            brief=replayed_brief(),
            strategy=copy.deepcopy(FIXTURE["strategy_t5"]),
            media_snapshot=copy.deepcopy(FIXTURE["media_snapshot"]),
            visuals=[],
            client_protocol=23,
        )
    r5 = {row.requirement_id: row for row in rows or []}["r5"]
    assert (r5.status, r5.verification, r5.reason) == ("met", "checked", "curved")


@pytest.mark.parametrize(
    ("client_protocol", "verified"),
    [
        (None, [*PHONE_FEATURES, "textBend"]),
        (8, [*PHONE_FEATURES, "textBend"]),
        (23, PHONE_FEATURES),
    ],
    ids=["unstamped-build", "build-before-bend", "bend-not-verified"],
)
def test_a_phone_that_cant_bend_gets_a_straight_title_and_an_honest_draft(
    monkeypatch: pytest.MonkeyPatch, client_protocol: int | None, verified: list[str]
) -> None:
    from app.services.phone_montage_preflight import montage_blockers  # noqa: PLC0415

    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    brief = replayed_brief()
    unified = plan(brief, client_protocol=client_protocol)
    assert [row.get("bend") for row in titles(text_elements(unified))] == [None]
    assert unified.record()["text_shapes"] == [
        {"text": TITLE, "role": "label", "media_id": PALMS, "bend": None, "reason": "phone"}
    ]
    with reply_language_for("en"):
        rows = montage_blockers(
            creator_id=CREATOR_ID,
            edit_format="montage",
            brief=brief,
            strategy=copy.deepcopy(FIXTURE["strategy_t5"]),
            media_snapshot=copy.deepcopy(FIXTURE["media_snapshot"]),
            visuals=[],
            client_protocol=client_protocol,
        )
    r5 = {row.requirement_id: row for row in rows or []}["r5"]
    assert r5.status == "not_possible" and r5.verification == "checked"
    assert r5.reason == "I can't curve text in this edit on your iPhone yet, so it stays straight"
    # A limit of the edit, said before approval: never a "simpler version?" question.
    assert is_format_limit(r5.reason)


@pytest.mark.parametrize("client_protocol", [23, None], ids=["bends", "stays-straight"])
def test_the_text_composer_is_not_called_for_the_curve(
    monkeypatch: pytest.MonkeyPatch, client_protocol: int | None
) -> None:
    """The worker's creation-text step skips its model when the plan already shows every
    asked text. A curve the phone can't draw is no reason to call it (no new model cost, no
    rewritten copy); the receipt says the text stays straight instead.

    The M1 brief itself always reaches the model (its "make it all vertical" and the palms
    label are not provable on the text lane, before and after this change), so this keeps
    only the asks the lane can prove, with the curved title as a title-scope ask.
    """
    import app.services.creation_text_composition as composition  # noqa: PLC0415
    from app.services.creation_text_composition import (  # noqa: PLC0415
        compose_creation_text,
        snapshot_copy_constraints,
    )

    def no_model(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("the text composer model must not run")

    monkeypatch.setattr(composition.CreationTextComposer, "run", no_model)
    monkeypatch.setattr(composition.CreationTextComposerFast, "run", no_model)
    kept = [req for req in prod_brief("v3").requirements if req.id not in {"r2", "r3", "r5"}]
    brief = apply_updates(
        CreativeBrief(version=3, requirements=kept),
        [BriefUpdate(kind="text", scope="title", literal=TITLE, description="Curved title")],
        source_turn_id=None,
    )
    unified = plan(brief, client_protocol=client_protocol)
    bends = [row.get("bend") for row in titles(text_elements(unified))]
    assert bends == ([CURVED_TEXT_BEND] if client_protocol else [None])
    composed = compose_creation_text(
        unified.snapshot,
        creator_request="\n".join(FIXTURE["creator_messages"]),
        copy_constraints=snapshot_copy_constraints(unified.snapshot),
        text_requirements=brief.live(),
    )
    assert composed.text_composition is not None
    assert composed.text_composition.operations == []


def worker_r5(brief: CreativeBrief, record: dict) -> dict:
    """r5 as the finished video's review shows it.

    A finished phone montage's review keeps the receipts the worker judged on its plan
    record when it laid the video out (``kria_runtime``: ``requirement_receipts``); a text
    ask is one of them.
    """
    return r5_receipt(brief, plan_facts_from_unified_montage(record))


def test_prods_review_said_done_for_the_title() -> None:
    """Faithfulness: prod's own brief and plan record give prod's own stored "Done"."""
    stored = {
        row["requirement_id"]: row for row in FIXTURE["prod_unified_record"]["requirement_receipts"]
    }["r5"]
    r5 = worker_r5(prod_brief("v3"), FIXTURE["prod_unified_record"])
    assert (r5["status"], r5["reason"]) == (stored["status"], stored["reason"]) == ("met", None)


def test_the_review_checks_the_curve() -> None:
    brief = replayed_brief()
    # Wrong-content control: prod's own plan, the same words on the same clip, straight.
    # With the creator's curve on the brief, the words alone are not a "Done" any more.
    prod = worker_r5(brief, FIXTURE["prod_unified_record"])
    assert prod["status"] != "met"
    ours = worker_r5(brief, plan(brief).record())
    assert (ours["status"], ours["reason"]) == ("met", "curved")
    # A phone that can't bend: the review says why, never "Done".
    straight = worker_r5(brief, plan(brief, client_protocol=None).record())
    assert (straight["status"], straight["reason"]) == (
        "not_possible",
        "I can't curve text in this edit on your iPhone yet, so it stays straight",
    )


def test_a_saved_row_without_the_bend_is_not_curved() -> None:
    """After an editor turn the saved rows decide: a row that lost its bend is a miss."""
    from app.kria.brief_checks import check_text_shape  # noqa: PLC0415

    brief = replayed_brief()
    r5 = next(req for req in brief.live() if req.id == "r5")
    rows = text_elements(plan(brief))
    bent = check_text_shape(r5, plan_facts_from_phone_variant({"text_elements": rows}))
    assert bent is not None and (bent.status, bent.reason) == ("met", "curved")
    straight = [{k: v for k, v in row.items() if k != "bend"} for row in rows]
    lost = check_text_shape(r5, plan_facts_from_phone_variant({"text_elements": straight}))
    assert lost is not None and (lost.status, lost.reason) == (
        "not_possible",
        "The text is straight in the finished video, not curved",
    )


# -- old plans stay byte-identical ----------------------------------------------------------


def test_prods_approved_plan_still_validates() -> None:
    """The guided compiler re-validates a stored plan by recompiling its snapshot; the
    approved job's plan (title twice, no bend) must still be accepted unchanged."""
    from app.pipeline.guided_story import validate_execution_plan  # noqa: PLC0415

    validated = validate_execution_plan(
        copy.deepcopy(FIXTURE["prod_execution_plan"]), copy.deepcopy(FIXTURE["prod_guided_edit"])
    )
    assert [row["id"] for row in titles(validated["text_elements"])] == [
        "guided-title",
        "clip-label-unified-cut-1",
    ]


# -- the words that ask for a shape (EN / TR), and the ones that don't ----------------------


@pytest.mark.parametrize(
    ("words", "shape"),
    [
        ("Curved title on the palm trees", "curved"),
        ("curved white title over the trees", "curved"),
        ("make the title curved", "curved"),
        ("the title should be arched", "curved"),
        ("title on an arc over the square", "curved"),
        ("curve the title over the palms", "curved"),
        ("bent text on the first clip", "curved"),
        ("Kavisli başlık palmiyelerin üstünde", "curved"),
        ("başlık yay şeklinde olsun", "curved"),
        ("make the title straight", "straight"),
        ("the title should not be curved", "straight"),
        ("don't curve the title", "straight"),
        ("remove the curve", "straight"),
        ("başlık kavisli olmasın", "straight"),
        # The shape belongs to something else, or nothing asks for one.
        ("label on the curved bridge", None),
        ("the curved bridge label", None),
        ("title on the palm trees", None),
        ("a bend in the road with the label", None),
        ("rainbow title", None),
        # Two shapes in one sentence: the words don't say which text gets which.
        ("curved title and straight labels", None),
    ],
)
def test_which_words_ask_for_a_shape(words: str, shape: str | None) -> None:
    assert shape_in_words(words) == shape


def test_a_later_straight_ask_wins_and_a_junk_fact_is_dropped() -> None:
    brief = replayed_brief()
    straightened = apply_updates(
        brief,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id="r5",
                expected_version=3,
                kind="text",
                scope=f"clip:{PALMS}",
                literal=TITLE,
                description="make the title straight on the palm trees",
            )
        ],
        source_turn_id=None,
    )
    r5 = next(req for req in straightened.live() if req.id == "r5")
    assert r5.facts == {"text_shape": "straight"}
    junk = apply_updates(
        CreativeBrief(),
        [BriefUpdate(kind="text", scope="title", literal=TITLE, facts={"text_shape": "wavy"})],
        source_turn_id=None,
    )
    assert junk.requirements[0].facts == {}
