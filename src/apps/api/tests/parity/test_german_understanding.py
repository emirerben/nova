"""German twins of the Turkish understanding tests that sit outside the parity table.

KRI-1453. ``test_decision_parity_de.py`` gives every EN/TR parity case a German
phrasing. These are the other Turkish understanding suites, asked in German:

* choice answers (``tests/services/test_kri520_choice_turkish.py``): ordinals, declining the
  title, handing the choice over, and the non-answers that must stay unanswered;
* the edit copilot's typed seconds (``tests/test_kri520_copilot_turkish.py``);
* a title hold in number words (``tests/kria/test_title_hold_grounding.py``);
* speech-cleanup fillers (``tests/pipeline/test_silence_cut.py``) and the caption-language
  check of German speech (``tests/pipeline/test_caption_language_id.py``);
* which language a German chat message reads as (``tests/kria/test_kri520_reply_language.py``).

Failure modes, written before the fixes:

1. A German reply to a choice card ("die zweite", "kein Titel", "du entscheidest") is not
   read, so the card is asked again or the model guesses.
2. A word that is only a number or a shrug ("zwei", "egal") is taken for an answer.
3. Bare "nein" skips the title when Kria asked "do you have an idea?" (it means "no idea,
   write one"), the trap Turkish "yok" has.
4. "10 Sekunden" answering "how long?" is not read, so the pending edit stays unresolved.
5. "zwei Sekunden" is not the creator's own title hold, so Kria asks about a length
   the creator stated; or a bare "zwei" with no unit becomes a hold.
6. German hesitations ("äh", "ähm") are not cut as fillers; or a real word is.
7. German speech is called Turkish (ö/ü), so the caption check re-runs whisper in the wrong
   language.
8. A German chat message is read as Turkish ("Lösch alle Beschriftungen": ö) or English
   ("Ordne die Clips in der ...": "in", "Clips"), so Kria answers in the wrong language.
9. Any of the above changes an English or Turkish answer.

Under the language policy (DECISIONS 2026-10-21) the fixes only grow closed lists (choice
answer phrases, units, number words, filler sounds, detector words); no new pattern reads
intent from German words. Gaps of that kind stay strict xfails in the parity suite.
"""

from __future__ import annotations

import pytest

from app.services.choice_questions import (
    OPT_NO_TITLE,
    build_choice_question,
    delegated_choice,
    match_open_choice,
    title_text_choice,
)
from app.services.creative_copy_decisions import (
    OPT_CANCEL,
    authorship_question,
    wording_question,
)


@pytest.fixture
def german_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)


def _three_options() -> dict:
    return {
        "options": [
            {"key": "a", "label": "Alpha plan"},
            {"key": "b", "label": "Beta plan"},
            {"key": "c", "label": "Gamma plan"},
        ]
    }


def _title_question() -> dict:
    return build_choice_question(title_text_choice(["r1"]).candidate())


def _delegable_question() -> dict:
    return {
        "kind": "duration_vs_count",
        "options": [{"key": "keep_all", "recommended": True}, {"key": "trim"}],
    }


# ── 1/2: choice answers ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reply", "key"),
    [
        ("die erste", "a"),
        ("Die Erste", "a"),
        ("erste", "a"),
        ("Erste Option", "a"),
        ("die erste Option", "a"),
        ("den ersten", "a"),
        ("das erste", "a"),
        ("die zweite", "b"),
        ("Zweite!", "b"),
        ("der zweite", "b"),
        ("zweite Variante", "b"),
        ("Nummer 2", "b"),
        ("Nr. 2", "b"),
        ("Nummer zwei", "b"),
        ("Option zwei", "b"),
        ("Variante 2", "b"),
        ("die dritte", "c"),
        ("DIE DRITTE", "c"),
        ("drittes", "c"),
        ("Nummer drei", "c"),
        # Language-neutral numbering works in German already.
        ("2", "b"),
        ("2.", "b"),
        ("Option 3", "c"),
    ],
)
def test_german_ordinals_pick_the_numbered_option(reply: str, key: str) -> None:
    assert match_open_choice(_three_options(), reply) == key


@pytest.mark.parametrize(
    "reply",
    ["zwei", "drei", "vierte", "die vierte", "Nummer", "die zweite und die dritte", "egal", ""],
)
def test_unclear_german_numbers_are_not_answers(reply: str) -> None:
    assert match_open_choice(_three_options(), reply) is None


