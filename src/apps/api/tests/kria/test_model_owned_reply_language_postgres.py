"""The model-owned chat language through the real runtime (``submit_turn`` + ``run_kria_turn``).

Failure modes are listed in ``test_model_owned_reply_language.py``. Here only the Main
Creator's provider call is replaced: ``MainCreatorAgent.run`` returns the agent's own ``parse``
of a recorded-shape answer (or a fixed output for the held-draft replay), so the planner's call
site, the worker's rebind after planning, the completion transaction that stores the language
and every reply around them are the server's own. Scratch Postgres, no model call.

The held-draft cases reuse the KRI-968 Don Julio T1 replay (prod's brief, events, take and draft
strategy), whose readiness hold writes server copy after the model call.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents.main_creator import MainCreatorAgent, MainCreatorOutput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ChoiceSelectionIn, SubmitTurnBody
from app.kria.runtime import submit_turn
from app.models import CreationThread, CreationThreadEvent, CreatorAgentTurn
from app.tasks.kria_runtime import run_kria_turn

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_kri968_simpler_version_names_drops_postgres import (  # noqa: E402
    EVENTS,
    TAG,
    _planned,
    _seed_t1,
    _strategy,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

INCIDENT = (
    "Add the following lyrics as text, line by line: Vazgeçmem dünyaları Önüme serseler de "
    "Senin isminde saklı Mutlukuk da keder de Kaderimiz yazılmış Seninle Fenerbahçe"
)
TURKISH_PHONE = "tr-TR,tr;q=0.9"
EN_HELD = "I held this first draft back."
TR_HELD = f"Daha basit sürümde çıkarılacak: {TAG}."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The KRI-968 replay's prod flags (phone pilot, brief binding, choice questions)."""
    for name, value in {
        "kria_runtime_v2_enabled": True,
        "main_creator_agent_enabled": True,
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "kria_runtime_v2_phone_user_ids": [],
        "subtitled_archetype_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
        "phone_render_verified_features": ["positionedText", "animatedText"],
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _ask(language: object, question: str) -> str:
    """A recorded-shape Main Creator answer: a plain question and the language it read."""
    return json.dumps(
        {
            "action": {
                "kind": "ask_user",
                "question": question,
                "reason_code": "lyrics_placement",
                "options": [],
            },
            "conversation_language": language,
        }
    )


class _Model:
    """Stands in for the provider call only; records what the model was told."""

    def __init__(self, answer: str | MainCreatorOutput | Exception) -> None:
        self.answer = answer
        self.inputs: list = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Model:
        model = self

        def _run(agent, agent_input, ctx=None):  # noqa: ANN001, ANN202
            model.inputs.append(agent_input)
            if isinstance(model.answer, Exception):
                raise model.answer
            if isinstance(model.answer, MainCreatorOutput):
                return model.answer
            return agent.parse(model.answer, agent_input)

        monkeypatch.setattr(MainCreatorAgent, "run", _run)
        return self


def _revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _stored(thread_id: uuid.UUID) -> str | None:
    with sync_session() as db:
        return (db.get(CreationThread, thread_id).state or {}).get("reply_language")


def _last_reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.role == "assistant",
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


async def _turn(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    message: str,
    *,
    locale: str | None = None,
    choice: tuple[str, str] | None = None,
) -> dict:
    selection = (
        {"choice_selection": ChoiceSelectionIn(question_id=choice[0], option_key=choice[1])}
        if choice
        else {}
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"model-language-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
                **selection,
            ),
            locale=locale,
        )
    return await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)


# == 1: the incident's first message ============================================================


