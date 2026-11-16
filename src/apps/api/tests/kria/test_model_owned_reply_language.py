"""Kria's chat language is decided by the model that reads the creator's message.

Prod thread 59f3707e (2026-11-16): the first message of a new chat was an English instruction
around Turkish lyrics ("Add the following lyrics as text, line by line: Vazgeçmem dünyaları
..."). Word scoring counted ~17 Turkish lyric words against 9 English ones and the chat answered
in Turkish. The owner rejected thresholds and word limits: the Main Creator (and, on a rendered
follow-up, the brief extractor) now reports ``conversation_language``, the language the creator
writes to Kria in, leaving out text meant for the video (agents/DECISIONS.md, language policy).

Failure modes, written down before the code (the Postgres half is
``test_model_owned_reply_language_postgres.py``, through ``submit_turn`` + ``run_kria_turn``):

1. The incident message gets a Turkish reply or ``tr`` stored (model said ``en``).
2. A genuinely Turkish sentence (model ``tr``) is answered in English or not stored.
3. A real mid-chat switch is not followed.
4. "Yap" / "Do it" / "Yes" (model ``None``) flip the stored language.
5. An unsupported code ("xx", or "de" with German off) is stored, or burns a schema retry.
6. A failed or timed-out Main Creator call crashes the language handling or changes it.
7. Server copy produced after the model call (the readiness hold) is in the stored language
   instead of the model's.
8. A turn with no Main Creator call changes the stored language.
9. With ``KRIA_REPLY_LANGUAGE_ENABLED`` off anything changes (prompt, binding, storage).
10. The model writes the field inside ``action`` (``extra="forbid"``) and the turn burns a retry.
11. The stored language is still sent as an order ("write in Turkish") instead of context.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import BriefExtractorAgent
from app.agents.main_creator import MainCreatorAgent
from app.config import settings
from app.kria import planner
from app.kria.reply_language import (
    adopt_conversation_language,
    conversation_language_line,
    current_reply_language,
    model_conversation_language,
    model_language_scope,
    prompt_language_line,
    remember_reply_language,
    reply_language_for,
)
from tests.agents.test_main_creator_agent import _input

INCIDENT = (
    "Add the following lyrics as text, line by line: Vazgeçmem dünyaları Önüme serseler de "
    "Senin isminde saklı Mutlukuk da keder de Kaderimiz yazılmış Seninle Fenerbahçe"
)


def _ask(language: object = "absent", *, nested: bool = False, question: str = "Where?") -> str:
    action = {"kind": "ask_user", "question": question, "reason_code": "placement", "options": []}
    data: dict = {"action": action}
    if language != "absent":
        (action if nested else data)["conversation_language"] = language
    return json.dumps(data)


def _incident_input():  # noqa: ANN202
    return _input().model_copy(update={"user_message": INCIDENT, "reply_language": "tr"})


# -- 1, 2, 4, 5, 10: the parse keeps only a language Kria replies in -------------------------


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("en", "en"), ("tr", "tr"), (None, None), ("absent", None), ("xx", None), (7, None)],
)
def test_main_creator_reports_the_creators_language(reported: object, expected: str) -> None:
    output = MainCreatorAgent(None).parse(_ask(reported), _incident_input())
    assert output.conversation_language == expected


def test_german_only_while_german_replies_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = MainCreatorAgent(None)
    monkeypatch.setattr(settings, "kria_german_replies_enabled", False)
    assert agent.parse(_ask("de"), _input()).conversation_language is None
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    assert agent.parse(_ask("de"), _input()).conversation_language == "de"


def test_a_language_written_inside_action_is_read_not_retried() -> None:
    output = MainCreatorAgent(None).parse(_ask("en", nested=True), _incident_input())
    assert output.conversation_language == "en"
    assert output.action.question == "Where?"


def test_the_field_is_left_out_of_dumps_when_unknown() -> None:
    output = MainCreatorAgent(None).parse(_ask(None), _input())
    assert "conversation_language" not in output.model_dump()


def test_brief_extractor_reports_the_language_too() -> None:
    agent = BriefExtractorAgent(None)
    raw = {"brief_updates": [], "request_scope": "edit", "conversation_language": "tr"}
    live = BriefExtractionInput(user_message="Yazıyı büyüt", require_request_scope=True)
    assert agent.parse(json.dumps(raw), live).conversation_language == "tr"
    raw["conversation_language"] = "xx"
    assert agent.parse(json.dumps(raw), live).conversation_language is None


# -- 11, 9: the prompt asks for the language and never orders the stored one ----------------


def test_main_creator_prompt_gives_the_stored_language_as_context() -> None:
    prompt = MainCreatorAgent(None).render_prompt(_incident_input())
    assert prompt.endswith(conversation_language_line("tr") + "\n")
    assert "This chat has been in Turkish (tr) so far." in prompt
    assert "conversation_language" in prompt
    assert "lyrics" in conversation_language_line("tr")
    assert prompt_language_line("tr") not in prompt


def test_brief_extractor_prompt_asks_only_on_a_live_followup() -> None:
    agent = BriefExtractorAgent(None)
    offline = BriefExtractionInput(user_message="Yazıyı büyüt", reply_language="tr")
    assert "conversation_language" not in agent.render_prompt(offline)
    live = offline.model_copy(update={"require_request_scope": True})
    assert agent.render_prompt(live).endswith(
        conversation_language_line("tr", beside="brief_updates") + "\n"
    )


def test_kill_switch_off_renders_no_language_line(monkeypatch: pytest.MonkeyPatch) -> None:
    on = MainCreatorAgent(None).render_prompt(_incident_input())
    monkeypatch.setattr(settings, "kria_reply_language_enabled", False)
    off = MainCreatorAgent(None).render_prompt(_incident_input())
    assert "conversation_language" not in off
    assert on.startswith(off.rstrip("\n"))


# -- binding: the model's language replaces the stored one for the rest of the turn ----------


def test_adopt_rebinds_the_turn_and_none_keeps_it() -> None:
    with reply_language_for("tr"), model_language_scope():
        assert adopt_conversation_language(None) is None
        assert current_reply_language() == "tr"
        assert model_conversation_language() is None
        assert adopt_conversation_language("xx") is None
        assert current_reply_language() == "tr"
        assert adopt_conversation_language("en") == "en"
        assert current_reply_language() == "en"
        assert model_conversation_language() == "en"
    assert current_reply_language() is None
    assert model_conversation_language() is None


def test_adopt_is_a_no_op_with_the_kill_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_reply_language_enabled", False)
    with model_language_scope():
        assert adopt_conversation_language("tr") is None
        assert current_reply_language() is None
        assert model_conversation_language() is None


class _Thread:
    def __init__(self, state: dict) -> None:
        self.state = state


def test_remember_stores_and_drops_the_old_switch_back_keys() -> None:
    thread = _Thread(
        {"reply_language": "tr", "reply_language_before": "en", "reply_language_switched_by": "x"}
    )
    remember_reply_language(thread, "en")
    assert thread.state == {"reply_language": "en"}
    remember_reply_language(thread, None)
    remember_reply_language(thread, "xx")
    assert thread.state == {"reply_language": "en"}


@pytest.mark.asyncio
async def test_the_planner_adopts_what_the_model_read(monkeypatch) -> None:  # noqa: ANN001
    """The planner's own call: parse runs, then the turn follows the model's language."""

    def _run(self, agent_input, ctx=None):  # noqa: ANN001, ANN202
        return self.parse(_ask("en"), agent_input)

    monkeypatch.setattr(MainCreatorAgent, "run", _run)
    inputs = planner._CreatorInputs(
        agent_input=_incident_input(), intent_clips=[], creator_request=None
    )
    with reply_language_for("tr"), model_language_scope():
        output = await planner._call_main_creator(
            inputs, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
        )
        assert output.conversation_language == "en"
        planner._adopt_read_language(output)
        assert current_reply_language() == "en"
        assert model_conversation_language() == "en"
        # A stand-in output without the field keeps what is bound.
        planner._adopt_read_language(object())
        assert current_reply_language() == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["terminal", "deadline"])
async def test_a_failed_main_creator_call_keeps_the_stored_language(
    monkeypatch,  # noqa: ANN001
    failure: str,
) -> None:
    from app.agents._runtime import ProviderDeadlineExceededError, TerminalError

    error = TerminalError if failure == "terminal" else ProviderDeadlineExceededError

    def _run(self, agent_input, ctx=None):  # noqa: ANN001, ANN202
        raise error("provider down")

    monkeypatch.setattr(MainCreatorAgent, "run", _run)
    inputs = planner._CreatorInputs(
        agent_input=_incident_input(), intent_clips=[], creator_request=None
    )
    with reply_language_for("tr"), model_language_scope():
        with pytest.raises((RuntimeError, ProviderDeadlineExceededError)):
            await planner._call_main_creator(
                inputs, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
            )
        assert current_reply_language() == "tr"
        assert model_conversation_language() is None


@pytest.mark.asyncio
async def test_a_rendered_followup_copilot_answers_in_the_extractors_language(monkeypatch) -> None:  # noqa: ANN001
    """Extractor -> editor ops -> copilot: the copilot reply follows the extractor's reading."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.kria.test_planner_editor_target_miss import _ask as ask_planner
    from tests.kria.test_planner_editor_target_miss import _wire_real

    heard: list = []
    ops = [{"op": "patch_text", "bar_id": "guided-title", "patch": {"text": "Merhaba"}}]

    async def _copilot(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        heard.append(current_reply_language())
        return SimpleNamespace(
            ops=ops, outcome="proposed", reply="Tamam.", intent="edit", rejection_reasons=[]
        )

    db, item, creator_id, _runs, _copilot_mock = _wire_real(
        monkeypatch,
        render_status="ready",
        copilot=AsyncMock(side_effect=_copilot),
        extracted_updates=[],
    )
    monkeypatch.setattr(settings, "kria_copilot_first_enabled", False)
    extractor_runs: list = []

    class _Extractor:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, agent_input, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            extractor_runs.append(agent_input.reply_language)
            return SimpleNamespace(
                brief_updates=[],
                request_scope="edit",
                clarification=None,
                delegates_all_choices=False,
                conversation_language="tr",
            )

    monkeypatch.setattr(planner, "BriefExtractorAgent", _Extractor)
    with reply_language_for("en"), model_language_scope():
        await ask_planner(db, item, creator_id, "Başlığı Merhaba yap")
        assert model_conversation_language() == "tr"
    assert extractor_runs == ["en"]  # the stored language was context
    assert heard == ["tr"]
