"""KRI-552: an identical re-send of a message whose turn is running (or just finished).

Prod thread ddc6e71b (stress kit N3, 2026-10-08): the creator sent the marathon prompt at
14:59:00 (seq 7, turn a0913b2e), the chat looked stuck, and they sent the identical text
again at 15:00:09 (seq 9, turn 20a1f6c3). What followed:

- the second message bumped the thread revision while turn 1 was planning, so turn 1 was
  thrown back ("requeued") and planned twice more;
- turn 1's draft was bound with no brief, so its render-ready review had zero receipts;
- the second copy sat `queued`, then re-planned the whole brief after the render finished and
  asked the creator to restate things the render had already done (seq 19).

These tests replay that shape against Postgres. The duplicate is answered in one line and
never becomes a turn that plans; the first turn is never disturbed.

Failure modes pinned here:
- the duplicate still becomes a queued/pending turn (a second plan, a second question);
- the duplicate's events still invalidate the first turn's revision fence (a requeue) or
  take the brief the first turn was about to bind;
- a legitimate retry is swallowed (failed turn, a question that needs an answer, a different
  message, a message that answers a tapped question, a re-send long after the render);
- the reply is English-only, or the "finished" reply drops the receipts the first turn showed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.brief_binding import BriefBinding
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
)
from app.tasks.kria_runtime import run_kria_turn

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

# The prompt of prod seq 7 and seq 9, word for word.
_MESSAGE = (
    "My first marathon story with my voiceover. Cut my long pauses and the part where I restart "
    'the kilometer thirty sentence. When I say "the medal", show my medal photo. When I say '
    '"four hours and twelve minutes", show the watch photo. Keep the crowd noise quiet under my '
    "voice. Clean captions."
)
_TURKISH = "Maraton hikâyem, kendi sesimle. Uzun duraklamaları kes. Altyazılar temiz olsun."
_SUMMARY = "I'll trim your pauses and show the medal and watch photos on your words."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _planned() -> PlannedKriaTurn:
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="original_audio",
                pacing="balanced",
                render_program="guided",
                selected_media_ids=[],
                rationale="Pauses trimmed, photos on the words.",
            ),
            summary=_SUMMARY,
        )
    )
    return PlannedKriaTurn(
        plan=plan,
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=(
            BriefUpdate(
                kind="audio",
                scope="global",
                description="Cut my long pauses and the part where I restart the sentence",
            ),
        ),
        brief_route="replan",
        brief_clip_ids=("clip-1",),
    )


def _set_language(thread_id: uuid.UUID, language: str) -> None:
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**(thread.state or {}), "reply_language": language}
        db.commit()


async def _send(  # noqa: ANN202
    user_id,  # noqa: ANN001
    thread_id,  # noqa: ANN001
    message,  # noqa: ANN001
    *,
    client_event_id=None,  # noqa: ANN001
    session=None,  # noqa: ANN001
    revision=None,  # noqa: ANN001
):
    """One `submit_turn` call the way the iPhone makes it (fresh revision each time)."""

    async def _call(db):  # noqa: ANN001, ANN202
        seen = revision
        if seen is None:
            thread = await db.get(CreationThread, thread_id)
            seen = int(thread.revision)
            await db.rollback()
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=client_event_id or f"kri552-{uuid.uuid4().hex}",
                expected_thread_revision=seen,
            ),
        )
        return accepted

    if session is not None:
        return await _call(session)
    async with AsyncSessionLocal() as db:
        return await _call(db)


async def _send_from_the_worker_loop(user_id, thread_id, message):  # noqa: ANN001, ANN202
    """`_send`, from inside the planner stub: the worker's event loop owns no pooled engine."""
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            return await _send(user_id, thread_id, message, session=db)
    finally:
        await engine.dispose()


def _turns(thread_id: uuid.UUID) -> list[CreatorAgentTurn]:
    with sync_session() as db:
        return list(
            db.execute(
                select(CreatorAgentTurn)
                .where(CreatorAgentTurn.thread_id == thread_id)
                .order_by(CreatorAgentTurn.created_at, CreatorAgentTurn.id)
            ).scalars()
        )


def _duplicate_answers(thread_id: uuid.UUID, of: uuid.UUID) -> list[CreationThreadEvent]:
    with sync_session() as db:
        events = db.execute(
            select(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "assistant",
            )
            .order_by(CreationThreadEvent.sequence)
        ).scalars()
        return [e for e in events if (e.payload or {}).get("duplicate_of") == str(of)]


