"""Unit tests for app.pipeline.text_overlay_v2.line_grouping.

Covers the rules listed in the module docstring: atomized-only detection,
transcript matching, sentence-terminator boundary, silence-gap boundary,
max-words cap, unmatched-phrase boundary, and group-size threshold.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_overlay_pipeline import Phrase
from app.pipeline.text_overlay_v2.line_grouping import (
    DEFAULT_MAX_WORDS_PER_LINE,
    LineGroup,
    build_line_groups,
)


def _phrase(text: str, start_s: float, end_s: float, x_min: float = 0.1) -> Phrase:
    """Build an atomized Phrase covering one word at a known left-x."""
    return Phrase(
        lines=[text],
        start_t_s=start_s,
        end_t_s=end_s,
        aabb=(x_min, 0.7, x_min + 0.2, 0.85),
        mean_confidence=0.9,
    )


def _tw(text: str, start_s: float, end_s: float) -> dict:
    return {"text": text, "start_s": start_s, "end_s": end_s}


# ── Empty / degenerate inputs ────────────────────────────────────────────────


def test_empty_phrases_returns_empty():
    assert build_line_groups([], [_tw("hi", 0.0, 1.0)]) == []


def test_empty_transcript_returns_empty():
    # No transcript = no line source for grouping. Every phrase stays ungrouped.
    assert build_line_groups([_phrase("hi", 0.0, 1.0)], []) == []


def test_no_atomized_phrase_returns_empty():
    multi = Phrase(
        lines=["hello", "world"],  # multi-line, not atomized
        start_t_s=0.0,
        end_t_s=2.0,
        aabb=(0.1, 0.7, 0.5, 0.9),
        mean_confidence=0.9,
    )
    assert build_line_groups([multi], [_tw("hello", 0.0, 1.0), _tw("world", 1.0, 2.0)]) == []


# ── Happy path ───────────────────────────────────────────────────────────────


def test_three_aligned_words_become_one_group():
    phrases = [
        _phrase("good", 0.05, 0.5, x_min=0.1),
        _phrase("morning", 1.05, 1.5),
        _phrase("everyone", 2.05, 2.5),
    ]
    transcript = [
        _tw("good", 0.0, 1.0),
        _tw("morning", 1.0, 2.0),
        _tw("everyone", 2.0, 3.0),
    ]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 1
    g = groups[0]
    assert isinstance(g, LineGroup)
    assert g.phrase_indices == [0, 1, 2]
    assert g.transcript_word_indices == [0, 1, 2]
    assert g.line_end_s == pytest.approx(3.0)
    # Left anchor comes from the FIRST phrase's bbox x_min.
    assert g.line_anchor_x_frac == pytest.approx(0.1)


def test_left_anchor_comes_from_first_phrases_bbox():
    phrases = [
        _phrase("hello", 0.0, 0.5, x_min=0.3),
        _phrase("world", 1.0, 1.5, x_min=0.5),
    ]
    transcript = [_tw("hello", 0.0, 0.6), _tw("world", 1.0, 1.6)]
    groups = build_line_groups(phrases, transcript)
    assert groups[0].line_anchor_x_frac == pytest.approx(0.3)


# ── Boundaries ───────────────────────────────────────────────────────────────


def test_sentence_terminator_splits_into_two_groups():
    phrases = [
        _phrase("hello", 0.05, 0.5),
        _phrase("there", 1.05, 1.5),
        _phrase("how", 2.55, 3.0),
        _phrase("are", 3.55, 4.0),
        _phrase("you", 4.55, 5.0),
    ]
    transcript = [
        _tw("hello", 0.0, 0.6),
        _tw("there.", 1.0, 1.6),  # sentence-terminator on this word
        _tw("how", 2.5, 3.1),
        _tw("are", 3.5, 4.1),
        _tw("you", 4.5, 5.1),
    ]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 2
    assert groups[0].phrase_indices == [0, 1]
    assert groups[1].phrase_indices == [2, 3, 4]


def test_question_mark_terminator_splits():
    phrases = [
        _phrase("hi", 0.0, 0.3),
        _phrase("yes", 1.0, 1.3),
    ]
    transcript = [_tw("hi?", 0.0, 0.4), _tw("yes", 1.0, 1.4)]
    groups = build_line_groups(phrases, transcript)
    # `hi?` terminates the sentence between phrase 0 and phrase 1.
    # Result: two singleton candidates, but min_group_size=2 filters both out.
    assert groups == []


def test_silence_gap_splits():
    phrases = [
        _phrase("good", 0.0, 0.3),
        _phrase("morning", 0.6, 0.9),
        # Big silence gap before "everyone"
        _phrase("everyone", 5.0, 5.3),
        _phrase("today", 5.6, 5.9),
    ]
    transcript = [
        _tw("good", 0.0, 0.4),
        _tw("morning", 0.6, 1.0),
        _tw("everyone", 5.0, 5.4),  # 4.0s silence after `morning`
        _tw("today", 5.6, 6.0),
    ]
    groups = build_line_groups(phrases, transcript, silence_gap_s=0.7)
    assert len(groups) == 2
    assert groups[0].phrase_indices == [0, 1]
    assert groups[1].phrase_indices == [2, 3]


def test_silence_gap_under_threshold_keeps_one_group():
    phrases = [
        _phrase("good", 0.0, 0.3),
        _phrase("morning", 1.0, 1.3),
    ]
    transcript = [
        _tw("good", 0.0, 0.4),
        _tw("morning", 1.0, 1.4),  # 0.6s gap < default 0.7s
    ]
    groups = build_line_groups(phrases, transcript, silence_gap_s=0.7)
    assert len(groups) == 1


def test_max_words_cap_splits_long_lines():
    # 10 atomized phrases that would otherwise form one group; cap at 4.
    phrases = [_phrase(f"w{i}", i * 0.5, i * 0.5 + 0.3) for i in range(10)]
    transcript = [_tw(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(10)]
    groups = build_line_groups(phrases, transcript, max_words_per_line=4)
    # 10 / 4 = 3 groups: [0,1,2,3], [4,5,6,7], [8,9]. The third is size 2 (>=
    # min_group_size=2 default).
    assert [len(g.phrase_indices) for g in groups] == [4, 4, 2]


def test_max_words_cap_at_default_8():
    phrases = [_phrase(f"w{i}", i * 0.5, i * 0.5 + 0.3) for i in range(DEFAULT_MAX_WORDS_PER_LINE + 1)]
    transcript = [_tw(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(DEFAULT_MAX_WORDS_PER_LINE + 1)]
    groups = build_line_groups(phrases, transcript)
    # 9 words → groups [0..7] (8 words) then singleton [8] gets dropped under
    # min_group_size=2.
    assert len(groups) == 1
    assert len(groups[0].phrase_indices) == DEFAULT_MAX_WORDS_PER_LINE


# ── Unmatched phrases ────────────────────────────────────────────────────────


def test_unmatched_phrase_closes_group_and_is_omitted():
    # Phrase "PERU" has no transcript match → ungrouped. The surrounding
    # matched phrases form their own group (if size >= 2).
    phrases = [
        _phrase("hello", 0.0, 0.3),
        _phrase("there", 1.0, 1.3),
        _phrase("PERU", 2.0, 2.3),  # visual-only label, no transcript match
        _phrase("hi", 3.0, 3.3),
        _phrase("again", 4.0, 4.3),
    ]
    transcript = [
        _tw("hello", 0.0, 0.4),
        _tw("there", 1.0, 1.4),
        _tw("hi", 3.0, 3.4),
        _tw("again", 4.0, 4.4),
    ]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 2
    assert groups[0].phrase_indices == [0, 1]
    assert groups[1].phrase_indices == [3, 4]
    # Phrase 2 ("PERU") is not in any group → Stage G passes it through.


def test_singleton_match_is_dropped_below_min_group_size():
    phrases = [
        _phrase("alone", 0.0, 0.3),
    ]
    transcript = [_tw("alone", 0.0, 0.4)]
    # Default min_group_size=2; this 1-word match doesn't form a group.
    assert build_line_groups(phrases, transcript) == []


def test_min_group_size_override_keeps_singletons():
    phrases = [_phrase("alone", 0.0, 0.3)]
    transcript = [_tw("alone", 0.0, 0.4)]
    groups = build_line_groups(phrases, transcript, min_group_size=1)
    assert len(groups) == 1
    assert groups[0].phrase_indices == [0]


# ── Word matching robustness ─────────────────────────────────────────────────


def test_punctuation_in_transcript_word_still_matches():
    phrases = [_phrase("hello", 0.0, 0.3), _phrase("world", 1.0, 1.3)]
    transcript = [_tw('"hello,"', 0.0, 0.4), _tw("world!", 1.0, 1.4)]
    groups = build_line_groups(phrases, transcript)
    # "world!" has a terminator — so it should split. But it's the LAST word,
    # so there's no group break to detect; the group remains [hello, world].
    # Actually: punctuation lies on the SECOND word, which terminates AFTER
    # that word. There's no third word, so no split needed.
    assert len(groups) == 1
    assert groups[0].phrase_indices == [0, 1]


def test_casefold_match():
    phrases = [_phrase("GOOD", 0.0, 0.3), _phrase("morning", 1.0, 1.3)]
    transcript = [_tw("good", 0.0, 0.4), _tw("Morning", 1.0, 1.4)]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 1


def test_pydantic_transcript_word_input_works():
    """Accepts Pydantic TranscriptWord instances, not just dicts."""
    from app.agents._schemas.text_alignment import TranscriptWord  # noqa: PLC0415

    phrases = [_phrase("hi", 0.0, 0.3), _phrase("there", 1.0, 1.3)]
    transcript = [
        TranscriptWord(text="hi", start_s=0.0, end_s=0.4),
        TranscriptWord(text="there", start_s=1.0, end_s=1.4),
    ]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 1


def test_repeated_word_in_ocr_consumes_distinct_transcript_words():
    """Two OCR phrases for the SAME word match DIFFERENT transcript words
    (the closest unmatched one each)."""
    phrases = [
        _phrase("rain", 0.0, 0.3),
        _phrase("rain", 1.0, 1.3),
        _phrase("rain", 2.0, 2.3),
    ]
    transcript = [
        _tw("rain", 0.0, 0.4),
        _tw("rain", 1.0, 1.4),
        _tw("rain", 2.0, 2.4),
    ]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 1
    assert groups[0].transcript_word_indices == [0, 1, 2]


def test_phrase_far_from_any_transcript_match_is_ungrouped():
    """OCR phrase appears at t=10s but the same word in transcript is at t=0s
    (15s+ apart). Out of MATCH_TIME_TOLERANCE_S range → no match."""
    phrases = [
        _phrase("hello", 10.0, 10.3),
        _phrase("there", 11.0, 11.3),
    ]
    transcript = [_tw("hello", 0.0, 0.4), _tw("there", 1.0, 1.4)]
    groups = build_line_groups(phrases, transcript)
    # Both phrases too far from their transcript matches → ungrouped.
    assert groups == []
