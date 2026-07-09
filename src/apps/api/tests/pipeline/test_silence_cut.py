"""Unit matrix for the pure silence/filler/retake cut-plan module (plans/010 T1).

No ffmpeg, no network, no API keys — everything here is arithmetic on word
timings and silence ranges. Covers: the universal filler lexicon (incl.
Turkish characters, elongation collapse, punctuation), segment-signal guards,
acoustic-gap classification + the zero-silence calibration gate, the pause
intersection rule (D16: no silencedetect agreement ⇒ no cut), leading/trailing
trims, merge + MIN_CUT_S hygiene, every safety-rail bailout, retake-span
snapping (never mid-word), remap_words exactness, no-op identity, and a
seeded property-style sweep over random layouts.
"""

import dataclasses
import random
from types import SimpleNamespace

import pytest

import app.pipeline.silence_cut as silence_cut
from app.pipeline.silence_cut import (
    ACOUSTIC_GAP_MAX_S,
    BAILOUT_CLIP_TOO_SHORT,
    BAILOUT_MAX_REMOVAL,
    BAILOUT_NO_WORDS,
    BAILOUT_OUTPUT_TOO_SHORT,
    KEPT_GAP_S,
    MAX_REMOVAL_FRAC,
    MIN_CLIP_S,
    MIN_CUT_S,
    MIN_OUTPUT_S,
    PAD_ACOUSTIC_S,
    PAD_S,
    REASON_FILLER_ACOUSTIC,
    REASON_FILLER_LEXICAL,
    REASON_RETAKE,
    REASON_SILENCE,
    CutPlan,
    Removal,
    build_cut_plan,
    is_filler_token,
    no_op_plan,
    remap_words,
)

DUR = 20.0


def w(text: str, start: float, end: float, **extra) -> dict:
    return {"text": text, "start_s": start, "end_s": end, **extra}


def lexical_fixture(filler: str = "um", **extra) -> list[dict]:
    """so [1.0,1.3] · FILLER [2.0,2.4] · lets [3.0,3.3] — with silences=[],
    only rule 1 can fire (rule 2 is calibration-gated, rule 3 needs silence)."""
    return [w("so", 1.0, 1.3), w(filler, 2.0, 2.4, **extra), w("lets", 3.0, 3.3)]


def only_removal(plan: CutPlan) -> Removal:
    assert plan.bailout_reason is None
    assert len(plan.removed) == 1, plan.removed
    return plan.removed[0]


def assert_spans(actual: list[tuple[float, float]], expected: list[tuple[float, float]]):
    assert len(actual) == len(expected), (actual, expected)
    for (a_lo, a_hi), (e_lo, e_hi) in zip(actual, expected):
        assert a_lo == pytest.approx(e_lo, abs=1e-9)
        assert a_hi == pytest.approx(e_hi, abs=1e-9)


def assert_no_cuts(plan: CutPlan, duration: float = DUR):
    assert plan.bailout_reason is None
    assert plan.removed == []
    assert_spans(plan.keep_segments, [(0.0, duration)])
    assert plan.time_saved_s == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------------
# Filler lexicon (is_filler_token)
# ---------------------------------------------------------------------------------


class TestFillerLexicon:
    @pytest.mark.parametrize(
        "token",
        [
            # exact lexicon members
            "uh",
            "um",
            "er",
            "erm",
            "hmm",
            "mm",
            "mhm",
            "ıı",
            "ııı",
            "eee",
            "aaa",
            "ıh",
            # case + punctuation normalization
            "Um",
            "UH,",
            "uh...",
            "Erm",
            "ıh.",
            "Hmm!",
            # elongation collapse (runs cap at the lexicon form)
            "uhhh",
            "uhhhh",
            "ummm",
            "ummmm",
            "mmm",
            "hmmm",
            "mhmm",
            # Turkish elongations
            "ıııı",
            "ııııı",
            "eeee",
            "aaaa",
        ],
    )
    def test_filler_tokens_match(self, token):
        assert is_filler_token(token), token

    @pytest.mark.parametrize(
        "token",
        [
            # real Turkish exclamations — deliberately excluded
            "ee",
            "aa",
            # sub-lexicon fragments never match
            "e",
            "a",
            "m",
            "ı",
            "hm",
            # real words (never cut in v1)
            "like",
            "şey",
            "you",
            "know",
            "hello",
            "eh",
            "era",
            "umbrella",
            "hummus",
            # junk
            "",
            "   ",
            "123",
            "?!",
        ],
    )
    def test_non_fillers_do_not_match(self, token):
        assert not is_filler_token(token), token


