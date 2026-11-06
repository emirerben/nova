"""KRI-1236: "Make it without these" leaves out only what Kria can't do, and the review says so.

Qendresa Q1 (prod thread b931bc4e, 2026-11-06 20:48-20:54 UTC, Fly main 11310a4ea, TestFlight
1910, protocol 42; parent KRI-1234). The prompt held eleven asks. Approval readiness held the
first draft on eight of them: r4 (screenshots / recordings in the top or bottom part) was the
only ``unsupported`` one; r1 (tight pauses), r5-r9 (the five "1. Spec-kit" labels) and r10 (the
"comment REPO" ending with an arrow) were ``needs_choice`` promises ("I'll show it the moment you
say that word", "I'll draw a curly arrow"). The reply was cut at 1200 characters after r8 and the
card named nothing (the 8-ask "leaves out" sentence was over 600 characters). The creator tapped
"Make it without these"; the Main Creator removed r1 and r4-r9 itself, a ``remove`` update cancels
the server's consent (KRI-1113 only waives a ``change``), and the removals were persisted as the
creator's own (``superseded_by: creator``). Brief v2 still had r10 (never shown in the cut list),
readiness held it, a second card named it, the second tap had the model remove r10 too (its
rationale: "I've removed the closing arrow request"), and the ending text went with the arrow.
The draft and the after-render review listed r2, r3 and r11 as done and never said that the
pauses, labels, ending and cards were dropped.

Ways this can fail, written before any fix:

 F1 the incident: the tap removes asks the card never named (the promises: pauses, labels, ending).
 F2 the incident's mechanism: the model's own ``remove`` updates on the tap turn are persisted as
    the creator's removals, so the server's named consent never runs (no "Left out as agreed").
 F3 a removed ask is missing from the draft reply or from the after-render review.
 F4 the offer and the tap disagree: the card names a different or a truncated list (the 600-char
    cap returned a card that named nothing), or names asks the tap would not remove.
 F5 the ending's words go with its arrow: r10 is one ask holding both.
 F6 a loop: the same card comes back after the tap, or a second card follows the first.
 F7 over-reach: a typed instruction that really removes asks ("drop the labels") is ignored
    because it is mistaken for the tap.
 F8 over-reach: a legacy card that named nothing (prod's own) consents to a set it never showed.
 F9 over-reach: an ask Kria really cannot do but readiness calls needs_choice / evidence_missing
    (a named line that may stay, wind noise) can no longer be left out, so the creator is stuck.
    Pinned by the KRI-901 and approval-readiness suites; KRI-1235 makes the promises approvable,
    which is what keeps them off the card (written after a first, stricter fix broke them).
 F10 the post-consent hold reads "Here's the simpler version ... Should I make it without these?".

Runs the real runtime over scratch Postgres (``submit_turn``, ``run_kria_turn``, brief
persistence, receipts, the hold, the offer and consent, the approval card, the dispatch claim and
the after-render review). Two things are replayed rather than recomputed: the planner's output
(prod's approved strategy, and the model's brief updates as prod persisted them), and approval
readiness's verdict rows, which are prod's recorded rows from event 7 laid over the real report
(digests stay real). ``promises_approvable`` lays the same rows over with the needs_choice
promises approvable, which is what KRI-1235's readiness fix is for; that variant is the ticket's
done-when ("a replay of this thread with the readiness fix").
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import app.tasks.kria_runtime as kria_runtime
from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import approval_readiness, brief_checks
from app.kria.api_schemas import ApprovalDecisionBody, ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import BriefUpdate, CreativeBrief, load_latest_brief_sync
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    PlanItem,
)
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

FIXTURE: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri1236_qendresa_q1_simpler.json"
    ).read_text(encoding="utf-8")
)
EVENTS = {event["sequence"]: event for event in FIXTURE["events"]}
PROMPT = EVENTS[4]["content"]
TAP = "Make it without these"
PROD_ROWS = {
    row["requirement_id"]: row
    for row in EVENTS[7]["payload"]["brief_coverage"]["approval_readiness"]["requirements"]
}
PROD_POLICY_PROBLEM = EVENTS[7]["payload"]["brief_coverage"]["approval_readiness"]["policy_problem"]
BRIEF_V1 = CreativeBrief.model_validate(copy.deepcopy(FIXTURE["brief_v1"]))
TEXTS = {req.id: req.text() for req in BRIEF_V1.requirements}
CARDS = TEXTS["r4"]
ENDING = TEXTS["r10"]
HELD_FOR_CHOICE = ["r1", "r5", "r6", "r7", "r8", "r9", "r10"]
# The facts `_closing_arrow_reason` reads, for a draft on a route that cannot draw the arrow.
_NO_ARROW_ROUTE = SimpleNamespace(closing_arrow=False, rendered_variant=False, phone_talking=False)
ALL_IDS = [req.id for req in BRIEF_V1.requirements]
NOT_READY = "approval_requirements_not_ready"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod switches this chat ran under that the runtime reads here."""
    for name, value in {
        "kria_runtime_v2_enabled": True,
        "main_creator_agent_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_approval_readiness_enabled": True,
        "kria_choice_questions_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


# -- replayed inputs ---------------------------------------------------------------------------


def _replay_readiness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    promises_approvable: bool,
    after_removal: dict[str, str] | None = None,
    arrow_missing: bool = False,
) -> None:
    """Approval readiness with prod's event-7 verdict for every live Q1 ask.

    ``promises_approvable``: the needs_choice promises are approvable (``unverified``, named
    after render), as KRI-1235 makes them. ``after_removal``: dispositions to use once r4 is no
    longer live (a blocker the omission itself surfaces, F10). ``arrow_missing``: the route
    cannot draw the ending's arrow (the arrow switch off, not the phone Talking writer): r10's
    words are found and the real arrow check decides, "The arrow isn't in the video yet" while
    the ask still wants one.
    """
    real = approval_readiness.assess_readiness

    def assess(brief, strategy, **kwargs):  # noqa: ANN001, ANN202
        report = real(brief, strategy, **kwargs)
        live = {req.id: req for req in brief.live()} if brief is not None else {}
        rows = []
        for row in report["requirements"]:
            req_id = row["requirement_id"]
            prod = PROD_ROWS.get(req_id)
            if prod is not None:
                disposition, reason = prod["disposition"], prod["reason"]
                if promises_approvable and disposition == "needs_choice":
                    disposition = "unverified"
                if after_removal and "r4" not in live:
                    disposition = after_removal.get(req_id, disposition)
                if arrow_missing and req_id == "r10":
                    reason = brief_checks._closing_arrow_reason(live[req_id], _NO_ARROW_ROUTE)
                    disposition = "executable" if reason is None else "needs_choice"
                row = {
                    **row,
                    "disposition": disposition,
                    "reason": reason,
                    "evidence_owner": prod["evidence_owner"],
                }
            rows.append(row)
        # Prod recorded no policy problem; this scratch project has no phone route to name.
        replayed = {
            **report,
            "requirements": rows,
            "policy_problem": PROD_POLICY_PROBLEM,
            "ready": not PROD_POLICY_PROBLEM
            and all(r["disposition"] in approval_readiness.APPROVABLE for r in rows),
        }
        replayed["digest"] = approval_readiness.digest(approval_readiness._identity(replayed))
        return replayed

    monkeypatch.setattr(approval_readiness, "assess_readiness", assess)