def _append(db, thread, *, role, event_type, content, payload, client_event_id=None):  # noqa: ANN001, ANN202
    """One transcript event, numbered the way the runtime numbers them."""
    sequence = (
        int(
            db.execute(
                select(func.coalesce(func.max(CreationThreadEvent.sequence), -1)).where(
                    CreationThreadEvent.thread_id == thread.id
                )
            ).scalar_one()
        )
        + 1
    )
    thread.revision = int(thread.revision) + 1
    event = CreationThreadEvent(
        thread_id=thread.id,
        sequence=sequence,
        revision=thread.revision,
        role=role,
        event_type=event_type,
        content=content,
        payload=payload,
        client_event_id=client_event_id,
    )
    db.add(event)
    db.flush()
    return event


def _seed_prior_turn(
    thread_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    status: str,
    mode: str = "act",
    completed_ago: timedelta | None = None,
    observed: tuple[str, str] | None = ("assistant_review", "The narrated cut is ready."),
    user_payload: dict | None = None,
) -> uuid.UUID:
    """A turn the creator already got an answer to, as the worker leaves it."""
    turn_id = uuid.uuid4()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        source = _append(
            db,
            thread,
            role="user",
            event_type="user_message",
            content=message,
            payload={
                "request_digest": "x",
                "runtime_version": 2,
                "turn_id": str(turn_id),
                "turn_status": status,
                **(user_payload or {}),
            },
            client_event_id=f"seed-{turn_id.hex}",
        )
        observed_id = None
        if observed is not None:
            reply = _append(
                db,
                thread,
                role="assistant",
                event_type=observed[0],
                content=observed[1],
                payload={"turn_id": str(turn_id), "turn_value": "review", "receipt_ids": []},
            )
            observed_id = reply.id
        db.add(
            CreatorAgentTurn(
                id=turn_id,
                thread_id=thread_id,
                session_id=session_id,
                source_event_id=source.id,
                client_event_id=source.client_event_id,
                request_digest="x",
                status=status,
                plan_json={"mode": mode, "intents": []},
                observed_event_id=observed_id,
                completed_at=(datetime.now(UTC) - completed_ago) if completed_ago else None,
            )
        )
        db.commit()
    return turn_id


# --- the prod incident ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resend_while_the_first_turn_plans_is_answered_and_leaves_the_first_turn_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prod seq 7 + seq 9: the first turn must not requeue, keeps its brief, and the copy
    never becomes a turn of its own."""
    user_id, thread_id, _session = _seed_runtime_project()
    calls: list[int] = []

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(1)
        if len(calls) == 1:
            # 69 s in, the creator re-sends the identical text (seq 9).
            await _send_from_the_worker_loop(user_id, thread_id, _MESSAGE)
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    first = await _send(user_id, thread_id, _MESSAGE)

    result = await asyncio.to_thread(run_kria_turn.run, first.turn_id)

    # (d) the copy never disturbs the first turn: no requeue, one planning pass.
    assert result == {"turn_id": first.turn_id, "status": "awaiting_approval"}
    assert len(calls) == 1
    turns = _turns(thread_id)
    assert [t.status for t in turns] == ["awaiting_approval", "completed"]
    # (a) the copy is answered in place, never planned: no plan, no question.
    copy_turn = turns[1]
    assert copy_turn.plan_json["mode"] == "respond"
    assert copy_turn.plan_json["turn_value"] == "progress"
    answers = _duplicate_answers(thread_id, turns[0].id)
    assert len(answers) == 1
    assert "same message" in answers[0].content
    assert "couldn't safely verify" not in answers[0].content
    # (d) the brief and the binding belong to the first turn.
    with sync_session() as db:
        versions = list(
            db.execute(
                select(CreativeBriefVersion).where(CreativeBriefVersion.thread_id == thread_id)
            ).scalars()
        )
        assert [v.source_turn_id for v in versions] == [turns[0].id]
        item_id = db.get(CreatorAgentSession, _session).plan_item_id
        head = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True)
            )
        ).scalar_one()
        binding = BriefBinding.model_validate(head.snapshot_json["brief_binding"])
        assert binding.state == "pinned"
        assert binding.brief is not None and binding.brief.version == 1


@pytest.mark.asyncio
async def test_the_copy_does_not_leave_a_queued_turn_that_blocks_the_next_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the fix the copy sat `queued`; the next real message then got a 409."""
    user_id, thread_id, _session = _seed_runtime_project()

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    first = await _send(user_id, thread_id, _MESSAGE)
    copy = await _send(user_id, thread_id, _MESSAGE)

    assert copy.status == "completed"
    assert [t.status for t in _turns(thread_id)] == ["pending", "completed"]
    # A different message right after is accepted, not refused as a waiting follow-up.
    other = await _send(user_id, thread_id, "Also make the title bigger please")
    assert other.status == "queued"
    assert first.turn_id != other.turn_id


