"""KRI-795: a render refused for an unresolved choice says what is unresolved and asks it again.

Prod T1 (thread 32A5B608, jobs 0701fcc9 + f88555bc, 2026-10-21): both renders were refused at
dispatch (`phone_plan_unsupported`, typed `needs_choice`, error "I can't verify this ordering
rule from the approved media."). Both chat cards read "This edit uses something your iPhone
can't render yet. Start a new edit with a different clip or format. Tell me which option you
want and I'll continue." The iPhone never saw the job, the clip and the format were fine, and no
options were shown. The creator then typed the option label "Continue without a fixed order";
nothing was open to answer, so it was ignored.

Failure modes, written before the code:

* F1 a needs_choice refusal blames the iPhone ("can't render yet") or sends the creator to a
  different clip or format
* F2 "Tell me which option you want" is shown with no options: no picker, and no option list in
  the text for a build that only shows the text
* F3 the unresolved item is not named, or is named in contract words ("ordering rule",
  "approved media")
* F4 the re-offered picker carries a conflict / input_digest the next turn's gate does not
  compute, so a tap is taken and never replayed (asked again, or ignored)
* F5 the re-offered picker reuses the old question_id, so the client and the answer fold treat
  it as already answered
* F6 a picker is attached when the thread never asked that question (a tap nothing replays)
* F7 an unrelated earlier question (the simpler-version one) is re-offered for an order refusal
* F8 a typed option label after the refusal (T1 turn 4) is not taken as the answer
* F9 the answer given after the refusal does not resolve the contract on the next draft
* F10 a Turkish chat gets the worker's English, or loses the reason
* F11 choice questions switched off: a picker is attached anyway, or the text points at one
* F12 the recovery changes: it stays ask_user, never a Retry
* F13 an untyped phone_plan_unsupported (a real device gap) loses its iPhone line
* F14 requirement_conflict refusals (lengths, revision, soundtracks) still blame the iPhone
* F15 the worker's way forward is added twice
* F16 a device-record needs_choice refusal still says "which option" with no options
* F17 the re-asked card still recommends the answer the refused render carried, so a
  "you choose" on the card hands the same refusal back

F1-F12 and F17 run here on the scratch Postgres through the real observer, the real `submit_turn`
free-text matcher, the real choice gate and the real render contract. F1-F3, F10 and F13-F16 are
swept over every refusal the contract writes in `test_kri795_refusal_copy.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria.brief import BriefRequirement
from app.kria.brief_binding import BriefBinding
from app.kria.reply_language import reply_language_for
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.services.choice_questions import (
    answered_brief,
    simpler_version_question,
    tag_event,
)
from app.services.creator_render_contract import build_render_contract
from app.tasks import kria_runtime
from app.tasks.kria_runtime import _observe_dispatched_execution
from tests.kria.test_choice_conflict_gate import _brief, _gate, _planned, _rows, _strategy
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.kria.test_title_block_answer_postgres import _failed_event, _reply

GENERATION = "gen-1"
ROWS = _rows(3)
SNAPSHOT = {"clip_assignments": ROWS}
# The canonical rule the contract cannot verify from the media (KRI-762 F3).
BRIEF = _brief(
    BriefRequirement(
        id="r2", kind="order", scope="global", description="clips 1, 2, 3 in that sequence"
    )
)
# Exactly what prod stored on job 0701fcc9 (job_debug.json).
PROD_DETAIL = "I can't verify this ordering rule from the approved media."
PROD_DECLINE = {
    "alternative": "Tell me which option you want and I'll continue.",
    "decline_reason": "needs_choice",
    "failure_reason": "phone_plan_unsupported",
}
PROD_CARD = (
    "This edit uses something your iPhone can't render yet. Start a new edit with a different "
    "clip or format. Tell me which option you want and I'll continue."
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)


async def _draft_time_question(monkeypatch: pytest.MonkeyPatch, language: str) -> dict:
    """The order question the real gate asks for this brief (T1 seq 6)."""
    with reply_language_for(language):
        asked = await _gate(monkeypatch, _planned(), rows=ROWS, brief=BRIEF)
    assert asked.plan.choice_question["conflict"] == "order_basis"
    return {"content": asked.plan.response, "question": asked.plan.choice_question}


def _seed_refused_render(
    history: list[dict], *, detail: str = PROD_DETAIL, answers: list[dict] | None = None
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A thread whose approved render was refused at dispatch, awaiting its observer.

    ``history`` is the chat before the approval: ``{"role", "event_type", "content",
    "payload"}`` rows appended after the seeded project events. ``answers`` are the choice
    answers the approval pinned (prod T1: on the binding; the job's strategy had lost them).
    """
    user_id, thread_id, session_id = _seed_runtime_project()
    binding = BriefBinding.create(
        thread_id, BRIEF, latest_message="Make the edit", choice_answers=answers
    )
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
        sequence = 2
        for row in [
            *history,
            {
                "role": "user",
                "event_type": "user_message",
                "content": "Approve",
                "payload": None,
            },
        ]:
            source = CreationThreadEvent(
                thread_id=thread_id,
                sequence=sequence,
                revision=sequence + 1,
                client_event_id=f"s-{uuid.uuid4().hex}",
                **row,
            )
            db.add(source)
            db.flush()
            sequence += 1
        turn = CreatorAgentTurn(
            thread_id=thread_id,
            session_id=session_id,
            source_event_id=source.id,
            client_event_id=source.client_event_id,
            request_digest=uuid.uuid4().hex,
            status="observing",
        )
        db.add(turn)
        db.flush()
        job = Job(
            user_id=user_id,
            status="processing_failed",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            failure_reason="phone_plan_unsupported",
            error_detail=detail,
            assembly_plan={
                "variants": [],
                "native_device_only": True,
                "creator_generation_id": GENERATION,
                "creator_brief_binding": binding.model_dump(mode="json"),
                "creator_decline": dict(PROD_DECLINE),
            },
        )
        db.add(job)
        db.flush()
        execution = CreatorAgentExecution(
            session_id=session_id,
            turn_id=turn.id,
            idempotency_key=f"s-{uuid.uuid4().hex}",
            request_digest=uuid.uuid4().hex,
            expected_revision=sequence,
            tool_name="render.request",
            tool_version=1,
            risk="approval_required",
            target_thread_id=thread_id,
            target_job_id=job.id,
            target_ownership_epoch=0,
            # Keep this fixture out of other xdist workers' global observer sweeps.
            # Its own observer below makes the dispatched handoff atomically.
            status="accepted",
            started_at=datetime.now(UTC),
            dispatched_at=datetime.now(UTC),
        )
        db.add(execution)
        item.current_job_id = job.id
        session.status = "rendering"
        session.target_job_id = job.id
        thread.active_job_id = job.id
        thread.revision = sequence
        db.commit()
        execution_id = execution.id
    return user_id, thread_id, execution_id


