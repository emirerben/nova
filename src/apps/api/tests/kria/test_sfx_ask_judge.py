"""KRI-1561 PR 6: a general sound-effect ask is judged by what the worker did.

Before this, "add sound effects where they fit" on a Talking edit had no checker and got the
catch-all "A Talking video can't do this, so it stays as filmed", even when effects were placed.

Failure modes, written before the tests:

1. A placed render still answers with the catch-all (or says nothing), so the creator is told the
   opposite of what they got.
2. A declined render answers with the catch-all instead of the real limit (a closed gate, no
   timed words, an empty catalogue).
3. A render with no receipt is called "met" because a treatment is stored.
4. A draft is held at approval because its promise is read as a shortfall.
5. Another format reads differently from before the typed fact existed.
6. The brief writer is taught the fact with every gate closed (every prompt would change bytes).
"""

from __future__ import annotations

import dataclasses

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    _SFX_NOT_PLACED,
    _TALKING_NO_SUCH_EDIT,
    PlanFacts,
    build_receipts,
    is_judged,
    plan_facts_from_phone_variant,
)
from app.kria.reply_language import reply_language_for
from app.services.sfx_capability import limit_line
from app.services.sfx_chooser import SfxLimit

ASK_EN = BriefRequirement(
    id="s1",
    kind="audio",
    scope="global",
    description="add sound effects where they fit",
    facts={"audio_intent": "sound_effects"},
)
ASK_TR = ASK_EN.model_copy(update={"description": "uygun yerlere ses efekti ekle"})
UNTYPED = ASK_EN.model_copy(update={"facts": {}})


def _talking(**changes) -> PlanFacts:
    return PlanFacts(edit_format="subtitled", phone_talking=True, **changes)


def _receipt_of(req, facts):
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt


def _placed(count: int) -> dict:
    rows = [{"id": f"ask-sfx-{i}", "effect_id": "fx-pop", "at_s": 1.0 + i} for i in range(count)]
    return {"version": 1, "asked": True, "placed": rows, "declined": None}


def test_a_render_that_placed_effects_says_so_and_never_the_catch_all():
    facts = _talking(rendered_variant=True, sfx_choice=_placed(3))
    receipt = _receipt_of(ASK_EN, facts)

    assert receipt.status == "met" and receipt.stage == "checked"
    assert receipt.reason == "I added 3 sound effects"
    assert _TALKING_NO_SUCH_EDIT not in str(receipt.reason)
    assert is_judged(ASK_EN, receipt)
    with reply_language_for("tr"):
        assert _receipt_of(ASK_TR, facts).reason == "3 ses efekti ekledim"
    assert _receipt_of(ASK_EN, _talking(rendered_variant=True, sfx_choice=_placed(1))).reason == (
        "I added 1 sound effect"
    )


@pytest.mark.parametrize("limit", list(SfxLimit))
def test_a_render_that_declined_names_the_real_limit(limit):
    choice = {"version": 1, "asked": True, "placed": [], "declined": limit.value}
    receipt = _receipt_of(ASK_EN, _talking(rendered_variant=True, sfx_choice=choice))

    assert receipt.status == "not_possible" and receipt.stage == "checked"
    assert receipt.reason == limit_line(limit)
    assert receipt.reason != _TALKING_NO_SUCH_EDIT


def test_a_talking_render_with_no_receipt_is_never_met():
    """Negative control: the ask exists, nothing was served (the planner never set it)."""
    receipt = _receipt_of(ASK_EN, _talking(rendered_variant=True))
    assert receipt.status == "not_possible"
    assert receipt.reason == _SFX_NOT_PLACED


def test_a_rendered_voiceover_montage_is_judged_from_its_own_receipt():
    facts = PlanFacts(
        edit_format="voiceover",
        audio_strategy="voiceover",
        voiceover_edit=True,
        rendered_variant=True,
        sfx_choice=_placed(2),
    )
    receipt = _receipt_of(ASK_EN, facts)
    assert receipt.status == "met" and receipt.reason == "I added 2 sound effects"


