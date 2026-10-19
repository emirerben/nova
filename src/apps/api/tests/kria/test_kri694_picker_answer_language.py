"""KRI-694: the stand-in a model reads for a tapped answer follows the chat's language.

Tigre night N1 (thread 2EA478EC, a Turkish chat): the clip-picker answer reached Main
Creator as "I tapped the clips you asked about." -- an English line, attributed to the
creator, in an otherwise Turkish conversation.

How this could fail, and what pins each:

* The stand-in stays English in a Turkish chat (`model_text` never looked at the language).
* `_claim` computes the turn's latest message BEFORE the task binds the chat language, so
  a contextvar-only fix would fix the history rows but leave `user_message` English
  (the exact line in the trace).
* A Turkish chat's request lists the stand-in twice (the claimed message and the history
  row differ), because the two reads used different languages.
* An English (or unknown-language) chat changes at all -- the English constants stay the
  byte-identical default.
* The Turkish sentence trips a Turkish reader ("klip", "seç" ...) and becomes an invented
  requirement; or carries a digit a model could read as an order / duration.
* The structured answer itself (`clip_selection`, KRI-697 identity binding) is touched:
  only the sentence stands in, the payload stays authoritative.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.reply_language import remember_reply_language, reply_language_for
from app.models import CreationThread, CreationThreadEvent
from app.schemas.edit_proposal import (
    recognize_round_robin_cadence,
    recognize_total_duration_s,
    rejects_round_robin_cadence,
    turkish_media_scope,
    turkish_narrowing_cue,
    turkish_stated_media_counts,
)
from app.services.song_order import (
    CLIP_ANSWER_MESSAGE,
    CLIP_ANSWER_MESSAGE_TR,
    CLIP_SKIP_MESSAGE,
    CLIP_SKIP_MESSAGE_TR,
    SONG_ORDER_ANSWER_MESSAGE,
    SONG_ORDER_ANSWER_MESSAGE_TR,
    SONG_RANGE_ANSWER_MESSAGE,
    SONG_RANGE_ANSWER_MESSAGE_TR,
    model_text,
)
from app.tasks.kria_runtime import _append_sync_event
from tests.kria.test_clip_selection_turns_postgres import (
    QUESTION_ID,
    _answer,
    _ask,
    _submit,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_ANSWERED = {"clip_selection": {"question_id": "q", "answers": []}}
_SKIPPED = {"clip_selection": {"question_id": "q", "skipped": True}}
_SONG_ORDER = {"song_order": {"question_id": "q", "ordered_media_ids": ["a"]}}
_SONG_RANGE = {"song_range": {"question_id": "q"}}

_STAND_INS = [
    (_ANSWERED, CLIP_ANSWER_MESSAGE, CLIP_ANSWER_MESSAGE_TR),
    (_SKIPPED, CLIP_SKIP_MESSAGE, CLIP_SKIP_MESSAGE_TR),
    (_SONG_ORDER, SONG_ORDER_ANSWER_MESSAGE, SONG_ORDER_ANSWER_MESSAGE_TR),
    (_SONG_RANGE, SONG_RANGE_ANSWER_MESSAGE, SONG_RANGE_ANSWER_MESSAGE_TR),
]
_IDS = ["clip-answer", "clip-skip", "song-order", "song-range"]


@pytest.mark.parametrize(("payload", "english", "turkish"), _STAND_INS, ids=_IDS)
def test_stand_in_follows_the_bound_chat_language(payload, english, turkish) -> None:
    echo = "Işıklı ev: clip 9. None of these for Kadıköy"
    with reply_language_for("tr"):
        assert model_text(payload, echo) == turkish
    # English, an unsupported language and "nothing bound" all keep the English constant.
    with reply_language_for("en"):
        assert model_text(payload, echo) == english
    assert model_text(payload, echo) == english
    assert english != turkish


@pytest.mark.parametrize(("payload", "english", "turkish"), _STAND_INS, ids=_IDS)
def test_an_explicit_language_wins_over_the_unbound_default(payload, english, turkish) -> None:
    # `_claim` reads the message before the task binds the chat language.
    assert model_text(payload, "x", language="tr") == turkish
    assert model_text(payload, "x", language="en") == english
    assert model_text(payload, "x", language=None) == english
    with reply_language_for("tr"):
        assert model_text(payload, "x", language="en") == english


def test_typed_words_are_never_replaced_whatever_the_language() -> None:
    for language in ("tr", "en", None):
        assert model_text(None, "make it 20 seconds", language=language) == "make it 20 seconds"
        assert model_text({"choice_selection": {}}, "tamam", language=language) == "tamam"
        assert model_text({"clip_selection": None}, "hayır", language=language) == "hayır"


@pytest.mark.parametrize("turkish", [t for _, _, t in _STAND_INS], ids=_IDS)
def test_turkish_stand_ins_are_neutral_to_every_turkish_reader(turkish: str) -> None:
    # No digit or time a model could turn into an order / duration requirement.
    assert not any(ch.isdigit() for ch in turkish)
    # And no Turkish rule of the edit-proposal readers fires on it as if the creator asked.
    assert turkish_media_scope(turkish) is None
    assert turkish_narrowing_cue(turkish) is False
    assert turkish_stated_media_counts(turkish) == []
    assert recognize_round_robin_cadence(turkish) is None
    assert rejects_round_robin_cadence(turkish) is False
    assert recognize_total_duration_s(turkish) is None


# -- the real claim path (Postgres): the trace line `user_message` ----------------------------


@pytest.fixture
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_flags")
@pytest.mark.parametrize(
    ("language", "typed", "echo", "stand_in"),
    [
        (
            "tr",
            'Başlık: "Atakan\'ı almaya". "Sergio" dediğimde kaptanla iskeledeki klibi göster.',
            "Dodgeball: klip 1, 2",
            CLIP_ANSWER_MESSAGE_TR,
        ),
        (
            "en",
            'Title: "Picking Atakan up". Show the pier clip when I say "Sergio".',
            "Dodgeball: clips 1, 2",
            CLIP_ANSWER_MESSAGE,
        ),
    ],
    ids=["turkish-chat", "english-chat"],
)
async def test_the_claimed_message_and_the_full_request_follow_the_chat_language(
    language: str, typed: str, echo: str, stand_in: str
) -> None:
    from app.kria.planner import _load_raw_creator_request  # noqa: PLC0415
    from app.tasks.kria_runtime import _claim  # noqa: PLC0415

    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db, thread, role="user", event_type="user_message", content=typed, payload={}
        )
        remember_reply_language(thread, language)
        db.commit()
    _ask(thread_id)
    try:
        accepted = await _submit(user_id, thread_id, _answer(), message=echo)
        claimed = _claim(accepted.turn_id, "test-owner")
        assert isinstance(claimed, tuple)
        # The latest message the planner reads (`user_message` in the Main Creator trace).
        assert claimed[1] == stand_in
        # The planner runs with the chat's language bound; the history row must agree with
        # the claimed message so the request lists the stand-in once, not twice.
        with reply_language_for(language):
            async with AsyncSessionLocal() as db:
                request = await _load_raw_creator_request(
                    db, thread_id=thread_id, user_message=claimed[1]
                )
        assert request is not None
        assert request.splitlines() == [typed, stand_in]
        # The structured answer is untouched: stored by identity, never by the sentence.
        with sync_session() as db:
            payloads = (
                db.execute(
                    select(CreationThreadEvent.payload).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.role == "user",
                    )
                )
                .scalars()
                .all()
            )
        stored = [p["clip_selection"] for p in payloads if p and "clip_selection" in p]
        assert [s["question_id"] for s in stored] == [QUESTION_ID]
        assert stored[0]["answers"][0]["media_ids"] == ["m1", "m2"]
        # The creator's transcript still shows what they tapped.
        with sync_session() as db:
            shown = (
                db.execute(
                    select(CreationThreadEvent.content).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.role == "user",
                    )
                )
                .scalars()
                .all()
            )
        assert echo in shown
    finally:
        await async_engine.dispose()
