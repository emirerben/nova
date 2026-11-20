"""KRI-1508: a draft whose asks are all render promises never opens "Not everything ...".

Cusco DJI T2 "list 1" (prod 2026-11-16 21:53:32Z, thread ca6a8138, event #7 `draft_applied`,
build 2015, server 6d36200c4). The request: 'I list four lessons in this video. Add a label when
each one starts: "1. Esneklik", "2. Kilitli sistemler", "3. Form", "4. Doğayı biliyordu".
Turkish captions.' Every ask was doable and was delivered, yet the draft (and the approval card,
which repeats it) opened "Not everything you asked for made it in:" and listed the plain Turkish
captions ask as "Partly". Root cause: approval readiness already treats the phone Talking
captions plan as a render promise (`talking_caption_promise`, KRI-1405), but the reply's own
check (`_planned_at_render`) only knew the fixed promise sentences, so the captions row counted
as a shortfall.

Real: the brief rows as filed (the job's `creator_brief_binding`), `build_receipts` on a phone
Talking draft's facts, `assess_readiness`'s promise test and the reply composer. No model call.

Ways the fix can fail, each pinned below:
 1. the captions promise still counts as a miss (today's bug): the replay;
 2. a captions ask Nova CAN'T fully do (a language it can't caption, spellings still to write)
    becomes a promise: it must stay "Partly" and keep the opening line;
 3. a real shortfall next to the promises no longer opens "Not everything ...";
 4. the reply and the readiness card disagree about which rows are promises;
 5. a Turkish (or German) chat still opens with the shortfall line;
 6. the lead line counts a promise as "Partly done".
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.services.client_protocol import clear_request_context
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile

TURN = "79868d56-55c7-4100-a726-9ec63e6b60ba"
LABELS = ("1. Esneklik", "2. Kilitli sistemler", "3. Form", "4. Doğayı biliyordu")
DESCRIPTIONS = (
    "Show this label when the first lesson starts, as the first of four successive lesson labels.",
    "Show this label when the second lesson starts, replacing the preceding lesson label.",
    "Show this label when the third lesson starts, replacing the preceding lesson label.",
    "Show this label when the fourth lesson starts, replacing the preceding lesson label.",
)
LABEL_REASON = (
    "I'll show it when that part of your talk starts; if I don't hear where it starts, "
    "it won't appear"
)
CAPTIONS_REASON = (
    "Every voice in your take gets captions, off-camera ones too; they'll be in Turkish, and "
    "I'll check that in the finished video"
)
# Event #7's content, up to the notices the planner added after it ("Heads-up for this video").
PROD_DRAFT = (
    "Not everything you asked for made it in:\n"
    + "".join(
        f'- Will do at render: {d} ("{label}") ({LABEL_REASON})\n'
        for d, label in zip(DESCRIPTIONS, LABELS, strict=True)
    )
    + f"- Partly: Turkish captions. ({CAPTIONS_REASON})"
)


def _r(rid, kind, description, *, literal=None, facts=None, scope="per_clip"):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope=scope,
        description=description,
        literal=literal,
        facts=facts or {},
        source_turn_id=TURN,
    )


CAPTIONS = _r(
    "r5",
    "style",
    "Turkish captions.",
    scope="global",
    facts={"captions": "on", "caption_language": "tr"},
)
BRIEF = CreativeBrief(
    version=1,
    requirements=[
        *(
            _r(f"r{i}", "text", d, literal=label)
            for i, (d, label) in enumerate(zip(DESCRIPTIONS, LABELS, strict=True), start=1)
        ),
        CAPTIONS,
    ],
)
STRATEGY = {"edit_format": "subtitled", "audio_strategy": "original_audio"}


@pytest.fixture(autouse=True)
def prod(monkeypatch):
    apply_prod_profile(monkeypatch)
    clear_request_context()
    monkeypatch.setattr(settings, "spoken_list_labels_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))


def _facts() -> bc.PlanFacts:
    """A phone Talking draft: no captions drawn yet, the writer captions at render."""
    return dataclasses.replace(
        bc.plan_facts_from_strategy(STRATEGY), phone_talking=True, caption_words_at_render=True
    )


def _draft(brief: CreativeBrief = BRIEF, facts: bc.PlanFacts | None = None):
    facts = facts or _facts()
    receipts = bc.build_receipts(brief.live(), facts, include_unchecked=True)
    return receipts, facts


def test_the_draft_receipts_are_what_prod_stored():
    receipts, _facts_ = _draft()
    assert [(r.requirement_id, r.status, r.reason) for r in receipts] == [
        *((f"r{i}", "partial", LABEL_REASON) for i in range(1, 5)),
        ("r5", "partial", CAPTIONS_REASON),
    ]


def test_control_without_the_promise_ids_the_reply_is_prods():
    receipts, _facts_ = _draft()
    reply, _lead = bc.reply_and_lead_from_receipts(BRIEF, receipts)
    assert reply == PROD_DRAFT


def test_a_draft_of_promises_opens_with_the_plan_not_a_shortfall():
    receipts, facts = _draft()
    planned = bc.render_promise_ids(BRIEF.live(), receipts, facts)
    # The labels' reason is a fixed promise sentence already; the captions plan needs the facts.
    assert planned == {"r5"}
    reply, lead = bc.reply_and_lead_from_receipts(
        BRIEF, receipts, summary="Here's the plan.", planned_ids=planned
    )
    assert "Not everything" not in reply
    assert reply.startswith("Here's the plan.\n")
    assert f"- Will do at render: Turkish captions. ({CAPTIONS_REASON})" in reply
    assert "Partly" not in reply
    rows = [line for line in reply.splitlines() if line.startswith("- ")]
    assert len(rows) == 5 and all(row.startswith("- Will do at render: ") for row in rows)
    assert lead == ""  # the planner's own summary is kept: nothing failed


def test_the_reply_and_readiness_agree_on_every_row():
    from app.kria.approval_readiness import _promised_at_render

    receipts, facts = _draft()
    by_id = {req.id: req for req in BRIEF.live()}
    readiness = {
        r.requirement_id for r in receipts if _promised_at_render(by_id[r.requirement_id], r, facts)
    }
    assert bc.render_promise_ids(BRIEF.live(), receipts, facts) <= readiness
    assert "r5" in readiness


@pytest.mark.parametrize(
    ("description", "facts", "shortfall"),
    [
        # German without the translation step: Nova says it can't, the captions stay spoken.
        (
            "German captions.",
            {"captions": "on", "caption_language": "de"},
            "I can't make German captions yet",
        ),
        # A spelling ask with no words to spell yet: the creator still has to write them.
        (
            "Turkish captions, and spell every name correctly.",
            {"captions": "on", "caption_language": "tr"},
            "write out the words to spell",
        ),
    ],
)
def test_a_captions_ask_nova_cannot_fully_do_stays_partly(
    monkeypatch, description, facts, shortfall
):
    monkeypatch.setattr(settings, "caption_translation_enabled", False)
    req = _r("r5", "style", description, scope="global", facts=facts)
    brief = CreativeBrief(version=1, requirements=[req])
    receipts, plan = _draft(brief)
    [receipt] = receipts
    assert receipt.status == "partial" and shortfall in (receipt.reason or "")
    planned = bc.render_promise_ids(brief.live(), receipts, plan)
    assert planned == frozenset()
    reply, _lead = bc.reply_and_lead_from_receipts(brief, receipts, planned_ids=planned)
    assert reply.startswith("Not everything you asked for made it in:")
    assert "- Partly: " in reply


def test_a_real_shortfall_next_to_the_promises_still_opens_with_it():
    receipts, facts = _draft()
    shortfall = receipts[0].model_copy(update={"status": "not_possible", "reason": "No."})
    receipts = [shortfall, *receipts[1:]]
    planned = bc.render_promise_ids(BRIEF.live(), receipts, facts)
    assert "r1" not in planned
    reply, _lead = bc.reply_and_lead_from_receipts(BRIEF, receipts, planned_ids=planned)
    assert reply.startswith("Not everything you asked for made it in:")
    assert "- Will do at render: Turkish captions." in reply


@pytest.mark.parametrize("language", ["tr", "de"])
def test_a_turkish_or_german_chat_opens_without_the_shortfall_line(language):
    token = bind_reply_language(language)
    try:
        receipts, facts = _draft()
        planned = bc.render_promise_ids(BRIEF.live(), receipts, facts)
        reply, _lead = bc.reply_and_lead_from_receipts(BRIEF, receipts, planned_ids=planned)
        control, _ = bc.reply_and_lead_from_receipts(BRIEF, receipts)
    finally:
        release_reply_language(token)
    assert planned == {"r5"}
    opening = control.splitlines()[0]
    assert reply.splitlines()[0] != opening
    assert not reply.startswith(opening)


def test_the_lead_never_counts_a_promise_as_partly_done():
    receipts, facts = _draft()
    planned = bc.render_promise_ids(BRIEF.live(), receipts, facts)
    shortfall = receipts[0].model_copy(update={"status": "not_possible", "reason": "No."})
    _reply, lead = bc.reply_and_lead_from_receipts(
        BRIEF, [shortfall, *receipts[1:]], planned_ids=planned - {"r1"}
    )
    assert lead == "I couldn't do that."
