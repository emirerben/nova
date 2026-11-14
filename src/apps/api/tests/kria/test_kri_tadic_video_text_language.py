"""Thread aa36d140 ("Tadic"): pasted lyrics moved an English chat to Turkish for good.

Kria asked in plain text for the lines to put on the video; the creator pasted Turkish lyrics
with no label, the chat switched to Turkish, and "Do it" / "Yes" were too short to switch back.

Failure modes covered here, through the real `submit_turn`:
1. A typed "paste the lines" ask (no options) leaves the paste counting as chat language.
2. The same ask with options (an `ask_user:<reason>` card) is not read as a wording question.
3. An ask about how the text looks is read as a wording ask and holds a real switch.
4. The ask stays open after Kria's next reply and holds a later real switch.
5. After a switch made only by pasted video text, a short reply in the earlier language
   ("Do it") cannot take the chat back.
6. After a real-sentence switch, a short earlier-language reply flips the chat back.
7. A short reply in the new language ("Yap") restores the earlier one.
8. "make it pop" in a Turkish chat with no switch behind it turns the chat English.
9. Once restored, a later short reply flips the chat again.
10. After a real-sentence switch, pasting the lyrics (now the video's text) makes a later short
    reply in the earlier language ("make it pop", "Do it") flip the chat back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents._schemas.creator_agent import AskUser
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria import planner
from app.kria.contracts import KriaTurnPlan
from app.models import CreationThread, CreationThreadEvent, CreativeBriefVersion, CreatorAgentTurn

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_kri520_reply_language_postgres import (  # noqa: E402
    _send,
    _state_and_last_reply,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

REQUEST = "Add the lyrics of this song as text on the video"
LYRICS = (
    "Sarı lacivert gönlümüzde\nHer maçta yanındayız\nŞarkımız göğe yükselsin\n"
    "Kadıköy'de bir gece daha\nSensiz geçmez günlerimiz"
)
REFUSAL = "Your clips are about 14 seconds, so the video can't be 14.005 seconds. Simpler version?"


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


@pytest.fixture(autouse=True)
async def _fresh_async_pool():  # noqa: ANN202
    yield
    await async_engine.dispose()


def _lyrics_ask(reason_code: str = "lyrics_text_needed", options: list[str] | None = None):
    return planner.adapt_creator_action(
        AskUser(
            kind="ask_user",
            question="Could you paste the lines you want on the video?",
            reason_code=reason_code,
            options=options or [],
        )
    )


def _reply(thread_id: uuid.UUID, plan: KriaTurnPlan | None = None, *, legacy: bool = False):
    """Finish the pending turn the way the worker does: one reply event, the plan kept."""
    plan = plan or KriaTurnPlan(mode="respond", turn_value="question", response=REFUSAL)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        turn = db.execute(
            select(CreatorAgentTurn)
            .where(CreatorAgentTurn.thread_id == thread_id, CreatorAgentTurn.status == "pending")
            .order_by(CreatorAgentTurn.created_at.desc())
            .limit(1)
        ).scalar_one()
        sequence = db.execute(
            select(func.max(CreationThreadEvent.sequence)).where(
                CreationThreadEvent.thread_id == thread_id
            )
        ).scalar_one()
        thread.revision = int(thread.revision) + 1
        event = CreationThreadEvent(
            thread_id=thread_id,
            sequence=int(sequence) + 1,
            revision=int(thread.revision),
            role="assistant",
            event_type="assistant_response",
            content=plan.response,
            payload={
                "turn_id": str(turn.id),
                "turn_value": plan.turn_value,
                "schema_version": plan.schema_version,
                **({"choice_question": plan.choice_question} if plan.choice_question else {}),
            },
        )
        db.add(event)
        db.flush()
        dumped = plan.model_dump(mode="json")
        if legacy:  # a reply saved before the ask was tagged
            dumped.pop("asks_video_text", None)
        turn.plan_json = dumped
        turn.observed_event_id = event.id
        turn.status = "completed"
        turn.completed_at = datetime.now(UTC)
        db.commit()


def _brief_holds(thread_id: uuid.UUID, literal: str) -> None:
    """The planner took the pasted words as the video's text."""
    with sync_session() as db:
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                requirements=[{"id": "r1", "kind": "text", "scope": "global", "literal": literal}],
            )
        )
        db.commit()


def _language(thread_id: uuid.UUID) -> str:
    return _state_and_last_reply(thread_id)[0]["reply_language"]