@pytest.mark.asyncio
async def test_a_resend_while_the_draft_waits_for_approval_points_at_the_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _session = _seed_runtime_project()

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    first = await _send(user_id, thread_id, _MESSAGE)
    result = await asyncio.to_thread(run_kria_turn.run, first.turn_id)
    assert result["status"] == "awaiting_approval"

    copy = await _send(user_id, thread_id, f"  {_MESSAGE}\n")  # same words, stray whitespace

    assert copy.status == "completed"
    answers = _duplicate_answers(thread_id, uuid.UUID(first.turn_id))
    assert len(answers) == 1
    assert "approval" in answers[0].content
    assert [t.status for t in _turns(thread_id)] == ["awaiting_approval", "completed"]


@pytest.mark.asyncio
async def test_the_copy_is_answered_in_turkish_in_a_turkish_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _session = _seed_runtime_project()
    _set_language(thread_id, "tr")

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    first = await _send(user_id, thread_id, _TURKISH)
    await _send(user_id, thread_id, _TURKISH)

    answers = _duplicate_answers(thread_id, uuid.UUID(first.turn_id))
    assert len(answers) == 1
    assert "aynısı" in answers[0].content
    assert "same message" not in answers[0].content


@pytest.mark.asyncio
async def test_a_resend_after_the_render_finished_answers_from_the_review_it_already_showed() -> (
    None
):
    user_id, thread_id, session_id = _seed_runtime_project()
    review = (
        "The narrated cut is ready.\nDone: photos on 'the medal' and 'four hours and twelve "
        "minutes'.\nCouldn't: cut the restarted sentence."
    )
    first = _seed_prior_turn(
        thread_id,
        session_id,
        _MESSAGE,
        status="completed",
        completed_ago=timedelta(seconds=30),
        observed=("assistant_review", review),
    )

    copy = await _send(user_id, thread_id, _MESSAGE)

    assert copy.status == "completed"
    answers = _duplicate_answers(thread_id, first)
    assert len(answers) == 1
    # The receipts the creator already saw come back; nothing is planned or asked again.
    assert review in answers[0].content
    assert [t.status for t in _turns(thread_id)] == ["completed", "completed"]
    assert _turns(thread_id)[1].plan_json["mode"] == "respond"


@pytest.mark.asyncio
async def test_the_same_client_event_id_replays_the_copy_answer_once() -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_prior_turn(thread_id, session_id, _MESSAGE, status="planning", mode="act", observed=None)
    cid = f"kri552-replay-{uuid.uuid4().hex}"
    with sync_session() as db:
        seen = int(db.get(CreationThread, thread_id).revision)  # a retry re-sends the same body

    first = await _send(user_id, thread_id, _MESSAGE, client_event_id=cid, revision=seen)
    again = await _send(user_id, thread_id, _MESSAGE, client_event_id=cid, revision=seen)

    assert first.status == "completed"
    assert again.replayed is True
    assert again.turn_id == first.turn_id
    assert len(_turns(thread_id)) == 2


