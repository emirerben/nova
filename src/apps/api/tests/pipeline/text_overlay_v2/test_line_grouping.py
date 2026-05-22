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
    n = DEFAULT_MAX_WORDS_PER_LINE + 1
    phrases = [_phrase(f"w{i}", i * 0.5, i * 0.5 + 0.3) for i in range(n)]
    transcript = [_tw(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(n)]
    groups = build_line_groups(phrases, transcript)
    # 9 words → groups [0..7] (8 words) then singleton [8] gets dropped under
    # min_group_size=2.
    assert len(groups) == 1
    assert len(groups[0].phrase_indices) == DEFAULT_MAX_WORDS_PER_LINE


# ── Unmatched phrases ────────────────────────────────────────────────────────


def test_unmatched_phrase_silence_gap_separates_groups():
    """Silence gap from "there" end (1.4s) to "hi" start (3.0s) is 1.6s,
    above the 0.7s default. The unmatched "PERU" gets included in the FIRST
    group (cumulative reveal includes its OCR text), then the silence gap
    closes that group before "hi" opens a new one.
    """
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
    # "PERU" is included in group 0 — its OCR text contributes to the
    # cumulative reveal, with start_t_s=2.0 as the reveal timing.
    assert groups[0].phrase_indices == [0, 1, 2]
    assert groups[0].transcript_word_indices == [0, 1, None]
    assert groups[0].word_start_s_list == [0.0, 1.0, 2.0]
    assert groups[1].phrase_indices == [3, 4]
    assert groups[1].transcript_word_indices == [2, 3]


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


# ── Unmatched phrases mid-group: skip, don't close ──────────────────────────


def test_unmatched_phrase_mid_group_included_in_cumulative():
    """Regression for prod job 09f56ee3 (2026-05-22): "the work to get there"
    only revealed up to "the work to get" because "there" was unmatched and
    closed the group. After the fix, unmatched phrases JOIN the group with
    their OCR text + start_t_s, so the cumulative reveal reaches the full
    visible phrase. Bonus coverage: an OCR artifact "Z" between matched
    words also joins (Stage D artifact filter usually strips these, but
    they're harmless if they slip through).
    """
    phrases = [
        _phrase("the", 4.5, 4.7),
        _phrase("work", 5.0, 5.2),
        _phrase("to", 5.5, 5.6),
        _phrase("get", 5.7, 5.8),
        _phrase("Z", 5.85, 5.86),  # OCR artifact — no transcript match
        _phrase("there", 5.9, 6.0),  # real word, transcript missed it
    ]
    transcript = [
        _tw("the", 4.5, 4.8),
        _tw("work", 5.0, 5.3),
        _tw("to", 5.5, 5.6),
        _tw("get", 5.7, 5.8),
        # No "Z" (artifact) and no "there" (real-word miss) in transcript.
    ]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 1, "OCR artifact must not fragment the cumulative reveal"
    # All six phrases are in the group — matched and unmatched alike.
    assert groups[0].phrase_indices == [0, 1, 2, 3, 4, 5]
    assert groups[0].transcript_word_indices == [0, 1, 2, 3, None, None]
    # Word start times: transcript start_s for matched, OCR start_t_s for unmatched.
    assert groups[0].word_start_s_list == [4.5, 5.0, 5.5, 5.7, 5.85, 5.9]


def test_unmatched_phrase_does_not_block_silence_gap_closure():
    """Silence gap between MATCHED transcript words still closes the group,
    even with unmatched phrases sitting inside. The unmatched phrase joins
    the group it visually belongs to (the FIRST one — same continuous time
    window), then silence gap closes the group before the next match opens
    a new one.
    """
    phrases = [
        _phrase("hello", 0.0, 0.3),
        _phrase("there", 0.5, 0.7),
        _phrase("Q", 1.5, 1.6),  # artifact during the silence
        _phrase("again", 3.0, 3.3),
        _phrase("now", 3.5, 3.7),
    ]
    transcript = [
        _tw("hello", 0.0, 0.4),
        _tw("there", 0.5, 0.8),
        _tw("again", 3.0, 3.4),
        _tw("now", 3.5, 3.8),
    ]
    groups = build_line_groups(phrases, transcript)
    # Silence gap 3.0 - 0.8 = 2.2s > 0.7 default → close after group 1.
    # "Q" joins group 1 (no boundary check applies to unmatched).
    assert len(groups) == 2
    assert groups[0].phrase_indices == [0, 1, 2]
    assert groups[0].transcript_word_indices == [0, 1, None]
    assert groups[1].phrase_indices == [3, 4]


def test_unmatched_phrase_does_not_block_sentence_terminator():
    """Sentence terminator between matched words still closes. The unmatched
    'X' joins the FIRST group (with the matching word right before the
    terminator), then the next matched phrase opens a new group. Singletons
    drop under default min_group_size, so the test asserts ONE group of two.
    """
    phrases = [
        _phrase("done", 0.0, 0.3),
        _phrase("X", 0.4, 0.5),  # artifact between sentences
        _phrase("now", 1.0, 1.3),
        _phrase("here", 1.5, 1.7),  # second matched word so group >= min size
    ]
    transcript = [
        _tw("done.", 0.0, 0.4),
        _tw("now", 1.0, 1.4),
        _tw("here", 1.5, 1.8),
    ]
    groups = build_line_groups(phrases, transcript)
    # 'done.' terminator splits between phrase 0 and phrase 2. "X" joins
    # group 1 ([done, X]); group 2 ([now, here]) starts fresh after the
    # sentence break.
    assert len(groups) == 2
    assert groups[0].phrase_indices == [0, 1]
    assert groups[0].transcript_word_indices == [0, None]
    assert groups[1].phrase_indices == [2, 3]
    assert groups[1].transcript_word_indices == [1, 2]


def test_leading_unmatched_phrases_dont_consume_group_state():
    """Unmatched phrases BEFORE the first match should be silently skipped
    without affecting the eventual group that forms."""
    phrases = [
        _phrase("LOGO", 0.0, 0.1),  # artifact at the start
        _phrase("X", 0.1, 0.2),  # artifact
        _phrase("hello", 0.5, 0.7),
        _phrase("world", 1.0, 1.2),
    ]
    transcript = [
        _tw("hello", 0.5, 0.8),
        _tw("world", 1.0, 1.3),
    ]
    groups = build_line_groups(phrases, transcript)
    assert len(groups) == 1
    assert groups[0].phrase_indices == [2, 3]
