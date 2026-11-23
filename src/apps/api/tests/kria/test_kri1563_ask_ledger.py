"""KRI-1563: every part of a chat-edit ask ends done, declined or waiting on a choice.

Real compiler, real diff, real checkers, real ledger and reply: only the model's outputs (the
requirements and the copilot's ops) are given, from the production record where there is one
(job fa10c4a4 / thread 1b8974a0). The rows live in
``tests/fixtures/kria_replay/kri1563_ask_ledger/fixture.json`` and also run as
``make kria-replay FIXTURE=kri1563_ask_ledger``.

Failure modes, written before the code:

1. A requirement leaves the turn with no state, or "declined"/"waiting" with no reason.
2. A change the turn made appears in no reply line: cut by a phrase limit ("first two"), on a
   lane the diff cannot read (the pictures behind a Talking speaker, the corner she sits in,
   a Talking song), or claimed by an ask whose line never names it.
3. A change in one field proves an ask about another (a font change proves "in the middle").
4. A wrong-content change is reported as done (captions moved DOWN when the ask says middle).
5. A title Kria wrote is reported as "not added" because the checker counts only the
   creator's own words; a "no title" ask is held as unmet after the title was removed
   (KRI-1355).
6. "Only my voice, no music" split in two leaves "only my voice" unmet although the music
   was removed (KRI-1472).
7. A turn with no ops answers the whole message with one sentence: its parts keep no state.
8. One part the copilot could not do throws away the ops for the others (failed-render
   correction: ``test_failed_draft_correction_postgres``).
9. The switch is off and the turn still changes (``KRIA_ASK_LEDGER_ENABLED=false`` must give
   the previous replies).
10. The Turkish reply drifts from the English structure.
11. A mixed edit (one part an op can do, one no op family can) loses the supported op, or
    the reply refuses the whole message instead of naming only the part it could not do
    (rows ``mixed-one-unsupported-*`` and their red controls; Tadic's hold on #2485).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.kria import planner
from app.kria.ask_ledger import AskLedger, AskRow, ChangeRow, no_op_ledger, no_op_reply
from app.kria.brief import BriefRequirement
from app.kria.reply_language import reply_language_for
from app.routes._copilot import CopilotTurnResponse
from tests.kria.kri1563_ledger_cases import check_case, load_cases, run_case


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
def test_each_recorded_turn_accounts_for_every_part_and_every_change(case, monkeypatch) -> None:
    result = run_case(case, monkeypatch)
    assert check_case(case, result) == [], result.reply


def _alanur(case_id: str, monkeypatch):
    case = next(c for c in load_cases() if c["id"] == case_id)
    return case, run_case(case, monkeypatch)


def test_turn_2_names_the_title_it_added_and_the_corner_it_moved_her_into(monkeypatch) -> None:
    _case, result = _alanur("alanur-t2-tr", monkeypatch)
    # "görseller koy" names no field the brief types, so its line lists what changed for it
    # ("have a look") instead of a "Done" nothing checked.
    assert result.reply == (
        "- Yapıldı: 'Renk Kavgası' eklendi\n"
        "- Bir göz at: görseller koy (Değiştirdiklerim: Arkana 2 görsel kondu; "
        "Sağ alt köşede küçük gösteriliyorsun; Altyazılar artık pop ile giriyor)"
    )


def test_turn_3_does_not_call_captions_moved_down_done_and_says_she_is_full_screen(
    monkeypatch,
) -> None:
    _case, result = _alanur("alanur-t3-en", monkeypatch)
    captions, corner = result.reply.splitlines()
    assert captions.startswith("- Partly: captions in the middle and in a nicer style")
    assert "Moved the captions to the bottom, but that isn't what you asked for" in captions
    # The font and colour changes are named on the caption line, never sent to "Also changed".
    assert "Changed the caption font to Montserrat Bold" in captions
    assert "Put you back full screen" in corner


def test_production_replies_fail_the_ledger_check(monkeypatch) -> None:
    """Negative control: what Kria actually said on 2026-11-19 left changes out."""
    for case_id, missing in (
        ("alanur-t2-tr", {"speaker_background.windows", "speaker_background.speaker_frame"}),
        ("alanur-t3-tr", {"speaker_background.speaker_frame", "caption_meta.y_frac"}),
    ):
        case, result = _alanur(case_id, monkeypatch)
        ids = [row["id"] for row in case["requirements"]]
        found = set(result.ledger.gaps(ids, case["prod_reply"]))
        assert {f"unnamed_change:{key}" for key in missing} <= found
        assert result.ledger.gaps(ids, result.reply) == []


_WHOLE_REFUSAL = {
    "en": ("I can't change how your voice sounds, so I left the video as it is.", "Couldn't"),
    "tr": ("Sesinin tonunu değiştiremiyorum, videoyu olduğu gibi bıraktım.", "Yapamadım"),
}


@pytest.mark.parametrize("language", ["en", "tr"])
def test_a_mixed_edit_red_controls_fail_the_row(monkeypatch, language) -> None:
    """Red controls for the mixed rows: the check fails when the supported part is lost.

    The row passes only when the caption op applies and only the voice part is declined. The
    same row must fail when (a) the copilot drops the caption op and declines the voice part,
    (b) it refuses the whole message as one, and (c) the ops apply but the reply still refuses
    the whole ask.
    """
    case, result = _alanur(f"mixed-one-unsupported-{language}", monkeypatch)
    assert check_case(case, result) == []
    refusal, couldnt = _WHOLE_REFUSAL[language]
    whole = " ".join(req["description"] for req in case["requirements"])

    dropped = {
        **case,
        "ops": [],
        "copilot": {
            "outcome": "unsupported",
            "reply": refusal,
            "unmet_requests": case["unmet_requests"],
        },
    }
    declined_whole = {
        **dropped,
        "copilot": {
            "outcome": "unsupported",
            "reply": refusal,
            "unmet_requests": [{"request": whole, "reason": refusal}],
        },
    }
    for control in (dropped, declined_whole):
        problems = check_case(control, run_case(control, monkeypatch))
        assert any(p.startswith("r1: state 'declined', expected 'done'") for p in problems), (
            problems
        )
        assert any(p.startswith("change caption_meta.size_px") for p in problems), problems

    whole_reply = f"{couldnt}: {whole} ({refusal})"
    problems = check_case(case, replace(result, reply=whole_reply))
    assert any(p.startswith("r1 line lacks") for p in problems), problems


@pytest.mark.asyncio
async def test_the_planner_keeps_a_mixed_turns_ops_and_names_only_the_unmet_part(
    monkeypatch,
) -> None:
    """The planner passes the copilot's ops on with only the part it could not do as unmet."""
    unmet = [
        {"request": "make my voice sound deeper", "reason": "I can't change how your voice sounds"}
    ]
    response = CopilotTurnResponse(
        intent="edit",
        ops=[{"op": "set_caption_meta", "patch": {"size_px": 96}}],
        confidence=0.9,
        reply="Made the captions bigger.",
        outcome="applied",
        unmet_requests=unmet,
    )
    reqs = [
        _req("r1", "Make the captions bigger"),
        _req("r2", "Make my voice sound deeper", kind="audio"),
    ]
    plan = await _revision(monkeypatch, response, requirements=reqs)
    assert plan.mode == "act"
    (intent,) = plan.intents
    arguments = (
        intent.arguments if isinstance(intent.arguments, dict) else intent.arguments.model_dump()
    )
    assert arguments["operations"] == response.ops
    assert [dict(item) for item in arguments["unmet_requests"]] == unmet
    assert plan.requirement_receipts is None


