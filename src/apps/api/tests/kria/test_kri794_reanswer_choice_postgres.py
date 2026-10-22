"""KRI-794 (Merope café T1, 2026-10-21) on real Postgres: changing a closed choice by typing it.

The creator answered the order question with "Use the order you added the clips", the render
was refused ("I can't verify this ordering rule from the approved media."), and they typed the
other option's exact label, "Continue without a fixed order". No question was open, so the
message was read as a new edit: the old answer stayed, the draft said "I'm using the order you
added the clips" and the session's last render was refused again.

Only the Main Creator model call is replaced (the journey harness of
``test_choice_conflict_journey``); ``submit_turn``'s free-text matcher, the planner gate, the
render contract, the typed phone decline and the real render observer all run.

Ways this can fail (written before the code):

1. The other option's exact label (or a topical alias) typed after the card closed is not
   taken: the old answer stays (the incident).
2. The new answer is stored but the draft, its disclosure or the approval binding still
   carries the old one.
3. A standing answer is repeated on a later turn as if it were a fresh promise, with no
   word that it was chosen earlier or how to change it.
4. After a render refusal over the order, the next turn re-applies the refused answer and
   drafts the same edit again, instead of offering the choice once more.
5. That re-ask recommends the option that was just refused, or never says why it is asked.
6. A refusal about something else re-opens the order question.
7. The re-ask loops: past the ask limit the answer must keep applying, never be dropped.
8. False re-answers with no card on screen: a bare "2", an internal key ("unordered"), "you
   choose", a generic "fark etmez", a sentence that merely contains a label ("don't use the
   order you added the clips").
9. Naming the standing answer again turns a plain re-send into a new turn (KRI-552), unless a
   refusal of that choice came after it (then it is a real confirmation).
10. A client-written ``choice_selection`` for the closed question is accepted (answers must
    stay server-owned).
11. A consent-scoped question (the simpler version) becomes re-answerable by stale text.
12. The refusal is not linked to the choice by structure (an unresolved ORDER line raises
    with no ``field_path``), so nothing downstream can tell it was about the order.
13. A render approved BEFORE a typed re-answer is refused after the re-answer is stored: the
    refusal is blamed on the new answer, which is then re-asked instead of applied.
14. "You choose" on the refusal re-ask lands on the option that was just refused.
15. The kept note's "To change it, reply ..." quotes words that do not re-answer once the
    chat's language has changed.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief_binding import BriefBinding
from app.kria.runtime import RuntimeFailure, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.services import creator_render_contract as contract_module
from app.services.choice_questions import simpler_version_question
from app.services.creator_render_contract import (
    CreatorRenderContractError,
    build_render_contract,
    check_phone_dispatch_contract,
    decline_payload,
)
from app.tasks.kria_runtime import (
    _append_sync_event,
    _observe_dispatched_execution,
    run_kria_turn,
)
from tests.kria.test_choice_conflict_journey import (
    _decide,
    _draft_strategy,
    _events,
    _head_draft,
    _project,
    _questions,
    _say,
)

ASK = "Put them in the order I filmed them"
ATTACHMENT_LABEL = "Use the order you added the clips"
UNORDERED_LABEL = "Continue without a fixed order"
ATTACHMENT_NOTE = "I'm using the order you added the clips."
UNORDERED_NOTE = "I'm not promising a particular order."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    await async_engine.dispose()
    yield
    await async_engine.dispose()


def _selections(thread_id: uuid.UUID) -> list[dict]:
    return [
        e.payload["choice_selection"]
        for e in _events(thread_id)
        if e.role == "user" and e.payload and e.payload.get("choice_selection")
    ]


def _last_summary(thread_id: uuid.UUID) -> str:
    """The apply-strategy summary of the newest turn that drafted (the chat's plan line)."""
    with sync_session() as db:
        turns = (
            db.execute(
                select(CreatorAgentTurn)
                .where(CreatorAgentTurn.thread_id == thread_id)
                .order_by(CreatorAgentTurn.created_at.desc())
            )
            .scalars()
            .all()
        )
    for turn in turns:
        for intent in (turn.plan_json or {}).get("intents") or []:
            if intent.get("tool_name") == "draft.apply_strategy":
                return str(intent["arguments"].get("summary") or "")
    return ""


def _last_reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    return [e for e in _events(thread_id) if e.role == "assistant"][-1]


async def _answered(monkeypatch, option: str = "attachment_order") -> tuple:  # noqa: ANN001
    """The order question asked and answered by a TAP, its draft denied (no render pending)."""
    user_id, thread_id, session_id = _project(monkeypatch, order=True)
    await _say(user_id, thread_id, ASK)
    (question,) = _questions(thread_id)
    assert question["kind"] == "order_basis"
    selection = {"question_id": question["question_id"], "option_key": option}
    label = ATTACHMENT_LABEL if option == "attachment_order" else UNORDERED_LABEL
    result = await _say(user_id, thread_id, label, selection=selection)
    assert result["status"] == "awaiting_approval"
    assert _draft_strategy(_head_draft(thread_id))["choice_answers"][0]["option"] == option
    return user_id, thread_id, session_id, question


def _approve_and_dispatch(thread_id: uuid.UUID, session_id: uuid.UUID) -> uuid.UUID:
    """The awaiting draft is approved and dispatched; its job has already been refused.

    The refusal is the REAL one: the dispatch contract is built from the approved binding with
    the strategy's answer missing (the KRI-788 loss), and ``check_phone_dispatch_contract``
    raises the typed decline the worker persists. Nothing is observed yet (the render is in
    flight as far as the chat knows); ``_observe`` posts the card.
    """
    draft = _head_draft(thread_id)
    binding = BriefBinding.model_validate(draft.snapshot_json["brief_binding"])
    stripped = {k: v for k, v in _draft_strategy(draft).items() if k != "choice_answers"}
    contract = build_render_contract(
        stripped,
        generation_id="refused",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
    )
    with pytest.raises(CreatorRenderContractError) as refused:
        check_phone_dispatch_contract(
            contract, snapshot_generation_id="refused", has_voiceover_candidate=False
        )
    decline = decline_payload(refused.value)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        turn = (
            db.execute(
                select(CreatorAgentTurn).where(
                    CreatorAgentTurn.thread_id == thread_id,
                    CreatorAgentTurn.status == "awaiting_approval",
                )
            )
            .scalars()
            .one()
        )
        approval = (
            db.execute(select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id))
            .scalars()
            .one()
        )
        execution = (
            db.execute(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.turn_id == turn.id,
                    CreatorAgentExecution.tool_name == "render.request",
                )
            )
            .scalars()
            .one()
        )
        job = Job(
            user_id=thread.creator_id,
            status="processing_failed",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            failure_reason="phone_plan_unsupported",
            error_detail=str(refused.value),
            assembly_plan={
                "variants": [],
                "creator_decline": {**decline, "failure_reason": "phone_plan_unsupported"},
            },
        )
        db.add(job)
        db.flush()
        now = datetime.now(UTC)
        approval.status = "approved"
        approval.consumed_at = now
        execution.status = "dispatched"
        execution.target_job_id = job.id
        execution.target_thread_id = thread_id
        execution.dispatched_at = now
        turn.status = "observing"
        session.status = "rendering"
        session.target_job_id = job.id
        item.current_job_id = job.id
        thread.active_job_id = job.id
        db.commit()
        return execution.id