async def _turn(user_id, thread_id, message, plan=None, **kwargs) -> str:  # noqa: ANN001
    await _send(user_id, thread_id, message)
    _reply(thread_id, plan, **kwargs)
    return _language(thread_id)


@pytest.mark.asyncio
async def test_the_reported_thread_stays_english() -> None:
    user_id, thread_id, _ = _seed_runtime_project()

    assert await _turn(user_id, thread_id, REQUEST, _lyrics_ask()) == "en"
    assert await _turn(user_id, thread_id, LYRICS) == "en"
    for answer in ("Yap", "Do it", "Yes", "Evet", "Yap"):
        assert await _turn(user_id, thread_id, answer) == "en", answer


@pytest.mark.asyncio
async def test_a_lyrics_ask_with_options_holds_the_chat_too() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    ask = _lyrics_ask(options=["I'll paste them", "Skip the lyrics"])
    assert ask.choice_question["conflict"] == "ask_user:lyrics_text_needed"

    await _turn(user_id, thread_id, REQUEST, ask)
    await _send(user_id, thread_id, LYRICS)

    assert _language(thread_id) == "en"


@pytest.mark.asyncio
async def test_an_ask_about_how_the_text_looks_does_not_hold_a_real_switch() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _turn(user_id, thread_id, REQUEST, _lyrics_ask("text_position"))

    await _send(user_id, thread_id, "Yazıyı videonun en altına koyar mısın lütfen")

    assert _language(thread_id) == "tr"


@pytest.mark.asyncio
async def test_the_ask_closes_with_kria_s_next_reply() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _turn(user_id, thread_id, REQUEST, _lyrics_ask())
    await _turn(user_id, thread_id, LYRICS)

    # A real Turkish request after Kria answered is the creator switching.
    await _send(user_id, thread_id, "Fenerbahçe sözleri görünsün lütfen")

    assert _language(thread_id) == "tr"


@pytest.mark.asyncio
async def test_a_short_reply_takes_back_a_chat_only_pasted_lyrics_moved() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _turn(user_id, thread_id, REQUEST)  # an untagged plain-text ask (before the fix)
    assert await _turn(user_id, thread_id, LYRICS, legacy=True) == "tr"
    _brief_holds(thread_id, LYRICS)

    assert await _turn(user_id, thread_id, "Yap") == "tr"  # the new language never restores
    assert await _turn(user_id, thread_id, "Do it") == "en"
    assert "reply_language_before" not in _state_and_last_reply(thread_id)[0]
    assert await _turn(user_id, thread_id, "Evet") == "en"  # no ping-pong after the restore


@pytest.mark.asyncio
async def test_a_real_turkish_sentence_still_switches_for_good() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _turn(user_id, thread_id, REQUEST)
    sentence = "Bundan sonra Türkçe konuşalım, şarkı sözlerini sonra göndereceğim"
    assert await _turn(user_id, thread_id, sentence) == "tr"
    _brief_holds(thread_id, LYRICS)

    assert await _turn(user_id, thread_id, "Do it") == "tr"


@pytest.mark.asyncio
async def test_lyrics_pasted_after_a_real_switch_do_not_undo_it() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _turn(user_id, thread_id, REQUEST)
    sentence = "Bundan sonra Türkçe konuşalım, şarkı sözlerini sonra göndereceğim"
    assert await _turn(user_id, thread_id, sentence) == "tr"
    assert await _turn(user_id, thread_id, LYRICS) == "tr"
    _brief_holds(thread_id, LYRICS)

    assert await _turn(user_id, thread_id, "make it pop") == "tr"
    assert await _turn(user_id, thread_id, "Do it") == "tr"


@pytest.mark.asyncio
async def test_make_it_pop_in_a_turkish_chat_stays_turkish() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    assert await _turn(user_id, thread_id, "Bu videoya şarkı sözlerini ekler misin") == "tr"

    assert await _turn(user_id, thread_id, "make it pop") == "tr"


@pytest.mark.asyncio
async def test_the_kill_switch_keeps_the_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_reply_language_own_words_enabled", False)
    user_id, thread_id, _ = _seed_runtime_project()
    await _turn(user_id, thread_id, REQUEST, _lyrics_ask())
    assert await _turn(user_id, thread_id, LYRICS) == "tr"
    _brief_holds(thread_id, LYRICS)

    assert await _turn(user_id, thread_id, "Do it") == "tr"