def test_a_draft_promises_the_effects_without_holding_approval(monkeypatch):
    from app.kria.approval_readiness import _promised_at_render
    from app.services import sfx_capability

    monkeypatch.setattr(sfx_capability, "subtitled_sfx_limit", lambda: None)
    facts = _talking()
    receipt = _receipt_of(ASK_EN, facts)

    assert receipt.status == "partial"
    assert receipt.reason == "I'll place sound effects where your speech and pauses call for them"
    assert _promised_at_render(ASK_EN, receipt, facts)
    with reply_language_for("tr"):
        turkish = _receipt_of(ASK_TR, facts)
    assert _promised_at_render(ASK_TR, turkish, facts)


def test_a_draft_with_the_gate_closed_names_the_closed_gate(monkeypatch):
    from app.services import sfx_capability

    monkeypatch.setattr(sfx_capability, "subtitled_sfx_limit", lambda: SfxLimit.flag_off)
    receipt = _receipt_of(ASK_EN, _talking())
    assert receipt.status == "not_possible"
    assert receipt.reason == limit_line(SfxLimit.flag_off)


@pytest.mark.parametrize(
    "facts",
    [
        PlanFacts(edit_format="montage", audio_strategy="licensed_music"),
        PlanFacts(edit_format="montage", audio_strategy="voiceover"),
        PlanFacts(edit_format="voiceover", audio_strategy="voiceover", voiceover_edit=True),
    ],
    ids=["montage", "montage_voiceover_draft", "voiceover_without_receipt"],
)
def test_an_ask_on_other_footage_reads_as_it_did_untyped(facts):
    typed = _receipt_of(ASK_EN, facts)
    untyped = _receipt_of(UNTYPED, facts)
    assert typed.model_dump() == untyped.model_dump()
    assert is_judged(ASK_EN, typed) == is_judged(UNTYPED, untyped)


def test_the_receipt_loader_reads_the_workers_record():
    variant = {"resolved_archetype": "narrated", "phone_sfx_choice_receipt": _placed(2)}
    facts = plan_facts_from_phone_variant(variant)
    assert facts.sfx_choice == _placed(2)
    assert plan_facts_from_phone_variant({"resolved_archetype": "narrated"}).sfx_choice is None


def test_the_brief_writer_is_taught_the_fact_only_while_a_gate_is_open(monkeypatch):
    from app.agents import main_creator

    shut = main_creator.brief_prompt_section()
    assert '"audio_intent": "sound_effects"' not in shut

    monkeypatch.setattr(main_creator, "sfx_ask_taught", lambda: True)
    opened = main_creator.brief_prompt_section()
    assert '"audio_intent": "sound_effects"' in opened
    assert opened.replace(main_creator._BRIEF_SFX_ASK_SENTENCE, "") == shut


# --- the real chain: the Talking worker's receipt, read by the real facts loader and judge ----


def _judged_after_render(monkeypatch, *, closed_gate: bool):
    from app.tasks import generative_build as gb
    from tests.tasks.test_kri1561_phone_sfx_ask_worker import (
        ASK,
        SPOKEN,
        _agent_picks,
        _open_gates,
        _run,
    )
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

    if closed_gate:
        job, _snapshot, _session, _binding = _setup_subtitled(
            monkeypatch, transcript_words=SPOKEN, duration_s=16.0
        )
        _open_gates(monkeypatch)
        monkeypatch.setattr(gb.settings, "phone_subtitled_editor_lanes_enabled", False)
        _agent_picks(monkeypatch, ("fx-pop", 2.0))
        job.all_candidates["creator_strategy"] = {"sfx_ask": ASK}
        gb._run_generative_job(str(job.id))
    else:
        job, _payloads, _compiled = _run(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    facts = dataclasses.replace(
        plan_facts_from_phone_variant(variant), rendered_variant=True, phone_talking=True
    )
    return _receipt_of(ASK_EN, facts), variant


def test_a_real_render_that_placed_effects_is_judged_met(monkeypatch):
    receipt, variant = _judged_after_render(monkeypatch, closed_gate=False)
    placed = len(variant["phone_sfx_choice_receipt"]["placed"])

    assert placed == 2
    assert receipt.status == "met"
    assert receipt.reason == f"I added {placed} sound effects"


def test_a_real_render_with_a_closed_gate_is_judged_with_that_gate(monkeypatch):
    receipt, variant = _judged_after_render(monkeypatch, closed_gate=True)

    assert variant["phone_sfx_choice_receipt"]["declined"] == "flag_off"
    assert receipt.status == "not_possible"
    assert receipt.reason == limit_line(SfxLimit.flag_off)