def _observe(thread_id: uuid.UUID, execution_id: uuid.UUID) -> dict:
    """The real render observer settles the refused job and posts the chat card."""
    observed, _ = _observe_dispatched_execution(execution_id)  # runs in a worker thread
    assert observed == "failed"
    card = _last_reply(thread_id)
    assert card.event_type == "assistant_render_failed"
    return card.payload


def _refuse_the_approved_render(thread_id: uuid.UUID, session_id: uuid.UUID) -> dict:
    return _observe(thread_id, _approve_and_dispatch(thread_id, session_id))


async def _refused(monkeypatch) -> tuple:  # noqa: ANN001
    user_id, thread_id, session_id, question = await _answered(monkeypatch)
    payload = await asyncio.to_thread(_refuse_the_approved_render, thread_id, session_id)
    return user_id, thread_id, session_id, question, payload


# -- 1/2: the incident --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed", [UNORDERED_LABEL, "continue without a fixed order!", "no fixed order", "sıra olmasın"]
)
async def test_typing_the_other_option_after_the_card_closed_replaces_the_answer(
    monkeypatch, typed: str
) -> None:
    user_id, thread_id, _, question = await _answered(monkeypatch)
    assert ATTACHMENT_NOTE in _last_summary(thread_id)
    await _decide(user_id, thread_id, "deny")

    result = await _say(user_id, thread_id, typed)

    assert result["status"] == "awaiting_approval"
    assert _selections(thread_id)[-1] == {
        "question_id": question["question_id"],
        "option_key": "unordered",
        "reanswer": True,
    }
    assert len(_questions(thread_id)) == 1  # nothing asked again
    draft = _head_draft(thread_id)
    (answer,) = _draft_strategy(draft)["choice_answers"]
    assert answer["option"] == "unordered" and answer["source"] == "creator"
    summary = _last_summary(thread_id)
    assert UNORDERED_NOTE in summary and "order you added the clips" not in summary
    binding = BriefBinding.model_validate(draft.snapshot_json["brief_binding"])
    assert binding.choice_answers == [answer]  # approval pins the NEW answer
    pinned = build_render_contract(
        _draft_strategy(draft),
        generation_id="after",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
    )
    assert pinned.order_required is False and not pinned.unresolved


