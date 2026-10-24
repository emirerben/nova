"""Pure, source-timeline cuts for a creator's quoted spoken phrase."""

from __future__ import annotations

import pytest

from app.pipeline.explicit_speech_cut import ExplicitSpeechCutRequest, build_explicit_speech_cut
from app.pipeline.silence_cut import Removal, remap_words

WORDS = [
    {"text": "Başla", "start_s": 0.5, "end_s": 1.0},
    {"text": "Neredesin", "start_s": 6.32, "end_s": 7.34},
    {"text": "orada", "start_s": 7.5, "end_s": 7.9},
    {"text": "Neredesin?", "start_s": 9.22, "end_s": 9.70},
    {"text": "buradayım", "start_s": 9.8, "end_s": 10.3},
]


def test_second_duplicate_phrase_cuts_the_original_second_source_word() -> None:
    result = build_explicit_speech_cut(
        WORDS,
        ExplicitSpeechCutRequest(phrase="NEREDESİN", occurrence=2),
        duration_s=14.0,
    )

    assert result.plan.keep_segments == [(0.0, 9.22), (9.7, 14.0)]
    assert [(item.start_s, item.end_s) for item in result.plan.removed] == [(9.22, 9.7)]
    assert result.receipt == {
        "reason": "explicit_speech_phrase_removed_now",
        "status": "applied",
        "phrase": "NEREDESİN",
        "occurrence": 2,
        "source_span": {"start_s": 9.22, "end_s": 9.7},
        "source_start_s": 9.22,
        "source_end_s": 9.7,
        "word_indexes": [3],
    }


def test_duplicate_phrase_without_occurrence_is_refused() -> None:
    with pytest.raises(ValueError, match="ambiguous_phrase_occurrence"):
        build_explicit_speech_cut(
            WORDS, ExplicitSpeechCutRequest(phrase="Neredesin"), duration_s=14.0
        )


@pytest.mark.parametrize("occurrence", [0, -1, True, "second"])
def test_unrecognized_or_invalid_occurrence_is_refused(occurrence: object) -> None:
    with pytest.raises(ValueError, match="invalid_phrase_occurrence"):
        build_explicit_speech_cut(
            WORDS,
            ExplicitSpeechCutRequest(phrase="Neredesin", occurrence=occurrence),
            duration_s=14.0,
        )


def test_cut_composes_with_prior_removals_without_double_counting_and_remaps_words() -> None:
    existing = build_explicit_speech_cut(
        WORDS,
        ExplicitSpeechCutRequest(phrase="Başla"),
        duration_s=14.0,
    ).plan
    existing.removed.append(Removal(12.0, 13.0, "prior_cleanup"))
    existing.keep_segments = [(0.0, 0.5), (1.0, 12.0), (13.0, 14.0)]

    result = build_explicit_speech_cut(
        WORDS,
        ExplicitSpeechCutRequest(phrase="Neredesin", occurrence=2),
        duration_s=14.0,
        existing_plan=existing,
    )

    assert [(item.start_s, item.end_s) for item in result.plan.removed] == [
        (0.5, 1.0),
        (9.22, 9.7),
        (12.0, 13.0),
    ]
    assert result.plan.keep_segments == [(0.0, 0.5), (1.0, 9.22), (9.7, 12.0), (13.0, 14.0)]
    assert result.plan.time_saved_s == pytest.approx(1.98)
    assert [word["text"] for word in remap_words(WORDS, result.plan)] == [
        "Neredesin",
        "orada",
        "buradayım",
    ]
    assert remap_words(WORDS, result.plan)[-1]["text"] == "buradayım"
    assert remap_words(WORDS, result.plan)[-1]["start_s"] == pytest.approx(8.82)
    assert remap_words(WORDS, result.plan)[-1]["end_s"] == pytest.approx(9.32)


