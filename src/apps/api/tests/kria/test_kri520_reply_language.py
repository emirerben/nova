"""KRI-520: the chat's reply language — detection, stickiness, binding, prompt line."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.agents.main_creator import MainCreatorAgent
from app.kria.language import is_help_question, is_paraphrase_only, is_status_question
from app.kria.reply_language import (
    current_reply_language,
    detect_chat_language,
    is_client_stock_message,
    locale_language,
    prompt_language_line,
    remember_reply_language,
    reply_language_for,
    resolve_reply_language,
    say,
    thread_reply_language,
)
from tests.agents.test_main_creator_agent import _input


@pytest.mark.parametrize(
    "message",
    [
        "bitti mi",
        "nasıl gidiyor",
        "yardım",
        "tamam",
        "hayir",  # typed on an English keyboard
        "baslik ekle",
        "Başlığı büyüt",
        "videoyu daha kisa yap",
        "2. klibi başa al",
        "sen karar ver",
        "devam",
        "İstanbul'da bir gün",
        "Başlık: 'Best day ever'",  # quoted on-screen text stays the creator's
    ],
)
def test_turkish_messages_read_as_turkish(message: str) -> None:
    assert detect_chat_language(message) == "tr"


@pytest.mark.parametrize(
    "message",
    ["Make the title bigger", "yes", "add music", "I want the clips in order"],
)
def test_english_messages_read_as_english(message: str) -> None:
    assert detect_chat_language(message) == "en"


@pytest.mark.parametrize(
    "message",
    [
        "ok",
        "1",
        "go ahead",
        "",
        # Other languages never read as Turkish or English.
        "quiero un video de mi viaje to Madrid",
        "Mach es länger für mich",
        # Mixed evenly: says nothing on its own.
        "Title: Summer in Madrid, pastel sarı olsun",
    ],
)
def test_unclear_messages_say_nothing(message: str) -> None:
    assert detect_chat_language(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "Suggest an edit.",
        "Retry preparing my clips.",
        "Keep the same plan with my current footage",
        "Try generating this edit again",
        "Skip, decide for me",
        "Use this order: clips 3, 1, 2",
        "İlk gün: clips 3, 7",
        "None of these for Kadıköy",
        "Let's make a montage",
        "Make it warmer",
    ],
)
def test_app_sent_stock_sentences_never_set_the_language(message: str) -> None:
    assert is_client_stock_message(message)
    assert detect_chat_language(message) is None
    assert resolve_reply_language(message, previous="tr") == "tr"


def test_language_is_sticky_until_a_clear_switch() -> None:
    assert resolve_reply_language("make it pop", previous="tr") == "tr"
    assert resolve_reply_language("ok", previous="tr") == "tr"
    assert resolve_reply_language("Make the title much bigger please", previous="tr") == "en"
    assert resolve_reply_language("Kadıköy", previous="en") == "en"
    assert resolve_reply_language("Başlığı büyüt lütfen", previous="en") == "tr"


def test_device_language_only_breaks_ties() -> None:
    assert resolve_reply_language("tamam", locale="en-US") == "tr"
    assert resolve_reply_language("Suggest an edit.", locale="tr-TR,en;q=0.8") == "tr"
    assert resolve_reply_language("Suggest an edit.", locale="de-DE,tr;q=0.8") is None
    assert resolve_reply_language("Suggest an edit.", previous="en", locale="tr-TR") == "en"
    assert resolve_reply_language("Suggest an edit.") is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("tr-TR,tr;q=0.9,en-US;q=0.8", "tr"),
        ("tr", "tr"),
        ("en-GB", "en"),
        ("de-DE,tr;q=0.8", None),
        ("*", None),
        ("", None),
        (None, None),
    ],
)
def test_locale_language_reads_the_top_preference(header: str | None, expected: str | None) -> None:
    assert locale_language(header) == expected


def test_thread_state_round_trip_and_kill_switch() -> None:
    thread = SimpleNamespace(state={"media": []})
    assert thread_reply_language(thread) is None
    remember_reply_language(thread, "tr")
    assert thread.state == {"media": [], "reply_language": "tr"}
    remember_reply_language(thread, None)
    assert thread.state["reply_language"] == "tr"
    with patch("app.config.settings.kria_reply_language_enabled", False):
        assert thread_reply_language(thread) is None
        remember_reply_language(thread, "en")
        assert thread.state["reply_language"] == "tr"
        with reply_language_for("tr"):
            assert say(en="Done", tr="Tamam") == "Done"


def test_say_follows_the_bound_language_and_restores() -> None:
    assert current_reply_language() is None
    assert say(en="Done", tr="Tamam") == "Done"
    with reply_language_for("tr"):
        assert say(en="Done", tr="Tamam") == "Tamam"
        with reply_language_for("en"):
            assert say(en="Done", tr="Tamam") == "Done"
        assert current_reply_language() == "tr"
    with reply_language_for("es"):
        assert current_reply_language() is None
    assert current_reply_language() is None


def test_prompt_line_only_for_turkish() -> None:
    assert prompt_language_line(None) == ""
    assert prompt_language_line("en") == ""
    assert prompt_language_line("es") == ""
    line = prompt_language_line("tr")
    assert "Turkish (tr)" in line
    assert "exactly as the creator" in line


def test_main_creator_prompt_gets_the_line_only_for_turkish() -> None:
    agent = MainCreatorAgent(None)
    english = agent.render_prompt(_input())
    assert agent.render_prompt(_input().model_copy(update={"reply_language": "en"})) == english
    turkish = agent.render_prompt(_input().model_copy(update={"reply_language": "tr"}))
    assert turkish.startswith(english.rstrip("\n"))
    assert turkish.endswith(prompt_language_line("tr") + "\n")
    assert "reply_language" not in _input().model_dump()


def test_copilot_prompt_gets_the_line_only_for_turkish() -> None:
    agent = EditCopilotAgent(None)
    base = EditCopilotInput(utterance="başlığı büyüt")
    english = agent.render_prompt(base)
    assert agent.render_prompt(base.model_copy(update={"reply_language": "en"})) == english
    turkish = agent.render_prompt(base.model_copy(update={"reply_language": "tr"}))
    assert turkish.endswith(prompt_language_line("tr") + "\n")
    assert "reply_language" not in base.model_dump()


@pytest.mark.parametrize(
    ("message", "status", "help_request"),
    [
        ("Nasıl gidiyor?", True, False),
        ("Bitti mi?", True, False),
        ("DURUM NE", True, False),
        ("Yardım", False, True),
        ("Neler yapabilirsin?", False, True),
        ("Başlığı büyütmeme yardım et", False, False),
    ],
)
def test_turkish_status_and_help_questions(message: str, status: bool, help_request: bool) -> None:
    assert is_status_question(message) is status
    assert is_help_question(message) is help_request


def test_paraphrase_guard_reads_turkish_and_other_scripts() -> None:
    assert is_paraphrase_only(
        user_message="başlığı büyüt ve müziği kıs lütfen",
        assistant_message="Anladım, başlığı büyütmemi istiyorsun.",
    )
    assert not is_paraphrase_only(
        user_message="make the title bigger please",
        assistant_message="タイトルを大きくしました",
    )
    assert not is_paraphrase_only(
        user_message="başlığı büyüt ve müziği kıs lütfen",
        assistant_message="Başlığı büyüttüm ve müziği kıstım.",
    )
