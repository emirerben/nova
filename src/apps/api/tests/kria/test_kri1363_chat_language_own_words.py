"""KRI-1363: the chat answers in the language the creator writes, not the video's language.

An English chat that carried Turkish text for the video (quoted captions, "Title: ...", a bare
title typed as the answer to "what words for the title?") moved to Turkish: the detector counted
every word of the message as the creator talking. The messages below are the reproduction from
the ticket, run against the real detector.

Ways the fix could go wrong, written down before the code:

1. Quoted Turkish text in an English message still moves the chat to Turkish (the bug).
2. Unquoted text after "Title:" still moves an established chat.
3. An apostrophe inside a word ("Kadıköy'deyiz", "don't", iOS's "İstanbul’da") is read as a quote
   mark and swallows the creator's own words.
4. An unclosed quote mark hides the rest of the message.
5. Text after a colon is dropped as "a label's text" and a whole request disappears: the real
   Green 4 message opens "Yorumla episode 9: ... Make the whole video like a green screen: ...".
6. The creator really changes language mid-chat and Kria never follows.
7. A mixed message (their own words in both languages) flips the chat on video text again.
8. A bare title typed as the answer to a wording question moves the chat; or the guard also
   freezes the language on an ordinary question (clip picker, simpler version).
9. Turkish chat + English video text has the same bug the other way.
10. A message that is ONLY video text ("'Günaydın İstanbul'") sets the language of a new chat.
11. The kill switch does not restore the old detector byte for byte.
12. One ambiguous word of the old language (a Turkish dictionary word like 'son', a place name
    like 'İstanbul') vetoes a clear switch (Emir's review on #2268). Only a short "Label:" in
    the chat's language, followed by text in the other language, keeps the chat; a message
    with no such label moves on its own clear words.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.reply_language import (
    asks_for_video_text,
    detect_chat_language,
    resolve_reply_language,
)

QUOTED_TR = 'Use these captions: "Bugün Kadıköy\'deyiz. Burası çok güzel. Hadi başlayalım."'
LABEL_TR = "Title: Bugün İstanbul'da çok güzel bir gün geçirdik"
SMART_QUOTED_TR = "Put this at the start: “Merhaba arkadaşlar, bugün İstanbul’da çok güzel bir gün”"
SINGLE_QUOTED_TR = "Add the caption 'Bugün Kadıköy'deyiz, burası çok güzel bir yer' at the start"
BARE_TR_TITLE = "Bugün İstanbul'da çok güzel bir gün geçirdik"
QUOTED_EN = (
    'Altyazıları şöyle yap: "Good morning everyone, today we are in Istanbul and it is great"'
)
LABEL_EN = "Başlık: Good morning everyone, this is my first day here"


@pytest.fixture
def old_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_reply_language_own_words_enabled", False)


# -- 1, 2, 9: video text never moves an established chat -------------------------------------


@pytest.mark.parametrize("message", [QUOTED_TR, LABEL_TR, SMART_QUOTED_TR, SINGLE_QUOTED_TR])
def test_turkish_video_text_keeps_an_english_chat_english(message: str) -> None:
    assert resolve_reply_language(message, previous="en") == "en"


@pytest.mark.parametrize("message", [QUOTED_EN, LABEL_EN])
def test_english_video_text_keeps_a_turkish_chat_turkish(message: str) -> None:
    assert resolve_reply_language(message, previous="tr") == "tr"


@pytest.mark.parametrize("message", [QUOTED_TR, LABEL_TR])
def test_the_old_detector_moved_the_chat(old_detector, message: str) -> None:
    # 11: the kill switch is the reported bug, exactly.
    assert resolve_reply_language(message, previous="en") == "tr"


# -- 10: a new chat takes its language from the creator's own words --------------------------


def test_a_new_chat_reads_the_words_around_the_quote() -> None:
    assert resolve_reply_language(QUOTED_TR, previous=None) == "en"
    assert resolve_reply_language(QUOTED_EN, previous=None) == "tr"


def test_a_message_that_is_only_video_text_says_nothing() -> None:
    assert detect_chat_language("'Günaydın İstanbul, bugün çok güzel bir gün'") is None
    assert resolve_reply_language('"Bugün çok güzel bir gün"', previous="en") == "en"


# -- 3, 4, 5: what is NOT video text ---------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "language"),
    [
        # Apostrophes inside words are not quote marks.
        ("Bugün Kadıköy'deyiz ve burası çok güzel, videoyu kısalt lütfen", "tr"),
        ("Kadıköy’deki klibi başa al, sonra müziği değiştir lütfen", "tr"),
        ("I don't like the title, make it shorter and remove the music", "en"),
        ("It’s too long, please cut the first clip and keep the music", "en"),
        # An unclosed quote mark hides nothing.
        ('Make the title "shorter and remove the music please', "en"),
        ("Başlığı 'daha kısa yap ve müziği kaldır lütfen", "tr"),
        # Text after a colon is the creator talking (the real Green 4 message).
        ("At 0:05 add the title and remove the music please", "en"),
        ("0:05'te başlığı ekle ve müziği kaldır lütfen", "tr"),
        (
            "Yorumla episode 9: Kervan x Lumo Pamukkale show. Make the whole video like a green "
            "screen: me small at the bottom right in front, and behind me full screen whichever "
            "photo or video I'm talking about, changing every 3-5 seconds. No music.",
            "en",
        ),
        (
            "I want you to change these things for me please: müziği kapat ve başlığı sil",
            "en",
        ),
    ],
)
def test_the_creators_own_words_still_count(message: str, language: str) -> None:
    assert detect_chat_language(message) == language


# -- 6, 7: a real change of language is followed; a mixed message is not ---------------------


def test_a_whole_message_in_the_other_language_moves_the_chat() -> None:
    assert resolve_reply_language("Tamam, şimdi müziği değiştir ve başlığı sil", previous="en") == (
        "tr"
    )
    assert resolve_reply_language(
        "ok now please change the music and remove the title", previous="tr"
    ) == ("en")


def test_the_chat_moves_back_on_the_next_clear_message() -> None:
    chat = resolve_reply_language(QUOTED_TR, previous="en")
    chat = resolve_reply_language("Tamam, şimdi müziği değiştir ve başlığı sil", previous=chat)
    assert chat == "tr"
    chat = resolve_reply_language(QUOTED_EN, previous=chat)
    assert chat == "tr"
    chat = resolve_reply_language("please make the first clip shorter", previous=chat)
    assert chat == "en"


# -- 12: one ambiguous word of the old language never vetoes a clear switch ------------------


@pytest.mark.parametrize(
    ("message", "previous", "language"),
    [
        # Emir's review: "son" is a Turkish dictionary word, "İstanbul" has a Turkish letter.
        ("Please make the clip of my son shorter and remove the music", "tr", "en"),
        ("Please remove the İstanbul clip and make the rest shorter", "tr", "en"),
        # The mirror: a song called "Hello" inside a Turkish request.
        ("Videoyu kısalt ve Hello şarkısını kaldır, sonra ilk klibi sil lütfen", "en", "tr"),
        ("Bugün İstanbul'a gittik, videoyu kısalt ve müziği kaldır lütfen", "en", "tr"),
    ],
)
def test_one_ambiguous_word_does_not_hold_a_clear_switch(
    message: str, previous: str, language: str
) -> None:
    assert resolve_reply_language(message, previous=previous) == language


@pytest.mark.parametrize(
    ("message", "previous"),
    [
        # A label in the chat's language keeps it; a time ("0:05") or a long lead-in is no label.
        (LABEL_TR, "en"),
        (LABEL_EN, "tr"),
        ("Opening title: Bugün İstanbul'da çok güzel bir gün geçirdik", "en"),
    ],
)
def test_a_label_in_the_chats_language_keeps_it(message: str, previous: str) -> None:
    assert resolve_reply_language(message, previous=previous) == previous


def test_a_label_in_the_new_language_or_a_long_lead_in_is_a_switch() -> None:
    # "Başlık:" is Turkish: the creator labels in Turkish, so the chat follows them.
    assert resolve_reply_language("Başlık: Bugün İstanbul'da çok güzel bir gün", previous="en") == (
        "tr"
    )
    # A colon after a whole sentence is not a label.
    message = "Şimdi şunu yap lütfen ve çabuk ol: müziği kapat, başlığı sil, videoyu kısalt"
    assert resolve_reply_language(message, previous="en") == "tr"


def test_video_text_inside_a_sentence_holds_the_chat_only_when_marked() -> None:
    # 12 trade-off: with no quote and no label, the words alone cannot tell a title from the
    # creator changing language, and the Turkish outweighs the English, so the chat follows it.
    # Quoting the title or labelling it keeps the chat (the shapes the apps and Kria suggest).
    title = "Bugün İstanbul'da çok güzel bir gün geçirdik"
    assert resolve_reply_language(f"Add a title that says {title}", previous="en") == "tr"
    assert resolve_reply_language(f'Add a title that says "{title}"', previous="en") == "en"
    assert resolve_reply_language(f"Add a title that says: {title}", previous="en") == "tr"
    assert resolve_reply_language(f"Add this title: {title}", previous="en") == "en"


# -- 8: a typed answer to a wording question -------------------------------------------------


def _asked(question: dict) -> tuple[str, dict]:
    return ("assistant", {"choice_question": question})


TITLE_QUESTION = {"question_id": "q1", "conflict": "title_text", "allow_free_text": True}
HOOK_QUESTION = {
    "question_id": "q2",
    "conflict": "creative_copy",
    "creative_target": "opening_title",
    "allow_free_text": True,
}
SIMPLER_QUESTION = {
    "question_id": "q3",
    "kind": "strategy_refusal",
    "conflict": "simplification_requires_choice",
    "allow_free_text": True,
}


@pytest.mark.parametrize("question", [TITLE_QUESTION, HOOK_QUESTION])
def test_an_open_wording_question_asks_for_video_text(question: dict) -> None:
    events = [("user", {}), _asked(question)]
    assert asks_for_video_text(events) is True


def test_other_questions_and_answered_ones_do_not() -> None:
    assert asks_for_video_text([("user", {}), _asked(SIMPLER_QUESTION)]) is False
    assert asks_for_video_text([("user", {})]) is False
    answered = [
        ("user", {}),
        _asked(TITLE_QUESTION),
        ("user", {"choice_selection": {"question_id": "q1", "option_key": "no_title"}}),
    ]
    assert asks_for_video_text(answered) is False


def test_a_bare_title_typed_as_the_answer_keeps_the_chat_language() -> None:
    assert resolve_reply_language(BARE_TR_TITLE, previous="en", answers_video_text=True) == "en"
    # The same words with no wording question open are the creator changing language.
    assert resolve_reply_language(BARE_TR_TITLE, previous="en") == "tr"


def test_the_kill_switch_ignores_the_wording_question(old_detector) -> None:
    assert resolve_reply_language(BARE_TR_TITLE, previous="en", answers_video_text=True) == "tr"
    assert asks_for_video_text([("user", {}), _asked(TITLE_QUESTION)]) is False