def test_a_one_option_question_takes_german_ordinals_as_the_creators_words() -> None:
    title = _title_question()
    for words in ("Erste", "die erste", "Der Erste"):
        assert match_open_choice(title, words) is None, words


@pytest.mark.parametrize(
    "reply",
    [
        "kein Titel",
        "Kein Titel!",
        "keinen Titel",
        "Keinen Titel bitte",
        "ohne Titel",
        "Ohne Titel weiter",
        "nein",
        "Nein danke",
        "überspringen",
        "Überspringen",
        "lass den Titel weg",
        "Ich möchte keinen Titel",
    ],
)
def test_german_no_title_replies_decline_the_title(reply: str) -> None:
    assert match_open_choice(_title_question(), reply) == OPT_NO_TITLE


@pytest.mark.parametrize("reply", ["Sommer in Berlin", "Titel: Kein Ende", "nein sag ich doch"])
def test_german_title_words_are_never_taken_for_a_decline(reply: str) -> None:
    assert match_open_choice(_title_question(), reply) is None


def test_german_copy_skip_but_not_a_bare_no() -> None:
    for build in (
        lambda: authorship_question(target="opening_title", dependency_digest="d"),
        lambda: wording_question(
            target="opening_title", candidate="Wochenende", dependency_digest="d"
        ),
    ):
        question = build()
        for reply in ("überspringen", "kein Titel", "Keinen Titel", "ohne Titel"):
            assert match_open_choice(question, reply) == OPT_CANCEL, reply
        # "Hast du eine Idee?" -> "Nein": "I don't have one" (write it), never a skip.
        assert match_open_choice(question, "nein") != OPT_CANCEL


@pytest.mark.parametrize(
    "reply",
    [
        "du entscheidest",
        "Du entscheidest!",
        "entscheide du",
        "das überlasse ich dir",
        "Ich überlasse es dir",
        "deine Wahl",
        "liegt bei dir",
        "wie du willst",
        "überrasch mich",
        "Überrasche mich",
        "uberrasch mich",  # typed without the umlaut
    ],
)
def test_german_delegation_picks_the_recommended_option(reply: str) -> None:
    question = _delegable_question()
    assert match_open_choice(question, reply) is None
    assert delegated_choice(question, reply) == "keep_all"


@pytest.mark.parametrize(
    "reply", ["egal", "ist mir egal", "du entscheidest, aber kurz", "morgen entscheidest du"]
)
def test_a_german_shrug_or_instruction_is_not_a_delegation(reply: str) -> None:
    assert delegated_choice(_delegable_question(), reply) is None


def test_german_delegation_never_drops_a_title_or_approves_copy() -> None:
    assert delegated_choice(_title_question(), "du entscheidest") is None
    copy = authorship_question(target="opening_title", dependency_digest="d")
    assert delegated_choice(copy, "du entscheidest") is None


def test_english_and_turkish_choice_answers_are_unchanged() -> None:
    question = _three_options()
    assert match_open_choice(question, "number 1") == "a"
    assert match_open_choice(question, "ikinci") == "b"
    assert match_open_choice(question, "üç numara") == "c"
    assert match_open_choice(question, "iki") is None
    assert match_open_choice(_title_question(), "no title") == OPT_NO_TITLE
    assert match_open_choice(_title_question(), "başlık yok") == OPT_NO_TITLE
    assert delegated_choice(_delegable_question(), "sen seç") == "keep_all"
    assert delegated_choice(_delegable_question(), "whatever") is None


# ── 4: the copilot's typed seconds ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("10 Sekunden", 10.0),
        ("jeweils 3 Sekunden", 3.0),
        ("mach sie 5 Sekunden lang", 5.0),
        ("eine Sekunde? nein, 1 Sekunde", 1.0),
        ("2,5 Sekunden", 2.5),
        ("10 Sek.", 10.0),
        ("10sek", 10.0),
    ],
)
def test_duration_answer_reads_german_seconds(text: str, seconds: float) -> None:
    from app.agents.edit_copilot import _DURATION_ANSWER_RE, _duration_seconds

    match = _DURATION_ANSWER_RE.search(text)
    assert match is not None and _duration_seconds(match) == seconds


@pytest.mark.parametrize("text", ["10 Clips", "Sekunden", "zehn Sekunden", "10 Sekt", ""])
def test_duration_answer_ignores_german_non_durations(text: str) -> None:
    from app.agents.edit_copilot import _DURATION_ANSWER_RE

    assert _DURATION_ANSWER_RE.search(text) is None


# ── 5: a title hold in number words ─────────────────────────────────────────