def _asked_and_answered(asked: dict, option: str) -> list[dict]:
    """T1 seq 6-7: the gate's order question and the creator's tap."""
    question = asked["question"]
    return [
        {"role": "user", "event_type": "user_message", "content": "Make it", "payload": None},
        {
            "role": "assistant",
            "event_type": "assistant_response",
            "content": asked["content"],
            "payload": {"turn_value": "question", "choice_question": question},
        },
        {
            "role": "user",
            "event_type": "user_message",
            "content": next(o["label"] for o in question["options"] if o["key"] == option),
            "payload": {
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": option,
                }
            },
        },
    ]


async def _refused(
    history: list[dict], *, detail: str = PROD_DETAIL, answers: list[dict] | None = None
) -> tuple[uuid.UUID, uuid.UUID, CreationThreadEvent]:
    user_id, thread_id, execution_id = _seed_refused_render(history, detail=detail, answers=answers)

    def observe_fixture():  # noqa: ANN202
        with sync_session() as db:
            execution = db.get(CreatorAgentExecution, execution_id, with_for_update=True)
            assert execution.status == "accepted"
            execution.status = "dispatched"
            db.flush()

            @contextmanager
            def owned_session():  # noqa: ANN202
                yield db

            # The real observer reads/locks/writes this real Postgres transaction.
            # Other workers see accepted until its usual failed-card commit.
            with patch.object(kria_runtime, "sync_session", owned_session):
                return _observe_dispatched_execution(execution_id)

    observed, _ = await asyncio.to_thread(observe_fixture)
    assert observed == "failed"
    return user_id, thread_id, _failed_event(thread_id)


