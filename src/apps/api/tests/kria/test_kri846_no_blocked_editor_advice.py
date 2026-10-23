"""KRI-846: a named phrase cut never sends the creator to an editor action that is blocked.

Prod (café kit T1 Talking, phone): the creator wrote Cut her slip "with bread, no," so it says
"waffle cookie with blackberry jam". Also cut my half question "how are you gonna". After
KRI-792 the draft, the approval card and the finished-video receipt said "a retake or a
specific line isn't cut automatically yet, so trim that in the editor", and the finished
reply ended "... a sentence you restarted stays in; trim it in the editor." The iPhone editor
cannot do that: a talking-source slot cannot be trimmed (KRI-770), Range is locked on a fresh
draft (`canEditTimeline` false, server `timeline` capability false), there is no split, and the
server pins every talking-source window (`authored_editor.py`: 409 TIMELINE_STALE). Deleting
the whole kept segment also deletes wanted words. On Narrated the editor trims picture clips,
not the recorded voice. So the advice was false on every speech format.

Decision: copy only. Say plainly the line can't be cut yet and stays in. The real phrase cut
is KRI-724.

Failure modes, written before the fix:
1. One static string serves every speech format (Talking, cloud Talking, Narrated): any claim
   in it must be true for all of them, so it names no format-specific action.
2. Editor wording kept "for Narrated only": the narrated editor trims pictures, so a line of
   the voice still cannot be cut there.
3. A different editor action takes the place ("delete that piece", "use Range", "drag the
   handles"): each is blocked, unverified or removes wanted words.
4. The finished-reply footer keeps the editor tail: it is appended to EVERY cleaned Talking or
   Narrated render, even when no line cut was asked for.
5. The footer and the receipt both state the limit: the dedupe compares text, so a reworded
   receipt would repeat it.
6. A promise of a future feature ("soon", "coming"): nothing here is scheduled.
7. The line is built from the creator's phrase: `_loc` translates exact static keys only, so a
   Turkish chat would read English. One static EN string, one TR twin.
8. The copy stops saying "stays in" or flips the status to "met": the KRI-792 false "Done"
   returns. It must stay "partial", the cleanup lead stays FIRST (`is_format_limit` matches the
   lead, so the honest Partly still never asks "simpler version?").
9. The iOS approval card (`SpeechCleanupOffer.swift`) has hard-coded English that no server fix
   reaches; it must stop pointing at the editor too.
10. The copy outlives the refusal that justified it. The guard at the bottom pins the server's
    window pin and fails on purpose when talking-source windows become editable (KRI-724), so
    the copy is reconsidered then.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.kria import brief_checks
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    reply_from_receipts,
    speech_cleanup_review_note,
)
from app.kria.reply_language import reply_language_for
from tests.kria.test_kri792_named_phrase_cuts import (
    _HALF_QUESTION,
    _SLIP,
    _by_id,
    _draft_facts,
    _prod_brief,
)
from tests.tasks.test_kri551_cleanup_review_note import _n3_variant, _review

_REPO_ROOT = Path(__file__).resolve().parents[5]
_SWIFT_OFFER = _REPO_ROOT / "src/apps/ios/Kria/Features/SpeechCleanupOffer.swift"

# English "stays in" / Turkish "stays in the video": what the creator must still be told.
_STAYS = {"en": "stays in your video", "tr": "videoda kalıyor"}
_PHRASES = {"r3": _SLIP, "r4": _HALF_QUESTION}


def _no_editor_advice(text: str) -> None:
    lowered = text.lower()
    assert "editor" not in lowered, text
    assert "editör" not in lowered, text


def _lead(name: str, language: str) -> str:
    english = getattr(brief_checks, name)
    return english if language == "en" else brief_checks._REASON_TR[english]


# state -> (facts for the draft, the cleanup lead the reason must start with)
_DRAFT_STATES = {
    "on": ({"speech_cleanup_enabled": True, "speech_cleanup_offered": True}, "_CLEANUP_PLANNED"),
    "offered": ({"speech_cleanup_offered": True}, "_CLEANUP_AT_APPROVAL"),
    "unavailable": ({"speech_cleanup_offered": False}, "_CLEANUP_UNAVAILABLE"),
    "unknown": ({}, "_CLEANUP_IF_OFFERED"),
}
# outcome -> the rendered cleanup lead ("not_run": a device render that recorded no outcome)
_RENDERED_STATES = {
    "applied": "_CLEANUP_DONE",
    "no_change": "_CLEANUP_NOTHING_TO_CUT",
    "not_run": "_CLEANUP_NOT_RUN",
}


def _rendered_variant(outcome: str) -> dict:
    variant = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_cues": [{"text": "Hi, I'm Karolina", "start_s": 0.0, "end_s": 1.4}],
        "voiceover_caption_style": "sentence",
    }
    if outcome != "not_run":
        variant |= {
            "silence_cut_outcome": outcome,
            "silence_cut": {
                "removed": [{"start_s": 3.0, "end_s": 4.1, "reason": "silence"}],
                "time_saved_s": 1.1,
            },
        }
    return variant


def _assert_stays_in(receipt, lead: str, language: str) -> None:
    reason = receipt.reason or ""
    _no_editor_advice(reason)
    assert _STAYS[language] in reason, reason
    assert receipt.status == "partial", receipt  # never "met": KRI-792's false Done
    assert reason.startswith(lead), reason  # the cleanup lead stays first
    assert is_format_limit(reason)  # so the honest Partly never asks "simpler version?"
    assert not needs_creator_choice(receipt)


# --- a + b: the draft receipt and the approval card -------------------------------------------


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("state", list(_DRAFT_STATES))
@pytest.mark.parametrize("requirement_id", ["r3", "r4"])
def test_the_draft_receipt_says_the_line_stays_in_not_to_use_the_editor(
    requirement_id, state, language
):
    cleanup, lead = _DRAFT_STATES[state]
    with reply_language_for(language):
        receipt = _by_id(_prod_brief(), _draft_facts(**cleanup))[requirement_id]
        _assert_stays_in(receipt, _lead(lead, language), language)


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("state", list(_DRAFT_STATES))
def test_the_approval_card_text_says_it_for_both_phrase_cuts(state, language):
    """`approval_summary = reply_text`: the card repeats the draft reply (both phrase cuts)."""
    cleanup, _lead_name = _DRAFT_STATES[state]
    brief = _prod_brief()
    with reply_language_for(language):
        receipts = build_receipts(brief.live(), _draft_facts(**cleanup), include_unchecked=True)
        card = reply_from_receipts(brief, receipts)
    _no_editor_advice(card)
    assert card.count(_STAYS[language]) == 2, card  # the slip and the half question
    for phrase in _PHRASES.values():
        assert phrase in card


@pytest.mark.parametrize("edit_format", sorted(brief_checks._SPEECH_FORMATS))
def test_every_speech_format_gets_the_same_plain_line(edit_format):
    """1 + 2: Talking, cloud Talking and every Narrated shape say the same true thing."""
    req = BriefRequirement(
        id="r3",
        kind="audio",
        scope="global",
        description=_SLIP,
        facts={"speech_cleanup": True},
    )
    facts = PlanFacts(edit_format=edit_format, caption_style="karaoke", speech_cleanup_enabled=True)
    receipt = check_requirement(req, facts)
    _assert_stays_in(receipt, brief_checks._CLEANUP_PLANNED, "en")


# The kit's own wording, double quotes and all (the brief rows above hold the model's
# 'single-quoted' rewrite of it).
_KIT_ASKS = (
    'Cut her slip "with bread, no," so it says "waffle cookie with blackberry jam".',
    'Also cut my half question "how are you gonna".',
)


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("description", _KIT_ASKS)
def test_the_creators_own_double_quoted_wording_gets_the_same_line(description, language):
    req = BriefRequirement(
        id="r3",
        kind="audio",
        scope="global",
        description=description,
        facts={"speech_cleanup": True},
    )
    facts = _draft_facts(speech_cleanup_enabled=True, speech_cleanup_offered=True)
    with reply_language_for(language):
        receipt = check_requirement(req, facts)
        _assert_stays_in(receipt, _lead("_CLEANUP_PLANNED", language), language)


# --- c: the finished-video receipt -------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("outcome", list(_RENDERED_STATES))
@pytest.mark.parametrize("requirement_id", ["r3", "r4"])
def test_the_finished_receipt_says_the_line_stays_in_not_to_use_the_editor(
    requirement_id, outcome, language
):
    lead = _lead(_RENDERED_STATES[outcome], language)
    with reply_language_for(language):
        facts = plan_facts_from_phone_variant(_rendered_variant(outcome))
        receipt = _by_id(_prod_brief(), facts)[requirement_id]
        _assert_stays_in(receipt, lead, language)


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_finished_video_reply_has_no_editor_advice_on_the_phrase_lines(language):
    """The whole finished reply: both receipts, then the cleanup note, with the brief bound."""
    variant = _n3_variant(resolved_archetype="subtitled")
    text = _review(
        variant,
        brief=_prod_brief(),
        plan={"speech_cleanup_contract": "required_v1"},
        language=language,
    )
    label = "Partly" if language == "en" else "Kısmen"
    lines = [
        line
        for line in text.splitlines()
        if line.startswith((f"- {label}: Cut her slip", f"- {label}: Cut the half question"))
    ]
    assert len(lines) == 2, text
    for line in lines:
        _no_editor_advice(line)
        assert _STAYS[language] in line
    # The receipts already state the limit, so the cleanup note does not repeat it.
    assert text.count(_STAYS[language]) == 2, text
    _no_editor_advice(text.split("\n\n")[-1])


# --- d: the cleanup review note ---------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("outcome", ["applied", "no_change"])
def test_the_review_note_footer_says_a_restarted_sentence_stays_in(outcome, language):
    """4: with no brief (or no named cut) the footer is the only limit statement, and it is
    appended to every cleaned Talking or Narrated render."""
    variant = _n3_variant(silence_cut_outcome=outcome)
    with reply_language_for(language):
        note = speech_cleanup_review_note(variant) or ""
    _no_editor_advice(note)
    restart = {
        "en": "a sentence you restarted stays in",
        "tr": "baştan başladığın bir cümle videoda kalır",
    }
    assert restart[language] in note, note


@pytest.mark.parametrize("language", ["en", "tr"])
def test_a_reply_that_already_states_the_limit_is_not_given_the_footer(language):
    """5: the dedupe compares the receipt line with the constant, so they must stay one string."""
    brief = _prod_brief()
    with reply_language_for(language):
        facts = plan_facts_from_phone_variant(_rendered_variant("applied"))
        reply = reply_from_receipts(brief, list(_by_id(brief, facts).values()))
        note = speech_cleanup_review_note(_n3_variant(), text=reply) or ""
    assert _STAYS[language] in reply
    assert "sentence you restarted" not in note
    assert "baştan başladığın" not in note
    assert note  # the counts are new information, so they stay


# --- 10 + 9: the copy is tied to the real refusal ---------------------------------------------


def _talking_job_and_rows():
    from types import SimpleNamespace

    from app.routes import generative_jobs as gj
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    proxy = "users/u/analysis-proxy-video.mp4"
    variant = {
        "variant_id": "original_text",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_generation_id": "g1",
        "silence_cut": {"removed": [{"start_s": 2, "end_s": 3}, {"start_s": 7, "end_s": 8}]},
    }
    binding = {
        "media_id": "speaker",
        "proxy_path": proxy,
        "generation": "1",
        "original": {
            "sha256": "a" * 64,
            "byte_count": 1234,
            "duration_s": 10,
            "width": 1080,
            "height": 1920,
            "orientation_degrees": 0,
            "has_audio": True,
        },
    }
    job = SimpleNamespace(
        id="job",
        all_candidates={"clip_paths": [proxy]},
        assembly_plan={PHONE_SOURCES_FIELD: [binding], "variants": [variant]},
    )
    return job, gj.editor_deletion_timeline(job, variant)


def _save(job, rows, change):
    """A Save that posts every kept speech segment, the middle one changed by ``change``."""
    from app.routes import generative_jobs as gj
    from app.services.authored_editor import prepare_authored_editor_commit

    def slot(row, **changes):
        return gj.TimelineSlotEdit(
            slot_id=row["slot_id"],
            clip_index=row["clip_index"],
            in_s=row["in_s"],
            duration_s=row["duration_s"],
            **changes,
        )

    slots = [slot(rows[0]), slot(rows[1]), slot(rows[2])]
    slots[1] = slots[1].model_copy(update=change)
    request = gj.EditorCommitRequest(
        base_generation="g1", editor_state_version=1, timeline_slots=slots
    )
    return prepare_authored_editor_commit(job, "original_text", request, validation_arguments={})


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"duration_s": 3.5}, id="trim-the-end"),
        pytest.param({"in_s": 3.5, "duration_s": 3.5}, id="trim-the-start"),
        pytest.param({"in_s": 3.0, "duration_s": 4.5}, id="grow"),
        pytest.param({"clip_index": 1}, id="other-clip"),
    ],
)
def test_the_server_still_pins_every_talking_source_window(change):
    """WHY the copy says "can't": a trimmed talking-source segment is refused (409).

    When talking-source windows become editable (KRI-724 phrase cuts) this fails ON PURPOSE:
    reconsider `_NAMED_CUTS_STAY`, the review-note footer and `SpeechCleanupOffer.swift`
    before relaxing it. The baseline is the kept speech between the cleanup cuts.
    """
    job, rows = _talking_job_and_rows()
    assert [row["slot_id"] for row in rows] == [
        "native-phone-talking-source",
        "native-phone-talking-source-1",
        "native-phone-talking-source-2",
    ]
    with pytest.raises(HTTPException) as caught:
        _save(job, rows, change)
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "TIMELINE_STALE"}


def test_an_unchanged_talking_source_window_passes_the_pin():
    """The control: the exact baseline is not what the pin refuses."""
    job, rows = _talking_job_and_rows()
    try:
        _save(job, rows, {})
    except HTTPException as error:
        assert error.detail != {"code": "TIMELINE_STALE"}, error.detail


def test_the_iphone_approval_card_text_does_not_point_at_the_editor():
    """9: the card's English is hard-coded in the app, so a server fix never reaches it."""
    source = _SWIFT_OFFER.read_text(encoding="utf-8")
    offer = next(line for line in source.splitlines() if "Choose whether to remove" in line)
    _no_editor_advice(offer)
