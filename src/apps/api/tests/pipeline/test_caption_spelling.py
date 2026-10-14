"""Guard for the creator-spelling matcher (KRI-631): it must stay narrow.

Failure modes pinned: unrelated look-alike words rewritten, short targets fuzzy-matched,
words that are already right touched, Turkish endings lost, quoted on-screen text read as a
spelling ask.
"""

from __future__ import annotations

import pytest

from app.pipeline.caption_spelling import (
    apply_spelling_to_cues,
    extract_spelling_asks,
    respell_text,
)

_T1_BRIEF = 'Karaoke altyazı, önemli kelimeler vurgulu; "community" ve "cringe" İngilizce yazılsın.'


def test_t1_brief_yields_the_two_quoted_words():
    assert extract_spelling_asks(_T1_BRIEF) == ("community", "cringe")


@pytest.mark.parametrize(
    "brief",
    [
        'title: "Welcome" in big font',  # on-screen text, not a spelling ask
        'altyazı olmasın "community" yazma',  # negated
        'spell "go" right',  # too short to be a safe target
    ],
)
def test_non_asks_extract_nothing(brief):
    assert extract_spelling_asks(brief) == ()


@pytest.mark.parametrize(
    ("heard", "written"),
    [
        ("Biz bir komünite yaratıyoruz.", "Biz bir community yaratıyoruz."),
        ("Hatta komünitenin adına", "Hatta community'nin adına"),
        ("komünitin", "community'nin"),
        ("Komüniti, evet", "Community, evet"),
        ("komünite'nin", "community'nin"),
    ],
)
def test_turkish_hearings_become_the_asked_spelling_with_their_ending(heard, written):
    assert respell_text(heard, ("community",)) == written


@pytest.mark.parametrize(
    "text",
    [
        "komünist partisi",  # a real word one sound away
        "kominist",
        "community community'nin",  # already right
        "Deniz denim deniz Denis",  # short target: no fuzzy match, no case-only edits
        "komünal bir yaşam",
    ],
)
def test_unrelated_or_already_right_words_are_left_alone(text):
    assert respell_text(text, ("community", "Deniz")) == text


def test_cue_text_and_word_texts_change_but_timings_do_not():
    cue = {
        "text": "komünitenin adı",
        "start_s": 1.0,
        "end_s": 2.0,
        "words": [
            {"text": "komünitenin", "start_s": 1.0, "end_s": 1.5},
            {"text": "adı", "start_s": 1.5, "end_s": 2.0},
        ],
    }
    [out] = apply_spelling_to_cues([cue], ("community",))
    assert out["text"] == "community'nin adı"
    assert out["words"][0] == {"text": "community'nin", "start_s": 1.0, "end_s": 1.5}
    assert cue["text"] == "komünitenin adı"  # input not mutated