async def _t1_refused(asked: dict) -> tuple[uuid.UUID, uuid.UUID, CreationThreadEvent]:
    """Prod T1: upload order tapped, pinned on the binding, and the render still refused."""
    return await _refused(
        _asked_and_answered(asked, "attachment_order"),
        answers=[
            {
                "conflict": "order_basis",
                "kind": "order_basis",
                "option": "attachment_order",
                "input_digest": asked["question"]["input_digest"],
                "requirement_ids": ["r2"],
                "source": "creator",
            }
        ],
    )


def _last_selection(thread_id: uuid.UUID) -> dict:
    """The newest creator answer in the thread, by event order."""
    with sync_session() as db:
        payloads = db.execute(
            select(CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "user",
            )
            .order_by(CreationThreadEvent.sequence.desc())
        ).scalars()
        return next(p["choice_selection"] for p in payloads if p and "choice_selection" in p)


def _thread_events(thread_id: uuid.UUID) -> tuple:
    with sync_session() as db:
        rows = db.execute(
            select(
                CreationThreadEvent.role,
                CreationThreadEvent.payload,
                CreationThreadEvent.event_type,
                CreationThreadEvent.content,
            )
            .where(CreationThreadEvent.thread_id == thread_id)
            .order_by(CreationThreadEvent.sequence)
        ).all()
    return tuple(tag_event(*row) for row in rows)


def _assert_no_dead_end(content: str) -> None:
    for wrong in (
        "iPhone",  # F1: the phone never saw this edit
        "different clip or format",  # F1
        "Tell me which option you want",  # F2
        "ordering rule",  # F3
        "approved media",  # F3
    ):
        assert wrong not in content, content


# ── F1-F5, F12: the card names the order problem and re-asks the gate's own question ────────


@pytest.mark.asyncio
async def test_the_t1_refusal_names_the_order_and_reasks_the_same_order_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        asked = await _draft_time_question(monkeypatch, "en")
        _, _, event = await _t1_refused(asked)

        assert event.content != PROD_CARD
        _assert_no_dead_end(event.content)
        assert event.content.startswith("I couldn't keep the clip order you asked for.")
        # F2: the options are in the text too (a build without the picker shows only this).
        # F17: the refused render carried upload order, so it is no longer the recommendation.
        assert (
            "\n1. Use the order you added the clips"
            "\n2. Continue without a fixed order (recommended)\n"
        ) in event.content

        question = event.payload["choice_question"]
        original = asked["question"]
        # F4: the same conflict, inputs and options the gate asked about at draft time.
        for key in ("conflict", "kind", "input_digest"):
            assert question[key] == original[key], key
        assert [(o["key"], o["label"]) for o in question["options"]] == [
            (o["key"], o["label"]) for o in original["options"]
        ]
        assert [o["key"] for o in question["options"] if o["recommended"]] == ["unordered"]
        # F5: a new ask, so the earlier answer does not close it.
        assert question["question_id"] != original["question_id"]
        # F12: still the creator's decision, never a retry.
        assert event.payload["recovery"] == "ask_user"
        assert event.payload["decline_reason"] == "needs_choice"
        assert event.payload["code"] == "phone_plan_unsupported"
    finally:
        await async_engine.dispose()


# ── F8 + F9: an answer after the refusal is taken, replayed and resolves the contract ──────


@pytest.mark.parametrize(
    ("reply", "option"),
    [
        ("Continue without a fixed order", "unordered"),  # T1 turn 4, typed
        ("no fixed order", "unordered"),
        ("Use the order you added the clips", "attachment_order"),
        ("you choose", "unordered"),  # F17: delegation never lands on the refused answer
    ],
)
@pytest.mark.asyncio
async def test_an_answer_after_the_refusal_is_replayed_and_the_contract_resolves(
    monkeypatch: pytest.MonkeyPatch, reply: str, option: str
) -> None:
    try:
        asked = await _draft_time_question(monkeypatch, "en")
        user_id, thread_id, event = await _t1_refused(asked)
        await _reply(user_id, thread_id, reply)

        selection = _last_selection(thread_id)
        assert (selection["question_id"], selection["option_key"]) == (
            event.payload["choice_question"]["question_id"],
            option,
        )
        assert selection.get("delegated", False) is (reply == "you choose")
        # The next draft: the real gate replays the answer from the real thread events.
        gated = await _gate(
            monkeypatch, _planned(), rows=ROWS, brief=BRIEF, events=_thread_events(thread_id)
        )
        strategy = _strategy(gated)  # no question: answered
        (answer,) = strategy["choice_answers"]
        assert (answer["conflict"], answer["option"]) == ("order_basis", option)
        assert answer["input_digest"] == asked["question"]["input_digest"]
        contract = build_render_contract(
            strategy,
            generation_id=GENERATION,
            brief=answered_brief(BRIEF, strategy),
            media_snapshot=SNAPSHOT,
        )
        assert contract.unresolved == ()
        assert contract.order_required is (option == "attachment_order")
    finally:
        await async_engine.dispose()