# -- 3: a kept answer is said as kept ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_later_turn_says_the_answer_is_kept_and_how_to_change_it(monkeypatch) -> None:
    user_id, thread_id, _, _ = await _answered(monkeypatch)
    assert _last_summary(thread_id).endswith(ATTACHMENT_NOTE)  # the answering turn: plain
    await _decide(user_id, thread_id, "deny")

    await _say(user_id, thread_id, "Make it a bit faster")

    summary = _last_summary(thread_id)
    assert (
        f'As agreed earlier: {ATTACHMENT_NOTE} To change it, reply "{UNORDERED_LABEL}".' in summary
    )
    assert _draft_strategy(_head_draft(thread_id))["choice_answers"][0]["option"] == (
        "attachment_order"
    )


# -- 4/5/12: a refusal over the order re-offers the choice ---------------------------------


@pytest.mark.asyncio
async def test_the_refusal_card_names_the_order_choice_by_field_path(monkeypatch) -> None:
    _, thread_id, _, _, payload = await _refused(monkeypatch)
    assert payload["decline_reason"] == "needs_choice"
    assert payload["field_path"] == "ordering_choice"


@pytest.mark.asyncio
async def test_the_refusal_card_offers_the_order_choice_again_before_another_turn(
    monkeypatch,
) -> None:
    user_id, thread_id, _, first, payload = await _refused(monkeypatch)
    revision = _head_draft(thread_id).draft_revision

    questions = _questions(thread_id)
    assert len(questions) == 2  # the one re-ask, as a picker
    again = questions[-1]
    assert again["conflict"] == "order_basis"
    assert again["input_digest"] == first["input_digest"]
    recommended = {o["key"]: o["recommended"] for o in again["options"]}
    # never recommend what was just refused; recommend the way forward that cannot hit it
    assert recommended == {"attachment_order": False, "unordered": True}
    reply = _last_reply(thread_id)
    assert reply.content.startswith("I couldn't keep the clip order you asked for.")
    assert payload["choice_question"]["question_id"] == again["question_id"]
    assert _head_draft(thread_id).draft_revision == revision  # the refused edit is not redrafted

    tapped = {"question_id": again["question_id"], "option_key": "unordered"}
    result = await _say(user_id, thread_id, UNORDERED_LABEL, selection=tapped)
    assert result["status"] == "awaiting_approval"
    assert _draft_strategy(_head_draft(thread_id))["choice_answers"][0]["option"] == "unordered"


