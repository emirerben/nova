"""KRI-700: "brighten the dark river clips" is never promised as brightened footage.

Tigre night-boat N2 (thread FEFCCF5C, 2026-10-15, events #22-#23), an English iPhone
Voiceover edit of dark river clips. The brief stored `r7 [style/global] brighten the dark
river clips a bit so you can see something`; the applied draft's change list and the
approval card both said "... photos popping up as reaction beats right on cue, brightened
footage, and ending on the docked boats clip." No draft can brighten anything: a strategy has
no exposure field and the phone plans only take `none` / `golden_hour` (a warm grade whose
contrast makes night clips darker). No receipt covered r7, so the unmet list never named it.

The runtime half (the draft goes ahead, and neither its change list nor the approval card
repeats the claim) is `test_kri700_brighten_ask_is_a_couldnt_line_and_never_reaches_the_
approval_card` in `test_runtime_postgres_integration.py`.

Ways this could go wrong, written down before the code (each has a case):

1. A brighten / darken ask still gets no receipt, so "Not everything you asked for made it
   in" never names it.
2. It gets a receipt, but one that is not a format limit, so a first draft turns into the
   "should I make a simpler version?" question about a plan that has nothing simpler.
3. The model's summary survives in the reply, change list or approval card while a judged
   ask is unmet (`keeps_model_summary` and the reply disagree).
4. Something that is not a light change is read as one: an ask without the model's
   `footage_light` fact (KRI-667: words are never re-read on the server), or a fact value
   outside the closed vocabulary.
5. The Turkish reply carries the English reason, or the Turkish reason stops counting as a
   format limit.
6. An editor turn, whose receipts come from the before/after diff, is rerouted.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    keeps_model_summary,
    needs_creator_choice,
    plan_facts_from_strategy,
    reply_from_receipts,
    wants_light_change,
)
from app.kria.reply_language import reply_language_for

# The N2 brief row and the summary the draft and approval card carried, verbatim.
N2_ASK = "brighten the dark river clips a bit so you can see something"
N2_SUMMARY = (
    "I'll assemble the simpler version: your voiceover with clean English captions, photos "
    "popping up as reaction beats right on cue, brightened footage, and ending on the docked "
    "boats clip."
)
R7 = BriefRequirement(
    id="r7", kind="style", scope="global", description=N2_ASK, facts={"footage_light": "brighter"}
)
TITLE = BriefRequirement(id="r2", kind="text", scope="title", literal="Tigre Night")
VOICEOVER_STRATEGY = {
    "edit_format": "narrated_planned",
    "audio_strategy": "voiceover",
    "caption_style": "clean",
    "opening_title": "Tigre Night",
}


def _style(description: str, **facts: Any) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="style", scope="global", description=description, facts=facts
    )


# 1 + 2 + 3 -------------------------------------------------------------------


def test_n2_brighten_ask_is_a_couldnt_line_not_a_simpler_version_question() -> None:
    brief = CreativeBrief(version=7, requirements=[TITLE, R7])
    facts = plan_facts_from_strategy(VOICEOVER_STRATEGY)

    receipts = build_receipts(brief.live(), facts, include_unchecked=True)
    by_id = {r.requirement_id: r for r in receipts}

    assert by_id["r2"].status == "met"
    light = by_id["r7"]
    assert (light.status, light.verification) == ("not_possible", "checked")
    assert is_format_limit(light.reason)
    assert not any(needs_creator_choice(r) for r in receipts)  # the draft goes ahead

    reply = reply_from_receipts(brief, receipts, summary=N2_SUMMARY)
    assert reply.startswith("Not everything you asked for made it in")
    assert f"Couldn't: {N2_ASK}" in reply
    assert "can't change how bright or dark clips look" in reply
    assert "brightened footage" not in reply
    # The change list and the approval card follow the reply (kria_runtime).
    assert not keeps_model_summary(brief, [r.model_dump(mode="json") for r in receipts])


def test_summary_is_kept_only_when_every_judged_ask_is_met() -> None:
    brief = CreativeBrief(version=1, requirements=[TITLE])
    facts = plan_facts_from_strategy(VOICEOVER_STRATEGY)
    receipts = build_receipts(brief.live(), facts, include_unchecked=True)

    assert keeps_model_summary(brief, receipts)
    assert reply_from_receipts(brief, receipts, summary="A night on the river.") == (
        'A night on the river.\n- Done: "Tigre Night"'
    )
    unchecked = receipts[0].model_copy(update={"verification": "unchecked"})
    assert not keeps_model_summary(brief, [unchecked])
    # A receipt for a requirement this brief no longer has decides nothing.
    stale = receipts[0].model_copy(update={"requirement_id": "r9", "status": "partial"})
    assert keeps_model_summary(brief, [*receipts, stale])


def test_a_finished_render_says_the_same_and_an_editor_turn_keeps_its_own_path() -> None:
    rendered = PlanFacts(editor=True, rendered_variant=True, edit_format="narrated_planned")
    assert check_requirement(R7, rendered).status == "not_possible"

    editor_turn = PlanFacts(editor=True, edit_format="narrated_planned")
    assert check_requirement(R7, editor_turn).status != "not_possible"


# 4 + 5 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "req",
    [
        R7,
        _style("darken the sunset shots a little", footage_light="darker"),
        _style("so you can actually see the river", footage_light=" Brighter "),
        # The fact decides on any kind the model filed the ask under.
        BriefRequirement(
            id="r1",
            kind="timing",
            scope="global",
            description="x",
            facts={"footage_light": "darker"},
        ),
    ],
)
def test_the_models_footage_light_fact_marks_a_light_change(req: BriefRequirement) -> None:
    assert wants_light_change(req)


@pytest.mark.parametrize(
    "req",
    [
        _style(N2_ASK),  # no fact: the server never re-reads the words (KRI-667)
        _style(N2_ASK, footage_light="warmer"),
        _style(N2_ASK, footage_light=True),
        _style("give it a warm golden look"),
        BriefRequirement(
            id="r1", kind="select", scope="global", description="the dark clips are junk, skip them"
        ),
    ],
)
def test_without_a_valid_fact_nothing_is_read_as_a_light_change(req: BriefRequirement) -> None:
    assert not wants_light_change(req)


def test_turkish_reply_carries_the_turkish_reason() -> None:
    req = _style("nehir kliplerini biraz aydınlat", footage_light="brighter")
    with reply_language_for("tr"):
        receipt = check_requirement(req, plan_facts_from_strategy(VOICEOVER_STRATEGY))
        reply = reply_from_receipts(
            CreativeBrief(version=1, requirements=[req]), [receipt], summary="Bir taslak."
        )
    assert receipt.status == "not_possible"
    assert receipt.reason is not None and receipt.reason.startswith("Bu düzenlemede")
    assert is_format_limit(receipt.reason)
    assert "Yapamadım: nehir kliplerini biraz aydınlat" in reply
    assert "Bir taslak." not in reply