@pytest.mark.asyncio
async def test_the_incident_first_message_gets_an_english_reply(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id = _seed_t1(language=None)
    model = _Model(_ask("en", "Should each line show on its own clip?")).install(monkeypatch)

    result = await _turn(user_id, thread_id, INCIDENT, locale=TURKISH_PHONE)

    assert result["status"] == "completed", result
    assert _stored(thread_id) == "en"
    reply = _last_reply(thread_id)
    assert reply.content.startswith("Should each line show on its own clip?")
    # The device's Turkish was only context for the model; nothing ordered it.
    assert model.inputs[0].reply_language == "tr"
    prompt = MainCreatorAgent(None).render_prompt(model.inputs[0])
    assert "This chat has been in Turkish (tr) so far." in prompt
    assert "Creator chat language: Turkish" not in prompt


# == 2, 3, 4: a real switch is followed, a short reply keeps the chat ==========================


@pytest.mark.asyncio
async def test_a_turkish_sentence_switches_and_short_replies_keep_it(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id = _seed_t1(language="en")
    model = _Model(_ask("tr", "Sözler videonun başında mı görünsün?")).install(monkeypatch)

    await _turn(user_id, thread_id, "Bu sözleri videonun başında büyük göster lütfen")
    assert _stored(thread_id) == "tr"
    assert model.inputs[-1].reply_language == "en"

    for short in ("Do it", "Yes", "Yap"):
        model.answer = _ask(None, "Tamam, hangi klipte?")
        await _turn(user_id, thread_id, short)
        assert _stored(thread_id) == "tr", short
        assert model.inputs[-1].reply_language == "tr"

    # A clear English sentence later moves the chat back.
    model.answer = _ask("en", "Which clip should the lyrics start on?")
    await _turn(user_id, thread_id, "Actually, please answer me in English from now on")
    assert _stored(thread_id) == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["en", "tr"])
async def test_a_short_reply_the_model_cannot_read_keeps_the_chat(monkeypatch, stored) -> None:  # noqa: ANN001
    user_id, thread_id = _seed_t1(language=stored)
    _Model(_ask(None, "Which clip?")).install(monkeypatch)

    await _turn(user_id, thread_id, "Yap" if stored == "en" else "Do it")

    assert _stored(thread_id) == stored


# == 5: an unsupported code keeps the stored language ==========================================


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["xx", "de"])
async def test_an_unsupported_code_keeps_the_stored_language(monkeypatch, reported) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "kria_german_replies_enabled", False)
    user_id, thread_id = _seed_t1(language="tr")
    _Model(_ask(reported, "Hangi klip?")).install(monkeypatch)

    result = await _turn(user_id, thread_id, "Mach die Schrift größer bitte")

    assert result["status"] == "completed", result
    assert _stored(thread_id) == "tr"


# == 6: a failed Main Creator call keeps the language and fails the turn cleanly ===============


@pytest.mark.asyncio
async def test_a_failed_model_call_keeps_the_stored_language(monkeypatch) -> None:  # noqa: ANN001
    from app.agents._runtime import TerminalError

    user_id, thread_id = _seed_t1(language="tr")
    _Model(TerminalError("provider down")).install(monkeypatch)

    result = await _turn(user_id, thread_id, INCIDENT)

    assert _stored(thread_id) == "tr"
    reply = _last_reply(thread_id)
    # An honest recovery reply, in the stored Turkish (the incident text is English + lyrics).
    assert reply.content.startswith("Bu sefer bunu bir plana dönüştüremedim."), reply.content
    assert result["status"] in {"completed", "failed"}, result


# == 7: server copy written after the model call follows the model's language =================


async def _held_draft(monkeypatch, thread_language: str, model_language: str | None):  # noqa: ANN001, ANN202
    user_id, thread_id = _seed_t1(language=thread_language)
    output = _planned(_strategy()).model_copy(update={"conversation_language": model_language})
    _Model(output).install(monkeypatch)
    result = await _turn(
        user_id,
        thread_id,
        EVENTS[9]["content"],
        choice=(
            EVENTS[9]["payload"]["choice_selection"]["question_id"],
            EVENTS[9]["payload"]["choice_selection"]["option_key"],
        ),
    )
    assert result["status"] in {"completed", "awaiting_approval"}, result
    return thread_id, _last_reply(thread_id)


@pytest.mark.asyncio
async def test_the_readiness_hold_is_written_in_the_models_language(monkeypatch) -> None:  # noqa: ANN001
    thread_id, held = await _held_draft(monkeypatch, "tr", "en")

    assert held.payload["brief_coverage"]["reason"] == "simplification_requires_choice"
    assert EN_HELD in held.content
    assert TR_HELD not in held.content
    assert _stored(thread_id) == "en"