@pytest.mark.asyncio
async def test_a_different_message_sent_mid_planning_still_throws_the_first_turn_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fence only forgives copies: a real follow-up still makes the turn plan again."""
    user_id, thread_id, _session = _seed_runtime_project()
    calls: list[int] = []

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(1)
        if len(calls) == 1:
            await _send_from_the_worker_loop(user_id, thread_id, "Actually make it 20 seconds")
        return _planned()

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    first = await _send(user_id, thread_id, _MESSAGE)

    result = await asyncio.to_thread(run_kria_turn.run, first.turn_id)

    assert result == {"turn_id": first.turn_id, "status": "requeued"}
    assert [t.status for t in _turns(thread_id)] == ["pending", "queued"]


@pytest.mark.asyncio
async def test_planners_and_the_approval_pin_read_a_copied_message_once() -> None:
    """Read twice, a re-sent ask doubled every instruction the clip planner had to verify."""
    from app.kria.planner import _load_raw_creator_request
    from app.tasks.kria_runtime import _full_creator_request_sync

    user_id, thread_id, _session = _seed_runtime_project()
    await _send(user_id, thread_id, _MESSAGE)
    await _send(user_id, thread_id, _MESSAGE)  # the copy: answered, never planned

    with sync_session() as db:
        pinned = _full_creator_request_sync(db, thread_id=thread_id, through_sequence=99)
    async with AsyncSessionLocal() as db:
        raw = await _load_raw_creator_request(db, thread_id=thread_id, user_message=_MESSAGE)

    assert pinned == _MESSAGE
    assert raw == _MESSAGE


@pytest.mark.asyncio
async def test_the_kill_switch_restores_the_old_queueing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_duplicate_resend_enabled", False)
    user_id, thread_id, _session = _seed_runtime_project()
    await _send(user_id, thread_id, _MESSAGE)

    copy = await _send(user_id, thread_id, _MESSAGE)

    assert copy.status == "queued"
    assert [t.status for t in _turns(thread_id)] == ["pending", "queued"]


@pytest.mark.parametrize(
    ("events", "title_marker", "expected"),
    [
        (["copy", "copy-answer"], None, True),
        (["title", "copy", "copy-answer"], "proven", True),
        # A title the thread cannot prove, or any real message in the gap, is a real change.
        (["title", "copy", "copy-answer"], "wrong", False),
        (["copy", "copy-answer", "user"], None, False),
        (["user"], None, False),
        ([], None, True),  # nothing happened since the claim
    ],
    ids=["copy", "title+copy", "unproven-title", "copy+real-message", "real-message", "nothing"],
)
def test_the_revision_fence_forgives_only_copies_and_a_proven_title(
    events: list[str], title_marker: str | None, expected: bool
) -> None:
    from app.services.creation_thread_titles import conversation_revision_matches_sync

    _user_id, thread_id, _session = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        claimed = int(thread.revision)
        for kind in events:
            copy = {"duplicate_of": str(uuid.uuid4())}
            role, event_type, payload = {
                "title": ("system", "thread_title_generated", {"title": "T"}),
                "copy": ("user", "user_message", {"inert": True, **copy}),
                "copy-answer": ("assistant", "assistant_response", copy),
                "user": ("user", "user_message", {"turn_id": str(uuid.uuid4())}),
            }[kind]
            event = _append(
                db, thread, role=role, event_type=event_type, content="x", payload=payload
            )
            if kind == "title":
                proven = int(event.revision)
                thread.state = {
                    **(thread.state or {}),
                    "title_generated_revision": proven if title_marker == "proven" else proven - 1,
                }
        db.flush()
        assert conversation_revision_matches_sync(db, thread, claimed) is expected


# --- a retry is never swallowed ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior",
    [
        # The first try failed: the identical text is the retry the app asks for.
        {"status": "failed", "completed_ago": timedelta(seconds=10), "observed": None},
        # Kria answered with a question or a recovery: re-sending is how the creator retries.
        {
            "status": "completed",
            "mode": "respond",
            "completed_ago": timedelta(seconds=10),
            "observed": ("assistant_response", "Your clips changed while I was planning."),
        },
        # A finished draft that never produced a review (e.g. a refused or failed render).
        {
            "status": "completed",
            "completed_ago": timedelta(seconds=10),
            "observed": ("assistant_response", "The render failed. Try again."),
        },
        # Minutes later the creator is asking again, not impatiently re-sending.
        {
            "status": "completed",
            "completed_ago": timedelta(minutes=10),
            "observed": ("assistant_review", "The narrated cut is ready."),
        },
        # The earlier message answered a tapped question; its words are not "the request".
        {
            "status": "completed",
            "completed_ago": timedelta(seconds=10),
            "observed": ("assistant_review", "The narrated cut is ready."),
            "user_payload": {"choice_selection": {"question_id": "q", "option_id": "o"}},
        },
    ],
    ids=["failed", "question", "no-review", "long-after", "was-an-answer"],
)
async def test_a_retry_of_an_identical_message_is_planned_normally(prior: dict) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_prior_turn(thread_id, session_id, _MESSAGE, **prior)

    accepted = await _send(user_id, thread_id, _MESSAGE)

    assert accepted.status == "pending"
    assert _turns(thread_id)[-1].status == "pending"
    assert _duplicate_answers(thread_id, _turns(thread_id)[0].id) == []


@pytest.mark.asyncio
async def test_a_different_message_is_never_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_prior_turn(
        thread_id, session_id, _MESSAGE, status="completed", completed_ago=timedelta(seconds=5)
    )

    accepted = await _send(user_id, thread_id, _MESSAGE + " Make the title bigger.")

    assert accepted.status == "pending"
