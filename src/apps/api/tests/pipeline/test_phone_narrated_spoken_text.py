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


def test_occurrence_comes_from_facts_or_explicit_first_last_wording() -> None:
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
    assert requirement_spoken_text(worded).occurrence == "last"  # type: ignore[union-attr]


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
