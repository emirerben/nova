"""Focused contract checks for word-timed text on recorded voiceovers.

Failure modes considered before implementation: timing read from captions rather
than narration; first repeat silently chosen; Turkish number words failing to
ground; abbreviated copy treated as equivalent speech; a long literal silently
truncated; and placement claiming it cleared an unknown face.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement
from app.kria.recipes import Canvas
from app.kria.spoken_text_ask import SpokenTextAsk, requirement_spoken_text
from app.pipeline import phone_narrated_spoken_text as subject


def _ask(text: str, *triggers: str) -> SpokenTextAsk:
    return SpokenTextAsk(requirement_id="ask-1", text=text, triggers=triggers)


def _rows(monkeypatch, ask: SpokenTextAsk, words: list[dict]):
    # The matching contract is independent of renderer geometry.  Keep this
    # test focused on the narrated helper's input/output boundary.
    monkeypatch.setattr(subject, "_text_boxes", lambda *args, **kwargs: [])
    monkeypatch.setattr(subject, "caption_boxes", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        subject,
        "_place_clear_of_text",
        lambda row, avoid, *, canvas, face_regions: (
            dict(row),
            {"status": "clear_of_text", "face_protection": "fallback"},
        ),
    )
    return subject.narrated_spoken_text_rows(
        (ask,),
        words=words,
        cues=[],
        caption_style="clean",
        timeline_duration_s=10,
        canvas=Canvas(width=1080, height=1920),
    )


def test_narrated_text_uses_the_original_voiceover_word_timing(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("RULE ONE", "leave before eleven"),
        [
            {"text": "leave", "start_s": 3.25, "end_s": 3.5},
            {"text": "before", "start_s": 3.5, "end_s": 3.8},
            {"text": "eleven", "start_s": 3.8, "end_s": 4.1},
        ],
    )

    assert rows[0]["text"] == "RULE ONE"
    assert rows[0]["start_s"] == 3.25
    assert receipt["rows"] == [
        {
            "requirement_id": "ask-1",
            "text": "RULE ONE",
            "triggers": ["leave before eleven"],
            "status": "placed",
            "trigger": "leave before eleven",
            "element_id": "spoken-text-ask-1",
            "start_s": 3.25,
            "end_s": 5.25,
            "placement": {"status": "clear_of_text", "face_protection": "fallback"},
        }
    ]


def test_repeated_unqualified_phrase_is_not_given_a_made_up_timing(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("RULE", "rule"),
        [
            {"text": "rule", "start_s": 1, "end_s": 1.2},
            {"text": "rule", "start_s": 4, "end_s": 4.2},
        ],
    )

    assert rows == []
    assert receipt["rows"][0]["status"] == "ambiguous"
    assert "start_s" not in receipt["rows"][0]


def test_explicit_occurrence_selects_the_requested_repeat(monkeypatch) -> None:
    ask = SpokenTextAsk(requirement_id="ask-1", text="RULE", triggers=("rule",), occurrence="last")
    rows, receipt = _rows(
        monkeypatch,
        ask,
        [
            {"text": "rule", "start_s": 1, "end_s": 1.2},
            {"text": "rule", "start_s": 4, "end_s": 4.2},
        ],
    )

    assert rows[0]["start_s"] == 4
    assert receipt["rows"][0]["occurrence"] == "last"


def test_occurrence_comes_only_from_the_structured_fact() -> None:
    """KRI-667: wording alone never picks a repeat; the brief's `facts.occurrence` does."""
    structured = BriefRequirement(
        id="structured",
        kind="text",
        scope="global",
        literal="RULE",
        description="show it when I say rule",
        facts={"triggers": ["rule"], "occurrence": 2},
    )
    worded = BriefRequirement(
        id="worded",
        kind="text",
        scope="global",
        literal="RULE",
        description="show it the last time I say rule",
        facts={"triggers": ["rule"]},
    )

    assert requirement_spoken_text(structured).occurrence == 2  # type: ignore[union-attr]
    assert requirement_spoken_text(worded).occurrence is None  # type: ignore[union-attr]


@pytest.mark.parametrize("occurrence", [True, False, [], {}, 0, -1, "second"])
def test_malformed_occurrence_fact_does_not_select_a_repeated_word(occurrence) -> None:
    req = BriefRequirement(
        id="bad-occurrence",
        kind="text",
        scope="global",
        literal="RULE",
        description="show it when I say rule",
        facts={"triggers": ["rule"], "occurrence": occurrence},
    )

    assert requirement_spoken_text(req).occurrence is None  # type: ignore[union-attr]


def test_named_speaker_is_honestly_unsupported_on_a_voiceover(monkeypatch) -> None:
    ask = SpokenTextAsk(requirement_id="ask-1", text="RULE", triggers=("rule",), speaker="she")
    rows, receipt = _rows(monkeypatch, ask, [{"text": "rule", "start_s": 1, "end_s": 1.2}])

    assert rows == []
    assert receipt["rows"][0]["status"] == "unsupported_speaker"


def test_turkish_number_words_match_digits_but_abbreviations_do_not(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("TWENTY", "20 dakika"),
        [
            {"text": "yirmi", "start_s": 2, "end_s": 2.2},
            {"text": "dakika", "start_s": 2.2, "end_s": 2.5},
        ],
    )
    assert rows[0]["start_s"] == 2
    assert receipt["rows"][0]["status"] == "placed"

    rows, receipt = _rows(
        monkeypatch,
        _ask("TWENTY", "20 dk"),
        [
            {"text": "yirmi", "start_s": 2, "end_s": 2.2},
            {"text": "dakika", "start_s": 2.2, "end_s": 2.5},
        ],
    )
    assert rows == []
    assert receipt["rows"][0]["status"] == "not_heard"