# ---------------------------------------------------------------------------------
# Rule 1 — lexical filler cuts
# ---------------------------------------------------------------------------------


class TestLexicalRule:
    def test_filler_cut_span_reason_and_keep(self):
        plan = build_cut_plan(lexical_fixture(), [], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_FILLER_LEXICAL
        assert removal.start_s == pytest.approx(2.0 - PAD_S, abs=1e-9)
        assert removal.end_s == pytest.approx(2.4 + PAD_S, abs=1e-9)
        assert_spans(plan.keep_segments, [(0.0, 1.88), (2.52, DUR)])
        assert plan.time_saved_s == pytest.approx(0.64, abs=1e-9)
        assert plan.version == 1

    @pytest.mark.parametrize("filler", ["uhhh", "ıııı", "Um,", "eee"])
    def test_variant_forms_cut(self, filler):
        plan = build_cut_plan(lexical_fixture(filler), [], DUR)
        assert only_removal(plan).reason == REASON_FILLER_LEXICAL

    @pytest.mark.parametrize("word", ["like", "şey", "ee", "aa", "hello"])
    def test_real_words_and_exclamations_not_cut(self, word):
        assert_no_cuts(build_cut_plan(lexical_fixture(word), [], DUR))

    def test_removal_never_eats_adjacent_words(self):
        words = [w("so", 1.0, 1.95), w("um", 2.0, 2.4), w("lets", 2.45, 2.9)]
        removal = only_removal(build_cut_plan(words, [], DUR))
        # pad would reach 1.88 / 2.52, but neighbors clamp it
        assert removal.start_s == pytest.approx(1.95, abs=1e-9)
        assert removal.end_s == pytest.approx(2.45, abs=1e-9)

    def test_filler_first_word_clamps_to_clip_start(self):
        words = [w("um", 0.05, 0.4), w("go", 1.0, 1.4)]
        removal = only_removal(build_cut_plan(words, [], DUR))
        assert removal.start_s == pytest.approx(0.0, abs=1e-9)
        assert removal.end_s == pytest.approx(0.4 + PAD_S, abs=1e-9)

    def test_accepts_object_shaped_words(self):
        words = [
            SimpleNamespace(text="so", start_s=1.0, end_s=1.3),
            SimpleNamespace(text="um", start_s=2.0, end_s=2.4),
            SimpleNamespace(text="lets", start_s=3.0, end_s=3.3),
        ]
        removal = only_removal(build_cut_plan(words, [], DUR))
        assert removal.reason == REASON_FILLER_LEXICAL


class TestSegmentSignalGuard:
    @pytest.mark.parametrize(
        ("extra", "expect_cut"),
        [
            ({}, True),  # no signals present → None never blocks
            ({"segment_avg_logprob": -1.5}, False),  # below AVG_LOGPROB_MIN
            ({"segment_avg_logprob": -1.0}, True),  # at threshold → allowed
            ({"segment_avg_logprob": -0.2}, True),
            ({"segment_no_speech_prob": 0.8}, False),  # above NO_SPEECH_PROB_MAX
            ({"segment_no_speech_prob": 0.5}, True),  # at threshold → allowed
            ({"segment_no_speech_prob": 0.1}, True),
            ({"confidence": 0.4}, False),  # real faster-whisper value below 0.5
            ({"confidence": 0.5}, True),
            ({"confidence": 1.0}, True),  # whisper-1 hardcodes 1.0 → not a signal
            ({"segment_avg_logprob": -0.3, "segment_no_speech_prob": 0.9}, False),
        ],
    )
    def test_guard_blocks_or_allows_lexical_cut(self, extra, expect_cut):
        plan = build_cut_plan(lexical_fixture("um", **extra), [], DUR)
        if expect_cut:
            assert only_removal(plan).reason == REASON_FILLER_LEXICAL
        else:
            assert_no_cuts(plan)


# ---------------------------------------------------------------------------------
# Rule 2 — acoustic fillers
# ---------------------------------------------------------------------------------

# A tiny silence INSIDE a word keeps the silence list non-empty (calibration
# gate open) without ever intersecting an inter-word gap.
INERT_SILENCE = [(1.05, 1.1)]


class TestAcousticRule:
    def test_soundful_gap_cut_with_acoustic_pads(self):
        words = [w("one", 1.0, 1.5), w("two", 2.3, 2.8), w("three", 2.9, 3.4)]
        removal = only_removal(build_cut_plan(words, INERT_SILENCE, DUR))
        assert removal.reason == REASON_FILLER_ACOUSTIC
        assert removal.start_s == pytest.approx(1.5 + PAD_ACOUSTIC_S, abs=1e-9)
        assert removal.end_s == pytest.approx(2.3 - PAD_ACOUSTIC_S, abs=1e-9)

    def test_gap_at_max_bound_still_cut(self):
        words = [w("one", 1.0, 1.5), w("two", 1.5 + ACOUSTIC_GAP_MAX_S, 3.2)]
        removal = only_removal(build_cut_plan(words, INERT_SILENCE, DUR))
        assert removal.reason == REASON_FILLER_ACOUSTIC

    def test_gap_longer_than_max_left_alone(self):
        # laughter/singing/action noise — soundful but > ACOUSTIC_GAP_MAX_S
        words = [w("one", 1.0, 1.5), w("two", 2.8, 3.3)]
        assert_no_cuts(build_cut_plan(words, INERT_SILENCE, DUR))

    def test_gap_below_min_no_cut(self):
        words = [w("one", 1.0, 1.5), w("two", 1.6, 2.1)]
        assert_no_cuts(build_cut_plan(words, INERT_SILENCE, DUR))

    def test_silence_covered_gap_attributed_to_pause_rule(self):
        words = [w("one", 1.0, 1.5), w("two", 2.3, 2.8)]
        plan = build_cut_plan(words, [(1.5, 2.3)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE  # rule 3, not rule 2
        assert removal.start_s == pytest.approx(1.5 + KEPT_GAP_S / 2, abs=1e-9)
        assert removal.end_s == pytest.approx(2.3 - KEPT_GAP_S / 2, abs=1e-9)

    def test_partial_silence_overlap_blocks_acoustic(self):
        # any silence overlap disqualifies rule 2; the rule-3 intersection here
        # is a sub-MIN_CUT_S sliver, so the net result is no cut at all
        words = [w("one", 1.0, 1.5), w("two", 2.3, 2.8)]
        assert_no_cuts(build_cut_plan(words, [(1.5, 1.7)], DUR))

    def test_calibration_gate_empty_silences_disables_rule_2_only(self):
        words = [
            w("so", 1.0, 1.5),
            w("two", 2.3, 2.8),  # 0.8s soundful gap — rule 2 shaped
            w("um", 3.5, 3.9),  # lexical filler — rule 1 must still fire
            w("end", 4.5, 4.9),
        ]
        plan = build_cut_plan(words, [], DUR)
        assert [r.reason for r in plan.removed] == [REASON_FILLER_LEXICAL]


# ---------------------------------------------------------------------------------
# Rule 3 — pause tightening (dual-signal intersection)
# ---------------------------------------------------------------------------------


class TestPauseRule:
    def test_silent_pause_tightened_kept_gap_arithmetic(self):
        words = [w("one", 1.0, 1.5), w("two", 3.0, 3.5)]
        plan = build_cut_plan(words, [(1.5, 3.0)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE
        assert removal.start_s == pytest.approx(1.5 + KEPT_GAP_S / 2, abs=1e-9)
        assert removal.end_s == pytest.approx(3.0 - KEPT_GAP_S / 2, abs=1e-9)
        assert_spans(plan.keep_segments, [(0.0, 1.625), (2.875, DUR)])
        assert plan.time_saved_s == pytest.approx(1.25, abs=1e-9)

    def test_gap_without_silence_agreement_never_cut(self):
        # D16: whisper end times drift — word-gap arithmetic alone never cuts.
        # Gap is 1.5s (> ACOUSTIC_GAP_MAX_S, so rule 2 is out too).
        words = [w("one", 1.0, 1.5), w("two", 3.0, 3.5)]
        assert_no_cuts(build_cut_plan(words, INERT_SILENCE, DUR))

    def test_gap_below_max_pause_not_tightened(self):
        # fully silent but only 0.5s — under MAX_PAUSE_S, leave it be
        words = [w("one", 1.0, 1.5), w("two", 2.0, 2.5)]
        assert_no_cuts(build_cut_plan(words, [(1.5, 2.0)], DUR))

    def test_pause_removal_clipped_to_silence_pieces(self):
        words = [w("one", 1.0, 1.5), w("two", 4.5, 5.0)]
        plan = build_cut_plan(words, [(1.6, 2.2), (3.0, 4.0)], DUR)
        assert [r.reason for r in plan.removed] == [REASON_SILENCE, REASON_SILENCE]
        assert_spans(
            [(r.start_s, r.end_s) for r in plan.removed],
            [(1.625, 2.2), (3.0, 4.0)],  # window (1.625, 4.375) ∩ silences
        )
        assert_spans(plan.keep_segments, [(0.0, 1.625), (2.2, 3.0), (4.0, DUR)])

    def test_leading_silence_trimmed_to_lead_keep(self):
        words = [w("a", 2.0, 2.5), w("b", 2.6, 3.1)]
        plan = build_cut_plan(words, [(0.0, 2.0)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE
        assert_spans([(removal.start_s, removal.end_s)], [(0.0, 1.7)])
        assert_spans(plan.keep_segments, [(1.7, DUR)])

    def test_leading_within_lead_keep_untouched(self):
        words = [w("a", 0.25, 0.7), w("b", 0.8, 1.3)]
        assert_no_cuts(build_cut_plan(words, [(0.0, 0.25)], DUR))

    def test_trailing_silence_trimmed_to_trail_keep(self):
        words = [w("a", 14.0, 14.5), w("b", 14.6, 15.0)]
        plan = build_cut_plan(words, [(15.0, 20.0)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE
        assert_spans([(removal.start_s, removal.end_s)], [(15.5, 20.0)])
        assert_spans(plan.keep_segments, [(0.0, 15.5)])

    def test_trailing_within_trail_keep_untouched(self):
        words = [w("a", 19.0, 19.3), w("b", 19.4, 19.6)]
        assert_no_cuts(build_cut_plan(words, [(19.6, 20.0)], DUR))


# ---------------------------------------------------------------------------------
# Rule 4 hygiene — MIN_CUT_S drop + merge
# ---------------------------------------------------------------------------------


class TestMergeHygiene:
    def test_sub_min_cut_removals_dropped(self):
        words = [w("one", 1.0, 1.5), w("two", 3.5, 4.0)]
        # silence sliver → 0.1s intersection < MIN_CUT_S → dropped
        assert_no_cuts(build_cut_plan(words, [(2.0, 2.1)], DUR))

    def test_overlapping_removals_merge(self):
        words = [w("so", 1.0, 1.3), w("um", 2.0, 2.3), w("uh", 2.4, 2.7), w("go", 3.4, 3.7)]
        plan = build_cut_plan(words, [], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_FILLER_LEXICAL
        assert_spans([(removal.start_s, removal.end_s)], [(1.88, 2.82)])
        assert plan.time_saved_s == pytest.approx(0.94, abs=1e-9)

    def test_adjacent_removals_merge_keeping_first_reason(self):
        # retake removal (1.62, 2.88) touches the lexical removal (2.88, 3.52)
        # exactly → one merged removal wearing the FIRST-by-time reason.
        words = [w("start", 1.0, 1.5), w("take", 2.0, 2.5), w("um", 3.0, 3.4), w("final", 4.0, 4.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(1, 1)])
        removal = only_removal(plan)
        assert removal.reason == REASON_RETAKE
        assert_spans([(removal.start_s, removal.end_s)], [(1.62, 3.52)])
        assert_spans(plan.keep_segments, [(0.0, 1.62), (3.52, 10.0)])
        assert plan.time_saved_s == pytest.approx(1.9, abs=1e-9)


# ---------------------------------------------------------------------------------
# Safety rails — every bailout_reason
# ---------------------------------------------------------------------------------


def assert_noop_bailout(plan: CutPlan, reason: str, duration: float):
    assert plan.bailout_reason == reason
    assert plan.removed == []
    assert_spans(plan.keep_segments, [(0.0, duration)])
    assert plan.time_saved_s == pytest.approx(0.0, abs=1e-9)


class TestSafetyRails:
    @pytest.mark.parametrize(
        "words",
        [
            [],
            None,
            [{"text": "hi"}],  # missing timestamps
            [{"text": "", "start_s": 1.0, "end_s": 1.5}],  # empty text
        ],
    )
    def test_no_words_bailout(self, words):
        assert_noop_bailout(build_cut_plan(words, [], DUR), BAILOUT_NO_WORDS, DUR)

    def test_clip_too_short_bailout(self):
        plan = build_cut_plan(lexical_fixture(), [], MIN_CLIP_S - 0.1)
        assert_noop_bailout(plan, BAILOUT_CLIP_TOO_SHORT, MIN_CLIP_S - 0.1)

    def test_max_removal_exceeded_bailout(self):
        words = [w("a", 0.5, 1.0), w("b", 9.5, 9.9)]
        plan = build_cut_plan(words, [(1.0, 9.5)], 10.0)  # would remove 8.25s of 10s
        assert_noop_bailout(plan, BAILOUT_MAX_REMOVAL, 10.0)

    def test_retake_spans_share_max_removal_frac(self):
        words = [w("a", 0.5, 1.0), w("b", 2.0, 2.5), w("c", 3.0, 3.5), w("d", 9.0, 9.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(1, 2)])
        assert_noop_bailout(plan, BAILOUT_MAX_REMOVAL, 10.0)

    def test_output_too_short_bailout_defense_in_depth(self, monkeypatch):
        # Unreachable with the shipped constants (see the invariant test below),
        # so widen MAX_REMOVAL_FRAC to prove the rail still holds the line.
        monkeypatch.setattr(silence_cut, "MAX_REMOVAL_FRAC", 0.9)
        words = [w("a", 0.5, 1.0), w("b", 5.3, 5.7)]
        plan = build_cut_plan(words, [(1.0, 5.3)], 6.0)  # removes 4.05s of 6s
        assert_noop_bailout(plan, BAILOUT_OUTPUT_TOO_SHORT, 6.0)

    def test_shipped_constants_guarantee_min_output(self):
        # A plan passing both the clip-length and removal-fraction rails always
        # retains at least MIN_OUTPUT_S — the output rail is defense in depth.
        assert (1 - MAX_REMOVAL_FRAC) * MIN_CLIP_S >= MIN_OUTPUT_S


# ---------------------------------------------------------------------------------
# Retake spans
# ---------------------------------------------------------------------------------


class TestRetakeSpans:
    def test_span_snaps_outward_to_padded_word_boundaries(self):
        words = [w("a", 1.0, 1.5), w("b", 2.0, 2.5), w("c", 3.0, 3.5), w("d", 4.0, 4.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(1, 2)])
        removal = only_removal(plan)
        assert removal.reason == REASON_RETAKE
        assert_spans([(removal.start_s, removal.end_s)], [(1.5 + PAD_S, 4.0 - PAD_S)])
        # covers the abandoned take fully, outward only
        assert removal.start_s <= 2.0 and removal.end_s >= 3.5
        # never mid-word: boundaries sit strictly outside the surviving words
        for surviving in (words[0], words[3]):
            for boundary in (removal.start_s, removal.end_s):
                assert not (surviving["start_s"] < boundary < surviving["end_s"])

    def test_boundary_never_mid_word_when_gap_thinner_than_pad(self):
        # gap a→b is 0.05s < PAD_S: the padded boundary (2.12) would land INSIDE
        # the removed word b — the boundary must clamp to b's own start instead.
        words = [w("a", 1.0, 2.0), w("b", 2.05, 3.0), w("c", 4.0, 4.5)]
        plan = build_cut_plan(words, [], 12.0, retake_spans=[(1, 1)])
        removal = only_removal(plan)
        assert removal.start_s == pytest.approx(2.05, abs=1e-9)
        assert removal.end_s == pytest.approx(4.0 - PAD_S, abs=1e-9)
        assert not (1.0 < removal.start_s < 2.0)  # not inside the kept word a

    def test_span_at_first_word_extends_to_clip_start(self):
        words = [w("a", 0.8, 1.3), w("b", 2.0, 2.5), w("c", 3.0, 3.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(0, 1)])
        removal = only_removal(plan)
        assert_spans([(removal.start_s, removal.end_s)], [(0.0, 3.0 - PAD_S)])
        assert_spans(plan.keep_segments, [(2.88, 10.0)])

    def test_span_at_last_word_extends_to_duration(self):
        words = [w("a", 1.0, 1.5), w("b", 16.0, 16.5), w("c", 17.0, 17.5)]
        plan = build_cut_plan(words, [], DUR, retake_spans=[(2, 2)])
        removal = only_removal(plan)
        assert_spans([(removal.start_s, removal.end_s)], [(16.5 + PAD_S, DUR)])

    def test_invalid_spans_skipped_defensively(self):
        words = [w("a", 1.0, 1.5), w("b", 2.0, 2.5), w("c", 3.0, 3.5)]
        spans = [(5, 9), (2, 1), (-1, 0), (None, 1), ("x", "y")]
        assert_no_cuts(build_cut_plan(words, [], DUR, retake_spans=spans))


# ---------------------------------------------------------------------------------
# remap_words
# ---------------------------------------------------------------------------------


def plan_with_removals(removals: list[tuple[float, float]], duration: float) -> CutPlan:
    removed = [Removal(start_s=lo, end_s=hi, reason=REASON_SILENCE) for lo, hi in removals]
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for lo, hi in removals:
        if lo > cursor:
            keep.append((cursor, lo))
        cursor = hi
    if cursor < duration:
        keep.append((cursor, duration))
    saved = sum(hi - lo for lo, hi in removals)
    return CutPlan(keep_segments=keep, removed=removed, time_saved_s=saved)


class TestRemapWords:
    def test_drops_removed_words_and_shifts_survivors(self):
        plan = plan_with_removals([(2.0, 3.0)], 10.0)
        words = [w("a", 1.0, 1.5), w("gone", 2.2, 2.8), w("b", 4.0, 4.5)]
        remapped = remap_words(words, plan)
        assert [entry["text"] for entry in remapped] == ["a", "b"]
        assert remapped[0]["start_s"] == pytest.approx(1.0, abs=1e-9)
        assert remapped[0]["end_s"] == pytest.approx(1.5, abs=1e-9)
        assert remapped[1]["start_s"] == pytest.approx(3.0, abs=1e-9)
        assert remapped[1]["end_s"] == pytest.approx(3.5, abs=1e-9)

    def test_cumulative_shift_across_multiple_removals(self):
        plan = plan_with_removals([(1.0, 2.0), (5.0, 6.0)], 10.0)
        words = [w("a", 0.5, 0.9), w("b", 3.0, 3.5), w("c", 7.0, 7.5)]
        remapped = remap_words(words, plan)
        assert_spans(
            [(entry["start_s"], entry["end_s"]) for entry in remapped],
            [(0.5, 0.9), (2.0, 2.5), (5.0, 5.5)],
        )

    def test_word_starting_exactly_at_removal_end(self):
        plan = plan_with_removals([(1.0, 2.0)], 10.0)
        remapped = remap_words([w("a", 2.0, 2.5)], plan)
        assert_spans([(remapped[0]["start_s"], remapped[0]["end_s"])], [(1.0, 1.5)])

    def test_word_ending_exactly_at_removal_start_unshifted(self):
        plan = plan_with_removals([(2.0, 3.0)], 10.0)
        remapped = remap_words([w("a", 1.5, 2.0)], plan)
        assert_spans([(remapped[0]["start_s"], remapped[0]["end_s"])], [(1.5, 2.0)])

    def test_word_coinciding_with_removal_dropped(self):
        plan = plan_with_removals([(2.0, 2.5)], 10.0)
        assert remap_words([w("gone", 2.0, 2.5)], plan) == []

    def test_accepts_object_shaped_words(self):
        plan = plan_with_removals([(2.0, 3.0)], 10.0)
        remapped = remap_words([SimpleNamespace(text="hi", start_s=4.0, end_s=4.5)], plan)
        assert_spans([(remapped[0]["start_s"], remapped[0]["end_s"])], [(3.0, 3.5)])

    def test_noop_plan_is_identity(self):
        words = [w("a", 1.0, 1.5), w("b", 2.0, 2.5)]
        remapped = remap_words(words, no_op_plan(DUR))
        assert remapped == [
            {"text": "a", "start_s": 1.0, "end_s": 1.5},
            {"text": "b", "start_s": 2.0, "end_s": 2.5},
        ]

    def test_end_to_end_lexical_cut_then_remap(self):
        words = lexical_fixture()
        plan = build_cut_plan(words, [], DUR)
        remapped = remap_words(words, plan)
        assert [entry["text"] for entry in remapped] == ["so", "lets"]
        assert_spans(
            [(entry["start_s"], entry["end_s"]) for entry in remapped],
            [(1.0, 1.3), (3.0 - 0.64, 3.3 - 0.64)],
        )
        # kept spans keep their exact durations
        assert remapped[1]["end_s"] - remapped[1]["start_s"] == pytest.approx(0.3, abs=1e-9)

    def test_output_is_monotonic(self):
        plan = plan_with_removals([(1.0, 2.0), (4.0, 5.5)], 12.0)
        words = [w(f"w{i}", 2.0 + i, 2.4 + i) for i in range(6) if not (2.0 <= 2.0 + i < 5.5)]
        remapped = remap_words(words, plan)
        starts = [entry["start_s"] for entry in remapped]
        assert starts == sorted(starts)


# ---------------------------------------------------------------------------------
# no_op_plan + plan types
# ---------------------------------------------------------------------------------


class TestNoOpPlan:
    def test_identity_shape(self):
        plan = no_op_plan(12.5)
        assert plan.keep_segments == [(0.0, 12.5)]
        assert plan.removed == []
        assert plan.time_saved_s == 0.0
        assert plan.version == 1
        assert plan.bailout_reason is None

    def test_bailout_reason_passthrough(self):
        assert no_op_plan(8.0, bailout_reason=BAILOUT_NO_WORDS).bailout_reason == BAILOUT_NO_WORDS

    def test_removal_is_frozen(self):
        removal = Removal(start_s=1.0, end_s=2.0, reason=REASON_SILENCE)
        with pytest.raises(dataclasses.FrozenInstanceError):
            removal.start_s = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------------
# Property-style sweep (seeded — deterministic, xdist-safe)
# ---------------------------------------------------------------------------------


def _fully_removed(start: float, end: float, plan: CutPlan) -> bool:
    return any(start >= r.start_s - 1e-9 and end <= r.end_s + 1e-9 for r in plan.removed)


def _covered_by_keep(start: float, end: float, plan: CutPlan) -> bool:
    return any(lo - 1e-6 <= start and end <= hi + 1e-6 for lo, hi in plan.keep_segments)


def test_property_random_layouts_hold_plan_invariants():
    rng = random.Random(20260709)  # fixed literal seed — NEVER randomize collection
    for _ in range(40):
        duration = rng.uniform(8.0, 60.0)
        words: list[dict] = []
        t = rng.uniform(0.0, 1.5)
        while t < duration - 1.0 and len(words) < 60:
            end = min(t + rng.uniform(0.08, 0.6), duration)
            text = rng.choice(
                ["hello", "world", "um", "uh", "ııı", "take", "go", "eee", "ee", "aa"]
            )
            extra = {}
            if rng.random() < 0.3:
                extra["segment_avg_logprob"] = rng.uniform(-2.0, 0.0)
                extra["segment_no_speech_prob"] = rng.uniform(0.0, 1.0)
            words.append(w(text, t, end, **extra))
            t = end + rng.uniform(0.02, 2.0)

        silences: list[tuple[float, float]] = []
        if rng.random() < 0.5 and words[0]["start_s"] > 0.05:
            silences.append((0.0, words[0]["start_s"] * rng.uniform(0.5, 1.0)))
        for prev, nxt in zip(words, words[1:]):
            gap_lo, gap_hi = prev["end_s"], nxt["start_s"]
            if gap_hi - gap_lo > 0.05 and rng.random() < 0.6:
                lo = rng.uniform(gap_lo, gap_hi)
                hi = rng.uniform(lo, gap_hi)
                if hi - lo > 0.01:
                    silences.append((lo, hi))
        if rng.random() < 0.5 and words[-1]["end_s"] < duration - 0.05:
            silences.append((rng.uniform(words[-1]["end_s"], duration), duration))

        retake_spans = None
        if len(words) >= 4 and rng.random() < 0.3:
            i = rng.randrange(0, len(words) - 1)
            retake_spans = [(i, min(len(words) - 1, i + rng.randrange(0, 3)))]

        plan = build_cut_plan(words, silences, duration, retake_spans=retake_spans)

        if plan.bailout_reason is not None:
            assert plan.removed == []
            assert plan.keep_segments == [(0.0, duration)]
            assert plan.time_saved_s == 0.0
            continue

        # keep_segments: sorted, non-overlapping, inside [0, duration]
        for lo, hi in plan.keep_segments:
            assert 0.0 <= lo < hi <= duration + 1e-9
        for (_, prev_hi), (nxt_lo, _) in zip(plan.keep_segments, plan.keep_segments[1:]):
            assert nxt_lo > prev_hi
        # removals: sorted, non-overlapping, each >= MIN_CUT_S, capped total
        for removal in plan.removed:
            assert 0.0 <= removal.start_s < removal.end_s <= duration + 1e-9
            assert removal.end_s - removal.start_s >= MIN_CUT_S - 1e-9
        for r_prev, r_next in zip(plan.removed, plan.removed[1:]):
            assert r_next.start_s > r_prev.end_s
        total_removed = sum(r.end_s - r.start_s for r in plan.removed)
        assert total_removed <= MAX_REMOVAL_FRAC * duration + 1e-9
        assert duration - total_removed >= MIN_OUTPUT_S - 1e-9
        assert plan.time_saved_s == pytest.approx(total_removed, abs=1e-9)
        # keep + removed exactly partitions [0, duration]
        kept_total = sum(hi - lo for lo, hi in plan.keep_segments)
        assert kept_total + total_removed == pytest.approx(duration, abs=1e-6)

        # every surviving word's span sits inside one keep segment
        survivors = [
            word for word in words if not _fully_removed(word["start_s"], word["end_s"], plan)
        ]
        for word in survivors:
            assert _covered_by_keep(word["start_s"], word["end_s"], plan), (word, plan)

        # remap: one entry per survivor, monotonic, durations preserved
        remapped = remap_words(words, plan)
        assert len(remapped) == len(survivors)
        starts = [entry["start_s"] for entry in remapped]
        assert starts == sorted(starts)
        for original, entry in zip(survivors, remapped):
            original_len = original["end_s"] - original["start_s"]
            assert entry["end_s"] - entry["start_s"] == pytest.approx(original_len, abs=1e-6)
