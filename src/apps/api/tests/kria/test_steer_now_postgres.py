"""Steer now: a waiting message can take over from the turn Kria is still working on.

Green 5 (prod 2026-11-04, thread D842DE91, build 1888): the creator tapped the "Suggest an edit."
chip (turn 9588752d, 12:45:44), typed the full request 14 s later (turn bfd0b2c1, queued behind the
chip turn), and approved the chip's generic card at 12:47:15 because nothing said a newer message
was waiting. The video ignored the request.

Emir's direction: the new message looks like a queued message, with a "Steer now" button that
overrides. The server side is the existing turn cancel with `steer: true`. Every test drives the
real entry points (`submit_turn`, `cancel_turn`, `decide_approval`) and the real worker tasks
(`run_kria_turn`, `execute_kria_approval`) on real Postgres; only model calls are stubbed. The ways
this can fail, and which test pins each, are in docs/pipelines/steer-now.md.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.brief_extractor import BriefExtractionOutput
from app.agents._schemas.creator_agent import AskUser, CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody, TurnAccepted
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.reply_language import thread_reply_language
from app.kria.runtime import (
    RuntimeFailure,
    approval_fingerprint,
    cancel_turn,
    decide_approval,
    submit_turn,
)
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.tasks import kria_runtime as rt
from app.tasks.content_plan_build import DispatchResult

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_creative_brief import _ASK, _MANIFEST  # noqa: E402
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

GREEN5_CHIP = "Suggest an edit."
GREEN5_PROMPT = (
    "Yorumla episode 9: Kervan x Lumo Pamukkale show. Make the whole video like a green screen: "
    "me small at the bottom right in front, and behind me full screen whichever photo or video "
    "I'm talking about, changing every 3-5 seconds. Cut the pauses and the sentence I restarted. "
    "Captions small, white, lowercase, in the middle. No music."
)
STOP_LINE_EN = "Stopped. Working on your newer message instead."
STOP_LINE_TR = "Durdurdum. Bunun yerine yeni mesajın üzerinde çalışıyorum."


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(settings, "kria_steer_now_enabled", on)
    monkeypatch.setattr(settings, "kria_steer_now_user_ids", [])


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    _switch(monkeypatch, True)


# -- the app's calls ----------------------------------------------------------------------


async def _send(
    user_id: uuid.UUID, thread_id: uuid.UUID, message: str, *, locale: str | None = None
) -> TurnAccepted:
    """The app sends a message at the thread revision it last read (with its phone locale)."""
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            revision = int((await db.get(CreationThread, thread_id)).revision)
            await db.rollback()
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message=message,
                    client_event_id=f"steer-{uuid.uuid4().hex}",
                    expected_thread_revision=revision,
                ),
                locale=locale,
            )
            return accepted
    finally:
        await engine.dispose()


async def _steer(user_id: uuid.UUID, thread_id: uuid.UUID, turn_id: str, *, steer: bool = True):
    """The creator taps Steer now on the message waiting behind `turn_id`.

    Returns the server's answer, or the RuntimeFailure the route turns into a 4xx.
    """
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            revision = int((await db.get(CreationThread, thread_id)).revision)
            await db.rollback()
            try:
                cancelled, _successor = await cancel_turn(
                    db,
                    thread_id=thread_id,
                    turn_id=uuid.UUID(turn_id),
                    creator_id=user_id,
                    expected_thread_revision=revision,
                    **({"steer": True} if steer else {}),
                )
            except RuntimeFailure as failure:
                await db.rollback()
                return failure
            return cancelled
    finally:
        await engine.dispose()


async def _approve(user_id: uuid.UUID, thread_id: uuid.UUID, approval_id: uuid.UUID):
    """The creator taps "Create this video". Returns the answer or the 4xx failure."""
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval_id)
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                response, _ = await decide_approval(
                    db,
                    thread_id=thread_id,
                    approval_id=approval_id,
                    creator_id=user_id,
                    decision="approve",
                    body=body,
                )
            except RuntimeFailure as failure:
                await db.rollback()
                return failure
            return response
    finally:
        await engine.dispose()


# -- what the creator sees ----------------------------------------------------------------


def _events(thread_id: uuid.UUID, event_type: str) -> list[CreationThreadEvent]:
    with sync_session() as db:
        return list(
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == event_type,
                )
                .order_by(CreationThreadEvent.sequence)
            )
            .scalars()
            .all()
        )


def _user_message(thread_id: uuid.UUID, turn_id: str) -> CreationThreadEvent:
    return next(
        e for e in _events(thread_id, "user_message") if (e.payload or {}).get("turn_id") == turn_id
    )


def _status(turn_id: str) -> str:
    with sync_session() as db:
        return db.get(CreatorAgentTurn, uuid.UUID(turn_id)).status


def _running(thread_id: uuid.UUID) -> list[str]:
    """Turns Kria is working on now (the runtime allows at most one)."""
    with sync_session() as db:
        return [
            str(t.id)
            for t in db.execute(
                select(CreatorAgentTurn).where(
                    CreatorAgentTurn.thread_id == thread_id,
                    CreatorAgentTurn.status.in_(
                        ("pending", "planning", "awaiting_approval", "executing", "observing")
                    ),
                )
            )
            .scalars()
            .all()
        ]


def _live_cards(thread_id: uuid.UUID) -> list[str]:
    """Turn ids of approval cards the creator can still act on."""
    with sync_session() as db:
        return [
            str(a.turn_id)
            for a in db.execute(
                select(CreatorAgentApproval)
                .where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
                .order_by(CreatorAgentApproval.created_at)
            )
            .scalars()
            .all()
        ]


def _card_closed_in_chat(thread_id: uuid.UUID, approval_id: str) -> bool:
    """Both clients close a card only on an approval_* event naming it."""
    closing = {"approval_approved", "approval_denied", "approval_cancelled", "approval_expired"}
    with sync_session() as db:
        return any(
            (e.payload or {}).get("approval_id") == approval_id
            for e in db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(closing),
                )
            )
            .scalars()
            .all()
        )


def _reply_language(thread_id: uuid.UUID) -> str | None:
    with sync_session() as db:
        return thread_reply_language(db.get(CreationThread, thread_id))


def _set_language(thread_id: uuid.UUID, language: str) -> None:
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        thread.state = {**(thread.state or {}), "reply_language": language}
        db.commit()


# -- the worker, with the model stubbed ---------------------------------------------------


def _card_plan() -> PlannedKriaTurn:
    """A strategy draft: the turn ends on an approval card ("Create this video")."""
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="licensed_music",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="Open on the whisk and end on the packed order.",
            ),
            summary="Open on the whisk and finish on the packed order.",
        )
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


def _reply_plan(text_: str) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response=text_),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


async def _run_turn(turn_id: str) -> dict:
    with patch.object(rt.run_kria_turn, "apply_async"):
        return await asyncio.to_thread(rt.run_kria_turn.run, turn_id)


def _fake_dispatch(user_id: uuid.UUID, thread_id: uuid.UUID):  # noqa: ANN202
    def _dispatch(*_args, **_kwargs) -> DispatchResult:  # noqa: ANN002, ANN003
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id)
            session = db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
            item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
            job = Job(
                user_id=user_id,
                status="queued",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                assembly_plan={"variants": []},
            )
            db.add(job)
            db.flush()
            item.current_job_id = job.id
            db.commit()
            return DispatchResult("dispatched", job_id=str(job.id))

    return _dispatch


async def _start_render(monkeypatch: pytest.MonkeyPatch, user_id, thread_id, approval_id) -> dict:  # noqa: ANN001
    """The approval's render claim (`execute_kria_approval`), with the renderer stubbed."""
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch(user_id, thread_id)
    )
    return await asyncio.to_thread(rt.execute_kria_approval.run, str(approval_id))