def test_a_requirement_with_no_row_or_no_reason_is_a_gap() -> None:
    ledger = AskLedger(
        asks=(
            AskRow("r1", "done", (), ""),
            AskRow("r2", "declined", (), "  "),
            AskRow("r3", "unknown", (), "x"),
        ),
        changes=(ChangeRow("text.added", ("Added 'x'",), ("r1",)),),
        receipts=(),
    )
    assert ledger.gaps(["r1", "r2", "r3", "r4"], reply="Done.") == [
        "no_proof:r1",
        "no_reason:r2",
        "no_state:r3",
        "no_state:r4",
        "unnamed_change:text.added",
    ]


def test_a_change_named_on_its_line_is_no_gap() -> None:
    ledger = AskLedger(
        asks=(AskRow("r1", "done", ("Added 'x'",), "Added 'x'"),),
        changes=(ChangeRow("text.added", ("Added 'x'",), ("r1",)),),
        receipts=(),
    )
    assert ledger.gaps(["r1"], reply="Done: Added 'x'.") == []


def _req(rid: str, description: str, kind: str = "style") -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope="global", description=description)


def test_a_no_op_turn_gives_each_part_a_state_in_both_languages() -> None:
    reqs = [_req("r1", "relevant pictures"), _req("r2", "no constant background")]
    unmet = [{"request": "relevant pictures", "reason": "your pool has no other pictures"}]
    for language, couldnt in (("en", "Couldn't"), ("tr", "Yapamadım")):
        with reply_language_for(language):
            ledger = no_op_ledger(
                reqs, reply="That isn't possible here.", asks_question=False, unmet=unmet
            )
            reply = no_op_reply(reqs, ledger, question="That isn't possible here.")
        assert ledger.gaps(["r1", "r2"], reply) == []
        assert [row.state for row in ledger.asks] == ["declined", "declined"]
        first, second, last = reply.splitlines()
        assert first == f"- {couldnt}: relevant pictures (your pool has no other pictures)"
        assert second == f"- {couldnt}: no constant background"
        assert last == "That isn't possible here."