@pytest.mark.asyncio
async def test_typing_the_other_option_right_after_the_refusal_needs_no_reask(
    monkeypatch,
) -> None:
    """The exact incident sequence: answer, refusal, typed other label."""
    user_id, thread_id, _, first, payload = await _refused(monkeypatch)

    result = await _say(user_id, thread_id, UNORDERED_LABEL)

    assert result["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 2  # already re-asked on the failure card, no third ask
    assert _selections(thread_id)[-1]["question_id"] == payload["choice_question"]["question_id"]
    assert _selections(thread_id)[-1]["option_key"] == "unordered"
    assert _draft_strategy(_head_draft(thread_id))["choice_answers"][0]["option"] == "unordered"
    assert UNORDERED_NOTE in _last_summary(thread_id)


@pytest.mark.asyncio
async def test_retyping_the_refused_option_after_the_refusal_confirms_it(monkeypatch) -> None:
    user_id, thread_id, _, first, payload = await _refused(monkeypatch)

    result = await _say(user_id, thread_id, ATTACHMENT_LABEL)

    assert result["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 2
    assert _selections(thread_id)[-1] == {
        "question_id": payload["choice_question"]["question_id"],
        "option_key": "attachment_order",
    }
    assert _last_summary(thread_id).endswith(ATTACHMENT_NOTE)  # a fresh answer, said plainly


@pytest.mark.asyncio
async def test_a_second_refusal_after_the_reask_keeps_the_answer_without_a_third_ask(
    monkeypatch,
) -> None:
    user_id, thread_id, session_id, _, _ = await _refused(monkeypatch)
    again = _questions(thread_id)[-1]
    tapped = {"question_id": again["question_id"], "option_key": "attachment_order"}
    await _say(user_id, thread_id, ATTACHMENT_LABEL, selection=tapped)
    await asyncio.to_thread(_refuse_the_approved_render, thread_id, session_id)
    assert len(_questions(thread_id)) == 2  # failure card must respect the same ask limit

    result = await _say(user_id, thread_id, "Try again please")

    assert result["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 2  # the limit holds
    assert _draft_strategy(_head_draft(thread_id))["choice_answers"][0]["option"] == (
        "attachment_order"
    )  # never dropped for the creator
    assert "As agreed earlier" in _last_summary(thread_id)


@pytest.mark.asyncio
async def test_you_choose_on_the_refusal_reask_never_picks_the_refused_option(
    monkeypatch,
) -> None:
    user_id, thread_id, _, _, _ = await _refused(monkeypatch)
    assert len(_questions(thread_id)) == 2

    result = await _say(user_id, thread_id, "you choose")

    assert result["status"] == "awaiting_approval"
    (answer,) = _draft_strategy(_head_draft(thread_id))["choice_answers"]
    assert (answer["option"], answer["source"]) == ("unordered", "creator_delegated")


@pytest.mark.asyncio
async def test_a_refusal_of_the_render_approved_before_the_re_answer_is_not_blamed_on_it(
    monkeypatch,
) -> None:
    """The creator types the other option while the old render is in flight; that render is
    refused only after the re-answer is stored (the observer posts the card, then promotes the
    queued turn). The refusal is about the OLD answer: the new one applies, nothing is re-asked."""
    user_id, thread_id, session_id, question = await _answered(monkeypatch)
    execution_id = await asyncio.to_thread(_approve_and_dispatch, thread_id, session_id)
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=UNORDERED_LABEL,
                client_event_id=f"q-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    assert accepted.status == "queued"
    assert _selections(thread_id)[-1] == {
        "question_id": question["question_id"],
        "option_key": "unordered",
        "reanswer": True,
    }
    await asyncio.to_thread(_observe, thread_id, execution_id)  # the OLD render's card

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 1
    assert _draft_strategy(_head_draft(thread_id))["choice_answers"][0]["option"] == "unordered"
    assert UNORDERED_NOTE in _last_summary(thread_id)


@pytest.mark.asyncio
async def test_the_kept_note_quotes_words_that_re_answer_after_the_language_changes(
    monkeypatch,
) -> None:
    user_id, thread_id, _, question = await _answered(monkeypatch)
    await _decide(user_id, thread_id, "deny")

    await _say(user_id, thread_id, "Biraz daha hızlı yap lütfen")

    summary = _last_summary(thread_id)
    hint = f'Değiştirmek için "{UNORDERED_LABEL}" yaz.'
    assert "Daha önce anlaştığımız gibi:" in summary and hint in summary, summary
    await _decide(user_id, thread_id, "deny")
    await _say(user_id, thread_id, UNORDERED_LABEL)  # exactly what the note told them to type
    assert _selections(thread_id)[-1]["option_key"] == "unordered"
    assert _draft_strategy(_head_draft(thread_id))["choice_answers"][0]["option"] == "unordered"


@pytest.mark.asyncio
async def test_a_refusal_about_something_else_does_not_reopen_the_order(monkeypatch) -> None:
    user_id, thread_id, _, _ = await _answered(monkeypatch)
    await _decide(user_id, thread_id, "deny")
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_render_failed",
            content="This edit needs your confirmed recorded voice.",
            payload={
                "code": "phone_plan_unsupported",
                "decline_reason": "needs_choice",
                "field_path": "audio_strategy",
            },
        )
        db.commit()

    result = await _say(user_id, thread_id, "Try again please")

    assert result["status"] == "awaiting_approval"
    assert len(_questions(thread_id)) == 1


# -- 8/9/10/11: nothing else becomes an answer ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed",
    [
        "2",
        "unordered",
        "you choose",
        "fark etmez",
        "Don't continue without a fixed order",
        ATTACHMENT_LABEL,  # the standing answer again, no refusal since: not a new answer
    ],
)
async def test_without_the_card_only_an_option_name_re_answers(monkeypatch, typed: str) -> None:
    user_id, thread_id, _, _ = await _answered(monkeypatch)
    await _decide(user_id, thread_id, "deny")
    before = _selections(thread_id)

    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=typed,
                client_event_id=f"n-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )

    assert _selections(thread_id) == before


@pytest.mark.asyncio
async def test_a_client_selection_for_the_closed_question_is_still_stale(monkeypatch) -> None:
    user_id, thread_id, _, question = await _answered(monkeypatch)
    await _decide(user_id, thread_id, "deny")
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        with pytest.raises(RuntimeFailure) as err:
            await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message=UNORDERED_LABEL,
                    client_event_id=f"c-{uuid.uuid4().hex}",
                    expected_thread_revision=revision,
                    choice_selection={
                        "question_id": question["question_id"],
                        "option_key": "unordered",
                    },
                ),
            )
    assert err.value.code == "choice_selection_stale"