@pytest.mark.asyncio
async def test_the_readiness_hold_follows_a_switch_to_turkish(monkeypatch) -> None:  # noqa: ANN001
    thread_id, held = await _held_draft(monkeypatch, "en", "tr")

    assert TR_HELD in held.content
    assert EN_HELD not in held.content
    assert _stored(thread_id) == "tr"


@pytest.mark.asyncio
async def test_the_readiness_hold_keeps_the_stored_language_when_unread(monkeypatch) -> None:  # noqa: ANN001
    thread_id, held = await _held_draft(monkeypatch, "tr", None)

    assert TR_HELD in held.content
    assert _stored(thread_id) == "tr"


# == 8: a turn with no Main Creator call uses the stored language ===============================


@pytest.mark.asyncio
async def test_a_turn_with_no_model_call_answers_in_the_stored_language(monkeypatch) -> None:  # noqa: ANN001
    # No clips yet: the planner answers before any model runs.
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**(thread.state or {}), "reply_language": "tr"}
        db.commit()
    model = _Model(_ask("en", "unused")).install(monkeypatch)

    await _turn(user_id, thread_id, INCIDENT, locale="en-US")

    assert model.inputs == []
    assert _stored(thread_id) == "tr"
    assert _last_reply(thread_id).content.startswith("En az bir klip ekle")


@pytest.mark.asyncio
async def test_a_status_question_uses_the_stored_language(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id = _seed_t1(language="tr")
    model = _Model(_ask("en", "unused")).install(monkeypatch)

    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Status update",
                client_event_id=f"model-language-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
            ),
            locale="en-US",
        )

    assert accepted.status == "completed"
    assert model.inputs == []
    assert _stored(thread_id) == "tr"
    assert _last_reply(thread_id).content.startswith("Şu anda devam eden bir düzenleme yok")


# == 9: the kill switch ========================================================================


@pytest.mark.asyncio
async def test_kill_switch_off_stores_nothing_and_ignores_the_model(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "kria_reply_language_enabled", False)
    user_id, thread_id = _seed_t1(language=None)
    model = _Model(_ask("tr", "Which clip?")).install(monkeypatch)

    await _turn(
        user_id, thread_id, "Bu sözleri videonun başında büyük göster lütfen", locale=TURKISH_PHONE
    )

    assert _stored(thread_id) is None
    assert _last_reply(thread_id).content.startswith("Which clip?")
    assert "conversation_language" not in MainCreatorAgent(None).render_prompt(model.inputs[0])

    thread_id, held = await _held_draft(monkeypatch, None, "tr")
    assert EN_HELD in held.content
    assert _stored(thread_id) is None


# == deferred extraction (copilot-first turn): stores its reading only for the latest message ==


def _latest_turn_id(thread_id: uuid.UUID) -> str:
    with sync_session() as db:
        turn = (
            db.execute(
                select(CreatorAgentTurn)
                .where(CreatorAgentTurn.thread_id == thread_id)
                .order_by(CreatorAgentTurn.created_at.desc())
            )
            .scalars()
            .first()
        )
        return str(turn.id)


@pytest.mark.asyncio
async def test_deferred_extraction_stores_only_for_the_latest_message(monkeypatch) -> None:  # noqa: ANN001
    import app.tasks.kria_runtime as kria_runtime

    user_id, thread_id = _seed_t1(language="en")
    _Model(_ask(None, "Which clip?")).install(monkeypatch)
    await _turn(user_id, thread_id, "Yazıyı biraz büyüt")
    first = _latest_turn_id(thread_id)

    async def _read(_snapshot, _message, language="tr"):  # noqa: ANN001, ANN202
        return (), None, language

    monkeypatch.setattr(kria_runtime, "_extract_brief_async", _read)
    # The copilot answered first; the extraction read Turkish for the latest message: stored.
    result = await asyncio.to_thread(kria_runtime.extract_kria_brief.run, first)
    assert result["status"] != "failed", result
    assert _stored(thread_id) == "tr"

    # A newer message has been decided since; a late extraction of the older one never wins.
    await _turn(user_id, thread_id, "Do it")
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**(thread.state or {}), "reply_language": "en"}
        db.commit()
    await asyncio.to_thread(kria_runtime.extract_kria_brief.run, first)
    assert _stored(thread_id) == "en"
