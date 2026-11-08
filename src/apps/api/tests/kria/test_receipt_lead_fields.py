"""KRI-739: the lead line rides the chat event only when an editor turn bound receipts.

KRI-1262: a draft or render reply that listed receipts instead of the model's summary has a
lead too, so the chat shows one failure line over the receipt rows instead of the same
failure twice.

Failure modes for the reply lead:
- every ask met: the model's own summary is the reply, so a lead ("Done.") would hide it;
- a lead that disagrees with the rows (counts, partial vs failed);
- the lead running over one line, or losing the server's notices the prose carries;
- a Turkish chat getting an English lead;
- asks nothing could judge yet (listed under "have a look") getting "Done.";
- a lead sent with no receipts under it, or after the reply was replaced by other words;
- the reply text itself changing: older clients and the web still read it.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    reply_and_lead_from_receipts,
    reply_from_receipts,
)
from app.kria.editor_receipts import receipt_lead
from app.kria.reply_language import reply_language_for
from app.tasks.kria_runtime import _receipt_lead_fields

RECEIPTS = [{"requirement_id": "r1", "status": "met", "verification": "checked"}]
CLIPS = ("c1", "c2", "c3")


def test_a_bound_editor_turn_carries_the_lead_and_what_else_changed() -> None:
    assert _receipt_lead_fields("Done.", ["Retimed the title (0–4.3s → 3–7.3s)"], RECEIPTS) == {
        "reply_lead": "Done.",
        "also_changed": ["Retimed the title (0–4.3s → 3–7.3s)"],
    }


def test_nothing_else_changed_omits_the_list() -> None:
    assert _receipt_lead_fields("Done.", [], RECEIPTS) == {"reply_lead": "Done."}


def test_every_other_event_stays_byte_identical() -> None:
    assert _receipt_lead_fields(None, [], RECEIPTS) == {}
    assert _receipt_lead_fields("", ["x"], RECEIPTS) == {}
    # A lead with no receipts under it would show a line about rows that are not there.
    assert _receipt_lead_fields("Done.", ["x"], []) == {}


def _req(rid: str, kind: str, description: str, scope: str = "per_clip") -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope=scope, description=description)


LABELS = _req("r1", "text", "label every clip")
WARMER = _req("r2", "style", "make it warmer", "global")
TEXT_ALL = PlanFacts(clip_ids=CLIPS, per_clip_text={"c1": "A", "c2": "B", "c3": "C"})
TEXT_ONE = PlanFacts(clip_ids=CLIPS, per_clip_text={"c1": "A"})
TEXT_NONE = PlanFacts(clip_ids=CLIPS, per_clip_text={})


def _reply(reqs, facts, **kwargs) -> tuple[str, str]:
    receipts = build_receipts(reqs, facts, include_unchecked=True)
    brief = CreativeBrief(version=1, requirements=list(reqs))
    return reply_and_lead_from_receipts(brief, receipts, summary="Nice cut.", **kwargs)


def test_every_ask_met_keeps_the_models_summary_and_sends_no_lead() -> None:
    text, lead = _reply([LABELS], TEXT_ALL)

    assert lead == ""
    assert text == "Nice cut.\n- Done: label every clip"


def test_a_failed_ask_is_one_line_over_the_rows() -> None:
    text, lead = _reply([LABELS], TEXT_NONE)

    assert lead == "I couldn't do that."
    assert "\n" not in lead
    assert text.startswith("Not everything you asked for made it in:\n- Couldn't: label every clip")


def test_a_partly_done_ask_says_partly_done() -> None:
    assert _reply([LABELS], TEXT_ONE)[1] == "Partly done."


def test_some_asks_done_and_some_not_counts_them() -> None:
    reqs = [LABELS, _req("r3", "text", "title says X", "title")]
    receipts = build_receipts(reqs, TEXT_ALL, include_unchecked=True)
    failed = receipts[1].model_copy(update={"status": "not_possible"})
    brief = CreativeBrief(version=1, requirements=reqs)

    _, lead = reply_and_lead_from_receipts(brief, [receipts[0], failed], summary="Nice cut.")

    assert lead == "I did 1 of your 2 asks."


def test_asks_only_the_video_can_show_say_so_not_done() -> None:
    text, lead = _reply([WARMER], PlanFacts(clip_ids=CLIPS))

    assert lead == "Have a look at these in the video."
    assert text.startswith("Have a look at these in the video:")


def test_the_servers_notices_follow_the_lead_like_the_prose() -> None:
    text, lead = _reply([LABELS], TEXT_NONE, notices=["I used your time zone."])

    assert lead == "I couldn't do that. I used your time zone."
    assert text.endswith("I used your time zone.")


def test_a_turkish_chat_gets_a_turkish_lead() -> None:
    with reply_language_for("tr"):
        _, lead = _reply([LABELS], TEXT_NONE)

    assert lead == "Bunu yapamadım."


def test_the_reply_text_is_unchanged_for_older_clients_and_the_web() -> None:
    for facts in (TEXT_ALL, TEXT_ONE, TEXT_NONE):
        receipts = build_receipts([LABELS], facts, include_unchecked=True)
        brief = CreativeBrief(version=1, requirements=[LABELS])
        assert reply_and_lead_from_receipts(brief, receipts, summary="Nice cut.")[
            0
        ] == reply_from_receipts(brief, receipts, summary="Nice cut.")


def test_an_outcome_row_that_failed_counts_even_with_no_ledger_receipt() -> None:
    brief = CreativeBrief(version=1, requirements=[])
    outcomes = [{"status": "not_possible", "name": "Sunset group", "reason": "no clip got one"}]

    text, lead = reply_and_lead_from_receipts(brief, [], summary="Nice cut.", outcomes=outcomes)

    assert lead == "I couldn't do that."
    assert text.startswith("Not everything you asked for made it in:")


def test_the_draft_lead_reads_like_the_editor_turn_lead() -> None:
    receipts = build_receipts([LABELS], TEXT_NONE, include_unchecked=True)

    assert _reply([LABELS], TEXT_NONE)[1] == receipt_lead(receipts)