def _strategy_plan():  # noqa: ANN202
    """Prod's approved strategy (turn 3), on this scratch project's media."""
    strategy = {**copy.deepcopy(FIXTURE["approved_strategy"]), "selected_media_ids": []}
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(strategy),
            summary="Your talk with the requested opening title, word-by-word captions, and "
            "original audio.",
        )
    )


def _planned(*updates: BriefUpdate) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=_strategy_plan(),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=tuple(updates),
        brief_route="replan",
        brief_clip_ids=("clip-1",),
    )


def _prompt_updates() -> tuple[BriefUpdate, ...]:
    """The eleven asks of the prompt, as brief v1 recorded them (r1-r11 in order)."""
    return tuple(
        BriefUpdate(
            kind=req.kind,
            scope=req.scope,
            literal=req.literal,
            description=req.description,
            facts=copy.deepcopy(req.facts),
        )
        for req in BRIEF_V1.requirements
    )


def _model_removals(turn: str, version: int) -> tuple[BriefUpdate, ...]:
    """What the Main Creator wrote on prod's tap turns (persisted as superseded_by=creator)."""
    return tuple(
        BriefUpdate(operation="remove", target_requirement_id=req_id, expected_version=version)
        for req_id in FIXTURE["model_removals"][turn]
    )


# -- the chat ----------------------------------------------------------------------------------


