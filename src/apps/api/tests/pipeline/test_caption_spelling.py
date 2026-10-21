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


# KRI-793: several-word names from the approved brief ("Oblea de la Casa"). Café T1's own
# transcript wrote "Ole de la Casa" and "Meropa". Failure modes pinned: a phrase matched across
# punctuation or a different word count (timings would shift), an unrelated phrase rewritten,
# a case-only edit, the sentence-start capital lost.

_T1_SPELLINGS = ("Oblea de la Casa", "dulce de leche", "leche condensada", "Merope")


@pytest.mark.parametrize(
    ("heard", "written"),
    [
        ("And this is the Ole de la Casa.", "And this is the Oblea de la Casa."),
        ("where we are right now, Meropa, how", "where we are right now, Merope, how"),
        ("Dulse de leche, please", "Dulce de leche, please"),  # sentence-start capital kept
        ("cheese, dulce de leche,", "cheese, dulce de leche,"),  # already right
    ],
)
def test_several_word_names_are_respelled_as_the_creator_wrote_them(heard, written):
    assert respell_text(heard, _T1_SPELLINGS) == written


@pytest.mark.parametrize(
    "text",
    [
        "oblea de la casa",  # same letters, only the case differs: never a case-only edit
        "Ole, de la Casa",  # punctuation inside: not one spoken name
        "Obleadela Casa",  # another word count: word timings could not stay 1:1
        "Ole de la Costa",  # a different place
        "la casa de leche",
    ],
)
def test_other_phrases_are_left_alone(text):
    assert respell_text(text, _T1_SPELLINGS) == text


def test_a_phrase_spanning_timed_words_keeps_each_words_timing():
    cue = {
        "text": "the Ole de la Casa.",
        "words": [
            {"text": "the", "start_s": 14.9, "end_s": 15.2},
            {"text": "Ole", "start_s": 15.2, "end_s": 15.6},
            {"text": "de", "start_s": 15.6, "end_s": 15.8},
            {"text": "la", "start_s": 15.8, "end_s": 16.0},
            {"text": "Casa.", "start_s": 16.0, "end_s": 16.4},
        ],
    }
    [out] = apply_spelling_to_cues([cue], _T1_SPELLINGS)
    assert out["text"] == "the Oblea de la Casa."
    assert [w["text"] for w in out["words"]] == ["the", "Oblea", "de", "la", "Casa."]
    assert [(w["start_s"], w["end_s"]) for w in out["words"]] == [
        (w["start_s"], w["end_s"]) for w in cue["words"]
    ]


def test_punctuation_between_timed_words_breaks_the_phrase():
    cue = {
        "text": "Ole, de la Casa",
        "words": [
            {"text": "Ole,", "start_s": 0.0, "end_s": 0.4},
            {"text": "de", "start_s": 0.4, "end_s": 0.6},
            {"text": "la", "start_s": 0.6, "end_s": 0.8},
            {"text": "Casa", "start_s": 0.8, "end_s": 1.2},
        ],
    }
    assert apply_spelling_to_cues([cue], _T1_SPELLINGS) == [cue]


@pytest.mark.parametrize(
    ("shown", "target", "misheard"),
    [
        ("Meropa", "Merope", True),
        ("Ole de la Casa", "Oblea de la Casa", True),
        ("Merope", "Merope", False),
        ("MEROPE", "Merope", False),  # a capitals caption style is the same spelling
        ("Europe", "Merope", False),
        ("Ole de la Costa", "Oblea de la Casa", False),
        ("Oblea", "Oblea de la Casa", False),
    ],
)
def test_is_mishearing_matches_what_the_respelling_would_fix(shown, target, misheard):
    from app.pipeline.caption_spelling import is_mishearing

    assert is_mishearing(shown, target) is misheard


def test_two_phrases_sharing_a_word_keep_cue_text_and_words_in_step():
    cue = {
        "text": "dulse de leche condensata",
        "words": [
            {"text": "dulse", "start_s": 0.0, "end_s": 0.3},
            {"text": "de", "start_s": 0.3, "end_s": 0.4},
            {"text": "leche", "start_s": 0.4, "end_s": 0.7},
            {"text": "condensata", "start_s": 0.7, "end_s": 1.2},
        ],
    }
    [out] = apply_spelling_to_cues([cue], _T1_SPELLINGS)
    assert out["text"] == "dulce de leche condensada"
    assert " ".join(w["text"] for w in out["words"]) == out["text"]


@pytest.mark.parametrize(
    ("heard", "written"),
    [
        ("Ole de la Casa'da tatlı", "Oblea de la Casa'da tatlı"),  # the ending stays
        ("Meropeye gittik", "Merope'ye gittik"),  # right name, only the apostrophe missing
    ],
)
def test_turkish_endings_on_listed_names(heard, written):
    assert respell_text(heard, _T1_SPELLINGS) == written


def test_a_short_listed_word_is_never_fuzzy_matched():
    from app.pipeline.caption_spelling import is_mishearing

    assert respell_text("kasada ödedik", ("Casa",)) == "kasada ödedik"
    assert is_mishearing("kasa", "Casa") is False