def test_too_long_text_is_refused_instead_of_silently_truncated(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("x" * 81, "hello"),
        [{"text": "hello", "start_s": 2, "end_s": 2.2}],
    )

    assert rows == []
    assert receipt["rows"][0]["status"] == "text_too_long"


def test_text_safe_placement_uses_a_fallback_face_without_claiming_a_sample(monkeypatch) -> None:
    ask = _ask("RULE", "rule")
    rows, receipt = subject.narrated_spoken_text_rows(
        (ask,),
        words=[{"text": "rule", "start_s": 1, "end_s": 1.2}],
        cues=[],
        caption_style="clean",
        timeline_duration_s=10,
        canvas=Canvas(width=1080, height=1920),
    )

    assert rows
    placed = receipt["rows"][0]
    assert placed["placement"]["face_protection"] == "fallback"
    assert "face_collision" not in placed


# PR #1758's N2 rules (KRI-695): each rule's brief names two cues for ONE moment, its number
# and its phrase ("1" and "leave before 11"). Failure modes considered before the change: the
# two cues counted as a repeat, so every rule asks "which occurrence"; the number winning over
# a heard phrase; an intro count ("Three rules") silently taken as rule three; a phrase said
# twice hidden by falling back to a number heard once; two different cues of the same length
# no longer asking; a contraction in the cue or in the recording ("you're" / "you are", also
# with a curly apostrophe) not matching its long form; "'s" / "'d" rewritten although each has
# two long forms; the long form moving the word's start; Turkish apostrophe suffixes changing.
def _word(text: str, start_s: float) -> dict:
    return {"text": text, "start_s": start_s, "end_s": start_s + 0.2}


def _spoken(start_s: float, *texts: str) -> list[dict]:
    return [_word(text, start_s + 0.2 * index) for index, text in enumerate(texts)]


def test_a_rule_number_and_its_phrase_name_one_moment_and_the_phrase_places_it(
    monkeypatch,
) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("1 · leave before 11", "1", "leave before 11"),
        [_word("1", 0.0), *_spoken(0.5, "leave", "before", "11")],
    )

    assert rows[0]["start_s"] == 0.5
    assert receipt["rows"][0]["status"] == "placed"
    assert receipt["rows"][0]["trigger"] == "leave before 11"


def test_the_rule_number_places_the_text_only_when_its_phrase_is_not_heard(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("1 · leave before 11", "1", "leave before 11"),
        _spoken(2.0, "Rule", "one,", "leave", "early"),
    )

    assert rows[0]["start_s"] == 2.2
    assert receipt["rows"][0]["trigger"] == "1"


def test_an_intro_count_and_a_numbered_rule_still_ask_which_one(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("3 · 20 minutes of darkness", "3", "20 minutes of darkness"),
        [*_spoken(0.0, "Three", "rules."), *_spoken(6.0, "Rule", "three.")],
    )

    assert rows == []
    assert receipt["rows"][0]["status"] == "ambiguous"


def test_a_repeated_phrase_is_not_hidden_behind_a_number_heard_once(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("1 · leave before 11", "1", "leave before 11"),
        [
            _word("1", 0.0),
            *_spoken(0.5, "leave", "before", "11"),
            *_spoken(5.0, "leave", "before", "11"),
        ],
    )

    assert rows == []
    assert receipt["rows"][0]["status"] == "ambiguous"


def test_two_different_cues_of_the_same_length_heard_apart_still_ask(monkeypatch) -> None:
    rows, receipt = _rows(
        monkeypatch,
        _ask("MEROPE", "Merope", "Caro"),
        [_word("Merope", 1.0), _word("Caro", 4.0)],
    )

    assert rows == []
    assert receipt["rows"][0]["status"] == "ambiguous"


@pytest.mark.parametrize(
    ("cue", "spoken"),
    [
        ("you're all amigos", ("you", "are", "all", "amigos")),
        ("you are all amigos", ("you're", "all", "amigos")),
        ("you’re all amigos", ("you", "are", "all", "amigos")),
        ("you're all amigos", ("You’re", "all", "amigos.")),
        ("we don't stop", ("we", "do", "not", "stop")),
    ],
)
def test_a_contraction_and_its_long_form_are_the_same_spoken_words(
    monkeypatch, cue, spoken
) -> None:
    rows, receipt = _rows(monkeypatch, _ask("2 · you're all amigos", cue), _spoken(4.5, *spoken))

    assert receipt["rows"][0]["status"] == "placed"
    assert rows[0]["start_s"] == 4.5


def test_an_apostrophe_s_with_two_long_forms_is_never_expanded(monkeypatch) -> None:
    rows, receipt = _rows(monkeypatch, _ask("LATE", "it is late"), _spoken(1.0, "it's", "late"))

    assert rows == []
    assert receipt["rows"][0]["status"] == "not_heard"


def test_a_turkish_apostrophe_suffix_still_folds_to_the_name(monkeypatch) -> None:
    rows, receipt = _rows(monkeypatch, _ask("ATAKAN", "Atakan"), _spoken(3.0, "Atakan'ı", "gördün"))

    assert receipt["rows"][0]["status"] == "placed"
    assert rows[0]["start_s"] == 3.0