def _seed(*, through_prod_hold: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    """A subtitled scratch project; with ``through_prod_hold`` it holds prod's brief v1, the
    prompt and prod's own first hold (event 7, the card that named nothing)."""
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "subtitled"
        thread.state = {**(thread.state or {}), "edit_format": "subtitled", "reply_language": "en"}
        if through_prod_hold:
            db.add(
                CreativeBriefVersion(
                    thread_id=thread_id,
                    version=1,
                    requirements=copy.deepcopy(FIXTURE["brief_v1"]["requirements"]),
                )
            )
            sequence, revision = 2, int(thread.revision)
            for prod in (EVENTS[4], EVENTS[7]):
                revision += 1
                user = prod["actor"] == "user"
                db.add(
                    CreationThreadEvent(
                        thread_id=thread_id,
                        sequence=sequence,
                        revision=revision,
                        role="user" if user else "assistant",
                        event_type="user_message" if user else "assistant_response",
                        content=prod["content"],
                        payload=copy.deepcopy(prod["payload"]),
                    )
                )
                sequence += 1
            thread.revision = revision
        db.commit()
    return user_id, thread_id


def _revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _last_reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    message: str,
    planned: PlannedKriaTurn,
    *,
    tap_on: str | None = None,
) -> CreationThreadEvent:
    # The capability manifest the real planner resolves for the strategy it proposes, so the
    # approval re-check reads the same capabilities the draft turn did.
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        item = await db.get(PlanItem, thread.active_plan_item_id)
        manifest = await approval_readiness.resolve_readiness_manifest(
            db, item, planned.plan.intents[0].arguments["strategy"]
        )
    planned = dataclasses.replace(planned, readiness_manifest=manifest)

    async def _plan(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return planned

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _plan)
    selection = (
        {"choice_selection": ChoiceSelectionIn(question_id=tap_on, option_key="simpler_version")}
        if tap_on
        else {}
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri1236-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
                **selection,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    return _last_reply(thread_id)


def _brief(thread_id: uuid.UUID) -> CreativeBrief:
    with sync_session() as db:
        return load_latest_brief_sync(db, thread_id)


def _holds(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        payloads = db.execute(
            select(CreationThreadEvent.payload).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "assistant_response",
            )
        ).scalars()
        return sum(
            1
            for payload in payloads
            if ((payload or {}).get("brief_coverage") or {}).get("reason") == NOT_READY
        )


async def _approve_and_review(user_id: uuid.UUID, thread_id: uuid.UUID) -> str:
    """Approve the card, claim the dispatch, then build the after-render review."""
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()
        approval_id = approval.id
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="approve",
            body=body,
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
    assert claim is not None
    variant = {
        "variant_id": "subtitled",
        "status": "ready",
        "render_generation_id": str(uuid.uuid4()),
    }
    with sync_session() as db, ExitStack() as language:
        thread = db.get(CreationThread, thread_id)
        kria_runtime._bind_thread_language(language, thread)
        execution = db.get(CreatorAgentExecution, claim.execution_id)
        job = SimpleNamespace(
            id=uuid.uuid4(),
            assembly_plan={"variants": [variant], "creator_brief_binding": claim.brief_binding},
            all_candidates={},
            content_plan_item_id=thread.active_plan_item_id,
        )
        review, _receipts = kria_runtime._approved_generation_review(
            db,
            thread,
            job,
            variant,
            execution,
            kria_runtime._render_ready_review_copy(variant["variant_id"]),
        )
    return review


# == the incident as prod recorded it ==========================================================


def test_prod_record_two_holds_model_removals_and_a_silent_done_list() -> None:
    held, second = EVENTS[7], EVENTS[9]
    # The first card named nothing, and the cut reply never showed r9 or r10.
    assert "leaves out" not in held["content"]
    assert "omitted_requirement_ids" not in held["payload"]["choice_question"]
    assert "-…" in held["content"] and ENDING not in held["content"]
    assert [r for r, row in PROD_ROWS.items() if row["disposition"] == "unsupported"] == ["r4"]
    assert [r for r, row in PROD_ROWS.items() if row["disposition"] == "needs_choice"] == (
        HELD_FOR_CHOICE
    )
    # The second card named only the ending, at brief v2.
    assert second["payload"]["choice_question"]["omitted_requirement_ids"] == ["r10"]
    # Every drop was the model's, not the server's omission.
    removed = {
        req["id"]: req["facts"].get("superseded_by")
        for req in FIXTURE["brief_v3"]["requirements"]
        if req["status"] == "superseded"
    }
    assert removed == {req_id: "creator" for req_id in ["r1", "r4", *HELD_FOR_CHOICE[1:]]}
    # The done-list and the review: three asks, not a word about the eight dropped ones.
    for shown in (EVENTS[11]["content"], EVENTS[19]["content"]):
        assert "Left out" not in shown
        assert shown.count("- Done:") == 3


# == F1/F2/F3/F5/F6: the done-when, with KRI-1235's readiness ===================================


@pytest.mark.asyncio
async def test_one_hold_names_only_the_cards_and_the_tap_keeps_every_other_ask(monkeypatch) -> None:
    _replay_readiness(monkeypatch, promises_approvable=True)
    user_id, thread_id = _seed()

    held = await _turn(monkeypatch, user_id, thread_id, PROMPT, _planned(*_prompt_updates()))
    assert held.event_type == "assistant_response"
    question = held.payload["choice_question"]
    assert question["omitted_requirement_ids"] == ["r4"]
    assert f"The simpler version leaves out: {CARDS}." in held.content
    simpler = next(o for o in question["options"] if o["key"] == "simpler_version")
    assert simpler["description"] == f"Leaves out: {CARDS}"

    # The creator taps the card; the model removes r1 and r4-r9 on that turn, as in prod.
    draft = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        TAP,
        _planned(*_model_removals("turn_2", version=1)),
        tap_on=question["question_id"],
    )
    assert draft.event_type == "draft_applied", draft.content
    assert draft.content.startswith("Here's the simpler version.")
    assert f"- Left out as agreed: {CARDS}" in draft.content
    assert draft.content.count("Left out") == 1
    assert _holds(thread_id) == 1

    brief = _brief(thread_id)
    assert [req.id for req in brief.live()] == [r for r in ALL_IDS if r != "r4"]
    cards = next(req for req in brief.requirements if req.id == "r4")
    assert cards.facts.get("superseded_by") == "server"
    ending = next(req for req in brief.live() if req.id == "r10")
    assert ending.literal == "comment REPO"

    review = await _approve_and_review(user_id, thread_id)
    assert f"- Left out as agreed: {CARDS}" in review
    assert review.count("Left out") == 1