# ── F10: a Turkish chat ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turkish_chat_gets_the_reason_and_the_turkish_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kria_runtime, "thread_reply_language", lambda _thread: "tr")
    try:
        asked = await _draft_time_question(monkeypatch, "tr")
        user_id, thread_id, event = await _t1_refused(asked)

        content = event.content
        assert content.startswith("İstediğin klip sırasını koruyamadım. Hangisini tercih edersin?")
        assert (
            "\n1. Klipleri eklediğin sırayı kullan\n2. Sabit bir sıra olmadan devam et (önerilen)\n"
        ) in content
        assert "Bir seçeneğe dokun ya da kendi sözlerinle anlat." in content
        for english in ("I couldn't", "Tell me", "iPhone", "order"):
            assert english not in content, content

        await _reply(user_id, thread_id, "Sabit bir sıra olmadan devam et")
        assert _last_selection(thread_id)["option_key"] == "unordered"
    finally:
        await async_engine.dispose()


# ── F6, F7, F11: no picker that nothing would replay ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_order_question_in_the_thread_means_no_picker_and_a_real_next_step() -> None:
    try:
        _, _, event = await _refused([])

        assert "choice_question" not in event.payload  # F6
        _assert_no_dead_end(event.content)
        assert event.content == (
            "I couldn't keep the clip order you asked for. Tell me how you want the clips "
            "ordered, and I'll make a new draft."
        )
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_an_unrelated_earlier_question_is_not_reoffered_for_an_order_refusal() -> None:
    try:
        simpler = simpler_version_question()
        _, _, event = await _refused(
            [
                {
                    "role": "assistant",
                    "event_type": "assistant_response",
                    "content": "Should I try a different approach, or make this simpler version?",
                    "payload": {"turn_value": "question", "choice_question": simpler},
                }
            ]
        )
        assert "choice_question" not in event.payload  # F7
        assert "Tell me how you want the clips ordered" in event.content
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_choice_questions_off_attach_no_picker_and_point_at_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        asked = await _draft_time_question(monkeypatch, "en")
        monkeypatch.setattr(settings, "kria_choice_questions_enabled", False)
        _, _, event = await _t1_refused(asked)

        assert "choice_question" not in event.payload  # F11
        assert "Tap an option" not in event.content
        _assert_no_dead_end(event.content)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_refusal_the_order_question_cannot_fix_gets_no_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload order was chosen and the clip rows did not add up: re-asking would recommend
    the answer that just failed, so the card says what to do instead."""
    try:
        asked = await _draft_time_question(monkeypatch, "en")
        _, _, event = await _refused(
            _asked_and_answered(asked, "attachment_order"),
            detail="I need to know which clips are in the edit to verify their order.",
        )
        assert "choice_question" not in event.payload
        assert event.content == (
            "I couldn't tell which clips are in this edit, so I couldn't keep their order. "
            "Tell me how you want the clips ordered, and I'll make a new draft."
        )
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_refusal_fixture_survives_an_unrelated_global_observer_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parallel CI worker's global sweep cannot consume this test's observation."""
    seed = _seed_refused_render
    swept: list[uuid.UUID] = []

    def seed_with_sweep(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        project = seed(*args, **kwargs)
        swept.append(project[2])
        assert kria_runtime._settle_dispatched_executions([project[2]]) == 0
        return project

    monkeypatch.setitem(globals(), "_seed_refused_render", seed_with_sweep)
    try:
        asked = await _draft_time_question(monkeypatch, "en")
        _, thread_id, event = await _t1_refused(asked)
        assert len(swept) == 1
        _assert_no_dead_end(event.content)
        assert event.payload["choice_question"]["kind"] == "order_basis"
        with sync_session() as db:
            failed_events = db.execute(
                select(CreationThreadEvent.id).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "assistant_render_failed",
                )
            ).all()
        assert len(failed_events) == 1
    finally:
        await async_engine.dispose()