async def _card_showing_with_a_waiting_message(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Turn A's card is up; the creator then sent B, which waits behind it."""
    user_id, thread_id, _ = _seed_runtime_project()
    monkeypatch.setattr(rt, "_plan_with_live_agent", AsyncMock(return_value=_card_plan()))
    first = await _send(user_id, thread_id, "make the intro faster")
    assert (await _run_turn(first.turn_id))["status"] == "awaiting_approval"
    waiting = await _send(user_id, thread_id, "and use a calmer song")
    assert waiting.status == "queued"
    with sync_session() as db:
        approval_id = db.execute(
            select(CreatorAgentApproval.id).where(
                CreatorAgentApproval.turn_id == uuid.UUID(first.turn_id)
            )
        ).scalar_one()
    return SimpleNamespace(
        user_id=user_id, thread_id=thread_id, first=first, waiting=waiting, approval_id=approval_id
    )


# -- Green 5 ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_green5_steer_now_gives_one_card_for_the_typed_request(monkeypatch) -> None:  # noqa: ANN001
    """F4, F10: chip, then the typed request while the chip turn plans, then Steer now."""
    user_id, thread_id, _ = _seed_runtime_project()
    # The phone is set to Turkish; the chip carries no words of the creator's.
    chip = await _send(user_id, thread_id, GREEN5_CHIP, locale="tr-TR")
    typed: list[TurnAccepted] = []
    stops: list = []
    planned_for: list[str] = []

    async def _planner(_snapshot, user_message, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        planned_for.append(user_message)
        if user_message == GREEN5_CHIP:
            typed.append(await _send(user_id, thread_id, GREEN5_PROMPT, locale="tr-TR"))
            stops.append(await _steer(user_id, thread_id, chip.turn_id))
        return _card_plan()

    monkeypatch.setattr(rt, "_plan_with_live_agent", _planner)
    chip_run = await _run_turn(chip.turn_id)

    assert typed[0].status == "queued"
    # The app needs to know which turn the waiting message waits behind.
    assert _user_message(thread_id, typed[0].turn_id).payload["queued_behind_turn_id"] == (
        chip.turn_id
    )
    assert not isinstance(stops[0], RuntimeFailure), stops[0]
    # The chip's plan is thrown away: no card for the generic suggestion.
    assert chip_run["status"] == "ignored"
    assert _status(chip.turn_id) == "cancelled"
    assert _events(thread_id, "approval_requested") == []
    # The chat says honestly what happened, in the chat's language (English after the request).
    stop = _events(thread_id, "turn_cancelled")[-1]
    assert stop.payload["steered_to_turn_id"] == typed[0].turn_id
    line = _events(thread_id, "status_update")[-1]
    assert (line.role, line.payload["turn_id"], line.content) == (
        "assistant",
        chip.turn_id,
        STOP_LINE_EN,
    )
    assert _status(typed[0].turn_id) == "pending"

    typed_run = await _run_turn(typed[0].turn_id)

    assert typed_run == {"turn_id": typed[0].turn_id, "status": "awaiting_approval"}
    # Exactly one card, for the request the creator typed.
    assert [e.payload["turn_id"] for e in _events(thread_id, "approval_requested")] == [
        typed[0].turn_id
    ]
    assert _live_cards(thread_id) == [typed[0].turn_id]
    assert planned_for == [GREEN5_CHIP, GREEN5_PROMPT]
    assert _reply_language(thread_id) == "en"


# -- an addition: the stopped message's asks are kept --------------------------------------

ADDITION_A = "Captions small, white, lowercase, in the middle."
ADDITION_B = "And no music."
PRIOR_BRIEF = CreativeBrief(
    version=1,
    requirements=[
        BriefRequirement(
            id="r1",
            kind="style",
            scope="global",
            description="Make the whole video like a green screen",
            facts={"speaker_background": True},
        )
    ],
)


async def _what_the_brief_reader_sees(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    message: str,
    turn_id: str,
    prior: CreativeBrief | None,
) -> str:
    """Plan the promoted turn through the real planner; return the request its brief reader got."""
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        source_sequence = _user_message(thread_id, turn_id).sequence
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    manifest = _MANIFEST.model_copy(update={"item_id": str(item_id)})
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=prior))
    seen: list[str] = []

    async def _creator(inputs, **_kw):  # noqa: ANN001, ANN202
        seen.append(str(inputs.agent_input.creator_request or inputs.agent_input.user_message))
        return SimpleNamespace(action=AskUser(**_ASK), brief_updates=[])

    async def _extractor(_inputs, *, creator_request, **_kw):  # noqa: ANN001, ANN202
        seen.append(str(creator_request))
        return BriefExtractionOutput(brief_updates=[])

    monkeypatch.setattr(planner, "_call_main_creator", _creator)
    monkeypatch.setattr(planner, "_call_brief_extractor", _extractor)
    # As the worker does: the turn answers its own message and everything before it.
    token = planner.answering_through_sequence.set(source_sequence)
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await planner.plan_live_turn(
                db, thread_id=thread_id, item_id=item_id, creator_id=user_id, user_message=message
            )
    finally:
        planner.answering_through_sequence.reset(token)
        await engine.dispose()
    assert seen, "no brief reader ran"
    return "\n".join(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["first_turn", "prior_brief"])