def _grounded(seconds: float, text: str) -> bool:
    from app.kria.planner import title_hold_grounded

    return title_hold_grounded(seconds, evidence=text, user_sources=[text])


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("Lass den Titel zwei Sekunden stehen", 2.0),
        ("Zeig den Titel 2 Sekunden lang", 2.0),
        ("Titel für drei Sekunden", 3.0),
        ("TITEL FÜNF SEKUNDEN", 5.0),
        ("Titel funf Sekunden", 5.0),
        ("der Titel soll eine Sekunde bleiben", 1.0),
        ("Titel 1,5 Sekunden", 1.5),
    ],
)
def test_a_german_title_hold_is_the_creators_own_words(text: str, seconds: float) -> None:
    assert _grounded(seconds, text)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("Titel: Zwei Freunde", 2.0),  # a number word with no unit is not a hold
        ("Lass den Titel zwei Sekunden stehen", 3.0),  # a different length
        ("Mach zwei Titel", 2.0),
    ],
)
def test_german_words_that_are_not_that_hold(text: str, seconds: float) -> None:
    assert not _grounded(seconds, text)


def test_english_and_turkish_title_holds_are_unchanged() -> None:
    assert _grounded(2.0, "keep the title for two seconds")
    assert _grounded(2.0, "Başlık iki saniye kalsın")
    assert not _grounded(2.0, "on second thought, keep the title")


# ── 6/7: speech cleanup ──────────────────────────────────────────────────────


@pytest.mark.parametrize("token", ["äh", "Äh,", "ähm", "Ähm.", "ähhh", "ähmmm", "öh", "Öhm"])
def test_german_hesitations_are_fillers(token: str) -> None:
    from app.pipeline.silence_cut import is_filler_token

    assert is_filler_token(token)


@pytest.mark.parametrize(
    "token", ["ähnlich", "Öl", "öfter", "Ehre", "hm", "also", "Ähre", "ah", "uh", "ııı"]
)
def test_german_words_are_not_fillers_and_old_fillers_stay(token: str) -> None:
    from app.pipeline.silence_cut import is_filler_token

    # "ah", "uh", "ııı" were fillers before and still are; the German words never are.
    assert is_filler_token(token) is (token in {"ah", "uh", "ııı"})


_GERMAN_SPEECH = (
    "Hallo zusammen, heute zeige ich euch mein Lieblingscafé in München und wir trinken "
    "zusammen einen Kaffee, der wirklich gut ist."
)
_GERMAN_SPEECH_UMLAUTS = (
    "Also wir sind jetzt in Köln, es ist schön hier und gleich gehen wir über die Brücke "
    "zum Dom, das wird bestimmt toll."
)


@pytest.mark.parametrize("text", [_GERMAN_SPEECH, _GERMAN_SPEECH_UMLAUTS])
def test_german_speech_is_german_never_turkish(text: str) -> None:
    from app.pipeline.caption_language import (
        caption_language_verdict,
        detect_text_language,
        infer_language_from_text,
    )

    assert detect_text_language(text) == "de"
    assert infer_language_from_text(text) == "de"
    assert caption_language_verdict([text], "de") == ("confirmed", "de")
    assert caption_language_verdict([text], "tr") == ("contradicted", "de")


# ── 8: which language a German message reads as ─────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Lösch alle Beschriftungen",
        "Ordne die Clips in der Reihenfolge, in der ich sie gefilmt habe.",
        "Nimm das Sonnenuntergangsfoto als Titelbild.",
        "Mach es langsam und ruhig, keine schnellen Schnitte.",
        "Ist mein Video schon fertig?",
    ],
)
def test_german_messages_read_as_german(text: str, german_on: None) -> None:
    from app.kria.reply_language import creator_text_language, detect_chat_language

    assert detect_chat_language(text) == "de"
    assert creator_text_language(text) == "de"


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("delete all the labels", "en"),
        ("Put the clips in the order I filmed them.", "en"),
        ("I will make the video shorter", "en"),
        ("Use the sunset photo as the cover, die hard fans love it", "en"),
        ("bütün etiketleri sil", "tr"),
        ("klipleri çektiğim sıraya göre diz", "tr"),
        ("Bunu sen seç, ben karar veremedim", "tr"),
    ],
)
def test_english_and_turkish_keep_their_language_with_german_on(
    text: str, language: str, german_on: None
) -> None:
    from app.kria.reply_language import creator_text_language, detect_chat_language

    assert detect_chat_language(text) == language
    assert creator_text_language(text) in {language, None}