def test_receipt_proves_phrase_was_already_removed() -> None:
    existing = build_explicit_speech_cut(
        WORDS,
        ExplicitSpeechCutRequest(phrase="Neredesin", occurrence=2),
        duration_s=14.0,
    ).plan

    result = build_explicit_speech_cut(
        WORDS,
        ExplicitSpeechCutRequest(phrase="Neredesin", occurrence=2),
        duration_s=14.0,
        existing_plan=existing,
    )

    assert result.receipt["status"] == "applied"
    assert result.receipt["reason"] == "explicit_speech_phrase_already_removed"
    assert result.plan.time_saved_s == pytest.approx(existing.time_saved_s)


@pytest.mark.parametrize(
    "words",
    [
        [{"text": "Neredesin", "start_s": 1.0, "end_s": 1.0}],
        [{"text": "Neredesin", "start_s": float("nan"), "end_s": 2.0}],
    ],
)
def test_invalid_selected_source_timings_are_refused(words: list[dict]) -> None:
    with pytest.raises(ValueError, match="invalid_source_word_timing"):
        build_explicit_speech_cut(
            words, ExplicitSpeechCutRequest(phrase="Neredesin"), duration_s=14.0
        )


def test_unrelated_zero_duration_saved_word_does_not_block_the_explicit_cut() -> None:
    result = build_explicit_speech_cut(
        [
            {"text": "mi", "start_s": 14.54, "end_s": 14.54},
            {"text": "Neredesin", "start_s": 15.0, "end_s": 15.4},
            {"text": "Mesaj", "start_s": 18.38, "end_s": 18.38},
        ],
        ExplicitSpeechCutRequest(phrase="Neredesin"),
        duration_s=20.0,
    )

    assert result.receipt["source_span"] == {"start_s": 15.0, "end_s": 15.4}


def test_overlapping_unselected_word_is_refused_to_preserve_neighbour_speech() -> None:
    with pytest.raises(ValueError, match="ambiguous_source_word_overlap"):
        build_explicit_speech_cut(
            [
                {"text": "Neredesin", "start_s": 9.22, "end_s": 9.7},
                {"text": "off camera", "start_s": 9.5, "end_s": 10.0},
            ],
            ExplicitSpeechCutRequest(phrase="Neredesin"),
            duration_s=14.0,
        )


def test_source_timing_precision_is_not_rounded_before_the_cut_plan() -> None:
    result = build_explicit_speech_cut(
        [{"text": "Neredesin", "start_s": 9.22222, "end_s": 9.70007}],
        ExplicitSpeechCutRequest(phrase="Neredesin"),
        duration_s=14.0,
    )

    assert result.plan.removed[0].start_s == 9.22222
    assert result.plan.removed[0].end_s == 9.70007


def test_low_confidence_selected_word_is_refused() -> None:
    with pytest.raises(ValueError, match="low_confidence_source_phrase"):
        build_explicit_speech_cut(
            [
                {
                    "text": "Neredesin",
                    "start_s": 9.22,
                    "end_s": 9.7,
                    "confidence": 0.49,
                }
            ],
            ExplicitSpeechCutRequest(phrase="Neredesin"),
            duration_s=14.0,
        )


def test_cut_that_would_leave_less_than_two_seconds_is_refused() -> None:
    existing = build_explicit_speech_cut(
        [{"text": "keep", "start_s": 0.0, "end_s": 0.5}],
        ExplicitSpeechCutRequest(phrase="keep"),
        duration_s=10.0,
    ).plan
    existing.removed = [Removal(0.0, 8.0, "prior_cleanup")]
    existing.keep_segments = [(8.0, 10.0)]

    with pytest.raises(ValueError, match="explicit_speech_cut_output_too_short"):
        build_explicit_speech_cut(
            [{"text": "remove", "start_s": 8.5, "end_s": 9.0}],
            ExplicitSpeechCutRequest(phrase="remove"),
            duration_s=10.0,
            existing_plan=existing,
        )


@pytest.mark.parametrize("source,phrase_request", [("kâr", "kar"), ("sık", "sik"), ("three", "3")])
def test_destructive_match_does_not_fold_distinct_words(source, phrase_request):
    with pytest.raises(ValueError, match="phrase_not_found"):
        build_explicit_speech_cut(
            [{"text": source, "start_s": 1.0, "end_s": 2.0}],
            ExplicitSpeechCutRequest(phrase_request),
            duration_s=5.0,
        )