async def test_steer_now_keeps_the_stopped_messages_asks(monkeypatch, prior: str) -> None:  # noqa: ANN001
    """F5: A ("captions …") is stopped for B ("and no music"); B's plan reads both."""
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, ADDITION_A)
    waiting = await _send(user_id, thread_id, ADDITION_B)
    assert waiting.status == "queued"
    assert not isinstance(await _steer(user_id, thread_id, first.turn_id), RuntimeFailure)

    seen = await _what_the_brief_reader_sees(
        monkeypatch,
        user_id=user_id,
        thread_id=thread_id,
        message=ADDITION_B,
        turn_id=waiting.turn_id,
        prior=PRIOR_BRIEF if prior == "prior_brief" else None,
    )

    assert ADDITION_A in seen
    assert ADDITION_B in seen


@pytest.mark.asyncio
async def test_a_stopped_chip_tap_is_not_read_as_an_ask(monkeypatch) -> None:  # noqa: ANN001
    """F15: the chip's stock sentence carries no creator words; only the typed request is read."""
    user_id, thread_id, _ = _seed_runtime_project()
    chip = await _send(user_id, thread_id, GREEN5_CHIP)
    typed = await _send(user_id, thread_id, ADDITION_A)
    assert not isinstance(await _steer(user_id, thread_id, chip.turn_id), RuntimeFailure)

    seen = await _what_the_brief_reader_sees(
        monkeypatch,
        user_id=user_id,
        thread_id=thread_id,
        message=ADDITION_A,
        turn_id=typed.turn_id,
        prior=PRIOR_BRIEF,
    )

    assert ADDITION_A in seen
    assert GREEN5_CHIP not in seen