@pytest.mark.asyncio
async def test_a_closed_simpler_version_question_is_never_re_answered(monkeypatch) -> None:
    user_id, thread_id, _, _ = await _answered(monkeypatch)
    await _decide(user_id, thread_id, "deny")
    question = simpler_version_question()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="Should I try a different approach, or make this simpler version?",
            payload={"turn_value": "question", "choice_question": question},
        )
        _append_sync_event(
            db,
            thread,
            role="user",
            event_type="user_message",
            content="Try a different approach",
            payload={
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": "different_approach",
                }
            },
        )
        db.commit()
        revision = db.get(CreationThread, thread_id).revision
    before = _selections(thread_id)
    async with AsyncSessionLocal() as db:
        await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Make this simpler version",
                client_event_id=f"s-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    assert _selections(thread_id) == before


# -- 12: every tagged order line is one the contract really writes -------------------------


def test_every_tagged_order_line_is_written_by_the_contract_and_pins_are_not_tagged() -> None:
    source = Path(contract_module.__file__).read_text()
    counts: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            counts[node.value] = counts.get(node.value, 0) + 1
    for line in contract_module.ORDER_BASIS_UNRESOLVED:
        # Once in the set, at least once where `build_render_contract` appends it.
        assert counts.get(line, 0) >= 2, line
        assert contract_module.unresolved_field_path(line) == "ordering_choice"
    assert contract_module.unresolved_field_path(contract_module._SEQUENCE_UNRESOLVED) is None
    assert contract_module.unresolved_field_path(contract_module._pin_not_found("the cafe")) is None