# == F8: prod's own first card (it named nothing), tapped after the fix ========================


@pytest.mark.asyncio
async def test_prods_legacy_card_drops_nothing_it_did_not_show(monkeypatch) -> None:
    """Prod's event-7 card, tapped with prod's model removals once the promises are approvable:
    the set it blocked is not the set held now, so nothing is dropped and the new card names
    only the cards ask. (On origin/main this reproduces prod's brief v2: r2, r3, r10, r11.)"""
    _replay_readiness(monkeypatch, promises_approvable=True)
    user_id, thread_id = _seed(through_prod_hold=True)
    tap = EVENTS[8]["payload"]["choice_selection"]

    reply = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        TAP,
        _planned(*_model_removals("turn_2", version=1)),
        tap_on=tap["question_id"],
    )
    assert reply.event_type == "assistant_response"
    assert [req.id for req in _brief(thread_id).live()] == ALL_IDS
    assert reply.payload["choice_question"]["omitted_requirement_ids"] == ["r4"]
    assert f"The simpler version leaves out: {CARDS}." in reply.content


# == F1/F2/F3/F4 without KRI-1235: every held ask is named, and the review says what went ======


@pytest.mark.asyncio
async def test_without_the_readiness_fix_the_card_names_all_eight_and_receipts_them(
    monkeypatch,
) -> None:
    """Prod's own verdicts (promises still held): the card names all eight asks, none cut off;
    the tap drops exactly those, by the server, and the draft says "Left out as agreed" for
    each. Keeping the promises off the card is KRI-1235's verdict, not this card's."""
    _replay_readiness(monkeypatch, promises_approvable=False)
    user_id, thread_id = _seed()
    held_ids = ["r1", "r4", *HELD_FOR_CHOICE[1:]]

    held = await _turn(monkeypatch, user_id, thread_id, PROMPT, _planned(*_prompt_updates()))
    question = held.payload["choice_question"]
    assert question["omitted_requirement_ids"] == held_ids
    leaves_out = held.content.split("The simpler version leaves out: ", 1)[1]
    for req_id in held_ids:
        assert TEXTS[req_id] in leaves_out

    draft = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        TAP,
        _planned(*_model_removals("turn_2", version=1)),
        tap_on=question["question_id"],
    )
    assert draft.event_type == "draft_applied", draft.content
    for req_id in held_ids:
        assert f"- Left out as agreed: {TEXTS[req_id]}" in draft.content
    brief = _brief(thread_id)
    assert [req.id for req in brief.live()] == ["r2", "r3", "r11"]
    assert {req.facts.get("superseded_by") for req in brief.requirements if not req.live} == {
        "server"
    }
    assert _holds(thread_id) == 1