# -- Steer now while the card is being written ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["stop_first", "card_first"])
async def test_steer_now_while_the_card_is_being_written(monkeypatch, order: str) -> None:  # noqa: ANN001
    """F1: the tap lands just before, or just after, the turn's card commits."""
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    waiting: list[TurnAccepted] = []
    stops: list = []

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        if not waiting:
            waiting.append(await _send(user_id, thread_id, "and use a calmer song"))
        return _card_plan()

    complete = rt._complete_draft_turn  # noqa: SLF001

    def _tap_around_the_card(turn_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if order == "stop_first" and str(turn_id) == first.turn_id:
            stops.append(asyncio.run(_steer(user_id, thread_id, first.turn_id)))
            return complete(turn_id, **kwargs)
        result = complete(turn_id, **kwargs)
        if order == "card_first" and str(turn_id) == first.turn_id:
            stops.append(asyncio.run(_steer(user_id, thread_id, first.turn_id)))
        return result

    monkeypatch.setattr(rt, "_plan_with_live_agent", _planner)
    monkeypatch.setattr(rt, "_complete_draft_turn", _tap_around_the_card)
    await _run_turn(first.turn_id)

    assert not isinstance(stops[0], RuntimeFailure), stops[0]
    assert _status(first.turn_id) == "cancelled"
    # A card the stop closed must also be closed in the chat (no live buttons left on it).
    for card in _events(thread_id, "approval_requested"):
        assert _card_closed_in_chat(thread_id, card.payload["approval_id"])
    assert _events(thread_id, "status_update")[-1].content == STOP_LINE_EN
    assert _status(waiting[0].turn_id) == "pending"

    await _run_turn(waiting[0].turn_id)

    assert _live_cards(thread_id) == [waiting[0].turn_id]


# -- Steer now and Approve at the same moment ----------------------------------------------


@pytest.mark.asyncio
async def test_steer_now_after_approve_is_refused_and_the_video_runs(monkeypatch) -> None:  # noqa: ANN001
    """F2, F16: Approve commits first; the Steer tap lands before the render has started.

    Approving does not change the turn's status, so a plain cancel still finds it
    `awaiting_approval`, stops it and starts the waiting message, and the render claim then sets
    the stopped turn to `executing` anyway: two turns running, and a chat that says "stopped".
    """
    ctx = await _card_showing_with_a_waiting_message(monkeypatch)
    approved = await _approve(ctx.user_id, ctx.thread_id, ctx.approval_id)
    assert not isinstance(approved, RuntimeFailure), approved

    stop = await _steer(ctx.user_id, ctx.thread_id, ctx.first.turn_id)

    assert isinstance(stop, RuntimeFailure)
    assert (stop.status_code, stop.code) == (409, "turn_not_cancellable")
    assert _status(ctx.waiting.turn_id) == "queued"
    assert _events(ctx.thread_id, "status_update") == []

    rendered = await _start_render(monkeypatch, ctx.user_id, ctx.thread_id, ctx.approval_id)

    assert rendered["status"] == "dispatched"
    assert _running(ctx.thread_id) == [ctx.first.turn_id]
    assert _status(ctx.waiting.turn_id) == "queued"  # runs after the video


@pytest.mark.asyncio
async def test_steer_now_first_makes_approve_refuse(monkeypatch) -> None:  # noqa: ANN001
    """F2: the Steer tap commits first; the Approve tap then finds the card closed."""
    ctx = await _card_showing_with_a_waiting_message(monkeypatch)

    stop = await _steer(ctx.user_id, ctx.thread_id, ctx.first.turn_id)
    approved = await _approve(ctx.user_id, ctx.thread_id, ctx.approval_id)

    assert not isinstance(stop, RuntimeFailure), stop
    assert isinstance(approved, RuntimeFailure)
    assert (approved.status_code, approved.code) == (409, "approval_not_pending")
    assert _card_closed_in_chat(ctx.thread_id, str(ctx.approval_id))
    assert _running(ctx.thread_id) == [ctx.waiting.turn_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("round_", range(4))
async def test_steer_and_approve_at_the_same_moment_never_both_win(
    monkeypatch, round_: int
) -> None:  # noqa: ANN001, ARG001
    """F2: both taps race on separate connections. One wins; no deadlock, no 500."""
    ctx = await _card_showing_with_a_waiting_message(monkeypatch)

    taps = [
        _steer(ctx.user_id, ctx.thread_id, ctx.first.turn_id),
        _approve(ctx.user_id, ctx.thread_id, ctx.approval_id),
    ]
    if round_ % 2:  # odd rounds start the Approve tap first
        taps.reverse()
    answers = await asyncio.wait_for(asyncio.gather(*taps), timeout=30)
    stop, approved = answers if round_ % 2 == 0 else answers[::-1]
    rendered = await _start_render(monkeypatch, ctx.user_id, ctx.thread_id, ctx.approval_id)

    stopped = not isinstance(stop, RuntimeFailure)
    approved_ok = not isinstance(approved, RuntimeFailure)
    assert stopped != approved_ok, (stop, approved)
    if approved_ok:
        # Read before the approval committed, the Steer's revision is stale (the app refreshes
        # and retries once, then gets turn_not_cancellable); read after, it is too late.
        assert stop.status_code == 409
        assert stop.code in {"turn_not_cancellable", "thread_revision_stale"}
        assert rendered["status"] == "dispatched"
        assert _running(ctx.thread_id) == [ctx.first.turn_id]
        assert _status(ctx.waiting.turn_id) == "queued"
    else:
        # Read before the stop committed, the card's revision is stale; read after, it is closed.
        assert approved.status_code == 409
        assert approved.code in {"approval_not_pending", "thread_revision_stale"}
        assert rendered["status"] != "dispatched"
        assert _running(ctx.thread_id) == [ctx.waiting.turn_id]


@pytest.mark.asyncio
async def test_steer_now_never_stops_an_approved_video(monkeypatch) -> None:  # noqa: ANN001
    """F16: once the render has started, Steer now is refused and the video keeps going."""
    ctx = await _card_showing_with_a_waiting_message(monkeypatch)
    assert not isinstance(
        await _approve(ctx.user_id, ctx.thread_id, ctx.approval_id), RuntimeFailure
    )
    assert (await _start_render(monkeypatch, ctx.user_id, ctx.thread_id, ctx.approval_id))[
        "status"
    ] == "dispatched"

    stop = await _steer(ctx.user_id, ctx.thread_id, ctx.first.turn_id)

    assert isinstance(stop, RuntimeFailure)
    assert (stop.status_code, stop.code) == (409, "turn_not_cancellable")
    assert _status(ctx.first.turn_id) in {"executing", "observing"}
    assert _status(ctx.waiting.turn_id) == "queued"


# -- taps that must not stop the wrong thing -----------------------------------------------


@pytest.mark.asyncio
async def test_steer_now_with_nothing_waiting_is_refused() -> None:
    """F7: no message waits behind the turn, so there is nothing to switch to."""
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")

    stop = await _steer(user_id, thread_id, first.turn_id)

    assert isinstance(stop, RuntimeFailure)
    assert (stop.status_code, stop.code) == (409, "nothing_to_steer_to")
    assert _status(first.turn_id) == "pending"


@pytest.mark.asyncio
async def test_a_stale_or_double_steer_never_stops_the_waiting_message(monkeypatch) -> None:  # noqa: ANN001
    """F6, F9: a second tap, or a tap on a turn that already finished, leaves B running."""
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    waiting = await _send(user_id, thread_id, "and use a calmer song")
    assert not isinstance(await _steer(user_id, thread_id, first.turn_id), RuntimeFailure)

    again = await _steer(user_id, thread_id, first.turn_id)  # a double tap
    assert _status(waiting.turn_id) == "pending"
    if isinstance(again, RuntimeFailure):
        assert again.status_code == 409

    # A turn that answered on its own, with a message promoted behind it.
    later_user, later_thread, _ = _seed_runtime_project()
    answered = await _send(later_user, later_thread, "make the opening shorter")
    behind: list[TurnAccepted] = []

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        if not behind:
            behind.append(await _send(later_user, later_thread, "and use a calmer song"))
        return _reply_plan("Which clip should open the video?")

    monkeypatch.setattr(rt, "_plan_with_live_agent", _planner)
    await _run_turn(answered.turn_id)
    assert _status(behind[0].turn_id) == "pending"

    stale = await _steer(later_user, later_thread, answered.turn_id)

    assert isinstance(stale, RuntimeFailure)
    assert stale.status_code == 409
    assert _status(behind[0].turn_id) == "pending"


@pytest.mark.asyncio
async def test_a_third_message_waits_for_the_slot() -> None:
    """F3: one message can wait. A third is refused until Steer now (or the turn) frees the slot."""
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    waiting = await _send(user_id, thread_id, "and use a calmer song")

    with pytest.raises(RuntimeFailure) as refused:
        await _send(user_id, thread_id, "also trim the ending")
    assert (refused.value.status_code, refused.value.code) == (409, "queued_successor_exists")

    assert not isinstance(await _steer(user_id, thread_id, first.turn_id), RuntimeFailure)
    third = await _send(user_id, thread_id, "also trim the ending")

    assert third.status == "queued"
    assert _user_message(thread_id, third.turn_id).payload["queued_behind_turn_id"] == (
        waiting.turn_id
    )


@pytest.mark.asyncio
async def test_the_stop_line_follows_a_turkish_chat() -> None:
    """F10: a Turkish chat gets the Turkish line."""
    user_id, thread_id, _ = _seed_runtime_project()
    _set_language(thread_id, "tr")
    first = await _send(user_id, thread_id, "girişi hızlandır", locale="tr-TR")
    await _send(user_id, thread_id, "ve daha sakin bir şarkı kullan", locale="tr-TR")

    assert not isinstance(await _steer(user_id, thread_id, first.turn_id), RuntimeFailure)

    assert _events(thread_id, "status_update")[-1].content == STOP_LINE_TR


# -- switch off ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_is_byte_identical(monkeypatch) -> None:  # noqa: ANN001
    """F12: off, a Steer is refused outright and everything else is today's behaviour."""
    _switch(monkeypatch, False)
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    waiting = await _send(user_id, thread_id, "and use a calmer song")

    assert "queued_behind_turn_id" not in _user_message(thread_id, waiting.turn_id).payload
    refused = await _steer(user_id, thread_id, first.turn_id)
    assert isinstance(refused, RuntimeFailure)
    assert (refused.status_code, refused.code) == (404, "steer_now_unavailable")
    assert _status(first.turn_id) == "pending"

    # The plain cancel (the web's "Cancel request") is unchanged.
    plain = await _steer(user_id, thread_id, waiting.turn_id, steer=False)
    assert not isinstance(plain, RuntimeFailure)
    stop = _events(thread_id, "turn_cancelled")[-1]
    assert (stop.content, set(stop.payload)) == (None, {"turn_id", "approval_ids"})
    assert _events(thread_id, "status_update") == []
    assert _events(thread_id, "approval_cancelled") == []


@pytest.mark.asyncio
async def test_the_capability_follows_the_switch(monkeypatch) -> None:  # noqa: ANN001
    """The app shows Steer now only when the server says so (global switch or allowlist)."""
    from app.routes.creation_threads import capabilities  # noqa: PLC0415

    user = SimpleNamespace(id=uuid.uuid4())
    _switch(monkeypatch, False)
    assert (await capabilities(user))["steer_now"] is False
    monkeypatch.setattr(settings, "kria_steer_now_user_ids", [str(user.id)])
    assert (await capabilities(user))["steer_now"] is True
    _switch(monkeypatch, True)
    assert (await capabilities(user))["steer_now"] is True