def _copilot(outcome: str, reply: str, unmet=()) -> CopilotTurnResponse:
    return CopilotTurnResponse(
        intent="reject" if outcome == "unsupported" else "clarify",
        ops=[],
        confidence=0.5,
        reply=reply,
        outcome=outcome,
        needs_clarification=outcome == "clarification",
        unmet_requests=list(unmet),
    )


async def _revision(monkeypatch, response: CopilotTurnResponse, **kw):
    monkeypatch.setattr(
        planner,
        "_load_editor_target",
        AsyncMock(
            return_value=planner._EditorTarget(job_id=uuid.uuid4(), snapshot={}, conversation=[])
        ),
    )
    monkeypatch.setattr(planner, "run_copilot_turn", AsyncMock(return_value=response))
    return await planner._plan_editor_revision(
        SimpleNamespace(rollback=AsyncMock()),
        thread_id=uuid.uuid4(),
        item=SimpleNamespace(id=uuid.uuid4()),
        user_message="x",
        **kw,
    )


@pytest.mark.asyncio
async def test_the_planner_sends_a_no_op_turn_through_the_ledger(monkeypatch) -> None:
    reqs = [_req("r1", "relevant pictures"), _req("r2", "no constant background")]
    response = _copilot(
        "unsupported",
        "That kind of edit isn't available for this draft yet.",
        [{"request": "relevant pictures", "reason": "your pool has no other pictures"}],
    )
    plan = await _revision(monkeypatch, response, requirements=reqs)
    assert plan.mode == "respond"
    assert plan.response.startswith(
        "- Couldn't: relevant pictures (your pool has no other pictures)\n"
        "- Couldn't: no constant background\n"
        "That kind of edit isn't available for this draft yet."
    )
    assert plan.response.endswith(planner._REDO_OFFER)
    assert [(r["requirement_id"], r["status"]) for r in plan.requirement_receipts] == [
        ("r1", "not_possible"),
        ("r2", "not_possible"),
    ]


@pytest.mark.asyncio
async def test_a_one_part_question_keeps_its_own_words_and_still_records_the_state(
    monkeypatch,
) -> None:
    plan = await _revision(
        monkeypatch,
        _copilot("clarification", "How much shorter?"),
        requirements=[_req("r1", "shorter", kind="timing")],
    )
    assert plan.response == "How much shorter?"
    (receipt,) = plan.requirement_receipts
    assert receipt["status"] == "partial" and receipt["verification"] == "unchecked"


@pytest.mark.asyncio
async def test_a_long_no_op_reply_stays_under_the_plan_bound(monkeypatch) -> None:
    reqs = [
        _req(f"r{i}", "make the pictures behind me relevant to the words " * 3) for i in range(9)
    ]
    plan = await _revision(monkeypatch, _copilot("unsupported", "No can do."), requirements=reqs)
    assert len(plan.response) <= 1200


@pytest.mark.asyncio
async def test_with_the_switch_off_a_no_op_turn_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_ask_ledger_enabled", False)
    reqs = [_req("r1", "relevant pictures"), _req("r2", "no constant background")]
    plan = await _revision(monkeypatch, _copilot("unsupported", "No can do."), requirements=reqs)
    assert plan.response == "No can do." + planner._REDO_OFFER
    assert plan.requirement_receipts is None
    assert "requirement_receipts" not in plan.model_dump(mode="json")