# == F5: an ending held only for its arrow keeps its words =====================================


@pytest.mark.asyncio
async def test_an_ending_held_for_its_arrow_keeps_its_words(monkeypatch) -> None:
    _replay_readiness(monkeypatch, promises_approvable=True, arrow_missing=True)
    user_id, thread_id = _seed()

    held = await _turn(monkeypatch, user_id, thread_id, PROMPT, _planned(*_prompt_updates()))
    question = held.payload["choice_question"]
    assert question["omitted_requirement_ids"] == ["r4", "r10"]
    arrow = 'the arrow next to your ending ("comment REPO")'
    assert f"The simpler version leaves out: {CARDS}; {arrow}." in held.content

    draft = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        TAP,
        _planned(),
        tap_on=question["question_id"],
    )
    assert draft.event_type == "draft_applied", draft.content
    assert f"- Left out as agreed: {arrow}" in draft.content
    assert f"- Left out as agreed: {ENDING}" not in draft.content
    ending = next(req for req in _brief(thread_id).live() if req.id == "r10")
    assert ending.literal == "comment REPO"
    assert ending.description == TEXTS["r10"].split(" (")[0]
    # The real arrow check: the kept ending no longer asks for an arrow, so nothing holds it.
    assert brief_checks._closing_arrow_reason(ending, _NO_ARROW_ROUTE) is None

    review = await _approve_and_review(user_id, thread_id)
    assert f"- Left out as agreed: {arrow}" in review
    assert f"- Left out as agreed: {CARDS}" in review


# == F7: a typed instruction that removes asks is still the creator's ==========================


@pytest.mark.asyncio
async def test_a_typed_removal_is_not_mistaken_for_the_tap(monkeypatch) -> None:
    _replay_readiness(monkeypatch, promises_approvable=True)
    user_id, thread_id = _seed()
    await _turn(monkeypatch, user_id, thread_id, PROMPT, _planned(*_prompt_updates()))
    labels = ["r5", "r6", "r7", "r8", "r9"]
    reply = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        "Drop the numbered labels",
        _planned(
            *(
                BriefUpdate(operation="remove", target_requirement_id=r, expected_version=1)
                for r in labels
            )
        ),
    )
    brief = _brief(thread_id)
    assert {req.id for req in brief.live()} == set(ALL_IDS) - set(labels)
    assert {
        req.id: req.facts.get("superseded_by") for req in brief.requirements if not req.live
    } == {r: "creator" for r in labels}
    # The cards are still held and offered by name.
    assert reply.payload["choice_question"]["omitted_requirement_ids"] == ["r4"]


# == F10: the omission surfaces a new blocker ==================================================


@pytest.mark.asyncio
async def test_a_hold_after_the_tap_says_what_still_blocks_and_offers_nothing(monkeypatch) -> None:
    _replay_readiness(monkeypatch, promises_approvable=True, after_removal={"r10": "needs_choice"})
    user_id, thread_id = _seed()
    held = await _turn(monkeypatch, user_id, thread_id, PROMPT, _planned(*_prompt_updates()))
    reply = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        TAP,
        _planned(),
        tap_on=held.payload["choice_question"]["question_id"],
    )
    assert reply.event_type == "assistant_response"
    assert not reply.content.startswith("Here's the simpler version")
    assert f"- {ENDING}: {PROD_ROWS['r10']['reason']}" in reply.content
    assert not reply.payload.get("choice_question")
    assert "Should I make it without these" not in reply.content
    assert [req.id for req in _brief(thread_id).live()] == ALL_IDS


# == F4: the card names exactly what it leaves out, or there is no card ========================


def test_the_offer_names_all_eight_asks_prod_could_not_show() -> None:
    held = ["r1", "r4", *HELD_FOR_CHOICE[1:]]
    sentence, question = kria_runtime._simpler_version_offer(BRIEF_V1, held)
    assert question is not None
    assert question["omitted_requirement_ids"] == held
    for req_id in held:
        assert TEXTS[req_id] in sentence


def test_a_list_too_long_to_name_gets_no_card() -> None:
    long = BRIEF_V1.model_copy(
        update={
            "requirements": [
                req.model_copy(update={"description": f"{req.description} " + "x" * 150})
                for req in BRIEF_V1.requirements
            ]
        }
    )
    assert kria_runtime._simpler_version_offer(long, ALL_IDS) == ("", None)
